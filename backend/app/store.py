"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

这里额外承担三件事，订阅推送的“可恢复写入”依赖它们：

1. ``RLock``：并发触发推送时，唯一的投递记录只能被创建一次；
2. ``atomic_replace``：整批数据先写进临时列表，全部成功后再一次替换，
   生成中途中断不会覆盖上一份可用数据集；
3. 可选的文件持久化（``APP_STORE_FILE``）：先写临时文件并 fsync，再原子替换，
   并保留上一份完整文件作为 ``.bak``；进程中断后优先加载主文件，损坏时回退备份。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from typing import Any, Callable

from app.seed import SEED_ROWS


def _module_file_snapshot(tables: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    return {name: [dict(row) for row in rows] for name, rows in tables.items()}


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = _module_file_snapshot(SEED_ROWS)
        self._versions: dict[str, int] = {name: 1 for name in self._tables}
        # 上一份“完整版本”的编号：中断恢复时从这里继续，而不是重新覆盖。
        self._last_complete: dict[str, int] = dict(self._versions)
        # 最近一份完整版本的内存镜像：主文件与备份都不可用时从哪里继续。
        self._recovery_snapshot: dict[str, list[dict[str, Any]]] = _module_file_snapshot(self._tables)
        self._path = os.environ.get("APP_STORE_FILE", "").strip()
        self._loaded_from = "seed"
        if self._path:
            self._load_from_disk()

    # ------------------------------------------------------------------ 基础读取
    def module_names(self) -> list[str]:
        with self._lock:
            return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self.rows(module):
                if int(row.get("id", 0)) == entry_id:
                    return row
            return None

    def find_by(self, module: str, **criteria: Any) -> list[dict[str, Any]]:
        with self._lock:
            result: list[dict[str, Any]] = []
            for row in self.rows(module):
                if all(row.get(key) == value for key, value in criteria.items()):
                    result.append(row)
            return result

    def find_one_by(self, module: str, **criteria: Any) -> dict[str, Any] | None:
        matches = self.find_by(module, **criteria)
        return matches[0] if matches else None

    # ------------------------------------------------------------------ 写入
    def next_id(self, module: str) -> int:
        with self._lock:
            rows = self.rows(module)
            return max((int(row.get("id", 0)) for row in rows), default=0) + 1

    def append_row(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """追加一行；id 由仓库统一分配，调用方不用自己猜。"""
        with self._lock:
            rows = self.rows(module)
            entry = dict(row)
            if not entry.get("id"):
                entry["id"] = max((int(item.get("id", 0)) for item in rows), default=0) + 1
            rows.append(entry)
            self._touch(module)
            self._persist()
            return entry

    def insert_unique(
        self,
        module: str,
        key: Callable[[dict[str, Any]], Any],
        row: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """按业务唯一键插入；并发调用时只有第一个能成功创建。

        返回 ``(记录, 是否新建)``。已有记录原样返回，由调用方决定是复用还是拒绝。
        """
        with self._lock:
            wanted = key(row)
            for existing in self.rows(module):
                if key(existing) == wanted:
                    return existing, False
            entry = dict(row)
            rows = self.rows(module)
            if not entry.get("id"):
                entry["id"] = max((int(item.get("id", 0)) for item in rows), default=0) + 1
            rows.append(entry)
            self._touch(module)
            self._persist()
            return entry, True

    def update_row(self, module: str, entry_id: int, patch: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            entry = self.find(module, entry_id)
            if entry is None:
                return None
            entry.update(patch)
            self._touch(module)
            self._persist()
            return entry

    def replace_rows(self, module: str, rows: list[dict[str, Any]]) -> None:
        """一次性替换整批数据。

        ``rows`` 已经在调用方完整构造好；这里先复制到暂存列表，再整体换入。
        如果构造阶段抛异常，本方法根本不会被调用，旧数据集保持可用。
        """
        with self._lock:
            staged = [dict(row) for row in rows]
            self._tables[module] = staged
            self._touch(module, complete=True)
            self._persist()

    # ------------------------------------------------------------------ 版本与恢复
    def version(self, module: str) -> int:
        with self._lock:
            return self._versions.get(module, 1)

    def last_complete_version(self, module: str) -> int:
        with self._lock:
            return self._last_complete.get(module, 1)

    def checkpoint(self, module: str) -> int:
        """标记当前版本为“上一完整版本”，供中断后继续。"""
        with self._lock:
            self._last_complete[module] = self._versions.get(module, 1)
            self._recovery_snapshot[module] = [dict(row) for row in self.rows(module)]
            self._persist()
            return self._last_complete[module]

    def recovery_info(self, module: str) -> dict[str, Any]:
        with self._lock:
            return {
                "模块": module,
                "当前版本": self._versions.get(module, 1),
                "上一完整版本": self._last_complete.get(module, 1),
                "记录数": len(self.rows(module)),
                "持久化文件": self._path or "未启用（内存模式）",
                "数据来源": self._loaded_from,
            }

    def _touch(self, module: str, *, complete: bool = False) -> None:
        self._versions[module] = self._versions.get(module, 1) + 1
        if complete:
            self._last_complete[module] = self._versions[module]

    # ------------------------------------------------------------------ 持久化
    def _load_from_disk(self) -> None:
        for candidate, source in ((self._path, "主文件"), (f"{self._path}.bak", "备份文件")):
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            tables = payload.get("tables")
            if not isinstance(tables, dict):
                continue
            if not all(isinstance(value, list) for value in tables.values()):
                continue
            self._tables = {name: [dict(row) for row in rows] for name, rows in tables.items()}
            self._versions = {
                name: int(payload.get("versions", {}).get(name, 1)) for name in self._tables
            }
            self._last_complete = {
                name: int(payload.get("last_complete", {}).get(name, 1)) for name in self._tables
            }
            self._loaded_from = source
            return
        self._loaded_from = "seed（未找到可用的持久化数据）"

    def _persist(self) -> None:
        if not self._path:
            return
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        payload = {
            "tables": self._tables,
            "versions": self._versions,
            "last_complete": self._last_complete,
        }
        tmp_path = f"{self._path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        backup_path = f"{self._path}.bak"
        # 写备份前先确认主文件可解析；写坏的主文件绝不能覆盖好备份。
        if _is_complete_file(self._path):
            try:
                shutil.copyfile(self._path, backup_path)
            except OSError:
                pass
        os.replace(tmp_path, self._path)
        # 第一次落盘时还没有备份：把刚写好的完整文件复制一份，保证任何时刻都有可回退版本。
        if not os.path.exists(backup_path) and _is_complete_file(self._path):
            try:
                shutil.copyfile(self._path, backup_path)
            except OSError:
                pass


def _is_complete_file(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("tables"), dict)

    # ------------------------------------------------------------------ 概览
    def overview(self) -> dict[str, object]:
        labels = {"subscriptionpush": "订阅推送记录", "pendinganalysis": "待推送分析数据"}
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": labels.get(name, name),
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
