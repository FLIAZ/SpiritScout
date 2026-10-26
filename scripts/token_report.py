"""只读导出版本与阶段的用量，不调用模型、不输出密钥、提示词或工具正文。"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from spiritscout import __version__
from spiritscout.gateway.usage import read_usage


def audit_attempts(workspace: Path, version: str | None) -> dict[str, Any]:
    """失败/取消尝试与成功响应分列；已消费但无 usage 的调用保持未知。"""
    rows: list[dict[str, Any]]=[]
    errors: list[dict[str, str]]=[]
    for source in sorted(workspace.glob('sessions/*/audit.jsonl')):
        seen: set[str]=set()
        try:
            with source.open() as stream:
                for line in stream:
                    try:
                        event=json.loads(line)
                    except ValueError:
                        errors.append({'file':str(source.relative_to(workspace)),'error':'invalid_json_line'})
                        continue
                    if not isinstance(event,dict) or event.get('type')!='model.attempt_finished':
                        continue
                    row=event.get('detail') or {}
                    if not isinstance(row,dict) or (version is not None and row.get('runtime_version')!=version):
                        continue
                    key=row.get('attempt_id')
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    rows.append(row)
        except OSError as error:
            errors.append({'file':str(source.relative_to(workspace)),'error':type(error).__name__})
    states: dict[str,int]=defaultdict(int)
    for row in rows:
        states[str(row.get('status') or 'unknown')]+=1
    result: dict[str, Any]={'attempts':len(rows),'statuses':dict(states),
        'unreported_attempts':sum(row.get('usage_status')!='reported' for row in rows),
        'read_errors':errors,'note':'separate attempt usage; do not add to successful logical response totals'}
    for key in ('input_tokens','output_tokens'):
        known=[row[key] for row in rows if type(row.get(key)) is int and row[key]>=0]
        result['reported_'+key]=sum(known) if known else None
        result[key+'_unknown_attempts']=len(rows)-len(known)
    return result


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [row for row in rows if row.get('cached_input_tokens') is not None]
    inputs = sum(row.get('input_tokens', 0) for row in measured)
    cached = sum(min(row['cached_input_tokens'], row.get('input_tokens', 0)) for row in measured)
    attempts = [row for row in rows if row.get('sdk_attempts') is not None]
    prefixes = [row for row in rows if row.get('request_bytes') is not None]
    parts: dict[str, int] = defaultdict(int)
    for row in rows:
        for key, value in (row.get('context_parts') or {}).items():
            parts[key] += value
    return {
        'calls': len(rows), 'input_tokens': sum(row.get('input_tokens', 0) for row in rows),
        'output_tokens': sum(row.get('output_tokens', 0) for row in rows),
        'measured_calls': len(measured), 'unmeasured_calls': len(rows) - len(measured),
        'measured_input_tokens': inputs, 'cached_input_tokens': cached,
        'uncached_input_tokens': inputs - cached, 'hit_rate': cached / inputs if inputs else None,
        'sdk_attempts': sum(row['sdk_attempts'] for row in attempts) if attempts else None,
        'attempts_unknown_calls': len(rows) - len(attempts),
        'unreported_attempts': sum(row.get('unreported_attempts') or 0 for row in attempts) if attempts else None,
        'prefix_samples': len(prefixes),
        'request_bytes': sum(row.get('request_bytes') or 0 for row in prefixes),
        'reused_prefix_bytes': sum(row.get('reused_prefix_bytes') or 0 for row in prefixes),
        'new_suffix_bytes': sum(row.get('new_suffix_bytes') or 0 for row in prefixes),
        'context_parts_bytes': dict(parts),
    }


def build_report(workspace: Path, *, version: str | None = __version__) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    sources = set(workspace.glob('sessions/*/usage.db'))
    sources.update(path for path in workspace.glob('sessions/*/usage.json') if not path.with_suffix('.db').exists())
    for source in sorted(sources):
        try:
            snapshot = read_usage(source.with_suffix('.json'), ledger=source.with_suffix('.db')) or {}
        except (OSError, ValueError, sqlite3.Error) as error:
            errors.append({'file': str(source.relative_to(workspace)), 'error': type(error).__name__})
            continue
        seen: set[str] = set()
        for row in snapshot.get('call_series', []):
            call_id = row.get('call_id')
            if call_id and call_id in seen:
                continue
            if call_id:
                seen.add(call_id)
            if version is None or row.get('runtime_version') == version:
                rows.append(row)
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        enabled = row.get('patch_optimization_enabled')
        mode = 'enabled' if enabled is True else 'disabled' if enabled is False else 'unknown'
        groups[(row.get('runtime_version') or 'legacy', row.get('model', ''),
                row.get('cache_scope') or row.get('task_type', ''), row.get('task_type', ''), mode)].append(row)
    return {
        'current_version': __version__, 'version_filter': version, 'source_files': len(sources),
        'methodology': {
            'cache_hits': 'endpoint-reported usage only; unknown excluded from hit rate',
            'prefix_bytes': 'local diagnostic, not provider token counts or cache hits',
            'attempts': 'SDK requests visible to the wrapper; missing usage stays unknown',
            'coverage': 'persisted successful logical responses; failed requests remain in audit logs',
            'legacy': 'records without runtime_version are not attributed to the current version',
        },
        'summary': summarize(rows), 'unreadable_files': errors,
        'audit_attempts':audit_attempts(workspace,version),
        'groups': [dict(runtime_version=key[0], model=key[1], cache_scope=key[2], task_type=key[3],
                        patch_optimization=key[4], **summarize(items))
                   for key, items in sorted(groups.items())],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=Path('.spiritscout'))
    parser.add_argument('--version', default=__version__, help='默认仅统计当前代码版本')
    parser.add_argument('--all-versions', action='store_true', help='包括明确标为 legacy 的旧记录')
    args = parser.parse_args()
    print(json.dumps(build_report(args.workspace, version=None if args.all_versions else args.version), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
