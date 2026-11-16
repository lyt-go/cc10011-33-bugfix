"""订阅推送调度入口：按频率合并待推送数据并推送，生成可追溯的订阅推送记录。

这个模块只负责“什么时候调用”，不承载业务规则：

* ``run_once`` 遍历已启用订阅，逐个调用 ``SubscriptionService.dispatch``；
* 订阅未到下次推送时间、命中静默时段、已暂停都会被服务层拦下并记录原因；
* ``APP_SCHEDULER_ENABLED=1`` 时随应用启动一个后台线程，按 ``APP_SCHEDULER_INTERVAL``
  秒轮询；默认关闭，避免本地跑起来就不受控地发消息。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

from app.services.subscription import SubscriptionService, STATUS_PUSHABLE
from app.store import store

logger = logging.getLogger("app.scheduler")


def run_once(*, now: Any = None) -> dict[str, Any]:
    """执行一轮调度，返回每个订阅的结果摘要。"""
    service = SubscriptionService()
    results: list[dict[str, Any]] = []
    pushed_records = 0
    for config in list(store.rows("subscription")):
        if config.get("status") != STATUS_PUSHABLE:
            continue
        sub_id = int(config.get("id") or 0)
        try:
            outcome = service.dispatch(sub_id, now=now, due_only=True)
        except Exception as exc:  # 单个订阅失败不影响其它订阅
            logger.warning("订阅 %s 调度失败：%s", sub_id, exc)
            results.append({"订阅配置": sub_id, "pushed": False, "reason": str(exc)})
            continue
        pushed_records += len(outcome.get("records", []))
        results.append(
            {
                "订阅配置": sub_id,
                "pushed": bool(outcome.get("pushed")),
                "created": outcome.get("created", 0),
                "reason": outcome.get("reason", ""),
                "next_push_time": outcome.get("next_push_time", ""),
            }
        )
    return {"处理订阅数": len(results), "新增推送记录": pushed_records, "明细": results}


class SchedulerThread:
    def __init__(self, interval_seconds: int = 60) -> None:
        self.interval = max(int(interval_seconds), 5)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: dict[str, Any] | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="subscription-scheduler", daemon=True)
        self._thread.start()
        logger.info("订阅推送调度已启动，间隔 %s 秒", self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.last_result = run_once()
            except Exception as exc:  # 调度线程不能因为单轮异常退出
                logger.exception("订阅推送调度异常：%s", exc)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


scheduler = SchedulerThread(int(os.environ.get("APP_SCHEDULER_INTERVAL", "60") or 60))
