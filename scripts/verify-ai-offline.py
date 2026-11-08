#!/usr/bin/env python3
"""真实沙箱内验证离线 AI 依赖和微型数值链路；不下载或调用 LLM。"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile

from spiritscout.core.models import RunMode
from spiritscout.tools.sandbox import sandbox_exec

PROBE = r'''
import os
for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[name] = '1'
import json, hashlib
from pathlib import Path
import numpy as np
import torch
import torch.nn.functional as F
torch.set_num_threads(1)
torch.manual_seed(0)
device = __DEVICE__
assert device == 'cpu' or torch.cuda.is_available(), 'CUDA 在实际沙箱中不可用'
assert torch.__version__.split('+')[0] == '2.15.0', torch.__version__
assert np.__version__ == '2.5.4', np.__version__
model = torch.nn.Linear(2, 2).to(device)
with torch.no_grad():
    model.weight.copy_(torch.tensor([[1., 0.], [-1., 0.]]))
    model.bias.copy_(torch.tensor([-.5, .5]))
model.eval()
torch.save(model.state_dict(), 'tiny.pt')
loaded = torch.nn.Linear(2, 2).to(device)
loaded.load_state_dict(torch.load('tiny.pt', map_location=device, weights_only=True))
loaded.eval()
original = torch.tensor([[.6, .4]], dtype=torch.float32, device=device)
x = original.clone().requires_grad_(True)
loss = F.cross_entropy(loaded(x), torch.tensor([1], device=device))
gradient, = torch.autograd.grad(loss, x)
candidate = (x - .2 * gradient.sign()).detach().clamp(0, 1)
np.save('original.npy', original.cpu().numpy())
np.save('candidate.npy', candidate.cpu().numpy())
# 验证端重新加载最终文件；不沿用攻击端张量或标签声明。
a = np.load('candidate.npy', allow_pickle=False)
b = np.load('original.npy', allow_pickle=False)
assert a.shape == b.shape == (1, 2) and a.dtype == b.dtype == np.float32
assert np.isfinite(a).all() and a.min() >= 0 and a.max() <= 1
linf = float(np.abs(a.astype(np.float64) - b.astype(np.float64)).max())
assert linf <= .200001
with torch.inference_mode():
    before = int(loaded(torch.from_numpy(b).to(device)).argmax(1).item())
    after = int(loaded(torch.from_numpy(a).to(device)).argmax(1).item())
assert before == 0 and after == 1
print(json.dumps({'ok':True, 'torch':torch.__version__, 'numpy':np.__version__,
    'device':device, 'built_cuda':torch.version.cuda,
    'gpu_name':torch.cuda.get_device_name(0) if device == 'cuda' else None,
    'tiny_checkpoint_reload':True, 'numpy_bridge':True,
    'original_class':before, 'saved_candidate_class':after, 'linf':linf,
    'sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ('tiny.pt','original.npy','candidate.npy')},
    'actual_llm_call':False, 'qualification':'微型自建分类器与工具链验证，不是比赛解题率或小模型能力评测'}, ensure_ascii=False))
'''


async def main(device, lightweight):
    with tempfile.TemporaryDirectory(prefix='spiritscout-ai-probe-') as root:
        result = await sandbox_exec(PROBE.replace('__DEVICE__', repr(device)), language='python', timeout=60, workspace_root=root,
            workspace_scope='offline-ai', _run_mode=RunMode.CTF, _lightweight_local=lightweight)
        if not result.ok or result.returncode:
            print(json.dumps({'ok': False, 'error': result.error or result.stderr,
                              'stdout': result.stdout}, ensure_ascii=False))
            return 1
        proof = json.loads(result.stdout)
        proof['execution_mode'] = 'lightweight' if lightweight else 'normal'
        print(json.dumps(proof, ensure_ascii=False, indent=2))
        return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    parser.add_argument('--mode', choices=('normal', 'lightweight'), default='lightweight')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.device, args.mode == 'lightweight')))
