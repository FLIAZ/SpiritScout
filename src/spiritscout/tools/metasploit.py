"""Metasploit 检查适配：固定资源脚本，数据用 JSON 传递，沿用审批与沙箱。"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import uuid
from pathlib import Path

from spiritscout.core.models import AgentType, BlackboardLayer
from spiritscout.tools.sandbox import sandbox_exec
from spiritscout.tools.sandbox_paths import open_directory
from spiritscout.tools.scan_evidence import write_artifact
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec

SPEC = ToolSpec(name="metasploit_scan", kind=ToolKind.BUILTIN, risk=RiskLevel.INTRUSIVE,
    description="运行选定 Metasploit 模块的 check，或 auxiliary 模块的 run；不是自动漏洞确认或 exploit。",
    action_required=True, automatic_replay=False,
    allowed_params={"module", "options", "operation", "action"})
SCHEMA = {"type": "function", "function": {"name": SPEC.name, "description": SPEC.description,
    "parameters": {"type": "object", "properties": {"module": {"type": "string"},
        "options": {"type": "object", "additionalProperties": {"type": "string"}},
        "operation": {"type": "string", "enum": ["info", "check", "run"]},
        "action": {"type": "string", "description": "如实声明本次语义动作，SRC 危险动作即时审批"}},
        "required": ["module", "options", "action"]}}}


def resource_script(module: str, options: dict, operation: str, output: str) -> str:
    payload = base64.b64encode(json.dumps({"module": module, "options": options,
        "operation": operation, "output": output}).encode()).decode()
    return """<ruby>
require 'json'
require 'base64'
spec = JSON.parse(Base64.strict_decode64('%s'))
record = {'module' => spec['module'], 'operation' => spec['operation'], 'options' => spec['options'], 'ok' => false}
begin
  mod = framework.modules.create(spec['module'])
  raise '模块不存在或无法加载' unless mod
  record['name'] = mod.name
  record['references'] = mod.references.map { |ref| [ref.ctx_id, ref.ctx_val] }
  record['description'] = mod.description
  record['option_schema'] = mod.options.map { |key, opt| {'name' => key, 'required' => opt.required, 'description' => opt.desc, 'default' => opt.default} }
  record['framework_version'] = Msf::Framework::Version
  spec['options'].each { |key, value| mod.datastore[key] = value }
  mod.datastore['HttpTrace'] = true
  mod.datastore['HttpTraceHeadersOnly'] = false
  mod.datastore['HttpTraceColors'] = ''
  mod.init_ui(input, output)
  if spec['operation'] == 'info'
    record['info_only'] = true
  elsif spec['operation'] == 'check'
    result = mod.check
    record['check_result'] = result.to_s
  else
    mod.run_simple('LocalInput' => input, 'LocalOutput' => output, 'RunAsJob' => false)
    record['run_result'] = '模块执行返回，是否存在漏洞仍需按原始输出判断'
  end
  record['ok'] = true
rescue StandardError => error
  record['error'] = "#{error.class}: #{error.message}"
ensure
  File.binwrite(spec['output'], JSON.generate(record))
