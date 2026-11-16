"""订阅推送的时间口径：频率窗口、跨天静默时段与下次推送时间。

这里集中处理三件容易出错的事：

1. **跨天静默**：``22:00-07:00`` 不是 ``22:00 <= t <= 07:00``，而是
   ``t >= 22:00`` 或 ``t < 07:00``；凌晨 01:00 必须判定为静默。
2. **双时区冲突**：接收人本地时区与渠道统一时区各自判断一次。两者结论不一致时，
   采用“任一命中即静默”的保守策略——夜间宁可不打扰，也不把静默时段误判成可推送；
   同时把两个时区、命中窗口、判定依据一起写进投递记录，保证可追溯。
3. **下次推送时间**：按推送频率向后推一个窗口；如果落点仍在静默时段，
   顺延到静默结束时刻。暂停订阅不产生新的下次推送时间。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 渠道统一时区：同一个渠道的所有接收人共用这个时区做兜底判断。
CHANNEL_TIMEZONES: dict[str, str] = {
    "邮件": "Asia/Shanghai",
    "企业微信": "Asia/Shanghai",
    "短信": "Asia/Shanghai",
    "Webhook": "UTC",
}
DEFAULT_CHANNEL_TIMEZONE = "Asia/Shanghai"

# 接收人本地时区：真实项目里来自用户档案，这里给出示例档案，未登记时回落到渠道时区。
RECIPIENT_TIMEZONES: dict[str, str] = {
    "王芳": "Asia/Shanghai",
    "李强": "America/New_York",
    "赵敏": "Europe/London",
    "陈晨": "Asia/Tokyo",
    "值班管理员": "Asia/Shanghai",
}

# 频率 → 分钟数；0 表示实时（立即推送）。
FREQUENCY_MINUTES: dict[str, int] = {
    "实时": 0,
    "每小时": 60,
    "每日": 1440,
    "每周": 10080,
    "每月": 43200,
}
FREQUENCY_ALIASES: dict[str, str] = {
    "hourly": "每小时",
    "daily": "每日",
    "weekly": "每周",
    "monthly": "每月",
    "realtime": "实时",
    "real-time": "实时",
    "1h": "每小时",
    "1d": "每日",
    "7d": "每周",
}

_MAX_QUIET_SHIFT = timedelta(days=2)


@dataclass(frozen=True)
class QuietWindow:
    start: time
    end: time
    raw: str

    @property
    def cross_day(self) -> bool:
        return self.start > self.end


def resolve_zone(name: str | None, fallback: str = DEFAULT_CHANNEL_TIMEZONE) -> tzinfo:
    for candidate in (name, fallback, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def channel_zone(config: dict[str, Any]) -> tzinfo:
    return resolve_zone(
        str(config.get("渠道时区") or "").strip() or CHANNEL_TIMEZONES.get(str(config.get("推送渠道") or ""))
    )


def recipient_zone_name(config: dict[str, Any]) -> str:
    explicit = str(config.get("接收人时区") or "").strip()
    if explicit:
        return explicit
    recipient = str(config.get("接收人") or "").strip()
    if recipient in RECIPIENT_TIMEZONES:
        return RECIPIENT_TIMEZONES[recipient]
    return str(CHANNEL_TIMEZONES.get(str(config.get("推送渠道") or ""), DEFAULT_CHANNEL_TIMEZONE))


def recipient_zone(config: dict[str, Any]) -> tzinfo:
    return resolve_zone(recipient_zone_name(config), str(channel_zone(config)))


def normalize_frequency(value: Any) -> str:
    raw = str(value or "").strip()
    if raw in FREQUENCY_MINUTES:
        return raw
    alias = FREQUENCY_ALIASES.get(raw.lower())
    if alias:
        return alias
    return "每日"


def frequency_minutes(value: Any) -> int:
    return FREQUENCY_MINUTES.get(normalize_frequency(value), 1440)


def parse_quiet_window(value: Any) -> QuietWindow | None:
    """解析 ``HH:MM-HH:MM``；允许中文冒号、空格和“次日”字样。"""
    raw = str(value or "").strip()
    if not raw or "-" not in raw:
        return None
    left, _, right = raw.partition("-")
    left = left.replace("：", ":").replace("次日", "").strip()
    right = right.replace("：", ":").replace("次日", "").strip()
    try:
        start = _parse_hhmm(left)
        end = _parse_hhmm(right)
    except ValueError:
        return None
    return QuietWindow(start=start, end=end, raw=raw)


def _parse_hhmm(value: str) -> time:
    hour_text, _, minute_text = value.partition(":")
    hour = int(hour_text)
    minute = int(minute_text or "0")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"非法时间：{value}")
    return time(hour=hour, minute=minute)


def parse_now(value: Any, tz: tzinfo | None = None) -> datetime:
    """解析调用方传入的 ``now``；不带时区时按渠道时区解释。"""
    if isinstance(value, datetime):
        moment = value
    elif value:
        text = str(value).strip().replace("Z", "+00:00")
        for candidate in (text, text.replace("/", "-")):
            try:
                moment = datetime.fromisoformat(candidate)
                break
            except ValueError:
                continue
        else:
            moment = datetime.now(tz or resolve_zone(DEFAULT_CHANNEL_TIMEZONE))
    else:
        moment = datetime.now(tz or resolve_zone(DEFAULT_CHANNEL_TIMEZONE))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz or resolve_zone(DEFAULT_CHANNEL_TIMEZONE))
    return moment.astimezone(tz or moment.tzinfo)


def is_quiet_at(local_moment: datetime, window: QuietWindow) -> bool:
    current = local_moment.time()
    if window.cross_day:
        return current >= window.start or current < window.end
    return window.start <= current < window.end


def quiet_end(local_moment: datetime, window: QuietWindow) -> datetime:
    """返回当前静默窗口的结束时刻（与入参同一时区）。"""
    base = local_moment.replace(second=0, microsecond=0)
    end_today = base.replace(hour=window.end.hour, minute=window.end.minute)
    if window.cross_day and base.time() >= window.start:
        return end_today + timedelta(days=1)
    if end_today <= base:
        return end_today + timedelta(days=1)
    return end_today


def evaluate_silence(
    config: dict[str, Any],
    moment: datetime,
    *,
    now_text: str | None = None,
) -> dict[str, Any]:
    """同时按接收人本地时区与渠道统一时区判断静默，并给出唯一结论。

    结论取“任一命中即静默”，避免接收人本地已到夜间、渠道时区却仍是白天时误发。
    """
    window = parse_quiet_window(config.get("静默时段"))
    ch_zone = channel_zone(config)
    rc_zone = recipient_zone(config)
    channel_moment = moment.astimezone(ch_zone)
    if now_text:
        recipient_moment = parse_now(now_text, rc_zone)
    else:
        recipient_moment = moment.astimezone(rc_zone)

    result: dict[str, Any] = {
        "静默时段": window.raw if window else "",
        "接收人本地时区": recipient_zone_name(config),
        "渠道统一时区": getattr(ch_zone, "key", str(ch_zone)),
        "接收人本地时间": recipient_moment.strftime("%Y-%m-%d %H:%M:%S"),
        "渠道时间": channel_moment.strftime("%Y-%m-%d %H:%M:%S"),
        "跨天": bool(window.cross_day) if window else False,
        "接收人本地静默": False,
        "渠道统一时区静默": False,
        "是否静默": False,
        "冲突": False,
        "冲突处理": "",
        "判定时区": "",
        "判定依据": "未配置静默时段，按可推送处理",
        "静默结束时间": "",
    }
    if window is None:
        return result

    recipient_quiet = is_quiet_at(recipient_moment, window)
    channel_quiet = is_quiet_at(channel_moment, window)
    result["接收人本地静默"] = recipient_quiet
    result["渠道统一时区静默"] = channel_quiet
    result["是否静默"] = recipient_quiet or channel_quiet
    result["冲突"] = recipient_quiet != channel_quiet

    if recipient_quiet or channel_quiet:
        if recipient_quiet:
            decider_name = result["接收人本地时区"]
            decider_moment = recipient_moment
            decider_zone = rc_zone
            scope = "接收人本地时区"
        else:
            decider_name = result["渠道统一时区"]
            decider_moment = channel_moment
            decider_zone = ch_zone
            scope = "渠道统一时区"
        result["判定时区"] = decider_name
        result["判定依据"] = f"跨天静默时段 {window.raw} 命中 {scope}" if window.cross_day else f"静默时段 {window.raw} 命中 {scope}"
        end_moment = quiet_end(decider_moment, window)
        result["静默结束时间"] = end_moment.strftime("%Y-%m-%d %H:%M")
        result["_静默结束_带时区"] = end_moment
        result["_判定时区对象"] = decider_zone
        if result["冲突"]:
            result["冲突处理"] = "两个时区结论不一致，按任一命中即静默处理，避免夜间打扰"
        else:
            result["冲突处理"] = "两个时区结论一致"
    else:
        result["判定依据"] = f"静默时段 {window.raw} 未命中（接收人本地与渠道时区均不在区间内）"
        result["冲突处理"] = "两个时区结论一致"
    return result


def _strip_internal(evaluation: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in evaluation.items() if not key.startswith("_")}


def next_push_time(
    config: dict[str, Any],
    moment: datetime,
) -> tuple[str, dict[str, Any]]:
    """返回 ``(下次推送时间, 调度判定)``；落点处于静默时顺延到静默结束。"""
    ch_zone = channel_zone(config)
    local_now = moment.astimezone(ch_zone)
    minutes = frequency_minutes(config.get("推送频率"))
    base = local_now if minutes <= 0 else local_now + timedelta(minutes=minutes)
    base = base.replace(second=0, microsecond=0)
    evaluation = evaluate_silence(config, base)
    shifted = False
    if evaluation["是否静默"]:
        end_moment = evaluation.get("_静默结束_带时区")
        if isinstance(end_moment, datetime):
            base = end_moment.astimezone(ch_zone).replace(second=0, microsecond=0)
            shifted = True
            # 顺延后再次判断，防止极窄的静默窗口叠加。
            second_pass = evaluate_silence(config, base)
            if second_pass["是否静默"]:
                end_again = second_pass.get("_静默结束_带时区")
                if isinstance(end_again, datetime) and end_again - base <= _MAX_QUIET_SHIFT:
                    base = end_again.astimezone(ch_zone).replace(second=0, microsecond=0)
                    evaluation = second_pass
    schedule = {
        "基准时间": local_now.strftime("%Y-%m-%d %H:%M"),
        "频率": normalize_frequency(config.get("推送频率")),
        "频率分钟": minutes,
        "因静默顺延": shifted,
        "静默判定": _strip_internal(evaluation),
    }
    return base.strftime("%Y-%m-%d %H:%M"), schedule


def frequency_window(config: dict[str, Any], generated_at: datetime) -> str:
    """把一条待推送数据归到它所属的频率窗口，用于同频合并。"""
    local = generated_at.astimezone(channel_zone(config))
    minutes = frequency_minutes(config.get("推送频率"))
    if minutes >= 43200:
        return local.strftime("%Y-%m")
    if minutes >= 10080:
        iso_year, iso_week, _ = local.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if minutes >= 1440:
        return local.strftime("%Y-%m-%d")
    if minutes >= 60:
        return local.strftime("%Y-%m-%d %H")
    return local.strftime("%Y-%m-%d %H:%M")


def public_evaluation(evaluation: dict[str, Any]) -> dict[str, Any]:
    return _strip_internal(evaluation)
