"""授权事实与范围标注：测试边界由模型在上下文中维护。

用户声明、项目规则与扩围答复交给模型理解，不在后端匹配禁打目标。
白名单与会话批准集仅用于范围标注；危险动作仍由独立审批通道逐次确认。
启动时仍要求提供目标或本地分析授权，不把空声明视为网络测试授权。
"""

from __future__ import annotations

import fnmatch
import ipaddress
import posixpath
from datetime import UTC, datetime
from urllib.parse import unquote, urlparse, urlunparse

from spiritscout.core.exceptions import ScopeViolation
from spiritscout.core.models import Scope


class ScopeGuard:
    def __init__(self, scope: Scope):
        self.scope = scope
        # 会话级敏感动作批准集：即时确认放行后写入，随守卫实例消亡（不落盘）
        self._approved_sensitive: set[str] = set()
        # 会话级范围批准集：用户确认扩围后写入（URL 归一到 origin、主机归一到域）
        self._approved_targets: set[str] = set()
        # 已授权页面直接引用的公开静态资源只继承“精确 URL 的只读分析”许可。
        # 它不等价于用户扩围，不能把 CDN origin、相邻路径或脚本里发现的接口放行。
        self._derived_readonly_resources: dict[str, str] = {}
        self._asset_decisions: dict[str, dict[str, object]] = {}

    # ------------------------------------------------------------ 启动门

    def assert_operable(self) -> None:
        """启动前硬校验：范围为空拒绝启动（G1）；时间窗口外拒绝启动。"""
        if self.scope.is_empty():
            raise ScopeViolation("授权范围为空（域名/网段/URL 均未配置），拒绝启动")
        now = datetime.now(UTC)
        tws, twe = self.scope.time_window_start, self.scope.time_window_end
        if tws and now < tws:
            raise ScopeViolation(f"当前时间在授权窗口开始之前（{tws.isoformat()}）")
        if twe and now > twe:
            raise ScopeViolation(f"授权时间窗口已结束（{twe.isoformat()}）")

    # ------------------------------------------------------------ 目标校验

    def check_target(self, target: str) -> str:
        """按初始范围/会话批准集分类，仅供标注与导入参考筛选。

        不读取排除声明、不参与工具执行门；测试边界由模型自行判断。
        """
        host, url = self._split(target)
        if url:
            # URL 授权必须命中 URL 前缀，或命中用户显式给出的域名范围。
            # allowed_urls 中出现某主机只代表允许解析该主机，不会把路径授权
            # 隐式扩大为整站授权。
            if (self._match_url(url) or (host and self._match_domain(host))
                    or self._match_approved(target, host)):
                return target
        elif host and (self._match_host(host) or self._match_approved(target, host)):
            return target
        raise ScopeViolation(f"目标超出授权范围: {target}")

    def register_asset_decision(
        self, target: str, status: str, *, reason: str = "", evidence: list[str] | None = None,
    ) -> str:
        """登记组织归属候选或资产级测试资格。"""
        allowed = {"candidate_only", "needs_review", "report_only", "allowed_test", "blocked"}
        if status not in allowed:
            raise ScopeViolation(f"未知资产范围状态: {status}")
        key = self._asset_key(target)
        self._asset_decisions[key] = {
            "status": status, "reason": str(reason)[:1000],
            "evidence": [str(item)[:500] for item in (evidence or [])[:20]],
        }
        return key

    def asset_decision(self, target: str) -> dict[str, object]:
        """返回资产级范围决定。未登记目标按当前 Scope 策略推导。"""
        key = self._asset_key(target)
        explicit = self._explicit_allowed(target)
        for pattern in [*self.scope.denied, *self.scope.excluded_assets]:
            if self._pattern_matches(target, pattern):
                return {"status": "blocked", "reason": "命中明确排除项", "evidence": []}
        if self.scope.scope_mode == "explicit":
            return {"status": "allowed_test" if explicit else "blocked",
                    "reason": "显式范围" if explicit else "未命中显式范围", "evidence": []}
        rule = next((item for item in self.scope.asset_rules
                     if isinstance(item, dict) and self._pattern_matches(target, str(item.get("pattern") or ""))), None)
        if rule is not None:
            return {"status": str(rule.get("status") or "needs_review"),
                    "reason": str(rule.get("reason") or "资产级规则"),
                    "evidence": list(rule.get("evidence") or []) if isinstance(rule.get("evidence"), list) else []}
        if key in self._asset_decisions:
            return dict(self._asset_decisions[key])
        if explicit:
            return {"status": "allowed_test", "reason": "显式范围", "evidence": []}
        if self._organization_match(target):
            return {"status": "candidate_only", "reason": "组织归属候选待核验", "evidence": []}
        return {"status": "blocked", "reason": "未命中项目或组织范围", "evidence": []}

    def check_execution_target(self, target: str) -> str:
        """主动测试入口使用的范围门。显式模式保持旧兼容行为。"""
        decision = self.asset_decision(target)
        if decision.get("status") != "allowed_test":
            raise ScopeViolation(
                f"资产当前不可主动测试: {target}（{decision.get('status')}，{decision.get('reason')}）"
            )
        return target

    def check_discovery_target(self, target: str) -> str:
        """被动归属和 DNS 发现可读取组织候选，不能因此获得主动测试资格。"""
        decision = self.asset_decision(target)
        if decision.get("status") == "blocked" and any(
                self._pattern_matches(target, pattern)
                for pattern in [*self.scope.denied, *self.scope.excluded_assets]):
            raise ScopeViolation(f"资产命中明确排除项: {target}")
        if self.scope.scope_mode == "explicit":
            return self.check_target(target)
        if decision.get("status") in {"blocked", "report_only"}:
            raise ScopeViolation(f"资产不可执行发现请求: {target}（{decision.get('reason')}）")
        return target

    def is_allowed(self, target: str) -> bool:
        """目标是否落在授权白名单/会话批准集内（信息标注用，不做拦截）。"""
        try:
            self.check_target(target)
            return True
        except ScopeViolation:
            return False

    @staticmethod
    def _asset_key(target: str) -> str:
        value = str(target or "").strip().lower().rstrip(".")
        return value or target

    def _explicit_allowed(self, target: str) -> bool:
        try:
            self.check_target(target)
            return True
        except ScopeViolation:
            return False

    def _organization_match(self, target: str) -> bool:
        host, url = self._split(target)
        if not host:
            return False
        patterns = [*self.scope.organization_domains]
        return any(fnmatch.fnmatchcase(self._normalize_host(host), self._normalize_host(pattern))
                   for pattern in patterns if pattern)

    def _pattern_matches(self, target: str, pattern: str) -> bool:
        if not pattern:
            return False
        host, url = self._split(target)
        if url and self._url_within(url, pattern):
            return True
        return bool(host and fnmatch.fnmatchcase(self._normalize_host(host), self._normalize_host(pattern)))

    def approve_target(self, target: str) -> str:
        """用户确认扩围：目标写入会话批准集并返回规范化条目。

        URL 归一到 origin 前缀（同意一个目标=同意该站点的这个服务），
        裸主机/域名按域批准；随会话消亡，不落盘。
        """
        target = str(target or "").strip()
        host, url = self._split(target)
        if url:
            parsed = urlparse(url)
            if not parsed.hostname:
                raise ScopeViolation(f"无法解析的扩围目标: {target}")
            entry = (f"{parsed.scheme.lower()}://{self._normalize_host(parsed.hostname)}")
            if parsed.port:
                entry += f":{parsed.port}"
            entry += "/"
        else:
            if not host:
                raise ScopeViolation(f"无法解析的扩围目标: {target}")
            entry = self._normalize_host(host)
        self._approved_targets.add(entry)
        return entry

    def approved_targets(self) -> frozenset[str]:
        """用户经提问确认的会话扩围目标（随守卫实例消亡，不落盘）。"""
        return frozenset(self._approved_targets)

    def approve_derived_resource(self, target: str, source_page: str) -> str:
        """登记授权页面明确引用的单个公开资源，返回精确规范化 URL。

        查询串不参与资源身份，避免同一带版本参数的文件产生重复授权；路径、
        origin 与端口必须精确匹配。调用方仍只能做 GET/离线分析，不能据此访问
        同域其他资源。source_page 必须属于原授权或已经登记的派生资源。
        """
        if not (self.is_allowed(source_page) or self.is_derived_resource(source_page)):
            raise ScopeViolation(f"派生资源来源页面未获授权: {source_page}")
        entry = self._exact_resource_url(target)
        if entry is None:
            raise ScopeViolation(f"无法解析的派生资源: {target}")
        self._derived_readonly_resources[entry] = str(source_page)[:2000]
        return entry

    def is_derived_resource(self, target: str) -> bool:
        """目标是否是已登记的精确只读派生资源。"""
        entry = self._exact_resource_url(target)
        return bool(entry and entry in self._derived_readonly_resources)

    def derived_readonly_resources(self) -> frozenset[str]:
        """用于规划校验和请求缓存身份的精确派生资源集合。"""
        return frozenset(self._derived_readonly_resources)

    def _match_approved(self, target: str, host: str | None) -> bool:
        """会话批准集匹配：origin 前缀条目按 URL 匹配，域条目按主机匹配。"""
        for entry in self._approved_targets:
            if "://" in entry:
                if self._url_within(target, entry):
                    return True
            elif host and (host == entry or fnmatch.fnmatchcase(
                    self._normalize_host(host), entry)):
                return True
        return False

    def check_url(self, url: str) -> str:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        if not parsed.hostname:
            raise ScopeViolation(f"无法解析的 URL: {url}")
        return self.check_target(url)

    def check_ip(self, ip: str) -> str:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError as exc:
            raise ScopeViolation(f"非法 IP: {ip}") from exc
        for cidr in self.scope.allowed_cidrs:
            try:
                if addr in ipaddress.ip_network(cidr, strict=False):
                    return ip
            except ValueError:
                continue
        raise ScopeViolation(f"IP 超出授权网段: {ip}")

    def is_sensitive_allowed(self, action: str) -> bool:
        """敏感操作门（G5）：反弹shell/上传getshell/容器逃逸/爆破/删除等
        默认禁绝；仅运行期即时确认放行过的动作（会话级批准集）免重复
        询问。启动参数不提供一揽子免审——授权决定权在每次触发时的用户
        确认，不在启动配置。"""
        return action in self._approved_sensitive

    @property
    def approved_sensitive(self) -> frozenset[str]:
        """会话内已即时批准的敏感动作（审批粒度：同会话同类动作免重复询问）。"""
        return frozenset(self._approved_sensitive)

    def approve_sensitive(self, action: str) -> None:
        """即时确认放行：本次会话内该动作不再询问（授权决策留痕在审批审计）。"""
        self._approved_sensitive.add(action)

    # ------------------------------------------------------------ 内部

    def _match_host(self, host: str) -> bool:
        host = self._normalize_host(host)
        # IP 走网段
        try:
            ipaddress.ip_address(host)
            try:
                self.check_ip(host)
                return True
            except ScopeViolation:
                return False
        except ValueError:
            pass
        if self._match_domain(host):
            return True
        # URL 白名单中的主机允许被 DNS 解析，但不因此获得整站 URL 权限。
        for url in self.scope.allowed_urls:
            parsed = urlparse(url if "://" in url else f"http://{url}")
            if parsed.hostname and host == self._normalize_host(parsed.hostname):
                return True
        return False

    def _match_domain(self, host: str) -> bool:
        """只匹配显式域名白名单，不读取 allowed_urls。"""
        host = self._normalize_host(host)
        for pattern in self.scope.allowed_domains:
            pattern = self._normalize_host(pattern)
            if fnmatch.fnmatchcase(host, pattern):
                return True
        return False

    def _match_url(self, url: str) -> bool:
        return any(self._url_within(url, prefix)
                   for prefix in self.scope.allowed_urls)

    @classmethod
    def _url_within(cls, url: str, prefix: str) -> bool:
        candidate = cls._url_parts(url)
        allowed = cls._url_parts(prefix)
        if candidate is None or allowed is None:
            return False
        c_scheme, c_host, c_port, c_path = candidate
        a_scheme, a_host, a_port, a_path = allowed
        if (c_scheme, c_host, c_port) != (a_scheme, a_host, a_port):
            return False
        return a_path == "/" or c_path == a_path or c_path.startswith(a_path + "/")

    @classmethod
    def _url_parts(cls, raw: str) -> tuple[str, str, int, str] | None:
        """URL → 可比较的 origin + 路径边界；查询串不扩大或缩小授权。"""
        parsed = urlparse(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        scheme = parsed.scheme.lower()
        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError:
            return None
        # 解码转义分隔符并归一化点路径，避免 /app/../admin 一类边界绕过。
        path = parsed.path or "/"
        # 多层解码防止 %252e%252e 在下游框架二次解码后越界。
        for _ in range(5):
            decoded = unquote(path)
            if decoded == path:
                break
            path = decoded
        path = path.replace("\\", "/")
        path = posixpath.normpath("/" + path.lstrip("/"))
        if path != "/":
            path = path.rstrip("/")
        return scheme, cls._normalize_host(parsed.hostname), port, path

    @classmethod
    def _exact_resource_url(cls, raw: str) -> str | None:
        parts = cls._url_parts(str(raw or ""))
        if parts is None:
            return None
        scheme, host, port, path = parts
        default = 443 if scheme == "https" else 80
        netloc = host if port == default else f"{host}:{port}"
        return urlunparse((scheme, netloc, path, "", "", ""))

    @staticmethod
    def _normalize_host(host: str) -> str:
        value = str(host or "").strip().rstrip(".").lower()
        if value.startswith("*."):
            return "*." + value[2:].encode("idna").decode("ascii")
        try:
            return value.encode("idna").decode("ascii")
        except UnicodeError:
            return value

    @staticmethod
    def _split(target: str) -> tuple[str | None, str | None]:
        """拆出 (host, url)。纯域名/IP → (host, None)；URL → (host, url)。"""
        if "://" in target:
            parsed = urlparse(target)
            return parsed.hostname, target
        return target, None
