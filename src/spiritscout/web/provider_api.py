"""用户请求时读取供应商模型列表。

按供应商协议选择接口；不重试、不缓存，失败时返回已尝试的地址和错误。"""

from __future__ import annotations

from typing import Any

import httpx

_TIMEOUT = 20.0
_ANTHROPIC_VERSION = "2023-06-01"

# 厂商预设目录（Web「添加供应商」对话框：选中即带入协议与地址，
# 填入后全部字段仍可手改；「自定义」项由前端提供，不在此列）。
# base_url 以各厂商 2026-10 现行接入文档为准（国内站优先；国际站域名不同者
# 在名称中注明，用户可按需手改）。
# brand/mark 是前端徽章图标用的品牌色与短标识字符（零外部资源，不引用任何
# 厂商官方 logo 图片）。
PROVIDER_PRESETS: list[dict[str, str]] = [
    # ---- 国内厂商 ----
    {"key": "ark", "name": "火山方舟", "provider": "openai_compatible",
     "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
     "brand": "#f53f3f", "mark": "火"},
    {"key": "deepseek", "name": "DeepSeek", "provider": "openai_compatible",
     "base_url": "https://api.deepseek.com", "brand": "#4d6bfe", "mark": "DS", "billing_scheme": "deepseek_api"},
    {"key": "qwen", "name": "阿里百炼", "provider": "openai_compatible",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "brand": "#7c5cfc", "mark": "百"},
    {"key": "zhipu", "name": "智谱", "provider": "openai_compatible",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "billing_scheme": "glm_api",
     "brand": "#2f54eb", "mark": "智"},
    {"key": "moonshot", "name": "Kimi（Moonshot）", "provider": "openai_compatible",
     "base_url": "https://api.moonshot.cn/v1", "brand": "#1a1a1a", "mark": "K", "billing_scheme": "kimi_api"},
    {"key": "kimi-code", "name": "Kimi Code / Token Plan", "provider": "openai_compatible",
     "base_url": "https://api.kimi.com/coding/v1", "brand": "#1a1a1a", "mark": "K", "billing_scheme": "kimi_token_plan"},
    {"key": "minimax", "name": "MiniMax", "provider": "openai_compatible",
     "base_url": "https://api.minimax.cn/v1", "brand": "#6554f0", "mark": "M"},
    {"key": "qianfan", "name": "百度千帆", "provider": "openai_compatible",
     "base_url": "https://qianfan.baidubce.com/v2", "brand": "#306eff", "mark": "帆"},
    {"key": "hunyuan", "name": "腾讯混元", "provider": "openai_compatible",
     "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
     "brand": "#0052d9", "mark": "混"},
    {"key": "stepfun", "name": "阶跃星辰", "provider": "openai_compatible",
     "base_url": "https://api.stepfun.com/v1", "brand": "#101010", "mark": "阶"},
    {"key": "siliconflow", "name": "硅基流动", "provider": "openai_compatible",
     "base_url": "https://api.siliconflow.cn/v1", "brand": "#12a57c", "mark": "硅"},
    # ---- 国际厂商 ----
    {"key": "openai", "name": "OpenAI", "provider": "openai_compatible",
     "base_url": "https://api.openai.com/v1", "brand": "#10a37f", "mark": "O"},
    {"key": "anthropic", "name": "Anthropic", "provider": "anthropic_compatible",
     "base_url": "https://api.anthropic.com", "brand": "#d97757", "mark": "A"},
    {"key": "gemini", "name": "Google Gemini", "provider": "openai_compatible",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "brand": "#4b8bf5", "mark": "G"},
    {"key": "xai", "name": "xAI", "provider": "openai_compatible",
     "base_url": "https://api.x.ai/v1", "brand": "#1d1f23", "mark": "X"},
    {"key": "groq", "name": "Groq", "provider": "openai_compatible",
     "base_url": "https://api.groq.com/openai/v1", "brand": "#f55036", "mark": "Q"},
    {"key": "mistral", "name": "Mistral", "provider": "openai_compatible",
     "base_url": "https://api.mistral.ai/v1", "brand": "#ff7000", "mark": "Mi"},
    {"key": "openrouter", "name": "OpenRouter", "provider": "openai_compatible",
     "base_url": "https://openrouter.ai/api/v1", "brand": "#6366f1", "mark": "OR"},
    {"key": "ollama", "name": "Ollama（本地）", "provider": "openai_compatible",
     "base_url": "http://127.0.0.1:11434/v1", "brand": "#3c3c3c", "mark": "Ol"},
]

# 同一厂商的接入版本独立保存，前端按 family 展开二级菜单。
# 目录只描述端点，不承诺该账户具备订阅权益或公开 OAuth。
# 端点交叉参考官方文档与 CC Switch 的 claude/codexProviderPresets（2026-11-07）。
_FAMILIES = {p['key']: p for p in PROVIDER_PRESETS}
for _p in PROVIDER_PRESETS:
    _p.update(family=_p['key'], family_name=_p['name'], edition='按量 API')
