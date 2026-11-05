"""冻结构建首次启动的配置生成回归；不启动真实服务。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spiritscout import __main__ as entry


class PortableRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix='spiritscout portable ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'
        self.app = self.root / 'app'
        self.app.mkdir()
        configs = self.bundle / 'configs'
        configs.mkdir(parents=True)
        (configs / 'system.example.yaml').write_text('mode: src\n', encoding='utf-8')
        (configs / 'models.example.yaml').write_text(
            'providers: []\nmodels: []\n', encoding='utf-8')
        (self.bundle / '.env.example').write_text('A=1\n', encoding='utf-8')
        self.original_cwd = Path.cwd()

    def tearDown(self) -> None:
        os.chdir(self.original_cwd)

    def _frozen(self):
        return mock.patch.multiple(
            sys, create=True, frozen=True,
            executable=str(self.app / 'SpiritScout.exe'),
            _MEIPASS=str(self.bundle))

    def test_first_run_creates_editable_configs_next_to_executable(self) -> None:
        with self._frozen():
            entry._prepare_portable_runtime()
        self.assertEqual(Path.cwd(), self.app)
        configs = self.app / 'configs'
        self.assertEqual((configs / 'system.yaml').read_text(encoding='utf-8'),
                         'mode: src\n')
        self.assertIn('models: []', (configs / 'models.yaml').read_text(encoding='utf-8'))
        self.assertTrue((configs / 'system.example.yaml').is_file())
        self.assertTrue((self.app / '.env.example').is_file())

    def test_existing_config_and_env_are_preserved(self) -> None:
        configs = self.app / 'configs'
        configs.mkdir()
        (configs / 'system.yaml').write_text('mode: ctf\n', encoding='utf-8')
        (self.app / '.env.example').write_text('B=2\n', encoding='utf-8')
        with self._frozen():
            entry._prepare_portable_runtime()
        self.assertEqual((configs / 'system.yaml').read_text(encoding='utf-8'),
                         'mode: ctf\n')
        self.assertEqual((self.app / '.env.example').read_text(encoding='utf-8'),
                         'B=2\n')

    def test_source_run_does_not_create_files(self) -> None:
        entry._prepare_portable_runtime()
        self.assertFalse((self.app / 'configs').exists())


class DefaultModelConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix='spiritscout model config ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self) -> None:
        os.chdir(self.original_cwd)

    def test_missing_default_registry_is_created(self) -> None:
        entry._ensure_default_model_config('configs/models.yaml')
        data = (self.root / 'configs/models.yaml').read_text(encoding='utf-8')
        self.assertEqual(data, 'providers: []\nmodels: []\n')

    def test_custom_path_is_not_created(self) -> None:
        entry._ensure_default_model_config('custom/models.yaml')
        self.assertFalse((self.root / 'custom/models.yaml').exists())


if __name__ == '__main__':
    unittest.main()
