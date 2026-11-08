"""AI 附件的离线只读检查器；作为独立脚本在既有沙箱内运行，不执行附件代码。"""
from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import math
import os
import pickletools
import struct
import sys
import zipfile
from pathlib import Path

HEADER_LIMIT = 1024 * 1024
PAGE_BYTES = 3600


def environment():
    packages = {}
    for name in ('torch', 'numpy', 'torchvision', 'safetensors', 'onnx', 'onnxruntime', 'scikit-learn', 'Pillow'):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {'python': sys.version.split()[0], 'executable': sys.executable, 'packages': packages,
            'visible_cuda_devices': sorted(str(p) for p in Path('/dev').glob('nvidia[0-9]*') if p.name[6:].isdecimal()),
            'imports_tested': False, 'cuda_tested': False,
            'notice': '安装元数据不证明可导入或 ABI/CUDA 可用；本工具未加载模型、未联网。'}


def identity(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}


def npy_header(stream):
    if stream.read(6) != b'\x93NUMPY':
        raise ValueError('不是 NPY 内容，不能仅按扩展名加载')
    version = stream.read(2)
    if version not in (b'\x01\x00', b'\x02\x00', b'\x03\x00'):
        raise ValueError('不支持的 NPY 版本')
    size_bytes = stream.read(2 if version[0] == 1 else 4)
    size = int.from_bytes(size_bytes, 'little')
    if not 0 < size <= 16384:
        raise ValueError('NPY 头部超过 16 KiB；未加载数组')
    raw = stream.read(size)
    if len(raw) != size:
        raise ValueError('NPY 头部截断')
    header = ast.literal_eval(raw.decode('utf-8' if version[0] == 3 else 'latin1'))
    shape = header.get('shape') if isinstance(header, dict) else None
    if (not isinstance(shape, tuple) or len(shape) > 64
            or any(type(n) is not int or n < 0 for n in shape)
            or not isinstance(header.get('fortran_order'), bool) or 'descr' not in header):
        raise ValueError('NPY 头部结构无效')
    return {'shape': shape, 'dtype': header['descr'], 'fortran_order': header['fortran_order'],
            'elements': math.prod(shape), 'data_offset': stream.tell(), 'values_loaded': False}


def page(rows, offset, limit):
    selected = []
    for row in rows[offset:offset + limit]:
        if len(json.dumps(selected + [row], ensure_ascii=False).encode()) > PAGE_BYTES:
            if not selected:
                raise ValueError('单条元数据超过展示预算；请用 python 按字段读取，未截断或执行内容')
            break
        selected.append(row)
    following = offset + len(selected)
    return {'items': selected, 'total': len(rows), 'offset': offset,
            'next_offset': following, 'has_more': following < len(rows)}


def pickle_metadata(stream, size, args):
    if size > HEADER_LIMIT:
        raise ValueError('pickle 超过 1 MiB 静态扫描上限；未反序列化，需在沙箱按需分析')
    data = stream.read(HEADER_LIMIT + 1)
    rows = []
    count = 0
    for op, arg, position in pickletools.genops(data):
        count += 1
        if count > 100000:
            raise ValueError('pickle 操作码过多，静态扫描未完成')
        if op.name in {'GLOBAL', 'STACK_GLOBAL', 'REDUCE', 'BUILD', 'INST', 'OBJ', 'NEWOBJ',
                       'NEWOBJ_EX', 'EXT1', 'EXT2', 'EXT4', 'PERSID', 'BINPERSID'}:
            rows.append({'offset': position, 'opcode': op.name, 'argument': arg})
    return {'format': 'pickle_opcodes', 'opcode_count': count, **page(rows, args['offset'], args['limit']),
            'executed': False, 'safety_verdict': 'unknown',
            'notice': '仅列出需结合加载器分析的操作码；没有命中也不能证明安全，未解析动态栈或执行 reducer。'}


