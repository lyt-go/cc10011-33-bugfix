"""订阅推送链路回归测试。

只依赖标准库的 unittest，直接调用服务层与 FastAPI 应用，覆盖这次修复的约束：

* 编辑订阅配置不丢接收人/渠道/静默时段/下次推送时间
* 状态机只允许评审 → 启用/暂停，暂停后不产生推送结果，恢复只重算下次推送时间
* 同一内容版本 + 接收人 + 渠道只产生一条投递记录，并发触发只成功一次
* 跨天静默判定正确，双时区冲突时只按一个静默规则投递
* 告警恢复、洞察发布都沿调用链走到订阅推送
* 归属控制：共享渠道对非本人只读，越权修改返回 403
* 中断后恢复不覆盖已可用数据集

运行：``python -m unittest discover -s tests -v``
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.services import schedule, userinsight
from app.services.alarmhistory import AlarmhistoryService
from app.services.subscription import (
    Conflict,
    PermissionDenied,
    SubscriptionService,
    ValidationFailed,
)
from app.store import store


def reset_store() -> None:
    """每个用例从种子数据重新开始，避免用例之间互相污染。"""
    from app.seed import SEED_ROWS

    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._versions = {name: 1 for name in store._tables}
    store._last_complete = dict(store._versions)


class SubscriptionConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.service = SubscriptionService()

    def test_edit_keeps_core_fields(self) -> None:
        before = self.service.get_entry(1)
        updated = self.service.update_entry(1, {"推送频率": "每小时", "静默时段": ""})
        self.assertEqual(updated["接收人"], before["接收人"])
        self.assertEqual(updated["推送渠道"], before["推送渠道"])
        self.assertEqual(updated["静默时段"], before["静默时段"])
        self.assertTrue(updated["下次推送时间"])
        self.assertIn("静默时段", updated["保留字段"])
        self.assertEqual(updated["修订号"], int(before["修订号"]) + 1)

    def test_create_keeps_recipient_and_channel(self) -> None:
        entry, missing = self.service.create_entry(
            {
                "订阅编号": "SUBS-1001",
                "订阅内容": "USER-0001",
                "接收人": "王芳",
                "推送渠道": "企业微信",
                "推送频率": "每周",
                "静默时段": "22:00-07:00",
            }
        )
        self.assertEqual(missing, [])
        assert entry is not None
        self.assertEqual(entry["接收人"], "王芳")
        self.assertEqual(entry["推送渠道"], "企业微信")
        self.assertEqual(entry["静默时段"], "22:00-07:00")
        self.assertEqual(entry["status"], "草稿")

    def test_invalid_frequency_and_quiet_window_rejected(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.update_entry(1, {"推送频率": "每十秒"})
        with self.assertRaises(ValidationFailed):
            self.service.update_entry(1, {"静默时段": "22点到7点"})

    def test_status_machine(self) -> None:
        with self.assertRaises(Conflict):
            self.service.run_action(2, "暂停订阅")  # 评审中不能直接暂停
        with self.assertRaises(Conflict):
            self.service.run_action(1, "启用订阅")  # 已启用不能重复启用
        entry, message = self.service.run_action(4, "暂停订阅")
        self.assertEqual(entry["status"], "已暂停")
        self.assertIn("暂停订阅", message)

    def test_draft_must_be_reviewed_before_enable(self) -> None:
        created, _ = self.service.create_entry(
            {"订阅编号": "SUBS-1002", "订阅内容": "USER-0001", "接收人": "王芳"}
        )
        assert created is not None
        with self.assertRaises(Conflict):
            self.service.run_action(int(created["id"]), "启用订阅")
        with self.assertRaises(Conflict):
            self.service.run_action(int(created["id"]), "暂停订阅")
        reviewed, _ = self.service.run_action(int(created["id"]), "提交订阅评审")
        self.assertEqual(reviewed["status"], "订阅评审中")
        enabled, _ = self.service.run_action(int(created["id"]), "启用订阅")
        self.assertEqual(enabled["status"], "已启用")
        self.assertTrue(enabled["下次推送时间"])

    def test_paused_subscription_produces_no_new_records(self) -> None:
        self.service.run_action(1, "暂停订阅")
        before = len(store.rows("subscriptionpush"))
        outcome = self.service.dispatch(1, now="2026-11-16 12:00", due_only=False)
        self.assertFalse(outcome["pushed"])
        self.assertIn("已暂停", outcome["reason"])
        self.assertEqual(len(store.rows("subscriptionpush")), before)

    def test_resume_only_recomputes_next_push_time(self) -> None:
        paused = self.service.run_action(4, "暂停订阅")[0]
        before_push = paused["暂停前下次推送时间"]
        resumed = self.service.run_action(4, "启用订阅")[0]
        self.assertEqual(resumed["status"], "已启用")
        self.assertTrue(resumed["下次推送时间"])
        self.assertTrue(resumed["暂停期间不补发"])
        self.assertEqual(resumed["暂停前下次推送时间"], before_push)
        self.assertEqual(resumed["暂停时间"], "")
        self.assertTrue(resumed["上次暂停时间"])

    def test_ownership_control(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.service.update_entry(1, {"静默时段": "21:00-06:00"}, operator="李强")
        with self.assertRaises(PermissionDenied):
            self.service.run_action(1, "暂停订阅", operator="李强")
        # 共享渠道对非本人只读，但仍可读
        view = self.service.get_entry(1, operator="李强")
        self.assertTrue(view["共享只读"])
        self.assertFalse(view["可写"])
        # 本人可写
        self.assertFalse(self.service.get_entry(1, operator="王芳")["共享只读"])


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.service = SubscriptionService()

    def test_same_version_only_one_record(self) -> None:
        content = {
            "报告编号": "USER-0001",
            "洞察版本": "USER-0001@v1",
            "快照摘要": "新用户次留提升 3.2 个百分点",
            "生成时间": "2026-11-16",
            "来源": "用户洞察",
            "来源类型": "洞察报告",
            "版本序号": 1,
        }
        first = self.service.dispatch(
            5, now="2026-11-16 12:00", due_only=False, contents=[content]
        )
        second = self.service.dispatch(
            5, now="2026-11-16 12:10", due_only=False, contents=[content]
        )
        self.assertTrue(first["pushed"])
        self.assertFalse(second["pushed"])
        self.assertEqual(second["skipped_duplicate"], 1)
        records = store.find_by(
            "subscriptionpush", 内容版本="USER-0001@v1", 接收人="王芳", 推送渠道="企业微信"
        )
        self.assertEqual(len(records), 1)

    def test_concurrent_dispatch_creates_once(self) -> None:
        content = {
            "报告编号": "CONC-1",
            "洞察版本": "CONC-1@v1",
            "快照摘要": "并发触发",
            "生成时间": "2026-11-16",
            "来源": "测试",
            "来源类型": "洞察报告",
            "版本序号": 1,
        }

        def worker() -> None:
            self.service.dispatch(
                5, now="2026-11-16 12:00", due_only=False, contents=[content]
            )

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        records = store.find_by("subscriptionpush", 内容版本="CONC-1@v1")
        self.assertEqual(len(records), 1)

    def test_merge_by_frequency_window(self) -> None:
        merged = self.service.merge_pending(store.find("subscription", 1))
        self.assertEqual(merged["合并前条数"], 3)
        self.assertEqual(merged["合并后批次数"], 1)
        self.assertEqual(merged["批次"][0]["频率窗口"], "2026-11-16")
        hourly = dict(store.find("subscription", 1), 推送频率="每小时")
        windows = {
            schedule.frequency_window(hourly, schedule.parse_now(text))
            for text in ("2026-11-16 09:10", "2026-11-16 10:05", "2026-11-16 10:50")
        }
        self.assertEqual(len(windows), 2)

    def test_record_is_traceable(self) -> None:
        outcome = self.service.dispatch(1, now="2026-11-16 12:00", due_only=False)
        self.assertTrue(outcome["pushed"])
        for record in outcome["records"]:
            self.assertIn("是否静默", record["静默判定"])
            self.assertTrue(record["下次推送时间"])
        chain = self.service.trace(subscription_id=1)["投递链"]
        self.assertTrue(chain)
        self.assertTrue(all(item["静默判定"] for item in chain))

    def test_retry_failed_but_not_delivered(self) -> None:
        message = self.service.retry_delivery(2)["message"]
        self.assertIn("已重试", message)
        record = self.service.get_delivery(2)
        self.assertEqual(record["状态"], "已投递")
        self.assertEqual(record["重试次数"], 1)
        duplicate = self.service.retry_delivery(2)
        self.assertTrue(duplicate["duplicated"])
        self.assertEqual(self.service.get_delivery(2)["重试次数"], 1)


class SilenceTests(unittest.TestCase):
    def test_cross_day_window(self) -> None:
        window = schedule.parse_quiet_window("22:00-07:00")
        assert window is not None
        self.assertTrue(window.cross_day)
        self.assertTrue(schedule.is_quiet_at(schedule.parse_now("2026-11-17 01:00"), window))
        self.assertTrue(schedule.is_quiet_at(schedule.parse_now("2026-11-16 23:30"), window))
        self.assertFalse(schedule.is_quiet_at(schedule.parse_now("2026-11-16 12:00"), window))
        self.assertFalse(schedule.is_quiet_at(schedule.parse_now("2026-11-16 07:00"), window))

    def test_conflict_uses_one_rule(self) -> None:
        config = {
            "接收人": "李强",
            "接收人时区": "America/New_York",
            "推送渠道": "Webhook",
            "渠道时区": "UTC",
            "静默时段": "22:00-07:00",
        }
        evaluation = schedule.evaluate_silence(
            config, schedule.parse_now("2026-11-16T23:00:00+00:00")
        )
        self.assertFalse(evaluation["接收人本地静默"])
        self.assertTrue(evaluation["渠道统一时区静默"])
        self.assertTrue(evaluation["冲突"])
        self.assertTrue(evaluation["是否静默"])
        self.assertEqual(evaluation["判定时区"], "UTC")
        self.assertIn("任一命中即静默", evaluation["冲突处理"])

    def test_quiet_blocks_dispatch(self) -> None:
        reset_store()
        service = SubscriptionService()
        before = len(store.rows("subscriptionpush"))
        outcome = service.dispatch(1, now="2026-11-17 01:00", due_only=False)
        self.assertFalse(outcome["pushed"])
        self.assertIn("静默", outcome["reason"])
        self.assertEqual(len(store.rows("subscriptionpush")), before)

    def test_next_push_time_skips_quiet(self) -> None:
        config = dict(store.find("subscription", 1), 渠道时区="Asia/Shanghai", 推送频率="每小时")
        next_time, info = schedule.next_push_time(
            config, schedule.parse_now("2026-11-16 21:30", schedule.resolve_zone("Asia/Shanghai"))
        )
        self.assertTrue(info["因静默顺延"])
        self.assertEqual(next_time, "2026-11-17 07:00")


class DeliveryChainTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.service = SubscriptionService()

    def test_alarm_recovery_respects_paused(self) -> None:
        store.find("subscription", 4)["订阅内容"] = "全部告警"
        self.service.run_action(4, "暂停订阅")
        alarm = AlarmhistoryService()
        outcome = alarm.run_action(2, "标记恢复")[0]
        results = outcome.get("订阅推送结果") or []
        self.assertTrue(results)
        self.assertTrue(all(item["结果"] == "跳过" for item in results))
        self.assertEqual(outcome.get("订阅推送记录"), [])

    def test_alarm_recovery_delivers_once(self) -> None:
        store.find("subscription", 4)["订阅内容"] = "全部告警"
        alarm = AlarmhistoryService()
        first = alarm.run_action(2, "标记恢复")[0]
        self.assertTrue(first.get("订阅推送记录"))
        record_ids = list(first["订阅推送记录"])
        alarm.run_action(2, "标记恢复")
        after = store.find("alarmhistory", 2)
        self.assertEqual(after["订阅推送记录"], record_ids)

    def test_insight_publish_creates_snapshot_and_delivery(self) -> None:
        insight = store.find("userinsight", 1)
        insight["核心发现"] = "新用户次留提升 4.1 个百分点"
        service = userinsight.UserinsightService()
        entry, _ = service.run_action(1, "发布报告")
        self.assertEqual(entry["最新快照"], "USER-0001@v2")
        snapshots = service.snapshots_of("USER-0001")
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[-1]["快照摘要"], "新用户次留提升 4.1 个百分点")
        # 日频订阅不会立即投递，新版本先登记为待推送数据，等调度按频率合并。
        pending = store.find_by(
            "pendinganalysis", 订阅配置=1, 洞察版本="USER-0001@v2"
        )
        self.assertTrue(pending)
        self.assertEqual(pending[0]["状态"], "待推送")
        outcome = self.service.dispatch(1, now="2026-11-17 08:00", due_only=False)
        self.assertTrue(outcome["pushed"])
        records = store.find_by("subscriptionpush", 内容版本="USER-0001@v2")
        self.assertTrue(records)
        self.assertTrue(all(row["快照摘要"] == "新用户次留提升 4.1 个百分点" for row in records))

    def test_republish_same_content_does_not_duplicate(self) -> None:
        service = userinsight.UserinsightService()
        service.run_action(2, "发布报告")
        first_versions = {row["洞察版本"] for row in store.find_by("subscriptionpush", 洞察报告="USER-0002")}
        service.run_action(2, "发布报告")
        second_versions = {row["洞察版本"] for row in store.find_by("subscriptionpush", 洞察报告="USER-0002")}
        self.assertEqual(first_versions, second_versions)

    def test_pause_syncs_snapshot(self) -> None:
        self.service.run_action(1, "暂停订阅")
        snapshots = [
            row
            for row in store.rows("userinsightsnapshot")
            if 1 in (row.get("订阅配置") or [])
        ]
        self.assertTrue(snapshots)
        self.assertTrue(all(row["订阅状态"] == "已暂停" for row in snapshots))
        self.assertTrue(all(row["投递开关"] == "暂停" for row in snapshots))


class StoreRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()

    def test_interrupted_replace_keeps_previous_dataset(self) -> None:
        before = len(store.rows("subscriptionpush"))

        class Boom(Exception):
            pass

        def broken() -> list[dict]:
            raise Boom("生成到一半中断")

        with self.assertRaises(Boom):
            store.replace_rows("subscriptionpush", broken())
        self.assertEqual(len(store.rows("subscriptionpush")), before)

    def test_persist_and_reload(self) -> None:
        from app.store import Store

        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "store.json")
        os.environ["APP_STORE_FILE"] = path
        try:
            first = Store()
            first.checkpoint("subscriptionpush")
            persisted = Store()
            self.assertIn(persisted._loaded_from, {"主文件", "备份文件"})
            self.assertTrue(persisted.rows("subscriptionpush"))
            self.assertGreaterEqual(persisted.recovery_info("subscriptionpush")["上一完整版本"], 1)

            # 主文件损坏时回退上一份完整备份。
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{broken")
            recovered = Store()
            self.assertIn(recovered._loaded_from, {"主文件", "备份文件"})
            self.assertTrue(recovered.rows("subscriptionpush"))
        finally:
            os.environ.pop("APP_STORE_FILE", None)
            for name in os.listdir(directory):
                os.remove(os.path.join(directory, name))
            os.rmdir(directory)


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()

    def test_run_once_only_touches_due_enabled(self) -> None:
        from app.services.scheduler import run_once

        paused = store.find("subscription", 1)
        paused["status"] = "已暂停"
        enabled = store.find("subscription", 5)
        enabled["下次推送时间"] = "2026-01-01 00:00"
        outcome = run_once(now="2026-11-16 12:00")
        touched = {item["订阅配置"] for item in outcome["明细"]}
        self.assertNotIn(1, touched)
        self.assertIn(5, touched)
        self.assertGreaterEqual(outcome["新增推送记录"], 1)


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.client = TestClient(app)

    def test_edit_and_ownership_status_codes(self) -> None:
        own = self.client.patch(
            "/api/subscription/1",
            json={"values": {"静默时段": ""}},
            headers={"X-Operator": "%E7%8E%8B%E8%8A%B3"},
        )
        self.assertEqual(own.status_code, 200)
        self.assertTrue(own.json()["ok"])
        other = self.client.patch(
            "/api/subscription/1",
            json={"values": {"静默时段": "21:00-06:00"}},
            headers={"X-Operator": "%E6%9D%8E%E5%BC%BA"},
        )
        self.assertEqual(other.status_code, 403)

    def test_illegal_transition_returns_conflict(self) -> None:
        response = self.client.post(
            "/api/subscription/2/actions", json={"values": {"action": "暂停订阅"}}
        )
        self.assertEqual(response.status_code, 409)

    def test_delivery_endpoints(self) -> None:
        response = self.client.post(
            "/api/subscription/1/dispatch",
            json={"values": {"now": "2026-11-16 12:00", "force": True}},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        deliveries = self.client.get("/api/subscription/deliveries").json()
        self.assertTrue(deliveries["items"])
        trace = self.client.get("/api/subscription/trace?subscription_id=1").json()
        self.assertTrue(trace["静默判定可追溯"])

    def test_top_level_action_still_supported(self) -> None:
        userinsight_reset = self.client.post(
            "/api/userinsight/1/actions", json={"action": "发布报告"}
        )
        self.assertEqual(userinsight_reset.status_code, 200)
        self.assertTrue(userinsight_reset.json()["ok"])


if __name__ == "__main__":
    unittest.main()
