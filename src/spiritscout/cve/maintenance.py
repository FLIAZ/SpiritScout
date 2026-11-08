"""运维入口：导入官方快照、按 NVD 修改时间增量更新、索引本地模板。"""
from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from spiritscout.cve.service import CVEService
from spiritscout.cve.store import CVE_ID, CVEStore, cve_id, normalize


def import_records(store: CVEStore, path: Path) -> dict[str, int]:
    """不解压到文件系统；逐记录处理，支持官方双层 ZIP。"""
    counts = {"imported": 0, "invalid": 0}
    pending = []

    def accept(data: Any):
        if not isinstance(data, dict):
            counts["invalid"] += 1
            return
        records = [("nvd", r) for r in data["vulnerabilities"]] if "vulnerabilities" in data else (
            [("cvelist", data)] if "cveMetadata" in data else [])
        for source, record in records:
            try:
                normalize(record, source)
                pending.append((source, record))
            except (KeyError, ValueError, TypeError, AttributeError):
                counts["invalid"] += 1
            if len(pending) >= 500:
                counts["imported"] += store.put_many(pending)
                pending.clear()

    def archive(file, depth=0):
        if depth > 1:
            raise ValueError("快照嵌套超过两层")
        with zipfile.ZipFile(file) as z:
            for entry in z.infolist():
                if entry.filename.lower().endswith('.zip'):
                    with z.open(entry) as source, tempfile.TemporaryFile() as target:
                        while chunk := source.read(1024 * 1024):
                            target.write(chunk)
                        target.seek(0)
                        archive(target, depth + 1)
                elif entry.filename.lower().endswith('.json'):
                    if Path(entry.filename).name in {'delta.json', 'deltaLog.json'}:
                        continue  # 发布附带的变更清单，不是 CVE 记录。
                    if entry.file_size > 64 * 1024 * 1024:
                        raise ValueError("单条快照文件超过 64 MiB")
                    try:
                        with z.open(entry) as stream:
                            accept(json.load(stream))
                    except (ValueError, UnicodeError):
                        counts["invalid"] += 1

    if path.is_dir():
        for file in path.rglob('CVE-*.json'):
            try:
                accept(json.loads(file.read_text(encoding='utf-8')))
            except (ValueError, UnicodeError):
                counts["invalid"] += 1
    elif zipfile.is_zipfile(path):
        archive(path)
    else:
        accept(json.loads(path.read_text(encoding='utf-8')))
    if pending:
        counts["imported"] += store.put_many(pending)
    if not counts["imported"]:
        raise ValueError("没有导入任何 CVE 记录")
    return counts


def bootstrap(service: CVEService, directory: Path, *, offline: bool = False) -> dict:
    """官方发布资产缓存下载；文件完成后才导入，失败不标记完成。"""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "cvelist-full.zip"
    previous = service.store.meta_get("snapshot_download") or {}
    if offline:
        tag = previous.get('tag', '')
        if not (tag.startswith('cve_') and target.is_file()
                and target.stat().st_size == previous.get('size')):
            raise ValueError('没有已校验完成的本地官方快照，请先联网 bootstrap 或使用 import')
        release = {'tag_name': tag, 'published_at': previous.get('published_at')}
        asset = {'name': previous.get('name') or tag[4:14] + '_all_CVEs.zip'}
        digest = previous.get('digest')
        if digest and digest.startswith('sha256:'):
            with target.open('rb') as file:
                if hashlib.file_digest(file, 'sha256').hexdigest() != digest[7:]:
                    raise ValueError('缓存官方快照 SHA256 不匹配')
    else:
        with httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": "SpiritScout-CVE"}) as client:
            response = client.get("https://api.github.com/repos/CVEProject/cvelistV5/releases/latest")
            response.raise_for_status()
            release = response.json()
            asset = next(a for a in release["assets"] if "all_CVEs" in a["name"])
            same_snapshot = (previous.get('name') == asset['name']
                             or previous.get('tag') == release['tag_name'])
            download = not (target.exists() and target.stat().st_size == asset["size"] and same_snapshot)
            candidate = target.with_suffix('.part') if download else target
            if download:
                partial = target.with_suffix('.part')
                with client.stream("GET", asset["browser_download_url"]) as stream:
                    stream.raise_for_status()
                    with partial.open('wb') as out:
                        for chunk in stream.iter_bytes(1024 * 1024):
                            out.write(chunk)
                if partial.stat().st_size != asset["size"]:
                    raise ValueError("官方快照下载长度不匹配")
            digest = asset.get("digest")
            if digest and digest.startswith("sha256:"):
                with candidate.open('rb') as file:
                    actual = hashlib.file_digest(file, 'sha256').hexdigest()
                if actual != digest[7:]:
                    raise ValueError("官方快照 SHA256 不匹配")
            if download:
                candidate.replace(target)
            service.store.meta_put("snapshot_download", {"tag": release["tag_name"], "size": asset["size"],
                "name": asset['name'], 'published_at': release['published_at'], 'digest': digest})
    result = import_records(service.store, target)
    if not result['invalid']:
        service.store.meta_put("cvelist_snapshot", {"tag": release["tag_name"],
            "published_at": release['published_at'],
            "coverage_until": asset["name"][:10] + "T00:00:00+00:00",
            "finished_at": datetime.now(timezone.utc).isoformat(), **result})
    return {"tag": release["tag_name"], 'offline': offline, **result}


