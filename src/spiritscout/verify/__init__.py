"""漏洞全链路核验（设计书 4.3.2，硬约束 C5）。

初筛 → 独立复现（≥2 种方式交叉）→ 误报排查 → SRC 合规判定 → PoC/EXP 自动生成与实测。
仅「中危及以上且实际可利用」进入收录池；低危与配置类仅记录不输出（G4）。
"""

from spiritscout.verify.cvss import cvss31_base_score, severity_from_score
from spiritscout.verify.pipeline import VerifyPipeline
from spiritscout.verify.rules import BaselineRules

__all__ = ["BaselineRules", "VerifyPipeline", "cvss31_base_score", "severity_from_score"]
