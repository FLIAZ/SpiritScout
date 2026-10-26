"""支持运行时增删节点、依赖检查和失败重排的任务 DAG。"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.ids import new_id
from spiritscout.core.models import DAGNode, TaskStatus


@dataclass
class PlanUpsertResult:
    """落图回执：created=真正新建的节点；skipped=被跳过的规划节点与原因。

    跳过/拒绝必须携带原因回执给规划方——静默吞掉会让模型误以为节点
    「在途」而空等（2026-10-16 会话复盘：40+ 轮调度空转根因之一）。
    """
    created: list[DAGNode] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


def _challenge_key(meta: dict | None) -> str:
    """赛题归属键：meta.challenge.id；通用节点（无赛题）为空串。

    去重与依赖绑定按「战术×赛题」判同：多道题并行时同战术节点
    互不吞并，各题的路线只在自己的题内闭环。
    """
    ch = (meta or {}).get("challenge") or {}
    if not isinstance(ch, dict):
        return ""
    return str(ch.get("id") or "")


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

    def upsert_from_plan(self, plan_nodes: list[dict], round_no: int) -> PlanUpsertResult:
        """在副本中校验整批变更，成功后才原子应用节点和重绑定的依赖。"""
        # target 是单目标字段，格式错误应回传规划器，不能当主机发往网络。
        for spec in plan_nodes:
            meta = spec.get("meta") if isinstance(spec.get("meta"), dict) else {}
            for target in (spec.get("target"), meta.get("target")):
                if target is None or target == "":
                    continue
                valid = isinstance(target, str) and not any(c.isspace() for c in target.strip())
                if valid:
                    try:
                        parsed = urlsplit(target if "://" in target else "//" + target)
                        host = parsed.hostname or ""
                        valid = not any(c in host for c in ",;，；、")
                    except ValueError:
                        valid = False
                if not valid:
                    raise SpiritScoutError(
                        f"规划错误：节点「{spec.get('tactic', '')}」的 target 必须是单个目标，"
                        "不能包含地址列表或未编码空白；多个目标请拆成独立节点")
        staged = TaskDAG.from_dict(self.to_dict())
        created, skipped = staged._upsert_from_plan(plan_nodes, round_no)
        for nid, node in staged._nodes.items():
            if nid in self._nodes:
                self._nodes[nid].depends_on = list(node.depends_on)
            else:
                self._nodes[nid] = node
        return PlanUpsertResult(
            created=[self._nodes[n.node_id] for n in created], skipped=skipped)

    def _upsert_from_plan(
        self, plan_nodes: list[dict], round_no: int,
    ) -> tuple[list[DAGNode], list[dict[str, str]]]:
        """从元规划输出合并节点（动态重规划：同「战术×赛题」节点不重复建；
        无赛题的通用节点维持原战术维度语义）。

        两遍构建：先建全部新节点、后解析依赖——计划内节点可以互相引用，
        节点在计划中的先后顺序不再决定依赖能否解析（子节点先于父节点
        出现也不丢边）。未知依赖抛 SpiritScoutError（规划错误明确返回，
        不静默丢边）；绑定规则见 _pick_dependency；建完校验环/自依赖，
        校验失败整体回滚本次新建。同一战术被跳过的节点带原因进回执。
        """
        created: list[DAGNode] = []
        skipped: list[dict[str, str]] = []
        pairs: list[tuple[dict, DAGNode]] = []
        # 第一遍：建节点（依赖先置空）；去重键为「战术×赛题」——
        # 多道题并行时同战术节点各题独立落图，不互相静默吞并。
        for spec in plan_nodes:
            tactic = str(spec.get("tactic", "CUSTOM"))
            ckey = _challenge_key(spec.get("meta"))
            existing = [n for n in self.find_by_tactic(tactic)
                        if _challenge_key(n.meta) == ckey]
            # 历史失败不应让已重建成功的战术每轮再次生成。
            latest = max(enumerate(existing), key=lambda p: (p[1].round_created, p[0]))[1] \
                if existing else None
            if latest and latest.status not in {
                TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.PRUNED,
            }:
                skipped.append({
                    "tactic": tactic,
                    "title": str(spec.get("title", tactic))[:120],
                    "reason": (f"同战术节点已存在（{latest.tactic}"
                               + (f"·赛题 {ckey}" if ckey else "")
                               + f"，状态 {latest.status.value}）"),
                })
                continue
            meta = dict(spec["meta"]) if isinstance(spec.get("meta"), dict) else {}
            if isinstance(spec.get("continue_from"), dict):
                meta["continue_from"] = dict(spec["continue_from"])
            # 规划节点可指定本节点的测试目标（多域 SRC 按域拆节点）；
            # 留空则派发时沿用会话目标。
            target = str(spec.get("target") or "").strip()
            if target:
                meta.setdefault("target", target)
            node = self.add_node(
                tactic, str(spec.get("title", tactic)),
                technique=spec.get("technique"), depends_on=[],
                round_created=round_no, meta=meta,
            )
            created.append(node)
            pairs.append((spec, node))
        if not pairs:
            return created, skipped
        try:
            # 第二遍：解析依赖（新建与既有节点都在候选内）
            for spec, node in pairs:
                dep_ids: list[str] = []
                for dt in (spec.get("depends_on") or []):
                    if str(dt) == node.tactic:
                        raise SpiritScoutError(f"规划错误：节点「{node.tactic}」不能依赖自身")
                    target = self._pick_dependency(str(dt), exclude=node)
                    if target is None:
                        raise SpiritScoutError(
                            f"规划错误：节点「{node.tactic}」依赖未知战术 "
                            f"{dt}（depends_on 必须指向计划内或已存在的战术）")
                    dep_ids.append(target.node_id)
                node.depends_on = dep_ids
            replacements = {(node.tactic, _challenge_key(node.meta)): node
                            for node in created}
            for node in self._nodes.values():
                if node.status not in {TaskStatus.PENDING, TaskStatus.DISPATCHED}:
                    continue
                for index, dep_id in enumerate(node.depends_on):
                    dep = self._nodes.get(dep_id)
                    replacement = replacements.get(
                        (dep.tactic, _challenge_key(dep.meta))) if dep else None
                    if (dep and replacement and dep.status in {
                        TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.PRUNED,
                    }):
                        node.depends_on[index] = replacement.node_id
            self._ensure_acyclic(self.all_nodes())
        except SpiritScoutError:
            for _, node in pairs:                # 整体回滚：坏计划不落半个节点
                self._nodes.pop(node.node_id, None)
            raise
        return created, skipped

    def _pick_dependency(self, tactic: str, exclude: DAGNode) -> DAGNode | None:
        """依赖绑定规则：同战术候选取最新创建且未失败的——失败重建后
        下游绑到重建节点，不被旧失败节点卡死；全部失败仍绑最新（下游
        等待重试完成后解锁，是否重建由复盘层决策）。多赛题并行时优先
        同赛题候选：A 题的后续阶段不绑到 B 题的同名战术节点上（无同题
        候选时才回退全量候选）。"""
        cands = [n for n in self._nodes.values()
                 if n.tactic == tactic and n.node_id != exclude.node_id]
        if not cands:
            return None
        ckey = _challenge_key(exclude.meta)
        same = [n for n in cands if _challenge_key(n.meta) == ckey]
        cands = same or cands
        pool = [n for n in cands if n.status != TaskStatus.FAILED] or cands
        best = pool[0]
        for n in pool[1:]:
            if n.round_created >= best.round_created:
                best = n
        return best

    def _ensure_acyclic(self, new_nodes: list[DAGNode]) -> None:
        """环/自依赖校验（DFS）：新边引入的环会让节点永远无法就绪。"""
        state: dict[str, int] = {}               # 0=未访 1=在栈 2=完成
        stack: list[str] = []

        def visit(node_id: str) -> None:
            mark = state.get(node_id, 0)
            if mark == 1:
                cycle = stack[stack.index(node_id):] + [node_id]
                names = " → ".join(
                    self._nodes[nid].tactic for nid in cycle if nid in self._nodes)
                raise SpiritScoutError(f"规划错误：依赖成环（{names}）")
            if mark == 2:
                return
            state[node_id] = 1
            stack.append(node_id)
            for dep in self._nodes[node_id].depends_on:
                if dep in self._nodes:
                    visit(dep)
            stack.pop()
            state[node_id] = 2

        for node in new_nodes:
            visit(node.node_id)

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
