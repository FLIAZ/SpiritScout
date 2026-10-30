"""技能目录原文保留、正文投影及浏览接口契约。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from test_resource_management import cfg_at
from test_skill_archives import SKILL, zipped

from spiritscout.resources.archives import parse_skill_archive
from spiritscout.resources.assistant import validate_skill
from spiritscout.web.app import create_app


class SkillFilesTests(unittest.TestCase):
    def test_archive_preserves_project_paths_and_original_markdown(self) -> None:
        items, _ = parse_skill_archive(zipped({
            'wrapper/project/SKILL.md': SKILL,
            'wrapper/project/references/nested/details.md': b'# Details\n**Evidence**',
            'wrapper/project/scripts/demo.py': b'print("reference only")',
        }))
        self.assertEqual(items[0]['files']['SKILL.md'], SKILL.decode())
        self.assertEqual(items[0]['files']['references/nested/details.md'], '# Details\n**Evidence**')
        self.assertIn('Evidence', items[0]['body'])

    def test_project_file_validation(self) -> None:
        for files in ({}, {'SKILL.md': ''}, {'SKILL.md': 'x', '../x': 'bad'},
                      {'SKILL.md': 'x', '/abs': 'bad'}, {'SKILL.md': 'x', 'a/./b': 'bad'},
                      {'SKILL.md': 'x', 'a': 'file', 'a/b': 'conflict'},
                      {'SKILL.md': 'x', 'a': 123}, {'SKILL.md': 'x', 'a': 'x'*100001}):
            with self.subTest(files=list(files)), self.assertRaises(ValueError):
                validate_skill({'skill_id': 'project', 'files': files, 'body': 'old'})

    def test_files_persist_and_edits_update_reference_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(cfg_at(Path(directory)), [])
            with TestClient(app, base_url='http://127.0.0.1') as client:
                response = client.post('/api/resources/skills/import', content=zipped({
                    'skill/SKILL.md': SKILL, 'skill/references/notes.md': b'# Original'}))
                self.assertEqual(response.status_code, 200)
                item = client.get('/api/resources/skills/sample-skill').json()
                self.assertIn('references/notes.md', item['files'])
                self.assertNotIn('files', client.get('/api/resources/skills?q=sample-skill').json()['items'][0])
                response = client.put('/api/resources/skills/sample-skill', json={
                    'data': {'files': {**item['files'], 'references/notes.md': '# Updated'}},
                    'expected_version': 1})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('# Updated', client.get('/api/resources/skills/sample-skill').json()['body'])
                self.assertEqual(client.put('/api/resources/skills/sample-skill', json={
                    'data': {'files': {'SKILL.md': 'root', '../../bad': 'bad'}},
                    'expected_version': 2}).status_code, 422)
