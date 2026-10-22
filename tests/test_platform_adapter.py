"""平台适配器测试：档案解析、列题映射、实例生命周期、提交判定。

平台契约回到适配器边界：HTTP 用 httpx.MockTransport 模拟（含传输层
失败），其余全走真实代码路径——URL 渲染、字段映射、名额与节流、
审计打码都在测试范围内。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import yaml

from spiritscout.core.audit import AuditLogger
from spiritscout.ctf.platform import (
    PlatformAdapter,
    parse_platform_profile,
)

PROFILE: dict[str, Any] = {
    "name": "tsecbench",
    "base_url": "http://10.0.100.58",
    "headers": {"X-API-Key": "secret-key"},
    "ops": {
        "precheck": {"method": "GET", "url": "http://10.0.100.58"},
        "list": {"method": "GET", "path": "/api/v1/challenges",
                 "items_field": "data.challenges"},
        "start": {"method": "POST", "path": "/api/v1/challenge/start",
                  "body": {"unique_code": "{challenge}"},
                  "addr_field": "container_addr"},
        "close": {"method": "POST", "path": "/api/v1/challenge/close",
                  "body": {"unique_code": "{challenge}"}},
        "submit": {"method": "POST", "path": "/api/v1/challenge/submit",
                   "body": {"unique_code": "{challenge}", "flag": "{flag}"},
                   "accepted_field": "correct", "duplicate_code": "duplicate"},
    },
    "limits": {"max_active": 2},
    "challenge_fields": {
        "id": "unique_code", "title": "description",
        "difficulty": "difficulty", "container_addr": "container_addr",
        "is_completed": "is_completed",
    },
}


def _challenge(cid: str, *, addr: str = "", done: bool = False) -> dict[str, Any]:
    return {"unique_code": cid, "description": f"题目 {cid}", "difficulty": "easy",
            "container_addr": [addr] if addr else [],
            "is_completed": done, "total_score": 100}


class FakePlatform:
    """MockTransport 的后端：记录请求，按路径分发应答。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Any] = {}
        self.challenges: list[dict[str, Any]] = []

    def route(self, path: str, handler: Any) -> None:
        self.routes[path] = handler

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fn = self.routes.get(request.url.path)
        if fn is None:
            return httpx.Response(404, json={"code": "not_found"})
        return fn(request)


class PlatformAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.audit = AuditLogger(self.root / "audit.jsonl")

    def adapter(self, profile: dict[str, Any] | None = None,
                platform: FakePlatform | None = None,
                *, max_active: int | None = None,
                progress_path: Path | None = None,
                ) -> tuple[PlatformAdapter, FakePlatform]:
        raw = json.loads(json.dumps(profile or PROFILE))
        if max_active is not None:
            raw.setdefault("limits", {})["max_active"] = max_active
        fake = platform or FakePlatform()
        adapter = PlatformAdapter(
            parse_platform_profile(raw), audit=self.audit,
            progress_path=progress_path)
        real = httpx.AsyncClient

        def factory(**kw: Any) -> httpx.AsyncClient:
            return real(transport=httpx.MockTransport(fake.handler), **kw)

        patcher = mock.patch.object(httpx, "AsyncClient", side_effect=factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addAsyncCleanup(adapter.aclose)
        return adapter, fake

    def _audit_lines(self) -> list[dict[str, Any]]:
        path = self.root / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # ------------------------------------------------------------ 档案解析

    def test_profile_parse_variants_and_redaction(self) -> None:
        profile = parse_platform_profile(PROFILE)
        self.assertEqual(profile.name, "tsecbench")
        self.assertEqual(profile.ops["submit"].accepted_field, "correct")
        self.assertEqual(profile.redacted()["headers"], {"X-API-Key": "***"})
        self.assertEqual(profile.to_dict()["headers"]["X-API-Key"], "secret-key")
        # YAML 文本形态
        from_yaml = parse_platform_profile(
            yaml.dump(PROFILE, allow_unicode=True, sort_keys=False))
        self.assertEqual(from_yaml.challenge_fields["id"], "unique_code")
        # {"platform": {...}} 包裹形态
        wrapped = parse_platform_profile({"platform": dict(PROFILE)})
        self.assertEqual(wrapped.base_url, PROFILE["base_url"])
        # 有列题接口：显式字段映射缺题号 → 拒收（列题必须能取到题号）；
        # 未声明映射时落到默认 id/title
        with self.assertRaises(ValueError):
            parse_platform_profile({"ops": {"list": "/x"},
                                    "challenge_fields": {"title": "name"}})
        defaulted = parse_platform_profile({"ops": {"list": "/x"}})
        self.assertEqual(defaulted.challenge_fields["id"], "id")
        # 路径简写：# 字符串形态
        short = parse_platform_profile(
            {"base_url": "http://p", "ops": {"list": "/challenges"}})
        self.assertEqual(short.ops["list"].http_method(), "GET")

    # ------------------------------------------------------------ 预检与列题

    async def test_precheck_list_mapping_and_refresh_throttle(self) -> None:
        adapter, fake = self.adapter()
        fake.challenges = [_challenge("c1", addr="10.0.100.58:30001"),
                           _challenge("c2")]
        fake.route("/", lambda req: httpx.Response(200, json={"status": "ok"}))
        fake.route("/api/v1/challenges",
                   lambda req: httpx.Response(200, json={"data": {"challenges": fake.challenges}}))

        pre = await adapter.precheck()
        self.assertTrue(pre["ok"])
        listed = await adapter.list_challenges()
        self.assertEqual([c["id"] for c in listed], ["c1", "c2"])
        c1 = next(c for c in listed if c["id"] == "c1")
        self.assertEqual(c1["title"], "题目 c1")
        self.assertEqual(c1["url"], "http://10.0.100.58:30001")  # 地址归一补 scheme
        self.assertEqual(c1["platform"], "tsecbench")
        self.assertEqual(c1["difficulty"], "easy")  # 非接线字段按事实透传
        n_before = len(fake.requests)
        await adapter.refresh()          # 节流窗口内：不发请求，回快照
        self.assertEqual(len(fake.requests), n_before)
        await adapter.refresh(force=True)
        self.assertEqual(len(fake.requests), n_before + 1)
        actions = [f["action"] for f in adapter.facts]
        self.assertIn("precheck", actions)
        self.assertIn("list_refreshed", actions)
        # 审计打码：凭据值不得出现在审计文件里
        raw = (self.root / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("secret-key", raw)

    async def test_list_failure_keeps_snapshot_and_records_fact(self) -> None:
        adapter, fake = self.adapter()
        fake.route("/", lambda req: httpx.Response(200, json={"status": "ok"}))
        fake.challenges = [_challenge("c1")]
        fake.route("/api/v1/challenges",
                   lambda req: httpx.Response(200, json={"data": {"challenges": fake.challenges}}))
        await adapter.list_challenges()
        fake.routes["/api/v1/challenges"] = lambda req: httpx.Response(503, text="down")
        kept = await adapter.refresh(force=True)
        self.assertEqual([c["id"] for c in kept], ["c1"])  # 保留上次快照
        self.assertIn("list_error", [f["action"] for f in adapter.facts])

    async def test_progress_snapshot_persists_solved_and_total_ids(self) -> None:
        progress_path = self.root / "competition_progress.json"
        adapter, fake = self.adapter(progress_path=progress_path)
        fake.challenges = [_challenge("c1", done=True), _challenge("c2")]
        fake.route("/api/v1/challenges",
                   lambda req: httpx.Response(
                       200, json={"data": {"challenges": fake.challenges}}))

        await adapter.list_challenges()
        progress = adapter.progress()
        stored = json.loads(progress_path.read_text(encoding="utf-8"))

        self.assertEqual(progress["total_challenges"], 2)
        self.assertEqual(progress["solved_ids"], ["c1"])
        self.assertEqual(progress["unsolved_ids"], ["c2"])
        self.assertEqual(stored["solved_ids"], ["c1"])
        self.assertEqual(adapter.summary()["total_challenges"], 2)

    # ------------------------------------------------------------ 实例生命周期

    async def test_accepted_submission_persists_fresh_platform_progress(self) -> None:
        path = self.root / "competition_progress.json"
        adapter, fake = self.adapter(progress_path=path)
        fake.challenges = [_challenge("c1"), _challenge("c2")]
        fake.route("/api/v1/challenges", lambda req: httpx.Response(
            200, json={"data": {"challenges": fake.challenges}}))
        await adapter.list_challenges()

        def submit(req: httpx.Request) -> httpx.Response:
            fake.challenges[0]["is_completed"] = True
            return httpx.Response(200, json={"correct": True})

        fake.route("/api/v1/challenge/submit", submit)
        result = await adapter.submit("flag{demo}", "c1")
        stored = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(result.accepted)
        self.assertEqual(stored["solved_ids"], ["c1"])
        self.assertEqual(stored["unsolved_ids"], ["c2"])
        self.assertEqual(stored["total_challenges"], 2)

    async def test_partial_submission_does_not_mark_challenge_solved(self) -> None:
        path = self.root / "competition_progress.json"
        adapter, fake = self.adapter(progress_path=path)
        fake.route("/api/v1/challenges", lambda req: httpx.Response(
            200, json={"data": {"challenges": [_challenge("c1")]}}))
        fake.route("/api/v1/challenge/submit", lambda req: httpx.Response(
            200, json={"correct": True}))
        await adapter.list_challenges()

        await adapter.submit("flag{partial}", "c1")

        self.assertEqual(adapter.progress()["solved_ids"], [])
        self.assertEqual(adapter.progress()["unsolved_ids"], ["c1"])

    async def test_ensure_instance_idempotent_and_capped(self) -> None:
        adapter, fake = self.adapter(max_active=1)
        starts: list[str] = []

        def start(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            starts.append(body["unique_code"])
            return httpx.Response(200, json={"unique_code": body["unique_code"],
                                             "container_addr": f"{body['unique_code']}.box:8080"})

        fake.route("/api/v1/challenge/start", start)
        fake.route("/api/v1/challenge/close",
                   lambda req: httpx.Response(200, json={"closed": True}))
        c1 = {"id": "c1", "title": "题目"}
        got = await adapter.ensure_instance(c1)
        self.assertEqual(got["url"], "http://c1.box:8080")
        again = await adapter.ensure_instance({"id": "c1", "title": "题目"})
        self.assertEqual(again["url"], "http://c1.box:8080")
        self.assertEqual(starts, ["c1"])              # 幂等：不重复开
        self.assertIsNone(await adapter.ensure_instance({"id": "c2"}))  # 名额满
        self.assertIn("instance_wait", [f["action"] for f in adapter.facts])
        await adapter.close_instance("c1")
        got2 = await adapter.ensure_instance({"id": "c2"})
        self.assertEqual(got2["url"], "http://c2.box:8080")  # 名额释放后补位
        self.assertEqual(starts, ["c1", "c2"])

    async def test_close_failure_keeps_instance_for_close_all_retry(self) -> None:
        adapter, fake = self.adapter(max_active=2)
        fake.route("/api/v1/challenge/start", lambda req: httpx.Response(
            200, json={"container_addr": "c1.box:8080"}))
        got = await adapter.ensure_instance({"id": "c1", "title": "题目"})
        self.assertEqual(got["url"], "http://c1.box:8080")
        attempts: list[int] = []

        def close(req: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(500, json={"code": "boom"})
            return httpx.Response(200, json={"closed": True})

        fake.route("/api/v1/challenge/close", close)
        self.assertFalse(await adapter.close_instance("c1", reason="故障"))
        # 失败不销账：记录仍在，会话收尾 close_all 还能重试并成功
        self.assertEqual(adapter.summary()["active_instances"], 1)
        self.assertEqual(await adapter.close_all(), 1)
        self.assertEqual(adapter.summary()["active_instances"], 0)
        self.assertEqual(len(attempts), 2)
        actions = [f["action"] for f in adapter.facts]
        self.assertIn("instance_close_failed", actions)
        self.assertIn("instance_closed", actions)

    async def test_start_failure_backoff_and_multi_challenge_isolation(self) -> None:
        adapter, fake = self.adapter(max_active=2)
        calls: list[str] = []

        def flaky(req: httpx.Request) -> httpx.Response:
            cid = json.loads(req.content)["unique_code"]
            calls.append(cid)
            if cid == "c1" and len(calls) == 1:  # 首次传输层失败
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"container_addr": f"{cid}.box:9000"})

        fake.route("/api/v1/challenge/start", flaky)
        fake.route("/api/v1/challenge/close", lambda req: httpx.Response(200, json={"closed": True}))
        self.assertIsNone(await adapter.ensure_instance({"id": "c1"}))  # 传输失败
        self.assertIsNone(await adapter.ensure_instance({"id": "c1"}))  # 退避窗口内不发请求
        self.assertEqual(calls, ["c1"])
        adapter._start_blocked_until = 0.0                              # 退避结束
        self.assertEqual((await adapter.ensure_instance({"id": "c1"}))["url"],
                         "http://c1.box:9000")
        got2 = await adapter.ensure_instance({"id": "c2"})
        self.assertEqual(got2["url"], "http://c2.box:9000")
        # 多题实例隔离：关一题不影响另一题
        await adapter.close_instance("c1")
        self.assertEqual(adapter.summary()["active_instances"], 1)
        # 重启：先关后开
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            restarted = await adapter.restart_instance({"id": "c2"})
        self.assertEqual(restarted["url"], "http://c2.box:9000")
        self.assertEqual(adapter.summary()["active_instances"], 1)

    async def test_start_without_addr_is_not_treated_as_ready(self) -> None:
        """2xx 但缺地址（异步供给/字段不符）：不能占着名额装成就绪。

        修复前：空地址照记「已启动」，该题永远拿不到入口还白占一个活跃
        名额——其他题也被名额挡在门外，会话整体停滞。
        """
        adapter, fake = self.adapter(max_active=1)
        calls: list[str] = []

        def start(req: httpx.Request) -> httpx.Response:
            cid = json.loads(req.content)["unique_code"]
            calls.append(cid)
            if len(calls) == 1:
                return httpx.Response(200, json={"unique_code": cid})   # 无地址
            return httpx.Response(200, json={"container_addr": f"{cid}.box:8080"})

        fake.route("/api/v1/challenge/start", start)
        self.assertIsNone(await adapter.ensure_instance({"id": "c1"}))  # 未就绪
        self.assertIsNone(await adapter.ensure_instance({"id": "c1"}))  # 退避内不重发
        self.assertEqual(calls, ["c1"])
        self.assertIn("instance_addr_missing",
                      [f["action"] for f in adapter.facts])
        adapter._start_blocked_until = 0.0                              # 退避结束
        got = await adapter.ensure_instance({"id": "c1"})
        self.assertEqual(got["url"], "http://c1.box:8080")
        self.assertEqual(calls, ["c1", "c1"])

    async def test_release_completed_prefers_platform_completion_signal(self) -> None:
        adapter, fake = self.adapter(max_active=2)
        fake.route("/api/v1/challenge/start",
                   lambda req: httpx.Response(
                       200, json={"container_addr": "box:7000"}))
        closed: list[str] = []

        def close(req: httpx.Request) -> httpx.Response:
            cid = json.loads(req.content)["unique_code"]
            closed.append(cid)
            return httpx.Response(200, json={"unique_code": cid, "closed": True})

        fake.route("/api/v1/challenge/close", close)
        fake.challenges = [_challenge("c1", done=True), _challenge("c2", done=False)]
        fake.route("/api/v1/challenges",
                   lambda req: httpx.Response(200, json={"data": {"challenges": fake.challenges}}))
        await adapter.ensure_instance({"id": "c1"})
        await adapter.ensure_instance({"id": "c2"})
        released = await adapter.release_completed(accepted_ids={"c2"})
        # 平台有完成态映射时以平台为准：题 1 已完成回收，"c2 已被判对" 不算完成
        self.assertEqual(released, ["c1"])
        self.assertEqual(closed, ["c1"])
        self.assertEqual(adapter.summary()["active_instances"], 1)

    async def test_release_falls_back_to_accepted_flags_without_signal(self) -> None:
        raw = json.loads(json.dumps(PROFILE))
        raw["challenge_fields"].pop("is_completed")
        adapter, fake = self.adapter(raw, max_active=2)
        fake.route("/api/v1/challenge/start",
                   lambda req: httpx.Response(200, json={"container_addr": "box:7000"}))
        fake.route("/api/v1/challenge/close",
                   lambda req: httpx.Response(200, json={"closed": True}))
        self.assertFalse(adapter.has_completion_signal())
        await adapter.ensure_instance({"id": "c1"})
        await adapter.ensure_instance({"id": "c2"})
        released = await adapter.release_completed(accepted_ids={"c1"})
        self.assertEqual(released, ["c1"])
        self.assertEqual(adapter.summary()["active_instances"], 1)
        self.assertEqual(await adapter.close_all(), 1)   # 会话收尾兜底回收
        self.assertEqual(adapter.summary()["active_instances"], 0)

    # ------------------------------------------------------------ 提交判定

    async def test_submit_acceptance_matrix(self) -> None:
        adapter, fake = self.adapter()
        fake.routes["/api/v1/challenge/submit"] = lambda req: httpx.Response(
            200, json={"correct": True, "awarded": 100})
        res = await adapter.submit("flag{a}", "c1")
        self.assertIs(res.accepted, True)
        sent = json.loads(fake.requests[-1].content)
        self.assertEqual(sent, {"unique_code": "c1", "flag": "flag{a}"})

        fake.routes["/api/v1/challenge/submit"] = lambda req: httpx.Response(
            200, json={"correct": False})
        self.assertIs((await adapter.submit("flag{b}", "c1")).accepted, False)

        fake.routes["/api/v1/challenge/submit"] = lambda req: httpx.Response(
            409, json={"code": "duplicate", "message": "already submitted"})
        # 重复提交 = 该 Flag 已计分（幂等），视为正确而非失败
        self.assertIs((await adapter.submit("flag{a}", "c1")).accepted, True)

        fake.routes["/api/v1/challenge/submit"] = lambda req: httpx.Response(
            400, json={"code": "invalid_flag", "message": "惠"})
        self.assertIs((await adapter.submit("flag{x}", "c1")).accepted, False)

        fake.routes["/api/v1/challenge/submit"] = lambda req: httpx.Response(
            500, text="oops")
        self.assertIsNone((await adapter.submit("flag{a}", "c1")).accepted)  # 退避重试

        def boom(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        fake.routes["/api/v1/challenge/submit"] = boom
        self.assertIsNone((await adapter.submit("flag{a}", "c1")).accepted)

        fake.routes["/api/v1/challenge/submit"] = lambda req: httpx.Response(200, json={})
        self.assertIsNone((await adapter.submit("flag{a}", "c1")).accepted)  # 无判定不误杀

    async def test_all_completed_uses_platform_completion_state(self) -> None:
        adapter, fake = self.adapter()
        self.assertFalse(adapter.all_completed())        # 尚未列题不宣称完成
        fake.route("/api/v1/challenges",
                   lambda req: httpx.Response(200, json={"data": {"challenges": fake.challenges}}))
        fake.challenges = [_challenge("c1", done=True), _challenge("c2", done=False)]
        await adapter.list_challenges()
        self.assertFalse(adapter.all_completed())        # 一题未完赛不早退
        fake.challenges = [_challenge("c1", done=True), _challenge("c2", done=True)]
        await adapter.refresh(force=True)
        self.assertTrue(adapter.all_completed())
        # 无完成态映射：不参与收手裁定
        raw = json.loads(json.dumps(PROFILE))
        raw["challenge_fields"].pop("is_completed")
        adapter2, _ = self.adapter(raw)
        self.assertFalse(adapter2.all_completed())

    # ------------------------------------------------------------ 持题与摘要

    async def test_owns_without_list_op_claims_all(self) -> None:
        raw = json.loads(json.dumps(PROFILE))
        raw["ops"].pop("list")
        raw["challenge_fields"].pop("is_completed", None)
        adapter, _ = self.adapter(raw)
        self.assertTrue(adapter.owns("anything"))
        self.assertFalse(adapter.has_completion_signal())  # 无列题即无完成态信号
        # 无 start 接口（如纯提交型平台）：ensure 原样返回题目
        raw2 = json.loads(json.dumps(PROFILE))
        raw2["ops"].pop("start")
        adapter2, fake2 = self.adapter(raw2)
        got = await adapter2.ensure_instance({"id": "c1", "url": "http://x"})
        self.assertEqual(got["url"], "http://x")
        self.assertEqual(fake2.requests, [])


if __name__ == "__main__":
    unittest.main()
