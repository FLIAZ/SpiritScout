"""CTF 执行者的公开探索落盘与上下文回读，不从正文推断因果。"""
from __future__ import annotations

from typing import Any

from spiritscout.blackboard.exchange import execute
from spiritscout.core.models import AgentType, BlackboardLayer
from spiritscout.gateway.progress import public_exploration

GUIDANCE = (
    '探索节点记录当前意图、尝试及其阻塞、排除或关键发现，后续回合会回读。'
    '其他分支可调用 graph_read 查询；exploration_write 修订节点，blackboard_write 发布可引用事实。'
)


def remember_ref(state: dict[str, Any], ref: dict[str, Any]) -> None:
    if ref.get('layer') != 'topology' or not ref.get('record_id'):
        return
    key = str(ref['record_id'])
    refs = state.setdefault('light_exploration_refs', [])
    refs[:] = [item for item in refs if item != key] + [key]


def refresh_context(bb: Any, state: dict[str, Any]) -> None:
    """显示最新版本，保留部分历史分支；完整图仍通过现有图工具查询。"""
    if bb is None or not state.get('light_exploration_refs'):
        return
    refs = state['light_exploration_refs']
    rows = []
    for key in refs[-4:]:
        row = bb.read_versioned(BlackboardLayer.TOPOLOGY, key)
        if not row or row[0].get('kind') != 'exploration':
            continue
        data, version = row
        value = {'blackboard_ref': {'layer': 'topology', 'record_id': key, 'version': version},
                 'node_type': data['node_type'], 'text': data['text'][:160], 'state': data['state'],
                 'reason': str(data.get('reason') or '')[:400], 'parent_refs': data.get('parent_refs', [])[:4]}
        if key == state.get('light_active_attempt') and data.get('public_milestone'):
            value['milestone'] = data['public_milestone']
        rows.append(value)
    state['light_exploration'] = {'active_attempt': state.get('light_active_attempt', ''),
        'nodes': rows, 'partial': True, 'known_nodes': len(refs),
        'notice': '其他分支与完整原因可用 graph_read/blackboard_read 查询。'}


def record_progress(bb: Any, state: dict[str, Any], detail: dict[str, Any], *, actor: str,
                    agent_type: AgentType, source: dict[str, Any]) -> dict[str, Any]:
    semantic = public_exploration(detail.get('exploration'))
    if semantic is None:
        return {'ok': False, 'error': '没有有效的公开探索声明'}
    # 同题同路线同标题复用记录；重启后即使检查点尚未保存，也从真实黑板找回。
    records = bb.query(BlackboardLayer.TOPOLOGY, limit=10000, data_filter=('kind', 'exploration'))
    scoped = [row for row in records if all(row['data'].get(key) == source.get(key)
                                           for key in ('task_id', 'route_id'))]

    def write(kind: str, text: str, status: str | None, parents: list[str], *, reason: str | None = None,
              extra: dict[str, Any] | None = None) -> dict[str, Any]:
        matches = [row for row in scoped if row['data'].get('node_type') == kind
                   and row['data'].get('text') == text
                   and (kind != 'attempt' or parents[0] in row['data'].get('parent_refs', []))]
        if len(matches) > 1:
            return {'ok': False, 'error': '同名探索节点归属不唯一，请用 exploration_write 按引用更新'}
        old = matches[0]['data'] if matches else {}
        args = {'node_type': kind, 'text': text,
                'state': status or old.get('state') or ('open' if kind == 'hypothesis' else 'running'),
                'reason': reason if reason is not None else old.get('reason', ''), 'parent_refs': parents}
        if matches:
            args.update(record_id=matches[0]['record_id'], version=matches[0]['version'])
            args['parent_refs'] = list(dict.fromkeys(matches[0]['data'].get('parent_refs', []) + parents))
        result = execute(bb, 'exploration_write', args, actor=actor, agent_type=agent_type,
                         source={**source, **(extra or {})})
        if result['ok']:
            remember_ref(state, result['blackboard_ref'])
        return result

    intent = write('hypothesis', semantic['intent'], None, [])
    if not intent['ok']:
        return intent
    parents = ['topology:' + intent['blackboard_ref']['record_id']]
    # 只用模型明确声明且归属唯一的 from 建转向关系，不能按时间拼接平行尝试。
    previous = [row for row in scoped if row['data'].get('node_type') == 'attempt'
                and row['data'].get('text') == semantic.get('from')
                and row['data'].get('text') != semantic['attempt']]
    if len(previous) == 1:
        parents.append('topology:' + previous[0]['record_id'])
    status = semantic.get('state')
    extra = {'public_milestone': semantic['milestone']} if semantic.get('milestone') else {}
    if semantic.get('milestone', {}).get('evidence_refs'):
        # 这里只存公开声明，不能把里程碑依据升级成已验证的 finding。
        extra['public_evidence_refs'] = semantic['milestone']['evidence_refs']
    attempt = write('attempt', semantic['attempt'], status, parents,
        reason=semantic.get('reason') or (detail['text'] if status in {'blocked', 'refuted', 'exhausted'} else None),
        extra=extra)
    if attempt['ok']:
        state['light_active_attempt'] = attempt['blackboard_ref']['record_id']
        refresh_context(bb, state)
    return attempt
