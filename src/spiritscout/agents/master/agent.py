"""主 Agent 入口：组装规划、协调、交互职责及 LangGraph 生命周期。"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from langgraph.graph import END, StateGraph

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
)

__all__ = ["MasterAgent", "MasterContext", "MasterState", "TACTIC_AGENT_MAP"]


class MasterAgent(MasterCoordination, MasterInteraction, MasterPlanning):
    """主 Agent（元规划决策层）。"""

    # 常规规划使用最高档；竞赛首轮只装配顺序待发队列，走低强度以尽快开工。
    # 子 Agent 的动态裁定见 _assign_dispatch_plan。模型未声明该档位时按端点
    # 默认档执行（网关门禁忽略未声明值，不猜不代填）
    _MASTER_EFFORT = "max"

    def __init__(self, ctx: MasterContext):
        self.ctx = ctx
        self.agent_id = new_id("master")
        self._graph = self._build_graph()

    # ------------------------------------------------------------ 图构建

    def _build_graph(self):
        graph = StateGraph(MasterState)
        graph.add_node("meta_plan", self._checkpoint_node("meta_plan", self._meta_plan))
        graph.add_node("dispatch", self._checkpoint_node("dispatch", self._dispatch))
        graph.add_node("aggregate", self._checkpoint_node("aggregate", self._aggregate))
        graph.add_node("review", self._checkpoint_node("review", self._review))
        graph.set_entry_point("meta_plan")
        graph.add_edge("meta_plan", "dispatch")
        graph.add_edge("dispatch", "aggregate")
        graph.add_edge("aggregate", "review")
        # 单段以 review 收束：继续/收手由外层分段循环携带状态决策——
        # 框架递归深度只覆盖一段（4 个节点），不构成长期迭代的上限
        graph.add_edge("review", END)
        return graph.compile()

    def _checkpoint_node(self, name: str, handler: Any) -> Any:
        async def execute(state: MasterState) -> dict[str, Any]:
            journal = getattr(self.ctx, "journal", None)
            if journal is not None:
                journal.checkpoint("master", "master", f"before_{name}", dict(state))
            update = await handler(state)
            if journal is not None:
                merged = {**state, **update}
                # 与图中的 operator.add reducer 保持一致，避免遗漏本轮任务/视图。
                for key in ("tasks_dispatched", "round_views"):
                    merged[key] = list(state.get(key, [])) + list(update.get(key, []))
                journal.checkpoint("master", "master", f"after_{name}", merged)
            return update

        return execute

    async def run(self, authorization: Authorization, target: str) -> dict[str, Any]:
        """启动多轮闭环迭代，直至模型复盘判定收敛或用户停止。

        分段执行：每段一轮（规划→派发→汇总→复盘），段间状态由外层循环
        携带——单段步数固定，框架递归上限不约束业务收敛；是否继续只由
        复盘决策与用户停止决定，不设固定回合顶。
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
        state: MasterState = {
            "target": target,
            "round_no": 0,
            "dag": TaskDAG().to_dict(),
            "tasks_dispatched": [],
            "round_views": [],
            "continue_iteration": True,
            "stop_reason": "",
            "plan_source": "",
        }
        # 用户直达通道：与派发/督导隔离，整场运行期间并行消费用户消息
        # （开工时间戳供对话通道回答「跑了多久」类问题，不依赖主循环）
        self.ctx.extra.setdefault("engagement_started_at", time.time())
        chat_task = (asyncio.create_task(self._user_chat_watch())
                     if self.ctx.enable_llm_planning else None)
        total_dispatched = 0
        try:
            while True:
                final = await self._graph.ainvoke(state)
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
