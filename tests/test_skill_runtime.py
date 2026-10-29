"""技能渐进加载、版本固定与任务隔离回归。"""
from __future__ import annotations

import tempfile
import unittest

from spiritscout.resources.assistant import validate_skill
from spiritscout.resources.runtime import SkillRuntime
from spiritscout.resources.store import ResourceStore


class SkillRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ResourceStore(self.tmp.name)
        data = {'skill_id': 'example-skill', 'description': '用于验证隐式发现和参考读取',
                'domain': 'web', 'files': {'SKILL.md': '# 使用说明\n' + '重要参考事实。' * 1200,
                'references/deep/note.md': '# 完整参考\n敏感动作仍遵循任务审批。'}}
        validate_skill(data)
        self.store.save('skill', data['skill_id'], data, 0)
        self.runtime = SkillRuntime(self.store)

    def test_discovery_has_enabled_custom_metadata_without_body(self) -> None:
        index = self.runtime.discovery()
        self.assertIn('用于验证隐式发现', str(index))
        self.assertNotIn('重要参考事实', str(index))
        self.assertIn('web', index['builtin_domains'])
        self.assertIn('web-sqli', str(index))

    def test_open_read_repeat_and_independent_contexts(self) -> None:
        state = {}
        first = self.runtime.execute('skill_open', {'skill_id': 'example-skill'}, state)
        self.assertTrue(first['ok'])
        self.assertGreater(len(first['content']), 6000)
        self.assertIn('references/deep/note.md', first['files'])
        self.assertNotIn('敏感动作', first['content'])
        seen = {first['ref']}
        second = self.runtime.execute('skill_open', {'skill_id': 'example-skill'}, state, seen=seen)
        self.assertTrue(second['already_in_context'])
        self.assertNotIn('content', second)
        # 相同缓存快照不能证明另一段模型上下文已经见过内容。
        replay = self.runtime.execute('skill_open', {'skill_id': 'example-skill'}, state)
        self.assertEqual(first['content'], replay['content'])
        other = self.runtime.execute('skill_open', {'skill_id': 'example-skill'}, {})
        self.assertIn('content', other)
        read = self.runtime.execute('skill_read', {'skill_id': 'example-skill',
                                    'path': 'references/deep/note.md'}, state)
        self.assertIn('敏感动作', read['content'])

    def test_pin_persists_across_edit_but_disable_and_delete_block_new_reads(self) -> None:
        state = {}
        before = self.runtime.execute('skill_open', {'skill_id': 'example-skill'}, state)
        data = self.store.skills('example-skill', include_body=True)[0]
        data['files']['references/deep/note.md'] = '新版本'
        validate_skill(data)
        self.store.save('skill', 'example-skill', data, 1)
        read = self.runtime.execute('skill_read', {'skill_id': 'example-skill',
                                    'path': 'references/deep/note.md'}, state)
        self.assertEqual(read['version'], before['version'])
        self.assertIn('敏感动作', read['content'])
        fresh = self.runtime.execute('skill_read', {'skill_id': 'example-skill',
                                     'path': 'references/deep/note.md'}, {})
        self.assertEqual(fresh['content'], '新版本')
        self.store.save('skill', 'example-skill', {**data, 'enabled': False}, 2)
        self.assertFalse(self.runtime.execute('skill_open', {'skill_id': 'example-skill'}, state)['ok'])
        self.store.delete_skill('example-skill', 3)
        self.assertFalse(self.runtime.execute('skill_open', {'skill_id': 'example-skill'}, state)['ok'])

    def test_search_pagination_invalid_paths_and_legacy_root(self) -> None:
        result = self.runtime.execute('skill_search', {'query': 'web', 'limit': 1}, {})
        self.assertTrue(result['has_more'])
        self.assertNotIn('content', result['items'][0])
        state = {}
        for path in ('../../etc/passwd', '/etc/passwd', 'missing.md'):
            self.assertFalse(self.runtime.execute('skill_read', {'skill_id': 'example-skill', 'path': path}, state)['ok'])
        self.store.save('skill', 'legacy-reference', {'skill_id': 'legacy-reference', 'body': '# 原文'}, 0)
        self.assertEqual(self.runtime.execute('skill_open', {'skill_id': 'legacy-reference'}, {})['content'], '# 原文')

    def test_legacy_builtin_override_gets_scope_without_rewriting_saved_config(self) -> None:
        key = 'ad-adcs'
        self.store.save('skill', key, {'skill_id': key, 'description': '>',
                                     'body': '用户保留的正文', 'enabled': False}, 0)
        view = self.store.skills(key, include_body=True)
        item = next(row for row in view if row['skill_id'] == key)
        self.assertGreater(len(item['description']), 30)
        self.assertEqual(item['body'], '用户保留的正文')
        saved = next(row for row in self.store.settings('skill') if row['id'] == key)
        self.assertEqual(saved['data']['description'], '>')
        self.assertFalse(item['enabled'])

    def test_explicit_line_reads_and_reread_do_not_silently_truncate(self) -> None:
        state = {}
        first = self.runtime.execute('skill_read', {'skill_id': 'example-skill',
            'path': 'references/deep/note.md', 'start_line': 2, 'end_line': 2}, state)
        self.assertNotIn('# 完整参考', first['content'])
        self.assertEqual(first['total_lines'], 2)
        self.assertIn('敏感动作', first['content'])
        again = self.runtime.execute('skill_read', {'skill_id': 'example-skill',
            'path': 'references/deep/note.md', 'start_line': 2, 'end_line': 2, 'reread': True}, state, seen={first['ref']})
        self.assertEqual(again['content'], first['content'])
        invalid = self.runtime.execute('skill_open', {'skill_id': 'example-skill', 'reread': 'false'}, state)
        self.assertFalse(invalid['ok'])
