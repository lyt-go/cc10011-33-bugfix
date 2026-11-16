"""用户洞察业务规则：状态流转、字段校验、报告快照与订阅推送联动。

洞察报告每次发布都会追加一份不可变快照（``userinsightsnapshot``）。快照是消息正文的
唯一引用来源：订阅推送只引用快照摘要，不再直接读取会被后续编辑覆盖的报告本体，
因此跨天静默判定发生在哪一版快照上都有据可查。

订阅状态回写（暂停/启用/恢复）通过本模块的 ``sync_subscription_status`` 同步到引用
该订阅的洞察快照，保证订阅列表与消息详情由同一份状态驱动。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "userinsight"
SNAPSHOT_MODULE = "userinsightsnapshot"
REQUIRED_FIELDS = ["报告编号", "分析主题", "目标人群"]
STATUS_ORDER = ["草稿", "生成中", "已发布", "已归档"]
ACTION_RULES = {"提交生成": "生成中", "发布报告": "已发布", "归档报告": "已归档"}
# 只允许向前流转，避免发布后又被“提交生成”打回覆盖已发布的快照。
ACTION_FROM = {
    "提交生成": {"草稿", "生成中"},
    "发布报告": {"草稿", "生成中", "已发布"},
    "归档报告": {"已发布", "已归档"},
}
SNAPSHOT_FIELDS = ["报告编号", "分析主题", "目标人群", "关键指标", "核心发现", "数据区间", "生成时间"]


class Conflict(Exception):
    """状态不允许；路由层转成 409。"""


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class UserinsightService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._decorate(row) for row in rows[start:start + size]], total

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        view = dict(row)
        snapshots = self.snapshots_of(str(row.get("报告编号") or ""))
        view["最新快照"] = snapshots[-1]["洞察版本"] if snapshots else ""
        view["快照数"] = len(snapshots)
        return view

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        view = dict(entry)
        view["快照"] = self.snapshots_of(str(entry.get("报告编号") or ""))
        return view

    def snapshots_of(self, report_no: str) -> list[dict[str, Any]]:
        if not report_no:
            return []
        return [
            item
            for item in store.rows(SNAPSHOT_MODULE)
            if str(item.get("报告编号") or "") == report_no
        ]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {"id": store.next_id(MODULE)}
        for field in SNAPSHOT_FIELDS:
            entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        store.append_row(MODULE, entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"洞察报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于用户洞察可执行范围"
        target = ACTION_RULES[action]
        current = entry.get("status")
        if current not in ACTION_FROM[action]:
            raise Conflict(f"洞察报告当前状态为「{current}」，不能执行「{action}」")

        entry["status"] = target
        entry["pending"] = target in {"生成中", "已发布"}
        entry["abnormal"] = False
        entry["更新时间"] = _now_text()
        store.update_row(MODULE, entry_id, entry)

        if target == "已发布":
            self._publish(entry)
        return entry, f"洞察报告已{action}"

    def _publish(self, entry: dict[str, Any]) -> None:
        """发布时写快照，并沿现有调用链交给订阅推送处理。"""
        # 延迟导入，避免服务之间循环引用。
        from app.services.subscription import SubscriptionService

        publisher = SubscriptionService()
        snapshot = publisher.record_insight_snapshot(entry, source="用户洞察")
        entry["最新快照"] = snapshot.get("洞察版本")
        entry["快照生成时间"] = _now_text()
        store.update_row(MODULE, int(entry["id"]), entry)
        outcome = publisher.on_insight_published(entry, snapshot=snapshot)
        entry["订阅同步结果"] = outcome.get("subscriptions", [])
        store.update_row(MODULE, int(entry["id"]), entry)


def apply_status_fields(snapshot: dict[str, Any], status: str) -> None:
    """把订阅状态写成快照上的可读字段；暂停与启用共用同一套口径。"""
    snapshot["订阅状态"] = status
    snapshot["订阅状态更新时间"] = _now_text()
    if status == "已暂停":
        snapshot["投递开关"] = "暂停"
        snapshot["投递说明"] = "订阅已暂停，暂停期间不产生新的订阅推送结果"
    else:
        snapshot["投递开关"] = "开启" if status == "已启用" else status
        snapshot["投递说明"] = "订阅状态已同步"


def snapshot_targets(subscription: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    target = str(subscription.get("订阅内容") or "").strip()
    report_no = str(snapshot.get("报告编号") or "").strip()
    if not target or not report_no:
        return False
    if target in {"全部洞察", "全部报告", "洞察报告", "*"}:
        return True
    return target == report_no


def sync_subscription_status(subscription: dict[str, Any]) -> int:
    """订阅状态回写：同步到引用该订阅的洞察快照，消息详情随之更新。

    返回受影响快照数。暂停后快照标记为“暂停期间不再投递”，启用后恢复为“已启用”。
    """
    sub_id = int(subscription.get("id") or 0)
    status = str(subscription.get("status") or "")
    affected = 0
    for snapshot in store.rows(SNAPSHOT_MODULE):
        refs = snapshot.get("订阅配置") or []
        if not isinstance(refs, list):
            refs = [refs]
        ref_ids = {int(ref or 0) for ref in refs}
        if sub_id not in ref_ids and not snapshot_targets(subscription, snapshot):
            continue
        apply_status_fields(snapshot, status)
        affected += 1
    return affected
