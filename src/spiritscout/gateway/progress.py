"""面向用户的阶段进展；只转发公开正文，不读取推理或另发模型请求。"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from spiritscout.tools.invocation import ExecutionScope, current_execution_scope

# 完整契约只追加一次；逐轮使用短提醒，轻量模式保留原探索协议。
EXPLORATION_PROGRESS_GUIDANCE = (
    '公开正文用自然语言说明观察到什么、意味着什么、下一步验证什么。探索变化时，在正文末尾追加合法 JSON：'
    '{"exploration":{"intent":"要确认的问题","attempt":"验证方法","state":"running"}}。'
    'intent/attempt 用简短稳定标题；转向可加 "from":"原方法标题" 与 "reason":"实际依据"。'
    '只有新发现或方向变化才加 "milestone":{"kind":"discovery","title":"发现","basis":"实际观察及判断","next":"下一步"}，'
    'kind 仅 discovery/breakthrough/turning_point。state 仅 running/blocked/supported/refuted/exhausted/closed。'
    'JSON 只供建图，不能代替正文，也不能出现在用户正文中；普通读取、重试和无结论执行不标注。'
)

FORMAT_GUIDANCE = '正文用自然语言；探索变化时按前述格式在末尾附 exploration JSON，JSON 不进入用户正文。'

INLINE_DISCOVERY_GUIDANCE = (
    '探索图展示以本条为准：重要发现/反证/转向随本轮已有决策附带 discovery，'
    'JSON 决策直接加该字段；原生工具决策在已有公开正文末尾另起一行附 '
    '{"discovery":{"key":"稳定标识","intent":"验证意图","title":"发现",'
    '"basis":"实际依据及重要性理由","importance":3,"confidence":"observed",'
    '"kind":"discovery","evidence_refs":["已有执行引用"]}}。'
    'importance 为0–5整数；confidence 为 hypothesis/observed/verified，'
    'kind 为 discovery/breakthrough/turning_point。同一发现沿用 key 修订。'
    '该元数据由框架隐藏并记录，不是额外动作；不要单独调用 report_discovery、'
    '等待回执或为绘图新增回合。无关键变化时省略，不重复原始输出、不编造依据。'
)


def native_discovery_reminder(_state: dict[str, Any]) -> str:
    """发现搭载本轮已有输出；不催生单独的展示工具往返。"""
    return '公开说明保持自然语言；关键发现随本轮决策附 discovery JSON，无变化省略，不为绘图单独调用工具或增加回合。'

# 旧原生入口保留以兼容历史调用及工具缓存前缀；新契约使用内联元数据。
DISCOVERY_SCHEMA = {'type': 'function', 'function': {
    'name': 'report_discovery', 'description': '记录关键发现，稳定key用于更新；重要性与可信度分开自评，observed/verified须附依据。普通执行不用，不代替黑板事实或独立验证。',
    'parameters': {'type': 'object', 'properties': {
        'key': {'type': 'string', 'maxLength': 80, 'description': '本路线内稳定的发现标识；更新沿用'},
        'intent': {'type': 'string', 'maxLength': 100},
        'title': {'type': 'string', 'maxLength': 100},
        'basis': {'type': 'string', 'maxLength': 600, 'description': '实际依据及为何重要；不复述完整思考'},
        'importance': {'type': 'integer', 'minimum': 0, 'maximum': 5},
        'confidence': {'type': 'string', 'enum': ['hypothesis', 'observed', 'verified']},
        'kind': {'type': 'string', 'enum': ['discovery', 'breakthrough', 'turning_point']},
        'evidence_refs': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string'}},
    }, 'required': ['key', 'intent', 'title', 'basis', 'importance', 'confidence', 'kind', 'evidence_refs']}}}


def ensure_discovery_reporting(state: dict[str, Any]) -> None:
    """云端旧会话在末尾补一次短契约，不重写已缓存历史或每轮追加提醒。"""
    if not state.get('discovery_reporting_v3'):
        state.setdefault('messages', []).append({'role': 'user', 'content': INLINE_DISCOVERY_GUIDANCE,
                                                 '_context_contract': True})
        state['discovery_reporting_v3'] = True


def _public_discovery(value: Any) -> dict[str, Any]:
    """校验和摘取自评的公开字段，不从自然语言猜测发现或分数。"""
    if not isinstance(value, dict):
        raise ValueError('发现记录须为对象')
    limits = {'key': 80, 'intent': 100, 'title': 100, 'basis': 600}
    if any(not isinstance(value.get(key), str) or not 1 <= len(value[key].strip()) <= limit
           for key, limit in limits.items()):
        raise ValueError('key、intent、title、basis 须为长度范围内的非空文本')
    if type(value.get('importance')) is not int or not 0 <= value['importance'] <= 5:
        raise ValueError('importance 须为0–5整数，重要性不代表真实性')
    if value.get('confidence') not in ('hypothesis', 'observed', 'verified') or value.get('kind') not in (
            'discovery', 'breakthrough', 'turning_point'):
        raise ValueError('confidence 或 kind 无效')
    refs = value.get('evidence_refs')
    if (not isinstance(refs, list) or len(refs) > 8
            or any(not isinstance(ref, str) or not ref.strip() or len(ref) > 300 for ref in refs)
            or value['confidence'] != 'hypothesis' and not refs):
        raise ValueError('已观察/已验证须附实际证据引用；推测可以留空，最多8条')
    public = {key: value[key].strip() for key in limits}
    public.update(importance=value['importance'], confidence=value['confidence'], kind=value['kind'],
                  evidence_refs=list(dict.fromkeys(ref.strip() for ref in refs)))
    return public


def publish_discovery(audit: Any, actor: str, value: Any, **context: Any) -> dict[str, Any]:
    """保存执行者自评的展示记录；不发布黑板事实，也不提升独立验证状态。"""
    public = _public_discovery(value)
    scope = current_execution_scope.get()
    detail = {key: getattr(scope, key, '') for key in ('task_id', 'route_id', 'step_id', 'turn')}
    detail.update(context)
    if not detail.get('task_id') or audit is None:
        raise ValueError('当前任务或审计不可用，发现尚未记录')
    audit.log('agent.discovery', actor, {**detail, **public})
    return {'ok': True, 'key': public['key'], 'importance': public['importance'], 'confidence': public['confidence']}


class PublicProgressEmitter:
    """模型流式增量 → 审计事件（节流合并，SSE 原样转播给前端轨迹）。

    增量只服务观感：合并窗口内的碎片拼成一条事件，避免逐 token 刷屏。
    """

    def __init__(self, audit: Any, agent_id: str, *, route: str = "",
                 challenge: str = "", turn: int = 0, interval: float = 0.25,
                 task_id: str = "", route_id: str = "", inline_discovery: bool = False):
        self.audit = audit
        self.agent_id = agent_id
        self.route = route
        self.challenge = challenge
        self.turn = turn
        self.task_id = task_id
        self.route_id = route_id
        self.interval = interval
        self._buf: list[str] = []
        self._kind = ""
        self._inline_discovery = inline_discovery
        self._public_raw = ""
        self._public_sent = ""
        self.has_public_text = False
        self._last = time.monotonic()

    def __call__(self, kind: str, text: str) -> None:
        if kind == "text" and text:
            self.has_public_text = True
        if kind == "reset":
            self.has_public_text = False
            self._buf.clear()
            self._kind = ""
            self._public_raw = self._public_sent = ""
            self._emit("reset", "")
            return
        if kind == "tool_call":
            return          # 参数碎片不转播；整轮结束后统一发 tool_call 事件
        if kind != self._kind:
            self.flush()
            self._kind = kind
        self._buf.append(text)
        if sum(map(len, self._buf)) >= 240 or \
                time.monotonic() - self._last >= self.interval:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        text = "".join(self._buf)
        self._buf.clear()
        self._last = time.monotonic()
        if not text.strip():
            return
        # 用户界面只看正文；建图 JSON 由完整响应解析，不进入实时流。
        if self._kind == "text":
            if self._inline_discovery:
                # JSON 可跨节流帧；保留解析前缀，不能把下一帧的字段碎片误当正文。
                self._public_raw += text
                public = parse_public_progress(self._public_raw).get('text', '')
                text = public[len(self._public_sent):] if public.startswith(self._public_sent) else ''
                self._public_sent += text
            else:
                text = parse_public_progress(text).get("text", "")
            if not text:
                return
        for start in range(0, len(text), 4000):
            self._emit(self._kind or "text", text[start:start + 4000])

    def _emit(self, kind: str, text: str) -> None:
        scope = current_execution_scope.get() or ExecutionScope()
        self.audit.log("agent.delta", self.agent_id, {
            "task_id": self.task_id or scope.task_id, "route_id": self.route_id or scope.route_id,
            "route": self.route, "challenge": self.challenge,
            "turn": self.turn, "kind": kind, "text": text})


def ensure_exploration_progress(state: dict[str, Any]) -> None:
    """旧会话只在末尾追加一次展示约定，保留已缓存历史前缀。"""
    if state.get('exploration_display_v2'):
        return
    messages = state.setdefault('messages', [])
    if not any(EXPLORATION_PROGRESS_GUIDANCE in str(message.get('content') or '')
               for message in messages if message.get('role') == 'user'):
        messages.append({'role': 'user', 'content': EXPLORATION_PROGRESS_GUIDANCE,
                         '_context_contract': True})
    state['exploration_display_v1'] = True
    state['exploration_display_v2'] = True


def public_exploration(value: Any) -> dict[str, Any] | None:
    """只接受模型明确声明的公开层级，不分析正文猜意图或提升为事实。"""
    if not isinstance(value, dict):
        return None
    if any(not isinstance(value.get(key), str) or not value[key].strip()
           or len(value[key]) > 100 for key in ('intent', 'attempt')):
        return None
    result = {key: value[key].strip() for key in ('intent', 'attempt')}
    for key, limit in [('from', 100), ('reason', 400)]:
        if isinstance(value.get(key), str) and value[key].strip():
            result[key] = value[key].strip()[:limit]
    if isinstance(value.get('state'), str) and value['state'] in {'running', 'blocked', 'supported', 'refuted', 'exhausted', 'closed'}:
        result['state'] = value['state']
    milestone = value.get('milestone')
    if (isinstance(milestone, dict)
            and milestone.get('kind') in ('discovery', 'breakthrough', 'turning_point')
            and all(isinstance(milestone.get(key), str) and milestone[key].strip()
                    for key in ('title', 'basis'))):
        public = {key: milestone[key].strip()[:limit]
                  for key, limit in (('kind', 30), ('title', 100), ('basis', 600))}
        if isinstance(milestone.get('next'), str):
            public['next'] = milestone['next'].strip()[:400]
        if isinstance(milestone.get('evidence_refs'), list):
            public['evidence_refs'] = list(dict.fromkeys(ref.strip()[:300]
                for ref in milestone['evidence_refs'][:20] if isinstance(ref, str) and ref.strip()))
        if type(milestone.get('importance')) is int and 0 <= milestone['importance'] <= 5:
            public['importance'] = milestone['importance']
        if milestone.get('confidence') in ('hypothesis', 'observed', 'verified'):
            public['confidence'] = milestone['confidence']
        result['milestone'] = public
    return result


def parse_public_progress(content: str) -> dict[str, Any]:
    """分离公开正文与显式展示对象；支持正文夹带 JSON，不解析或展示内部决策。"""
    raw = str(content or '').strip()
    prose: list[str] = []
    bodies: list[str] = []
    semantic = None
    discovery = None
    decoder = json.JSONDecoder()
    cursor = 0
    markers = re.compile(r'(?m)^[ \t]*(?:```[^\n]*|[\{\[])')
    protocol_keys = {'progress', 'commentary', 'exploration', 'discovery', 'analysis', 'actions', 'steps',
                     'tool_calls', 'skill_requests', 'decision', 'verdict', 'dag_nodes', 'local_tool', 'web_search'}
    for marker in markers.finditer(raw):
        if marker.start() < cursor:
            continue
        start = marker.start()
        token = marker.group().lstrip()
        fenced = token.startswith('```')
        if fenced:
            line_end = raw.find('\n', start)
            close = re.search(r'(?m)^[ \t]*```[ \t]*(?:\n|$)', raw[line_end + 1:]) if line_end >= 0 else None
            end = line_end + 1 + close.end() if close else len(raw)
            if token[3:].strip().lower() not in ('', 'json'):
                prose.append(raw[cursor:end])
                cursor = end
                continue
            value_start = line_end + 1 if line_end >= 0 else len(raw)
        else:
            value_start = start + len(marker.group()) - 1
            end = len(raw)
        try:
            data, length = decoder.raw_decode(raw[value_start:].lstrip())
            if not fenced:
                end = value_start + len(raw[value_start:]) - len(raw[value_start:].lstrip()) + length
        except ValueError:
            # 流式/被截断的对象没有完整语义；保留此前正文，隐藏未完成协议。
            prose.append(raw[cursor:start])
            cursor = end
            continue
        is_protocol = isinstance(data, dict) and bool(protocol_keys.intersection(data))
        if not is_protocol and (prose or raw[:start].strip()):
            prose.append(raw[cursor:end])
            cursor = end
            continue
        prose.append(raw[cursor:start])
        cursor = end
        if not isinstance(data, dict):
            continue
        bodies.extend(data[key].strip() for key in ('progress', 'commentary')
                      if isinstance(data.get(key), str) and data[key].strip())
        if 'discovery' in data:
            try:
                discovery = _public_discovery(data['discovery'])
            except ValueError:
                pass  # 可选展示元数据无效时仍保留本轮公开正文。
        exploration = data.get('exploration')
        if isinstance(exploration, dict) and 'milestone' not in exploration and 'milestone' in data:
            exploration = {**exploration, 'milestone': data['milestone']}
        if declared := public_exploration(exploration):
            semantic = declared
    prose.append(raw[cursor:])
    text = ''.join(prose).strip()
    if not text:
        text = '\n\n'.join(dict.fromkeys(bodies))
    return {'text': text, **({'exploration': semantic} if semantic else {}),
            **({'discovery': discovery} if discovery else {})}


def publish_progress(audit: Any, actor: str, content: str, **context: Any) -> dict[str, Any] | None:
    """将模型本次已有的公开说明保存为可回放事件，归属到实际任务／路线。"""
    if audit is None:
        return
    parsed = parse_public_progress(content)
    text = parsed['text']
    exploration = parsed.get('exploration')
    discovery_key = ''
    if parsed.get('discovery'):
        try:
            discovery_key = publish_discovery(audit, actor, parsed['discovery'], **context)['key']
        except ValueError as exc:
            audit.log('agent.discovery_rejected', actor, {'error': str(exc)})
    scope = current_execution_scope.get()
    detail = {key: getattr(scope, key, '') for key in ('task_id', 'route_id', 'step_id', 'turn')}
    detail.update(context)
    if not detail.get('task_id'):
        return
    if not text:
        return {**detail, 'discovery_key': discovery_key} if discovery_key else None
    detail = {**detail, 'text': text[:2400], **({'exploration': exploration} if exploration else {})}
    audit.log('agent.progress', actor, detail)
    return {**detail, **({'discovery_key': discovery_key} if discovery_key else {})}
