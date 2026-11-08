"""离线技能检索：中英术语召回、字段加权排序和渐进目录；分级不限制能力。"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from typing import Any

TIERS = {'core': '核心技能', 'domain': '领域技能', 'reference': '参考资料'}
# 仅为检索词汇，不参与意图分类、任务规划、授权或执行。
SYNONYMS = (
    ('idor', 'bola', '对象级授权', '对象级越权', '水平越权', '对象归属'),
    ('authorization', 'access control', '权限控制', '访问控制', '越权'),
    ('authentication', 'login', '身份认证', '登录', '登陆'),
    ('session', 'cookie', '会话管理', '登录态'),
    ('sqli', 'sql injection', 'sql注入', 'sql 注入'),
    ('xss', 'cross site scripting', '跨站脚本'),
    ('ssrf', 'server side request forgery', '服务端请求伪造'),
    ('csrf', 'cross site request forgery', '跨站请求伪造'),
    ('ssti', 'template injection', '模板注入'),
    ('xxe', 'xml external entity', '外部实体'),
    ('upload', '文件上传'), ('traversal', '路径穿越', '目录穿越'),
    ('deserialization', '反序列化'), ('race condition', '竞态', '条件竞争'),
    ('business logic', '业务逻辑'), ('graphql', '图查询'),
    ('oauth', '单点登录', 'sso'), ('jwt', 'json web token'),
    ('recon', 'reconnaissance', '侦察', '信息收集'),
    ('api', '接口'), ('web', '网站', '网页'),
    ('cloud', '云安全', '云平台'), ('container', '容器'),
    ('active directory', 'ad', '域安全', '活动目录'),
    ('reverse engineering', '逆向'), ('cryptography', 'crypto', '密码学'),
    ('binary', 'pwn', '二进制'), ('code review', '源码审计', '代码审计'),
    ('awdp', 'patch', '修补', '补丁'), ('offline', '离线', '断网'),
    ('report', 'reporting', '报告'), ('forensics', '取证'),
)
_CORE = frozenset({'web-idor', 'web-sqli', 'web-xss', 'web-ssrf', 'web-authentication',
                   'web-business-logic', 'awdp-source-audit', 'awdp-patch-delivery',
                   'ai-offline-environment'})
_STOP = frozenset('the a an and or to of in for with when this that is are testing test security '
                  'please help how can use using 用于 测试 安全 请问 帮我 如何 进行 一个 这个'.split())


def normalize(value: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value).casefold()).strip()


@lru_cache(maxsize=8192)
def tokens(value: str) -> set[str]:
    result = set(re.findall(r'[a-z0-9]+', normalize(value))) - _STOP
    for phrase in re.findall(r'[\u3400-\u9fff]+', value):
        result.update(phrase[i:i + 2] for i in range(len(phrase) - 1)
                      if phrase[i:i + 2] not in _STOP)
    return result


@lru_cache(maxsize=8192)
def concepts(value: str) -> set[str]:
    text = normalize(value).replace('-', ' ')
    return {f'concept:{i}' for i, group in enumerate(SYNONYMS)
            if any((term in text if re.search(r'[\u3400-\u9fff]', term) else
                    re.search(r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])', text))
                   for term in group)}


def metadata(row: dict[str, Any]) -> dict[str, Any]:
    tier = row.get('tier')
    if not isinstance(tier, str) or tier not in TIERS:
        tier = ('core' if row['skill_id'] in _CORE else 'reference'
                if row.get('domain') in {'defense', 'reporting'} else 'domain')
    return {'tier': tier, 'aliases': row.get('aliases') or [], 'tags': row.get('tags') or []}


def validate_metadata(data: dict[str, Any]) -> None:
    if 'tier' in data and (not isinstance(data['tier'], str) or data['tier'] not in TIERS):
        raise ValueError('Skill tier 须为 core、domain 或 reference')
    for key in ('aliases', 'tags'):
        values = data.get(key, [])
        if (not isinstance(values, list) or len(values) > 32
                or any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in values)):
            raise ValueError(f'Skill {key} 须为最多 32 个非空短文本的数组')
        if key in data:
            data[key] = list(dict.fromkeys(v.strip() for v in values))


def rank(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """返回稳定排序的元数据；稀有词、完整名称与同义概念优先，正文不注入。"""
    q = normalize(query)[:200]
    rows = [{**row, **metadata(row)} for row in items]
    if not q:
        return rows
    exact_ids = [row for row in rows if normalize(row['skill_id']) == q]
    if exact_ids:
        return [{**row, 'relevance': 1000.0, 'match_terms': [q],
                 'matched_alias': False, 'related_terms': False} for row in exact_ids]
    wanted = tokens(q) | concepts(q)
    documents, frequency = [], Counter()
    for row in rows:
        fields = [(row['skill_id'], 8), (' '.join(row['aliases']), 7),
                  (' '.join(row['tags']), 5), (row.get('description', ''), 2),
                  (row.get('domain', ''), 1)]
        weights: dict[str, int] = {}
        for value, weight in fields:
            for token in (tokens(value) | concepts(value)) & wanted:
                weights[token] = max(weights.get(token, 0), weight)
        frequency.update(weights)
        documents.append((row, weights))
    matches = []
    for row, weights in documents:
        names = [normalize(row['skill_id']), *(normalize(v) for v in row['aliases'])]
        exact = q in names
        # 完整子串支持自定义标识和短词；避免用无关高频词填满结果页。
        phrase = any(q in normalize(str(row.get(k, ''))) for k in ('skill_id', 'description', 'domain'))
        if not weights and not exact and not phrase:
            continue
        score = sum(weight * (1 + math.log((len(rows) + 1) / (frequency[t] + 1)))
                    for t, weight in weights.items())
        score *= 0.5 + len(weights) / max(1, len(wanted))
        score += 1000 if exact else 25 if phrase else 0
        matches.append({**row, 'relevance': round(score, 4),
                        'match_terms': sorted(t for t in weights if not t.startswith('concept:'))[:8],
                        'matched_alias': exact and q != normalize(row['skill_id']),
                        'related_terms': bool(any(t.startswith('concept:') for t in weights))})
    matches.sort(key=lambda row: (-row['relevance'], row['skill_id'].casefold()))
    return matches
