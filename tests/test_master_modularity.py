"""模块拆分前的模型请求契约：稳定前缀、原有参数与旧导入入口。"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from spiritscout.agents.master.agent import MasterAgent, MasterContext, MasterState
from spiritscout.agents.master.contracts import MasterContext as SharedContext
from spiritscout.agents.master.contracts import MasterState as SharedState
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType
from spiritscout.gateway.gateway import ChatResponse


async def capture(cls: type[MasterAgent]) -> list[dict]:
    master = object.__new__(cls)
    master.agent_id = "master-prefix-fixture"
    response = ChatResponse(
        content='{"steering":[]}',
        reasoning="核对过的路线进度",
        model="fixture",
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=0,
        latency_ms=0,
    )
    master.ctx = SimpleNamespace(
        audit=NullAuditLogger(),
        blackboard=None,
        policy=None,
        agents={AgentType.CTF_WEB: object(), AgentType.CTF_CRYPTO: object()},
        extra={
            "mission": "核对给定赛题，保留已确认的事实。",
            "standing_rules": "仅使用本次用户给出的测试规则。",
            "mode_prompt": "开放赛制，模型动态决策。",
            "competition_context": {
                "challenges": [{"id": "c1", "title": "缓存契约夹具", "score": 100}]
            },
        },
        gateway=SimpleNamespace(chat=AsyncMock(return_value=response)),
    )
    digest = {
        "tasks": [
            {
                "task_id": "t1",
                "agent": "ctf_web",
                "challenge": {"id": "c1"},
                "routes": [{"notes": ["旧观察"]}],
            }
        ]
    }
    requests = []
    for note in ["旧观察", "新观察"]:
        digest["tasks"][0]["routes"][0]["notes"] = [note]
        await master._llm_steer(digest)
        call = master.ctx.gateway.chat.call_args
        requests.append(
            {
                "messages": copy.deepcopy(call.args[0]),
                "options": {
                    k: v
                    for k, v in call.kwargs.items()
                    if k
                    in [
                        "reasoning_effort",
                        "preserve_context",
                        "max_tokens",
                        "temperature",
                        "tools",
                    ]
                },
            }
        )
    return requests


class MasterModuleContractTests(unittest.IsolatedAsyncioTestCase):
    def test_original_imports_keep_same_context_and_state(self) -> None:
        self.assertIs(MasterContext, SharedContext)
        self.assertIs(MasterState, SharedState)

    async def test_requests_match_before_split_and_keep_prefix(self) -> None:
        requests = await capture(MasterAgent)
        # 由拆分前实现的固定输入生成，同时覆盖真实系统提示、完整消息与调用参数。
        expected = [
            "1050a8e5d7f96e573f088526b7717a35207b4a121ee98c1e79d9acfc9f7e8c81",
            "702e3c6b3058c98ac47a99cbe56ed50db3e41ac7c9c1879235ec33912dd101c1",
        ]
        actual = [
            hashlib.sha256(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
            for item in requests
        ]
        self.assertEqual(actual, expected)
        first, second = [item["messages"] for item in requests]
        self.assertEqual(second[: len(first)], first)
        self.assertEqual(requests[0]["options"], requests[1]["options"])