def artifact(path, args):
    with path.open('rb') as stream:
        magic = stream.read(8)
        stream.seek(0)
        if magic.startswith(b'\x93NUMPY'):
            return {'format': 'npy', **npy_header(stream)}
        if magic.startswith(b'\x80'):
            return pickle_metadata(stream, path.stat().st_size, args)
        if magic.startswith(b'PK'):
            # ZipFile 会一次读取中央目录；先限制目录大小，权重主体仍可很大。
            stream.seek(max(0, path.stat().st_size - 65557))
            tail = stream.read()
            end = tail.rfind(b'PK\x05\x06')
            if end < 0 or len(tail) < end + 22:
                raise ValueError('ZIP 中央目录尾部缺失')
            central_size = struct.unpack_from('<I', tail, end + 12)[0]
            if central_size > 8 * 1024 * 1024:
                raise ValueError('ZIP 中央目录过大或使用 ZIP64；请按需用沙箱工具检查')
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                if len(names) != len(set(names)):
                    raise ValueError('ZIP 含重名条目，读取语义有歧义')
                member = args.get('member')
                if member:
                    info = archive.getinfo(member)
                    with archive.open(info) as content:
                        if member.endswith('.npy'):
                            return {'member': member, 'format': 'npy', **npy_header(content)}
                        if member.endswith('.pkl'):
                            return {'member': member, **pickle_metadata(content, info.file_size, args)}
                    raise ValueError('member 仅支持 NPY 头部或 pickle 静态操作码')
                rows = [{'name': i.filename, 'bytes': i.file_size, 'compressed_bytes': i.compress_size}
                        for i in archive.infolist()]
                return {'format': 'pytorch_zip' if any(n.endswith('/data.pkl') or n == 'data.pkl' for n in names) else 'zip',
                        **page(rows, args['offset'], args['limit']), 'extracted': False,
                        'notice': '容器目录不是权重或模型推理；用 member 读取选定 .npy/.pkl 元数据。'}
        size = int.from_bytes(magic, 'little')
        if len(magic) != 8 or not 2 <= size <= HEADER_LIMIT:
            raise ValueError('未识别为 NPY、ZIP、pickle 或 safetensors；未猜测加载器')
        stream.seek(8)
        raw = stream.read(size)
        if len(raw) != size:
            raise ValueError('safetensors 头部截断')
        header = json.loads(raw)
        if not isinstance(header, dict):
            raise ValueError('safetensors 头部不是对象')
        rows = []
        for name, value in header.items():
            if name == '__metadata__':
                continue
            if not isinstance(value, dict):
                raise ValueError('tensor 元数据无效')
            shape, bounds = value.get('shape'), value.get('data_offsets')
            if (not isinstance(shape, list) or any(type(n) is not int or n < 0 for n in shape)
                    or not isinstance(value.get('dtype'), str) or not isinstance(bounds, list) or len(bounds) != 2
                    or any(type(n) is not int for n in bounds)
                    or not 0 <= bounds[0] <= bounds[1] <= path.stat().st_size - 8 - size):
                raise ValueError('tensor 形状或数据偏移无效')
            rows.append({'name': name, 'shape': shape, 'dtype': value['dtype'], 'data_offsets': bounds})
        return {'format': 'safetensors_header', **page(rows, args['offset'], args['limit']),
                'values_loaded': False, 'notice': '只读元数据，未执行模型，也未证明模型行为或完整格式有效。'}


def compare(path, reference):
    import numpy as np

    arrays = []
    for source in (path, reference):
        with source.open('rb') as stream:
            header = npy_header(stream)
        dtype = np.dtype(header['dtype'])
        if dtype.hasobject or dtype.kind not in 'biuf' or dtype.itemsize > 8:
            raise ValueError('只比较实数/布尔 NPY；对象、复数和扩展精度需要另外验证')
        if dtype.kind in 'iu' and dtype.itemsize > 4:
            raise ValueError('64 位整数不能默认无损转 float64；请按题意做精确整数验证')
        if header['data_offset'] + header['elements'] * dtype.itemsize != source.stat().st_size:
            raise ValueError('NPY 数据长度与头部不一致')
        arrays.append(np.load(source, mmap_mode='r', allow_pickle=False, max_header_size=16384))
    candidate, baseline = arrays
    if candidate.shape != baseline.shape or not candidate.size:
        raise ValueError('数组形状不一致或为空；不自动广播')
    linf, total_l1, total_l2, changed = 0.0, 0.0, 0.0, 0
    minimum, maximum = math.inf, -math.inf
    with np.errstate(over='raise', invalid='raise'):
        for current, original in np.nditer(arrays, flags=['external_loop', 'buffered'],
                op_flags=[['readonly'], ['readonly']], op_dtypes=['float64', 'float64'], buffersize=65536):
            if not np.isfinite(current).all() or not np.isfinite(original).all():
                raise ValueError('数组含 NaN/Inf，不能作为有限扰动验证')
            delta = np.abs(current - original)
            linf = max(linf, float(delta.max()))
            total_l1 += float(delta.sum())
            total_l2 += float(np.dot(delta, delta))
            changed += int(np.count_nonzero(delta))
            minimum, maximum = min(minimum, float(current.min())), max(maximum, float(current.max()))
    return {'format': 'numeric_comparison', 'shape': candidate.shape, 'dtype': str(candidate.dtype),
            'reference_dtype': str(baseline.dtype), 'elements': int(candidate.size),
            'linf': linf, 'l1': total_l1, 'l2': math.sqrt(total_l2), 'changed_elements': changed,
            'candidate_min': minimum, 'candidate_max': maximum, 'metric_scope': 'entire_saved_array',
            'dtype_equal': candidate.dtype == baseline.dtype, 'complete': True,
            'notice': '数值单位沿用文件原值，范数覆盖整个数组；未验证分类目标、逐样本约束或赛方验收。'}


def run(args):
    operation = args['operation']
    if operation == 'environment':
        return {'ok': True, 'operation': operation, **environment()}
    path = Path(args['path'])
    result = artifact(path, args) if operation == 'artifact' else compare(path, Path(args['reference']))
    return {'ok': True, 'operation': operation, 'source': identity(path),
            **({'reference': identity(Path(args['reference']))} if operation == 'compare' else {}), **result}


def main():
    for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ[name] = '1'
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (1024 ** 3, 1024 ** 3))
        args = json.loads(sys.argv[1])
        result = run(args)
        text = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if len(text.encode()) > 5400:
            raise ValueError('回执超过展示预算，请缩小页或使用沙箱按字段读取')
    except (OSError, ValueError, TypeError, KeyError, ImportError, MemoryError, EOFError,
            SyntaxError, RuntimeError, FloatingPointError, zipfile.BadZipFile) as error:
        text = json.dumps({'ok': False, 'error': f'{type(error).__name__}: {str(error)[:500]}'}, ensure_ascii=False)
    print(text)


if __name__ == '__main__':
    main()
