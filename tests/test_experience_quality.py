"""经验来源、相关召回与原有调用／缓存契约。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.blackboard import Blackboard
from spiritscout.core.models import BlackboardLayer
from spiritscout.gateway.context import estimate_input_tokens
from spiritscout.knowledge.experience import ExperienceStore, format_insights, read_page
from spiritscout.runtime import _distill_digest, _distill_experience, _recall_experience


def source(session='sess-source', state='observed'):
    return {'ref': 'topology:observation@1', 'source_session': session, 'evidence_state': state}


class ExperienceQualityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'experience.db'
        self.store = ExperienceStore(self.path)
        self.addCleanup(self.store.close)

    def test_candidates_and_unrelated_observations_do_not_enter_automatic_context(self):
        self.store.add('没有依据的登录推断', tags=['登录'])
        valid = self.store.add('登录错误提示可以区分账号存在性', context='会员登录', tags=['web', '登录'],
                               source_session='sess-source', sources=[source()])
        for i in range(8):
            self.store.add(f'整数溢出的场景 {i}', context='二进制程序', tags=['pwn'],
                           source_session='sess-source', sources=[source()])
        snapshot = self.store.recall('核对会员登录接口')
        self.assertEqual([item['insight_id'] for item in snapshot], [valid])
        self.assertEqual(self.store.recall('无关场景'), [])
        self.assertEqual(self.store.recall('进行安全测试'), [])
        lines = format_insights(snapshot)
        self.assertIn('历史观察，需核验', lines[0])
        self.assertLess(sum(map(len, lines)), sum(map(len, format_insights(self.store.recent(limit=6)))))
        old_context = '\n'.join(format_insights(self.store.recent(limit=6)))
        self.assertLess(estimate_input_tokens([{'role': 'user', 'content': '\n'.join(lines)}]),
                        estimate_input_tokens([{'role': 'user', 'content': old_context}]))
        qualified = 'a' * 230 + '仅限只读验证'
        self.assertIn(qualified, format_insights([{'insight': qualified, 'quality_status': 'referenced',
                                                 'source_session': 'sess-source'}])[0],
                      'quality labels must not displace the observation or its qualifying condition')
        self.store.add('新的二进制观察', tags=['pwn'], source_session='sess-source', sources=[source()])
        self.assertEqual(self.store.recall('核对会员登录接口'), snapshot, 'unrelated concurrent writes cannot reorder matching context')

    def test_duplicate_can_gain_sources_but_cannot_revive_excluded_knowledge(self):
        key = self.store.add('登录观察', tags=['登录'])
        self.assertEqual(self.store.recent()[0]['quality_status'], 'candidate')
        self.store.add('登录观察', source_session='sess-source', sources=[source()])
        self.assertEqual(self.store.recall('登录')[0]['insight_id'], key)
        self.store.add('登录观察', source_session='sess-other', sources=[source('sess-other')])
        self.assertEqual(self.store.recent()[0]['source_session'], 'sess-source')
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE insight_quality SET status='expired' WHERE insight_id=?", (key,))
        self.store.add('登录观察', source_session='sess-source', sources=[source()])
        self.assertEqual(self.store.recall('登录'), [])
        self.assertEqual(self.store.recent()[0]['quality_status'], 'expired')

    def test_conflicting_expired_or_wrong_session_sources_stay_candidates(self):
        for i, sources in enumerate(([source(state='expired')], [source(state='conflicting')], [source('another')])):
            self.store.add(f'登录观察 {i}', tags=['登录'], source_session='sess-source', sources=sources)
        self.assertEqual(self.store.recall('登录'), [])
        self.assertEqual({item['quality_status'] for item in self.store.recent()}, {'candidate'})

    def test_legacy_rows_remain_readable_without_becoming_verified(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('INSERT INTO insights VALUES(?,?,?,?,?,?,?)',
                         ('old', '登录的旧观察', '', '["登录"]', 'old-session', '', 1))
        item = next(item for item in read_page(self.path)['items'] if item['insight_id'] == 'old')
        self.assertEqual(item['quality_status'], 'legacy')
        self.assertEqual(self.store.recall('登录'), [])
        self.assertEqual(self.store.search('旧观察')[0]['insight_id'], 'old')

    def test_old_schema_read_and_upgrade_do_not_rewrite_or_remove_legacy_rows(self):
        path = Path(self.folder.name) / 'old.db'
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE insights (insight_id TEXT PRIMARY KEY, insight TEXT, context TEXT, '
                         'tags TEXT, source_session TEXT, source_model TEXT, ts REAL)')
            conn.execute('INSERT INTO insights VALUES(?,?,?,?,?,?,?)', ('old', '历史观察', '', '[]', '', '', 1))
        before = path.read_bytes()
        self.assertEqual(read_page(path)['items'][0]['quality_status'], 'legacy')
        self.assertEqual(path.read_bytes(), before, 'read-only inspection must not migrate a database')
        upgraded = ExperienceStore(path)
        self.addCleanup(upgraded.close)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('INSERT INTO insights VALUES(?,?,?,?,?,?,?)', ('old-writer', '旧进程仍可写入', '', '[]', '', '', 2))
        self.assertEqual(upgraded.count(), 2)
        self.assertEqual(upgraded.recall('历史观察'), [])

    def test_digest_catalog_contains_real_versioned_refs_but_no_credentials_or_answers(self):
        bb = Blackboard(Path(self.folder.name) / 'bb.db')
        self.addCleanup(bb.close)
        bb.write(BlackboardLayer.TOPOLOGY, 'observation', {'kind': 'finding', 'type': 'fact',
                 'text': '登录响应不同', 'evidence_state': 'observed'})
        bb.write(BlackboardLayer.TOPOLOGY, 'answer', {'kind': 'finding', 'type': 'flag', 'text': 'secret-answer'})
        bb.write(BlackboardLayer.CREDENTIAL, 'password', {'value': 'secret-password'})
        bb.write(BlackboardLayer.RECON_CLUE, 'reply', {'clue_type': 'user_reply', 'text': 'private-reply'})
        infra = SimpleNamespace(blackboard=bb, authorization=SimpleNamespace(
            mode=SimpleNamespace(value='ctf'), src_platform='', target_description='fixture'))
        digest = _distill_digest(infra, SimpleNamespace(), None, '登录', 'completed')
        self.assertEqual(digest['evidence_catalog'][0]['ref'], 'topology:observation@1')
        self.assertNotIn('secret-', json.dumps(digest))
        self.assertNotIn('private-reply', json.dumps(digest))

    def test_runtime_checks_source_version_without_granting_or_reusing_old_authorization(self):
        workspace = Path(self.folder.name) / 'sessions' / 'sess-current'
        workspace.mkdir(parents=True)
        old = workspace.parent / 'sess-source'
        old.mkdir()
        bb = Blackboard(old / 'bb.db')
        self.addCleanup(bb.close)
        bb.write(BlackboardLayer.TOPOLOGY, 'observation', {'kind': 'finding', 'type': 'fact',
                 'text': '登录观察', 'evidence_state': 'observed'})
        self.store.add('登录观察', tags=['登录'], source_session='sess-source', sources=[source()])
        infra = SimpleNamespace(cfg=SimpleNamespace(workspace=SimpleNamespace(root=str(workspace)),
                                experience=SimpleNamespace(recall_limit=6)), blackboard=None)
        self.assertEqual(len(_recall_experience(infra, self.store, '登录')), 1)
        bb.write(BlackboardLayer.TOPOLOGY, 'observation', {'kind': 'finding', 'type': 'fact',
                 'text': '原观察有冲突', 'evidence_state': 'conflicting'})
        self.assertEqual(_recall_experience(infra, self.store, '登录'), [])
        self.assertEqual(self.store.count(), 1, 'source changes must not delete the inspectable historical entry')


class DistillSourceContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_existing_call_checks_refs_and_preserves_system_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(Path(tmp) / 'experience.db')
            self.addCleanup(store.close)
            catalog = [{'ref': 'topology:observation@1', 'summary': '登录响应不同', 'evidence_state': 'observed'}]
            digest = {'outcome': 'completed', 'evidence_catalog': catalog, 'large_observation': '其他数据' * 5000}
            response = SimpleNamespace(content=json.dumps({'insights': [
                {'insight': '登录差异需要核验', 'tags': ['登录'], 'source_refs': ['topology:observation@1']},
                {'insight': '捏造来源的登录结论', 'tags': ['登录'], 'source_refs': ['topology:invented@99']}
            ]}))
            with patch('spiritscout.gateway.gateway.chat_decision', AsyncMock(return_value=response)) as call:
                await _distill_experience(object(), store, digest, timeout_seconds=10, source_session='sess-source')
            call.assert_awaited_once()
            messages = call.call_args.args[1]
            self.assertEqual(hashlib.sha256(messages[0]['content'].encode()).hexdigest(),
                             'b5d58d77e59d6d6002e67bcea035f3018934ee3b98adddbf92781261ff7fc616')
            self.assertLessEqual(len(messages[1]['content']), 8000)
            visible = json.loads(messages[1]['content'])
            self.assertIn('topology:observation@1', str(visible['evidence_catalog']))
            self.assertEqual(len(store.recall('登录')), 1)
            self.assertEqual(store.recall('登录')[0]['insight'], '登录差异需要核验')
            self.assertEqual(set(store.recall('登录')[0]['sources'][0]), {'ref', 'source_session', 'evidence_state'})
            self.assertEqual(store.count(), 2, 'unreferenced entries remain inspectable candidates')


if __name__ == '__main__':
    unittest.main()
