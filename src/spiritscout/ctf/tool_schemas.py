"""CTF 原生工具目录，保留模型可见的工具顺序和完整定义。"""
from __future__ import annotations

from typing import Any

from spiritscout.agents.scan_control import SCHEMA as SCANNER_CONTROL_SCHEMA
from spiritscout.blackboard.exchange import SCHEMAS
from spiritscout.ctf.ai_tools import SCHEMA as AI_SCHEMA
from spiritscout.resources.runtime import SKILL_SCHEMAS
from spiritscout.tools.background import SCHEMAS as BACKGROUND_SCHEMAS
from spiritscout.tools.catalog import MCP_PROXY_SCHEMAS
from spiritscout.tools.cve import SCHEMA as CVE_SCHEMA
from spiritscout.tools.cyberchef import SCHEMAS as CYBERCHEF_SCHEMAS
from spiritscout.tools.http_responses import SCHEMA as RESPONSE_SCHEMA
from spiritscout.tools.spec import ActionCategory
from spiritscout.tools.web_search import SEARCH_SCHEMA
from spiritscout.traffic.tools import SCHEMAS as TRAFFIC_SCHEMAS

_HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


def _fn(name: str, description: str, properties: dict[str, Any],
        required: list[str] | None = None) -> dict[str, Any]:
    """OpenAI 形态的函数工具 schema（网关统一转译到各协议）。"""
    if "action" in properties:
        properties = {**properties, "action": {
            **properties["action"], "enum": [a.value for a in ActionCategory],
        }}
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       **({"required": required} if required else {})}}}


