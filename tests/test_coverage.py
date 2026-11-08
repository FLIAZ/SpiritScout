"""覆盖投影不推断身份、不把尝试或陈旧引用升级为已验证。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spiritscout.blackboard import Blackboard
from spiritscout.blackboard.exploration import execute
from spiritscout.core.models import AgentType, BlackboardLayer
from spiritscout.web.coverage import read_coverage, read_evidence


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bb = Blackboard(self.root / 'bb.db')
        self.addCleanup(self.bb.close)
        for key in ('orders', 'login'):
            self.bb.write(BlackboardLayer.ASSET, key, {'value': '/api/' + key}, updated_by='recon')

    def explore(self, **changes) -> dict:
        return execute(self.bb, 'exploration_write', {'node_type': 'attempt', 'text': '比较订单权限',
            'state': 'supported', 'identity': 'member-a', 'test_type': '对象级权限',
            'asset_refs': ['asset:orders'], **changes}, actor='tester')

    def test_unknown_dimensions_and_supported_are_not_verified(self) -> None:
        self.explore(evidence_refs=['missing-execution'])
        page = read_coverage(self.root)
        self.assertEqual(page['counts'], {'untested': 1, 'pending': 1})
        row = next(r for r in page['items'] if r['identity'])
        self.assertEqual(row['evidence_missing'], 1)
        unknown = next(r for r in page['items'] if not r['identity'])
        self.assertEqual(unknown['test_type'], '')
        self.assertEqual(read_coverage(self.root, identity='member-a', query='权限')['total'], 1)

    def test_explicit_independent_confirmation_and_changed_version(self) -> None:
        data = {'title': '订单权限', 'status': 'accepted', 'poc_verified': True,
                'verification': {'verdict': 'confirmed'}}
        self.bb.write(BlackboardLayer.VULN, 'v1', data, updated_by='verifier', agent_type=AgentType.VERIFIER)
        self.explore(evidence_refs=['vuln:v1@1'])
        self.explore(identity='member-b', evidence_refs=['missing'])
        self.assertEqual(read_coverage(self.root, status='confirmed')['total'], 1)
        # 同资产另一个身份不能继承前一账号结论。
        self.assertEqual(read_coverage(self.root, identity='member-b')['items'][0]['status'], 'pending')
        self.bb.write(BlackboardLayer.VULN, 'v1', {**data, 'status': 'inconclusive'}, updated_by='verifier', agent_type=AgentType.VERIFIER)
        self.assertEqual(read_coverage(self.root, status='confirmed')['total'], 0)
        self.assertEqual(read_evidence(self.root, 'vuln:v1@1')['state'], 'changed')

    def test_agent_claim_alone_is_not_independent_verification(self) -> None:
        self.bb.write(BlackboardLayer.VULN, 'claim', {'status': 'accepted', 'verification': {'verdict': 'confirmed'}}, updated_by='master')
        self.explore(evidence_refs=['vuln:claim'])
        self.assertEqual(read_coverage(self.root, status='confirmed')['total'], 0)

    def test_evidence_file_boundary_and_credential_private(self) -> None:
        self.bb.write(BlackboardLayer.CREDENTIAL, 'private', {'password': 'not-public'}, updated_by='master')
        self.assertEqual(read_evidence(self.root, 'credential:private')['state'], 'restricted')
        folder = self.root / 'artifacts'
        folder.mkdir()
        (folder / 'response.txt').write_text('HTTP/1.1 403 Forbidden')
        (folder / 'link').symlink_to(self.root / 'bb.db')
        self.assertEqual(read_evidence(self.root, 'artifacts/response.txt')['kind'], 'file')
        self.assertFalse(read_evidence(self.root, 'artifacts/link')['resolved'])
        self.assertFalse(read_evidence(self.root, '../bb.db')['resolved'])

    def test_dimensions_survive_update_and_truncation_is_explicit(self) -> None:
        ref = self.explore()['blackboard_ref']
        execute(self.bb, 'exploration_write', {'record_id': ref['record_id'], 'version': ref['version'],
            'node_type': 'attempt', 'text': '等待另一个身份', 'state': 'blocked', 'reason': '缺少管理员账号'}, actor='tester')
        row = read_coverage(self.root, status='blocked')['items'][0]
        self.assertEqual(row['identity'], 'member-a')
        self.assertEqual(row['test_type'], '对象级权限')
        with patch('spiritscout.web.coverage._LIMIT', 1):
            self.assertTrue(read_coverage(self.root)['truncated'])
        with self.assertRaises(ValueError):
            self.explore(identity=['not-text'])


if __name__ == '__main__':
    unittest.main()
