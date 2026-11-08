"""python -m spiritscout.cve：不加载模型、不调用付费服务的情报库管理。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from spiritscout.config import load_system_config
from spiritscout.cve.maintenance import (
    bootstrap,
    import_records,
    index_templates,
    sync_cvelist,
    sync_nvd,
)
from spiritscout.cve.service import CVEService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='灵枢 CVE 情报库管理')
    parser.add_argument('--config', default='configs/system.yaml')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('status')
    command = sub.add_parser('bootstrap', help='下载并导入官方全量 CVE List 快照（约数百 MB）')
    command.add_argument('--download-dir', default='.spiritscout/cve-downloads')
    command.add_argument('--offline', action='store_true', help='重用已校验的完整下载，不访问发布 API')
    command = sub.add_parser('import', help='导入 CVE JSON 5 目录、官方 ZIP 或 NVD JSON')
    command.add_argument('path', type=Path)
    command = sub.add_parser('sync', help='NVD 增量更新；首次默认最近七天，可用 --since 回填')
    command.add_argument('--since', default='')
    command.add_argument('--source', choices=['all', 'cvelist', 'nvd'], default='all')
    command = sub.add_parser('templates', help='重建本地 Nuclei 模板关联')
    command.add_argument('--directory', type=Path)
    command = sub.add_parser('lookup')
    command.add_argument('cve_id')
    command.add_argument('--offline', action='store_true')
    command = sub.add_parser('search')
    command.add_argument('query')
    command.add_argument('--source', choices=['local', 'nvd'], default='local')
    command.add_argument('--offset', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        cfg = load_system_config(args.config)
        service = CVEService(cfg.cve)
        if args.action in {'bootstrap', 'sync'} and not cfg.cve.online and not getattr(args, 'offline', False):
            raise ValueError('cve.online=false，不能联网更新；可使用 import')
        if args.action == 'status':
            result = service.store.status()
        elif args.action == 'bootstrap':
            result = bootstrap(service, Path(args.download_dir), offline=args.offline)
        elif args.action == 'import':
            result = import_records(service.store, args.path)
        elif args.action == 'sync':
            result = {}
            if args.source in {'all', 'cvelist'}:
                result['cvelist'] = sync_cvelist(service)
            if args.source in {'all', 'nvd'}:
                result['nvd'] = sync_nvd(service, args.since)
        elif args.action == 'templates':
            result = index_templates(service.store, args.directory or Path(cfg.scanner.templates_dir))
        elif args.action == 'lookup':
            result = service.lookup(args.cve_id, online=cfg.cve.online and not args.offline)
        else:
            if args.offset < 0:
                raise ValueError('offset 不能为负数')
            result = service.search(args.query, source=args.source, offset=args.offset, online=cfg.cve.online)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        states = result.get('sources', [result.get('source_status', result)])
        failed = any(isinstance(s, dict) and s.get('status') in {'error', 'offline'} for s in states)
        return 1 if result.get('invalid') or (failed and not result.get('records')) else 0
    except Exception as exc:
        # 上游 HTTP 异常可能含 URL 或鉴权字段，不将请求原文打印给终端。
        print(f'CVE 操作失败：{type(exc).__name__}: ' +
              (str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else '请检查网络、配置和数据格式'), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
