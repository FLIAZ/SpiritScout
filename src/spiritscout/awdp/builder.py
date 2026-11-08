"""从模型选定的修改文件生成赛事补丁包；不选择漏洞或执行补丁。"""
from __future__ import annotations

import base64
import hashlib
import os
import shlex
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from spiritscout.awdp.package import member_path, pack
from spiritscout.awdp.patches import _member_path, _open_regular, _original_values
from spiritscout.tools.sandbox_paths import (
    open_directory,
    sandbox_directory,
    sandbox_tmp_directory,
)

SCHEMA = {"type": "function", "function": {"name": "build_patch",
    "description": "AWDP：对照原件与实际修复文件，断网语法预检后生成 patch.tar.gz（patch.sh + files/修复文件）。可选 update.sh/外层目录。目标依据部署规则；再用 verify_patch 回归。语法通过不等于修复有效，不自动编译或重启。",
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "files": {"type": "array", "minItems": 1, "maxItems": 64, "items": {
            "type": "object", "additionalProperties": False, "properties": {
                "source_ref": {"type": "string", "description": "优先填启动材料/source_manifest/edit_source 返回的原件引用。使用兼容 source 路径时此处填空字符串"},
                "source": {"type": "string", "description": "原归档成员路径或 attachments 下相对路径；不去掉外层目录"},
                "patched": {"type": "string", "description": "修改后的实际文件路径，位于本题 workdir、/tmp 或 artifacts"},
                "target": {"type": "string", "description": "部署根目录下的相对目标路径；省略时同 source。映射依据须写入修复说明"}},
            "required": ["source_ref", "patched"]}},
        "default_root": {"type": "string", "description": "实际部署根目录，默认 .（调用时的 cwd）；赛事通常无参数执行脚本，应从题面/部署配置确定服务器绝对路径。验证可用参数覆盖为干净副本"},
        "format": {"type": "string", "enum": ["tar_gzip", "shell_script"], "description": "默认 tar_gzip；仅赛事/用户明确要求裸脚本时选 shell_script"},
        "entrypoint": {"type": "string", "enum": ["patch.sh", "update.sh"], "description": "默认 patch.sh；平台要求 update.sh 时指定，包名随之为 update.tar.gz"},
        "package_dir": {"type": "string", "description": "默认空字符串，入口在包根；要求 update/update.sh 时填 update，不能凭猜测添加外层目录"}},
        "required": ["files"]}}}

_MAX_BYTES = 2 * 1024 * 1024


def _bytes(source):
    content = source.read(_MAX_BYTES + 1)
    if len(content) > _MAX_BYTES:
        raise ValueError("文件内容超过 2 MiB；可按平台约定自行生成提交脚本或归档")
    return content


def _patched_bytes(workspace: Path, challenge: str, raw: str, *, limit: int = _MAX_BYTES) -> bytes:
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        raise ValueError("patched 必须是实际修改文件路径")
    path = Path(raw)
    work, tmp = sandbox_directory(challenge), sandbox_tmp_directory(challenge)
    if path.is_absolute():
        if path.is_relative_to(workspace):
            path = path.relative_to(workspace)
        elif path.is_relative_to('/tmp'):
            path = tmp / path.relative_to('/tmp')
        else:
            raise ValueError("修改文件不在本题工作目录中")
    else:
        path = work / path
    if not any(path.is_relative_to(base) for base in (work, tmp, Path('artifacts'))):
        raise ValueError("修改文件不在本题工作目录中")
    with _open_regular(workspace, path) as source:
        content = source.read(limit + 1)
        if len(content) > limit:
            raise ValueError(f'文件超过 {limit} 字节读取上限')
        return content


def build_patch(workspace: Path, challenge: str, attachments: list[str], arguments: dict) -> dict:
    """快照原件/修改件，按全部文件预检、备份、写入的脚本契约生成交付件。"""
    return write_build(prepare_build(workspace, challenge, attachments, arguments))


@dataclass
class BuildPlan:
    workspace: Path
    challenge: str
    default_root: str
    output_format: str
    entrypoint: str
    package_dir: str
    records: list[dict]
    payloads: list[bytes]


