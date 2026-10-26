"""工作台旁路入口、模式限制、实例归属和独立执行链路。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import (
    AgentType,
    Authorization,
    BlackboardLayer,
    RunMode,
    Scope,
)
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.runtime import run_agent_side_session
from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session
from spiritscout.web.workbench import side_session_source


class SideSessionTests(unittest.TestCase):
    def test_api_rejects_competition_and_forged_actor_and_creates_independent_src_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            app=create_app(SystemConfig(workspace=WorkspaceConfig(root=tmp)), [])
            manager=app.state.manager
            parent=Session(session_id='sess-parent', mode='src', target='https://a.test', workspace=root/'sess-parent')
            parent.workspace.mkdir()
            parent.plan=prepare_launch(LaunchRequest(mode=RunMode.SRC,target='https://a.test',mission='原任务'))
            parent.standing_rules='只读核实，不删除文件'
            manager._sessions[parent.session_id]=parent
            bb=Blackboard(parent.bb_path)
            task={'task_id':'t1','task_type':'recon','agent_type':'recon','target':'https://a.test',
                  'payload':{'rules':'原始约定','node_title':'来源任务'}}
            bb.write(BlackboardLayer.TASK_STATE,'t1',{'task':task,'routes':[{'name':'路线','notes':['已发现入口']}]},updated_by='recon-real')
            bb.write(BlackboardLayer.TARGET_AUTH,'user-context',{'statements':[{'text':'不测生产'}]},updated_by='user')
            bb.close()
            created=Session(session_id='sess-child',mode='src',target=parent.target,workspace=root/'child')
            manager.create=AsyncMock(return_value=created)
            with TestClient(app,base_url='http://127.0.0.1') as client:
                url=f'/api/sessions/{parent.session_id}/agent-sessions'
                request={'actor':'recon-real','task_id':'t1','mission':'独立核实权限差异'}
                parent.mode='competition'
                self.assertEqual(client.post(url,json=request).status_code,422)
                manager.create.assert_not_awaited()
                parent.mode='src'
                self.assertEqual(client.post(url,json={**request,'actor':'recon-forged'}).status_code,422)
                self.assertEqual(client.post(url,json={**request,'mode':'competition'}).status_code,422)
                self.assertEqual(client.post(url,json={**request,'mission':'   '}).status_code,422)
                result=client.post(url,json=request)
                self.assertEqual(result.status_code,201,result.text)
                payload,plan=manager.create.call_args.args
                self.assertEqual(payload['side_agent'],'recon')
                self.assertEqual(payload['standing_rules'],parent.standing_rules)
                self.assertEqual(payload['side_context']['user_context']['statements'][0]['text'],'不测生产')
                self.assertEqual(plan.authorization.scope,parent.plan.authorization.scope)
                self.assertFalse(plan.needs_mode_routing)
                self.assertEqual(parent.plan.mission,'原任务')
                self.assertNotEqual(result.json()['session_id'],parent.session_id)
                self.assertEqual(side_session_source(parent,'recon-real','t1')['agent'],'recon')
                parent.mode='ctf'
                with self.assertRaises(SpiritScoutError):
                    side_session_source(parent,'recon-real','t1')  # 角色模式不兼容

    def test_ctf_specialist_can_start_side_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent=Session(session_id="parent",mode="ctf",target="https://challenge.test",workspace=Path(tmp))
            bb=Blackboard(parent.bb_path)
            try:
                bb.write(BlackboardLayer.TASK_STATE,"ctf-task",{"task":{
                    "task_id":"ctf-task","agent_type":"ctf_web","target":parent.target,
                    "payload":{"challenge":{"id":"web-1"}}}},updated_by="ctf-web-real")
                context=side_session_source(parent,"ctf-web-real","ctf-task")
                self.assertEqual(context["agent"],"ctf_web")
                self.assertEqual(context["task"]["payload"]["challenge"]["id"],"web-1")
            finally:
                bb.close()


class SideRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_agent_uses_normal_approval_and_context_without_master_or_parent_tool_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb=Blackboard(Path(tmp)/'bb.db')
            close=AsyncMock()
            gate=AsyncMock()
            infra=SimpleNamespace(blackboard=bb,workspace=Path(tmp),audit=NullAuditLogger(),bus=None,
                                  gateway=SimpleNamespace(usage_report=lambda:{'calls':0}),close=close)
            agent=SimpleNamespace(agent_id='recon-side',run=AsyncMock(return_value={'done':True}),
                _llm_chat_json=AsyncMock(side_effect=[{'reply':'我先核实','execute':True},{'reply':'已核实当前证据'}]))
            source={'task_id':'parent-task','routes':[{'name':'r','notes':['原始证据']}],
                    'task':{'task_type':'recon','target':'https://a.test',
                            'payload':{'rules':'原约定','continue_from':{'task_id':'old'}}}}
            try:
                with patch('spiritscout.runtime._build_infra',AsyncMock(return_value=infra)) as build, patch(
                    'spiritscout.runtime._make_assembler',return_value=SimpleNamespace(assemble=Mock(return_value=SimpleNamespace(agents={AgentType.RECON:agent})))):
                    result=await run_agent_side_session(cfg=SystemConfig(),models=[],
                        authorization=Authorization(auth_id='auth',mode=RunMode.SRC,scope=Scope()),
                        agent_key='recon',mission='新任务',context=source,agent_models={},approval_gate=gate,
                        question_broker=None,ring=None,pause_gate=None,dialogue_history=[],standing_rules='只读')
                    self.assertIs(build.call_args.kwargs['approval_gate'],gate)
                    task=agent.run.call_args.args[0]
                    self.assertEqual(task.payload['node_title'],'新任务')
                    self.assertNotIn('continue_from',task.payload)
                    self.assertIn('原约定',task.payload['rules'])
                    self.assertIn('只读',task.payload['rules'])
                    self.assertEqual(task.payload['source_context']['routes'][0]['notes'],['原始证据'])
                    self.assertEqual(result.run_summary['side_agent'],'recon')
                    self.assertEqual(agent._llm_chat_json.await_count,2)
                    close.assert_awaited_once()
            finally:
                bb.close()

    async def test_competition_runtime_is_rejected_before_infra_is_built(self) -> None:
        with patch('spiritscout.runtime._build_infra',AsyncMock()) as build:
            with self.assertRaises(SpiritScoutError):
                await run_agent_side_session(cfg=SystemConfig(),models=[],
                    authorization=Authorization(auth_id='a',mode=RunMode.COMPETITION,scope=Scope()),
                    agent_key='ctf_web',mission='任务',context={},agent_models={},approval_gate=None,
                    question_broker=None,ring=None,pause_gate=None,dialogue_history=[])
            build.assert_not_awaited()


class SideConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dialogue_reply_returns_without_tools_and_keeps_previous_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb=Blackboard(Path(tmp)/'bb.db')
            audit=Mock()
            agent=SimpleNamespace(agent_id='recon-dialogue', run=AsyncMock(),
                _llm_chat_json=AsyncMock(return_value={'reply':'我们刚才在核实登录入口，不需要重新扫描。','execute':False}))
            infra=SimpleNamespace(blackboard=bb,workspace=Path(tmp),audit=audit,bus=None,
                gateway=SimpleNamespace(usage_report=lambda:{}),close=AsyncMock())
            history=[{'role':'user','content':'先核实登录入口'},
                     {'role':'assistant','content':'已发现 /login'},
                     {'role':'user','content':'刚才查到哪里了？'}]
            replies=[]
            try:
                with patch('spiritscout.runtime._build_infra',AsyncMock(return_value=infra)), patch(
                    'spiritscout.runtime._make_assembler',return_value=SimpleNamespace(assemble=Mock(return_value=SimpleNamespace(agents={AgentType.RECON:agent})))):
                    await run_agent_side_session(cfg=SystemConfig(),models=[],
                        authorization=Authorization(auth_id='a',mode=RunMode.SRC,scope=Scope()),
                        agent_key='recon',mission='刚才查到哪里了？',context={},agent_models={},
                        approval_gate=None,question_broker=None,ring=None,pause_gate=None,
                        dialogue_history=history,on_reply=replies.append)
                agent.run.assert_not_awaited()
                self.assertEqual(agent._llm_chat_json.call_args.kwargs['user_obj']['dialogue_history'],history)
                self.assertEqual(len(replies),1)
                self.assertIn('登录入口',replies[0])
                reply_event=next(call for call in audit.log.call_args_list if call.args[0]=='agent.reply')
                self.assertEqual(reply_event.args[1],'recon-dialogue')
                self.assertTrue(reply_event.args[2]['final'])
                self.assertTrue(agent._llm_chat_json.call_args.kwargs['_allow_ask'] is False)
            finally:
                bb.close()


class SideConversationPersistenceTests(unittest.TestCase):
    def test_two_rounds_store_user_and_agent_messages_and_resume_as_same_agent(self) -> None:
        import time

        from spiritscout.runtime import EngagementResult
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            models=root/'models.yaml'
            models.write_text('models:\n  - model: fixture\n    provider: mock\n')
            cfg=SystemConfig(workspace=WorkspaceConfig(root=tmp),models_config_path=str(models))
            app=create_app(cfg,[])
            session=Session(session_id='sess-side',mode='src',target='https://a.test',workspace=root/'sessions'/'sess-side',
                status='completed',request={'side_agent':'recon','side_context':{}},
                launch_request={'mode':'src','target':'https://a.test','mission':'原问题'})
            session.workspace.mkdir(parents=True)
            session.plan=prepare_launch(LaunchRequest(mode=RunMode.SRC,target=session.target,mission='原问题'))
            app.state.manager._sessions[session.session_id]=session
            histories=[]
            async def answer(**kwargs):
                histories.append(kwargs['dialogue_history'])
                text=f"子 Agent 第 {len(histories)} 轮回复"
                AuditLogger(kwargs['cfg'].audit_log_path).log('agent.reply','recon-side',{'text':text,'name':'侦察子 Agent'})
                kwargs['on_reply'](text)
                return EngagementResult(run_summary={},verify_stats={},report_path=None)
            from spiritscout.core.audit import AuditLogger
            with patch('spiritscout.runtime.run_agent_side_session',side_effect=answer), TestClient(app,base_url='http://127.0.0.1') as client:
                for number in (1,2):
                    response=client.post('/api/sessions/sess-side/messages',json={'content':f'用户第 {number} 轮消息'})
                    self.assertEqual(response.status_code,200,response.text)
                    deadline=time.monotonic()+3
                    while time.monotonic()<deadline:
                        if session.status=='completed' and len(session.dialogue_history)==number*2:
                            break
                        time.sleep(.01)
                    self.assertEqual(len(session.dialogue_history),number*2)
                self.assertEqual([m['role'] for m in session.dialogue_history],['user','assistant','user','assistant'])
                self.assertEqual(histories[1][1]['content'],'子 Agent 第 1 轮回复')
                conversation=client.get('/api/sessions/sess-side/conversation')
                self.assertEqual(conversation.status_code,200)
                self.assertEqual(conversation.json()['messages'],session.dialogue_history)
                self.assertEqual(conversation.json()['pending_approvals'],[])
                self.assertNotIn('request',conversation.json())
                session.request={}
                self.assertEqual(client.get('/api/sessions/sess-side/conversation').status_code,422)
                saved=__import__('json').loads(session.meta_path.read_text())
                self.assertEqual(saved['dialogue_history'],session.dialogue_history)
