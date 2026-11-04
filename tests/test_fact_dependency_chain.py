"""传递失效只读计算，保留无关分支与历史版本，循环／长链不会递归崩溃。"""
import json
import tempfile
import unittest
from pathlib import Path

from spiritscout.blackboard import Blackboard
from spiritscout.core.models import AgentType, BlackboardLayer


class DependencyChainTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.bb = Blackboard(Path(self.folder.name) / 'bb.db')
        self.addCleanup(self.bb.close)

    def write(self, key, refs=(), **extra):
        return self.bb.write(BlackboardLayer.TOPOLOGY, key,
                             {'kind': 'finding', 'text': key, 'evidence_refs': list(refs), **extra})

    def read(self, key):
        return self.bb.read_versioned(BlackboardLayer.TOPOLOGY, key)

    def test_deep_version_change_is_visible_without_rewriting_descendants(self):
        self.write('A')
        self.write('B', ['topology:A@1'])
        self.write('C', ['topology:B@1'])
        self.write('unrelated')
        self.write('A', text='changed')
        c, version = self.read('C')
        self.assertEqual(c['evidence_state'], 'expired')
        self.assertEqual(c['dependency_issues'], [{'ref': 'topology:A@1', 'reason': 'version_changed'}])
        self.assertEqual(version, 1)
        self.assertEqual(self.read('unrelated')[0]['evidence_state'], 'unverified')
        rows = self.bb.query(BlackboardLayer.TOPOLOGY)
        self.assertEqual(next(r['data'] for r in rows if r['record_id'] == 'C')['evidence_state'], 'expired')

    def test_conflict_propagates_as_expired_not_as_a_proven_false_conclusion(self):
        self.write('A', assessment='conflicting', assessment_reason='两次观察冲突')
        self.write('B', ['topology:A@1'])
        self.write('C', ['topology:B@1'])
        self.assertEqual(self.read('A')[0]['evidence_state'], 'conflicting')
        self.assertEqual(self.read('C')[0]['evidence_state'], 'expired')

    def test_verified_conclusion_does_not_hide_expired_dependencies(self):
        self.write('A')
        self.write('B', ['topology:A@1'])
        self.bb.write(BlackboardLayer.VULN, 'verified', {'status': 'accepted',
                      'verification': {'verdict': 'confirmed'}, 'evidence_refs': ['topology:B@1']},
                      updated_by='verifier', agent_type=AgentType.VERIFIER)
        self.assertEqual(self.bb.read(BlackboardLayer.VULN, 'verified')['evidence_state'], 'independently_verified')
        self.write('A', text='new evidence')
        self.assertEqual(self.bb.read(BlackboardLayer.VULN, 'verified')['evidence_state'], 'expired')

    def test_long_chain_and_corrupt_cycle_are_safe(self):
        with self.bb.transaction() as conn:
            for i in range(1100):
                sources = [{'kind': 'blackboard', 'layer': 'topology', 'record_id': str(i - 1), 'version': 1}] if i else []
                data = {'kind': 'finding', 'provenance': {'sources': sources}}
                conn.execute('INSERT INTO bb_records(layer,record_id,version,data,updated_by,updated_at) VALUES(?,?,?,?,?,?)',
                             ('topology', str(i), 1, json.dumps(data), '', 'now'))
        self.assertEqual(self.read('1099')[0]['evidence_state'], 'unverified')
        self.write('0', text='changed root')
        self.assertEqual(self.read('1099')[0]['evidence_state'], 'expired')
        with self.bb.transaction() as conn:
            for name, parent in [('cycle-a', 'cycle-b'), ('cycle-b', 'cycle-a')]:
                data = {'kind': 'finding', 'provenance': {'sources': [
                    {'kind': 'blackboard', 'layer': 'topology', 'record_id': parent, 'version': 1}]}}
                conn.execute('INSERT INTO bb_records(layer,record_id,version,data,updated_by,updated_at) VALUES(?,?,?,?,?,?)',
                             ('topology', name, 1, json.dumps(data), '', 'now'))
        self.assertEqual(self.read('cycle-a')[0]['evidence_state'], 'expired')

    def test_refreshing_references_clears_projection_issues(self):
        self.write('A')
        self.write('B', ['topology:A@1'])
        self.write('A', text='updated')
        self.assertEqual(self.read('B')[0]['evidence_state'], 'expired')
        self.write('B', ['topology:A@2'])
        self.assertEqual(self.read('B')[0]['evidence_state'], 'unverified')
        self.assertNotIn('dependency_issues', self.read('B')[0])

    def test_concurrent_write_does_not_mix_dependency_snapshots(self):
        self.write('A')
        self.write('B', ['topology:A@1'])
        self.write('C', ['topology:B@1'])
        reader = Blackboard(Path(self.folder.name)/'bb.db', readonly=True)
        self.addCleanup(reader.close)
        changed = False
        def on_query(sql):
            nonlocal changed
            if not changed and "record_id='B'" in sql:
                changed = True
                self.write('A', text='concurrent update')
        reader._conn.set_trace_callback(on_query)
        self.assertEqual(reader.read(BlackboardLayer.TOPOLOGY, 'C')['evidence_state'], 'unverified')
        self.assertTrue(changed)
        self.assertEqual(reader.read(BlackboardLayer.TOPOLOGY, 'C')['evidence_state'], 'expired')

    def test_read_inside_write_transaction_does_not_commit_it(self):
        self.write('A')
        with self.assertRaises(RuntimeError), self.bb.transaction():
            # 用直接 DML 模拟调用方尚未提交的事务。
            self.bb._conn.execute("UPDATE bb_records SET updated_by='uncommitted' WHERE record_id='A'")
            with self.bb._read_snapshot():
                self.bb._conn.execute("SELECT data FROM bb_records WHERE record_id='A'").fetchone()
            self.assertTrue(self.bb._conn.in_transaction)
            raise RuntimeError('rollback caller transaction')
        with self.bb._lock:
            self.assertNotEqual(self.bb._conn.execute("SELECT updated_by FROM bb_records WHERE record_id='A'").fetchone()[0], 'uncommitted')


if __name__ == '__main__':
    unittest.main()
