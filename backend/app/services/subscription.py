"""订阅推送业务规则：状态流转、字段校验、归属控制、投递幂等与推送合并。

状态只允许「草稿 → 订阅评审中 → 已启用 ↔ 已暂停」，并且只允许：

    * 草稿/评审中/已启用/已暂停 提交订阅评审（回到评审中，不产生推送结果）
    * 草稿/评审中/已暂停 启用订阅
    * 已启用 暂停订阅

暂停后不再产生新的订阅推送结果：调度入口、告警恢复、洞察报告同步都会先看订阅状态；
恢复（启用）只重算下次推送时间，不补发暂停期间的消息。

投递幂等键 = 内容版本（``报告编号@版本``）+ 接收人 + 推送渠道。并发触发同一版本时，
仓库层 ``insert_unique`` 保证只有一次成功创建，其余请求复用已有记录并标记为重复触发。

静默判定同时看接收人本地时区与渠道统一时区，任一命中即静默；结论和依据随每条投递记录
一起落库，保证「静默判定」与「下次推送时间」都能回到具体记录上追溯。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from app.services import schedule
from app.store import store

MODULE = "subscription"
PUSH_MODULE = "subscriptionpush"
PENDING_MODULE = "pendinganalysis"
INSIGHT_SNAPSHOT_MODULE = "userinsightsnapshot"
ALARM_PUSH_INDEX = "alarmpushindex"

REQUIRED_FIELDS = ["订阅编号", "订阅内容", "接收人"]
CONFIG_FIELDS = [
    "订阅编号",
    "订阅内容",
    "接收人",
    "接收人账号",
    "接收人时区",
    "推送渠道",
    "推送频率",
    "数据粒度",
    "静默时段",
    "渠道时区",
    "归属人",
    "共享渠道",
]
STATUS_ORDER = ["草稿", "订阅评审中", "已启用", "已暂停"]
ACTION_RULES = {"提交订阅评审": "订阅评审中", "启用订阅": "已启用", "暂停订阅": "已暂停"}
# 每个动作允许的前置状态。核心约束：启用/暂停只能从「订阅评审中」发生，
# 草稿必须先送审；「已暂停」只能恢复为启用，不能绕过评审直接回到可投递状态。
ACTION_FROM = {
    "提交订阅评审": {"草稿", "订阅评审中", "已暂停"},
    "启用订阅": {"订阅评审中", "已暂停"},
    "暂停订阅": {"已启用"},
}

DEFAULT_SHARED_CHANNELS = {"企业微信", "短信", "邮件", "Webhook"}
ADMIN_OPERATORS = {"值班管理员", "管理员", "admin"}
STATUS_PUSHABLE = "已启用"
ALL_CONTENT_TARGETS = {"全部内容", "*"}
ALL_INSIGHT_TARGETS = {"全部洞察", "全部报告", "洞察报告"} | ALL_CONTENT_TARGETS
ALL_ALARM_TARGETS = {"全部告警", "全部告警恢复"} | ALL_CONTENT_TARGETS
# 编辑时显式提交空值也不能覆盖的字段：这四个变空正是历史故障的直接表现。
CORE_PROTECTED_FIELDS = {"接收人", "推送渠道", "静默时段", "下次推送时间"}


class PermissionDenied(Exception):
    """越权操作；路由层转成 403。"""


class Conflict(Exception):
    """状态或唯一性冲突；路由层转成 409。"""


class ValidationFailed(Exception):
    """字段校验失败；路由层按缺失字段给出 400 说明。"""

    def __init__(self, message: str, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = missing or []


def _text(value: Any) -> str:
    return str(value or "").strip()


def content_digest(*parts: Any) -> str:
    joined = "|".join(_text(part) for part in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


class SubscriptionService:
    # ================================================================== 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        operator: str | None = None,
        include_shared: bool = True,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("订阅编号", "")) or keyword in str(row.get("订阅内容", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if operator:
            rows = [row for row in rows if self._visible(row, operator, include_shared)]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._decorate(row, operator) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int, operator: str | None = None) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        if operator and not self._visible(entry, operator, include_shared=True):
            raise PermissionDenied(
                f"订阅配置 {entry_id} 不属于当前用户（{operator}），且不是共享渠道"
            )
        return self._decorate(entry, operator)

    def _decorate(self, entry: dict[str, Any], operator: str | None) -> dict[str, Any]:
        view = dict(entry)
        evaluation = entry.get("静默判定记录") or {}
        view["静默判定"] = {
            key: evaluation.get(key)
            for key in (
                "接收人本地时区",
                "渠道统一时区",
                "接收人本地时间",
                "渠道时间",
                "是否静默",
                "冲突",
                "冲突处理",
                "判定依据",
                "判定时区",
                "静默结束时间",
            )
            if key in evaluation
        }
        view["下次推送时间"] = entry.get("下次推送时间") or ""
        view["暂停后不补发"] = entry.get("status") == "已暂停"
        view["共享只读"] = not self.can_write(entry, operator)
        view["可写"] = self.can_write(entry, operator)
        return view

    # ================================================================== 归属
    def owner_of(self, entry: dict[str, Any]) -> str:
        return _text(entry.get("归属人")) or _text(entry.get("接收人"))

    def is_shared_channel(self, entry: dict[str, Any]) -> bool:
        flag = entry.get("共享渠道")
        if isinstance(flag, bool):
            return flag
        text = _text(flag).lower()
        if text in {"是", "true", "1", "yes"}:
            return True
        if text in {"否", "false", "0", "no"}:
            return False
        return _text(entry.get("推送渠道")) in DEFAULT_SHARED_CHANNELS

    def _visible(self, entry: dict[str, Any], operator: str, include_shared: bool) -> bool:
        if operator in ADMIN_OPERATORS:
            return True
        if self.owner_of(entry) == operator:
            return True
        return include_shared and self.is_shared_channel(entry)

    def can_write(self, entry: dict[str, Any], operator: str | None) -> bool:
        if not operator or operator in ADMIN_OPERATORS:
            return True
        owner = self.owner_of(entry)
        # 历史数据没有归属人时不做越权拦截，避免旧记录无法维护。
        if not owner:
            return True
        return owner == operator or _text(entry.get("接收人账号")) == operator

    def _ensure_writable(self, entry: dict[str, Any], operator: str | None) -> None:
        if not self.can_write(entry, operator):
            raise PermissionDenied(
                f"订阅配置 {entry.get('订阅编号') or entry.get('id')} 归属 {self.owner_of(entry)}，"
                "共享渠道对非本人只读，不能修改他人订阅"
            )

    # ================================================================== 登记与编辑
    def create_entry(
        self,
        values: dict[str, Any],
        *,
        operator: str | None = None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not _text(values.get(field))]
        if missing:
            return None, missing
        self._validate_values(values)

        entry: dict[str, Any] = {"id": store.next_id(MODULE)}
        for field in CONFIG_FIELDS:
            entry[field] = values.get(field)
        entry["订阅编号"] = _text(entry.get("订阅编号"))
        entry["推送频率"] = schedule.normalize_frequency(entry.get("推送频率"))
        entry["推送渠道"] = _text(entry.get("推送渠道")) or "邮件"
        entry["归属人"] = _text(entry.get("归属人")) or _text(operator) or _text(entry.get("接收人"))
        if not _text(entry.get("共享渠道")):
            entry["共享渠道"] = "是" if entry["推送渠道"] in DEFAULT_SHARED_CHANNELS else "否"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["修订号"] = 1
        entry["字段完整"] = bool(_text(entry.get("接收人")) and _text(entry.get("推送渠道")))
        entry["静默判定记录"] = {}
        entry["下次推送时间"] = ""
        store.append_row(MODULE, entry)
        store.checkpoint(MODULE)
        return entry, []

    def _validate_values(self, values: dict[str, Any]) -> None:
        """校验会影响调度口径的字段，非法值给出可读原因而不是悄悄回落。"""
        frequency = _text(values.get("推送频率"))
        if frequency:
            known = (
                frequency in schedule.FREQUENCY_MINUTES
                or frequency.lower() in schedule.FREQUENCY_ALIASES
            )
            if not known:
                raise ValidationFailed(
                    f"推送频率「{frequency}」无法识别，可选：{'、'.join(schedule.FREQUENCY_MINUTES)}"
                )
        quiet = _text(values.get("静默时段"))
        if quiet and schedule.parse_quiet_window(quiet) is None:
            raise ValidationFailed(
                f"静默时段「{quiet}」格式应为 HH:MM-HH:MM，跨天时段直接写 22:00-07:00"
            )

    def update_entry(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """编辑订阅配置：只覆盖显式提交的字段，未提交的字段保持原值。

        旧实现只写回必填字段，一次编辑就会把接收人/推送渠道/静默时段/下次推送时间清空；
        这里改为按字段合并，显式提交空值的受保护字段会保留原值并记录到「保留字段」。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            raise LookupError(f"订阅配置 {entry_id} 不存在或已归档")
        self._ensure_writable(entry, operator)
        self._validate_values(values)

        preserved: list[str] = []
        for field, value in values.items():
            if field in {"action", "status", "id", "pending", "abnormal", "修订号"}:
                continue
            if field not in CONFIG_FIELDS:
                entry[field] = value
                continue
            if value is None or str(value).strip() == "":
                if field in CORE_PROTECTED_FIELDS and _text(entry.get(field)):
                    preserved.append(field)
                continue
            entry[field] = schedule.normalize_frequency(value) if field == "推送频率" else value

        missing = [field for field in REQUIRED_FIELDS if not _text(entry.get(field))]
        if missing:
            raise ValidationFailed(f"缺少必填字段：{'、'.join(missing)}", missing)

        entry["修订号"] = int(entry.get("修订号") or 1) + 1
        entry["字段完整"] = bool(_text(entry.get("接收人")) and _text(entry.get("推送渠道")))
        entry["最近编辑时间"] = datetime.now(schedule.channel_zone(entry)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        if preserved:
            entry["保留字段"] = preserved
        # 编辑本身不改变排期，只有三种情况才重算下次推送时间：
        # 原本为空、有效修改了推送频率或静默时段。避免一次（含清空提交的）无关编辑把已到期的推送挪后。
        schedule_fields_changed = any(
            field in values and _text(values.get(field))
            for field in ("推送频率", "静默时段")
        )
        if entry.get("status") == STATUS_PUSHABLE and (
            not _text(entry.get("下次推送时间")) or schedule_fields_changed
        ):
            next_time, schedule_info = schedule.next_push_time(
                entry, datetime.now(schedule.channel_zone(entry))
            )
            entry["下次推送时间"] = next_time
            entry["静默判定记录"] = schedule_info.get("静默判定", {})
        store.update_row(MODULE, entry_id, entry)
        store.checkpoint(MODULE)
        return entry

    # ================================================================== 状态流转
    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        operator: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"订阅配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于订阅推送可执行范围"
        self._ensure_writable(entry, operator)

        target = ACTION_RULES[action]
        current = entry.get("status")
        if current not in ACTION_FROM[action]:
            raise Conflict(f"订阅配置当前状态为「{current}」，不能执行「{action}」")
        if action == "提交订阅评审" and not _text(entry.get("接收人")):
            raise ValidationFailed("接收人不能为空，无法提交评审", ["接收人"])

        entry["status"] = target
        entry["pending"] = target in {"订阅评审中", "已启用"}
        entry["abnormal"] = False
        now = datetime.now(schedule.channel_zone(entry))

        if target == STATUS_PUSHABLE:
            # 启用或从暂停恢复：只重算下次推送时间，不补发暂停期间的消息。
            next_time, schedule_info = schedule.next_push_time(entry, now)
            entry["下次推送时间"] = next_time
            entry["静默判定记录"] = schedule_info.get("静默判定", {})
            entry["恢复重算时间"] = now.strftime("%Y-%m-%d %H:%M:%S")
            entry["暂停期间不补发"] = current == "已暂停"
            if current == "已暂停":
                entry["上次暂停时间"] = entry.get("暂停时间") or entry.get("上次暂停时间", "")
                entry["暂停时间"] = ""
        elif target == "已暂停":
            entry["暂停时间"] = now.strftime("%Y-%m-%d %H:%M:%S")
            entry["暂停前下次推送时间"] = entry.get("下次推送时间") or ""
            entry["暂停原因"] = "人工暂停"
        else:
            entry["提交评审时间"] = now.strftime("%Y-%m-%d %H:%M:%S")

        store.update_row(MODULE, entry_id, entry)
        store.checkpoint(MODULE)
        self._sync_insight_snapshots(entry)
        return entry, f"订阅配置已{action}"

    def _sync_insight_snapshots(self, entry: dict[str, Any]) -> None:
        """另一个模块（用户洞察）的消息随订阅状态回写同步更新。"""
        from app.services import userinsight

        userinsight.sync_subscription_status(entry)

    # ================================================================== 待推送数据
    def list_pending_analysis(
        self,
        *,
        subscription_id: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(PENDING_MODULE)
        if subscription_id is not None:
            rows = [row for row in rows if int(row.get("订阅配置") or 0) == subscription_id]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    # ================================================================== 合并与推送
    def merge_pending(self, config: dict[str, Any], operator: str | None = None) -> dict[str, Any]:
        """按频率窗口合并该订阅的待推送分析数据。"""
        self._ensure_writable(config, operator)
        rows = [
            row
            for row in store.rows(PENDING_MODULE)
            if int(row.get("订阅配置") or 0) == int(config.get("id") or 0)
            and row.get("状态", "待推送") == "待推送"
        ]
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            generated = schedule.parse_now(row.get("生成时间"), schedule.channel_zone(config))
            window = schedule.frequency_window(config, generated)
            groups.setdefault(window, []).append(row)
        merged = [
            {
                "频率窗口": window,
                "频率": schedule.normalize_frequency(config.get("推送频率")),
                "条数": len(items),
                "洞察报告": sorted({str(item.get("报告编号")) for item in items}),
                "洞察版本": sorted({str(item.get("洞察版本")) for item in items}),
                "待推送数据ID": sorted(int(item.get("id") or 0) for item in items),
            }
            for window, items in sorted(groups.items())
        ]
        return {
            "订阅配置": config.get("id"),
            "频率": schedule.normalize_frequency(config.get("推送频率")),
            "合并前条数": len(rows),
            "合并后批次数": len(merged),
            "批次": merged,
        }

    def dispatch(
        self,
        subscription_id: int,
        *,
        now: Any = None,
        operator: str | None = None,
        due_only: bool = True,
        contents: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """按频率合并并推送订阅消息，落一条可追溯的订阅推送数据记录。

        三道闸门依次是：订阅状态、静默判定、下次推送时间。``due_only=False`` 只放宽
        时间闸门（事件触发），不会放宽暂停与静默——这两条必须始终生效。
        """
        config = store.find(MODULE, subscription_id)
        if config is None:
            raise LookupError(f"订阅配置 {subscription_id} 不存在或已归档")
        self._ensure_writable(config, operator)

        status = config.get("status")
        moment = schedule.parse_now(now, schedule.channel_zone(config))
        if status != STATUS_PUSHABLE:
            return {
                "pushed": False,
                "reason": f"订阅状态为「{status}」，只有已启用订阅才产生新的订阅推送结果",
                "records": [],
                "messages": [],
            }

        merged = self.merge_pending(config, operator)
        candidates = contents if contents is not None else self._candidate_contents(config)
        evaluation = schedule.evaluate_silence(config, moment)

        if evaluation["是否静默"]:
            # 静默期间不投递、也不落下“已投递”记录；数据留在待推送里等静默结束。
            next_time, schedule_info = schedule.next_push_time(config, moment)
            config["下次推送时间"] = next_time
            config["静默判定记录"] = schedule_info.get("静默判定", {})
            config["上次静默跳过时间"] = moment.strftime("%Y-%m-%d %H:%M:%S")
            store.update_row(MODULE, subscription_id, config)
            return {
                "pushed": False,
                "reason": (
                    f"命中静默时段的统一判定（{evaluation['判定依据']}），"
                    f"应在 {evaluation['静默结束时间']} 之后再推送"
                ),
                "evaluation": schedule.public_evaluation(evaluation),
                "merged": merged,
                "next_push_time": next_time,
                "records": [],
                "messages": [],
            }

        due_text = _text(config.get("下次推送时间"))
        if due_only and due_text:
            due_moment = schedule.parse_now(due_text, schedule.channel_zone(config))
            if moment < due_moment:
                return {
                    "pushed": False,
                    "reason": f"未到下次推送时间 {due_text}",
                    "evaluation": schedule.public_evaluation(evaluation),
                    "merged": merged,
                    "records": [],
                    "messages": [],
                }

        records: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        skipped = 0
        handled_versions: set[str] = set()
        for content in candidates:
            delivery, created = self._deliver(config, content, moment, evaluation)
            handled_versions.add(_text(delivery.get("内容版本")))
            if created:
                records.append(delivery)
                messages.append(self._message_payload(config, content, delivery, merged))
            else:
                skipped += 1

        next_time, schedule_info = schedule.next_push_time(config, moment)
        config["下次推送时间"] = next_time
        config["上次推送时间"] = moment.strftime("%Y-%m-%d %H:%M:%S")
        config["上次推送结果"] = "已推送" if records else "无新内容"
        config["静默判定记录"] = schedule_info.get("静默判定", {})
        store.update_row(MODULE, subscription_id, config)
        self._mark_pending_sent(config, handled_versions)
        store.checkpoint(PUSH_MODULE)

        if records:
            reason = ""
        elif skipped:
            reason = "同一内容版本已投递，重复触发被合并，未重复打扰接收人"
        else:
            reason = "没有匹配的待推送内容版本"
        return {
            "pushed": bool(records),
            "created": len(records),
            "skipped_duplicate": skipped,
            "merged": merged,
            "next_push_time": next_time,
            "evaluation": schedule.public_evaluation(evaluation),
            "records": records,
            "messages": messages,
            "reason": reason,
        }

    # ================================================================== 内容选择
    def _candidate_contents(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        """按订阅内容取“每个报告最新的一次快照”，保证同一版本只投一次。"""
        target = _text(config.get("订阅内容"))
        snapshots = [
            item
            for item in store.rows(INSIGHT_SNAPSHOT_MODULE)
            if _text(item.get("报告编号")) == target or target in ALL_INSIGHT_TARGETS
        ]
        if not snapshots:
            # 没有快照时退回报告本体，保证示例数据也能跑通（版本按 @v0 计）。
            snapshots = [
                {
                    "报告编号": _text(item.get("报告编号")),
                    "洞察版本": f"{_text(item.get('报告编号'))}@v0",
                    "快照摘要": _text(item.get("核心发现")),
                    "核心发现": _text(item.get("核心发现")),
                    "关键指标": _text(item.get("关键指标")),
                    "生成时间": _text(item.get("生成时间")),
                    "来源": "用户洞察（未快照）",
                    "来源类型": "洞察报告",
                }
                for item in store.rows("userinsight")
                if _text(item.get("报告编号")) == target or target in ALL_INSIGHT_TARGETS
            ]
        latest: dict[str, dict[str, Any]] = {}
        for item in snapshots:
            report_no = _text(item.get("报告编号"))
            current = latest.get(report_no)
            if current is None or int(item.get("版本序号") or 0) >= int(current.get("版本序号") or 0):
                latest[report_no] = item
        return sorted(latest.values(), key=lambda item: _text(item.get("报告编号")))

    def _deliver(
        self,
        config: dict[str, Any],
        content: dict[str, Any],
        moment: datetime,
        evaluation: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        report_no = _text(content.get("报告编号"))
        version = _text(content.get("洞察版本")) or f"{report_no}@v0"
        recipient = _text(config.get("接收人"))
        channel = _text(config.get("推送渠道"))

        delivery = {
            "订阅配置": int(config.get("id") or 0),
            "订阅编号": _text(config.get("订阅编号")),
            "订阅内容": _text(config.get("订阅内容")),
            "接收人": recipient,
            "推送渠道": channel,
            "洞察报告": report_no,
            "洞察版本": version,
            "内容编号": report_no,
            "内容版本": version,
            "内容哈希": content_digest(
                version, content.get("核心发现"), content.get("关键指标")
            ),
            "快照摘要": _text(content.get("快照摘要")) or _text(content.get("核心发现")),
            "快照生成时间": _text(content.get("生成时间")) or _text(content.get("快照生成时间")),
            "来源": _text(content.get("来源")) or "用户洞察",
            "来源类型": _text(content.get("来源类型")) or "洞察报告",
            "状态": "已投递",
            "频率": schedule.normalize_frequency(config.get("推送频率")),
            "触发时间": moment.strftime("%Y-%m-%d %H:%M:%S"),
            "静默判定": schedule.public_evaluation(evaluation),
            "下次推送时间": "",
            "重试次数": 0,
            "重复触发": False,
            "消息摘要": f"【{_text(content.get('来源类型')) or '洞察报告'} {report_no}】"
            + (_text(content.get("快照摘要")) or _text(content.get("核心发现")) or "内容已更新"),
            "渠道时区": getattr(schedule.channel_zone(config), "key", ""),
            "接收人时区": schedule.recipient_zone_name(config),
        }

        existing, created = store.insert_unique(
            PUSH_MODULE,
            key=lambda row: (
                _text(row.get("内容版本")),
                _text(row.get("接收人")),
                _text(row.get("推送渠道")),
            ),
            row=delivery,
        )
        if created:
            next_time, _ = schedule.next_push_time(config, moment)
            existing["下次推送时间"] = next_time
            return existing, True

        if existing.get("状态") in {"投递失败", "待重试"}:
            # 这个版本此前没有成功送达：调度到点时按重试处理，而不是当作重复触发丢弃。
            store.update_row(
                PUSH_MODULE,
                int(existing["id"]),
                {
                    "状态": "已投递",
                    "重试次数": int(existing.get("重试次数") or 0) + 1,
                    "最近重试时间": moment.strftime("%Y-%m-%d %H:%M:%S"),
                    "触发时间": moment.strftime("%Y-%m-%d %H:%M:%S"),
                    "静默判定": schedule.public_evaluation(evaluation),
                    "下次推送时间": schedule.next_push_time(config, moment)[0],
                },
            )
            return existing, True

        # 并发或重复触发：复用已有记录，不重复打扰接收人。
        existing["重复触发"] = True
        existing["重复触发次数"] = int(existing.get("重复触发次数") or 0) + 1
        existing["最近重复触发时间"] = moment.strftime("%Y-%m-%d %H:%M:%S")
        store.update_row(PUSH_MODULE, int(existing["id"]), existing)
        return existing, False

    def _message_payload(
        self,
        config: dict[str, Any],
        content: dict[str, Any],
        delivery: dict[str, Any],
        merged: dict[str, Any],
    ) -> dict[str, Any]:
        windows = [item for item in merged.get("批次", []) if item["条数"] > 0]
        lines = [f"{item['频率窗口']} 共 {item['条数']} 份内容" for item in windows]
        body = _text(content.get("快照摘要")) or _text(content.get("核心发现")) or "内容已更新"
        return {
            "投递记录ID": delivery.get("id"),
            "接收人": delivery.get("接收人"),
            "推送渠道": delivery.get("推送渠道"),
            "标题": f"订阅推送：{_text(config.get('订阅内容'))}",
            "正文": body,
            "洞察报告": delivery.get("洞察报告"),
            "内容版本": delivery.get("内容版本"),
            "引用快照": delivery.get("快照摘要"),
            "合并批次": lines,
            "静默判定": delivery.get("静默判定"),
            "下次推送时间": delivery.get("下次推送时间"),
        }

    def _mark_pending_sent(self, config: dict[str, Any], versions: set[str]) -> None:
        """只把本次真正处理过的版本标记为已推送，避免误伤其它待推送数据。"""
        if not versions:
            return
        for row in store.rows(PENDING_MODULE):
            if int(row.get("订阅配置") or 0) != int(config.get("id") or 0):
                continue
            if _text(row.get("洞察版本")) not in versions:
                continue
            if row.get("状态", "待推送") != "待推送":
                continue
            store.update_row(
                PENDING_MODULE,
                int(row.get("id") or 0),
                {
                    "状态": "已推送",
                    "推送时间": datetime.now(schedule.channel_zone(config)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                },
            )

    def queue_event(
        self,
        config: dict[str, Any],
        content: dict[str, Any],
        *,
        moment: datetime,
    ) -> dict[str, Any]:
        """非实时订阅收到新内容时，只登记待推送数据，等调度按频率合并。

        实时订阅直接走 ``dispatch``，但仍受静默与暂停约束。
        """
        report_no = _text(content.get("报告编号"))
        version = _text(content.get("洞察版本"))
        existing = store.find_one_by(
            PENDING_MODULE, 订阅配置=int(config.get("id") or 0), 洞察版本=version
        )
        if existing is None:
            store.append_row(
                PENDING_MODULE,
                {
                    "订阅配置": int(config.get("id") or 0),
                    "报告编号": report_no,
                    "洞察版本": version,
                    "生成时间": _text(content.get("生成时间"))
                    or moment.strftime("%Y-%m-%d %H:%M"),
                    "数据粒度": _text(config.get("数据粒度")) or "按报告",
                    "来源": _text(content.get("来源")) or "用户洞察",
                    "状态": "待推送",
                },
            )
        queued_merged = self.merge_pending(config)
        if not _text(config.get("下次推送时间")):
            next_time, schedule_info = schedule.next_push_time(config, moment)
            config["下次推送时间"] = next_time
            config["静默判定记录"] = schedule_info.get("静默判定", {})
            store.update_row(MODULE, int(config.get("id") or 0), config)
        return queued_merged

    # ================================================================== 投递记录
    def list_deliveries(
        self,
        *,
        subscription_id: int | None = None,
        insight_version: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(PUSH_MODULE)
        if subscription_id is not None:
            rows = [row for row in rows if int(row.get("订阅配置") or 0) == subscription_id]
        if insight_version:
            rows = [
                row
                for row in rows
                if row.get("洞察版本") == insight_version or row.get("内容版本") == insight_version
            ]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_delivery(self, record_id: int) -> dict[str, Any] | None:
        return store.find(PUSH_MODULE, record_id)

    def retry_delivery(self, record_id: int, *, operator: str | None = None) -> dict[str, Any]:
        """失败消息可重试：只允许失败记录重试，成功记录不会再打扰接收人。"""
        record = store.find(PUSH_MODULE, record_id)
        if record is None:
            raise LookupError(f"订阅推送记录 {record_id} 不存在")
        config = store.find(MODULE, int(record.get("订阅配置") or 0))
        if config is not None:
            self._ensure_writable(config, operator)
        if record.get("状态") == "已投递":
            return {
                "ok": True,
                "message": "该消息已成功投递，重试被忽略以避免重复打扰",
                "record": record,
                "duplicated": True,
            }
        if record.get("状态") not in {"投递失败", "待重试"}:
            return {
                "ok": False,
                "message": f"记录状态为「{record.get('状态')}」，不支持重试",
                "record": record,
            }

        duplicate = store.find_one_by(
            PUSH_MODULE,
            内容版本=record.get("内容版本"),
            接收人=record.get("接收人"),
            推送渠道=record.get("推送渠道"),
            状态="已投递",
        )
        if duplicate is not None and int(duplicate.get("id") or 0) != record_id:
            record["状态"] = "已合并"
            record["合并到"] = duplicate.get("id")
            record["最近重试时间"] = datetime.now(schedule.channel_zone(record)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            store.update_row(PUSH_MODULE, record_id, record)
            return {
                "ok": True,
                "message": "同一版本已有成功投递，本次重试合并，不重复打扰",
                "record": record,
                "duplicated": True,
            }

        record["状态"] = "已投递"
        record["重试次数"] = int(record.get("重试次数") or 0) + 1
        record["最近重试时间"] = datetime.now(schedule.channel_zone(record)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        store.update_row(PUSH_MODULE, record_id, record)
        return {"ok": True, "message": "失败消息已重试投递", "record": record, "duplicated": False}

    def mark_delivery_failed(
        self, record_id: int, reason: str, *, operator: str | None = None
    ) -> dict[str, Any]:
        record = store.find(PUSH_MODULE, record_id)
        if record is None:
            raise LookupError(f"订阅推送记录 {record_id} 不存在")
        config = store.find(MODULE, int(record.get("订阅配置") or 0))
        if config is not None:
            self._ensure_writable(config, operator)
        store.update_row(
            PUSH_MODULE,
            record_id,
            {
                "状态": "投递失败",
                "失败原因": reason,
                "失败时间": datetime.now(schedule.channel_zone(record)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            },
        )
        return record

    # ================================================================== 告警恢复联动
    def on_alarm_recovered(self, alarm: dict[str, Any], *, now: Any = None) -> list[dict[str, Any]]:
        """告警恢复后向关联订阅投递；暂停订阅不产生新的推送结果。

        同一条告警只处理一次（``alarmpushindex``），重复点“标记恢复”不会重复打扰。
        """
        alarm_key = _text(alarm.get("记录编号")) or f"ALARM-{alarm.get('id')}"
        if store.find_one_by(ALARM_PUSH_INDEX, 告警记录=alarm_key) is not None:
            return []
        moment = schedule.parse_now(now, schedule.channel_zone({"推送渠道": "邮件"}))
        recovered_at = _text(alarm.get("恢复时间")) or moment.strftime("%Y-%m-%d %H:%M")
        content = {
            "报告编号": alarm_key,
            "洞察版本": f"{alarm_key}@恢复-{recovered_at}",
            "快照摘要": (
                f"告警已恢复：{_text(alarm.get('告警规则')) or alarm_key}，"
                f"当前值 {_text(alarm.get('当前值')) or '—'}，"
                f"阈值对比 {_text(alarm.get('阈值对比')) or '—'}，"
                f"持续时长 {_text(alarm.get('持续时长')) or '—'}"
            ),
            "生成时间": recovered_at,
            "来源": "告警历史",
            "来源类型": "告警恢复",
            "版本序号": 1,
        }
        outcome: list[dict[str, Any]] = []
        for config in store.rows(MODULE):
            if not self._alarm_matches(config, alarm):
                continue
            if config.get("status") != STATUS_PUSHABLE:
                outcome.append(
                    {
                        "订阅配置": config.get("id"),
                        "结果": "跳过",
                        "原因": f"订阅状态为「{config.get('status')}」，暂停后不产生新的推送结果",
                    }
                )
                continue
            if schedule.normalize_frequency(config.get("推送频率")) != "实时":
                self.queue_event(config, content, moment=moment)
                outcome.append(
                    {
                        "订阅配置": config.get("id"),
                        "结果": "已登记待推送",
                        "原因": f"频率为{schedule.normalize_frequency(config.get('推送频率'))}，待按频率合并推送",
                    }
                )
                continue
            try:
                pushed = self.dispatch(
                    int(config.get("id") or 0),
                    now=moment,
                    due_only=False,
                    contents=[content],
                )
            except (PermissionDenied, LookupError):
                continue
            outcome.append(
                {
                    "订阅配置": config.get("id"),
                    "结果": "已投递" if pushed.get("pushed") else "未投递",
                    "原因": pushed.get("reason", ""),
                    "记录": [record.get("id") for record in pushed.get("records", [])],
                }
            )
        store.append_row(
            ALARM_PUSH_INDEX,
            {
                "告警记录": alarm_key,
                "处理时间": moment.strftime("%Y-%m-%d %H:%M:%S"),
                "处理结果": outcome,
            },
        )
        return outcome

    def _alarm_matches(self, config: dict[str, Any], alarm: dict[str, Any]) -> bool:
        target = _text(config.get("订阅内容"))
        rule = _text(alarm.get("告警规则"))
        record = _text(alarm.get("记录编号"))
        if not target:
            return False
        if target in ALL_ALARM_TARGETS or target in {rule, record}:
            return True
        return bool(rule and rule in target) or bool(target and target in rule)

    # ================================================================== 洞察同步联动
    def on_insight_published(
        self,
        insight: dict[str, Any],
        *,
        snapshot: dict[str, Any] | None = None,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """洞察报告发布后同步订阅推送：登记快照归属，并把新版本交给订阅调度。

        实时订阅立即投递（仍受静默与暂停约束）；其它频率登记为待推送数据，
        等下一次调度按频率窗口合并后统一推送。
        """
        report_no = _text(insight.get("报告编号"))
        if not report_no:
            return {"snapshot": None, "deliveries": [], "subscriptions": []}
        if snapshot is None:
            snapshot = self.record_insight_snapshot(insight, source="用户洞察")

        moment = schedule.parse_now(None, schedule.channel_zone({"推送渠道": "邮件"}))
        deliveries: list[dict[str, Any]] = []
        evaluations: list[dict[str, Any]] = []
        for config in store.rows(MODULE):
            target = _text(config.get("订阅内容"))
            if not (target == report_no or target in ALL_INSIGHT_TARGETS):
                continue
            refs = [int(item or 0) for item in (snapshot.get("订阅配置") or [])]
            sub_id = int(config.get("id") or 0)
            if sub_id not in refs:
                snapshot["订阅配置"] = refs + [sub_id]
            # 快照上的订阅状态与订阅列表保持同一口径，消息详情据此展示是否会被投递。
            from app.services.userinsight import apply_status_fields

            apply_status_fields(snapshot, _text(config.get("status")))

            if config.get("status") != STATUS_PUSHABLE:
                evaluations.append(
                    {
                        "订阅配置": sub_id,
                        "结果": "跳过",
                        "原因": f"订阅状态为「{config.get('status')}」，不产生新的订阅推送结果",
                    }
                )
                continue

            if schedule.normalize_frequency(config.get("推送频率")) != "实时":
                merged = self.queue_event(config, snapshot, moment=moment)
                evaluations.append(
                    {
                        "订阅配置": sub_id,
                        "结果": "已登记待推送",
                        "原因": f"频率为{schedule.normalize_frequency(config.get('推送频率'))}，待按频率合并推送",
                        "合并后批次数": merged.get("合并后批次数", 0),
                    }
                )
                continue

            try:
                outcome = self.dispatch(
                    sub_id, now=moment, due_only=False, contents=[snapshot], operator=operator
                )
            except (PermissionDenied, LookupError) as exc:
                evaluations.append({"订阅配置": sub_id, "结果": "跳过", "原因": str(exc)})
                continue
            deliveries.extend(outcome.get("records", []))
            evaluations.append(
                {
                    "订阅配置": sub_id,
                    "结果": "已推送" if outcome.get("records") else "未产生新记录",
                    "原因": outcome.get("reason", ""),
                    "下次推送时间": outcome.get("next_push_time", ""),
                }
            )
        return {
            "snapshot": snapshot.get("洞察版本"),
            "deliveries": deliveries,
            "subscriptions": evaluations,
        }

    def record_insight_snapshot(self, insight: dict[str, Any], *, source: str) -> dict[str, Any]:
        """按报告编号追加不可变快照版本；内容未变时复用已有版本，避免虚增版本。"""
        report_no = _text(insight.get("报告编号"))
        summary = _text(insight.get("核心发现")) or _text(insight.get("分析主题")) or report_no
        digest = content_digest(
            report_no, summary, insight.get("关键指标"), insight.get("数据区间")
        )
        for item in store.rows(INSIGHT_SNAPSHOT_MODULE):
            if _text(item.get("报告编号")) == report_no and item.get("内容哈希") == digest:
                return item

        existing_versions = [
            item
            for item in store.rows(INSIGHT_SNAPSHOT_MODULE)
            if _text(item.get("报告编号")) == report_no
        ]
        version_no = len(existing_versions) + 1
        snapshot = {
            "报告编号": report_no,
            "洞察版本": f"{report_no}@v{version_no}",
            "版本序号": version_no,
            "内容哈希": digest,
            "快照摘要": summary,
            "核心发现": _text(insight.get("核心发现")),
            "关键指标": _text(insight.get("关键指标")),
            "数据区间": _text(insight.get("数据区间")),
            "生成时间": _text(insight.get("生成时间")) or datetime.now().strftime("%Y-%m-%d"),
            "快照时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "来源": source,
            "来源类型": "洞察报告",
            "源记录ID": insight.get("id"),
            "状态": "已快照",
            "订阅配置": [],
        }
        created, _ = store.insert_unique(
            INSIGHT_SNAPSHOT_MODULE,
            key=lambda row: _text(row.get("洞察版本")),
            row=snapshot,
        )
        return created

    # ================================================================== 恢复与追溯
    def recovery_info(self) -> dict[str, Any]:
        return {
            "推送记录": store.recovery_info(PUSH_MODULE),
            "订阅配置": store.recovery_info(MODULE),
            "说明": "中断后从上一完整版本继续；已可用的推送数据集不会被半成品覆盖。",
        }

    def trace(
        self, *, insight_version: str | None = None, subscription_id: int | None = None
    ) -> dict[str, Any]:
        """把订阅配置、快照、投递记录与静默判定串成一条可追溯链。"""
        records, _ = self.list_deliveries(
            subscription_id=subscription_id,
            insight_version=insight_version,
            page=1,
            size=1000,
        )
        configs = {
            int(row.get("id") or 0): row
            for row in store.rows(MODULE)
            if subscription_id is None or int(row.get("id") or 0) == subscription_id
        }
        snapshots = {
            _text(row.get("洞察版本")): row
            for row in store.rows(INSIGHT_SNAPSHOT_MODULE)
            if insight_version is None or _text(row.get("洞察版本")) == insight_version
        }
        chain = []
        for record in records:
            config = configs.get(int(record.get("订阅配置") or 0), {})
            version = _text(record.get("内容版本")) or _text(record.get("洞察版本"))
            snapshot = snapshots.get(version, {})
            chain.append(
                {
                    "投递记录": record.get("id"),
                    "内容版本": version,
                    "接收人": record.get("接收人"),
                    "推送渠道": record.get("推送渠道"),
                    "订阅状态": config.get("status", ""),
                    "下次推送时间": config.get("下次推送时间", ""),
                    "快照摘要": snapshot.get("快照摘要", record.get("快照摘要")),
                    "静默判定": record.get("静默判定", {}),
                    "投递时间": record.get("触发时间"),
                    "状态": record.get("状态"),
                }
            )
        return {
            "订阅配置": list(configs.values()),
            "快照": list(snapshots.values()),
            "投递链": chain,
            "静默判定可追溯": bool(chain) and all(item["静默判定"] for item in chain),
            "下次推送时间可追溯": all(
                item["下次推送时间"] or item["状态"] != "已投递" for item in chain
            ),
        }
