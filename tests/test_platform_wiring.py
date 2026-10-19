"""平台接线测试：主 Agent 调度期开容器/完成回收/复盘事实 + 装配完成态过滤。

平台适配器本体在 test_platform_adapter（httpx.MockTransport 全真路径）；
这里测的是「谁在什么时候调它」：调度环按题开实例且名额未就绪不派发、
每轮收口按完成态回收、复盘视图携带平台事实、CTF 装配按平台完成态跳题。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.agents.sub.base import SubAgentContext
from spiritscout.blackboard import Blackboard, ConvergenceEngine
from spiritscout.bus.inprocess import InProcessBus
from spiritscout.competition import CompetitionRules, ContestType
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType, RunMode, Scope, TaskStatus
from spiritscout.modes.assembler import ModeAssembler
from spiritscout.policy.engine import PolicyEngine
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.registry import ToolRegistry


class StubPlatform:
    """平台适配器的替身：只保真被调用面的语义（供给/回收/完成态）。"""

    def __init__(self, provision: dict[str, dict[str, Any] | None] | None = None, *,
                 completed: set[str] | None = None,
                 signal: bool = True, raise_release: bool = False,
                 all_done: bool = False) -> None:
        self.provision = provision or {}
        self.completed = completed or set()
        self.signal = signal
        self.raise_release = raise_release
        self.all_done = all_done
        self.ensure_calls: list[str] = []
        self.release_calls: list[set[str]] = []
        self.facts: list[dict[str, Any]] = [{"action": "list_refreshed", "n": i}
                                            for i in range(10)]

    def owns(self, cid: str) -> bool:
        return True

    def has_completion_signal(self) -> bool:
        return self.signal

    def all_completed(self) -> bool:
        return bool(self.all_done) if self.signal else False

    def is_completed(self, cid: str) -> bool:
        return cid in self.completed

    async def ensure_instance(self, challenge: dict[str, Any]) -> dict[str, Any] | None:
        cid = str(challenge.get("id") or "")
        self.ensure_calls.append(cid)
        return self.provision.get(cid)

    async def release_completed(self,
                                accepted_ids: set[str] | None = None) -> list[str]:
        self.release_calls.append(set(accepted_ids or set()))
        if self.raise_release:
            raise RuntimeError("平台抖动")
        return sorted(self.completed)

    def summary(self) -> dict[str, Any]:
        return {"platform": "stub", "active_instances": 1}


class _BusStub:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)


class _ConvergenceStub:
    def aggregate_round_view(self, round_no: int) -> dict[str, Any]:
        return {"round": round_no}


class MasterPlatformHookTests(unittest.IsolatedAsyncioTestCase):
    def _master(self, extra: dict[str, Any], *,
                guard: ScopeGuard | None = None) -> MasterAgent:
        master = object.__new__(MasterAgent)
        master.agent_id = "master-test"
        master.ctx = SimpleNamespace(
            pause_gate=None, enable_llm_planning=False,
            ring=None, blackboard=None, audit=NullAuditLogger(),
            tactic_map={"CTF-WEB": (AgentType.CTF_WEB, "ctf_solve")},
            agents={AgentType.CTF_WEB: SimpleNamespace()},
            policy=SimpleNamespace(guard=guard or ScopeGuard(Scope())),
            extra=extra)
        return master

    @staticmethod
    def _plan_override(state: dict[str, Any]) -> dict[str, Any]:
        return {"analysis": "测试计划", "dag_nodes": [
            {"tactic": "CTF-WEB", "title": "c1 方向",
             "meta": {"challenge": {"id": "c1", "title": "题一"}}},
            {"tactic": "CTF-WEB", "title": "c2 方向",
             "meta": {"challenge": {"id": "c2", "title": "题二"}}},
            {"tactic": "CTF-WEB", "title": "c3 方向",
             "meta": {"challenge": {"id": "c3", "title": "题三"}}},
        ]}

    async def test_meta_plan_provisions_instances_and_holds_back_full_slots(self) -> None:
        platform = StubPlatform({
            "c1": {"id": "c1", "title": "题一", "url": "http://c1.box:8080",
                   "container_addr": "c1.box:8080"},
            "c2": {"id": "c2", "title": "题二", "url": "http://c2.box:8080",
                   "container_addr": "c2.box:8080"},
            "c3": None,   # 名额已满 / 平台暂未接受
        })
        guard = ScopeGuard(Scope(local_analysis=True))
        master = self._master({"platform": platform}, guard=guard)
        master.ctx.plan_override = self._plan_override
        out = await master._meta_plan(
            {"dag": TaskDAG().to_dict(), "round_no": 0, "target": "ctf://challenges"})

        self.assertEqual(platform.ensure_calls, ["c1", "c2", "c3"])
        tasks = out["tasks_dispatched"]
        self.assertEqual(len(tasks), 2)                     # c3 本轮不派发
        by_cid = {t["task"]["payload"]["challenge"]["id"]: t["task"] for t in tasks}
        self.assertEqual(set(by_cid), {"c1", "c2"})
        self.assertEqual(by_cid["c1"]["target"], "http://c1.box:8080")
        self.assertEqual(by_cid["c1"]["payload"]["challenge"]["url"],
                         "http://c1.box:8080")
        dag = TaskDAG.from_dict(out["dag"])
        nodes = {n.title: n for n in dag.all_nodes()}
        self.assertEqual(nodes["c1 方向"].status, TaskStatus.RUNNING)
        self.assertEqual(nodes["c3 方向"].status, TaskStatus.PENDING)  # 保持待发等下轮
        # 平台下发的容器地址并入批准集：运行期不再被标「范围之外」
        self.assertTrue(guard.is_allowed("http://c1.box:8080"))
        self.assertFalse(guard.is_allowed("http://evil.example.com:1"))

    async def test_flag_hard_stop_respects_platform_completion(self) -> None:
        dag = TaskDAG()
        view = {"flags": {"extracted": 1, "submitted": 1,
                          "accepted": 1, "rejected": 0}}
        # 无平台：维持原语义（Flag 已收 + 节点全完成 = 确定性收手）
        self.assertTrue(self._master({})._flag_hard_stop(view, dag))
        # 平台有按题完成态但视野内还有未完成题：不早退，继续重攻剩余题
        platform = StubPlatform(completed={"c1"}, all_done=False)
        self.assertFalse(self._master({"platform": platform})
                         ._flag_hard_stop(view, dag))
        # 平台通报全部完赛：允许走收手快路
        platform2 = StubPlatform(completed={"c1", "c2"}, all_done=True)
        self.assertTrue(self._master({"platform": platform2})
                        ._flag_hard_stop(view, dag))
        # 平台无按题完成态：不参与收手裁定
        platform3 = StubPlatform(signal=False)
        self.assertTrue(self._master({"platform": platform3})
                        ._flag_hard_stop(view, dag))
        # 尚未收到任何 Flag：照常继续（收手快路前提不成立）
        self.assertFalse(self._master({})._flag_hard_stop(
            {"flags": {"accepted": 0, "extracted": 1}}, dag))

    async def test_platform_release_reports_accepted_and_survives_failure(self) -> None:
        flag_manager = SimpleNamespace(accepted_flags=lambda: [
            SimpleNamespace(challenge_id="c1")])
        platform = StubPlatform(completed={"c1"})
        master = self._master({"platform": platform, "flag_manager": flag_manager})
        await master._platform_release()
        self.assertEqual(platform.release_calls, [{"c1"}])

        boom = StubPlatform(completed={"c1"}, raise_release=True)
        master2 = self._master({"platform": boom, "flag_manager": flag_manager})
        await master2._platform_release()          # 回收失败只留痕，不抛出
        self.assertEqual(boom.release_calls, [{"c1"}])

    async def test_aggregate_view_carries_platform_facts(self) -> None:
        platform = StubPlatform()
        master = self._master({"platform": platform,
                               "platform_facts": platform.facts})
        master.ctx.bus = _BusStub()
        master.ctx.convergence = _ConvergenceStub()
        out = await master._aggregate({"round_no": 3})
        view = out["round_views"][0]
        self.assertEqual(view["platform"]["summary"]["platform"], "stub")
        self.assertEqual(len(view["platform"]["recent"]), 8)  # 末 8 条，防爆 token
        self.assertEqual(master.ctx.bus.events[-1].payload["platform"]["summary"]
                         ["active_instances"], 1)


class CflAssemblyPlatformTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bb = Blackboard(root / "bb.db")
        self.addAsyncCleanup(self.bb.aclose)
        self.bus = InProcessBus(rate_limit_per_second=0)
        await self.bus.start()
        self.addAsyncCleanup(self.bus.stop)
        self.ctx = SubAgentContext(
            self.bus, self.bb, ConvergenceEngine(self.bb), SimpleNamespace(),
            ToolRegistry(LocalExecutor()),
            PolicyEngine(RunMode.CTF, ScopeGuard(Scope(local_analysis=True))))

    def _assembly(self, platform: StubPlatform | None) -> Any:
        return ModeAssembler(self.ctx, enable_llm_planning=False).assemble(
            RunMode.CTF,
            challenges=[{"id": "c1", "title": "题一", "url": "http://a/1"},
                        {"id": "c2", "title": "题二", "url": "http://a/2"}],
            flag_manager=None, platform=platform)

    async def test_ctf_assembly_skips_platform_completed_challenges(self) -> None:
        platform = StubPlatform(completed={"c1"})
        assembly = self._assembly(platform)
        plan = assembly.plan_provider({"round_no": 1, "dag": {}})
        cids = {n["meta"]["challenge"]["id"] for n in plan["dag_nodes"]}
        self.assertEqual(cids, {"c2"})   # 平台通报完成的题不再重攻

    async def test_ctf_assembly_without_signal_keeps_all_challenges(self) -> None:
        platform = StubPlatform(signal=False)
        assembly = self._assembly(platform)
        plan = assembly.plan_provider({"round_no": 1, "dag": {}})
        cids = {n["meta"]["challenge"]["id"] for n in plan["dag_nodes"]}
        self.assertEqual(cids, {"c1", "c2"})  # 无完成态信号：照常全量重攻


class CompetitionAssemblyPlatformTests(unittest.IsolatedAsyncioTestCase):
    """渗透赛（Jeopardy）平台接线：题源/完成态过滤与 CTF 同源。"""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bb = Blackboard(root / "bb.db")
        self.addAsyncCleanup(self.bb.aclose)
        self.bus = InProcessBus(rate_limit_per_second=0)
        await self.bus.start()
        self.addAsyncCleanup(self.bus.stop)
        self.ctx = SubAgentContext(
            self.bus, self.bb, ConvergenceEngine(self.bb), SimpleNamespace(),
            ToolRegistry(LocalExecutor()),
            PolicyEngine(RunMode.CTF, ScopeGuard(Scope(local_analysis=True))))

    def _assembly(self, platform: StubPlatform | None) -> Any:
        return ModeAssembler(self.ctx, enable_llm_planning=False).assemble(
            RunMode.COMPETITION,
            challenges=[{"id": "c1", "title": "题一", "url": "http://a/1"},
                        {"id": "c2", "title": "题二", "url": "http://a/2"}],
            rules=CompetitionRules(contest_type=ContestType.JEOPARDY),
            flag_manager=None, competition_engine=None, platform=platform)

    async def test_jeopardy_assembly_honours_platform_completion(self) -> None:
        plan = self._assembly(StubPlatform(completed={"c1"})).plan_provider(
            {"round_no": 1, "dag": {}})
        cids = {n["meta"]["challenge"]["id"] for n in plan["dag_nodes"]}
        self.assertEqual(cids, {"c2"})   # 平台通报完成的题不再重攻

    async def test_jeopardy_assembly_without_platform_keeps_all(self) -> None:
        plan = self._assembly(None).plan_provider({"round_no": 1, "dag": {}})
        cids = {n["meta"]["challenge"]["id"] for n in plan["dag_nodes"]}
        self.assertEqual(cids, {"c1", "c2"})


if __name__ == "__main__":
    unittest.main()
