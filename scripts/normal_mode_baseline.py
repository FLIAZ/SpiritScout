"""只读回放正常模式历史的发送视图，输出体积指标；不调用模型、不改写会话。"""
from __future__ import annotations

import argparse
import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet

from spiritscout.gateway.normal_context import project


def read_checkpoints(root: Path) -> list[dict]:
    cipher = Fernet((root / 'execution.key').read_bytes())
    result = []
    with closing(sqlite3.connect((root / 'bb.db').resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        heads = conn.execute("SELECT * FROM runtime_checkpoints WHERE checkpoint_id IN "
            "(SELECT MAX(checkpoint_id) FROM runtime_checkpoints WHERE scope='ctf_conversation' GROUP BY checkpoint_key)").fetchall()
        for head in heads:
            row, tails, latest = head, [], None
            while row['schema_version'] == 2:
                delta = json.loads(cipher.decrypt(row['state_cipher']))
                base = delta['base_checkpoint']
                if type(base) is not int or base >= row['checkpoint_id'] or len(tails) >= 31:
                    raise ValueError('invalid checkpoint chain')
                if latest is None:
                    latest = delta['state']
                tails.append(delta['tail'])
                row = conn.execute('SELECT * FROM runtime_checkpoints WHERE checkpoint_id=? AND scope=? AND checkpoint_key=?',
                    (base, head['scope'], head['checkpoint_key'])).fetchone()
                if row is None:
                    raise ValueError('checkpoint base missing')
            if row['schema_version'] != 1:
                raise ValueError('unsupported checkpoint version')
            state = json.loads(cipher.decrypt(row['state_cipher']))
            messages = state.get('messages', [])
            for tail in reversed(tails):
                messages.extend(tail)
            result.append({**(latest or state), 'messages': messages})
    return result


def measure(root: Path) -> dict:
    rows = []
    for state in read_checkpoints(root):
        original = copy.deepcopy(state['messages'])
        view = project(state, [])
        users = [m for m in original if m.get('role') in {'user', 'system'}]
        rows.append({'messages': len(original), 'archived_turns': view['archived_turns'],
            'original_estimated_tokens': view['original_tokens'], 'projected_estimated_tokens': view['estimated_tokens'],
            'reduction_percent': round(100 * (1 - view['estimated_tokens'] / max(1, view['original_tokens'])), 2),
            'original_history_unchanged': original == state['messages'],
            'all_user_system_messages_preserved': all({k: v for k, v in m.items() if not k.startswith('_')}
                in view['messages'] for m in users)})
    return {'session': root.name, 'routes': rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sessions', nargs='+', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = {'kind': 'offline_context_projection', 'note': '离线体积估算，未计工具 schema；不代表实际 token 账单、缓存命中率或答题质量。',
              'sessions': [measure(root) for root in args.sessions]}
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + '\n', encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