def prepare_build(workspace: Path, challenge: str, attachments: list[str], arguments: dict) -> BuildPlan:
    """冻结待打包字节；语法预检和最终交付使用同一份内容。"""
    if not isinstance(arguments, dict) or set(arguments) - {'files', 'default_root', 'format', 'entrypoint', 'package_dir'}:
        raise ValueError("build_patch 参数不符合契约")
    entries, default_root = arguments.get('files'), arguments.get('default_root', '.')
    if (not isinstance(entries, list) or not 1 <= len(entries) <= 64
            or not isinstance(default_root, str) or not default_root.strip()
            or len(default_root) > 4096 or any(c in default_root for c in '\0\r\n')):
        raise ValueError("需要 1–64 个修改文件和有效 default_root")
    output_format = arguments.get('format', 'tar_gzip')
    entrypoint = arguments.get('entrypoint', 'patch.sh')
    package_dir = arguments.get('package_dir', '')
    if output_format not in {'tar_gzip', 'shell_script'} or entrypoint not in {'patch.sh', 'update.sh'}:
        raise ValueError('format 需为 tar_gzip/shell_script，entrypoint 需为 patch.sh/update.sh')
    if not isinstance(package_dir, str) or len(package_dir) > 200:
        raise ValueError('package_dir 必须为空或单层相对目录')
    if package_dir and (member_path(package_dir) != package_dir or '/' in package_dir or package_dir.startswith('-')
                        or output_format == 'shell_script'):
        raise ValueError('package_dir 仅支持归档中的单层相对目录')
    mappings = []
    workspace = workspace.resolve()
    from spiritscout.awdp.originals import catalog, resolve_ref

    manifest = None
    for item in entries:
        if not isinstance(item, dict) or set(item) - {'source', 'source_ref', 'patched', 'target'}:
            raise ValueError("文件映射必须包含 source、patched，可选 target")
        try:
            if item.get('source_ref'):
                if 'source' in item:
                    raise ValueError('source_ref 与 source 只能选择一种')
                manifest = manifest or catalog(workspace, challenge, attachments)
                entry, _ = resolve_ref(workspace, challenge, item['source_ref'], manifest)
                source = entry['source']
            else:
                source = _member_path(item.get('source'))
        except ValueError as exc:
            raise ValueError(str(exc) + '；source 必须是原附件成员相对路径，如 bundle/app.py；'
                             '不能填工作副本绝对路径。patched 请保留 edit_source 返回的完整路径。') from exc
        target = _member_path(item.get('target', source))
        if any(ord(c) < 32 or ord(c) == 127 for c in source + target):
            raise ValueError("文件路径不能含控制字符")
        mappings.append((source, item.get('patched'), target))
    if any(len({item[index] for item in mappings}) != len(mappings) for index in (0, 2)):
        raise ValueError("source 或 target 重复，不能生成歧义补丁")
    workspace = workspace.resolve()
    try:
        originals = _original_values(workspace, attachments, {item[0] for item in mappings}, _bytes)
    except (tarfile.TarError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError(f"无法读取原始附件，请核对归档格式、完整性和加密状态：{exc}") from exc
    records, payloads, total = [], [], 0
    for source, patched, target in mappings:
        before = originals[source]
        try:
            after = _patched_bytes(workspace, challenge, patched)
        except FileNotFoundError as exc:
            raise ValueError(f'修复文件尚不存在：{patched}；先用 edit_source 生成副本，'
                             'patched 原样使用其返回路径；build_patch 不负责修改源码。') from exc
        if before == after:
            raise ValueError(f"修改文件与原件相同：{source}")
        total += len(after)
        if total > _MAX_BYTES:
            raise ValueError("修复文件内容合计超过 2 MiB；请按平台规则自行生成提交件")
        records.append({'source': source, 'target': target,
            'before_sha256': hashlib.sha256(before).hexdigest(),
            'after_sha256': hashlib.sha256(after).hexdigest()})
        payloads.append(after)
    return BuildPlan(workspace, challenge, default_root, output_format, entrypoint, package_dir, records, payloads)


def write_build(plan: BuildPlan) -> dict:
    from spiritscout.awdp.originals import save_record

    workspace, challenge = plan.workspace, plan.challenge
    default_root, output_format = plan.default_root, plan.output_format
    entrypoint, package_dir = plan.entrypoint, plan.package_dir
    records, payloads = [dict(item) for item in plan.records], plan.payloads
    prefix = package_dir + '/' if package_dir else ''
    entry_path = prefix + entrypoint
    bundled = output_format == 'tar_gzip'
    script = _script(records, payloads, default_root, entrypoint, bundled)
    content = script.encode()
    script_sha = hashlib.sha256(content).hexdigest()
    filename = entrypoint.removesuffix('.sh') + '.tar.gz' if bundled else entrypoint
    if bundled:
        members = {entry_path: (content, 0o755)}
        for item, data in zip(records, payloads, strict=True):
            item['payload'] = prefix + 'files/' + item['source']
            members[item['payload']] = (data, 0o644)
        content = pack(members)
    digest = hashlib.sha256(content).hexdigest()
    # 元数据留在控制面，不往赛事包里塞报告或内部 manifest。
    save_record(workspace, challenge, 'builds', digest, {'mapping': records,
        'sha256': digest, 'script_sha256': script_sha, 'default_root': default_root,
        'format': output_format, 'entrypoint': entry_path, 'filename': filename})
    directory = sandbox_directory(challenge) / ('awdp-' + os.urandom(8).hex())
    with open_directory(workspace, directory, create=True) as fd:
        output = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600 if bundled else 0o700, dir_fd=fd)
        with os.fdopen(output, 'wb') as stream:
            stream.write(content)
    return {'ok': True, 'archive': str(workspace / directory / filename),
        'files': [item['source'] for item in records], 'mapping': records,
        'sha256': digest, 'script_sha256': script_sha, 'format': output_format, 'entrypoint': entry_path,
        'usage': ('解压后执行 ' if bundled else '') + f'sh {shlex.quote(entry_path)} [部署根目录]',
        'default_root': default_root,
        'requires': ['sh', 'sha256sum', 'mktemp', 'cp', 'cat', 'rm'] + ([] if bundled else ['base64']),
        'executed': False, 'vulnerability_verified': False,
        'note': '已生成最终提交件，未执行。入口 UTF-8/LF、0755；普通源码不额外加执行权限。'
                '所有目标先校验原件/修补后哈希；拒绝链接；重跑不重复修改。'
                'verify_patch 验证整个提交件；包、脚本或修复文件变化后都须重验。'
                '请核对赛事解包目录、部署根、命令白名单及重启约定；工具不猜服务名或重启服务。'
                '写入失败会尝试恢复已写文件；这不是并发写入下的事务保证。'}


