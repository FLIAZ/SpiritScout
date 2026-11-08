"""只读回放真实轻量会话的上下文；不调用模型、不执行历史工具、不修改会话数据库。

用法：.venv/bin/python scripts/replay_light_context.py --session PATH --find main --output REPORT.json
报告只含校验与计量，不复制审计原文、密钥或真实模型配置。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.agents import _complete_tool_history
from spiritscout.ctf.evidence_review import result_reference
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.light_memory import (
    MEMORY_KEY,
    memory_execute,
    read_history,
    tool_records,
)
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.gateway.context import estimate_input_tokens
from spiritscout.tools.journal import ExecutionJournal


def load_snapshot(root: Path) -> dict:
    """复用实际增量检查点解码器，但不运行会创建/迁移数据库的初始化。"""
    with closing(sqlite3.connect((root / 'bb.db').as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT checkpoint_key FROM runtime_checkpoints WHERE scope='ctf_conversation' "
                                 'ORDER BY checkpoint_id DESC LIMIT 1').fetchone()
        if row is None:
            raise ValueError('该会话没有可回放的 ctf_conversation 检查点')
        journal = object.__new__(ExecutionJournal)
        journal._cipher = Fernet((root / 'execution.key').read_bytes())
        journal.blackboard = SimpleNamespace(transaction=lambda: nullcontext(connection))
        result = journal.latest_checkpoint('ctf_conversation', row['checkpoint_key'])
    return result


def make_agent(root: Path, saved: dict, budget: int):
    mode = RunMode(json.loads((root / 'meta.json').read_text()).get('mode', 'ctf'))
    if mode not in (RunMode.CTF, RunMode.AWDP):
        raise ValueError('仅支持轻量 CTF/AWDP 单路线检查点')
    ctx = SimpleNamespace(mode=mode, shared_cache=None, search_enabled=False,
        gateway=SimpleNamespace(config=SimpleNamespace(lightweight_local=True), context_budget=lambda **kw: budget),
        audit=NullAuditLogger(), builtin_ctx={'workspace_root': str(root)}, blackboard=None, policy=None, ring=None)
    ctx.for_agent = lambda _: ctx
    agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=mode == RunMode.AWDP))
    frame = next((message['_light_task_frame'] for message in saved['state']['messages']
                  if message.get('_light_task_frame')), {})
    task_id, route_id = saved['checkpoint_key'].split(':', 1)
    task = Task(task_id=task_id, agent_type=agent.agent_type, task_type='ctf_single',
                target=frame.get('target') or '', payload=frame)
    agent._world_snapshot = lambda: saved['state'].get('world_state', {})
    return agent, task, SimpleNamespace(name='solve', route_id=route_id)


def prefix_state(saved: dict, stop: int) -> dict:
    state = copy.deepcopy(saved)
    state['messages'] = state['messages'][:stop]
    state.pop(MEMORY_KEY, None)  # 不把检查点末尾的笔记灌回更早回合。
    state.pop('last_evidence_review', None)
    state['review_due'] = None
    state['evidence_rows'] = [row for row in state.get('evidence_rows', [])
                             if result_reference(row, state['messages']).get('message_index') is not None]
    state['evidence_seen'] = {key: entry for key, entry in state.get('evidence_seen', {}).items()
        if result_reference(entry.get('first_observation', {}), state['messages']).get('message_index') is not None}
    results = [m for m in state['messages'] if m.get('role') == 'tool']
    if results and (key := results[-1].get('_context_repeat_key')):
        count = sum(m.get('_context_repeat_key') == key for m in results)
        if count > 1:
            state['review_due'] = {'tool': 'historical_tool', 'same_result_count': count,
                                  'reason': '历史中相同请求与结果再次出现'}
    return state


def replay(root: Path, *, budget: int = 10650, queries: list[str] | None = None) -> dict:
    root = root.resolve()
    saved = load_snapshot(root)
    original = saved['state']['messages']
    checksum = hashlib.sha256(json.dumps(original, ensure_ascii=False).encode()).hexdigest()
    agent, task, route = make_agent(root, saved, budget)
    cuts = [i for i, m in enumerate(original) if m.get('role') == 'assistant' and m.get('tool_calls')]
    cuts.append(len(original))
    timeline, search_checks, retrieval_checks = [], 0, 0
    for turn, stop in enumerate(cuts, 1):
        state = prefix_state(saved['state'], stop)
        before = copy.deepcopy(state['messages'])
        view = agent._light_model_messages(task, route, state)
        estimate = estimate_input_tokens(view, agent._model_tool_schemas())
        assert estimate <= budget, (turn, estimate, budget)
        assert _complete_tool_history(view), f'第 {turn} 轮工具配对不完整'
        assert state['messages'] == before, f'第 {turn} 轮改写了原始历史'
        catalog = next((m['content'] for m in view if str(m.get('content', '')).startswith('历史证据目录')), '')
        data = json.loads(catalog.split('\n', 1)[1]) if catalog else {}
        checks = {}
        for query in queries or []:
            expected = [row['message_index'] for row in tool_records(before)
                        if query in json.dumps(row['arguments'], ensure_ascii=False) + '\n' + str(row['message'].get('content') or '')]
            found, cursor = [], 0
            while True:
                result = read_history(state, {'view': 'index', 'find': query, 'offset': cursor})
                assert result['ok'] and result['total_matches'] == len(expected)
                for item in result['items']:
                    index = item['message_index']
                    found.append(index)
                    page = read_history(state, {'message_index': index, 'view': 'content', 'length': 4000})
                    assert page['ok'] and page['text'] == before[index]['content'][:4000]
                    retrieval_checks += 1
                if not result['has_more']:
                    break
                assert result['next_offset'] > cursor
                cursor = result['next_offset']
            assert found == expected
            search_checks += 1
            checks[query] = {'source_results': len(found), 'in_sent_catalog': query in catalog}
        timeline.append({'turn': turn, 'raw_messages': stop, 'estimated_input_tokens': estimate,
                         'retained_tool_results': sum(m.get('role') == 'tool' for m in view),
                         'catalog_groups': len(data.get('archived', [])), 'omitted_groups': data.get('omitted_groups', 0),
                         'review': 'last_evidence_review' in state and bool(state['last_evidence_review']), 'queries': checks})
    # 单独写入回归夹具笔记，验证真实旧来源可引用、序列化恢复后仍可见；这不是模型作出的发现。
    state = prefix_state(saved['state'], len(original))
    source = next(row for row in state['evidence_seen'].values() if row.get('first_observation', {}).get('ok'))
    index = result_reference(source['first_observation'], original)['message_index']
    quote = original[index]['content'][:120]
    note = 'REGRESSION_FIXTURE_NOTE: preserve the original source; validation remains pending.'
    result = memory_execute(state, {'action': 'update', 'base_revision': 0, 'upsert': [{
        'id': 'replay-source', 'kind': 'fact', 'text': note, 'sources': [{'message_index': index, 'quote': quote}]}]})
    assert result['ok'], result
    state = json.loads(json.dumps(state, ensure_ascii=False))
    view = agent._light_model_messages(task, route, state)
    assert note in str(view)
    assert state['messages'] == original
    assert estimate_input_tokens(view, agent._model_tool_schemas()) <= budget
    assert hashlib.sha256(json.dumps(original, ensure_ascii=False).encode()).hexdigest() == checksum
    return {'session_id': root.name, 'checkpoint_id': saved['checkpoint_id'], 'checkpoint_turn': saved['state'].get('turn'),
            'source_messages_sha256': checksum, 'input_budget': budget, 'replayed_requests': len(timeline),
            'search_checks': search_checks, 'original_source_reads': retrieval_checks,
            'max_estimated_input_tokens': max(row['estimated_input_tokens'] for row in timeline),
            'tool_pairs_valid': True, 'original_history_unchanged': True,
            'memory_checkpoint_roundtrip': True, 'memory_note_is_test_fixture': True,
            'live_model_calls': 0, 'historical_tools_executed': 0, 'timeline': timeline}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--budget', type=int, default=10650)
    parser.add_argument('--find', action='append', default=[])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = replay(args.session, budget=args.budget, queries=args.find)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'timeline'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
