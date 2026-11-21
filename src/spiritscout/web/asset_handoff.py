"""把用户勾选的资产冻结为会话材料；不把整个项目或批次自动当作授权。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spiritscout.assets.export import asset_csv
from spiritscout.blackboard import Blackboard, ConvergenceEngine
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.ids import new_id
from spiritscout.core.models import AgentType, Asset
from spiritscout.resources.store import ResourceStore


def resolve_handoff(store: ResourceStore, payload: dict[str, Any]) -> dict[str, Any] | None:
    campaign_id = payload.get('asset_campaign_id')
    if not campaign_id:
        if payload.get('asset_values'):
            raise SpiritScoutError('选择资产时必须指定收集批次')
        return None
    if payload.get('mode') != 'src':
        raise SpiritScoutError('收集资产只能带入 SRC 测试会话')
    campaign = store.asset_campaign(campaign_id)
    if campaign is None or campaign['project_id'] != payload.get('project_id'):
        raise SpiritScoutError('资产收集批次不存在或不属于所选项目')
    if campaign['status'] not in {'completed', 'completed_with_errors', 'stopped', 'failed'}:
        raise SpiritScoutError('请先结束资产收集，再选择资产开始测试会话')
    values = list(dict.fromkeys(payload.get('asset_values') or []))
    if not values:
        raise SpiritScoutError('请勾选本次要测试的资产')
    batch = store.asset_campaign_targets(campaign_id)
    targets = {item['value']: item for item in batch['targets']}
    if any(value not in targets for value in values):
        raise SpiritScoutError('所选资产不属于该批次，请刷新清单后重选')
    return {'campaign_id': campaign_id, 'project_id': campaign['project_id'],
            'targets': [targets[value] for value in values],
            'excluded_assets': batch['excluded_assets']}


async def materialize_handoff(snapshot: dict[str, Any], workspace: Path, bb_path: Path) -> list[dict[str, Any]]:
    targets = snapshot['targets']
    directory = workspace / 'attachments'
    directory.mkdir(exist_ok=True)
    name = 'collected-assets.csv'
    path = directory / name
    path.write_text(asset_csv(targets), encoding='utf-8-sig')
    records = [{'kind': 'attachment', 'name': name, 'rel': f'attachments/{name}',
                'path': str(path.resolve()), 'size': path.stat().st_size}]
    # 内部快照不是模型输入；恢复会话时沿用冻结材料，不重新读取会变化的项目清单。
    (workspace / 'asset-handoff.json').write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
    bb = Blackboard(bb_path)
    try:
        convergence = ConvergenceEngine(bb)
        for item in targets:
            convergence.merge_asset(Asset(
                asset_id=new_id('asset'), asset_type=item['asset_type'], value=item['value'],
                attrs={k: item[k] for k in ('title', 'ip', 'http_status', 'verification_status',
                                          'egress_id', 'egress_name', 'exit_ip', 'source_ref') if k in item},
                source_agent='asset-collection', scope_status='allowed_test',
                scope_reason='用户创建 SRC 会话时明确勾选',
            ), AgentType.MASTER)
    finally:
        await bb.aclose()
    return records
