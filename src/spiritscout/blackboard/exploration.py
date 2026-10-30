"""模型维护探索图；资产与探索分层存储，显式引用连接两图。"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from spiritscout.core.models import AgentType, BlackboardLayer

KINDS = ('goal', 'hypothesis', 'attempt')
STATES = ('open', 'running', 'blocked', 'supported', 'refuted', 'exhausted', 'closed')
ASSET_RELATIONS = ('contains', 'resolves_to', 'exposes', 'redirects_to', 'related')
ASSET_LABELS = dict(zip(ASSET_RELATIONS, ('包含', '解析到', '暴露', '跳转到', '关联'), strict=True))
GUIDANCE = (
    '探索图与事实分开：用 exploration_write 记录目标、假设、尝试及状态，'
    '保留失败/阻塞原因，以 parent_refs 表达模型判断的派生关系，以 asset_refs 关联已有资产。'
    'graph_read 可按图、状态、资产或父节点查找，完整记录用 blackboard_read 按引用读取。'
    '有依据的产出仍用 blackboard_write 发布，可附 exploration_refs/asset_refs；'
    'asset_link 登记已有资产之间的包含、解析、暴露、跳转或关联关系，必须附依据；'
    '探索完成或 supported 不等于漏洞已验证。资产关联不代表授权，不改变现有调度或审批。'
)
SCHEMAS = [
    {'type': 'function', 'function': {
        'name': 'asset_link', 'description': '登记两条已有资产的有向关系及依据，关系不会自动升级为已验证结论。',
        'parameters': {'type': 'object', 'properties': {
            'source_ref': {'type': 'string'}, 'target_ref': {'type': 'string'},
            'relation': {'type': 'string', 'enum': list(ASSET_RELATIONS)},
            'evidence_refs': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}},
            'required': ['source_ref', 'target_ref', 'relation', 'evidence_refs']}}},
    {'type': 'function', 'function': {
        'name': 'graph_read', 'description': '按需查询资产或探索节点目录；失败和阻塞分支也保留，可按引用读取原文。',
        'parameters': {'type': 'object', 'properties': {
            'graph': {'type': 'string', 'enum': ['assets', 'exploration']},
            'state': {'type': 'string', 'enum': list(STATES)},
            'related_ref': {'type': 'string', 'description': '查询与资产或探索节点直接关联的节点，使用 layer:record_id 引用'},
            'offset': {'type': 'integer', 'minimum': 0},
            'relation_offset': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}, 'required': ['graph']}}},
    {'type': 'function', 'function': {
        'name': 'exploration_write', 'description': '创建或更新目标/假设/尝试；不是事实发布或工具执行。更新须携带当前 version。',
        'parameters': {'type': 'object', 'properties': {
            'record_id': {'type': 'string'}, 'version': {'type': 'integer', 'minimum': 1},
            'node_type': {'type': 'string', 'enum': list(KINDS)},
            'text': {'type': 'string'}, 'state': {'type': 'string', 'enum': list(STATES)},
            'reason': {'type': 'string'},
            **{name: {'type': 'array', 'items': {'type': 'string'}}
               for name in ('parent_refs', 'asset_refs', 'evidence_refs')}},
            'required': ['node_type', 'text', 'state']}}},
]
NAMES = {schema['function']['name'] for schema in SCHEMAS}


def resolve_ref(bb: Any, ref: str, *, asset: bool = False, exploration: bool = False) -> str:
    """图结构引用必须指向当前会话的实际记录；不根据 URL/时间猜测关系。"""
    if not isinstance(ref, str):
        raise ValueError('图引用必须是字符串')
    name, sep, version = ref.rpartition('@')
    if not sep:
        name = ref
    layer_name, separator, key = name.partition(':')
    if not separator or not key:
        raise ValueError('图引用必须为 layer:record_id，可附 @version')
    layer = BlackboardLayer(layer_name)
    row = bb.read_versioned(layer, key)
    if not row or sep and (not version.isdigit() or int(version) != row[1]):
        raise ValueError('图引用不存在或版本已变化，请重新查询')
    if asset and not (layer == BlackboardLayer.ASSET or layer == BlackboardLayer.TOPOLOGY
                      and row[0].get('kind') == 'finding' and row[0].get('type') == 'asset'):
        raise ValueError('asset_refs 只能引用资产记录')
    if exploration and (layer != BlackboardLayer.TOPOLOGY or row[0].get('kind') != 'exploration'):
        raise ValueError('parent_refs 只能引用探索节点')
    return f'{layer.value}:{key}'


def refs(bb: Any, values: Any, **kwargs: Any) -> list[str]:
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError('图引用须为数组，最多 100 条')
    return list(dict.fromkeys(resolve_ref(bb, value, **kwargs) for value in values))


def directory(bb: Any, graph: str, *, state: str = '', related_ref: str = '',
              offset: int = 0, limit: int = 20, recent: bool = False, relation_offset: int = 0) -> dict[str, Any]:
    if graph not in {'assets', 'exploration'}:
        raise ValueError('graph 须为 assets 或 exploration')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('offset≥0，limit 为 1–50 的整数')
    if type(relation_offset) is not int or relation_offset < 0:
        raise ValueError('relation_offset≥0')
    if state and state not in STATES:
        raise ValueError('未知探索状态')
    related_targets: set[str] = set()
    if related_ref:
        related_ref = resolve_ref(bb, related_ref)
        related_layer, _, related_key = related_ref.partition(':')
        related = bb.read(BlackboardLayer(related_layer), related_key) or {}
        related_targets = set(related.get('asset_refs', []) + related.get('parent_refs', []))
    relations = bb.query(BlackboardLayer.TOPOLOGY, limit=10001, data_filter=('type', 'asset_relation')) if graph == 'assets' else []
    for row in relations:
        data = row['data']
        if related_ref in (data.get('source_ref'), data.get('target_ref')):
            related_targets.update(data.get('asset_refs', []))
    related_targets.discard(related_ref)
    layer = BlackboardLayer.ASSET if graph == 'assets' else BlackboardLayer.TOPOLOGY
    # 当前黑板查询接口有界；明确返回截断标志，不能把采样冒充完整图。
    rows = bb.query(layer, limit=limit + 1 if recent else 10001,
                    **({'data_filter': ('kind', 'exploration')} if graph == 'exploration' else {}))
    rows = [{**row, 'layer': layer.value} for row in rows]
    if graph == 'assets':
        rows += [{**row, 'layer': 'topology'} for row in bb.query(
            BlackboardLayer.TOPOLOGY, limit=10001, data_filter=('type', 'asset'))
            if row['data'].get('kind') == 'finding']
    truncated = len(rows) > (limit if recent else 10000)
    items = []
    for row in rows[:10000]:
        data = row['data']
        if state and data.get('state') != state:
            continue
        if (related_ref and related_ref not in data.get('asset_refs', []) + data.get('parent_refs', [])
                and f'{row["layer"]}:{row["record_id"]}' not in related_targets):
            continue
        items.append({'blackboard_ref': {'layer': row['layer'], 'record_id': row['record_id'], 'version': row['version']},
                      'summary': str(data.get('text') or data.get('value') or row['record_id'])[:160],
                      'node_type': data.get('node_type', data.get('asset_type', 'asset')),
                      'state': data.get('state', ''), 'updated_by': row['updated_by'],
                      **({} if recent else {'asset_refs': data.get('asset_refs', []),
                                            'parent_refs': data.get('parent_refs', [])})})
    page = items[offset:offset + limit]
    if recent:
        # 先选最近窗口，再按稳定身份排列；节点更新不让未变化的整列发生位移。
        page.sort(key=lambda item: (item['blackboard_ref']['layer'], item['blackboard_ref']['record_id']))
    visible = {f'{item["blackboard_ref"]["layer"]}:{item["blackboard_ref"]["record_id"]}' for item in page}
    edges = [{'source_ref': row['data'].get('source_ref'), 'target_ref': row['data'].get('target_ref'),
              'relation': row['data'].get('relation'), 'evidence_state': row['data'].get('evidence_state', 'unverified'),
              'blackboard_ref': {'layer': 'topology', 'record_id': row['record_id'], 'version': row['version']}}
             for row in relations[:10000] if row['data'].get('source_ref') in visible or row['data'].get('target_ref') in visible]
    return {'ok': True, 'items': page, 'total': len(items), 'truncated': truncated or len(relations) > 10000,
            **({'relations': edges[relation_offset:relation_offset + limit], 'relations_total': len(edges),
                'relations_has_more': relation_offset + limit < len(edges),
                'next_relation_offset': relation_offset + limit} if graph == 'assets' else {}),
            'has_more': offset + limit < len(items), 'next_offset': offset + limit}


def execute(bb: Any, name: str, args: dict[str, Any], *, actor: str,
            agent_type: AgentType | None = None, source: dict[str, Any] | None = None) -> dict[str, Any]:
    if name == 'graph_read':
        return directory(bb, args.get('graph', ''), state=args.get('state', ''),
                         related_ref=args.get('related_ref', ''), offset=args.get('offset', 0), limit=args.get('limit', 20),
                         relation_offset=args.get('relation_offset', 0))
    if agent_type in {AgentType.REPORT, AgentType.VERIFIER}:
        raise ValueError('当前角色只读黑板')
    if name == 'asset_link':
        source_ref = resolve_ref(bb, args.get('source_ref'), asset=True)
        target_ref = resolve_ref(bb, args.get('target_ref'), asset=True)
        relation, evidence = args.get('relation'), args.get('evidence_refs')
        if source_ref == target_ref or relation not in ASSET_RELATIONS:
            raise ValueError('资产关系须连接不同资产，并使用合法 relation')
        if not isinstance(evidence, list) or not evidence or not all(isinstance(ref, str) and ref.strip() for ref in evidence):
            raise ValueError('资产关系需要非空 evidence_refs')
        key = 'asset-relation-' + hashlib.sha256(json.dumps([source_ref, relation, target_ref, evidence]).encode()).hexdigest()[:24]
        def label(ref: str) -> str:
            layer, _, record_id = ref.partition(':')
            asset = bb.read(BlackboardLayer(layer), record_id) or {}
            return str(asset.get('value') or asset.get('text') or record_id)

        data = {**(source or {}), 'kind': 'finding', 'type': 'asset_relation', 'source_ref': source_ref,
                'target_ref': target_ref, 'relation': relation, 'asset_refs': [source_ref, target_ref],
                'text': f'{label(source_ref)} {ASSET_LABELS[relation]} {label(target_ref)}', 'evidence_refs': evidence}
        if bb.read(BlackboardLayer.TOPOLOGY, key) is None:
            bb.write(BlackboardLayer.TOPOLOGY, key, data, updated_by=actor,
                     agent_type=agent_type or AgentType.MASTER, expected_version=0)
        row = bb.read_versioned(BlackboardLayer.TOPOLOGY, key)
        return {'ok': True, 'blackboard_ref': {'layer': 'topology', 'record_id': key, 'version': row[1]},
                'evidence_state': row[0].get('evidence_state', 'unverified')}
    node_type, text, state = args.get('node_type'), args.get('text'), args.get('state')
    if node_type not in KINDS or state not in STATES or not isinstance(text, str) or not text.strip():
        raise ValueError('探索节点需要合法 node_type、state 和非空 text')
    reason = args.get('reason', '')
    if not isinstance(reason, str) or state in {'blocked', 'refuted', 'exhausted'} and not reason.strip():
        raise ValueError('阻塞、反驳和走不通的分支必须保留原因')
    key = args.get('record_id')
    if key is not None and (not isinstance(key, str) or not key.strip()):
        raise ValueError('record_id 须为非空字符串')
    existing = bb.read_versioned(BlackboardLayer.TOPOLOGY, key) if isinstance(key, str) and key else None
    if key and (not existing or existing[0].get('kind') != 'exploration'):
        raise ValueError('更新目标必须是已有探索节点')
    version = args.get('version')
    if existing and (type(version) is not int or version != existing[1]):
        raise ValueError('探索节点版本已变化或缺失，请重新读取后更新')
    if not existing and version is not None:
        raise ValueError('新节点不能指定 version')
    key = key or 'exploration-' + uuid.uuid4().hex
    old = existing[0] if existing else {}
    data = {**old, **(source or {}), 'kind': 'exploration', 'node_type': node_type,
            'text': text.strip(), 'state': state, 'reason': reason,
            'parent_refs': refs(bb, args.get('parent_refs', old.get('parent_refs', [])), exploration=True),
            'asset_refs': refs(bb, args.get('asset_refs', old.get('asset_refs', [])), asset=True)}
    if f'topology:{key}' in data['parent_refs']:
        raise ValueError('探索节点不能引用自身为父节点')
    evidence = args.get('evidence_refs', old.get('evidence_refs', []))
    if not isinstance(evidence, list) or not all(isinstance(ref, str) and ref.strip() for ref in evidence):
        raise ValueError('evidence_refs 须为非空字符串数组')
    data['evidence_refs'] = evidence
    if data != old:
        bb.write(BlackboardLayer.TOPOLOGY, key, data, updated_by=actor,
                 agent_type=agent_type or AgentType.MASTER, expected_version=existing[1] if existing else 0)
    row = bb.read_versioned(BlackboardLayer.TOPOLOGY, key)
    return {'ok': True, 'blackboard_ref': {'layer': 'topology', 'record_id': key, 'version': row[1]},
            'chars': len(json.dumps(row[0], ensure_ascii=False)), 'state': state}
