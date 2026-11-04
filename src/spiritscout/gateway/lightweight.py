"""轻量补偿资格：部署、会话模式和完整候选模型集共同显式授权。"""
from __future__ import annotations

from collections.abc import Sequence

from spiritscout.config.settings import ModelEntry
from spiritscout.core.models import RunMode


def lightweight_enabled(requested: bool, mode: RunMode,
                        models: Sequence[ModelEntry]) -> bool:
    """混合编队或云端备份保守关闭补偿，防止裁剪作用于非本地模型。"""
    return (requested and mode in (RunMode.CTF, RunMode.AWDP) and bool(models)
            and all(entry.provider == "ollama" and entry.ollama_lightweight
                    for entry in models))
