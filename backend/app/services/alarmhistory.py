"""告警历史业务规则：状态流转、恢复投递与筛选口径都收在这里。

“标记恢复”是告警与订阅推送之间的投递节点：状态先落到「已恢复」，再把这次恢复交给
订阅推送服务；订阅处于暂停状态时不产生新的推送结果（由订阅服务统一判定）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "alarmhistory"
REQUIRED_FIELDS = ["记录编号", "告警规则", "触发时间"]
STATUS_ORDER = ["草稿", "已触发", "已确认", "已恢复", "已关闭"]
ACTION_RULES = {"确认告警": "已触发", "标记恢复": "已恢复", "关闭告警": "已关闭"}
ACTION_FROM = {
    "确认告警": {"草稿", "已触发", "已确认"},
    "标记恢复": {"已触发", "已确认", "已恢复"},
    "关闭告警": {"已恢复", "已确认", "已关闭"},
}
NEGATIVE_ACTIONS = []


class Conflict(Exception):
    """状态不允许；路由层转成 409。"""


class AlarmhistoryService:
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
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._decorate(entry)

    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        view = dict(entry)
        pushes = [
            row
            for row in store.rows("subscriptionpush")
            if row.get("洞察报告") == str(entry.get("记录编号") or "")
        ]
        view["订阅推送记录"] = [int(row.get("id") or 0) for row in pushes]
        view["投递成功数"] = sum(1 for row in pushes if row.get("状态") == "已投递")
        return view

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        store.append_row(MODULE, entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"告警记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于告警历史可执行范围"
        target = ACTION_RULES[action]
        current = entry.get("status")
        if current not in ACTION_FROM[action]:
            raise Conflict(f"告警记录当前状态为「{current}」，不能执行「{action}」")

        entry["status"] = target
        entry["pending"] = target in {"已触发", "已确认"}
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if target == "已恢复":
            entry["恢复时间"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        result = entry
        store.update_row(MODULE, entry_id, entry)

        if target == "已恢复":
            self._push_recovered(entry)
            result = store.find(MODULE, entry_id) or entry
        return result, f"告警记录已{action}"

    def _push_recovered(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """沿现有调用链把恢复事件交给订阅推送；订阅侧负责判定暂停与静默。"""
        from app.services.subscription import SubscriptionService

        publisher = SubscriptionService()
        outcome = publisher.on_alarm_recovered(entry)
        record_ids: list[int] = []
        for item in outcome:
            record_ids.extend(int(record_id) for record_id in item.get("记录", []) or [])
        # 重复“标记恢复”时订阅侧会返回空结果，这里保留上一次的投递记录不覆盖。
        if record_ids or not entry.get("订阅推送记录"):
            entry["订阅推送结果"] = outcome
            entry["订阅推送记录"] = record_ids
            entry["订阅推送数"] = len(record_ids)
            store.update_row(MODULE, int(entry["id"]), entry)
        return outcome

