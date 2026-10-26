"""审批、重放、真实连接与 DAG 依赖的行为回归。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from spiritscout.agents.master.dag import TaskDAG
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import RunMode, Scope, TaskStatus
from spiritscout.policy.engine import PolicyEngine
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.builtin import BUILTIN_SPECS, http_probe
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.http_transport import ScopedHTTPTransport
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.spec import ToolKind, ToolResult, ToolSpec


class ExecutionRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_or_missing_action_never_reaches_executor(self) -> None:
        gate = AsyncMock(return_value=True)
        policy = PolicyEngine(RunMode.CTF, ScopeGuard(Scope(local_analysis=True)),
                              approval_gate=gate)
        registry = ToolRegistry(LocalExecutor(), policy=policy)
        execute = AsyncMock(return_value=ToolResult(tool="sandbox_exec", ok=True))
        registry.register_builtin("sandbox_exec", execute, BUILTIN_SPECS["sandbox_exec"])
        for params in [{"code": "test"}, {"code": "test", "action": "unclassified"}]:
            self.assertFalse((await registry.call("sandbox_exec", params)).ok)
        execute.assert_not_awaited()
        gate.assert_not_awaited()
        self.assertTrue((await registry.call(
            "sandbox_exec", {"code": "test", "action": "analysis"})).ok)
        gate.assert_not_awaited()

    async def test_sensitive_action_is_denied_even_with_startup_allow(self) -> None:
        gate = AsyncMock(return_value=False)
        policy = PolicyEngine(RunMode.SRC, ScopeGuard(Scope(allow_sensitive_actions=True)),
                              approval_gate=gate)
        registry = ToolRegistry(LocalExecutor(), policy=policy)
        execute = AsyncMock()
        registry.register_builtin("sandbox_exec", execute, BUILTIN_SPECS["sandbox_exec"])
        result = await registry.call("sandbox_exec", {"code": "test", "action": "delete_files"})
        self.assertFalse(result.ok)
        gate.assert_awaited_once()
        execute.assert_not_awaited()

    async def test_write_error_is_not_retried_or_fallen_back(self) -> None:
        gate = AsyncMock(return_value=True)
        policy = PolicyEngine(RunMode.SRC, ScopeGuard(Scope()), approval_gate=gate)
        registry = ToolRegistry(LocalExecutor(), policy=policy, max_retries=3)
        spec = ToolSpec(name="http_probe", kind=ToolKind.BUILTIN, fallback_tool="other")
        execute = AsyncMock(return_value=ToolResult(tool="http_probe", ok=False, error="lost response"))
        fallback = AsyncMock(return_value=ToolResult(tool="other", ok=True))
        registry.register_builtin("http_probe", execute, spec)
        registry.register_builtin("other", fallback, ToolSpec(name="other", kind=ToolKind.BUILTIN))
        result = await registry.call("http_probe", {
            "url": "https://audit.test/", "method": "POST", "action": "intrusive_write",
        })
        self.assertFalse(result.ok)
        execute.assert_awaited_once()
        fallback.assert_not_awaited()

    async def test_post_analysis_is_still_not_automatically_replayed(self) -> None:
        registry = ToolRegistry(LocalExecutor())
        execute = AsyncMock(return_value=ToolResult(tool="http_probe", ok=False))
        registry.register_builtin("http_probe", execute, BUILTIN_SPECS["http_probe"])
        await registry.call("http_probe", {"method": "POST", "action": "analysis"})
        execute.assert_awaited_once()
        execute.reset_mock()
        self.assertFalse((await registry.call("http_probe", {"method": "DELETE"})).ok)
        execute.assert_not_awaited()

    async def test_readonly_probe_can_retry(self) -> None:
        registry = ToolRegistry(LocalExecutor())
        execute = AsyncMock(side_effect=[ToolResult(tool="http_probe", ok=False),
                                         ToolResult(tool="http_probe", ok=True)])
        registry.register_builtin("http_probe", execute, BUILTIN_SPECS["http_probe"])
        self.assertTrue((await registry.call("http_probe", {"method": "GET"})).ok)
        self.assertEqual(execute.await_count, 2)

    async def test_missing_program_returns_structured_result(self) -> None:
        result = await LocalExecutor().run(ToolSpec(
            name="missing", kind=ToolKind.CLI,
            command_template=["/nonexistent/spiritscout-regression"],
        ), {})
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 127)

    async def test_mcp_action_argument_is_forwarded_as_remote_data(self) -> None:
        policy = PolicyEngine(RunMode.SRC, ScopeGuard(Scope()),
                              approval_gate=AsyncMock(return_value=True))
        registry = ToolRegistry(LocalExecutor(), policy=policy)
        execute = AsyncMock(return_value=ToolResult(tool="remote", ok=True))
        registry.register_builtin("remote", execute, ToolSpec(name="remote", kind=ToolKind.MCP))
        self.assertTrue((await registry.call("remote", {"action": "inspect_function"})).ok)
        execute.assert_awaited_once_with(action="inspect_function")

    async def test_known_sensitive_mcp_action_still_requires_approval(self) -> None:
        gate = AsyncMock(return_value=False)
        policy = PolicyEngine(RunMode.SRC, ScopeGuard(Scope()), approval_gate=gate)
        registry = ToolRegistry(LocalExecutor(), policy=policy)
        execute = AsyncMock()
        registry.register_builtin("remote", execute, ToolSpec(name="remote", kind=ToolKind.MCP))
        self.assertFalse((await registry.call("remote", {"action": "reverse_shell"})).ok)
        gate.assert_awaited_once()
        execute.assert_not_awaited()


class DAGRegressionTests(unittest.TestCase):
    def test_multi_target_field_rejected_without_partial_nodes(self) -> None:
        """坏目标由规划器修正，不静默挑第一个地址或丢失其它目标。"""
        for bad in ("http://10.0.100.58, https://bench.test", "a.test,b.test",
                    ["a.test", "b.test"], "http://10.0.100.58,%20https://bench.test"):
            for field in ({"target": bad}, {"meta": {"target": bad}}):
                with self.subTest(bad=bad, field=field):
                    dag = TaskDAG()
                    with self.assertRaisesRegex(SpiritScoutError, "target 必须是单个目标"):
                        dag.upsert_from_plan([{"tactic": "GOOD", "target": "https://a.test"},
                                              {"tactic": "BAD", **field}], 0)
                    self.assertEqual(dag.all_nodes(), [])

    def test_separate_targets_and_commas_in_url_paths_remain_valid(self) -> None:
        dag = TaskDAG()
        result = dag.upsert_from_plan([
            {"tactic": "RECON", "target": "http://10.0.100.58"},
            {"tactic": "RECON@2", "target": "https://bench.test/a,b?next=https://a.test"},
            {"tactic": "RECON@3", "target": "10.0.0.0/24"},
            {"tactic": "RECON@4", "target": "http://[::1]:8080/"},
        ], 0)
        self.assertEqual(len(result.created), 4)

    def test_failed_parent_replacement_rebinds_existing_child(self) -> None:
        dag = TaskDAG()
        dag.upsert_from_plan([{"tactic": "CHILD", "depends_on": ["PARENT"]},
                              {"tactic": "PARENT"}], 0)
        parent = dag.find_by_tactic("PARENT")[0]
        child = dag.find_by_tactic("CHILD")[0]
        dag.mark(parent.node_id, TaskStatus.FAILED)
        replacement = dag.upsert_from_plan([{"tactic": "PARENT"}], 1).created[0]
        dag.mark(replacement.node_id, TaskStatus.DONE)
        self.assertEqual(child.depends_on, [replacement.node_id])
        self.assertEqual(dag.ready_nodes(), [child])
        again = dag.upsert_from_plan([{"tactic": "PARENT"}], 2)
        self.assertEqual(again.created, [])
        # 跳过必须带回执原因：静默吞节点会让规划方误判「在途」
        self.assertEqual(len(again.skipped), 1)
        self.assertIn("已存在", again.skipped[0]["reason"])

    def test_invalid_replacement_rolls_back_rebindings(self) -> None:
        dag = TaskDAG()
        dag.upsert_from_plan([{"tactic": "PARENT"},
                              {"tactic": "CHILD", "depends_on": ["PARENT"]}], 0)
        dag.mark(dag.find_by_tactic("PARENT")[0].node_id, TaskStatus.FAILED)
        before = dag.to_dict()
        with self.assertRaises(SpiritScoutError):
            dag.upsert_from_plan([{"tactic": "PARENT", "depends_on": ["CHILD"]}], 1)
        self.assertEqual(dag.to_dict(), before)

    def test_same_tactic_two_challenges_both_land_and_dedupe_per_challenge(self) -> None:
        dag = TaskDAG()
        result = dag.upsert_from_plan([
            {"tactic": "CTF-EXPLOIT", "title": "题A 主攻",
             "meta": {"challenge": {"id": "A"}}},
            {"tactic": "CTF-EXPLOIT", "title": "题B 主攻",
             "meta": {"challenge": {"id": "B"}}},
        ], 0)
        self.assertEqual(len(result.created), 2)
        self.assertEqual(result.skipped, [])
        # 同题同战术重复规划被跳过；异题新题不受影响
        again = dag.upsert_from_plan([
            {"tactic": "CTF-EXPLOIT", "title": "题A 重规划",
             "meta": {"challenge": {"id": "A"}}},
            {"tactic": "CTF-EXPLOIT", "title": "题C 主攻",
             "meta": {"challenge": {"id": "C"}}},
        ], 1)
        self.assertEqual([n.meta["challenge"]["id"] for n in again.created], ["C"])
        self.assertEqual(len(again.skipped), 1)
        self.assertIn("赛题 A", again.skipped[0]["reason"])

    def test_challenge_dependency_prefers_same_challenge(self) -> None:
        dag = TaskDAG()
        dag.upsert_from_plan([
            {"tactic": "RECON", "title": "题A 侦察",
             "meta": {"challenge": {"id": "A"}}},
            {"tactic": "RECON", "title": "题B 侦察",
             "meta": {"challenge": {"id": "B"}}},
            {"tactic": "EXPLOIT", "title": "题B 攻击", "depends_on": ["RECON"],
             "meta": {"challenge": {"id": "B"}}},
        ], 0)
        recon = {n.meta["challenge"]["id"]: n for n in dag.find_by_tactic("RECON")}
        exploit = dag.find_by_tactic("EXPLOIT")[0]
        self.assertEqual(exploit.depends_on, [recon["B"].node_id])


class CredentialLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_chinese_user_reply_credential_is_ledgered_redacted_and_idempotent(self) -> None:
        from types import SimpleNamespace

        from spiritscout.agents.hostops.agent import PostExploitAgent
        from spiritscout.blackboard import Blackboard
        from spiritscout.core.models import AgentType, BlackboardLayer, Task

        with tempfile.TemporaryDirectory() as temp:
            bb = Blackboard(Path(temp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.RECON_CLUE, "reply-real", {
                "clue_type": "user_reply",
                "value": "账号18325032098,密码MQLMQLxxl123**",
                "detail": "请提供一组测试账号和密码"})
            # 提问正文含「密码」但答复本身没有：不得误报
            bb.write(BlackboardLayer.RECON_CLUE, "reply-mention", {
                "clue_type": "user_reply", "value": "稍后提供",
                "detail": "需要一次性下发测试账号与密码"})
            agent = object.__new__(PostExploitAgent)
            agent.agent_id = "post-test"
            agent.ctx = SimpleNamespace(blackboard=bb)
            task = Task(task_id="cred", task_type="post_exploit",
                        agent_type=AgentType.POST_EXPLOIT, target="https://example.test")

            result = await agent._step_credential(task)
            self.assertTrue(result.ok)
            self.assertNotIn("暂无凭据", result.note)
            self.assertNotIn("MQLMQLxxl123", result.note)
            self.assertNotIn("MQLMQLxxl123", str(result.findings))
            # 凭据线索落凭据层（post_exploit 对线索层无写权）
            leads = bb.query(BlackboardLayer.CREDENTIAL, limit=50)
            self.assertEqual(len(leads), 1)
            stored = str(leads[0]["data"])
            self.assertNotIn("MQLMQLxxl123", stored)
            self.assertNotIn("18325032098", stored)
            self.assertTrue(leads[0]["data"]["redacted"])

            # 再次执行：同一证据不重复登记，且不会从线索本身再派生线索
            await agent._step_credential(task)
            self.assertEqual(len(bb.query(BlackboardLayer.CREDENTIAL, limit=50)), 1)


class ConnectionRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_https_certificate_is_verified_against_original_hostname(self) -> None:
        # 证书只含域名、不含回环 IP；只有正确保留 SNI/验证名才能握手成功。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "audit.test")])
            now = datetime.now(UTC)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - timedelta(minutes=1))
                    .not_valid_after(now + timedelta(days=1))
                    .add_extension(x509.SubjectAlternativeName([x509.DNSName("audit.test")]), False)
                    .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
                    .sign(key, hashes.SHA256()))
            cert_path, key_path = root / "cert.pem", root / "key.pem"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                  serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(cert_path, key_path)
            sni = []
            server_ctx.set_servername_callback(lambda _sock, host, _ctx: sni.append(host))

            async def handler(reader, writer):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_ctx)
            port = server.sockets[0].getsockname()[1]
            client_ctx = ssl.create_default_context(cafile=str(cert_path))
            resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                               ("127.0.0.1", port))])
            try:
                transport = ScopedHTTPTransport(ScopeGuard(Scope()), lambda: httpx.AsyncHTTPTransport(
                    verify=client_ctx, trust_env=False))
                with patch.object(asyncio.get_running_loop(), "getaddrinfo", resolver):
                    async with httpx.AsyncClient(transport=transport) as client:
                        response = await client.get(f"https://audit.test:{port}/")
                self.assertEqual(response.text, "ok")
                self.assertEqual(sni, ["audit.test"])
            finally:
                server.close()
                await server.wait_closed()

    async def test_actual_connection_uses_checked_ip_and_original_host(self) -> None:
        hits: list[bytes] = []
        denied_hits: list[bool] = []

        async def allowed(reader, writer):
            hits.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async def denied(reader, writer):
            denied_hits.append(True)
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(allowed, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        blocked = await asyncio.start_server(denied, "127.0.0.2", port)
        resolve = socket.getaddrinfo
        lookups = []

        def rebinding(host, service, *args, **kwargs):
            name = host.decode() if isinstance(host, bytes) else host
            if name == "audit.test":
                lookups.append(name)
                return resolve("127.0.0.1" if len(lookups) == 1 else "127.0.0.2",
                               service, *args, **kwargs)
            return resolve(host, service, *args, **kwargs)

        try:
            with patch("socket.getaddrinfo", side_effect=rebinding), patch.dict(
                "os.environ", {"HTTP_PROXY": "http://127.0.0.2:1", "NO_PROXY": ""},
            ):
                result = await http_probe(f"http://audit.test:{port}/", guard=ScopeGuard(
                    Scope(allowed_domains=["audit.test"], denied=["127.0.0.2"])))
            self.assertTrue(result.ok, result.error)
            self.assertEqual(len(lookups), 1)
            self.assertEqual(denied_hits, [])
            self.assertIn(f"Host: audit.test:{port}".encode(), hits[0])
            self.assertEqual(result.parsed["final_url"], f"http://audit.test:{port}/")
        finally:
            server.close()
            blocked.close()
            await server.wait_closed()
            await blocked.wait_closed()

    async def test_sni_and_pools_preserve_logical_origin(self) -> None:
        seen = []
        transports = []

        def factory():
            def record(req):
                seen.append((req.url.host, req.headers["host"], req.extensions["sni_hostname"]))
                return httpx.Response(200, content=b"ok")
            transport = httpx.MockTransport(record)
            transports.append(transport)
            return transport

        resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                           ("127.0.0.1", 443))])
        transport = ScopedHTTPTransport(ScopeGuard(Scope()), factory)
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", resolver):
            async with httpx.AsyncClient(transport=transport) as client:
                await client.get("https://a.test/")
                await client.get("https://b.test/")
        self.assertEqual(seen, [("127.0.0.1", "a.test", "a.test"),
                                ("127.0.0.1", "b.test", "b.test")])
        self.assertEqual(len(transports), 2)

    async def test_legacy_denied_dns_address_does_not_prevent_transport(self) -> None:
        resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                           ("127.0.0.1", 80)),
                                          (socket.AF_INET6, socket.SOCK_STREAM, 6, "",
                                           ("::1", 80, 0, 0))])
        seen = []
        def respond(request):
            seen.append(request.url.host)
            return httpx.Response(200, text="local fixture")
        transport = ScopedHTTPTransport(ScopeGuard(Scope(denied=["::1"])),
                                        lambda: httpx.MockTransport(respond))
        try:
            with patch.object(asyncio.get_running_loop(), "getaddrinfo", resolver):
                async with httpx.AsyncClient(transport=transport) as client:
                    response = await client.get("http://audit.test/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(seen, ["127.0.0.1"])
        finally:
            await transport.aclose()

    async def test_ipv4_mapped_ipv6_is_not_filtered_by_legacy_denial(self) -> None:
        seen = []
        def respond(request):
            seen.append(request.url.host)
            return httpx.Response(200)
        transport = ScopedHTTPTransport(ScopeGuard(Scope(denied=["127.0.0.0/8"])),
                                        lambda: httpx.MockTransport(respond))
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("http://[::ffff:127.0.0.1]/")
        self.assertEqual(response.status_code, 200)
        # httpx/不同 Python 版本可能使用十六进制或混合表示，比较地址语义。
        self.assertEqual([ipaddress.ip_address(host) for host in seen],
                         [ipaddress.ip_address("::ffff:127.0.0.1")])