def sync_nvd(service: CVEService, since: str = "") -> dict:
    """窗口 <=120 天，逐页落库；整窗口成功才推进水位，可从失败窗口重试。"""
    saved = service.store.meta_get('nvd_watermark')
    start = datetime.fromisoformat(since or saved or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    end = datetime.now(timezone.utc)
    if start > end:
        raise ValueError("同步起点不能晚于当前时间")
    start -= timedelta(minutes=2)  # 边界重叠，幂等 upsert 消除重复。
    count = 0
    while start < end:
        stop = min(end, start + timedelta(days=119))
        offset = 0
        while True:
            data = service.request('nvd', params={
                'lastModStartDate': start.isoformat(), 'lastModEndDate': stop.isoformat(),
                'startIndex': offset, 'resultsPerPage': 2000})
            rows = data['vulnerabilities']
            total = data['totalResults']
            if data.get('startIndex') != offset or (not rows and offset < total):
                raise ValueError("NVD 返回了不完整分页，水位未推进")
            count += service.store.put_many([('nvd', r) for r in rows])
            offset += len(rows)
            if offset >= total:
                break
        # 显式回填旧区间不回退已有水位。
        watermark = service.store.meta_get('nvd_watermark')
        if not watermark or stop > datetime.fromisoformat(watermark):
            service.store.meta_put('nvd_watermark', stop.isoformat())
        start = stop
    return {'imported': count, 'watermark': service.store.meta_get('nvd_watermark')}


def index_templates(store: CVEStore, directory: Path) -> dict:
    """从模板声明提取编号；支持 HTTP/TCP/DNS 等任意目录，不执行模板。"""
    if not directory.is_dir():
        raise ValueError("Nuclei 模板目录不存在")
    rows, invalid = [], 0
    for file in directory.rglob('*.yaml'):
        try:
            if file.is_symlink():
                continue
            data = yaml.safe_load(file.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or not data.get('id') or not isinstance(data.get('info'), dict):
                continue
            info = data['info']
            declared = (info.get('classification') or {}).get('cve-id', [])
            if isinstance(declared, str):
                declared = [declared]
            ids = set(CVE_ID.findall(str(data['id'])))
            ids.update(i for v in declared or [] for i in CVE_ID.findall(str(v)))
            protocols = [p for p in ('http', 'network', 'tcp', 'dns', 'ssl', 'headless', 'code', 'file', 'javascript') if p in data]
            rows.append((str(file.relative_to(directory)), str(data['id']),
                         json.dumps(sorted(i.upper() for i in ids)), str(info.get('name', '')),
                         str(info.get('severity', 'unknown')), ','.join(protocols)))
        except (OSError, UnicodeError, yaml.YAMLError, TypeError, AttributeError):
            invalid += 1
    with store.connect() as db:
        db.execute('DELETE FROM templates')
        db.executemany('INSERT INTO templates VALUES(?,?,?,?,?,?)', rows)
    result = {'indexed': len(rows), 'invalid': invalid, 'directory': str(directory.resolve()),
              'finished_at': datetime.now(timezone.utc).isoformat()}
    store.meta_put('templates', result)
    return result


def sync_cvelist(service: CVEService) -> dict:
    """用官方滚动变更日志维护主库；超出日志窗口时明确要求重新 bootstrap。"""
    snapshot = service.store.meta_get('cvelist_snapshot') or {}
    watermark = service.store.meta_get('cvelist_watermark') or snapshot.get('coverage_until')
    if not watermark:
        raise ValueError('请先 bootstrap 官方全量快照，再增量同步 CVE List')
    start = datetime.fromisoformat(watermark.replace('Z', '+00:00'))
    raw = service.request('cvelist_delta')
    changes = raw['changes']
    if not changes:
        raise ValueError('官方 CVE 变更日志为空，水位未推进')
    times = [datetime.fromisoformat(row['fetchTime'].replace('Z', '+00:00')) for row in changes]
    if start < min(times):
        raise ValueError('CVE 同步中断超过官方变更日志窗口，请重新 bootstrap 补齐缺口')
    ids = set()
    for row, fetched in zip(changes, times, strict=True):
        if fetched >= start - timedelta(minutes=2):
            if row.get('error'):
                raise ValueError('官方变更日志包含错误，水位未推进，请稍后重试或 bootstrap')
            for kind in ('new', 'updated'):
                ids.update(cve_id(item['cveId']) for item in row.get(kind, []))

    def get(ident):
        record = service.request('cvelist', suffix=ident)
        if cve_id(record['cveMetadata']['cveId']) != ident:
            raise ValueError('CVE 来源返回了不匹配的编号')
        return ('cvelist', record)

    count, pending = 0, []
    # 只并行获取公开记录，写入仍批量顺序提交；任何失败都不推进同步水位。
    with ThreadPoolExecutor(max_workers=6) as pool:
        for item in pool.map(get, sorted(ids)):
            pending.append(item)
            if len(pending) >= 100:
                count += service.store.put_many(pending)
                pending.clear()
    if pending:
        count += service.store.put_many(pending)
    stop = max(times).isoformat()
    service.store.meta_put('cvelist_watermark', stop)
    return {'imported': count, 'watermark': stop}
