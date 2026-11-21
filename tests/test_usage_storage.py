"""用量逐笔事务、旧格式迁移、读取缓存失效和资源游标回归。"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import ModelEntry
from spiritscout.core.models import BlackboardLayer
from spiritscout.gateway import ModelGateway, ModelRegistry
from spiritscout.gateway.usage import UsageLedger, _file_cache, bind_usage, read_usage
from spiritscout.resources.store import ResourceStore


class UsageStorageTests(unittest.TestCase):
    def test_migrate_once_preserve_history_and_append_without_full_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            baseline = {"used_tokens": 100, "used_usd": .1, "per_model_tokens": {"old": 100},
                        "calls": 1, "call_series": [{"ts": 1, "tokens": 100, "model": "old",
                        "input_tokens": 90, "output_tokens": 10, "cached_input_tokens": 50}]}
            path.write_text(json.dumps(baseline))
            original = path.read_bytes()
            gateway = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]))
            ledger = bind_usage(gateway, path)
            with patch.object(gateway.budget, "persist_snapshot", side_effect=AssertionError("不能全量重写")):
                for index in range(100):
                    gateway.budget.record("new", 20, .01, ts=index + 2, call_id=f"call-{index}",
                                          input_tokens=15, output_tokens=5, cached_input_tokens=10)
            snapshot = ledger.read()
            self.assertEqual(snapshot["used_tokens"], 2100)
            self.assertEqual(snapshot["calls"], 101)
            self.assertEqual(len(snapshot["call_series"]), 101)
            self.assertEqual(snapshot["cache"]["cached_input_tokens"], 1050)
            self.assertEqual(path.read_bytes(), original, "旧格式原件保留，不反复重写")
            # 重启重建只回灌一次；重复 call_id 不重复计费。
            resumed = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]))
            new_ledger = bind_usage(resumed, path)
            new_ledger.append({"ts": 100, "model": "new", "tokens": 20, "call_id": "call-99"})
            self.assertEqual(new_ledger.read()["used_tokens"], 2100)
            self.assertEqual(resumed.budget.used_tokens, 2100)

    def test_read_cache_is_invalidated_by_atomic_replacement_and_is_not_mutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            path.write_text('{"used_tokens":100}')
            original_reader = Path.read_bytes
            with patch.object(Path, "read_bytes", autospec=True, side_effect=original_reader) as reader:
                one = read_usage(path)
                one["used_tokens"] = 12345
                self.assertEqual(read_usage(path)["used_tokens"], 100)
                self.assertEqual(reader.call_count, 1, "同一版本只读和校验一次")
            modified = path.stat().st_mtime_ns
            replacement = path.with_suffix(".tmp")
            replacement.write_text('{"used_tokens":900}')
            os.utime(replacement, ns=(modified, modified))
            replacement.replace(path)
            self.assertEqual(read_usage(path)["used_tokens"], 900, "同长度、同 mtime 替换也必须失效")

    def test_cache_retains_only_latest_snapshot_for_each_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            cache = _file_cache(str(path.resolve()), False)
            for tokens in range(30):
                # 用长度变化的载荷验证“只保留最新快照”。同尺寸原位重写在
                # 粗粒度时间戳文件系统（WSL DrvFs 等）上无法被 stat 版本
                # 检测；生产写入走原子替换，见下一个用例。
                path.write_text(json.dumps({"used_tokens": tokens, "pad": "x" * tokens}))
                self.assertEqual(read_usage(path)["used_tokens"], tokens)
                self.assertIs(_file_cache(str(path.resolve()), False), cache)
            self.assertEqual(cache.snapshot["used_tokens"], 29)

    def test_transactions_survive_reopen_and_do_not_expose_uncommitted_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = UsageLedger(Path(directory) / "usage.db")
            with sqlite3.connect(ledger.path) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("INSERT INTO calls(call_id,data) VALUES(?,?)",
                               ("pending", json.dumps({"call_id": "pending", "ts": 1, "model": "m", "tokens": 42})))
                self.assertEqual(ledger.read()["used_tokens"], 0)
                writer.commit()
                self.assertEqual(ledger.read()["used_tokens"], 42)
            self.assertEqual(UsageLedger(ledger.path).read()["used_tokens"], 42)

    def test_multiple_writers_and_duplicate_deliveries_keep_exact_totals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = UsageLedger(Path(directory) / "usage.db")
            def append(index: int) -> None:
                ledger.append({"call_id": str(index % 20), "ts": index + 1, "tokens": 7, "model": "m"})
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(append, range(40)))
            self.assertEqual(ledger.read()["used_tokens"], 140)
            self.assertEqual(ledger.read()["calls"], 20)

    def test_summary_matches_full_snapshot_and_keeps_history_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            legacy = json.dumps({"used_tokens": 12, "calls": 1, "call_series": [
                {"ts": 1, "model": "fixture", "tokens": 12, "input_tokens": 10,
                 "output_tokens": 2, "cached_input_tokens": 8}]})
            for use_ledger in (False, True):
                with self.subTest(ledger=use_ledger):
                    path.write_text(legacy)
                    ledger = UsageLedger(path.with_suffix('.db'), path) if use_ledger else None
                    full = read_usage(path)
                    selected = frozenset({"tokens", "context_parts"})
                    projected = read_usage(path, call_fields=selected)
                    self.assertEqual(projected, {**full, 'call_series': [
                        {k: v for k, v in row.items() if k in selected} for row in full['call_series']]})
                    projected['call_series'][0]['context_parts']['changed'] = 1
                    self.assertEqual(read_usage(path), full, '投影不能改写底层调用缓存')
                    summary = read_usage(path, include_calls=False)
                    self.assertEqual(summary, {k: v for k, v in full.items() if k != 'call_series'})
                    self.assertEqual(summary['cache']['hit_rate'], .8)
                    summary['cache']['groups'].clear()
                    summary['per_model_tokens']['fixture'] = 999
                    self.assertEqual(read_usage(path), full, '摘要调用方不能改写缓存或明细')
                    if ledger:
                        ledger.append({"call_id": "new", "ts": 2, "model": "fixture", "tokens": 5,
                                       "input_tokens": 4, "output_tokens": 1, "cached_input_tokens": 2})
                    else:
                        replacement = path.with_suffix('.tmp')
                        replacement.write_text('{"used_tokens":99}')
                        replacement.replace(path)
                    updated = read_usage(path)
                    self.assertEqual(read_usage(path, include_calls=False),
                                     {k: v for k, v in updated.items() if k != 'call_series'})


class ResourceImportPerformanceTests(unittest.TestCase):
    def test_unchanged_import_is_empty_then_reads_only_new_versions_and_full_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "sess-fixture"
            workspace.mkdir()
            bb = Blackboard(workspace / "bb.db")
            store = ResourceStore(root / "resources")
            def write(value: str) -> None:
                bb.write(BlackboardLayer.ASSET, "app", {"asset_type": "app", "value": "https://example.test",
                                                       "fingerprint": {"server": value}})
            try:
                write("one")
                self.assertGreater(store.import_session(workspace), 0)
                self.assertEqual(store.import_session(workspace), 0)
                write("two")
                self.assertEqual(store.import_session(workspace), 1)
                self.assertEqual(store.import_session(workspace), 0)
                self.assertGreater(store.import_session(workspace, full=True), 0)
                self.assertEqual(store.assets()["items"][0]["observations"], 2)
                self.assertEqual(store.assets()["items"][0]["data"]["fingerprint"]["server"], "two")
            finally:
                bb.close()

    def test_failed_import_replays_and_history_truncation_resets_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "sess-fixture"
            workspace.mkdir()
            bb = Blackboard(workspace / "bb.db")
            store = ResourceStore(root / "resources")
            try:
                for index in range(3):
                    bb.write(BlackboardLayer.ASSET, "app", {"asset_type": "app", "value": "https://example.test",
                                                         "fingerprint": {"version": index}})
                with patch.object(store, "observe", side_effect=RuntimeError("写入失败")):
                    with self.assertRaises(RuntimeError):
                        store.import_session(workspace)
                self.assertGreater(store.import_session(workspace), 0)
                self.assertEqual(store.import_session(workspace), 0)
                with sqlite3.connect(workspace / "bb.db") as writer:
                    writer.execute("DELETE FROM bb_record_history")
                self.assertGreater(store.import_session(workspace), 0)
                self.assertEqual(store.import_session(workspace), 0, "游标回退后不能永远重复全扫")
            finally:
                bb.close()

    def test_batch_settings_only_reads_encryption_key_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ResourceStore(directory)
            for index in range(3):
                store.save("skill", str(index), {"body": "fixture"}, 0)
            with patch.object(store, "cipher", wraps=store.cipher) as cipher:
                self.assertEqual(len(store.settings("skill")), 3)
                self.assertEqual(cipher.call_count, 1)