_FAMILIES['ark'].update(edition='Agent Plan', billing_scheme='other_token_plan', quota_query='ark_plan')
_FAMILIES['kimi-code'].update(family='moonshot', family_name='Kimi', edition='Token Plan · 国内')
_FAMILIES['moonshot'].update(family_name='Kimi', edition='按量 API · 国内')
_FAMILIES['qwen'].update(family_name='千问 / 阿里百炼')
_FAMILIES['ollama'].update(edition='本地', provider='ollama')


def _variant(key: str, family: str, edition: str, base_url: str, *, billing: str = 'manual',
             quota: str = 'auto', protocol: str = 'openai_compatible') -> None:
    parent = _FAMILIES[family]
    PROVIDER_PRESETS.append({'key': key, 'name': f"{parent['family_name']} · {edition}",
        'family': family, 'family_name': parent['family_name'], 'edition': edition,
        'provider': protocol, 'base_url': base_url, 'billing_scheme': billing, 'quota_query': quota,
        'brand': parent['brand'], 'mark': parent['mark']})


for _key, _name, _url, _mark, _color in [
    ('astron', '讯飞星辰 Astron', 'https://maas-api.cn-huabei-1.xf-yun.com/v1', '讯', '#ee7638'),
    ('bailing', '蚂蚁百灵', 'https://api.ant-ling.com/v1', '灵', '#527bd9'),
    ('byteplus', 'BytePlus', 'https://ark.ap-southeast.bytepluses.com/api/v3', 'B', '#3370ff'),
    ('longcat', '美团 LongCat', 'https://api.longcat.chat/openai/v1', 'L', '#21b873'),
    ('meta', 'Meta Model API', 'https://api.meta.ai/anthropic', '∞', '#0866ff'),
    ('qwencloud', 'QwenCloud', 'https://ap-southeast-1.maas.qwencloudapi.com/compatible-mode/v1', 'Q', '#6336e7'),
    ('streamlake', '快手万擎 StreamLake', 'https://wanqing.streamlakeapi.com/api/gateway/v1/endpoints', '万', '#2587cf'),
    ('mimo', '小米 MiMo', 'https://api.xiaomimimo.com/v1', '米', '#f27922'),
    ('opencode', 'OpenCode', 'https://opencode.ai/zen/v1', 'OC', '#555555'),
]:
    _p = {'key': _key, 'name': _name, 'family': _key, 'family_name': _name, 'edition': '按量 API',
          'base_url': _url, 'provider': 'anthropic_compatible' if _key == 'meta' else 'openai_compatible',
          'brand': _color, 'mark': _mark}
    # Meta 的 Messages 路径为 /v1/messages，基址不额外拼 /anthropic。
    if _key == 'meta':
        _p['base_url'] = 'https://api.meta.ai'
    PROVIDER_PRESETS.append(_p)
    _FAMILIES[_key] = _p

for _key, _family, _edition, _url, _billing, _quota in [
    ('ark-api', 'ark', '按量 API', 'https://ark.cn-beijing.volces.com/api/v3', 'manual', 'auto'),
    ('ark-coding', 'ark', 'Coding Plan', 'https://ark.cn-beijing.volces.com/api/coding/v3', 'other_token_plan', 'ark_plan'),
    ('zhipu-plan', 'zhipu', 'Token Plan · 国内', 'https://open.bigmodel.cn/api/coding/paas/v4', 'glm_token_plan', 'glm_plan'),
    ('zai-plan', 'zhipu', 'Coding Plan · 国际', 'https://api.z.ai/api/coding/paas/v4', 'other_token_plan', 'glm_plan'),
    ('kimi-api-global', 'moonshot', '按量 API · 国际', 'https://api.moonshot.ai/v1', 'manual', 'moonshot_balance'),
    ('kimi-code-global', 'moonshot', 'Token Plan · 国际', 'https://api.kimi.ai/coding/v1', 'other_token_plan', 'kimi_plan'),
    ('minimax-plan', 'minimax', 'Token Plan · 国内', 'https://api.minimax.cn/v1', 'other_token_plan', 'minimax_plan'),
    ('minimax-api-global', 'minimax', '按量 API · 国际', 'https://api.minimax.io/v1', 'manual', 'auto'),
    ('minimax-plan-global', 'minimax', 'Coding Plan · 国际', 'https://api.minimax.io/v1', 'other_token_plan', 'minimax_plan'),
    ('qwen-coding', 'qwen', 'Coding Plan', 'https://coding.dashscope.aliyuncs.com/v1', 'other_token_plan', 'auto'),
    ('qwen-token', 'qwen', 'Token Plan', 'https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1', 'other_token_plan', 'auto'),
    ('qwencloud-coding', 'qwencloud', 'Coding Plan', 'https://coding-intl.dashscope.aliyuncs.com/v1', 'other_token_plan', 'auto'),
    ('qwencloud-token', 'qwencloud', 'Token Plan', 'https://token-plan.maas.qwencloudapi.com/compatible-mode/v1', 'other_token_plan', 'auto'),
    ('qianfan-coding', 'qianfan', 'Coding Plan', 'https://qianfan.baidubce.com/v2/coding', 'other_token_plan', 'auto'),
    ('qianfan-token', 'qianfan', 'Token Plan', 'https://qianfan.baidubce.com/v2/tokenplan/personal', 'other_token_plan', 'auto'),
    ('tencent-plan', 'hunyuan', 'Token Plan · 个人版', 'https://api.lkeap.cloud.tencent.com/plan/v3', 'other_token_plan', 'auto'),
    ('tencent-enterprise', 'hunyuan', 'Token Plan · 企业版', 'https://tokenhub.tencentmaas.com/plan/v3', 'other_token_plan', 'auto'),
    ('stepfun-plan', 'stepfun', 'Step Plan · 国内', 'https://api.stepfun.com/step_plan/v1', 'other_token_plan', 'stepfun_balance'),
    ('stepfun-global', 'stepfun', '按量 API · 国际', 'https://api.stepfun.ai/v1', 'manual', 'stepfun_balance'),
    ('stepfun-plan-global', 'stepfun', 'Step Plan · 国际', 'https://api.stepfun.ai/step_plan/v1', 'other_token_plan', 'stepfun_balance'),
    ('siliconflow-global', 'siliconflow', '按量 API · 国际', 'https://api.siliconflow.com/v1', 'manual', 'siliconflow_balance'),
    ('astron-coding', 'astron', 'Coding Plan', 'https://maas-coding-api.cn-huabei-1.xf-yun.com/v1', 'other_token_plan', 'auto'),
    ('byteplus-coding', 'byteplus', 'Coding Plan', 'https://ark.ap-southeast.bytepluses.com/api/coding/v3', 'other_token_plan', 'auto'),
    ('streamlake-global', 'streamlake', '按量 API · 国际', 'https://vanchin.streamlake.ai/api/gateway/v1/endpoints', 'manual', 'auto'),
    ('mimo-token', 'mimo', 'Token Plan · 国内', 'https://token-plan-cn.xiaomimimo.com/v1', 'other_token_plan', 'auto'),
    ('opencode-go', 'opencode', 'Go 订阅', 'https://opencode.ai/inference/go/v1', 'other_token_plan', 'opencode_plan'),
]:
    _variant(_key, _family, _edition, _url, billing=_billing, quota=_quota)


