"""扫描证据按原字节交付、候选门与访问边界；不调用付费模型。"""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import ScannerConfig
from spiritscout.core.models import AgentType, BlackboardLayer, RunMode, Scope
from spiritscout.policy import PolicyEngine, ScopeGuard
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.metasploit import SPEC, MetasploitScanner, resource_script
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.scan_evidence import read_finding, submit_candidate
from spiritscout.tools.scanner import NucleiScanner
from spiritscout.tools.spec import ToolResult
from spiritscout.web.deliverables import read_vulnerability_page
from spiritscout.web.file_routes import mount_artifact_routes


class ScanEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.bb=Blackboard(self.root/'bb.db')
        self.addAsyncCleanup(self.bb.aclose)
        self.config=ScannerConfig()
        self.request='GET /record/2 HTTP/1.1\r\nX-Fixture: exact\r\n\r\n'
        self.response='HTTP/1.1 200 OK\r\n\r\n' + 'fixture evidence\n'*1500
        self.row={'template-id':'fixture-only','matched-at':'http://fixture.test/record/2',
            'info':{'name':'fixture','severity':'high','classification':{'cve-id':['CVE-fixture-only'],'cwe-id':['CWE-fixture']}},
            'request':self.request,'response':self.response,'curl-command':"curl 'http://fixture.test/record/2'"}

    async def scan(self):
        async def execute(command, **ctx):
            import shlex
            args=shlex.split(command)
            Path(args[args.index('-o')+1]).write_text(json.dumps(self.row)+'\n')
            return ToolResult(tool='sandbox_exec',ok=True)
        with patch('spiritscout.tools.scanner.sandbox_exec',side_effect=execute):
            result=await NucleiScanner(self.config,self.bb).run(['http://fixture.test'],template_ids=['fixture-only'],
                workspace_root=str(self.root),guard=ScopeGuard(Scope(allowed_urls=['http://fixture.test'])))
        self.assertTrue(result.ok)
        return result.parsed['preview'][0]

    async def test_original_packets_poc_and_ids_survive_candidate_and_api(self):
        hit=await self.scan()
        self.assertEqual(self.bb.query(BlackboardLayer.VULN),[],'命中不自动生成漏洞结论')
        hypothesis_ref='topology:hypothesis-fixture@3'
        finding=self.bb.read(BlackboardLayer.RECON_CLUE,hit['finding_id'])
        self.bb.write(BlackboardLayer.RECON_CLUE,hit['finding_id'],
            {**finding,'hypothesis_ref':hypothesis_ref})
        files=hit['scan_evidence']['files']
        self.assertEqual((self.root/files['request']).read_bytes(),self.request.encode())
        self.assertEqual((self.root/files['response']).read_bytes(),self.response.encode())
        page=read_finding(self.bb,self.root,hit['finding_id'],'response',0,100)
        self.assertTrue(page['has_more'])
        self.assertEqual(page['next_offset'],100)
        result=submit_candidate(self.bb,self.root,{'finding_id':hit['finding_id'],'title':'候选',
            'vuln_type':'idor','reason':'需要确认越权','poc_verified':True,'status':'accepted'})
        stored=self.bb.read(BlackboardLayer.VULN,result['vuln_id'])
        self.assertEqual(stored['status'],'candidate')
        self.assertFalse(stored['poc_verified'])
        self.assertEqual(stored['vulnerability_ids'],['CVE-fixture-only','CWE-fixture'])
        self.assertEqual(stored['hypothesis_ref'],hypothesis_ref)
        self.assertEqual(stored['hypothesis_refs'],[hypothesis_ref])
        page=read_vulnerability_page(self.root)
        self.assertEqual(page['items'][0]['hypothesis_ref'],hypothesis_ref)
        self.assertEqual(page['items'][0]['hypothesis_refs'],[hypothesis_ref])
        app=FastAPI()
        def require(session):
            if session!='one':
                raise HTTPException(404)
            return SimpleNamespace(workspace=self.root)
        mount_artifact_routes(app,require)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
            detail=await client.get('/api/sessions/one/vulnerabilities/'+result['vuln_id'])
            self.assertEqual(detail.status_code,200)
            self.assertEqual(detail.json()['data']['scanner_evidence'][0]['files'],files)
            self.assertEqual(detail.json()['data']['hypothesis_ref'],hypothesis_ref)
            self.assertEqual(detail.json()['data']['hypothesis_refs'],[hypothesis_ref])
            response=await client.get('/api/sessions/one/artifacts/download',params={'path':files['response']})
            self.assertEqual(response.content,self.response.encode())
            self.assertEqual((await client.get('/api/sessions/other/vulnerabilities/'+result['vuln_id'])).status_code,404)

    async def test_missing_real_hit_cannot_be_submitted(self):
        with self.assertRaises(ValueError):
            submit_candidate(self.bb,self.root,{'finding_id':'invented','title':'fake','vuln_type':'rce','reason':'fake'})

    async def test_missing_packets_are_explicit_not_fabricated(self):
        self.row.pop('request')
        self.row.pop('response')
        self.row.pop('curl-command')
        hit=await self.scan()
        self.assertEqual(hit['scan_evidence']['missing'],['request','response','poc'])

    async def test_metasploit_values_are_data_and_exploit_run_is_not_available(self):
        scanner=MetasploitScanner(self.config,self.bb)
        with patch('spiritscout.tools.metasploit.sandbox_exec',new_callable=AsyncMock) as backend:
            for module,options,operation in [('exploit/test',{},'run'),('auxiliary/scanner/http/version;exit',{},'check'),
                ('auxiliary/scanner/http/http_version',{'RHOSTS':'one\nexit'},'run')]:
                result=await scanner.run(module,options,operation,workspace_root=str(self.root))
                self.assertFalse(result.ok)
            backend.assert_not_awaited()
        script=resource_script('auxiliary/scanner/http/http_version',{'RHOSTS':"literal'; exit"},'info','result.json')
        encoded=script.split("decode64('",1)[1].split("')",1)[0]
        self.assertEqual(json.loads(base64.b64decode(encoded))['options']['RHOSTS'],"literal'; exit")

    async def test_wait_revision_ignores_own_clues(self):
        from spiritscout.agents.scanner import external_revision
        revision=external_revision(self.bb)
        self.bb.write(BlackboardLayer.RECON_CLUE,'own',{},agent_type=AgentType.SCANNER,updated_by='scanner')
        self.assertEqual(external_revision(self.bb),revision)
        self.bb.write(BlackboardLayer.ASSET,'external',{'value':'fixture'},agent_type=AgentType.RECON,updated_by='recon')
        self.assertNotEqual(external_revision(self.bb),revision)

    async def test_src_denied_metasploit_action_does_not_start_process(self):
        approval=AsyncMock(return_value=False)
        guard=ScopeGuard(Scope(allowed_urls=['http://fixture.test']))
        registry=ToolRegistry(LocalExecutor(),policy=PolicyEngine(RunMode.SRC,guard,approval_gate=approval))
        registry.register_builtin(SPEC.name,MetasploitScanner(self.config,self.bb).run,SPEC)
        with patch('spiritscout.tools.metasploit.sandbox_exec',new_callable=AsyncMock) as backend:
            result=await registry.call(SPEC.name,{'module':'auxiliary/scanner/http/http_version',
                'options':{'RHOSTS':'fixture.test'},'operation':'run','action':'brute_force'},
                workspace_root=str(self.root),guard=guard,_purpose='测试审批拒绝时不执行模块')
            self.assertFalse(result.ok)
            backend.assert_not_awaited()
        approval.assert_awaited_once()
        self.assertEqual(list(self.root.glob('artifacts/scans/*')),[])


if __name__=='__main__':
    unittest.main()
