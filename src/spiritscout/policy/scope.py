"""授权范围守卫（设计书 9.3：目标范围白名单、边界校验、越界自动拦截）。

合规自查 G1：未提供授权范围时系统拒绝启动；所有工具调用与 HTTP 请求的
目标必须先过 ScopeGuard。判定顺序：禁打列表 > 白名单（域名/网段/URL 前缀）。
"""

from __future__ import annotations

import fnmatch
import ipaddress
from datetime import UTC, datetime
from urllib.parse import urlparse

from spiritscout.core.exceptions import ScopeViolation
from spiritscout.core.models import Scope


class ScopeGuard:
    def __init__(self, scope: Scope):
        self.scope = scope
        # 会话级敏感动作批准集：即时确认放行后写入，随守卫实例消亡（不落盘）
        self._approved_sensitive: set[str] = set()

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
        """校验任意目标字符串（URL/域名/IP）。通过返回规范化目标，否则抛 ScopeViolation。"""
        host, url = self._split(target)
        self._check_denied(target, host)
        if url and self._match_url(url):
            return target
        if host and self._match_host(host):
            return target
        raise ScopeViolation(f"目标超出授权范围: {target}")

    def check_url(self, url: str) -> str:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        if not parsed.hostname:
            raise ScopeViolation(f"无法解析的 URL: {url}")
        return self.check_target(url)

    def check_ip(self, ip: str) -> str:
        self._check_denied(ip, ip)
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
        """敏感操作门（G5）：反弹shell/上传getshell/容器逃逸/爆破默认禁用；
        启动授权（scope）或运行期即时确认放行（会话级批准集）其一即可。"""
        return (self.scope.allow_sensitive_actions
                or action in self._approved_sensitive)

    def approve_sensitive(self, action: str) -> None:
        """即时确认放行：本次会话内该动作不再询问（授权决策留痕在审批审计）。"""
        self._approved_sensitive.add(action)

    # ------------------------------------------------------------ 内部

    def _check_denied(self, raw: str, host: str | None) -> None:
        for pattern in self.scope.denied:
            if raw.startswith(pattern) or (host and fnmatch.fnmatchcase(host, pattern)):
                raise ScopeViolation(f"目标命中禁打列表 {pattern}: {raw}")

    def _match_host(self, host: str) -> bool:
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
        # 域名（含 *. 通配）
        for pattern in self.scope.allowed_domains:
            if fnmatch.fnmatchcase(host, pattern):
                return True
            if pattern.startswith("*.") and host == pattern[2:]:
                return True
        # URL 白名单中的主机也算在范围内
        for url in self.scope.allowed_urls:
            parsed = urlparse(url if "://" in url else f"http://{url}")
            if parsed.hostname and fnmatch.fnmatchcase(host, parsed.hostname):
                return True
        return False

    def _match_url(self, url: str) -> bool:
        return any(url.startswith(prefix) for prefix in self.scope.allowed_urls)

    @staticmethod
    def _split(target: str) -> tuple[str | None, str | None]:
        """拆出 (host, url)。纯域名/IP → (host, None)；URL → (host, url)。"""
        if "://" in target:
            parsed = urlparse(target)
            return parsed.hostname, target
        return target, None
