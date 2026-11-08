"""创建独立 Ollama 轻量配置，不修改正常模式或原始 Qwen 模型。"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

import yaml


def api(base: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(base.rstrip('/') + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.load(response)
    if result.get('error'):
        raise ValueError(result['error'])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description='初始化 16GB RAM / 8GB VRAM 的独立 Ollama 轻量配置')
    parser.add_argument('--base-url', default='http://127.0.0.1:11434')
    parser.add_argument('--alias', default='spiritscout-qwen35-9b-light:latest')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    model_config = yaml.safe_load((root/'configs/models.ollama-light.example.yaml').read_text())
    model = model_config['models'][0]
    model['model'] = args.alias
    model_config['providers'][0]['base_url'] = args.base_url
    if args.dry_run:
        print(yaml.safe_dump(model_config, allow_unicode=True, sort_keys=False))
        return 0
    try:
        api(args.base_url, '/api/show', {'model': 'qwen3.5:9b'})
        models = api(args.base_url, '/api/tags').get('models', [])
        if not any(item.get('name') == args.alias for item in models):
            api(args.base_url, '/api/create', {'model': args.alias, 'from': 'qwen3.5:9b',
                'parameters': model['ollama_options'], 'stream': False})
        else:
            print('模型别名已存在，保留原文件；请求级参数仍按轻量配置发送。')
        data = root/'.spiritscout-ollama'
        data.mkdir(exist_ok=True)
        for name, body in [('models.yaml', model_config),
                           ('system.yaml', yaml.safe_load((root/'configs/system.ollama-light.example.yaml').read_text())),
                           ('agent_models.yaml', {'assignments': {}, 'naming_chain': []})]:
            path = data/name
            if path.exists():
                print(f'保留已有配置：{path}')
            else:
                with path.open('x', encoding='utf-8') as stream:
                    yaml.safe_dump(body, stream, allow_unicode=True, sort_keys=False)
        print('轻量模型：' + args.alias)
        print('启动：./launch.sh start --offline')
        print('正常模式仍使用 configs/system.yaml；不要同时让两种模式争用同一 GPU。')
        return 0
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(f'初始化未完成：{error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
