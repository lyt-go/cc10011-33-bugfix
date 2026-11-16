"""订阅推送接口：维护订阅配置，并暴露合并、推送、投递记录与追溯链路。

路由层只做参数解析和异常映射，状态流转与幂等全部在 ``app/services/subscription.py``。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Header, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.subscription import (
    Conflict,
    PermissionDenied,
    SubscriptionService,
    ValidationFailed,
)

router = APIRouter(prefix="/api/subscription", tags=["订阅推送"])

service = SubscriptionService()

LIST_FIELDS = ["订阅编号", "订阅内容", "接收人", "推送渠道", "推送频率", "数据粒度", "静默时段", "下次推送时间"]
STATUSES = ["草稿", "订阅评审中", "已启用", "已暂停"]


def _operator(x_operator: str | None) -> str | None:
    """当前操作用户。

    HTTP 头只能是 latin-1，中文操作人由前端 ``encodeURIComponent`` 后再传，
    这里统一解码；也可以直接传 ASCII 的账号名。
    """
    raw = (x_operator or "").strip()
    return (unquote(raw) or None) if raw else None


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按订阅编号或订阅内容检索"),
    status: str | None = Query(default=None, description="草稿、订阅评审中、已启用、已暂停"),
    page: int = 1,
    size: int = 20,
    include_shared: bool = Query(default=True, description="是否包含只读共享渠道"),
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> PageResult[dict]:
    """按订阅编号与状态过滤订阅推送列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        page=page,
        size=size,
        operator=_operator(x_operator),
        include_shared=include_shared,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> dict[str, Any]:
    """导出订阅推送清单：返回当前用户可见的全量数据。"""
    items, total = service.list_entries(page=1, size=10000, operator=_operator(x_operator))
    return {"module": "subscription", "total": total, "items": items}


@router.get("/deliveries", response_model=PageResult[dict])
def list_deliveries(
    subscription_id: int | None = Query(default=None),
    insight_version: str | None = Query(default=None, description="内容版本，如 USER-0001@v1"),
    status: str | None = Query(default=None, description="已投递、投递失败、待重试、已合并"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """订阅推送数据记录：每条记录都带静默判定与下次推送时间，便于追溯。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_deliveries(
        subscription_id=subscription_id,
        insight_version=insight_version,
        status=status,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/deliveries/{record_id}", response_model=dict)
def get_delivery(record_id: int) -> dict:
    record = service.get_delivery(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"订阅推送记录 {record_id} 不存在")
    return record


@router.post("/deliveries/{record_id}/retry", response_model=ActionResult)
def retry_delivery(
    record_id: int,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> ActionResult:
    """失败消息可重试；成功记录的重试会被忽略，避免重复打扰。"""
    try:
        outcome = service.retry_delivery(record_id, operator=_operator(x_operator))
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ActionResult(
        ok=bool(outcome["ok"]),
        message=str(outcome["message"]),
        entry=outcome.get("record"),
    )


@router.post("/deliveries/{record_id}/fail", response_model=ActionResult)
def mark_delivery_failed(
    record_id: int,
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> ActionResult:
    """渠道回执失败时把记录标记为投递失败，之后可重试。"""
    reason = str(payload.value("reason") or "渠道回执失败")
    try:
        record = service.mark_delivery_failed(record_id, reason, operator=_operator(x_operator))
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ActionResult(ok=True, message="已记录投递失败，可稍后重试", entry=record)


@router.get("/trace", response_model=dict)
def trace(
    insight_version: str | None = Query(default=None),
    subscription_id: int | None = Query(default=None),
) -> dict:
    """投递链追溯：订阅配置、洞察快照、静默判定与下次推送时间在同一视图里串起来。"""
    return service.trace(insight_version=insight_version, subscription_id=subscription_id)


@router.get("/recovery", response_model=dict)
def recovery() -> dict:
    """中断恢复信息：当前版本、上一完整版本与持久化文件。"""
    return service.recovery_info()


@router.post("/schedule", response_model=dict)
def run_schedule() -> dict:
    """执行一轮订阅调度：所有已启用订阅按频率合并并推送，未到期/静默/暂停的只记录原因。"""
    from app.services.scheduler import run_once

    return run_once()


@router.get("/{entry_id}", response_model=dict)
def get_entry(
    entry_id: int,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> dict:
    """读取单条订阅配置明细；不存在时给出可读的错误说明。"""
    try:
        entry = service.get_entry(entry_id, operator=_operator(x_operator))
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail=f"订阅配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> ActionResult:
    """登记一条订阅配置，缺字段时说明原因而不是静默丢弃。"""
    try:
        entry, missing = service.create_entry(
            payload.merged_values(), operator=_operator(x_operator)
        )
    except ValidationFailed as exc:
        return ActionResult(ok=False, message=str(exc))
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="订阅配置已登记", entry=entry)


@router.patch("/{entry_id}", response_model=ActionResult)
def update_entry(
    entry_id: int,
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> ActionResult:
    """编辑订阅配置：按字段合并，未提交的接收人/渠道/静默时段/下次推送时间保持原值。"""
    try:
        entry = service.update_entry(entry_id, payload.merged_values(), operator=_operator(x_operator))
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationFailed as exc:
        return ActionResult(ok=False, message=str(exc), entry=None)
    return ActionResult(ok=True, message="订阅配置已更新", entry=entry)


@router.post("/{entry_id}/merge", response_model=ActionResult)
def merge_pending(
    entry_id: int,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> ActionResult:
    """按频率窗口合并待推送分析数据，只返回合并结果，不实际发送。"""
    try:
        config = service.get_entry(entry_id, operator=_operator(x_operator))
        if config is None:
            raise LookupError(f"订阅配置 {entry_id} 不存在或已归档")
        merged = service.merge_pending(config, _operator(x_operator))
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ActionResult(ok=True, message=f"已按频率合并为 {merged['合并后批次数']} 个批次", entry=merged)


@router.post("/{entry_id}/dispatch", response_model=ActionResult)
def dispatch(
    entry_id: int,
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> ActionResult:
    """按频率合并并推送订阅消息，生成订阅推送数据记录。

    订阅未启用、未到下次推送时间、命中静默时段都会被拦下并说明原因。
    """
    values = payload.merged_values()
    try:
        outcome = service.dispatch(
            entry_id,
            now=values.get("now"),
            operator=_operator(x_operator),
            due_only=not bool(values.get("force")),
        )
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ActionResult(
        ok=bool(outcome.get("pushed")),
        message=_dispatch_message(outcome),
        entry=outcome,
    )


def _dispatch_message(outcome: dict[str, Any]) -> str:
    if outcome.get("pushed"):
        return (
            f"已推送 {outcome.get('created', 0)} 条订阅消息，"
            f"重复触发合并 {outcome.get('skipped_duplicate', 0)} 条，"
            f"下次推送时间 {outcome.get('next_push_time', '')}"
        )
    return str(outcome.get("reason") or "本次未产生新的订阅推送记录")


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> ActionResult:
    """对单条订阅配置执行提交订阅评审、启用订阅、暂停订阅；非法流转会被拦下并说明原因。"""
    action = str(payload.value("action") or "").strip()
    try:
        entry, message = service.run_action(entry_id, action, operator=_operator(x_operator))
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValidationFailed as exc:
        return ActionResult(ok=False, message=str(exc))
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
