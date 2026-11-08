"""中英检索、别名、分级和大目录上下文边界。"""
import tempfile
import unittest
from unittest.mock import patch

from spiritscout.resources.assistant import validate_skill
from spiritscout.resources.catalog import skill_catalog
from spiritscout.resources.runtime import SkillRuntime
from spiritscout.resources.skill_search import rank
from spiritscout.resources.store import ResourceStore


class SkillSearchTests(unittest.TestCase):
    def test_chinese_question_finds_english_idor_and_exact_identifier_wins(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = SkillRuntime(ResourceStore(root))
            rows = runtime.execute('skill_search', {'query': '如何检查对象级越权'}, {})['items']
            self.assertIn(rows[0]['skill_id'], {'web-idor', 'api-bola'})
            self.assertIn('web-idor', [r['skill_id'] for r in rows[:3]])
            self.assertNotIn('body', rows[0])
            self.assertTrue(rows[0]['related_terms'])
            self.assertEqual(runtime.execute('skill_search', {'query': 'web-ssrf'}, {})['items'][0]['skill_id'], 'web-ssrf')

    def test_custom_alias_tier_filters_and_disabled_exclusion(self):
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(root)
            for key, enabled in [('active', True), ('disabled', False)]:
                store.save('skill', key, {'skill_id': key, 'body': 'private body', 'description': 'Local knowledge',
                    'aliases': ['内部单据'], 'tags': ['finance'], 'tier': 'reference', 'enabled': enabled}, 0)
            result = SkillRuntime(store).execute('skill_search', {'query': '内部单据', 'tier': 'reference'}, {})
            self.assertEqual([r['skill_id'] for r in result['items']], ['active'])
            self.assertTrue(result['items'][0]['matched_alias'])
            result = skill_catalog(store.skills(), q='内部单据', tier='reference', enabled=False, sort='relevance')
            self.assertEqual(result['items'][0]['skill_id'], 'disabled')
            self.assertEqual(result['counts'], {'enabled': 1, 'disabled': 1})

    def test_no_unrelated_fallback_and_stable_pagination(self):
        items = [{'skill_id': f'entry-{i}', 'description': 'Exact useful reference', 'domain': 'web'} for i in range(30)]
        self.assertEqual(rank(items, 'unrelated-zzzz'), [])
        self.assertEqual(rank(items, 'useful'), rank(list(reversed(items)), 'useful'))

    def test_catalog_is_bounded_and_changes_beyond_preview_are_detected(self):
        rows = [{'skill_id': f'custom-{i:05}', 'description': 'Reference', 'source_kind': 'manual',
                 'domain': 'web', 'version': 1, 'tier': 'domain'} for i in range(3000)]
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(root)
            with patch.object(store, 'skills', return_value=rows):
                before = SkillRuntime(store).discovery()
                self.assertEqual(before['total'], 3000)
                self.assertLess(len(str(before)), 12000)
                rows[-1]['version'] = 2
                after = SkillRuntime(store).discovery()
                self.assertNotEqual(before['revision'], after['revision'])

    def test_invalid_metadata_cannot_enter_import_or_editor(self):
        for metadata in ({'tier': 'approved'}, {'tier': []}, {'tier': {}}, {'aliases': 'not-array'}, {'tags': ['']}, {'tags': [1]}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                validate_skill({'skill_id': 'sample', 'body': 'facts', **metadata})
