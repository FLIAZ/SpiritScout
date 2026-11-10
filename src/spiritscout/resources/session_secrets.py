"""会话秘密引用：明文仅在执行边界解析。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote, quote_plus

from spiritscout.agents.scoped_assets import _within
from spiritscout.core.ids import new_id
from spiritscout.core.models import Scope
from spiritscout.resources.store import ResourceStore


class SessionSecrets:
    def __init__(self, workspace):
        self.store = ResourceStore(Path(workspace) / 'private-secrets')
        self._saved = {}
        self._values = {}
        for row in self.store.settings('secret'):
            data = row['data']
            digest = hashlib.sha256(data['value'].encode()).hexdigest()
            self._saved[(digest, data['source'], data['kind'])] = row['id']
            if data['kind'] != 'js_source':
                self._values[data['value']] = row['id']

    def save(self, value: str, *, source: str, kind: str):
        if not isinstance(value, str) or not value or len(value.encode()) > 2 * 1024 * 1024:
            raise ValueError('秘密值为空或超过保存上限')
        digest = hashlib.sha256(value.encode()).hexdigest()
        key = (digest, source, kind)
        reference = self._saved.get(key)
        if reference is None:
            reference = new_id('secret')
            self.store.save('js_source' if kind == 'js_source' else 'secret', reference,
                            {'value': value, 'source': source, 'kind': kind}, 0)
            self._saved[key] = reference
        if kind != 'js_source':
            self._values[value] = reference
        return {'secret_ref': reference, 'kind': kind,
                'fingerprint': digest, 'status': 'unverified'}

    def resolve(self, reference: str, target: str):
        stored = self.store.setting('secret', reference) or self.store.setting('js_source', reference)
        row = stored['data'] if stored else None
        if not row or not _within(target, Scope(allowed_urls=[row['source']])):
            raise ValueError('秘密引用不存在或不适用于目标范围')
        return row['value']

    def redact(self, value):
        return protect_values(value, self._values)


def protect_values(value, values):
    replacements = {}
    for secret, reference in values.items():
        for variant in (secret, quote(secret, safe=''), quote_plus(secret), json.dumps(secret)[1:-1]):
            replacements[variant] = reference
    if not replacements:
        return value
    # 已发出的引用可再次经过投影层，不能把短秘密子串重复替换进引用本身。
    alternatives = set(replacements) | set(values.values())
    pattern = re.compile('|'.join(re.escape(secret) for secret in sorted(alternatives, key=len, reverse=True)))
    def protect(item):
        if isinstance(item, dict):
            return {key: protect(data) for key, data in item.items()}
        if isinstance(item, list):
            return [protect(data) for data in item]
        if isinstance(item, str):
            return pattern.sub(lambda match: replacements.get(match.group(0), match.group(0)), item)
        return item
    return protect(value)


def redact_known(workspace, value):
    root = Path(workspace) / 'private-secrets'
    if not (root / 'catalog.db').is_file():
        return value
    rows = ResourceStore(root).settings('secret')
    values = {}
    for row in rows:
        if row['data']['kind'] == 'js_source':
            continue
        secret = row['data']['value']
        values[secret] = row['id']
    return protect_values(value, values)
