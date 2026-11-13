"""主 Agent 入口：组装规划、协调、交互职责及分段执行生命周期。"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any
from urllib.parse import urlsplit

from spiritscout.agents.master.contracts import (
    TACTIC_AGENT_MAP,
    MasterContext,
    MasterState,
)
from spiritscout.agents.master.coordination import MasterCoordination
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.agents.master.interaction import MasterInteraction
from spiritscout.agents.master.planning import MasterPlanning
from spiritscout.core.ids import new_id
from spiritscout.core.models import (
    AgentType,
    Authorization,
    BlackboardLayer,
    RunMode,
)

__all__ = ["MasterAgent", "MasterContext", "MasterState", "TACTIC_AGENT_MAP"]


class MasterAgent(MasterCoordination, MasterInteraction, MasterPlanning):
    """主 Agent（元规划决策层）。"""

    # 常规规划使用最高档；竞赛首轮只装配顺序待发队列，走低强度以尽快开工。
    # 子 Agent 的动态裁定见 _assign_dispatch_plan。模型未声明该档位时按端点
    # 默认档执行（网关门禁忽略未声明值，不猜不代填）
    _MASTER_EFFORT = "max"

    @property
    def _master_effort(self) -> str:
        """当前部署的主 Agent 档位：Ollama 轻量模型只声明 low/high，
        max 整档会被网关忽略。实测（round1/4）该模型主 Agent 思考偶发
        数千 token 失控（一次吞掉 4 分钟），而 low 档 93 token 即可产出
        可用派单——轻量 × CTF 降为 low，把秒表留给解题环的思考。
        仅限 CTF 模式（轻量部署的专属补偿），SRC 会话不受影响。"""
        if (self.ctx.extra.get("lightweight_local")
                and getattr(self.ctx.policy, "mode", None) in (RunMode.CTF, RunMode.AWDP)):
            return "low"
        return self._MASTER_EFFORT

    def __init__(self, ctx: MasterContext):
        self.ctx = ctx
        self.agent_id = new_id("master")

    async def _start_wildcard_discovery(self, authorization: Authorization,
                                        target: str) -> dict[str, Any] | None:
        """显式通配符 SRC 首轮立即并行做只读资产发现。

        这是用户授权形态直接表达的信息收集需求，不依赖元规划模型是否恰好
        生成 scan_control。这里只许可 asset_discover；Nuclei 等验证工具仍须
        后续基于具体漏洞假设由模型单独派遣。
        """
        control = (self.ctx.extra or {}).get("scan_controller")
        wildcards = [
            domain.strip().lower().rstrip(".")
            for domain in authorization.scope.allowed_domains
            if isinstance(domain, str) and domain.strip().startswith("*.")
        ]
        if (authorization.mode != RunMode.SRC or control is None or not wildcards
                or control.forbidden or control.status != "idle"):
            return None
        host = (urlsplit(target).hostname or "").lower().rstrip(".")
        selected = next(
            (pattern for pattern in wildcards if host == pattern[2:]), wildcards[0])
        return await control.apply({
            "action": "dispatch",
            "revision": control.revision,
            "reason": "用户以通配符明确授权子域范围，首轮优先并行收集范围内资产",
            "target": selected,
            "goal": "发现范围内子域、存活 Web 入口与基础指纹，供后续按资产价值滚动派工",
            "tools": ["asset_discover"],
            "constraints": (
                "仅做只读 DNS、证书透明度与 HTTP 基线；结果必须同时位于本次通配符授权"
                "和派遣范围内；不运行 Nuclei、Metasploit、爆破或写入动作"),
        }, actor=self.agent_id)

    # ------------------------------------------------------------ 分段执行

    async def _run_segment(self, state: MasterState) -> MasterState:
        """单段顺序执行：规划→派发→汇总→复盘。

        单段拓扑固定线性，继续/收手由外层分段循环携带状态决策——段内
        步数固定（4 个节点），不构成长期迭代的上限。列表键按累加语义
        合并（tasks_dispatched / round_views），检查点 journal 落在节点
        边界前后。
        """
        journal = getattr(self.ctx, "journal", None)
        for name in ("meta_plan", "dispatch", "aggregate", "review"):
            if journal is not None:
                journal.checkpoint("master", "master", f"before_{name}", dict(state))
            update: dict[str, Any] = await getattr(self, f"_{name}")(state)
            appended = {key: list(state.get(key, [])) + list(update.get(key, []))
                        for key in ("tasks_dispatched", "round_views")}
            if journal is not None:
                journal.checkpoint("master", "master", f"after_{name}",
                                   {**state, **update, **appended})
            state = {**state, **update, **appended}
        return state

    async def run(self, authorization: Authorization, target: str) -> dict[str, Any]:
        """启动多轮闭环迭代，直至模型复盘判定收敛或用户停止。

        分段执行：每段一轮（规划→派发→汇总→复盘），段间状态由外层循环
        携带——单段步数固定；是否继续只由复盘决策与用户停止决定，不设
        固定回合顶。
        """
        assert self.ctx.audit is not None
        self.ctx.audit.log("decision.change", self.agent_id, {
            "action": "engagement_start", "target": target, "mode": authorization.mode.value})
        # 授权写入黑板目标与授权层
        self.ctx.blackboard.write(
            BlackboardLayer.TARGET_AUTH, authorization.auth_id,
            authorization.model_dump(mode="json"),
            agent_type=AgentType.MASTER, updated_by=self.agent_id,
            dedup_key=f"auth|{authorization.auth_id}",
        )
        await self._start_wildcard_discovery(authorization, target)
        state: MasterState = {
            "target": target,
            "round_no": 0,
            "dag": TaskDAG().to_dict(),
            "tasks_dispatched": [],
            "round_views": [],
            "continue_iteration": True,
            "stop_reason": "",
            "plan_source": "",
            "last_decision_event": "",
            "replan_requested": False,
        }
        # 用户直达通道：与派发/督导隔离，整场运行期间并行消费用户消息
        # （开工时间戳供对话通道回答「跑了多久」类问题，不依赖主循环）
        self.ctx.extra.setdefault("engagement_started_at", time.time())
        chat_task = (asyncio.create_task(self._user_chat_watch())
                     if self.ctx.enable_llm_planning else None)
        total_dispatched = 0
        try:
            while True:
                final = await self._run_segment(state)
                total_dispatched += len(final["tasks_dispatched"])
                if not final["continue_iteration"]:
                    break
                # 下一段携带全部累积态势；本轮派发记录不带入（路由摘要按轮取用）
                state = {**final, "tasks_dispatched": []}
        finally:
            if chat_task is not None:
                chat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await chat_task
        final_dag = TaskDAG.from_dict(final["dag"])
        summary = {
            "target": target,
            "rounds_executed": final["round_no"],
            "stop_reason": final["stop_reason"],
            "dag_stats": final_dag.stats(),
            "dag_nodes": self._dag_snapshot(final_dag),
            "round_views": final["round_views"],
            "tasks_dispatched": total_dispatched,
        }
        self.ctx.audit.log("decision.change", self.agent_id,
                           {"action": "engagement_end", **summary})
        return summary

    # ------------------------------------------------------------ 节点：元规划

    async def _gate_wait(self) -> None:
        """暂停门检查点：图节点边界挂起（在途节点跑完才停）。"""
        if self.ctx.pause_gate is not None:
            await self.ctx.pause_gate.wait()
