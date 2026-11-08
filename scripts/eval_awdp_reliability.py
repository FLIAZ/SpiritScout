#!/usr/bin/env python3
"""以无外部目标的小型输入契约附件，多轮验证轻量编辑、打包、回归和结束状态。"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'

import eval_awdp_local as runner

MISSION = ('阅读附件 README 和实际源码，修复其中明确说明的输入契约缺口。'
    '这是本地维护与工具稳定性评测，不访问服务或外部目标，只使用普通小值测试。'
    '修法由你决定，保留约定内的正常行为；生成 patch.tar.gz，根目录含 patch.sh 和必要修复文件。'
    '用 verify_patch 验证最终包：functional 检查合法行为，security 检查 README 明确要求拒绝的普通值，'
    '修前边界断言失败、修后通过；测试调用真实应用。通过后 submit_fix 携带真实 verification_ref。'
    '部署根就是附件解压根目录，不需要重启服务。依赖只用现成 Python/Ruby/Node 标准库；'
    '断网，不安装任何依赖。没有缺口就说明依据并完成，不生成补丁。')

CASES = {
    'python_package': {'files': {
        'app.py': 'from limits.policy import check_count\ndef count(value):\n    return check_count(value)\n',
        'limits/__init__.py': '',
        'limits/policy.py': 'def check_count(value):\n    return value\n',
        'README.md': 'count(value) 只接受 1 到 10（含边界）的整数，返回原整数；不接受 bool。'
                     '其他输入抛 ValueError。当前缺少校验，请补齐；保留 count(1)、count(10) 的行为。\n'},
        'check': '''from app import count
assert count(1) == 1 and count(10) == 10
for value in (0, 11, True, "2"):
    rejected = False
    try: count(value)
    except ValueError: rejected = True
    assert rejected, repr(value)
''', 'needs_fix': True},
    'ruby_nested': {'attachment_format': 'tar.gz', 'files': {
        'lib/formatter.rb': 'module Formatter\r\n  def self.mode(value)\r\n    value\r\n  end\r\nend\r\n',
        'README.md': 'Ruby 模块 Formatter.mode(value) 接受字符串 fast 或 safe，并原样返回。'
                     '其他值（例如 other 或 nil）必须抛 ArgumentError，当前缺少此检查。'
                     '加载实际 lib/formatter.rb；只需 Ruby 标准库，无 Gemfile 或第三方依赖。\n'},
        'check': '''import subprocess
script = 'require "./lib/formatter"; raise unless Formatter.mode("fast") == "fast"; raise unless Formatter.mode("safe") == "safe"; ["other", nil].each { |v| rejected = false; begin; Formatter.mode(v); rescue ArgumentError; rejected = true; end; raise unless rejected }'
run = subprocess.run(["ruby", "-e", script], capture_output=True, text=True)
assert run.returncode == 0, run.stderr
''', 'needs_fix': True},
    'node_config': {'files': {
        'theme.js': "function theme(value) { return value; }\nmodule.exports = { theme };\n",
        'README.md': 'Node.js CommonJS 的 theme(value) 仅接受 light、dark 两个字符串，原样返回；'
                     '其他值（例如 blue、null）抛 Error。当前缺少检查，请修复。只用 Node 标准库。\n'},
        'check': '''import subprocess
script = 'const {theme} = require("./theme.js"); const assert = require("node:assert/strict"); assert.equal(theme("light"), "light"); assert.equal(theme("dark"), "dark"); for(const x of ["blue",null]) assert.throws(()=>theme(x));'
run = subprocess.run(["node", "-e", script], capture_output=True, text=True)
assert run.returncode == 0, run.stderr
''', 'needs_fix': True},
    'safe_control': {'files': {
        'settings.py': 'def mode(value):\n    if value not in ("fast", "safe"):\n        raise ValueError("unsupported mode")\n    return value\n',
        'README.md': 'mode(value) 只接受 fast 和 safe，返回原值。其他输入须抛 ValueError。'
                     '核对是否符合该契约；已正确时说明依据，不修改文件。\n'},
        'check': '', 'needs_fix': False},
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8081/v1')
    parser.add_argument('--model', default='qwen35-9b')
    parser.add_argument('--context', type=int, default=98304)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--case', choices=list(CASES))
    parser.add_argument('--deadline', type=int, default=600)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--keep-server', action='store_true')
    parser.add_argument('--run-local', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.rounds <= 10:
        parser.error('rounds 为 1–10 次独立实验，不是产品回合上限')
    names = [args.case] if args.case else list(CASES)
    fixtures = {}
    for round_id in range(1, args.rounds + 1):
        for name in names:
            item = copy.deepcopy(CASES[name])
            item['mission'] = MISSION
            item['files']['README.md'] += f'\n独立测试材料批次 {round_id}，正常语义不变。\n'
            fixtures[f'{name}_r{round_id}'] = item
    runner.CASES = fixtures
    args.case = None
    if args.run_local and args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(runner.main(args))
    if args.run_local:
        (args.output / 'fixtures.json').write_text(json.dumps(fixtures, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
