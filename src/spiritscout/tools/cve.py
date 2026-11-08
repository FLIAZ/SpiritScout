"""面向智能体的 CVE 情报工具；参考数据不参与现场证据认定。"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from spiritscout.core.models import RunMode
from spiritscout.cve.service import NOTICE, CVEService
from spiritscout.tools.sandbox_paths import open_directory
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec

GUIDANCE = ("使用 cve_lookup 查询已观测产品、版本或 CVE 的参考情报。先查本地；"
            "有明确缺口时 source=nvd 或 action=lookup 补充官方情报，"
            "包版本用 action=package 与生态名查询 OSV。关键词结果只是候选，不能推断版本受影响。"
            "保留来源、时间、错误、分页和版本条件；比赛模式只使用本地收录及缓存。" + NOTICE)
SCHEMA = {"type": "function", "function": {"name": "cve_lookup", "description": GUIDANCE,
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "action": {"type": "string", "enum": ["search", "lookup", "package", "status"]},
        "cve_id": {"type": "string"}, "query": {"type": "string", "maxLength": 500},
        "source": {"type": "string", "enum": ["local", "nvd"], "default": "local"},
        "cpe": {"type": "string", "description": "已确认的完整 CPE 2.3，不能从产品名猜测"},
        "package": {"type": "string"}, "ecosystem": {"type": "string"}, "version": {"type": "string"},
        "page_token": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "refresh": {"type": "boolean", "default": False},
    }, "required": ["action"]}}}
SPEC = ToolSpec(name="cve_lookup", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
                description=GUIDANCE, automatic_replay=False,
                allowed_params=set(SCHEMA['function']['parameters']['properties']))


def _query(service: CVEService, params: dict, online: bool) -> dict:
    action = params.pop('action')
    refresh = params.pop('refresh', False)
    if not isinstance(refresh, bool):
        raise ValueError('refresh 需要布尔值')
    offset, limit = params.get('offset', 0), params.get('limit', 20)
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 50:
        raise ValueError('offset >= 0，limit 为 1–50 的整数')
    for key, value in params.items():
        if key not in {'offset', 'limit'} and (not isinstance(value, str) or len(value) > 4096):
            raise ValueError(f'{key} 需要有界字符串')
    if action == 'status':
        return service.store.status()
    if action == 'lookup':
        return service.lookup(params.get('cve_id', ''), online=online, refresh=refresh)
    if action == 'package':
        return service.package(params.get('package', ''), params.get('ecosystem', ''),
                               params.get('version', ''), page_token=params.get('page_token', ''),
                               online=online, refresh=refresh)
    if action == 'search':
        return service.search(params.get('query', ''), source=params.get('source', 'local'),
                              offset=offset, limit=limit, cpe=params.get('cpe', ''),
                              online=online, refresh=refresh)
    raise ValueError('不支持的 CVE action')


def _preview(data: dict, workspace_root: str) -> tuple[str, dict]:
    text = json.dumps(data, ensure_ascii=False)
    if len(text) <= 18000:
        return text, data
    if not workspace_root:
        raise ValueError('情报正文较大，需要工作区保存完整记录')
    relative = Path('artifacts') / 'cve'
    filename = f'{uuid.uuid4().hex}.json'
    with open_directory(Path(workspace_root).absolute(), relative, create=True) as directory:
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(text)
    result = {'notice': NOTICE, 'full_record_file': str(relative / filename), 'preview_only': True,
              'instruction': '使用文件读取工具按需读取完整情报；当前输出不是完整版本条件。',
              'source_status': data.get('source_status', data.get('sources')),
              'cve_id': data.get('cve_id'), 'total': data.get('total'),
              'next_offset': data.get('next_offset'), 'summary_excerpt': text[:10000]}
    return json.dumps(result, ensure_ascii=False), result


async def cve_lookup(*, _cve: CVEService | None = None, _run_mode: RunMode | None = None,
                     _lightweight_local: bool = False, workspace_root: str = '', **params: Any) -> ToolResult:
    # 环境上下文由 Registry 注入，只投影模型参数，避免向服务透传 guard 等对象。
    params = {k: v for k, v in params.items() if k in SPEC.allowed_params}
    if _cve is None:
        return ToolResult(tool=SPEC.name, ok=False, error='CVE 情报库未启用')
    try:
        local_only = _lightweight_local and _run_mode in (RunMode.CTF, RunMode.AWDP)
        online = _cve.config.online and _run_mode != RunMode.COMPETITION and not local_only
        data = await asyncio.to_thread(_query, _cve, params, online)
        text, parsed = await asyncio.to_thread(_preview, data, workspace_root)
        states = data.get('sources', [data.get('source_status', data)])
        usable = bool(data.get('records')) or any(s.get('status') in {'ok', 'cached'} for s in states if isinstance(s, dict))
        failed = any(s.get('status') in {'error', 'offline'} for s in states if isinstance(s, dict))
        return ToolResult(tool=SPEC.name, ok=usable or not failed, stdout=text, parsed=parsed,
                          error='情报源不可用，详见来源状态' if failed and not usable else '')
    except (ValueError, KeyError, TypeError) as exc:
        return ToolResult(tool=SPEC.name, ok=False, error=str(exc))
