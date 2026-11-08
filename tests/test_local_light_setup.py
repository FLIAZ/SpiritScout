"""本地双后端共用轻量工作区与跨平台初始化。"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from spiritscout.config.settings import SystemConfig, load_model_config
from spiritscout.core.models import RunMode
from spiritscout.gateway.lightweight import lightweight_enabled

spec = importlib.util.spec_from_file_location(
    "setup_local_light", Path(__file__).resolve().parents[1] / "scripts/setup_local_light.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class LocalSetupTests(unittest.TestCase):
    def test_native_backends_default_to_the_same_offline_profile(self):
        systems = []
        for backend in ('ollama', 'llama_cpp'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as name:
                result = subprocess.run(
                    [sys.executable, spec.origin, '--backend', backend, '--model', 'fixture'],
                    cwd=name, capture_output=True, text=True, check=False, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('./launch.sh start --offline', result.stdout)
                root = Path(name)
                profile = root / '.spiritscout-ollama'
                system = json.loads((profile / 'system.yaml').read_text())
                systems.append(system)
                self.assertEqual(system['workspace']['root'], '.spiritscout-ollama')
                self.assertEqual(system['models_config_path'], '.spiritscout-ollama/models.yaml')
                self.assertFalse((root / '.spiritscout-llama').exists())
        self.assertEqual(*systems)

    def test_reinitializing_with_another_backend_preserves_configuration_and_history(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            command = [sys.executable, spec.origin, '--model', 'fixture', '--backend']
            subprocess.run([*command, 'ollama'], cwd=root, capture_output=True,
                           check=True, timeout=10)
            profile = root / '.spiritscout-ollama'
            session = profile / 'sessions/fixture/meta.json'
            session.parent.mkdir(parents=True)
            session.write_text('{"status":"completed"}')
            before = {path: path.read_bytes() for path in profile.rglob('*') if path.is_file()}
            result = subprocess.run([*command, 'llama_cpp'], cwd=root, capture_output=True,
                                    text=True, check=False, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn('保留 system.yaml 和会话工作区', result.stderr)
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            self.assertFalse((root / '.spiritscout-llama').exists())

    def test_both_backends_load_and_enable_light_runtime(self):
        for backend in ("ollama", "llama_cpp"):
            for docker in (False, True):
                with self.subTest(backend=backend, docker=docker), tempfile.TemporaryDirectory() as name:
                    output = Path(name) / "configuration"
                    files = setup.configuration(backend, "qwen3.5:9b",
                                                "http://host.docker.internal:8081/v1",
                                                16384, output, docker)
                    setup.write_configuration(output, files)
                    config = SystemConfig.model_validate(files["system.yaml"])
                    models = load_model_config(output / "models.yaml")
                    self.assertTrue(lightweight_enabled(config.gateway.lightweight_local, RunMode.CTF, models))
                    self.assertEqual(models[0].context_window, 16384)
                    self.assertEqual(config.web.max_concurrent_sessions, 1)
                    self.assertEqual(models[0].base_url.endswith("/v1"), backend == "llama_cpp")
                    if docker:
                        self.assertEqual(config.agent_models_path, ".spiritscout-light/agent_models.yaml")

    def test_existing_configuration_is_not_partially_overwritten(self):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name)
            model_path = output / "models.yaml"
            model_path.write_text('{"existing":true}')
            with self.assertRaises(FileExistsError):
                setup.write_configuration(output, {"system.yaml": {}, "models.yaml": {}})
            self.assertEqual(json.loads(model_path.read_text()), {"existing": True})
            self.assertFalse((output / "system.yaml").exists())

    def test_invalid_endpoint_and_small_context_rejected(self):
        for url, context in (("file:///etc/passwd", 16384), ("http://user:pass@host", 16384),
                             ("http://localhost:8081", 4096)):
            with self.assertRaises(ValueError):
                setup.configuration("llama_cpp", "qwen", url, context, Path("unused"))
