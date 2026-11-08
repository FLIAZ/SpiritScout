"""主控管理扫描者：待命、显式派遣、撤回及版本化指令，执行仍为独立异步任务。"""
from __future__ import annotations

import asyncio
import copy
from typing import Any

from spiritscout.core.models import AgentType, BlackboardLayer
from spiritscout.tools.spec import ToolResult

TOOLS = ('nuclei_scan', 'metasploit_scan', 'http_probe')
GUIDANCE = (
    '扫描者「探微」由你管理，待命不代表获准扫描。结合用户原话和最新规则判断是否允许扫描；'
    '禁止扫描、仅被动分析或范围不明时不要派遣。需要扫描时在既有决策 JSON 中增加 '
    'scan_control:{"action":"dispatch","revision":scanner.revision,"reason":"依据用户规则的派遣理由",'
    '"target":"本次授权目标","goal":"具体信息收集目标","tools":["nuclei_scan"],'
    '"constraints":"目标边界、禁止动作等限制"}，按需选择 nuclei_scan/metasploit_scan/http_probe。'
    '派遣后与其他任务并行，不要重复创建扫描 DAG 节点。工具部署状态见 scanner，未预检不等于可用。'
    'scan_control 的 action 还支持 pause/resume/stop/forbid/allow；每次必须携带最新 revision 和 reason。'
    'pause 撤回在途扫描并保留证据，resume 让扫描者基于原任务与现有证据重新判断，不自动重放工具；'
    'stop 撤回本次派遣；forbid 禁止扫描工具，直到依据用户后续明确许可执行 allow，之后仍须重新 dispatch。'
    '不允许扫描时应明确 forbid，不能仅在回复里说停止。新用户消息会先挂起扫描，必须重新判断：'
    '普通提问且原许可仍有效可 resume，限制变化用 dispatch 更新任务，明确禁止用 forbid。'
    '工具操作仍沿用现有审批，不因派遣而扩围或豁免危险动作审批。'
    '遗漏 scan_control 不会自动启动或恢复扫描；控制回执以 scanner.last_result 为准，不声称未执行的操作成功。'
)


