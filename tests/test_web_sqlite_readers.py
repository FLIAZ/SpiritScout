"""只读会话连接及时释放，含 URI 特殊字符的路径保持原数据库身份。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from spiritscout.blackboard import Blackboard
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import BlackboardLayer
from spiritscout.web.conversation import ConversationResources
from spiritscout.web.sessions import Session
from spiritscout.web.workbench import side_session_source


class WebSqliteReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name)
        self.root = self.parent / '会话 #1?mode=rw%20'
        self.root.mkdir()
        self.session = Session('reader-fixture', 'ctf', 'fixture', self.root)
        bb = Blackboard(self.session.bb_path)
        try:
            bb.write(BlackboardLayer.TASK_STATE, 'task-1', {'task': {
                'task_id': 'task-1', 'agent_type': 'ctf_web', 'target': 'fixture',
                'payload': {'challenge': {'id': 'one'}}}}, updated_by='ctf-real')
            bb.write(BlackboardLayer.TARGET_AUTH, 'user-context', {'statements': []})
        finally:
            bb.close()
        self.resources = ConversationResources(self.session, NullAuditLogger(), {})

    @contextmanager
    def assert_connections_closed(self):
        connect = sqlite3.connect
        connections = []

        def track(*args, **kwargs):
            conn = connect(*args, **kwargs)
            connections.append(conn)
            return conn

        try:
            with patch('sqlite3.connect', side_effect=track):
                yield
            self.assertTrue(connections)
            for conn in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute('SELECT 1')
        finally:
            # 即使回归失败也关闭真实连接，避免测试自身依赖垃圾回收。
            for conn in connections:
                conn.close()

    def test_context_closes_connections_and_preserves_layer_counts(self) -> None:
        with self.assert_connections_closed():
            context = self.resources.context()
        self.assertEqual(context['blackboard_layers'], {'task_state': 1, 'target_auth': 1})

    def test_side_source_reads_special_path_without_creating_another_database(self) -> None:
        before = set(self.parent.iterdir())
        with self.assert_connections_closed():
            source = side_session_source(self.session, 'ctf-real', 'task-1')
        self.assertEqual(source['agent'], 'ctf_web')
        self.assertEqual(source['task']['payload']['challenge']['id'], 'one')
        self.assertEqual(set(self.parent.iterdir()), before)

    def test_rejected_actor_still_closes_connection(self) -> None:
        with self.assert_connections_closed():
            with self.assertRaises(SpiritScoutError):
                side_session_source(self.session, 'forged', 'task-1')

    def test_sql_errors_still_close_connections(self) -> None:
        with closing(sqlite3.connect(self.session.bb_path)) as conn:
            conn.execute('DROP TABLE bb_records')
            conn.execute('CREATE TABLE bb_records (unexpected TEXT)')
            conn.commit()
        readers = [self.resources.context,
                   lambda: side_session_source(self.session, 'ctf-real', 'task-1')]
        for reader in readers:
            with self.subTest(reader=reader):
                with self.assert_connections_closed():
                    with self.assertRaises(sqlite3.OperationalError):
                        reader()