def _candidate_urls(base_url: str, provider: str) -> list[str]:
    """按协议给出候选地址（去重保序）：先官方文档记载的，再常见变体。

    - anthropic_compatible：Anthropic 的 ``GET /v1/models`` 默认只回 20 条，
      官方参数 limit 可取全量（上限 1000），故先带 limit、再不带、再裸 /models
      （部分中转只实现裸路径）。
    - 阿里百炼：官方文档化的列表接口是 DashScope 原生
      ``GET {origin}/api/v1/models``（响应在 output.models[]）；兼容模式
      ``{base}/models`` 官方文档未记载，作为后备候选。
    - 其余 OpenAI 兼容各家：官方文档统一为 ``{base}/models``。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return []
    urls: list[str] = []
    if provider == "ollama":
        origin = base[:-3] if base.endswith('/v1') else base
        urls += [f"{origin}/api/tags"]
    elif provider == "anthropic_compatible":
        urls += [f"{base}/v1/models?limit=1000", f"{base}/v1/models", f"{base}/models"]
    elif "dashscope" in base or ".maas.aliyuncs.com" in base:
        origin = "/".join(base.split("/")[:3])       # scheme://host
        urls += [f"{origin}/api/v1/models", f"{base}/models"]
    else:
        urls += [f"{base}/models"]
        if not base.endswith(("/v1", "/v2", "/v3")):
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
    """统一解析各家模型列表（形状按官方文档与实测核实）：

    - OpenAI 兼容各家 / Anthropic：``data[]``，条目取 id；
    - 阿里百炼原生：``output.models[]``，条目以 model 为 ID、name 为显示名；
    - 智谱 Response 基址（/api/v1）：``models[]``，条目以 slug 为 ID；
    - Ollama 原生：``models[]``，条目 name/model 同为模型名。
    """
    if not isinstance(payload, dict):
        return []
    items = payload.get("data")
    if not isinstance(items, list):
        items = payload.get("models") if isinstance(payload.get("models"), list) else []
    if not items:
        out = payload.get("output")
        if isinstance(out, dict) and isinstance(out.get("models"), list):
            items = out["models"]
    result: list[dict[str, str]] = []
    for it in items:
        if isinstance(it, str):
            result.append({"id": it, "display_name": ""})
        elif isinstance(it, dict):
            mid = str(it.get("id") or it.get("model") or it.get("slug")
                      or it.get("name") or "").strip()
            if mid:
                result.append({"id": mid,
                               "display_name": str(it.get("display_name")
                                                   or it.get("name") or "")})
    return result


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
    error = "；".join(errors)[:400] or "端点未返回可用模型列表"
    if errors and all(": HTTP 404" in e for e in errors):
        # 所有候选都是 404 = 该端点没有模型列表接口（如方舟 plan 端点、混元）
        error = "该端点未提供模型列表接口"
    return {"ok": False, "tried": urls, "error": error}
