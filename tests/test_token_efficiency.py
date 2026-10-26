"""无损增量编码：恢复现场、历史前缀与输入规模的回归。"""
from __future__ import annotations

import copy
import json
import random
import unittest

from spiritscout.gateway.context import (
    DecisionMemory,
    decision_messages,
    estimate_input_tokens,
)


def apply_patch(document: object, operations: list[dict]) -> object:
    """独立按 JSON Pointer 定位并执行 RFC 6902 的 add/remove/replace 子集。"""
    document = copy.deepcopy(document)
    for operation in operations:
        path = operation['path']
        if path == '':
            document = copy.deepcopy(operation['value'])
            continue
        parts = [part.replace('~1', '/').replace('~0', '~') for part in path[1:].split('/')]
        parent = document
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = parts[-1]
        if isinstance(parent, list):
            index = len(parent) if key == '-' else int(key)
            if operation['op'] == 'remove':
                parent.pop(index)
            elif operation['op'] == 'add':
                parent.insert(index, copy.deepcopy(operation['value']))
            else:
                parent[index] = copy.deepcopy(operation['value'])
        elif operation['op'] == 'remove':
            del parent[key]
        else:
            parent[key] = copy.deepcopy(operation['value'])
    return document


class LosslessPatchTests(unittest.TestCase):
    def test_default_keeps_original_patch_encoding_before_live_validation(self) -> None:
        memory = DecisionMemory()
        old = {'facts': {f'field_{i}': i for i in range(40)}}
        new = {'facts': {f'field_{i}': i + 1 for i in range(40)}}
        memory.prepare(decision_messages('原契约', old))
        memory.commit('{"ok":true}')
        current = memory.prepare(decision_messages('原契约', new))
        self.assertEqual(json.loads(current[-1]['content'])['patch'], DecisionMemory.patch(old, new))

    def test_boolean_and_number_changes_are_not_lost(self) -> None:
        for old, new in [({'value': True}, {'value': 1}),
                         ({'nested': [False]}, {'nested': [0]}),
                         ({'count': 1}, {'count': 1.0})]:
            patch = DecisionMemory.compact_patch(old, new)
            self.assertEqual(json.dumps(apply_patch(old, patch)), json.dumps(new))

    def test_dense_updates_send_smaller_patch_without_losing_facts(self) -> None:
        old = {'mission': '用户规则原文' * 1000,
               'observations': {f'field_{i}': i for i in range(40)}}
        new = {**old, 'observations': {f'field_{i}': i + 1 for i in range(40)}}
        regular = DecisionMemory.patch(old, new)
        compact = DecisionMemory.compact_patch(old, new)
        self.assertEqual(apply_patch(old, compact), new)
        def encode(value: object) -> str:
            return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        self.assertLess(len(encode(compact)), len(encode(regular)) // 2)
        self.assertNotIn('用户规则原文', encode(compact))
        self.assertEqual(old['observations']['field_0'], 0)

    def test_existing_small_patches_are_unchanged(self) -> None:
        old = {'a/b': {'~key': 1}, 'list': [1, 2, 3]}
        new = {'a/b': {'~key': 2}, 'list': [4]}
        self.assertEqual(DecisionMemory.compact_patch(old, new), DecisionMemory.patch(old, new))

    def test_generated_documents_restore_exactly_and_never_enlarge_encoded_delta(self) -> None:
        rng = random.Random(42)
        def value(depth: int = 0) -> object:
            if depth > 2 or rng.randrange(3) == 0:
                return rng.choice([None, True, False, rng.randrange(100), '原文/字段~' * rng.randrange(5)])
            if rng.randrange(2):
                return {f'key/{i}~': value(depth + 1) for i in range(rng.randrange(16))}
            return [value(depth + 1) for _ in range(rng.randrange(16))]
        for _ in range(150):
            old, new = value(), value()
            regular, compact = DecisionMemory.patch(old, new), DecisionMemory.compact_patch(old, new)
            self.assertEqual(apply_patch(old, compact), new)
            def messages(patch: list[dict]) -> list[dict]:
                return [{'role': 'user', 'content': json.dumps({'patch': patch}, ensure_ascii=False, separators=(',', ':'), sort_keys=True)}]
            self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False).encode()), len(json.dumps(regular, ensure_ascii=False).encode()))
            self.assertLessEqual(estimate_input_tokens(messages(compact)), estimate_input_tokens(messages(regular)))

    def test_only_new_suffix_changes_and_committed_reasoning_survives(self) -> None:
        memory = DecisionMemory()
        original = {'mission': '不可改写的授权原文' * 100,
                    'facts': {f'field_{i}': i for i in range(40)}}
        first = memory.prepare(decision_messages('稳定系统契约', original))
        memory.commit('{"continue":true}', reasoning='已有推理')
        prefix = copy.deepcopy(memory.messages)
        changed = {**original, 'facts': {f'field_{i}': i + 1 for i in range(40)}}
        second = memory.prepare(decision_messages('稳定系统契约', changed), compact_patches=True)
        self.assertEqual(second[:len(prefix)], prefix)
        self.assertEqual(second[:len(first)], first)
        self.assertEqual(second[-2]['reasoning_content'], '已有推理')
        self.assertEqual(apply_patch(original, json.loads(second[-1]['content'])['patch']), changed)
