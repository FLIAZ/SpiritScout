"""单文件编辑的原文完整性、版本冲突及 Agent 新旧快照。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from test_resource_management import cfg_at
from test_skill_archives import zipped

from spiritscout.resources.runtime import SkillRuntime
from spiritscout.web.app import create_app


class SkillFileEditTests(unittest.TestCase):
    def test_edit_only_one_file_preserves_metadata_and_updates_agent_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(cfg_at(Path(directory)), [])
            with TestClient(app, base_url='http://127.0.0.1') as client:
                root = '---\nname: editable\ndescription: 原始描述\n---\n# 原标题\n'
                client.post('/api/resources/skills/import', content=zipped({
                    'editable/SKILL.md': root.encode(), 'editable/references/rules.md': b'# Before',
                    'editable/scripts/demo.py': b'print("keep")'}))
                before = client.get('/api/resources/skills/editable').json()
                runtime = SkillRuntime(app.state.resources)
                pinned = {}
                runtime.execute('skill_open', {'skill_id': 'editable'}, pinned)
                response = client.put('/api/resources/skills/editable/file', json={
                    'path': 'references/rules.md', 'content': '# After\n**保留格式**',
                    'expected_version': 1})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['version'], 2)
                after = client.get('/api/resources/skills/editable').json()
                self.assertEqual(after['files']['SKILL.md'], root)
                self.assertEqual(after['files']['scripts/demo.py'], 'print("keep")')
                self.assertEqual(after['description'], before['description'])
                self.assertEqual(after['created_at'], before['created_at'])
                self.assertIn('# After', after['body'])
                old = runtime.execute('skill_read', {'skill_id': 'editable', 'path': 'references/rules.md'}, pinned)
                fresh = runtime.execute('skill_read', {'skill_id': 'editable', 'path': 'references/rules.md'}, {})
                self.assertEqual(old['content'], '# Before')
                self.assertIn('# After', fresh['content'])

    def test_conflicts_invalid_paths_and_deleted_skill_never_recreate_or_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(cfg_at(Path(directory)), [])
            with TestClient(app, base_url='http://127.0.0.1') as client:
                client.put('/api/resources/skills/legacy-edit', json={'data': {'body': '# Original'}})
                endpoint = '/api/resources/skills/legacy-edit/file'
                edit = {'path': 'SKILL.md', 'content': '# Changed', 'expected_version': 1}
                self.assertEqual(client.put(endpoint, json=edit).status_code, 200)
                self.assertEqual(client.put(endpoint, json={**edit, 'content': '# Stale'}).status_code, 409)
                self.assertEqual(client.get('/api/resources/skills/legacy-edit').json()['body'], '# Changed')
                for path in ('../SKILL.md', '/etc/passwd', 'new.md', 'SKILL.md/child'):
                    self.assertEqual(client.put(endpoint, json={**edit, 'path': path, 'expected_version': 2}).status_code, 404)
                self.assertEqual(client.put(endpoint, json={**edit, 'content': '', 'expected_version': 2}).status_code, 422)
                client.delete('/api/resources/skills/legacy-edit?expected_version=2')
                self.assertEqual(client.put(endpoint, json={**edit, 'expected_version': 0}).status_code, 404)
                self.assertEqual(client.get('/api/resources/skills/legacy-edit').status_code, 404)

    def test_builtin_edit_is_a_local_override(self) -> None:
        from spiritscout.resources.store import builtin_skills
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(cfg_at(Path(directory)), [])
            with TestClient(app, base_url='http://127.0.0.1') as client:
                original = builtin_skills()['ad-adcs']['body']
                response = client.put('/api/resources/skills/ad-adcs/file', json={
                    'path': 'SKILL.md', 'content': '# 本地修改', 'expected_version': 0})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(client.get('/api/resources/skills/ad-adcs').json()['files']['SKILL.md'], '# 本地修改')
                self.assertEqual(builtin_skills()['ad-adcs']['body'], original)
