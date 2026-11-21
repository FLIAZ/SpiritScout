"""编码转换与古典密码的本地计算工具。"""
from __future__ import annotations

import base64
import binascii
import codecs
import re
import urllib.parse

# 疑似编码串的最小长度，过滤普通短词。
_MIN_BLOB_LEN = 12


def _decode_blobs(text: str) -> list[str]:
    """文本中的疑似编码串 → 链式解码结果（去重保序，≤20 条）。"""
    out: list[str] = []
    for blob in re.findall(rf"[A-Za-z0-9+/=_-]{{{_MIN_BLOB_LEN},}}", text)[:20]:
        decoded = _decode_chain(blob)
        if decoded and decoded != blob and decoded not in out:
            out.append(decoded)
    return out


def _decode_chain(text: str, max_depth: int = 4) -> str:
    """编码变换链自动推理：Base64/Hex/URL/Rot13 逐层尝试（4.4.4 编码变换链）。

    每层只接受「可打印且发生变化」的解码，避免指数爆炸。
    """
    current = text.strip()
    for _ in range(max_depth):
        next_text = _try_single_decode(current)
        if next_text is None or next_text == current:
            break
        current = next_text
    return current


def _try_single_decode(text: str) -> str | None:
    candidates: list[str] = []
    # Base64（需合法字符集与长度）
    if re.fullmatch(r"[A-Za-z0-9+/=]+", text) and len(text) % 4 == 0:
        try:
            candidates.append(base64.b64decode(text, validate=True)
                                .decode("utf-8", errors="strict"))
        except (binascii.Error, UnicodeDecodeError, ValueError):
            pass
    # Hex
    if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", text) and len(text) >= 4:
        try:
            candidates.append(bytes.fromhex(text).decode("utf-8", errors="strict"))
        except UnicodeDecodeError:
            pass
    # URL
    if "%" in text:
        decoded = urllib.parse.unquote(text)
        if decoded != text:
            candidates.append(decoded)
    # Rot13（字母串）
    if re.fullmatch(r"[A-Za-z]+", text):
        candidates.append(codecs.decode(text, "rot_13"))
    for cand in candidates:
        if cand and all(ch.isprintable() or ch.isspace() for ch in cand):
            return cand
    return None


def _caesar_brute(text: str) -> list[str]:
    """凯撒全位移爆破（仅对纯字母密文），返回全部位移结果。"""
    if not re.fullmatch(r"[A-Za-z]+", text.strip()):
        return []
    out = []
    for shift in range(1, 26):
        out.append("".join(
            chr((ord(c) - base - shift) % 26 + base) if c.isalpha() else c
            for c in text.strip()
            for base in (ord('A') if c.isupper() else ord('a'),)
        ))
    return out


def _rail_fence_brute(text: str, max_rails: int = 6) -> list[str]:
    """栅栏密码定栏还原（W 型简化为之字形标准栅栏）。"""
    cipher = text.strip()
    if len(cipher) < 4 or not cipher.isalpha():
        return []
    results = []
    for rails in range(2, min(max_rails, len(cipher) - 1) + 1):
        # 计算每栏字符数
        pattern = list(range(rails)) + list(range(rails - 2, 0, -1))
        counts = [0] * rails
        for i in range(len(cipher)):
            counts[pattern[i % len(pattern)]] += 1
        # 切分密文并按栏重排
        rows: list[list[str]] = []
        idx = 0
        for count in counts:
            rows.append(list(cipher[idx: idx + count]))
            idx += count
        pos = [0] * rails
        plain = []
        for i in range(len(cipher)):
            rail = pattern[i % len(pattern)]
            plain.append(rows[rail][pos[rail]])
            pos[rail] += 1
        results.append("".join(plain))
    return results
