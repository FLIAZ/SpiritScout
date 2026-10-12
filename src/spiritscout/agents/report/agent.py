"""报告 Agent（设计书 4.6）：六段式 SRC 报告生成。

- 仅收录池（ACCEPTED：中危及以上且 PoC 实测通过）进入正文漏洞详情（硬约束 C5）；
- 低危与被淘汰候选仅在附录计数呈现（G4：仅记录不输出）；
- AI 参与声明与授权信息固定入「合规声明」段（G6）；
- 报告落盘前统一脱敏（手机号/身份证/邮箱，G8 凭据卫生延伸）。
报告 Agent 对黑板只读（写权限为空集，4.8.3）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from spiritscout.blackboard.store import Blackboard
from spiritscout.core.models import (
    Authorization,
    BlackboardLayer,
    Vulnerability,
    VulnStatus,
)

_TEMPLATE_DIR = Path(__file__).parent / "templates"

_MASK_RULES = [
    (re.compile(r"(1[3-9]\d)(\d{4})(\d{4})"), r"\1****\3"),           # 手机号
    (re.compile(r"(\d{6})\d{8}(\d{3}[\dXx])"), r"\1********\2"),       # 身份证
    (re.compile(r"([\w.+-]{2})[\w.+-]*(@[\w-]+(?:\.[\w-]+)+)"), r"\1***\2"),  # 邮箱
]

FIX_SUGGESTIONS = {
    "sqli": "使用参数化查询/预编译语句；最小权限数据库账户；关闭生产环境错误回显。",
    "xss": "输出编码（按上下文 HTML/JS/URL）；CSP 策略；输入侧白名单校验。",
    "ssrf": "URL 白名单与协议限制；禁访问内网保留地址段；出向流量隔离。",
    "info_disclosure": "移除生产环境敏感路径与备份文件；Web 服务器禁访问 .git/.svn 等元数据目录。",
}


class ReportAgent:
    """SRC 模式报告生成器（Markdown 基线，适配决策 A6）。"""

    def __init__(self, blackboard: Blackboard):
        self.bb = blackboard
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=select_autoescape(disabled_extensions=("md", "j2")),
            trim_blocks=True, lstrip_blocks=True,
        )

    # ------------------------------------------------------------ 数据装配

    def collect(self, current_task_ids: set[str] | None = None) -> dict[str, Any]:
        assets = [r["data"] for r in self.bb.query(BlackboardLayer.ASSET, limit=10000)]
        vulns = [Vulnerability.model_validate(r["data"])
                 for r in self.bb.query(BlackboardLayer.VULN, limit=10000)]
        pool = [v for v in vulns if v.status == VulnStatus.ACCEPTED]
        pool.sort(key=lambda v: (-v.severity.rank, v.vuln_type))
        rejected_stats: dict[str, int] = {}
        for v in vulns:
            if v.status != VulnStatus.ACCEPTED:
                rejected_stats[v.status.value] = rejected_stats.get(v.status.value, 0) + 1
        tasks = [r["data"] for r in self.bb.query(BlackboardLayer.TASK_STATE, limit=10000)]
        if current_task_ids:
            tasks = [data for data in tasks
                     if str((data.get("task") or {}).get("task_id"))
                     in current_task_ids]
        clues = [r["data"] for r in self.bb.query(BlackboardLayer.RECON_CLUE, limit=10000)]
        return {
            "assets": assets,
            "pool": pool,
            "rejected_stats": rejected_stats,
            "task_count": len(tasks),
            "clue_count": len(clues),
            "severity_counts": self._severity_counts(pool),
        }

    @staticmethod
    def _severity_counts(pool: list[Vulnerability]) -> dict[str, int]:
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for v in pool:
            if v.severity.value in counts:
                counts[v.severity.value] += 1
        return counts

    # ------------------------------------------------------------ 渲染

    def generate(self, authorization: Authorization, target: str,
                 out_path: str | Path, run_summary: dict[str, Any] | None = None) -> Path:
        current_task_ids = {
            str(task_id)
            for node in (run_summary or {}).get("dag_nodes", [])
            for task_id in node.get("task_ids", [])
        }
        data = self.collect(current_task_ids)
        template = self._env.get_template("src_report.md.j2")
        rendered = template.render(
            target=target,
            auth=authorization,
            generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            run_summary=run_summary or {},
            fix_suggestions=FIX_SUGGESTIONS,
            **data,
        )
        rendered = self._mask(rendered)
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return path

    @staticmethod
    def _mask(text: str) -> str:
        for pattern, repl in _MASK_RULES:
            text = pattern.sub(repl, text)
        return text

    # ------------------------------------------------------------ CTF WriteUp

    def generate_writeup(
        self,
        challenges: list[dict[str, Any]],
        flags: list,
        out_path: str | Path,
        context: dict[str, Any] | None = None,
    ) -> Path:
        """CTF/渗透赛结构化 WriteUp（5.2.4：题目、考点、解题思路、Flag、复现步骤）。"""
        from spiritscout.core.models import FlagCandidate

        flag_models = [f if isinstance(f, FlagCandidate)
                       else FlagCandidate.model_validate(f) for f in flags]
        by_challenge: dict[str, list[FlagCandidate]] = {}
        for f in flag_models:
            by_challenge.setdefault(f.challenge_id, []).append(f)
        # 任务状态层摘要（解题过程记录）
        task_states = [r["data"] for r in
                       self.bb.query(BlackboardLayer.TASK_STATE, limit=10000)
                       if "dag_snapshot" not in r["data"]]
        current_task_ids = {
            str(task_id) for task_id in (context or {}).get("current_task_ids", [])
        }
        if current_task_ids:
            task_states = [
                data for data in task_states
                if str((data.get("task") or {}).get("task_id"))
                in current_task_ids
            ]
        template = self._env.get_template("ctf_writeup.md.j2")
        rendered = template.render(
            generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            challenges=challenges, flags_by_challenge=by_challenge,
            task_states=task_states, context=context or {},
        )
        rendered = self._mask(rendered)
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return path
