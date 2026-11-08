"""工具观察的可逆文本表示，不判断内容的重要性或删除观察。"""
from __future__ import annotations

import json
from itertools import groupby


def lossless_runs(content: str) -> str | None:
    """仅在明显缩小时编码连续重复行；换行、顺序和出现次数全部保留。"""
    if len(content) < 4000:
        return None
    parts: list[str | dict] = []
    pending: list[str] = []
    repeated = False
    for line, group in groupby(content.splitlines(keepends=True)):
        count = sum(1 for _ in group)
        if count >= 4 and len(line) * count > 300:
            if pending:
                parts.append(''.join(pending))
                pending = []
            parts.append({'text': line, 'repeat': count})
            repeated = True
        else:
            pending.extend([line] * count)
    if not repeated:
        return None
    if pending:
        parts.append(''.join(pending))
    encoded = ('原消息 content 的无损表示：按顺序拼接字符串，并将 {text,repeat} 展开为 text 重复 repeat 次；'
               '没有删除原消息中的不同内容，也不增加新观察。\n' + json.dumps({
                   'format': 'exact_line_runs_v1', 'original_chars': len(content), 'parts': parts},
                   ensure_ascii=False, separators=(',', ':')))
    # 小幅缩短不足以抵消理解开销；原样发送更划算。
    return encoded if len(encoded) <= len(content) * .7 and len(content) - len(encoded) >= 2000 else None