class ScannerController:
    def __init__(self, infra, assembly, *, mission: str, standing_rules: str, dialogue_history=None):
        self.infra, self.assembly = infra, assembly
        self.mission, self.rules, self.history = mission, standing_rules, dialogue_history
        saved = infra.blackboard.read(BlackboardLayer.TASK_STATE, 'scanner-control') or {}
        self.revision = saved['revision'] + 1 if type(saved.get('revision')) is int else 0
        self.forbidden = saved.get('forbidden') is True
        self.status = 'forbidden' if self.forbidden else 'idle'
        self.permitted = False
        self.assignment: dict[str, Any] = {}
        self.task: asyncio.Task | None = None
        self.worker_id = ''
        self.available: dict[str, Any] = {}
        self.calls: set[asyncio.Task] = set()
        self.last_result: dict[str, Any] = {}
        self.lock = asyncio.Lock()
        self.closed = False
        self.user_batch = ''
        self._publish()

    def snapshot(self) -> dict[str, Any]:
        worker = self.infra.blackboard.read(BlackboardLayer.TASK_STATE, self.worker_id) if self.worker_id else None
        return {'status': self.status, 'revision': self.revision, 'forbidden': self.forbidden,
                'permitted': self.permitted, 'assignment': copy.deepcopy(self.assignment),
                'worker': worker or {}, 'available_tools': dict(self.available),
                'capabilities': {'nuclei_scan': '选择本地模板检测，保存原始请求/响应和命中',
                                 'metasploit_scan': '模块 info/check 或辅助模块 run，不执行 exploit',
                                 'http_probe': '按派遣目标补充 HTTP 环境观察'},
                'deployment': 'not_checked' if not self.available else 'checked',
                'last_result': dict(self.last_result), 'guidance': GUIDANCE}

    def _publish(self):
        self.infra.blackboard.write(BlackboardLayer.TASK_STATE, 'scanner-control',
            {'kind': 'scanner_control', 'status': self.status, 'revision': self.revision,
             'forbidden': self.forbidden, 'assignment': copy.deepcopy(self.assignment),
             'worker_id': self.worker_id, 'last_result': dict(self.last_result)},
            agent_type=AgentType.MASTER, updated_by='master')

    async def _cancel(self):
        from spiritscout.agents.scanner import close_scanner

        self.permitted = False
        for task in list(self.calls):
            task.cancel()
        await close_scanner(self.task)
        self.task = None
        if self.calls:
            await asyncio.gather(*list(self.calls), return_exceptions=True)

    async def hold_for_user(self, batch: str):
        """不猜测用户话意：任何新指导先阻断扫描，交主控本次已有对话决策重新裁定。"""
        async with self.lock:
            if self.closed or batch == self.user_batch:
                return
            self.user_batch = batch
            await self._cancel()
            self.revision += 1
            self.status = 'forbidden' if self.forbidden else 'paused' if self.assignment else 'idle'
            self.last_result = {'ok': True, 'action': 'user_hold', 'reason': '新用户消息待主控重新核对扫描许可'}
            self._publish()

    async def apply(self, command: Any, *, actor: str) -> dict[str, Any]:
        async with self.lock:
            action = command.get('action') if isinstance(command, dict) else None
            error = ''
            if not isinstance(command, dict) or action not in {'dispatch', 'pause', 'resume', 'stop', 'forbid', 'allow'}:
                error = '无效扫描控制指令'
            elif type(command.get('revision')) is not int or command['revision'] != self.revision:
                error = '扫描控制版本已变化，按最新状态重新决定；未执行旧指令'
            elif self.closed:
                error = '扫描发现分支已收尾'
            elif not isinstance(command.get('reason'), str) or not command['reason'].strip():
                error = '扫描控制必须说明依据'
            elif action in {'dispatch', 'resume'} and self.forbidden:
                error = '扫描已禁止；须依据用户后续许可先 allow，再重新派遣'
            elif action == 'resume' and (not self.assignment or self.status != 'paused'):
                error = '没有可恢复的暂停扫描任务'
            elif action == 'dispatch':
                if (not all(isinstance(command.get(k), str) and command[k].strip() for k in ('target', 'goal'))
                        or not isinstance(command.get('tools'), list) or not command['tools']
                        or any(tool not in TOOLS for tool in command['tools'])
                        or not isinstance(command.get('constraints', ''), str)):
                    error = '派遣需提供 target、goal、合法 tools 及文字 constraints'
            if error:
                result = {'ok': False, 'action': action, 'error': error, 'revision': self.revision}
            else:
                await self._cancel()
                self.revision += 1
                if action == 'dispatch':
                    self.assignment = {k: copy.deepcopy(command.get(k, ''))
                                       for k in ('target', 'goal', 'tools', 'constraints', 'reason')}
                if action == 'forbid':
                    self.forbidden = True
                elif action == 'allow':
                    self.forbidden = False
                    self.assignment = {}
                self.status = {'dispatch': 'running', 'resume': 'running', 'pause': 'paused',
                               'stop': 'stopped', 'forbid': 'forbidden', 'allow': 'idle'}[action]
                if action in {'dispatch', 'resume'}:
                    from spiritscout.agents.scanner import _run

                    self.permitted = True
                    assignment = copy.deepcopy(self.assignment)
                    self.task = asyncio.create_task(_run(self.infra, self.assembly, assignment['target'],
                        mission=self.mission, standing_rules=self.rules, dialogue_history=self.history,
                        delegation=assignment, control=self), name='scanner')
                result = {'ok': True, 'action': action, 'revision': self.revision, 'status': self.status}
            self.last_result = result
            self._publish()
            self.infra.audit.log('scanner.control', actor, {**result,
                'reason': command.get('reason', '') if isinstance(command, dict) else ''})
            return result

    async def run_tool(self, name: str, execute):
        if not self.permitted or self.forbidden or name not in self.assignment.get('tools', []):
            return ToolResult(tool=name, ok=False, returncode=-3,
                error='主控尚未许可本扫描工具或扫描已暂停/停止/禁止；不能绕过派遣限制')
        task = asyncio.create_task(execute())
        self.calls.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            return ToolResult(tool=name, ok=False, returncode=-1,
                error='主控已撤回在途扫描，已产生的证据保留')
        finally:
            self.calls.discard(task)

    async def finish_discovery(self):
        async with self.lock:
            self.closed = True
            from spiritscout.agents.scanner import close_scanner

            await close_scanner(self.task)
            self.task = None
            self.status = 'finishing'
            self._publish()

    async def close(self):
        async with self.lock:
            self.closed = True
            await self._cancel()
            self.status = 'closed'
            self._publish()
