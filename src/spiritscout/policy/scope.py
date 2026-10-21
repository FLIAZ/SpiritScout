"""授权范围守卫（设计书 9.3：目标范围、边界校验、动态扩围）。

边界语义（用户裁定）：
- **禁打列表是唯一硬边界**——用户显式配置的拒绝目标，命中即拒；
- 授权白名单是**初始范围**而非运行期围栏：范围外目标照常可达，后端
  不做拦截，只在结果里标注「范围外」；是否扩大测试范围由模型向用户
  提问（问题与选项由模型撰写），用户确认后目标写入会话批准集
  （随守卫实例消亡，不落盘）——授权决定权始终在用户手里。
合规自查 G1：未提供授权范围时系统拒绝启动。
"""

from __future__ import annotations

import fnmatch
import ipaddress
import posixpath
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import unquote, urlparse

from spiritscout.core.exceptions import ScopeViolation
from spiritscout.core.models import Scope


class ScopeGuard:
    def __init__(self, scope: Scope):
        self.scope = scope
        # 会话级敏感动作批准集：即时确认放行后写入，随守卫实例消亡（不落盘）
        self._approved_sensitive: set[str] = set()
        # 会话级范围批准集：用户确认扩围后写入（URL 归一到 origin、主机归一到域）
        self._approved_targets: set[str] = set()

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
        """校验任意目标字符串（URL/域名/IP）：禁打 > 白名单/会话批准集。

        通过返回规范化目标，否则抛 ScopeViolation。运行期工具门只调用
        ensure_not_denied（禁打硬边界）；本方法供范围分类与导入过滤使用。
        """
        host, url = self._split(target)
        self._check_denied(target, host)
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

    def ensure_not_denied(self, target: str) -> None:
        """禁打列表硬校验（用户显式配置的拒绝边界）；白名单不在此拦。"""
        host = self._split(target)[0]
        self._check_denied(target, host)

    def ensure_addrs_not_denied(self, addrs: Iterable[str]) -> None:
        """DNS 解析出的连接地址同受禁打约束（以域名访问禁打 IP 不绕过）。

        异步工具在自行解析主机后调用（多 A/AAAA 逐个校验）；本方法只做
        确定性匹配，不发网络请求。调用方必须连接本次已校验的地址，
        不能在检查后另行解析域名。
        """
        for addr in addrs:
            addr = str(addr).strip()
            if addr:
                self._check_denied(addr, addr)

    def is_allowed(self, target: str) -> bool:
        """目标是否落在授权白名单/会话批准集内（信息标注用，不做拦截）。"""
        try:
            self.check_target(target)
            return True
        except ScopeViolation:
            return False

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

    def _check_denied(self, raw: str, host: str | None) -> None:
        addr = self._as_address(host)
        for pattern in self.scope.denied:
            pattern_host, pattern_url = self._split(pattern)
            url_denied = bool(
                pattern_url and self._url_within(raw, pattern_url))
            host_denied = bool(
                host and not pattern_url and pattern_host
                and fnmatch.fnmatchcase(
                    self._normalize_host(host),
                    self._normalize_host(pattern_host)))
            # 禁打条目按网段解释（IPv4/IPv6 CIDR 或裸 IP）：裸 IP 命中 /32、/128
            cidr_denied = bool(
                addr is not None and self._addr_in_network(addr, pattern))
            if url_denied or host_denied or cidr_denied:
                raise ScopeViolation(f"目标命中禁打列表 {pattern}: {raw}")

    @staticmethod
    def _addr_in_network(
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address, pattern: str,
    ) -> bool:
        """禁打条目 → 网段（CIDR 或裸 IP）成员判断；非网段写法不参与。"""
        try:
            network = ipaddress.ip_network(pattern.strip(), strict=False)
            # IPv4 映射的 IPv6 仍可能连接 IPv4 服务，不能绕过 IPv4 禁打项。
            mapped = addr.ipv4_mapped if isinstance(addr, ipaddress.IPv6Address) else None
            return addr in network or (mapped is not None and mapped in network)
        except ValueError:
            return False

    @staticmethod
    def _as_address(host: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        """主机字面量 → IP 地址对象；域名/空值返回 None（不做 DNS 解析）。"""
        if not host:
            return None
        try:
            return ipaddress.ip_address(str(host).strip().strip("[]"))
        except ValueError:
            return None

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
            if pattern.startswith("*.") and host == pattern[2:]:
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
