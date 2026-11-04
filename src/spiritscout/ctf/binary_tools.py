"""轻量单执行者的结构化二进制静态读取；工具解析在现有沙箱内完成。"""
from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path
from typing import Any

from spiritscout.tools.sandbox_paths import sandbox_directory

SCHEMA = {"type": "function", "function": {
    "name": "binary_inspect",
    "description": "静态读取附件二进制的信息、函数、字符串、交叉引用或函数反汇编。不运行附件。结果分页；has_more 表示还有未读记录，query/offset 可缩小查询或续读，不重复第一页。disasm/xrefs 的地址取自实际函数/引用数据，不用文件名代替函数名。",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "附件绝对路径，或当前题目工作目录内的文件路径"},
        "operation": {"type": "string", "enum": ["info", "functions", "strings", "xrefs", "disasm"]},
        "address": {"type": "string", "description": "xrefs/disasm 的真实地址，十六进制 0x... 或十进制"},
        "query": {"type": "string", "description": "可选名称/内容检索词，不是固定考点清单"},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "description": "期望页大小；实际按展示预算返回，使用 next_offset 续读"}},
        "required": ["path", "operation"]}}}

_SCRIPT = r'''
import json, subprocess, sys
path, operation, address, query, offset, limit = json.loads(sys.argv[1])
try:
    commands = {'info': 'ij', 'functions': 'aflj', 'strings': 'izj',
                'xrefs': 'axtj @ ' + str(address), 'disasm': 'pdfj @ ' + str(address)}
    args = ['r2', '-q', '-e', 'scr.color=0', '-e', 'bin.relocs.apply=true']
    if operation in {'functions', 'xrefs', 'disasm'}:
        args.append('-A')
    args += ['-c', commands[operation], path]
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
    if len(result.stdout) > 16 * 1024 * 1024:
        raise ValueError('原始工具输出超过 16 MiB，不能完整解析')
    if result.returncode:
        raise ValueError('r2 exit=' + str(result.returncode) + ': ' + result.stderr.decode(errors='replace')[-500:])
    data = json.loads(result.stdout)
    if operation == 'info':
        keys = ('arch','bits','baddr','binsz','class','lang','os','machine','endian','stripped','subsys','nx','canary')
        values = {key: data.get('bin', {}).get(key) for key in keys}
        print(json.dumps({'ok': True, 'operation': operation, 'info': values}))
    else:
        function = None
        if operation == 'disasm':
            function = {key: data.get(key) for key in ('addr', 'name', 'size')}
            data = data.get('ops', [])
            if not data:
                raise ValueError('该地址未得到函数反汇编，请核对真实地址与已识别函数信息')
            keys = ('offset','disasm','jump','fail','ptr','comment')
        elif operation == 'functions':
            keys = ('offset','name','size','ninstr','nargs')
        elif operation == 'strings':
            keys = ('vaddr','paddr','size','length','string')
        else:
            keys = ('from','type','opcode','fcn_addr','fcn_name','refname')
        if not isinstance(data, list):
            raise ValueError('工具没有返回预期的结构化列表')
        if query:
            data = [item for item in data if query.lower() in json.dumps(item, ensure_ascii=False).lower()]
        page = []
        for item in data[offset:offset + limit]:
            entry = {key: item[key] for key in keys if key in item}
            if operation == 'disasm' and 'disasm' not in entry:
                entry['disasm'] = item.get('opcode', '')
            if isinstance(entry.get('string'), str) and len(entry['string']) > 500:
                entry['string'] = entry['string'][:500]
                entry['partial'] = True
            page.append(entry)
        response = {'ok': True, 'operation': operation, 'items': page, 'total': len(data),
                    'offset': offset, 'next_offset': offset + len(page),
                    'has_more': offset + len(page) < len(data), 'function': function}
        while len(json.dumps(response).encode()) > 3200 and len(page) > 1:
            page.pop()
            response['next_offset'] = offset + len(page)
            response['has_more'] = offset + len(page) < len(data)
        if len(json.dumps(response).encode()) > 3200:
            raise ValueError('单条记录超过展示窗口；使用更窄的查询或查看原始文件')
        print(json.dumps(response))
except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
    print(json.dumps({'ok': False, 'operation': operation, 'error': str(error)}))
'''