end
</ruby>
exit -y
""" % payload


class MetasploitScanner:
    def __init__(self, config, blackboard):
        self.config, self.blackboard = config, blackboard
        self.slot = asyncio.Semaphore(1)

    async def preflight(self, **ctx):
        if not self.config.metasploit_enabled or not self.config.metasploit_command:
            return ToolResult(tool=SPEC.name, ok=False, error="Metasploit 未启用")
        return await sandbox_exec("command -v " + shlex.quote(self.config.metasploit_command[0]), timeout=5, **ctx)

    async def run(self, module, options, operation="check", action="", **ctx):
        control = ctx.pop('_scan_control', None)
        if control is not None:
            return await control.run_tool(SPEC.name, lambda: self.run(module, options, operation, action, **ctx))
        if (not isinstance(module, str) or not re.fullmatch(r'(?:auxiliary|exploit)/[a-zA-Z0-9_/]+', module)
                or any(part in ("", ".", "..") for part in module.split('/'))):
            return ToolResult(tool=SPEC.name, ok=False, error="需要真实 auxiliary/exploit 模块路径")
        if operation not in {"info", "check", "run"} or (operation == "run" and not module.startswith("auxiliary/")):
            return ToolResult(tool=SPEC.name, ok=False, error="仅支持 check 或 auxiliary 模块 run，不执行 exploit")
        if (not isinstance(options, dict) or len(options) > 100
                or any(not isinstance(k, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]*', k)
                       or not isinstance(v, str) or len(v) > 4096 or any(c in v for c in '\r\n\x00')
                       for k, v in options.items())):
            return ToolResult(tool=SPEC.name, ok=False, error="options 必须是合法选项名到单行字符串值的对象")
        if not self.config.metasploit_enabled or not self.config.metasploit_command:
            return ToolResult(tool=SPEC.name, ok=False, error="Metasploit 未启用")
        async with self.slot:
            return await self._run(module, options, operation, ctx)

    async def _run(self, module, options, operation, ctx):
        root = Path(ctx['workspace_root']).absolute()
        scan_id = 'msf-' + uuid.uuid4().hex
        relative = Path('artifacts') / 'scans' / scan_id
        with open_directory(root, relative, create=True) as directory:
            script = resource_script(module, options, operation, 'result.json')
            write_artifact(directory, 'reproduce.rc', script)
            command = [*self.config.metasploit_command, '-q', '-n', '-r', str(root / relative / 'reproduce.rc')]
            cancelled = False
            try:
                result = await sandbox_exec('cd ' + shlex.quote(str(root / relative)) + ' && ' + shlex.join(command)
                    + ' > ' + shlex.quote(str(root / relative / 'execution.log')) + ' 2>&1',
                    timeout=self.config.timeout_seconds, **ctx)
            except asyncio.CancelledError:
                cancelled = True
                result = ToolResult(tool=SPEC.name, ok=False, returncode=-1, error='模块执行已取消')
            data = {}
            try:
                fd = os.open('result.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, 'rb') as source:
                    data = json.loads(source.read(1024 * 1024))
            except (OSError, ValueError):
                data = {'ok': False, 'error': '模块没有提供完整执行回执，请核对日志'}
            files = {key: (relative / name).as_posix() for key, name in
                     (('poc', 'reproduce.rc'), ('execution_log', 'execution.log'))}
            if (root / relative / 'result.json').is_file():
                files['result'] = (relative / 'result.json').as_posix()
            references = data.get('references') or []
            identifiers = [f'{pair[0]}-{pair[1]}' for pair in references
                if isinstance(pair, list) and len(pair) == 2 and pair[0] in {'CVE', 'CWE'}]
            evidence = {'files': files, 'vulnerability_ids': identifiers,
                        'missing': ['request', 'response'], 'poc_source': 'executed_metasploit_resource',
                        'http_trace': '若模块支持 HttpTrace，请求/响应原文在执行日志中',
                        'independently_verified': False}
            console_output = ''
            try:
                fd = os.open('execution.log', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, 'rb') as log:
                    console_output = log.read(16000).decode(errors='replace')
            except OSError:
                pass
            finding_id = 'scan-finding-' + scan_id
            observation = {'finding_id': finding_id, 'clue_type': 'scanner_finding', 'scanner': 'metasploit',
                'module': module, 'name': data.get('name', module), 'severity': 'info',
                'value': options.get('RHOSTS') or options.get('RHOST') or '',
                'scan_evidence': evidence, 'verification_status': 'unverified', 'module_result': data,
                'execution_status': 'cancelled' if cancelled else 'completed' if data.get('ok') else 'failed'}
            if operation != 'info':
                self.blackboard.write(BlackboardLayer.RECON_CLUE, finding_id, observation,
                                      agent_type=AgentType.SCANNER, updated_by='scanner')
            result.tool = SPEC.name
            result.ok = bool(result.ok and result.returncode == 0 and data.get('ok'))
            result.parsed = {'preview': [observation], 'module_result': data, 'scan_evidence': evidence,
                             'console_output': console_output, 'evidence_files': files,
                             'evidence_note': '实际执行的 Metasploit 资源脚本和日志；HTTP 模块若提供 HttpTrace，原请求/响应保留在日志中。模块返回不代表漏洞确认。'}
            result.stdout = json.dumps(result.parsed, ensure_ascii=False)
            result.error = '' if result.ok else data.get('error') or result.error or '模块执行未成功'
            if cancelled:
                raise asyncio.CancelledError
            return result
