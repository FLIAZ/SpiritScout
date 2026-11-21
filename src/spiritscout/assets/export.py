"""资产明细的确定性导出；HTTP 观察按出口保留，不生成模型总结。"""
from __future__ import annotations

import csv
import io
from typing import Any

FIELDS = ('asset', 'asset_type', 'title', 'ip', 'overall_verification_status', 'verification_status', 'http_status',
          'egress_id', 'egress_name', 'exit_ip', 'http_error', 'observed_at', 'source')


def asset_csv(targets: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=FIELDS)
    writer.writeheader()
    for asset in targets:
        observations = asset.get('http_observations') or [{}]
        for observation in observations:
            row = {**asset, **observation, 'asset': asset['value'],
                   'overall_verification_status': asset.get('verification_status', 'unverified'),
                   'observed_at': observation.get('last_seen', asset.get('last_seen', ''))}
            row["egress_id"] = row.get("egress_id") or "unknown"
            row["egress_name"] = row.get("egress_name") or "历史出口未知"
            values = {}
            for key in FIELDS:
                value = row.get(key, '')
                value = '' if value is None else value
                # Excel/WPS 不应把来自目标的标题、域名等执行为公式。
                if isinstance(value, str) and (value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n'))):
                    value = "'" + value
                values[key] = value
            writer.writerow(values)
    return output.getvalue()
