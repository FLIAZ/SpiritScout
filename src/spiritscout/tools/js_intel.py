"""按秘密引用读取已抓取脚本；仅返回脱敏分页，不重新联网。"""
import json

from spiritscout.agents.scoped_assets import _within
from spiritscout.resources.session_secrets import SessionSecrets, redact_known
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec

SPEC = ToolSpec(name='js_source_read', kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
                description='按 script_ref 分页读取已保存脚本，识别到的敏感常量保持 secret_ref')
SCHEMA = {'type': 'function', 'function': {'name': 'js_source_read', 'description': SPEC.description,
    'parameters': {'type': 'object', 'properties': {'reference': {'type': 'string'},
        'target': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 12000}}, 'required': ['reference', 'target']}}}


async def js_source_read(reference, target, *, guard, workspace_root='', offset=0, limit=8000, **_):
    try:
        if not workspace_root or not _within(target, guard.scope) or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12000:
            raise ValueError('脚本引用、分页或授权范围无效')
        vault = SessionSecrets(workspace_root)
        row = vault.store.setting('js_source', reference) or vault.store.setting('secret', reference)
        data = row['data'] if row else None
        if not data or data['kind'] != 'js_source':
            raise ValueError('引用不是脚本源码')
        source = redact_known(workspace_root, vault.resolve(reference, target))
        result = {'reference': reference, 'source': source[offset:offset + limit],
                  'offset': offset, 'next_offset': offset + limit if offset + limit < len(source) else None,
                  'total_chars': len(source), 'secret_redacted': True}
        return ToolResult(tool='js_source_read', ok=True, parsed=result, stdout=json.dumps(result, ensure_ascii=False))
    except (ValueError, KeyError, TypeError):
        return ToolResult(tool='js_source_read', ok=False, error='脚本引用或读取范围无效')
