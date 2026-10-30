"""技能分组、组合筛选及首次添加时间回归。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from test_resource_management import cfg_at

from spiritscout.resources.store import ResourceStore, builtin_skills
from spiritscout.web.app import create_app


def stamp(day: str) -> float:
    return datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class SkillCatalogTests(unittest.TestCase):
    def test_added_time_is_immutable_and_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ResourceStore(directory)
            data = {'skill_id': 'manual', 'body': 'facts', 'enabled': True,
                    'created_at': 1, 'updated_at': 1, 'source_kind': 'builtin'}
            with patch('spiritscout.resources.store.time.time', return_value=100):
                store.save('skill', 'manual', data, 0)
            with patch('spiritscout.resources.store.time.time', return_value=200):
                store.save('skill', 'manual', {**data, 'created_at': 999, 'enabled': False}, 1)
            item = ResourceStore(directory).skills('manual')[0]
            self.assertEqual(item['created_at'], 100)
            self.assertEqual(item['updated_at'], 200)
            self.assertEqual(item['source_kind'], 'manual')
            with patch('spiritscout.resources.store.time.time', return_value=300):
                store.import_skills([{'skill_id': 'archive', 'source': 'archive',
                                      'body': 'facts', 'created_at': 1}])
            self.assertEqual(store.skills('archive')[0]['created_at'], 300)

    def test_legacy_unknown_and_builtin_override_keep_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ResourceStore(directory)
            old = {'skill_id': 'legacy', 'body': 'facts', 'enabled': True}
            payload = store.cipher().encrypt(json.dumps(old).encode())
            with store.connection() as conn:
                conn.execute('INSERT INTO settings VALUES(?,?,?,?,?)',
                             ('skill', 'legacy', 1, payload, 100))
            store.save('skill', 'legacy', old, 1)
            self.assertIsNone(store.skills('legacy')[0]['created_at'])
            key = next(iter(builtin_skills()))
            store.save('skill', key, {**builtin_skills()[key], 'enabled': False}, 0)
            item = next(row for row in store.skills() if row['skill_id'] == key)
            self.assertEqual(item['source_kind'], 'builtin')
            self.assertIsNone(item['created_at'])

    def test_api_filters_counts_facets_dates_sort_and_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(cfg_at(Path(directory)), [])
            store = app.state.resources
            for key, enabled, domain, source, day in (
                ('alpha', True, 'web', 'archive', '2026-10-01T00:00:00'),
                ('beta', False, 'web', 'archive', '2026-10-28T23:59:59'),
                ('gamma', True, 'crypto', '', '2026-10-29T00:00:00'),
            ):
                with patch('spiritscout.resources.store.time.time', return_value=stamp(day)):
                    store.save('skill', key, {'skill_id': key, 'body': 'fact',
                        'description': '筛选测试', 'domain': domain,
                        'enabled': enabled, 'source': source}, 0)
            with TestClient(app, base_url='http://127.0.0.1') as client:
                result = client.get('/api/resources/skills', params={
                    'q': '筛选测试', 'domain': 'web', 'source': 'archive',
                    'enabled': 'false', 'added_from': '2026-10-01',
                    'added_to': '2026-10-28', 'sort': 'added_desc'}).json()
                self.assertEqual([row['skill_id'] for row in result['items']], ['beta'])
                self.assertEqual(result['counts'], {'enabled': 1, 'disabled': 1})
                self.assertEqual(result['total'], 1)
                self.assertIn('crypto', result['facets']['domains'])
                result = client.get('/api/resources/skills?q=筛选测试&sort=added_desc&limit=1').json()
                self.assertEqual(result['items'][0]['skill_id'], 'gamma')
                self.assertTrue(result['has_more'])
                result = client.get('/api/resources/skills?q=筛选测试&sort=added_desc&limit=1&offset=1').json()
                self.assertEqual(result['items'][0]['skill_id'], 'beta')
                self.assertEqual(client.get('/api/resources/skills?added_from=2026-10-29&added_to=2026-10-01').status_code, 422)
                self.assertEqual(client.get('/api/resources/skills?sort=bad').status_code, 422)
                self.assertEqual(client.get('/api/resources/skills?added_from=bad').status_code, 422)
                result = client.get('/api/resources/skills?source=builtin&date_state=unknown').json()
                self.assertGreater(result['total'], 0)
                result = client.get('/api/resources/skills?source=builtin&added_from=2026-10-01').json()
                self.assertEqual(result['total'], 0)

    def test_toggle_and_edit_do_not_change_added_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(cfg_at(Path(directory)), [])
            with TestClient(app, base_url='http://127.0.0.1') as client:
                self.assertEqual(client.put('/api/resources/skills/catalog-unique-fixture', json={
                    'data': {'body': 'facts', 'domain': 'web'}}).status_code, 200)
                before = client.get('/api/resources/skills/catalog-unique-fixture').json()
                response = client.put('/api/resources/skills/catalog-unique-fixture', json={
                    'data': {'enabled': False, 'created_at': 1, 'source_kind': 'archive'},
                    'expected_version': 1})
                self.assertEqual(response.status_code, 200)
                after = client.get('/api/resources/skills/catalog-unique-fixture').json()
                self.assertEqual(before['created_at'], after['created_at'])
                self.assertEqual(after['source_kind'], 'manual')
                result = client.get('/api/resources/skills?q=catalog-unique-fixture&enabled=false').json()
                self.assertEqual(result['total'], 1)
                self.assertEqual(result['counts'], {'enabled': 0, 'disabled': 1})

    def test_delete_custom_and_builtin_with_version_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(cfg_at(Path(directory)), [])
            store = app.state.resources
            with TestClient(app, base_url='http://127.0.0.1') as client:
                client.put('/api/resources/skills/delete-fixture', json={'data': {'body': 'facts'}})
                self.assertEqual(client.delete('/api/resources/skills/delete-fixture?expected_version=0').status_code, 409)
                self.assertEqual(client.delete('/api/resources/skills/delete-fixture?expected_version=1').status_code, 200)
                self.assertEqual(client.get('/api/resources/skills/delete-fixture').status_code, 404)
                # 自定义包删除后可以重新导入；内置记录跨重启仍保持移除。
                store.import_skills([{'skill_id': 'delete-fixture', 'body': 'new facts'}])
                self.assertEqual(client.get('/api/resources/skills/delete-fixture').status_code, 200)
                key = next(iter(builtin_skills()))
                self.assertEqual(client.delete(f'/api/resources/skills/{key}?expected_version=0').status_code, 200)
                self.assertEqual(client.get(f'/api/resources/skills/{key}').status_code, 404)
                self.assertNotIn(key, [item['skill_id'] for item in ResourceStore(store.root).skills()])
                self.assertEqual(client.delete(f'/api/resources/skills/{key}?expected_version=1').status_code, 404)
                self.assertEqual(client.delete('/api/resources/skills/missing?expected_version=0').status_code, 404)
                self.assertEqual(client.delete('/api/resources/skills/missing').status_code, 422)