async def inspect(agent, task, route, arguments: dict[str, Any]):
    from spiritscout.ctf.agents import _ToolOutcome

    operation = arguments.get("operation")
    if operation not in {"info", "functions", "strings", "xrefs", "disasm"}:
        return _ToolOutcome(ok=False, text="operation 必须为 info/functions/strings/xrefs/disasm")
    raw = arguments.get("path")
    if not isinstance(raw, str) or not raw:
        return _ToolOutcome(ok=False, text="需要真实文件 path")
    workspace = (getattr(agent.ctx, "builtin_ctx", None) or {}).get("workspace_root")
    if not workspace:
        return _ToolOutcome(ok=False, text="缺少当前会话工作区，不能读取附件")
    root = Path(workspace).resolve()
    working = root / sandbox_directory(agent.challenge_id(task))
    path = Path(raw) if Path(raw).is_absolute() else working / raw
    path = path.resolve()
    if not any(path.is_relative_to(base) for base in (root / "attachments", root / "artifacts", working)):
        return _ToolOutcome(ok=False, text="文件不在本题附件、工作目录或当前会话 artifacts 中")
    if not path.is_file():
        return _ToolOutcome(ok=False, text="文件不存在，请使用真实附件路径或工具返回的 workdir")
    offset, requested_limit = arguments.get("offset", 0), arguments.get("limit", 16)
    if type(offset) is not int or offset < 0 or type(requested_limit) is not int or requested_limit < 1:
        return _ToolOutcome(ok=False, text="offset 为非负整数，limit 为正整数")
    limit = min(requested_limit, 64)
    address = None
    if operation in {"xrefs", "disasm"}:
        try:
            value = arguments.get("address")
            if isinstance(value, bool):
                raise ValueError
            address = int(value, 0) if isinstance(value, str) else int(value)
            if not 0 <= address < 2**64:
                raise ValueError
        except (TypeError, ValueError):
            return _ToolOutcome(ok=False, text="需要实际地址 address，支持十六进制或十进制")
    query = arguments.get("query", "")
    if not isinstance(query, str) or len(query) > 200:
        return _ToolOutcome(ok=False, text="query 为不超过 200 字符的检索词")
    payload = json.dumps([str(path), operation, address, query, offset, limit], ensure_ascii=False)
    command = "python3 -I -c " + shlex.quote(_SCRIPT) + " " + shlex.quote(payload)
    result = await agent.ctx.tools.call("sandbox_exec", {"code": command, "language": "shell",
        "action": "analysis", "timeout": 50, "log_name": "binary_inspect"}, **agent._tool_ctx(task))
    if not result.ok or result.returncode:
        return _ToolOutcome(ok=False, text="二进制读取工具执行未完成：" + (result.error or result.stderr)[:500])
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return _ToolOutcome(ok=False, text="结构化工具结果未完整返回；原始输出：" + str(result.parsed.get("output_file")))
    data["requested_limit"] = requested_limit
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if operation == "info" or data.get("ok") is not True:
        preview = data
    else:
        rows = []
        for item in data.get("items") or []:
            address = item.get("offset", item.get("vaddr", item.get("from")))
            row = {"address": hex(address) if isinstance(address, int) else address}
            for key in ("name", "string", "disasm", "opcode", "size", "fcn_name"):
                if key in item:
                    row[key] = item[key][:160] if isinstance(item[key], str) else item[key]
            rows.append(row)
        preview = {"operation": operation, "items": rows, "total": data.get("total"),
                   "next_offset": data.get("next_offset"), "has_more": data.get("has_more")}
    preview_text = json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
    return _ToolOutcome(ok=data.get("ok") is True, text=text,
        note="二进制静态读取：" + str(operation),
        metadata={"invocation_id": result.invocation_id, "output_file": result.parsed.get("output_file"),
                  "observation": {"digest": hashlib.sha256(text.encode()).hexdigest(), "preview": preview_text,
                                  "structured": True, "empty": data.get("items") == []}})
