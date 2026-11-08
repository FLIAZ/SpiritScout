"""在隔离评测进程加载可信本机代码快照，不覆盖工作树或真实模型配置。"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


class PreviousNormalProfile:
    def __init__(self, root: Path):
        self.fingerprints = {}
        def load(relative: str, name: str):
            path = root / relative
            self.fingerprints[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ValueError(f'无法读取评测快照：{relative}')
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module

        self.memory = load('src/spiritscout/ctf/evidence_memory.py', 'spiritscout.ctf._eval_previous_memory')
        with patch.dict(sys.modules, {'spiritscout.ctf.evidence_memory': self.memory}):
            self.context = load('src/spiritscout/gateway/normal_context.py', 'spiritscout.gateway._eval_previous_normal')
            self.agents = load('src/spiritscout/ctf/agents.py', 'spiritscout.ctf._eval_previous_agents')

    def activate(self):
        return patch.dict(sys.modules, {'spiritscout.ctf.evidence_memory': self.memory,
                                       'spiritscout.gateway.normal_context': self.context})
