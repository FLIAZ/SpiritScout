"""DAG 任务图谱（设计书 4.1.1 / 6.2.4-1：动态、非刚性，支持边探索边补节点）。

Incalmo 反面教材（附录A §2.10）：刚性任务图会在早期侦察停滞（0 发现）。
本实现：节点可运行时增删、依赖满足即就绪、失败可回退重排（MazeRunner 对标）。
"""

from __future__ import annotations

from spiritscout.core.ids import new_id
from spiritscout.core.models import DAGNode, TaskStatus


class TaskDAG:
    def __init__(self) -> None:
        self._nodes: dict[str, DAGNode] = {}

    # ------------------------------------------------------------ 构建

    def add_node(
        self,
        tactic: str,
        title: str,
        *,
        technique: str | None = None,
        depends_on: list[str] | None = None,
        round_created: int = 0,
        meta: dict | None = None,
    ) -> DAGNode:
        node = DAGNode(
            node_id=new_id("dag"), tactic=tactic, title=title, technique=technique,
            depends_on=list(depends_on or []), round_created=round_created, meta=meta or {},
        )
        self._nodes[node.node_id] = node
        return node

    def upsert_from_plan(self, plan_nodes: list[dict], round_no: int) -> list[DAGNode]:
        """从元规划输出合并节点（动态重规划：已存在同战术节点不重复建）。"""
        created: list[DAGNode] = []
        for spec in plan_nodes:
            tactic = spec.get("tactic", "CUSTOM")
            existing = self.find_by_tactic(tactic)
            if existing and all(n.status != TaskStatus.FAILED for n in existing):
                continue
            dep_tactics = spec.get("depends_on", [])
            dep_ids = []
            for dt in dep_tactics:
                deps = self.find_by_tactic(dt)
                if deps:
                    dep_ids.append(deps[0].node_id)
            created.append(self.add_node(
                tactic, spec.get("title", tactic),
                technique=spec.get("technique"), depends_on=dep_ids,
                round_created=round_no, meta=spec.get("meta"),
            ))
        return created

    # ------------------------------------------------------------ 查询

    def find_by_tactic(self, tactic: str) -> list[DAGNode]:
        return [n for n in self._nodes.values() if n.tactic == tactic]

    def get(self, node_id: str) -> DAGNode | None:
        return self._nodes.get(node_id)

    def ready_nodes(self) -> list[DAGNode]:
        """依赖全部完成且自身待执行的节点（任务下发依据）。"""
        ready = []
        for node in self._nodes.values():
            if node.status != TaskStatus.PENDING:
                continue
            if all(
                (dep := self._nodes.get(d)) is not None and dep.status == TaskStatus.DONE
                for d in node.depends_on
            ):
                ready.append(node)
        return ready

    def all_nodes(self) -> list[DAGNode]:
        return list(self._nodes.values())

    def mark(self, node_id: str, status: TaskStatus) -> None:
        node = self._nodes[node_id]
        node.status = status

    def attach_task(self, node_id: str, task_id: str) -> None:
        self._nodes[node_id].task_ids.append(task_id)

    # ------------------------------------------------------------ 复盘

    def has_unfinished(self) -> bool:
        return any(
            n.status in (TaskStatus.PENDING, TaskStatus.RUNNING) for n in self._nodes.values()
        )

    def stats(self) -> dict:
        by_status: dict[str, int] = {}
        for n in self._nodes.values():
            by_status[n.status.value] = by_status.get(n.status.value, 0) + 1
        return {"nodes": len(self._nodes), "by_status": by_status}

    def to_dict(self) -> dict:
        return {nid: n.model_dump(mode="json") for nid, n in self._nodes.items()}

    @classmethod
    def from_dict(cls, data: dict) -> TaskDAG:
        dag = cls()
        for nid, nd in data.items():
            dag._nodes[nid] = DAGNode.model_validate(nd)
        return dag
