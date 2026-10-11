"""供应商端点的模型列表拉取（Web「模型」页：获取模型列表）。

主流兼容端点都提供模型列表接口（OpenAI 的 ``GET /models``、Anthropic 的
``GET /v1/models``），但并非所有中转/代理端点都实现——拉不到不是异常，
如实回报试过的地址与错误，用户改用手动填模型 ID 即可（同一入口）。
本模块只做一次显式外呼（用户点击触发），不缓存、不重试。
"""

from __future__ import annotations

from typing import Any

import httpx

_TIMEOUT = 20.0
_ANTHROPIC_VERSION = "2023-06-01"

# 厂商预设目录（Web「添加供应商」对话框：选中即带入协议/地址/建议环境变量名，
# 填入后全部字段仍可手改；「自定义」项由前端提供，不在此列）。
# key_env 为该厂商通行的环境变量名，仅作占位符建议，不含任何密钥。
# base_url 以各厂商 2026-10 现行接入文档为准（国内站优先；国际站域名不同者
# 在名称中注明，用户可按需手改）。
PROVIDER_PRESETS: list[dict[str, str]] = [
    # ---- 国内厂商 ----
    {"key": "ark", "name": "火山方舟", "provider": "openai_compatible",
     "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
     "key_env": "SPIRITSCOUT_LLM_API_KEY"},
    {"key": "deepseek", "name": "DeepSeek", "provider": "openai_compatible",
     "base_url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY"},
    {"key": "qwen", "name": "阿里百炼", "provider": "openai_compatible",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "key_env": "DASHSCOPE_API_KEY"},
    {"key": "zhipu", "name": "智谱", "provider": "openai_compatible",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "key_env": "ZHIPU_API_KEY"},
    {"key": "moonshot", "name": "Kimi（Moonshot）", "provider": "openai_compatible",
     "base_url": "https://api.moonshot.cn/v1", "key_env": "MOONSHOT_API_KEY"},
    {"key": "minimax", "name": "MiniMax", "provider": "openai_compatible",
     "base_url": "https://api.minimax.cn/v1", "key_env": "MINIMAX_API_KEY"},
    {"key": "qianfan", "name": "百度千帆", "provider": "openai_compatible",
     "base_url": "https://qianfan.baidubce.com/v2", "key_env": "QIANFAN_API_KEY"},
    {"key": "hunyuan", "name": "腾讯混元", "provider": "openai_compatible",
     "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
     "key_env": "HUNYUAN_API_KEY"},
    {"key": "stepfun", "name": "阶跃星辰", "provider": "openai_compatible",
     "base_url": "https://api.stepfun.com/v1", "key_env": "STEP_API_KEY"},
    {"key": "siliconflow", "name": "硅基流动", "provider": "openai_compatible",
     "base_url": "https://api.siliconflow.cn/v1", "key_env": "SILICONFLOW_API_KEY"},
    # ---- 国际厂商 ----
    {"key": "openai", "name": "OpenAI", "provider": "openai_compatible",
     "base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
    {"key": "anthropic", "name": "Anthropic", "provider": "anthropic_compatible",
     "base_url": "https://api.anthropic.com", "key_env": "ANTHROPIC_API_KEY"},
    {"key": "gemini", "name": "Google Gemini", "provider": "openai_compatible",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "key_env": "GEMINI_API_KEY"},
    {"key": "xai", "name": "xAI", "provider": "openai_compatible",
     "base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY"},
    {"key": "groq", "name": "Groq", "provider": "openai_compatible",
     "base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    {"key": "mistral", "name": "Mistral", "provider": "openai_compatible",
     "base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY"},
    {"key": "openrouter", "name": "OpenRouter", "provider": "openai_compatible",
     "base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY"},
    {"key": "ollama", "name": "Ollama（本地）", "provider": "openai_compatible",
     "base_url": "http://127.0.0.1:11434/v1", "key_env": ""},
]


def _candidate_urls(base_url: str, provider: str) -> list[str]:
    """按协议给出候选地址（去重保序）：先最可能的，再常见变体。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return []
    urls: list[str] = []
    if provider == "anthropic_compatible":
        urls += [f"{base}/v1/models", f"{base}/models"]
    else:
        urls += [f"{base}/models"]
        if not base.endswith(("/v1", "/v3")):
            urls += [f"{base}/v1/models"]
    out: list[str] = []
    for u in urls:
        if u not in out:
            out.append(u)
    return out


def _headers(provider: str, api_key: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if not api_key:
        return headers
    if provider == "anthropic_compatible":
        # Anthropic 用 x-api-key；部分中转同时认 Bearer，两个都带以兼容
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = _ANTHROPIC_VERSION
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _parse(payload: Any) -> list[dict[str, str]]:
    """统一解析：OpenAI 与 Anthropic 的列表都在 data[]，条目取 id/display_name。"""
    if not isinstance(payload, dict):
        return []
    items = payload.get("data")
    if not isinstance(items, list):
        items = payload.get("models") if isinstance(payload.get("models"), list) else []
    out: list[dict[str, str]] = []
    for it in items:
        if isinstance(it, str):
            out.append({"id": it, "display_name": ""})
        elif isinstance(it, dict):
            mid = str(it.get("id") or it.get("name") or "").strip()
            if mid:
                out.append({"id": mid,
                            "display_name": str(it.get("display_name")
                                                or it.get("name") or "")})
    return out


async def fetch_model_list(spec: dict[str, Any]) -> dict[str, Any]:
    """拉取供应商可用模型列表。

    spec: {name, provider, base_url, api_key}（api_key 已解析为明文，仅内存）。
    返回 {ok, models: [{id, display_name}], url} 或 {ok: False, error, tried}。
    """
    provider = str(spec.get("provider") or "openai_compatible")
    urls = _candidate_urls(str(spec.get("base_url") or ""), provider)
    if not urls:
        return {"ok": False, "error": "供应商未配置 base_url", "tried": []}
    headers = _headers(provider, str(spec.get("api_key") or ""))
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url, headers=headers)
            except Exception as exc:  # noqa: BLE001 —— 逐个候选地址试，失败如实上报
                errors.append(f"{url}: {type(exc).__name__}: {exc}"[:200])
                continue
            if resp.status_code >= 400:
                errors.append(f"{url}: HTTP {resp.status_code} {resp.text[:160]}")
                continue
            try:
                models = _parse(resp.json())
            except ValueError:
                errors.append(f"{url}: 响应不是 JSON")
                continue
            if not models:
                errors.append(f"{url}: 响应里没有模型条目")
                continue
            return {"ok": True, "url": url, "models": models}
    return {"ok": False, "tried": urls,
            "error": "；".join(errors)[:400] or "端点未返回可用模型列表"}
