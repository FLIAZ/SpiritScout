"""为资产、漏洞、题目和任务生成唯一标识。"""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    """生成带前缀的全局唯一 ID，如 ``asset-3f9a2c1e7b4d``。

    前缀约定：asset / vuln / task / dag / route / cred / flag / rpt / evt / sess。
    """
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
