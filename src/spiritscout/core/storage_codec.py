"""本地存储的无损编码；原文与压缩记录可混读，不参与模型上下文裁剪。"""
from __future__ import annotations

import json
import struct
import zlib
from typing import Any

_MAGIC = b"SSZ1\x00"
_HEADER_SIZE = len(_MAGIC) + 8
MIN_COMPRESS_BYTES = 1024


def is_compressed(data: bytes) -> bool:
    return data.startswith(_MAGIC)


def compress_bytes(raw: bytes) -> bytes:
    """仅压缩有实际收益的大记录；长度头用于检验完整性及限制解压输出。"""
    if len(raw) < MIN_COMPRESS_BYTES:
        return raw
    packed = _MAGIC + struct.pack(">Q", len(raw)) + zlib.compress(raw, level=3)
    return packed if len(packed) + 64 < len(raw) else raw


def decompress_bytes(data: bytes) -> bytes:
    if not data.startswith(_MAGIC):
        return data
    if len(data) < _HEADER_SIZE:
        raise ValueError("存储压缩头不完整")
    length = struct.unpack(">Q", data[len(_MAGIC):_HEADER_SIZE])[0]
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(data[_HEADER_SIZE:], length + 1)
    except (zlib.error, OverflowError) as exc:
        raise ValueError("存储压缩记录损坏") from exc
    if len(raw) != length or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("存储压缩记录长度或结尾校验失败")
    return raw


def pack_json(value: Any, **kwargs: Any) -> bytes:
    return compress_bytes(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), **kwargs).encode("utf-8"))


def unpack_json(value: str | bytes) -> Any:
    """兼容旧 TEXT、旧 JSON 字节及带版本头的压缩 BLOB。"""
    return json.loads(decompress_bytes(value) if isinstance(value, bytes) else value)
