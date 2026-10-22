"""工具错误的机械诊断：前置真实失败原因，不替模型决定恢复动作。"""

from __future__ import annotations

import re

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def clean_tool_text(text: str) -> str:
    """移除终端着色，保留正文、定位器及逐步调用日志。"""
    return _ANSI.sub("", text)


def browser_failure(tool: str, text: str) -> dict[str, str]:
    """只依据工具原文分类；未知超时不能推测成页面遮挡或点击已完成。"""
    if not tool.startswith("browser_"):
        return {}
    lower = text.lower()
    reasons = [
        ("element is outside of the viewport", "target_outside_viewport",
         "目标元素已找到，但位于可视区域外；自动滚动后仍无法点击。"),
        ("intercepts pointer events", "target_obscured",
         "目标元素的点击被其他页面元素遮挡，未确认完成。"),
        ("not attached to the dom", "stale_target",
         "目标引用已失效或元素已从页面移除，需要重新观察当前页面。"),
        ("not found in the current page snapshot", "stale_target",
         "目标引用已失效或元素已从页面移除，需要重新观察当前页面。"),
        ("element is not stable", "target_unstable",
         "目标元素持续变化，等待稳定超时；未确认完成点击。"),
    ]
    # 等待日志可能先动画、后遮挡；取最后一次实际记录的阻塞条件。
    observed = [(lower.rfind(marker), kind, summary) for marker, kind, summary in reasons
                if marker in lower]
    if observed:
        _, kind, summary = max(observed)
        return {"failure_kind": kind, "failure_summary": summary}
    if "timeout" in lower:
        return {"failure_kind": "browser_timeout",
                "failure_summary": "浏览器操作等待超时；现有日志不足以确认具体阻塞原因或动作是否完成。"}
    return {}


def browser_tool_guidance(tool: str) -> str:
    """说明浏览器定位契约；恢复路径由模型依据现场自行选择。"""
    if tool == "browser_click":
        return (" 页面快照中存在元素不代表它当前可点击；引用和坐标应来自最新页面状态。"
                "轮播非当前项可能在视口外。失败诊断是本次观察结果，需据当前页面重新判断；"
                "不要把超时当作成功，不要无变化地重复相同引用。")
    return ""


def chrome_tool_guidance(tool: str) -> str:
    """可见共享浏览器的协作说明，是否请用户接手由模型按当前页面决定。"""
    if tool == "browser_close":
        return " 此连接复用用户的可见浏览器；结束测试时保留浏览器及配置档案，只关闭本次新建的测试标签。"
    if tool in {"browser_navigate", "browser_cookie_list", "browser_cookie_get"}:
        return (" 此连接操作用户可见的现有浏览器，与用户共享页面和登录态。"
                "若需要用户登录、输入验证码或拖动滑块，使用 ask_user 说明具体页面和操作，"
                "等待用户答复期间不要继续操作该页面；答复后重新读取页面或 Cookie 验证结果，"
                "不要假定登录成功，不代替用户破解验证码。多条路线不要同时操作同一标签。"
                "Cookie（包括 HttpOnly）只在本次任务需要时读取，不写入共享项目记忆或报告。")
    return ""
