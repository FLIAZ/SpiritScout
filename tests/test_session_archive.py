"""会话档案的持久化、证据分层、分页与文件访问边界。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.models import BlackboardLayer
from spiritscout.tools.invocation import ExecutionScope
from spiritscout.tools.journal import ExecutionJournal
from spiritscout.web.app import create_app
from spiritscout.web.deliverables import open_artifact, read_deliverables
from spiritscout.web.sessions import Session


class SessionArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "sessions" / "sess-archive"
        self.workspace.mkdir(parents=True)
        self.bb = Blackboard(self.workspace / "bb.db")
        self.addCleanup(self.bb.close)
        self.bb.write(BlackboardLayer.TOPOLOGY, "business-context", {
            "evidence": [{"id": "E1", "kind": "page", "url": "https://archive.test/?session=hidden-value",
                          "headers": {"Authorization": "Bearer hidden-header"}}],
            "facts": [{"statement": "公开入口存在", "evidence_refs": ["E1"]},
                      {"statement": "无证据断言", "evidence_refs": ["missing"]}],
            "hypotheses": [{"statement": "权限边界待验证", "evidence_refs": ["E1"]}],
            "gaps": ["缺少多角色记录"]}, updated_by="business-agent")
        (self.workspace / "artifacts").mkdir()
        (self.workspace / "artifacts" / "证据.txt").write_text("saved output", encoding="utf-8")

    def test_supported_facts_stay_separate_from_hypotheses(self) -> None:
        result = read_deliverables(self.workspace)
        self.assertEqual(result["counts"]["fact"], 1)
        self.assertEqual(result["counts"]["hypothesis"], 3)
        fact = next(item for item in result["items"] if item["kind"] == "fact")
        self.assertEqual(fact["refs"][0]["id"], "E1")
        self.assertEqual(fact["record_id"], "business-context")
        self.assertEqual(fact["version"], 1)
        serialized = json.dumps(result)
        self.assertNotIn("hidden-value", serialized)
        self.assertNotIn("hidden-header", serialized)

    def test_pagination_filter_and_reopen_keep_saved_data(self) -> None:
        first = read_deliverables(self.workspace, limit=2)
        second = read_deliverables(self.workspace, offset=2, limit=2)
        self.assertTrue(first["has_more"])
        self.assertFalse({r["id"] for r in first["items"]} & {r["id"] for r in second["items"]})
        filtered = read_deliverables(self.workspace, kind="hypothesis", query="多角色")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(read_deliverables(self.workspace)["counts"], first["counts"])
        self.assertEqual(len(read_deliverables(self.workspace, kind="file")["items"]), 1)

    def test_evidence_and_files_default_group_preserves_other_original_records(self) -> None:
        self.bb.write(BlackboardLayer.ASSET, 'compressed-asset', {
            'value': 'https://archive.test/api', 'attrs': {'notes': '长记录' * 12000}}, updated_by='recon')
        self.bb.write(BlackboardLayer.TOPOLOGY, 'test-attempt', {
            'kind': 'exploration', 'node_type': 'attempt', 'text': '对照账号权限',
            'state': 'blocked', 'reason': '缺少第二个账号', 'asset_refs': ['asset:compressed-asset']},
            updated_by='tester')
        page = read_deliverables(self.workspace, kind='sources')
        self.assertEqual({row['kind'] for row in page['items']}, {'evidence', 'file'})
        self.assertEqual(page['total'], page['counts']['evidence'] + page['counts']['file'])
        assets = read_deliverables(self.workspace, kind='asset')
        self.assertEqual(assets['total'], 1, '压缩保存的原始记录同样可检索')
        self.assertEqual(assets['items'][0]['record_id'], 'compressed-asset')
        original = read_deliverables(self.workspace, query='test-attempt')
        self.assertEqual(original['total'], 1)
        self.assertEqual(original['items'][0]['record_id'], 'test-attempt')
        self.assertGreater(read_deliverables(self.workspace)['total'], page['total'])

    def test_tool_execution_reference_is_searchable_as_raw_evidence(self) -> None:
        journal = ExecutionJournal(self.bb, self.workspace, "run-archive")
        journal.begin("inv-archive-evidence", "http_probe", "d" * 64, "never",
                      params={"url": "https://archive.test/?token=hidden"},
                      scope=ExecutionScope(agent_id="recon", task_id="task-1"))
        journal.transition("inv-archive-evidence", "succeeded")
        result = read_deliverables(self.workspace, kind="evidence",
                                   query="inv-archive-evidence")
        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertEqual(item["text"], "inv-archive-evidence")
        self.assertEqual(item["title"], "http_probe · inv-archive-evid")
        self.assertEqual(item["source"], "recon / task-1")
        self.assertEqual(item["status"], "succeeded")
        self.assertNotIn("hidden", json.dumps(result))

    def test_file_access_rejects_traversal_and_links(self) -> None:
        secret = self.root / "private.txt"
        secret.write_text("private-marker")
        (self.workspace / "artifacts" / "linked.txt").symlink_to(secret)
        for path in ("../private.txt", str(secret), "artifacts/../meta.json", "artifacts/linked.txt", "bb.db"):
            with self.subTest(path=path), self.assertRaises(OSError):
                open_artifact(self.workspace, path)
        with open_artifact(self.workspace, "artifacts/证据.txt") as file:
            self.assertEqual(file.read(), b"saved output")
        files = read_deliverables(self.workspace, kind="file")["items"]
        self.assertEqual(len(files), 1)

    def test_api_is_session_scoped_and_download_is_attachment(self) -> None:
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(self.root)),
                           models_config_path=str(self.root / "models.yaml"))
        app = create_app(cfg, [])
        session = Session(session_id="sess-archive", mode="src", target="archive.test",
                          workspace=self.workspace, status="completed")
        app.state.manager._sessions[session.session_id] = session
        with TestClient(app, base_url="http://127.0.0.1") as client:
            prefix = "/api/sessions/sess-archive"
            response = client.get(prefix + "/deliverables", params={"kind": "fact"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], 1)
            self.assertEqual(client.get(prefix + "/deliverables?kind=bad").status_code, 422)
            grouped = client.get(prefix + "/deliverables?kind=sources")
            self.assertEqual(grouped.status_code, 200)
            self.assertEqual({row['kind'] for row in grouped.json()['items']}, {'evidence', 'file'})
            self.assertEqual(client.get(prefix + "/deliverables?offset=-1").status_code, 422)
            self.assertEqual(client.get("/api/sessions/missing/deliverables").status_code, 404)
            response = client.get(prefix + "/artifacts/download", params={"path": "artifacts/证据.txt"})
            self.assertEqual(response.content, b"saved output")
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertEqual(client.get(prefix + "/artifacts/download", params={"path": "bb.db"}).status_code, 404)
            # NUL 字符路径抛 ValueError：按「不存在」404，不能炸成 500
            self.assertEqual(client.get(prefix + "/artifacts/download",
                                        params={"path": "artifacts/\x00.txt"}).status_code,
                             404)

    def test_share_file_to_chat_and_attachment_serving(self) -> None:
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(self.root)),
                           models_config_path=str(self.root / "models.yaml"))
        app = create_app(cfg, [])
        session = Session(session_id="sess-archive", mode="src", target="archive.test",
                          workspace=self.workspace, status="completed")
        app.state.manager._sessions[session.session_id] = session
        (self.workspace / "attachments").mkdir(exist_ok=True)
        (self.workspace / "attachments" / "上传.txt").write_text("uploaded", encoding="utf-8")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            prefix = "/api/sessions/sess-archive"
            # 附件目录可回显（预览/下载自己的上传），但不进档案文件列表
            self.assertEqual(client.get(prefix + "/artifacts/download",
                                        params={"path": "attachments/上传.txt"}).content, b"uploaded")
            self.assertEqual(
                len(read_deliverables(self.workspace, kind="file")["items"]), 1)
            # 发送到会话：file.shared 审计留痕，回包带相对路径
            response = client.post(prefix + "/files/share",
                                   json={"path": "artifacts/证据.txt", "note": "证据文件"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["path"], "artifacts/证据.txt")
            self.assertEqual(client.post(prefix + "/files/share",
                                         json={"path": "bb.db"}).status_code, 404)
            self.assertEqual(client.post(prefix + "/files/share",
                                         json={"path": "../private.txt"}).status_code, 404)
        lines = session.audit_path.read_text(encoding="utf-8").strip().splitlines()
        shared = json.loads(lines[-1])
        self.assertEqual(shared["type"], "file.shared")
        self.assertEqual(shared["detail"]["path"], "artifacts/证据.txt")
        self.assertEqual(shared["detail"]["by"], "user")

    def test_empty_session_does_not_create_database(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(read_deliverables(empty)["total"], 0)
        self.assertFalse((empty / "bb.db").exists())