def _script(records: list[dict], payloads: list[bytes], default_root: str,
            entrypoint: str = 'patch.sh', bundled: bool = False) -> str:
    # 数据仅以 shell 单词引用或 base64 here-doc 写入，不将源码作为脚本执行。
    lines = ['#!/bin/sh', 'set -eu', 'umask 077',
        '# SpiritScout AWDP patch. Run against a quiescent deployment or a clean test copy.',
        'fail() { printf "%s\\n" "$*" >&2; exit 1; }',
        f'[ "$#" -le 1 ] || fail "Usage: sh {entrypoint} [deployment-root]"',
        'for cmd in ' + ('' if bundled else 'base64 ') + 'sha256sum mktemp cp cat rm; do',
        '  command -v "$cmd" >/dev/null 2>&1 || fail "Missing dependency: $cmd"', 'done',
        # 在切换部署目录前定位包内数据，不依赖模型工作区或平台传入的 cwd。
        *(['case "$0" in */*) package=${0%/*} ;; *) package=. ;; esac',
           'package=$(CDPATH= cd -P "$package" && pwd -P) || fail "Invalid package directory"'] if bundled else []),
        'root=${1-' + shlex.quote(default_root) + '}',
        'case "$root" in /*) ;; *) root="./$root" ;; esac',
        'CDPATH= cd -P "$root" || fail "Invalid deployment root"',
        'digest() { value=$(sha256sum < "$1") || return 1; printf "%s" "${value%% *}"; }',
        'regular() {', '  part=$1', '  while :; do',
        '    [ ! -L "./$part" ] || fail "Refusing symlink: $1"',
        '    case "$part" in */*) part=${part%/*} ;; *) break ;; esac', '  done',
        '  [ -f "./$1" ] || fail "Missing regular target: $1"', '}',
        'check() {', '  regular "$1"', '  actual=$(digest "./$1")',
        '  [ "$actual" = "$2" ] || [ "$actual" = "$3" ] || fail "Source hash mismatch: $1"',
        '  [ "$actual" = "$3" ] || [ -w "./$1" ] || fail "Target not writable: $1"', '}',
    ]
    for item in records:
        lines.append('check ' + shlex.join([item['target'], item['before_sha256'], item['after_sha256']]))
    lines += ['stage=$(mktemp -d "${TMPDIR:-/tmp}/spiritscout-patch.XXXXXXXX")', 'cleanup() {',
              '  status=$?', '  trap - 0 1 2 3 15', '  if [ "$status" -ne 0 ]; then']
    for index, item in reversed(list(enumerate(records))):
        target = shlex.quote('./' + item['target'])
        lines += [f'    if [ -f "$stage/{index}.started" ]; then',
                  f'      cat "$stage/{index}.old" > {target} || printf "%s\\n" "Rollback failed: file {index}" >&2',
                  '    fi']
    lines += ['  fi', '  rm -rf "$stage"', '  exit "$status"', '}', 'trap cleanup 0',
              "trap 'exit 1' 1 2 3 15"]
    for index, (item, payload) in enumerate(zip(records, payloads, strict=True)):
        lines += [f'cp {shlex.quote("./" + item["target"])} "$stage/{index}.old"']
        if bundled:
            lines += [f'cp "$package"/{shlex.quote("files/" + item["source"])} "$stage/{index}.new"']
        else:
            lines += [f'base64 -d > "$stage/{index}.new" <<\'SPIRITSCOUT_DATA_{index}\'',
                      base64.encodebytes(payload).decode('ascii').rstrip('\n'), f'SPIRITSCOUT_DATA_{index}']
        lines += [f'[ "$(digest "$stage/{index}.new")" = {item["after_sha256"]} ] || fail "Invalid patch content"']
    for index, item in enumerate(records):
        target = shlex.quote('./' + item['target'])
        lines += ['check ' + shlex.join([item['target'], item['before_sha256'], item['after_sha256']]),
                  f'if [ "$(digest {target})" != {item["after_sha256"]} ]; then',
                  f'  : > "$stage/{index}.started"', f'  cat "$stage/{index}.new" > {target}',
                  f'  [ "$(digest {target})" = {item["after_sha256"]} ] || fail "Post-write hash mismatch"', 'fi']
    lines.append('printf "%s\\n" "Patch applied; application regression tests are still required."')
    return '\n'.join(lines) + '\n'