CTF_TOOL_SCHEMAS = [
        *SKILL_SCHEMAS,
        *SCHEMAS,
        *MCP_PROXY_SCHEMAS,
        RESPONSE_SCHEMA,
        *TRAFFIC_SCHEMAS,
        *BACKGROUND_SCHEMAS,
        SCANNER_CONTROL_SCHEMA,
        *CYBERCHEF_SCHEMAS,
        _fn("steering_feedback", "向灵枢回报本任务指令的处理情况；accepted=说明下一步计划，"
            "completed=给出实际动作与证据，blocked=说明无法执行的事实原因。回执不代替实际操作。",
            {"instruction_id": {"type": "string"},
             "status": {"type": "string", "enum": ["accepted", "completed", "blocked"]},
             "detail": {"type": "string", "description": "计划、执行证据或阻塞原因"}},
            ["instruction_id", "status", "detail"]),
        _fn("http_request",
            "向赛题目标发一次 HTTP 请求并拿到响应（状态码/响应头/正文摘要）。"
            "GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS 全部支持——表单提交、"
            "接口调用、登录动作都用它：method 指定方法，data 放请求体"
            "（表单串按 x-www-form-urlencoded 发，JSON 原文按 application/json "
            "发；要别的类型或精确控制时用 headers 显式给 Content-Type）。"
            "path 传站内相对路径（推荐），或 url 传完整地址（须在授权范围内）。"
            "响应原文保存在会话内，返回 body_ref 与完整性状态；长正文仅预览。"
            "需要完整代码或中段线索时用 http_response_read 本地读取或搜索，不要重复抓取或依赖服务端 Range。",
            {
                "method": {"type": "string", "enum": list(_HTTP_METHODS),
                           "description": "HTTP 方法，全部可用；缺省 GET，"
                                          "提交表单/调用接口时用 POST"},
                "path": {"type": "string",
                         "description": "站内路径，如 /api/v1/users"},
                "url": {"type": "string",
                        "description": "完整 URL（与 path 二选一）"},
                "params": {"type": "object", "description": "查询参数（键值对）"},
                "headers": {"type": "object", "description": "额外请求头（键值对）"},
                "data": {"type": "string", "description": "请求体原文（POST/PUT 用）"},
                "refresh": {"type": "boolean",
                            "description": "true=对该地址重新发起真实请求（页面"
                            "状态变化、时序对照或独立复核时用）；缺省 false——仅精确同请求在短有效期内可复用，返回保留原执行来源"},
                "action": {"type": "string",
                           "description": "本次请求的行为类别（如实自报）。"
                           "GET/HEAD/OPTIONS 纯读取可不填；POST/PUT/PATCH/"
                           "DELETE 这类有副作用的请求必填：analysis=只是"
                           "查询/读取；intrusive_write=写入/上传/增删改数据；"
                           "brute_force=爆破；webshell_upload=上传 WebShell。"
                           "危险类别（删除/爆破/反弹连接/WebShell/逃逸）会"
                           "即时请示用户；其余如实注明即可。"
                           "缺声明的副作用请求会被退回。"},
            }),
        _fn("shell",
            "在隔离沙箱里执行一条 shell 命令（bash -c），拿到输出与退出码。"
            "适合 HTTP 交互、socket 通信、文件检索与处理等命令行动作；"
            "具体命令以实际执行为准，不确定时先 `command -v <name>` 探测；"
            "工作目录在会话内持久（脚本与产物落那里，跨回合直接可用；"
            "/tmp 每次运行即焚），系统文件只读，联网仅限授权目标。命令全文"
            "留痕于审计。每次调用"
            "必须用 action 如实声明这条命令要做什么——审计与审批以它为准。",
            {
                "command": {"type": "string", "description": "要执行的命令"},
                "action": {"type": "string",
                           "description": "本次执行的行为类别（如实自报，缺了"
                           "不执行）：analysis=只读分析/读取/计算/解码；"
                           "delete_files=删除文件；reverse_shell=反弹 shell；"
                           "webshell_upload=上传 WebShell；container_escape="
                           "容器/沙箱逃逸尝试；brute_force=爆破；"
                           "intrusive_write=其他有副作用的写入/上传。删除/"
                           "爆破/反弹连接/WebShell/逃逸会即时请示用户，每次"
                           "单独批准；其余如实注明即可。"},
                "timeout": {"type": "number",
                            "description": "超时秒数（缺省 60，上限 300）"},
            }, required=["command", "action"]),
        _fn("python",
            "在隔离沙箱里执行一段 Python 代码（python3），拿到输出与退出码。"
            "适合 WebSocket/socket 协议交互、数据计算与解码、脚本化构造请求；"
            "具体有哪些库以实际导入结果为准——不确定时先 `python3 -c "
            "'import <模块>'` 或 `pip list | grep -i <关键词>` 查看，"
            "不要凭记忆假设。"
            "工作目录会话内持久，产物跨回合直接可用（/tmp 每次运行即焚）；"
            "产物也可用附件分析工具继续研究。每次调用必须用 action 如实"
            "声明这段代码要做什么——审计与审批以它为准。",
            {
                "code": {"type": "string", "description": "完整 Python 代码"},
                "action": {"type": "string",
                           "description": "本次执行的行为类别（如实自报，缺了"
                           "不执行）：analysis=只读分析/读取/计算/解码；"
                           "delete_files=删除文件；reverse_shell=反弹 shell；"
                           "webshell_upload=上传 WebShell；container_escape="
                           "容器/沙箱逃逸尝试；brute_force=爆破；"
                           "intrusive_write=其他有副作用的写入/上传。删除/"
                           "爆破/反弹连接/WebShell/逃逸会即时请示用户，每次"
                           "单独批准；其余如实注明即可。"},
                "timeout": {"type": "number",
                            "description": "超时秒数（缺省 60，上限 300）"},
            }, required=["code", "action"]),
        _fn("analyze_text",
            "对题面文本做编码链解码（Base64/Hex/URL/Rot13 递归）与 Flag 提取。"
            "怀疑线索藏在题面或编码串里时用它；已经分析过就不必再来。",
            {}),
        _fn("analyze_attachments",
            "对全部附件做魔数识别 + 字符串提取 + Flag 提取。有附件时先跑它，"
            "方向工具（checksec/algo_hints/deep_strings）依赖它的结果。",
            {}),
        _fn("note",
            "把关键发现写进团队共享笔记——同题并行工作的其他方向 Agent 下一"
            "回合就能看到（如「/api/v1/admin 需要 adm_token」「终端接口是 "
            "POST /api/exec，命令放 cmd 参数」）。只记录有依据的事实，不登记执行步骤或写文件行为。",
            {"text": {"type": "string", "description": "要共享的事实"},
             "evidence_refs": {"type": "array", "items": {"type": "string"},
                               "description": "已有工具调用 ID、文件或黑板记录引用"}},
            required=["text", "evidence_refs"]),
        _fn("send_file",
            "把产出文件发送到会话界面，用户可直接在线预览（Markdown 会渲染成"
            "文档）或下载到本机。产出统一放在 artifacts/ 下（exp 脚本、字典、"
            "解题报告等）；凡需要用户查看或保存的文件就发过去，不要只留在"
            "工作区里。",
            {"path": {"type": "string",
                      "description": "artifacts/ 内的文件路径，如 artifacts/exp.py"},
             "note": {"type": "string",
                      "description": "一句话说明文件是什么（可选，展示在文件卡片上）"}},
            required=["path"]),
        _fn("asset_memory_lookup", "读取同一主机的跨会话历史观测；历史不代表当前事实，不含凭据或授权。",
            {"host": {"type": "string"}, "query": {"type": "string"}, "offset":{"type":"integer","minimum":0}}, required=["host"]),
        SEARCH_SCHEMA,
        CVE_SCHEMA,
        AI_SCHEMA,
        _fn("kb_search",
            "检索本地经验知识库：敏感路径/备份文件清单、PHP 弱类型等平台特性"
            "事实、模板注入 canary、隐写与提权检查点、本机工具面与字典位置、"
            "攻击路径模板与核验方法。遇到不熟悉的平台特性、不确定本地有哪些"
            "工具或字典、想核对常见检查点时用它；它是参考资料，"
            "采纳与否由你基于现场证据判断。",
            {"query": {"type": "string",
                       "description": "检索词，如：备份文件 / PHP 弱类型 / "
                                      "隐写 / 提权 / 模板注入 / 工具面"}},
            required=["query"]),
        _fn("ask_user",
            "向用户提问并等待答复。遇到授权范围外的目标、拿不准的路线分叉、"
            "需要用户拍板的取舍时用它：问题用你自己的话写清「看到了什么、"
            "为什么想继续、不同选择意味着什么」；options 给 2~4 个你建议的"
            "选项（用户也可以自由输入其他意见）。索取账号、密码、验证码、"
            "Token、Cookie 或请求记录时，直接要求用户在当前提问卡输入；"
            "options 只保留无法提供或改走其他方案等即时决策，不要生成稍后"
            "提供、准备好再给之类承诺选项；targets 填你希望纳入测试"
            "范围的目标，形如 [{\"target\": \"主机或 URL\", \"why\": \"一句话"
            "说明它是什么、为什么需要（面向普通用户）\"}]——用户勾选后系统会"
            "记录扩围；已有任务授权覆盖的平台动态靶机无需重复确认，不能仅因地址"
            "未匹配初始列表就提问。只列真正超出已有授权的新目标。一次性把"
            "问题问清楚，不要反复追问。",
            {"blocking_task_ids": {"type": "array", "items": {"type": "string"},
                                   "description": "依赖答复的任务 ID；默认只等待当前任务"},
             "block_all_tasks": {"type": "boolean", "description": "共同前置条件或用户要求全队等待时设 true"},
             "question": {"type": "string",
                          "description": "要问用户的问题（自己组织语言）"},
             "options": {"type": "array", "items": {"type": "string"},
                         "description": "建议选项（2~4 个，用户可自由输入）"},
             "multiple": {"type": "boolean",
                          "description": ("选项可多选：仅当选项彼此不互斥、"
                                          "用户可能同时选多项时设 true"
                                          "（如「接下来优先看哪些方向」"
                                          "「需要开通哪些账号/环境」）；"
                                          "「允许/拒绝」这类互斥决策保持 "
                                          "false。多选选项要写成可独立"
                                          "成立的动作或结论")},
             "targets": {"type": "array",
                         "items": {"type": "object",
                                   "properties": {
                                       "target": {"type": "string",
                                                  "description": "主机或 URL"},
                                       "why": {"type": "string",
                                               "description": "面向普通用户的一句话说明"}},
                                   "required": ["target"]},
                         "description": "希望纳入本会话范围的新目标（含一句话说明）"}},
            required=["question"]),
        _fn("submit_flag",
            "把你判定为本题 Flag 的值提交（判定权在你：依据题面给定的格式与"
            "你拿到的证据）。后端只做登记、去重与赛方接口提交，应答（对/错/"
            "未判定）会回灌给你；同一个值不要重复提交。",
            {"flag": {"type": "string",
                      "description": "Flag 原文（照抄证据里的值，不要加工）"},
             "note": {"type": "string",
                      "description": "判定依据（一句话：来源与推导）"}},
            required=["flag"]),
        _fn("spawn",
            "派生一个分支执行者去独立推进一个子问题，其最终结论会回传给你。"
            "当前分支会等待其收尾；可独立推进的扫描请用 scanner_control，不占用主线等待。"
            "brief 写清：为什么需要这个分支、它专属的任务是什么、解决什么"
            "问题、与当前工作的边界（它不做什么）；expect 写清期望回传的"
            "结论。各司其职：子问题值得独立推进时才派，顺手能做的不要派。",
            {"brief": {"type": "string",
                       "description": "分支任务简报（为什么派/做什么/边界）"},
             "expect": {"type": "string",
                        "description": "期望回传的结论"}},
            required=["brief"]),
    ]
