"""知识库种子数据（设计书第三章 + 附录B/C 原文落地，3.5 引用规范）。

- 全部条目带 source + last_verified（调研日期 2026-10-09 / 代码镜像 2026-10-10）；
- 官方来源内容标【已查证】、社区经验标【经验总结】（与原文标注约定一致）；
- ATT&CK 27 行映射逐行取自附录B-三（真实 ID，以 attack.mitre.org 为准）；
- 6 类攻击路径模板取自附录B-四（含分支决策点与核验方法）；
- 7 平台 SRC 规则取自附录C-一（含 1.2 合并/降级/红线细则）；
- P0~P4 分级取自附录C-2.1/2.2，权重按附录C-5.2（P0=10,P1=7,P2=4,P3=1,P4=0）；
- 10 类挖洞模板取自附录C-三（含合规红线）；
- 报告六段式与驳回原因库取自附录C-4.2/4.4。
"""

from __future__ import annotations

from typing import Any

SEED_VERSION = "2026-10-11.k1"

V_ATTACK = "2026-10-09"   # 附录B 调研日期
V_SRC = "2026-10-09"      # 附录C 调研日期
V_CODE = "2026-10-10"     # 代码镜像日期

SRC_ATTACK = "设计书附录B-三（attack.mitre.org 官方页实际访问 2026-10-09）"


def seed_rows() -> dict[str, list[dict[str, Any]]]:
    """返回 {表名: 行列表}（JSON 列保持 Python 对象，由 store 序列化）。"""
    return {
        "base_assets": _assets(),
        "base_vuln_standards": _platform_rules(),
        "base_fp_signatures": _fp_signatures(),
        "base_poc_templates": _poc_templates(),
        "base_competition_rules": _competition_rules(),
        "exp_attack_map": _attack_map(),
        "exp_redteam_entries": _redteam_entries(),
        "exp_attack_paths": _attack_paths(),
        "exp_vuln_priority": _vuln_priority(),
        "exp_hunt_templates": _hunt_templates(),
        "exp_report_templates": _report_templates(),
    }


# ================================================================ ATT&CK 27 行映射


def _attack_map() -> list[dict[str, Any]]:
    # (id, 战术ID, 战术名, 技术ID, 技术名, 子技术, 一句话操作, 常用工具, 对应子Agent)
    raw = [
        (1, "TA0043", "Reconnaissance", "T1595", "Active Scanning", "-",
         "主动扫描目标网段/端口", "nmap, masscan, amass", "侦察 Agent"),
        (2, "TA0043", "Reconnaissance", "T1594", "Search Open Website/Domains", "-",
         "搜索引擎/Dork 收集子域、邮箱、员工", "Google dorks, theHarvester", "侦察 Agent"),
        (3, "TA0001", "Initial Access", "T1190", "Exploit Public-Facing Application", "-",
         "利用公网 Web 漏洞 RCE", "nuclei, Burp, exp 脚本", "Web 漏洞 Agent"),
        (4, "TA0001", "Initial Access", "T1133", "External Remote Services", "-",
         "利用 VPN/Citrix/暴露的 RDP 弱口令", "hydra, xrdp", "侦察/Web Agent"),
        (5, "TA0001", "Initial Access", "T1566", "Phishing", ".001/.002/.003",
         "钓鱼附件/链接/经第三方", "gophish, 宏, LNK", "钓鱼 Agent（授权内）"),
        (6, "TA0001", "Initial Access", "T1078", "Valid Accounts", ".001-.004",
         "使用泄露/默认/域/云账户登录", "任意合法服务", "侦察/后渗透"),
        (7, "TA0002", "Execution", "T1059", "Command and Scripting Interpreter",
         ".001 PowerShell/.003 Unix Shell/.005 Visual Basic",
         "命令行/脚本解释器执行", "powershell, bash, cscript", "后渗透 Agent"),
        (8, "TA0002", "Execution", "T1204", "User Execution", ".002 Malicious File",
         "诱导用户执行恶意文件", "钓鱼落地", "钓鱼 Agent"),
        (9, "TA0003", "Persistence", "T1136", "Create Account", ".001 Local/.002 Domain",
         "创建后门账户", "net user, Add-ADUser", "后渗透 Agent"),
        (10, "TA0003", "Persistence", "T1053", "Scheduled Task/Job",
         ".001 Windows/.003 Cron", "计划任务/cron 持久化", "schtasks, crontab", "后渗透 Agent"),
        (11, "TA0004", "Privilege Escalation", "T1548",
         "Abuse Elevation Control Mechanism",
         ".002 Bypass UAC/.003 Sudo and Sudo Caching",
         "滥用提权控制机制（UAC/sudo）", "Potato 系列, sudo", "后渗透 Agent"),
        (12, "TA0004", "Privilege Escalation", "T1078", "Valid Accounts", "-",
         "用高权限账户直接提权", "-", "后渗透 Agent"),
        (13, "TA0005", "Stealth", "T1218", "System Binary Proxy Execution",
         ".001 Compiled HTML/.005 Mshta/.013 MMC",
         "用白名单二进制代理执行（LOLBins）", "mshta, regsvr32, rundll32", "后渗透 Agent"),
        (14, "TA0005", "Stealth", "T1070", "Indicator Removal",
         ".002 Clear Event Logs/.006 Timestomp", "清日志/改时间戳",
         "wevtutil, timestomp", "后渗透 Agent"),
        (15, "TA0006", "Credential Access", "T1003", "OS Credential Dumping",
         ".001 LSASS/.002 SAM/.003 NTDS/.004 LSA Secrets/.006 DCSync/.008 /etc/passwd|shadow",
         "转储 OS 凭据", "mimikatz, secretsdump, laZagne", "后渗透 Agent"),
        (16, "TA0006", "Credential Access", "T1110", "Brute Force",
         ".001 Guessing/.002 Cracking/.003 Spraying/.004 Stuffing",
         "在线猜解/离线破解/喷洒/撞库", "hydra, hashcat, kerbrute", "侦察/后渗透"),
        (17, "TA0006", "Credential Access", "T1557", "Adversary-in-the-Middle",
         ".001 Name Resolution Poisoning and SMB Relay",
         "LLMNR/NBT-NS 投毒+NTLM 中继", "Responder, ntlmrelayx", "内网横向 Agent"),
        (18, "TA0006", "Credential Access", "T1558", "Steal or Forge Kerberos Tickets",
         ".001 Golden/.002 Silver/.003 Kerberoasting/.004 AS-REP Roasting",
         "Kerberos 票据窃取/伪造", "Rubeus, Impacket(GetUserSPNs/ticketer)", "后渗透 Agent"),
        (19, "TA0006", "Credential Access", "T1552", "Unsecured Credentials",
         ".004 Private Keys/.005 Cloud Instance Metadata API/.007 Container API",
         "找私钥/云元数据/容器 API", "curl, awscli", "后渗透/云 Agent"),
        (20, "TA0007", "Discovery", "T1087", "Account Discovery", ".001 Local/.002 Domain",
         "枚举本地/域账户", "net user, BloodHound, PowerView", "内网探测 Agent"),
        (21, "TA0007", "Discovery", "T1016", "System Network Configuration Discovery", "-",
         "网卡/路由/ARP/内网网段", "ipconfig, route, arp", "内网探测 Agent"),
        (22, "TA0008", "Lateral Movement", "T1021", "Remote Services",
         ".001 RDP/.002 SMB/.006 WinRM", "远程服务横向（RDP/SMB/WinRM）",
         "xfreerdp, psexec, evil-winrm", "内网横向 Agent"),
        (23, "TA0008", "Lateral Movement", "T1550", "Use Alternate Authentication Material",
         ".001 Pass-the-Hash/.002 Pass-the-Ticket/.003 Pass-the-Cookie",
         "PTH/PtT/PtC", "mimikatz, impacket", "内网横向 Agent"),
        (24, "TA0009", "Collection", "T1005", "Data from Local System", "-",
         "收集本地敏感数据", "find, grep", "后渗透 Agent"),
        (25, "TA0011", "C2", "T1071", "Application Layer Protocol",
         ".001 Web Protocols/.004 DNS", "利用 HTTP(S)/DNS 做 C2",
         "Cobalt Strike, dnscat2", "C2 Agent"),
        (26, "TA0010", "Exfiltration", "T1041", "Exfiltration Over C2 Channel", "-",
         "经现有 C2 通道外传", "C2 框架", "后渗透 Agent"),
        (27, "TA0010", "Exfiltration", "T1048", "Exfiltration Over Alternative Protocol",
         ".001 Symmetric/.002 Asymmetric/.003 Unencrypted Non-C2",
         "经替代协议（DNS/ICMP/SMTP）外传", "dnscat2, icmp 隧道", "后渗透 Agent"),
    ]
    return [
        {"id": i, "tactic_id": ta, "tactic_name": ta_name,
         "technique_id": tid, "technique_name": tname,
         "sub_techniques": sub, "operation": op, "tools": tools,
         "agent_type": agent, "source": SRC_ATTACK, "last_verified": V_ATTACK}
        for (i, ta, ta_name, tid, tname, sub, op, tools, agent) in raw
    ]


# ================================================================ 攻击路径模板（6 类）


def _attack_paths() -> list[dict[str, Any]]:
    src = "设计书附录B-四（完整步骤序列表）"

    def step(no: str, action: str, pre: str, tools: str, verify: str) -> dict:
        return {"step": no, "action": action, "precondition": pre,
                "tools": tools, "verify": verify}

    return [
        {
            "path_id": "PATH-WEB-TO-INTRANET-01",
            "scenario": "Web 打点 → 内网横向（最经典 SRC 路径）",
            "entry_conditions": "发现公网 URL，且存在 RCE/文件上传/SQLi/SSRF 等可直达执行的漏洞",
            "step_sequence": [
                step("1.1", "信息收集：子域名、端口、指纹、技术栈", "入口可达",
                     "amass, nmap, whatweb, nuclei", "输出资产清单，确认开放端口与框架版本"),
                step("1.2", "Web 漏洞探测与确认", "指纹已知", "Burp, nuclei, sqlmap",
                     "核验：对每个疑似漏洞做最小化 PoC，确认返回异常/可执行"),
                step("1.3", "利用漏洞获取初始 Shell", "漏洞可利用", "exp 脚本, msf",
                     "核验：执行 whoami/id 回显，确认当前权限与主机名"),
                step("1.4", "反弹/交互式 Shell 稳定化", "已命令执行", "bash -i, nc, msfvenom",
                     "核验：能执行交互命令、能上传文件"),
                step("1.5", "本机枚举：系统版本、用户、网络配置", "立足成功",
                     "ipconfig/ifconfig, systeminfo", "确认内网网段、是否域成员"),
                step("1.6", "内网探测：存活、端口、域控位置", "拿到内网网卡",
                     "nmap, fscan, crackmapexec", "输出内网拓扑与存活主机清单"),
                step("1.7", "本机/横向提权", "有普通权限", "根据 OS 选提权路径（见路径 3/2）",
                     "核验：拿到 admin/root/SYSTEM 或域管"),
                step("1.8", "横向移动到目标主机", "有可用凭据/服务", "PsExec/WinRM/WMI/RDP",
                     "核验：在目标机执行命令回显"),
                step("1.9", "达成目标并留痕/清理", "横向成功", "-",
                     "证明可达敏感数据；按授权清理"),
            ],
            "decision_points": "1.3 无 RCE 但有 SQLi → 转数据库取凭据；有 SSRF → 转路径 4（云）；"
                               "无 Web 漏洞但有弱口令 → 转 T1110.003 喷洒",
            "failure_handling": "无 Web 漏洞入口则退回侦察补面；提权失败转凭据窃取横向",
            "related_tactics": ["TA0043", "TA0001", "TA0002", "TA0007",
                                "TA0004", "TA0006", "TA0008", "TA0009"],
            "prerequisite_knowledge": "公网 URL 可达",
            "source": src, "last_verified": V_ATTACK,
        },
        {
            "path_id": "PATH-AD-DOMAIN-02",
            "scenario": "Windows 域攻击链（从立足点到 Domain Admin）",
            "entry_conditions": "机器加入域，已拿到任一域普通用户凭据或交互式 Shell",
            "step_sequence": [
                step("2.1", "域信息枚举：域名、DC、域版本、用户、SPN", "域用户权限",
                     "BloodHound(SharpHound), PowerView, impacket-GetADUsers",
                     "拿到域 SID、DC IP、用户清单"),
                step("2.2", "（可选）LLMNR/NBT-NS 投毒 + NTLM 中继", "内网可监听流量",
                     "Responder, ntlmrelayx",
                     "捕获 Net-NTLM hash；核验：中继到目标机器拿到 shell 或解出账号"),
                step("2.3", "Kerberoasting", "任一域用户 + 目标有 SPN 账户",
                     "GetUserSPNs.py / Rubeus kerberoast",
                     "拿到 TGS hash（hashcat -m 13100）；核验：离线破解出明文密码"),
                step("2.4", "（并行）AS-REP Roasting", "目标账户禁用了预认证",
                     "GetNPUsers.py / Rubeus asreproast",
                     "拿到 AS-REP hash（hashcat -m 18200）；破解"),
                step("2.5", "Pass-the-Hash / Over-Pass-the-Hash", "拿到某用户 NTLM hash",
                     "impacket-wmiexec / psexec, mimikatz",
                     "核验：用 hash 免密登录到该用户可访问机器"),
                step("2.6", "提权至高权限后转储 LSASS/SAM", "本机 admin 或 SYSTEM",
                     "mimikatz sekurlsa::logonpasswords", "核验：拿到内存中明文/hash"),
                step("2.7", "DCSync", "有 DS-Replication 权限（域管/特定 ACE）",
                     "mimikatz lsadump::dcsync /user:krbtgt",
                     "核验：拿到 krbtgt hash 与全域用户 hash"),
                step("2.8", "黄金票据（Golden Ticket）", "拿到 krbtgt hash + 域 SID",
                     "mimikatz kerberos::golden / Rubeus / ticketer.py",
                     "核验：伪造任意账户 TGT，能访问任意域资源"),
                step("2.9", "（备选）白银票据（Silver Ticket）", "拿到某服务账户 hash", "同上",
                     "针对单服务伪造 TGS，更隐蔽"),
                step("2.10", "持久化与横向到目标", "域管权限", "DCSync 后任意横向",
                     "核验：在 DC 或目标机落命令"),
            ],
            "decision_points": "2.2 若 SMB 签名强制开启 → 中继失败，退回 Kerberoasting；"
                               "2.3 破解不出 → 转 BloodHound 找 ACE/约束委派/AD CS 路径"
                               "（Shadow Credentials、AD CS Domain Escalation、Constrained/RBCD Delegation）",
            "failure_handling": "中继失败退回 Kerberoasting；破解不出转 ACE/委派/AD CS 提权路径",
            "related_tactics": ["TA0007", "TA0006", "TA0008", "TA0003"],
            "prerequisite_knowledge": "已知域控 IP / 域内普通用户凭据",
            "source": src, "last_verified": V_ATTACK,
        },
        {
            "path_id": "PATH-LINUX-PRIVESC-03",
            "scenario": "Linux 提权与后渗透",
            "entry_conditions": "拿到任一非 root 用户的交互式 Shell",
            "step_sequence": [
                step("3.1", "基础枚举：内核版本、发行版、用户、组",
                     "uname -a; cat /etc/*release; id; who; last",
                     "linpeas / linux-exploit-suggester", "输出提权面清单"),
                step("3.2", "sudo 配置滥用", "sudo -l", "对照 GTFOBins 查每个 NOPASSWD 二进制",
                     "核验：如 sudo find . -exec /bin/sh \\; 拿到 root shell"),
                step("3.3", "SUID/SGID 二进制", "find / -perm -4000 -type f 2>/dev/null",
                     "GTFOBins 逐个查", "核验：如 vim/nmap/python 以 root 跑 shell"),
                step("3.4", "cron 任务滥用",
                     "cat /etc/crontab; ls -la /etc/cron.*; ps aux | grep cron",
                     "若 root 跑的脚本/通配符/脚本可写，植入命令", "核验：等待 cron 触发拿到 root"),
                step("3.5", "SSH 私钥滥用", "cat ~/.ssh/id_*; grep -r \"PRIVATE KEY\" /",
                     "用私钥登录其他主机或 root", "核验：ssh -i id_rsa root@target"),
                step("3.6", "内核漏洞", "3.1 确认内核版本过旧",
                     "匹配 CVE（如 DirtyCow/CVE-2016-5195 类）",
                     "核验：跑 exp 拿到 root；警告：内核 exp 易崩系统，授权内先快照"),
                step("3.7", "服务/配置文件可写", "找可写的服务配置/数据库配置",
                     "改配置触发 root 执行", "拿到 root"),
                step("3.8", "后渗透：留存、凭据收集、内网", "root 后",
                     "cat /etc/shadow, history, 找 hash", "离线破解 hashcat"),
            ],
            "decision_points": "3.2 无 sudo 可滥用 → 3.3 无 SUID → 3.4 无 cron → 3.6 内核不旧 → "
                               "转横向（本机无可提权面时，靠窃取的 SSH key/云凭据横向到其他主机）",
            "failure_handling": "无提权面则靠窃取凭据横向；内核 exp 慎用（易崩系统，先快照）",
            "related_tactics": ["TA0007", "TA0004", "TA0006", "TA0003"],
            "prerequisite_knowledge": "非 root 交互式 Shell",
            "source": src, "last_verified": V_ATTACK,
        },
        {
            "path_id": "PATH-CLOUD-SSRF-04",
            "scenario": "云环境攻击链（SSRF → 元数据 → IAM 提权 → 横向/容器逃逸）",
            "entry_conditions": "公网 Web 应用存在 SSRF（可让服务器发起任意请求），且运行在云主机上",
            "step_sequence": [
                step("4.1", "确认 SSRF", "Web 有可控 URL/参数", "Burp Collaborator / interactsh",
                     "核验：服务器发起了对攻击者控制地址的请求"),
                step("4.2", "访问云元数据服务", "SSRF 可请求内网地址",
                     "http://169.254.169.254/latest/meta-data/iam/security-credentials/（AWS）；"
                     "阿里云 100.100.100.200", "核验：返回角色名"),
                step("4.3", "窃取临时凭据", "元数据无防护（IMDSv1）", "访问上一步返回的角色路径",
                     "核验：拿到 AccessKeyId/SecretAccessKey/Token"),
                step("4.4", "用凭据枚举 IAM 权限", "拿到 AK/SK",
                     "aws sts get-caller-identity; aws iam list-*", "确认该角色实际权限"),
                step("4.5", "IAM 权限提升",
                     "角色有可滥用权限（iam:PassRole/iam:CreateAccessKey/lambda:CreateFunction）",
                     "按权限链提权（Attacking IAM 标准路径）",
                     "核验：拿到更高权限或持久化 access key"),
                step("4.6", "（并行）S3 桶权限配置错误", "枚举到桶名",
                     "aws s3 ls s3://bucket 看是否可读/可写", "核验：列出/写入对象"),
                step("4.7", "容器/集群逃逸", "workload 在容器/K8s 中",
                     "检查 privileged 容器、挂载宿主目录、Docker API 暴露",
                     "核验：在宿主执行命令"),
                step("4.8", "（备选）供应链投毒", "有代码仓库/CI 写权限",
                     "typosquat / 投毒依赖", "在下一次构建植入"),
            ],
            "decision_points": "4.2 若目标已强制 IMDSv2（需 PUT token）→ SSRF 无法带 token，"
                               "元数据路径失败，转找应用配置里的硬编码 AK/SK（T1552.001）；"
                               "4.5 若无 IAM 提权链 → 看该角色能直接读的 S3/数据库是否即目标",
            "failure_handling": "IMDSv2 强制转硬编码凭据；无提权链转直接可读资源",
            "related_tactics": ["TA0001", "TA0006", "TA0004", "TA0008", "TA0003"],
            "prerequisite_knowledge": "存在 SSRF；目标运行在云主机（参考：Capital One 事件）",
            "source": src, "last_verified": V_ATTACK,
        },
        {
            "path_id": "PATH-PHISHING-CRED-05",
            "scenario": "办公网钓鱼与凭据收割（按授权范围考虑）",
            # 钓鱼/外泄属高敏路径，默认禁用；授权声明显式开启敏感动作后才可选
            "sensitivity": "sensitive",
            "entry_conditions": "有目标员工邮箱清单（OSINT 所得），授权范围内可发钓鱼邮件",
            "step_sequence": [
                step("5.1", "钓鱼素材准备：仿冒页面/附件", "授权",
                     "gophish, 克隆登录页（HackTricks Phishing Methodology）",
                     "本地测试页面可收集凭据"),
                step("5.2", "鱼叉式钓鱼投递", "员工邮箱清单",
                     "按 T1566.001 附件 / .002 链接 / .003 第三方", "邮件送达（不触发网关）"),
                step("5.3", "凭据收割", "用户上当输入", "仿冒页/宏/恶意 LNK",
                     "核验：后台收到明文凭据或回调"),
                step("5.4", "（可选）落地执行", "用户执行附件", "宏 → 反弹 shell",
                     "核验：C2 收到会话"),
                step("5.5", "凭据验证与喷洒", "拿到凭据", "验证 OWA/VPN 是否可登录",
                     "核验：登录成功，转入路径 2 域攻击链"),
            ],
            "decision_points": "5.3 无输入但有 5.4 执行 → 直接立足；若 MFA 强制 → "
                               "转 MFA 拦截/复用会话 cookie（T1550.003 / T1539）",
            "failure_handling": "MFA 强制转会话 cookie 复用；无回调换素材重投（授权次数内）",
            "related_tactics": ["TA0042", "TA0001", "TA0006", "TA0002"],
            "prerequisite_knowledge": "目标员工邮箱清单；明确书面授权",
            "source": src, "last_verified": V_ATTACK,
        },
        {
            "path_id": "PATH-DATA-EXFIL-06",
            "scenario": "数据外泄路径（收集→汇聚→加密→外传）",
            # 钓鱼/外泄属高敏路径，默认禁用；授权声明显式开启敏感动作后才可选
            "sensitivity": "sensitive",
            "entry_conditions": "已能访问敏感数据（数据库/文件共享）",
            "step_sequence": [
                step("6.1", "收集定位敏感数据", "已立足", "find/grep、数据库导出",
                     "列出目标文件/表"),
                step("6.2", "汇聚打包", "数据分散", "tar/zip/csv 导出到临时目录",
                     "生成待外传包"),
                step("6.3", "加密/混淆", "防内容检测", "对称加密（T1048.001）或 Base64 编码",
                     "生成密文/编码包"),
                step("6.4", "选择外传通道（按出站策略决策）", "知道哪些端口出网",
                     "先测出网策略（curl 出 443/53/25 是否通）", "确认可用通道"),
                step("6.5a", "经 C2 通道外传", "已有 C2 会话", "T1041，数据塞进现有 C2 流量",
                     "核验：C2 端收到文件分片"),
                step("6.5b", "经替代协议外传", "C2 通道被限制",
                     "T1048：DNS 隧道（dnscat2）、ICMP、SMTP",
                     "核验：攻击者 DNS 服务器/邮件收到数据"),
                step("6.5c", "经合法云存储外传", "可访问外部网盘",
                     "上传到攻击者控制的桶/Git", "核验：外部可见文件"),
            ],
            "decision_points": "先测出网策略（443/53/25）→ 通则用对应通道；"
                               "全堵 → 降级证明「数据已被打包待传」并记录日志证据",
            "failure_handling": "出站全堵则降级为打包待传证明，记录日志证据（授权内不强行外发）",
            "related_tactics": ["TA0009", "TA0010"],
            "prerequisite_knowledge": "已访问敏感数据；已知出站策略",
            "source": src, "last_verified": V_ATTACK,
        },
    ]


# ================================================================ checklist 条目


def _redteam_entries() -> list[dict[str, Any]]:
    src_b = "设计书附录B"

    def entry(entry_id: str, title: str, tactics: list[str], tech: str,
              platforms: list[str], pre: list[str],
              steps: list[tuple[str, str, str]], tools: list[str],
              verify: str, expected: str, fail: str, source: str,
              severity: str, category: str) -> dict:
        return {
            "entry_id": entry_id, "title": title, "tactic_ids": tactics,
            "technique_id": tech, "os_platform": platforms, "preconditions": pre,
            "steps": [{"step": i + 1, "action": a, "command": c, "expected": e}
                      for i, (a, c, e) in enumerate(steps)],
            "tools": tools, "verify_method": verify, "expected_result": expected,
            "fail_handling": fail, "source": source, "severity": severity,
            "category": category, "last_verified": V_ATTACK,
        }

    return [
        # —— 附录B-5.2 完整 JSON 示例（原文落地）
        entry("RT-AD-003", "Kerberoasting 域内服务账户离线破解", ["TA0006"], "T1558.003",
              ["Windows", "Linux"],
              ["已获得任一域普通用户的可用凭据（用户名/密码 或 hash）",
               "能与域控 88 端口(Kerberos)通信",
               "目标域内存在注册了 SPN 的用户账户"],
              [("枚举带 SPN 的账户",
                "impacket-GetUserSPNs.py domain/user:pass -dc-ip <DC_IP> -request",
                "列出 SPN 账户并导出 TGS 加密部分"),
               ("离线破解", "hashcat -m 13100 tgs_hash.txt rockyou.txt",
                "破解出服务账户明文密码")],
              ["impacket-GetUserSPNs", "Rubeus", "hashcat"],
              "hashcat 输出明文密码，且用该密码能成功登录对应账户",
              "获得高权限服务账户明文，可用于横向或 DCSync 前置",
              "破解失败→检查密码策略；无弱口令→转 BloodHound 枚举 ACL/约束委派/AD CS 提权路径",
              "https://attack.mitre.org/techniques/T1558/003/ ; https://hacktricks.wiki/ "
              "(Active Directory Methodology > Kerberoast)",
              "high", "windows_domain"),
        # —— 以下条目字段内容取自附录B-四路径表的步骤/工具/核验列（同源转录）
        entry("RT-WEB-001", "Web 漏洞最小化 PoC 确认", ["TA0001"], "T1190",
              ["Web", "通用"], ["指纹与技术栈已知"],
              [("Web 漏洞探测与确认", "Burp/nuclei/sqlmap 对疑似漏洞逐一验证",
                "返回异常/可执行")],
              ["Burp", "nuclei", "sqlmap"],
              "对每个疑似漏洞做最小化 PoC，确认返回异常/可执行",
              "漏洞确认可利用，进入打点环节",
              "无 RCE 但有 SQLi → 转数据库取凭据；有 SSRF → 转云路径（PATH-CLOUD-SSRF-04）",
              f"{src_b}-四 路径1 步1.2", "high", "web_entry"),
        entry("RT-WEB-002", "初始 Shell 权限与主机确认", ["TA0002"], "T1059",
              ["Windows", "Linux"], ["Web 漏洞确认可利用"],
              [("利用漏洞获取初始 Shell", "exp 脚本/msf 触发漏洞",
                "whoami/id 回显当前权限与主机名")],
              ["exp 脚本", "msf"],
              "执行 whoami/id 回显，确认当前权限与主机名",
              "获得初始立足点，明确当前权限级别",
              "拿不到交互 → 先稳定通道（bash -i/nc/msfvenom）再枚举",
              f"{src_b}-四 路径1 步1.3", "high", "web_entry"),
        entry("RT-AD-001", "域信息枚举（BloodHound）", ["TA0007"], "T1087.002",
              ["Windows"], ["已获得域内普通用户权限"],
              [("域信息枚举", "SharpHound 收集 / PowerView / impacket-GetADUsers",
                "拿到域 SID、DC IP、用户清单")],
              ["BloodHound(SharpHound)", "PowerView", "impacket-GetADUsers"],
              "拿到域 SID、DC IP、用户清单",
              "域拓扑与攻击面图谱，支撑后续 Kerberoasting/委派路径选路",
              "采集被 EDR 拦截 → 改 impacket 远程低频枚举",
              f"{src_b}-四 路径2 步2.1", "medium", "windows_domain"),
        entry("RT-AD-002", "LLMNR/NBT-NS 投毒 + NTLM 中继", ["TA0006"], "T1557.001",
              ["Windows", "Linux"], ["内网可监听流量", "目标网段 SMB 签名未强制"],
              [("投毒监听", "Responder -I eth0", "捕获 Net-NTLM hash"),
               ("中继利用", "ntlmrelayx.py -t smb://target", "中继拿到 shell 或解出账号")],
              ["Responder", "ntlmrelayx"],
              "捕获到 Net-NTLM hash；中继到目标机器拿到 shell 或解出账号",
              "获得内网机器立足点或可用凭据",
              "SMB 签名强制 → 中继失败，退回 Kerberoasting（RT-AD-003）",
              f"{src_b}-四 路径2 步2.2", "high", "windows_domain"),
        entry("RT-AD-004", "DCSync 全域凭据同步", ["TA0006"], "T1003.006",
              ["Windows", "Linux"], ["拥有 DS-Replication 权限（域管/特定 ACE）"],
              [("DCSync", "mimikatz lsadump::dcsync /user:krbtgt",
                "拿到 krbtgt hash 与全域用户 hash")],
              ["mimikatz", "impacket-secretsdump"],
              "拿到 krbtgt hash 与全域用户 hash",
              "全域凭据掌控，黄金票据前置",
              "权限不足 → 先 BloodHound 找 ACE 提权路径",
              f"{src_b}-四 路径2 步2.7", "critical", "windows_domain"),
        entry("RT-AD-005", "黄金票据伪造任意账户 TGT", ["TA0006"], "T1558.001",
              ["Windows", "Linux"], ["已拿到 krbtgt hash 与域 SID"],
              [("伪造黄金票据",
                ("mimikatz kerberos::golden /user:Administrator /domain:<域> /sid:<SID> "
                 "/krbtgt:<hash>（或 ticketer.py）"), "伪造任意账户 TGT")],
              ["mimikatz", "Rubeus", "ticketer.py"],
              "伪造任意账户 TGT，能访问任意域资源",
              "域内持久化最高权限访问能力",
              "票据失效 → krbtgt 已轮换需重新 DCSync；备选白银票据针对单服务",
              f"{src_b}-四 路径2 步2.8", "critical", "windows_domain"),
        entry("RT-LIN-001", "sudo 配置滥用提权（GTFOBins 对照）", ["TA0004"], "T1548.003",
              ["Linux"], ["非 root 交互式 Shell"],
              [("列出 sudo 权限", "sudo -l", "列出 NOPASSWD/可执行二进制"),
               ("对照 GTFOBins 逐条利用", "sudo find . -exec /bin/sh \\;",
                "拿到 root shell")],
              ["sudo", "GTFOBins"],
              "如 sudo find . -exec /bin/sh \\; 拿到 root shell",
              "root 权限",
              "无可滥用 sudo → 转 SUID（RT-LIN-002）→ cron → 内核 CVE（易崩慎用）",
              f"{src_b}-四 路径3 步3.2", "high", "linux_privesc"),
        entry("RT-LIN-002", "SUID/SGID 二进制提权", ["TA0004"], "T1548",
              ["Linux"], ["非 root 交互式 Shell"],
              [("枚举 SUID/SGID", "find / -perm -4000 -type f 2>/dev/null",
                "SUID 二进制清单"),
               ("GTFOBins 逐个查", "vim/nmap/python 等以 root 跑 shell", "root shell")],
              ["find", "GTFOBins"],
              "如 vim/nmap/python 以 root 跑 shell",
              "root 权限",
              "无 SUID 面 → cron 可写 → SSH 私钥 → 内核 CVE（最后手段，先快照）",
              f"{src_b}-四 路径3 步3.3", "high", "linux_privesc"),
        entry("RT-CLD-001", "SSRF 打云元数据窃取临时凭据", ["TA0006"], "T1552.005",
              ["Cloud", "Web"], ["Web 存在 SSRF（服务器可发起任意请求）", "目标在云主机上"],
              [("访问元数据服务",
                ("http://169.254.169.254/latest/meta-data/iam/security-credentials/（AWS）"
                 " / http://100.100.100.200/（阿里云）"), "返回角色名"),
               ("窃取临时凭据", "访问角色路径", "拿到 AccessKeyId/SecretAccessKey/Token")],
              ["curl", "awscli", "Burp Collaborator / interactsh"],
              "拿到 AccessKeyId/SecretAccessKey/Token（IMDSv1 无防护时）",
              "云临时凭据，可枚举 IAM 权限并尝试提权",
              "IMDSv2 强制（需 PUT token）→ SSRF 无法带 token，转找硬编码 AK/SK（T1552.001）",
              f"{src_b}-四 路径4 步4.2/4.3", "critical", "cloud"),
        entry("RT-PHI-001", "鱼叉钓鱼与凭据收割（授权范围内）", ["TA0001"], "T1566",
              ["Windows", "通用"], ["有目标员工邮箱清单（OSINT 所得）", "授权范围内可发钓鱼邮件"],
              [("素材准备", "gophish 克隆登录页", "本地测试页面可收集凭据"),
               ("投递", "T1566.001 附件/.002 链接/.003 第三方", "邮件送达不触发网关"),
               ("收割", "仿冒页收集输入", "后台收到明文凭据或回调")],
              ["gophish", "宏", "LNK"],
              "后台收到明文凭据或回调",
              "用户凭据或落地会话，转路径 2 域攻击链",
              "MFA 强制 → 转会话 cookie 复用（T1550.003/T1539）；无回调换素材重投",
              f"{src_b}-四 路径5 步5.1-5.3", "high", "phishing"),
        entry("RT-EXF-001", "DNS 隧道替代协议外泄", ["TA0010"], "T1048.003",
              ["Windows", "Linux"], ["已收集打包数据", "C2 通道受限但 53 出网"],
              [("DNS 隧道外发", "dnscat2 将数据编码进子域名/TXT 记录",
                "攻击者 DNS 服务器收到数据")],
              ["dnscat2", "icmp 隧道"],
              "攻击者 DNS 服务器/邮件收到数据分片",
              "授权范围内证明数据可被外传",
              "53 也不通 → 降级证明「已打包待传」并记录日志证据",
              f"{src_b}-四 路径6 步6.5b", "medium", "exfiltration"),
    ] + _inspection_entries()


def _inspection_entries() -> list[dict[str, Any]]:
    """具体检查清单条目（告诉 Agent 到哪里看、看什么信号）。

    内容形态对标 HackTricks / PayloadsAllTheThings / GTFOBins 的速查条目：
    每条 = 具体目标（文件/路径/事实）+ 命中信号 + 不适用时的转向。它们是
    检索用的参考资料而非执行脚本——是否采用由智能体基于现场证据判断。
    """
    src = "设计书附录B/附录C 知识源（HackTricks、PayloadsAllTheThings、GTFOBins、小虎鲸CTF）"

    def entry(entry_id: str, title: str, tech: str, platforms: list[str],
              steps: list[tuple[str, str, str]], verify: str, fail: str,
              category: str) -> dict:
        return {
            "entry_id": entry_id, "title": title, "tactic_ids": ["TA0001"],
            "technique_id": tech, "os_platform": platforms,
            "preconditions": ["目标可达或已取得附件"],
            "steps": [{"step": i + 1, "action": a, "command": c, "expected": e}
                      for i, (a, c, e) in enumerate(steps)],
            "tools": ["http_request", "shell", "python"], "verify_method": verify,
            "expected_result": "命中检查点并取得线索", "fail_handling": fail,
            "source": src, "severity": "info", "category": category,
            "last_verified": V_ATTACK,
        }

    return [
        entry("RT-WEB-LEAK-001", "Web 敏感路径与备份文件检查清单", "T1190",
              ["Web", "通用"],
              [("robots.txt 与 sitemap",
                "GET /robots.txt、/sitemap.xml",
                "Disallow 条目点名隐藏路径；看到敏感目录名即升级"),
               ("版本库泄露",
                "GET /.git/HEAD、/.svn/entries、/.hg/hgrc",
                "返回 ref: refs/heads/... 即 .git 源码可恢复（gakki412/松散文件）"),
               ("环境与配置文件",
                "GET /.env、/.env.bak、/config.php.bak、/application.yml、/WEB-INF/web.xml",
                "返回数据库串/密钥/调试开关等内容"),
               ("备份与编辑器残留",
                "GET /www.zip、/backup.zip、/web.rar、/site.tar.gz、/index.php.bak、/.index.php.swp",
                "返回压缩包或源码片段而非 404"),
               ("目录指纹与信息文件",
                "GET /.DS_Store、/composer.json、/package.json、/npm-shrinkwrap.json",
                "暴露目录结构/依赖树，可进一步枚举路径"),
               ("调试与管理面",
                "GET /phpinfo.php、/info.php、/test.php、/debug、/console、/actuator、/swagger、/api-docs、/admin",
                "phpinfo 含 FLAG 环境变量与扩展清单；actuator/docs 暴露内部接口"),
               ("服务自述",
                "GET /server-status、/cgi-bin/、/.well-known/",
                "Apache 状态页/CGI 入口/协议揭示")],
              "任一路径返回非 404 且内容含凭据/源码/结构信息即升级为主线",
              "全 404 则放下清单，回到页面功能本身（表单、参数、JS 接口、注释）",
              "web_entry"),
        entry("RT-WEB-PHP-002", "PHP 弱类型与比较特性事实表", "-",
              ["Web"],
              [("empty 与 \"0\"",
                "empty(\"0\") === true 且 \"0\" !== \"\"",
                "isset+empty+!== 组合可用字符串 \"0\" 通过"),
               ("0e magic hash",
                "md5(\"240610708\") == \"0\" 成立（0e462…全数字）；同类样本 QNKCDZO、alambana",
                "松散比较下 md5 结果形如 0e\\d+ 等价于 0"),
               ("科学计数法",
                "strlen(\"1e4\") === 3 且 1e4 > 1000",
                "长度限制与数值限制可用同一字符串同时满足"),
               ("数组绕过 hash 比较",
                "md5([1,2]) 与 md5([3,4]) 均为 NULL，== 宽松比较为 true；sha1 同理",
                "以数组为参时 md5/sha1 返回 NULL，NULL==NULL 成立"),
               ("strcmp 数组传参",
                "strcmp($x, []) 在 PHP5 返回 NULL，==0 判 true",
                "参数传数组可绕过字符串比较"),
               ("preg_match 未锚定",
                "模式缺少 $ 或 \\z 时，载荷末尾追加 %0a（换行）可绕过尾部匹配",
                "匹配通过但后半段带入了注入载荷"),
               ("in_array 宽松",
                "in_array(0, [\"a\", \"b\"]) === true",
                "未设第三参 true 的数组查找可被 0/\"\" 混入")],
              "以题目源码实际使用的比较运算符（== 与 ===）逐条对照",
              "全程 === / 强类型时弱类型路径全部失效，转找逻辑漏洞或注入面",
              "web_entry"),
        entry("RT-WEB-SSTI-003", "模板注入 canary 与引擎判别", "-",
              ["Web"],
              [("通用 canary",
                "{{7*7}}、${7*7}、<%= 7*7 %>、{7*7}、#{7*7} 逐个注入并比对回显",
                "回显 49/49/49/49/49 即对应语法被求值"),
               ("Jinja2 判别",
                "{{7*'7'}} → 7777777",
                "Jinja2 特征（Twig 为 49）"),
               ("Smarty 判别",
                "{7*7} → 49 且 {$smarty.version} 有回显",
                "Smarty 模板引擎"),
               ("表达式环境",
                "Spring: #{...}；OGNL: %{...}（Struts2）",
                "对应 Java 环境的求值语法")],
              "canary 被求值即确认 SSTI，后续 payload 由现场选择",
              "全部原样回显则不是模板注入，转查其他参数注入面",
              "web_entry"),
        entry("RT-MISC-STEG-004", "隐写与附件检查清单", "-",
              ["通用"],
              [("魔数与结构",
                "file x；xxd x | head；对比扩展名与真实格式",
                "格式不符即改后缀/换解析器（容器内藏文件）"),
               ("嵌入文件",
                "binwalk -Me x；foremost x",
                "分离出内嵌的 zip/图片/文本"),
               ("字符串全量",
                "strings -n 6 x；strings -el x（UTF-16LE）",
                "明文 flag、密码提示、URL"),
               ("元数据",
                "exiftool x",
                "Comment/Author/GPS 里的线索"),
               ("零宽字符",
                "python 检查 U+200B/200C/200D/FEFF 序列",
                "零宽编码的隐藏信息"),
               ("尾随数据",
                "PNG 检查 IEND 之后字节；JPEG 检查 FFD9 之后字节",
                "文件尾附加的压缩包/文本"),
               ("通道隐写",
                "zsteg x（PNG/BMP LSB）；steghide extract -sf x（先试空口令）；音频看频谱图",
                "LSB 平面异色、口令命中、频谱可见图案"),
               ("压缩包",
                "zipinfo/7z l 看注释与加密位；伪加密改标志位",
                "注释线索/伪加密可解")],
              "任一手段产出可读线索即跟进；输出仍过 Flag 提取",
              "全无产出则回到题面描述重新审题",
              "misc_stego"),
        entry("RT-LIN-PRIV-005", "Linux 提权检查点（GTFOBins 对照）", "T1548",
              ["Linux"],
              [("sudo 配置",
                "sudo -l",
                "出现 (root) NOPASSWD 条目 → GTFOBins 查对应二进制用法"),
               ("SUID/SGID",
                "find / -perm -4000 -o -perm -2000 -type f 2>/dev/null",
                "非标准 SUID 二进制（nmap --interactive、vim、find 等）"),
               ("文件能力",
                "getcap -r / 2>/dev/null",
                "cap_setuid+ep 等能力位（python/perl 可直接提权）"),
               ("计划任务",
                "cat /etc/crontab；ls -la /etc/cron*",
                "root 执行且脚本可写 → 注入命令"),
               ("内核版本",
                "uname -a",
                "老内核对照已知 CVE（注意：可能崩溃系统，CTF 靶机可试）")],
              "任一检查点给出可直接执行命令且回显 root 身份即完成提权",
              "逐项无果则转向凭据搜集与横向（转路径模板 2）",
              "privilege_escalation"),
    ]


# ================================================================ SRC 平台规则（7 平台）


def _platform_rules() -> list[dict[str, Any]]:
    """附录C-1.1 总览 + 1.2 细则。rule_type 枚举对齐附录C-5.1 字段设计。"""
    v = V_SRC

    def rule(platform: str, rule_type: str, *, tier: str = "",
             include: str = "", exclude: str = "", levels: dict | None = None,
             reward: str = "", auto: bool | None = None, report: dict | None = None,
             url: str = "", effective: str = "", note: str = "") -> dict:
        return {"platform": platform, "asset_tier": tier, "rule_type": rule_type,
                "include_condition": include, "exclude_condition": exclude,
                "level_mapping": levels or {}, "reward_range": reward,
                "auto_test_allowed": auto, "report_required": report or {},
                "source_url": url, "verified_at": v, "effective_date": effective,
                "note": note}

    asrc_url = "https://security.alibaba.com/announcement/announcement?id=219"
    tsrc_url = "https://security.tencent.com/uploadimg_dir/other/TSRC.pdf"
    byte_url = ("https://lf-bytesecurity.bytecdn.com/obj/bytesecurity/bytesrc/"
                "字节跳动安全响应中心基本原则与测试规范.pdf")
    butian_url = "https://www.butian.net/newneed"
    vulbox_url = "https://www.vulbox.com/faq?id=79"

    return [
        # ---------------- 阿里云 ASRC
        rule("asrc", "accept", include=(
            "【已查证】严重：海量资损刷钱、核心网络服务器权限、核心账号体系敏感信息多元组"
            "任意查询/修改、主账号任意登录、APP 远程无条件 RCE、云内提权到宿主机；"
            "云产品须为 aliyuncs.com 等阿里云域名"), url=asrc_url),
        rule("asrc", "exclude", exclude=(
            "【已查证】无等级（0 贡献值）：功能缺陷/乱码/兼容性、Self-XSS、无敏感操作 CSRF、"
            "无意义异常信息泄露、内网 IP/域名泄露、管理后台泄露、默认 demo、"
            "仅小众浏览器触发的 XSS、邮件轰炸、轮询多手机短信轰炸、纯猜测；"
            "社会工程学（非盲打钓鱼）不在收集范围；仅理论可行无 PoC＝无效"), url=asrc_url),
        rule("asrc", "merge_dedup", include=(
            "【已查证】同一应用下同类型漏洞确认前 3 个；同一问题点导致只确认第 1 个；"
            "同一功能越权增删改查只确认 1 个；同库 SQL 注入确认前 3 个"
            "（第 1 个正常、后 2 个降一级）；0Day 最多前 3 个；同一组件多业务问题确认前 3 个；"
            "降级场景：需用户交互（除 CSRF/反射 XSS/JSONP 外）、利用有依赖/限制、"
            "影响量极小、部分环节重复；仅非 Chrome 触发的 XSS/JSONP 最高低危"),
             url=asrc_url, note="测试机/测试数据漏洞＝无效（未运行实际业务）"),
        rule("asrc", "reward", levels={"严重": None, "高": None, "中": None, "低": None, "无": 0},
             reward=("5 级×4 资产档：超级资产严重 10 万/高危 4 万；关键资产严重 3 万/"
                     "高危 1.2 万/中危 1500/低危 500；核心资产严重 1 万；一般资产严重 6000"),
             url="https://security.alibaba.com/announcement/announcement?id=211",
             note="资产档：超级/关键/核心/一般"),
        # ---------------- 腾讯 TSRC
        rule("tsrc", "accept", include=(
            "【已查证】技术维度（用户交互/攻击媒介/前置条件）+ 业务维度"
            "（功能重要度/影响数量/活跃度）综合定级；严重：核心产品远程可利用"),
             url=tsrc_url, note="鼓励完整 PoC/可一键复现脚本"),
        rule("tsrc", "exclude", exclude="【已查证】重复、无法复现、无实际影响按重复/忽略处理",
             url=tsrc_url),
        rule("tsrc", "reward",
             reward=("贡献值＝积分×贡献系数（核心严重 9×220≈1980，1 贡献值=1 安全币=5 元）；"
                     "严重额外现金：核心税后 3 万+，重点 1~3 万；高质量报告奖最高 5000；"
                     "通用软件严重最高 100 万"),
             url="https://security.tencent.com/index.php/blog/msg/200",
             note="资产系数：核心/重点/边缘产品"),
        # ---------------- 字节 ByteSRC（红线核心来源）
        rule("bytesrc", "accept", include=(
            "【已查证】V6.0 增设「重大漏洞」：抖音高系数资产中无交互接管任意账号/"
            "无交互获取任意用户手机号或身份证且无风控拦截＝20 万/个；其他重大 10 万"),
             url="https://q2a1fk6o03.feishu.cn/docx/WPOHdeVdLoJgPfx25VmcfVJxnde",
             effective="V6.0"),
        rule("bytesrc", "exclude", exclude=(
            "【已查证】AI 直接生成、未经人工复现的报告＝无效；同一漏洞源合并；"
            "超出「暂不收取业务范围」清单＝不收"), url=byte_url,
             note="暂不收取范围清单不定期更新：https://src.bytedance.com/announcement"),
        rule("bytesrc", "redline", exclude=(
            "【已查证】禁止引起业务异常；禁止扫描器/自动化工具，只允许手工测试；"
            "禁止任何 DoS/DDoS 及自动扫描产生大流量；禁止内网渗透延伸行为；"
            "注入严禁读表数据，UPDATE/DELETE/INSERT 类注入禁自动化；"
            "禁止近源攻击/物理入侵/社工/邮件钓鱼；禁止使用社工库"),
             url=byte_url, auto=False, effective="V4.0/V6.0"),
        rule("bytesrc", "approval", include=(
            "【已查证】敏感测试（反弹 shell、容器逃逸、文件上传）须提前在线报备，"
            "未报备不计奖励"),
             url=byte_url,
             report={"敏感测试": ["reverse_shell", "container_escape", "webshell_upload"],
                     "需提前报备": True}),
        rule("bytesrc", "reward",
             reward="重大/严重/高/中/低 × 高/中/低系数资产；单洞累计可达 20 万；"
                    "成长体系额外 +1200~9000 元",
             url="https://security.bytedance.com/news/"
                 "douyin-vulnerability-bounty-increased-to-200000-yuan-per-vulnerability/"),
        # ---------------- 补天
        rule("butian", "accept", include=(
            "【已查证】通用 2.0：未授权下可直接或组合达 RCE（远程命令执行/文件上传等）；"
            "VPN 收录二次认证绕过/账号枚举；虚拟化收录虚拟机逃逸；分通用型+事件型"),
             url="https://www.butian.net/Article/content/id/667"),
        rule("butian", "exclude", exclude=(
            "【已查证】无法复现、危害小、抄袭/未验证、厂商太小（权重<1）或已停维、"
            "与平台记录重复或细节已公开"), url=butian_url),
        rule("butian", "redline", exclude=(
            "【已查证】禁止对生产环境文件覆盖/删除等高并发操作；"
            "测试致系统异常须立即联系平台"), url=butian_url),
        rule("butian", "merge_dedup", include="【已查证】同源同类型漏洞只收一例",
             url=butian_url),
        rule("butian", "reward",
             reward=("公益 SRC：KB 库币（1KB≈5 元兑实物）+ 现金；专属 SRC 现金按厂商计划；"
                     "事件漏洞高危大型厂商 ¥100~2000；低危≤1KB 无积分；周三打款"),
             url="https://www.butian.net/Reward/pub"),
        # ---------------- 漏洞盒子
        rule("vulbox", "accept", include=(
            "【已查证】SQL 注入须注出≥1 条数据证明危害；弱口令按系统重要度定级"
            "（重要系统管理员弱口令=高危）；越权/未授权"), url=vulbox_url),
        rule("vulbox", "exclude", exclude=(
            "【已查证】严禁拖库；单纯报错无危害证明＝忽略；已获 webshell 禁止下载源码审计"
            "（须先报平台征得厂商同意）；GitHub/网盘泄露类须上报厂商确认归属，常归潜在风险不奖励"),
             url=vulbox_url),
        rule("vulbox", "merge_dedup", include=(
            "【已查证】同一漏洞源产生的多个漏洞计 1 个（同接口多漏洞/同发布系统多页面/"
            "框架整站/泛解析多站/未身份校验多接口越权/未 token 校验多 CSRF/同上传组件/"
            "验证码失效多问题/报错多页敏感信息）；前后关联漏洞合并；同系统多用户弱口令"
            "只确认第 1 个；同系统短时≥3 个同类型高危＝合并/降级后续忽略；"
            "严重敏感身份信息＝至少 3 个敏感字段（姓名/身份证、银行卡、手机/邮箱、密码、地址）"),
             url=vulbox_url, note="边缘/废弃业务系统酌情降级；CVSS 高利用难度降级"),
        rule("vulbox", "reward",
             reward="严重/高/中/低：严重 ¥200~4000（按项目）；低危（反射 XSS/普通 CSRF/"
                    "URL 跳转/phpinfo）¥50~200",
             url="https://www.vulbox.com/news/detail-225"),
        # ---------------- CNVD
        rule("cnvd", "accept", include=(
            "【已查证】原创漏洞；通用型（CVSS2.0 基准>4.0，可多实例验证）可获收录证书；"
            "事件型覆盖电信运营商/中央部委等；须可验证（PoC/多实例/验证过程+截图）"),
             url="https://www.cnvd.org.cn/webinfo/list?type=16"),
        rule("cnvd", "exclude",
             exclude="【已查证】小厂商产品、黑盒案例不足 10 起等不颁证书",
             url="https://www.cnvd.org.cn/webinfo/list?type=16"),
        rule("cnvd", "reward",
             reward="积分＝CVSS2.0 基本向量评分 × 目标对象权重系数；三级审核"
                    "（完整性→整理→有效性+原创性查重）；bonus.cnvd.org.cn 积分兑换",
             url="https://zc.cnvd.org.cn/news/info?id=169&newsTypeCode=100100"),
        # ---------------- CNNVD
        rule("cnnvd", "accept", include=(
            "【已查证】依据 GB/T 30279：技术分级（被利用性×影响程度）→ 综合分级"
            "（叠加环境因素）；技术分级为高 ≠ 综合评级为高"),
             url="https://www.cnnvd.org.cn/frontend/aboutUs"),
        rule("cnnvd", "level_mapping",
             levels={"超危": None, "高": None, "中": None, "低": None},
             url="https://www.cnnvd.org.cn/frontend/aboutUs",
             note="非赏金制，供政府/行业/厂商共享通报；数据 XML 格式对外下载"),
    ]


# ================================================================ P0~P4 分级表


def _vuln_priority() -> list[dict[str, Any]]:
    """附录C-2.1 主分级表 + 2.2 拒收清单 + 六差异对照（弱口令补充）。

    权重按附录C-5.2：P0=10, P1=7, P2=4, P3=1, P4=0。
    """
    v = V_SRC
    src21 = "设计书附录C-2.1（综合 ASRC 五级/TSRC 技术+业务维度/漏洞盒子评定原则）"
    src22 = "设计书附录C-2.2 拒收清单"

    def p(vtype: str, pri: str, weight: int, harm: list[str], tendency: str,
          detect: list[str], reject: str, scene: str, source: str = src21) -> dict:
        return {"vuln_type": vtype, "priority": pri, "harm_keywords": harm,
                "weight": weight, "src_tendency": tendency, "detect_points": detect,
                "reject_if": reject, "representative_scene": scene,
                "source": source, "last_verified": v}

    rows = [
        p("rce", "P0", 10, ["系统命令", "服务器权限", "组合利用链"],
          "极高，单价最高（超级资产严重 10 万级）",
          ["找反序列化入口（Redis/Log4j/Fastjson/Jackson）", "模板注入点",
           "可执行命令的功能"], "",
          "云产品管控台 RCE、通用软件 0day、心跳类协议 RCE"),
        p("sqli", "P0", 10, ["敏感数据", "核心库", "批量泄露"],
          "高（须注出≥1 条数据证明危害，严禁拖库）",
          ["参数/JSON/HTTP 头注入点", "盲注用时间/布尔", "WAF 绕过编码分块"],
          "仅报错无数据回显→降 P4；严禁 information_schema 全拖库",
          "核心业务数据库 SQL 注入→获取用户手机号/身份证多元组"),
        p("account_takeover", "P0", 10, ["越权操作", "任意账号", "主账号体系"],
          "高（设计缺陷类，严重/高危）",
          ["验证码爆破", "重置 token 可预测", "重置接口越权", "session 不失效"],
          "需高条件（如爆破 6 位码）降级",
          "主账号体系任意密码重置；6 位验证码爆破重置他人密码"),
        p("idor", "P1", 7, ["越权操作", "核心数据", "业务影响"],
          "高（业务逻辑核心，不需深代码，SRC 最吃香）",
          ["改 user_id/order_id/手机号参数", "改 Role 头/JWT", "双账号对照测"],
          "同一功能增删改查越权只报 1 个（避免合并降权）",
          "改 user_id=1→2 看他人信息；普通用户调管理员接口"),
        p("payment_logic", "P1", 7, ["业务资损", "0 元购", "并发竞态"],
          "高（ASRC 严重：实际海量资损/直接刷钱）",
          ["抓包改金额/数量为负/小数", "优惠券叠加/重复用", "并发重放签到/领奖"],
          "禁真实支付，仅抓包观测；证明资损即停",
          "支付金额改 0.01；优惠券叠加到 0 元；并发刷签到积分"),
        p("ssrf", "P1", 7, ["内网穿透", "云元数据", "敏感数据"],
          "高（ASRC 高危：HTTP/Socks 代理进核心网或回显 SSRF）",
          ["找 URL 抓取/图片代理/webhook 功能", "打 169.254.169.254 云元数据",
           "gopher 打 Redis"],
          "仅能读自身无进一步利用＝降级",
          "图片转存 SSRF→读阿里云/AWS 元数据→取 AK/SK"),
        p("file_upload", "P1", 7, ["webshell", "服务器权限"],
          "高（未授权可组合达 RCE 即收）",
          ["扩展名/Content-Type/图片马/.htaccess 绕过", "二次渲染绕过"],
          "字节等平台须提前报备；拿 webshell 后漏洞盒子禁下载源码审计",
          "头像上传绕过→传 WebShell"),
        p("weak_password", "P1", 7, ["弱口令", "管理员", "系统重要度"],
          "漏洞盒子/补天明确收录（重要系统管理员弱口令=高危）",
          ["默认口令/常见弱口令", "按系统重要度定级"],
          "同系统多用户弱口令只确认第 1 个；后台爆破仅收成功登录案例",
          "重要系统管理员弱口令直接登录后台",
          "设计书附录C-六差异对照（漏洞盒子 faq?id=79 / 补天收录规范）"),
        p("unauthorized_access", "P2", 4, ["未授权", "后台接管", "少量敏感信息"],
          "中（含敏感信息=中危；海量服务器权限=严重/高危）",
          ["路径爆破找 /admin /api", "swagger 未授权", "springboot actuator"],
          "", "/actuator/env 未授权泄露配置；后台未授权"),
        p("stored_xss", "P2", 4, ["Cookie 窃取", "打管理员", "核心功能"],
          "中~高（打管理员获取敏感数据=高危；普通页面存储 XSS=中危）",
          ["找富文本/评论/签名", "打后台审核员/客服"], "",
          "客服后台存储 XSS→盗取客服 session 进管理平台"),
        p("info_disclosure", "P2", 4, ["敏感数据", "配置", "备份文件"],
          "中~低（视字段数；≥3 敏感字段才够严重信息定义）",
          ["/backup /config.php /db.sql", ".git 泄露", "swagger 文档泄露"],
          "不足 3 个敏感字段降级；纯 phpinfo/目录遍历＝P4 拒收",
          "/db_backup.sql 拿到用户明文密码"),
        p("minor_idor", "P2", 4, ["少量越权", "策略绕过"],
          "中（少量核心资产修改=中危）",
          ["短信验证码复用/缺失", "加密算法破解", "安全锁绕过"], "",
          "修改密码后端不校验验证码→任意重置"),
        p("xss", "P3", 1, ["需交互", "影响有限"],
          "低（ASRC 列中危/低危；漏洞盒子低危 ¥50~200）",
          ["callback=jsonp 读敏感接口", "Origin 反射+凭据"],
          "仅非 Chrome 触发＝低危/拒收", "反射 XSS 打 Cookie；CORS ACAO:*+凭据"),
        p("url_redirect", "P3", 1, ["钓鱼辅助", "骚扰"],
          "低~不收（ASRC 低危；需登录/固定域名跳转降级）",
          ["target/url/link/redirect 参数"], "", "登录后跳转钓鱼站"),
        # —— P4 拒收清单（附录C-2.2，12 类，weight=0 → 规则引擎直接判无效不提交）
        p("pure_info_disclosure", "P4", 0, [], "一律不提交",
          [], "phpinfo/目录遍历/异常报错/SVN/内网 IP 域名泄露/无敏感信息 demo",
          "纯信息泄露，无敏感数据无法进一步利用", src22),
        p("config_advice", "P4", 0, [], "一律不提交",
          [], "缺安全头/弱 TLS 套件/无 CSP 等配置建议", "属配置非漏洞，无利用链", src22),
        p("self_xss", "P4", 0, [], "一律不提交",
          [], "仅自己输入自己看到的 XSS", "无受害者、无法触发他人", src22),
        p("harmless_csrf", "P4", 0, [], "一律不提交",
          [], "改头像/退出登录等无敏感操作 CSRF", "无实际危害", src22),
        p("error_no_proof", "P4", 0, [], "一律不提交",
          [], "SQL 报错但无危害证明（不出数据）", "未证明危害", src22),
        p("business_logic", "P4", 0, [], "一律不提交",
          [], "业务正常逻辑/预期行为", "非缺陷", src22),
        p("unreproducible", "P4", 0, [], "一律不提交",
          [], "无法复现/仅理论可行无成功案例/仅给结果无 PoC", "审核员复现不出", src22),
        p("duplicate", "P4", 0, [], "一律不提交",
          [], "与已有/已公开漏洞重复", "已被收录", src22),
        p("test_env", "P4", 0, [], "一律不提交",
          [], "测试环境/测试数据漏洞（未运行实际业务的机器）", "无生产危害", src22),
        p("vendor_eol", "P4", 0, [], "一律不提交",
          [], "厂商已停止维护/权重过低小站", "无处置价值", src22),
        p("non_chrome_xss", "P4", 0, [], "一律不提交",
          [], "仅非 Chrome 触发的 XSS/JSONP", "利用面极窄", src22),
        p("mail_bombing", "P4", 0, [], "一律不提交",
          [], "邮件轰炸/轮询多手机短信轰炸", "骚扰类、非安全漏洞", src22),
    ]
    return rows


# ================================================================ 10 类挖洞思路模板


def _hunt_templates() -> list[dict[str, Any]]:
    v = V_SRC
    src = "设计书附录C-三"
    pre_common = ("已读目标 SRC 规则页确认授权范围与红线；全程双账号/抓包，"
                  "操作留痕截图带时间戳；不拖库、不下源码、不发起真实支付")

    def t(tid: str, scene: str, steps: list[str], tools: list[str], pitfall: str,
          related: str, redline: str, expect: str, pre: str = pre_common) -> dict:
        return {"template_id": tid, "scene": scene, "precondition": pre,
                "step_sequence": steps, "tools": tools, "common_pitfall": pitfall,
                "related_vuln_type": related, "compliance_redline": redline,
                "expected_priority": expect, "source": src, "last_verified": v}

    return [
        t("HUNT-01", "目标资产收集与攻击面梳理", [
            "读 SRC 公告拿授权根域名/资产清单（含子域、APP、小程序、云资产）",
            ("子域名枚举：OneForAll/Subfinder/Assetfinder + site:target.com -www，"
             "配合 DNS 历史记录（viewdns）找未上 CDN 的真实 IP"),
            "IP/端口/指纹：微步/Shodan/Zoomeye 查归属，Nmap 扫端口，Wappalyzer/WhatWeb 识别技术栈",
            "API 与文档：Dirsearch/Gobuster 扫 /api/ /v1/ /swagger-ui.html /v2/api-docs /actuator",
            "敏感文件：/robots.txt /backup/ /config.php /.git/ /db_backup.sql /README.md",
            "移动端/小程序/供应链：反编译 APP 找硬编码 AK/SK/内部接口；抓小程序包找后端 API",
            "产出攻击面清单（域名→服务→技术栈→可疑入口），按 P0~P4 排优先级",
        ], ["OneForAll", "Subfinder", "Nmap", "Wappalyzer", "Dirsearch", "Shodan"],
          "勿超授权范围扫描；测试/预发子域常缺 WAF 但也可能是无效测试环境",
          "unauthorized_access,info_disclosure", "严格在 SRC 公告资产内测", "P2"),
        t("HUNT-02", "登录与认证绕过 / 任意密码重置", [
            "走完整注册→登录→改密→找回密流程，抓全部接口",
            "测验证码：可复用/后端不校验（空值/固定值）/爆破（6 位无次数限制）/返回包泄露",
            "测重置 token：可预测/与用户无关、改 user=victim 参数、重置链接不绑定会话",
            "测 session：登录前后是否刷新（会话固定）、退出后 Cookie 是否失效",
            "测 JWT：弱密钥爆破、alg:none、篡改 role",
            "判定：能接管任意主账号＝P0；需高条件（爆破 6 位码）降级",
        ], ["Burp", "jwt_tool", "hashcat"], "验证码复用误判（前端校验假象）",
          "account_takeover", "爆破注意频率，禁造成验证码服务不可用", "P0"),
        t("HUNT-03", "支付/优惠券/订单业务逻辑", [
            "定位商品下单→结算→支付→退款全链路接口，全部抓包",
            "金额篡改：改 price/amount 为 0.01、负数、极小小数；改数量为负",
            "优惠券：叠加互斥券、一次性券重复用、改券面额/适用范围",
            "并发竞态：Burp Intruder 并发重放签到/领奖/提现接口，看是否重复到账",
            "退款/积分：退款后货物未回收、积分重复计算、负数金额溢出",
        ], ["Burp Intruder", "抓包代理"], "前端改价后端重算则无效",
          "payment_logic", "禁止真实下单/真实支付，仅抓包观测请求与响应；证明资损即可停", "P1"),
        t("HUNT-04", "水平/垂直越权", [
            "注册 A、B 两个测试账号，A 登录抓包",
            "水平：把 user_id/order_id/phone 从 A 的值改成 B 的值，看能否读/改 B 的数据",
            "垂直：用普通用户 A 的 Cookie 直接调管理员接口，或改 Role:user→admin",
            "遍历：ID 从自增 1 开始遍历，统计可获取的数据量与字段敏感度",
            "判定：核心数据任意遍历＝P0/P1；非核心少量越权＝P2/P3",
        ], ["Burp", "双测试账号"], "同一功能增删改查越权只报 1 个（避免合并降权）",
          "idor,minor_idor", "仅用自建测试账号数据验证，不遍历真实用户隐私", "P1"),
        t("HUNT-05", "SSRF 打内网 / 云元数据", [
            "找所有服务端取 URL 的功能：图片转存、网址预览、webhook、PDF 生成、导入远程文件",
            "基础验证：URL 改 127.0.0.1/内网 IP 看回显（区分真 SSRF 与伪 SSRF）",
            ("打云元数据：http://100.100.100.200/（阿里云）/"
             "http://169.254.169.254/latest/meta-data/（AWS），读临时 AK/SK"),
            "进阶：gopher 协议打内网 Redis/Memcached；dict 探测端口；DNS rebinding 绕白名单",
            "回显受限：用 OOB（DNSLog/带外平台）确认盲 SSRF",
            "判定：可进核心网络代理或可回显＝高危；仅能读自身无进一步利用＝降级",
        ], ["Burp Collaborator", "DNSLog", "gopher 协议"],
          "真 SSRF vs 伪 SSRF（仅展示固定域名）；IMDSv2 需 token 打不动",
          "ssrf", "不探测与目标无关的内网资产；云凭据只验证不滥用", "P1"),
        t("HUNT-06", "文件上传绕过 getshell", [
            "找上传点（头像/附件/富文本/导入），抓包",
            "绕扩展名：.phtml/.php5/.pHp 大小写、.jpg.php、.htaccess、.user.ini",
            "绕 Content-Type：filename=shell.php 但 Content-Type: image/jpeg",
            "绕内容校验：图片马（GIF89a 头）、二次渲染绕过、截断 %00",
            "上传后访问路径确认解析，执行 whoami 证明",
        ], ["Burp", "图片马", "冰蝎/哥斯拉（报备后）"], "二次渲染破坏马内容",
          "file_upload",
          "字节等平台文件上传须提前报备；拿 webshell 后漏洞盒子禁止下载源码审计", "P1"),
        t("HUNT-07", "注入（SQL）绕 WAF", [
            "探测点：URL 参数、表单、JSON body、HTTP 头（User-Agent/Referer）",
            "输入 ' / and 1=1 / and 1=2 看报错与页面变化",
            "无回显：时间盲注 if(1=1,sleep(5),1)、布尔盲注逐字符猜解",
            "绕 WAF：内联注释 /*!*/、大小写混写、编码（URL/双重/Hex）、分块、等价函数",
            "只注出 1 条数据证明危害即停；判定：核心库＝高危，非核心＝中危，报错无数据＝拒收",
        ], ["sqlmap", "Burp", "手工盲注脚本"], "拖库被处罚；WAF 拦截误判为不存在",
          "sqli", "严禁 information_schema 全拖库（漏洞盒子明确严禁）；字节注入禁读表", "P0"),
        t("HUNT-08", "JSONP / CORS 配置类", [
            "JSONP：找 ?callback=xxx 接口，看是否返回敏感数据且 callback 未校验来源",
            "CORS：发 Origin: https://evil.com，看 ACAO 是否反射且 Allow-Credentials: true",
            "构造 PoC 页面跨域读取敏感接口数据",
            "判定：能劫持敏感信息（手机号/订单）＝中危；仅非 Chrome 触发/无敏感数据＝低危/拒收",
        ], ["Burp", "PoC 页面"], "无凭据的 CORS 反射无危害",
          "xss", "PoC 只读自建账号数据", "P3"),
        t("HUNT-09", "SSO / 联合登录", [
            "梳理 OAuth/CAS/SSO 授权流程：authorize→callback→token",
            "测 callback 地址是否校验（改 redirect_uri 到 evil 域名偷 code/token）",
            "测 state 参数是否缺失（CSRF 绑定他人账号）",
            "测登出后 token 是否失效、跨子域 cookie 作用域过宽",
            "判定：可劫持他人登录态/账号绑定＝高危",
        ], ["Burp"], "redirect_uri 多级校验（后缀/包含匹配绕过）",
          "account_takeover", "只劫持自建账号验证", "P1"),
        t("HUNT-10", "测试环境 / 内部系统 / 供应链 N-day", [
            ("测试/预发环境：子域名/历史 DNS 找 test./uat./pre./dev. 子域，"
             "常缺 WAF/弱口令/调试接口开放"),
            "内部系统：oa./admin./crm./jenkins./gitlab./wiki. 未授权或弱口令",
            ("供应链 N-day：识别组件版本（Struts2/Log4j/Fastjson/Shiro/Confluence），"
             "对照最新 CVE 打 PoC；N-day 红利期短，爆发后第一时间扫资产"),
            ("0day vs N-day 取舍：量产优先 N-day（命中率高、周期短）；"
             "高价值目标深耕 0day（ASRC 0Day 须有实际影响案例）"),
        ], ["Nmap", "nuclei", "CVE PoC 库"], "测试环境漏洞可能被判定无效（ASRC）",
          "rce,unauthorized_access,weak_password",
          "N-day 扫描控制频率；确认资产归属再测", "P0~P2"),
    ]


# ================================================================ 误报特征库


def _fp_signatures() -> list[dict[str, Any]]:
    v_code, v_src = V_CODE, V_SRC

    def fp(fp_id: str, vtype: str, desc: str, kind: str, ref: str,
           action: str, source: str, verified: str) -> dict:
        return {"fp_id": fp_id, "vuln_type": vtype, "desc": desc,
                "check_kind": kind, "check_ref": ref, "action": action,
                "source": source, "last_verified": verified}

    return [
        # —— 命名代码检查（与核验流水线 _fp_check 内置逻辑一一对应）
        fp("fp-encoded-reflect", "xss",
           "反射内容经 HTML 实体编码，浏览器不可执行", "code", "xss_encoded",
           "reject", "内置核验流水线（verify/pipeline.py）", v_code),
        fp("fp-static-error-page", "sqli",
           "报错特征来自静态自定义错误页（基线响应即含同样特征，且无布尔差异）",
           "code", "error_in_baseline", "reject",
           "内置核验流水线（verify/pipeline.py）", v_code),
        fp("fp-canary-no-reflect", "ssrf",
           "canary 出现在响应中但为原样回显参数而非实际请求结果",
           "code", "ssrf_echo_only", "reject",
           "内置核验流水线（verify/pipeline.py）", v_code),
        # —— 正则模式检查（附录C-2.2 拒收清单的机器可判子集）
        fp("fp-self-xss", "xss",
           "Self-XSS（仅自己输入自己看到）：无受害者，各平台明确不收",
           "pattern", r"(?i)self[-_ ]?xss", "reject",
           "设计书附录C-2.2（ASRC 无等级清单）", v_src),
        fp("fp-test-env", "*",
           "测试环境/测试数据漏洞（未运行实际业务的机器）：ASRC 判无效",
           "pattern", r"(?i)\b(test|uat|pre|dev|staging)\d*\.", "downgrade",
           "设计书附录C-2.2（ASRC§七 影响测试数据=无效）", v_src),
        fp("fp-non-chrome-only", "xss",
           "仅非 Chrome 触发的 XSS/JSONP：利用面极窄，ASRC 最高低危",
           "pattern", r"(?i)(firefox|safari|\bie\b|edge)\s*(only|仅)", "downgrade",
           "设计书附录C-2.2（ASRC§七第 8 条）", v_src),
        fp("fp-mail-bombing", "*",
           "邮件轰炸/轮询多手机短信轰炸：骚扰类、非安全漏洞",
           "pattern", r"(?i)(邮件|短信)轰炸|mail[- ]?bomb", "reject",
           "设计书附录C-2.2（ASRC 无等级清单）", v_src),
    ]


# ================================================================ PoC 模板库


def _poc_templates() -> list[dict[str, Any]]:
    """与 verify/poc_templates.py 内置逻辑同源的模板镜像（结构化可检索）。"""
    src = "内置 PoC 模板（verify/poc_templates.py）同源镜像"
    v = V_CODE

    def poc(tid: str, vtype: str, name: str, spec: dict, verify: str) -> dict:
        return {"template_id": tid, "vuln_type": vtype, "name": name,
                "request_spec": spec, "verify_method": verify,
                "source": src, "last_verified": v}

    return [
        poc("POC-SQLI-001", "sqli", "SQL 注入报错回显 PoC",
            {"method": "GET", "params_from": "payload_or_default",
             "default_payload": "'", "expect": "sql_error"},
            "响应含 SQL 报错特征（mysql_/ORA-/SQLSTATE 等）"),
        poc("POC-XSS-001", "xss", "反射型 XSS 原始反射 PoC",
            {"method": "GET", "params_from": "evidence.payload",
             "expect": "raw_reflection", "canary": "spirxss"},
            "响应包含未编码的 canary+标签反射"),
        poc("POC-SSRF-001", "ssrf", "SSRF canary 带外证明 PoC",
            {"method": "GET", "params_from": "evidence.canary_url",
             "expect": "substring", "substring": "SPIR-SSRF-CANARY"},
            "响应含 canary 服务返回特征（证明服务端实际发起请求）"),
        poc("POC-INFO-001", "info_disclosure", "敏感信息泄露复访 PoC",
            {"method": "GET", "params_from": "none",
             "marker_from": "evidence.marker",
             "expect": "substring_or_status_200"},
            "复访返回敏感标记或 200（证明泄露可稳定复现）"),
    ]


# ================================================================ 赛制规则库（基线）


def _competition_rules() -> list[dict[str, Any]]:
    src = "设计书 4.5/5.3（赛制自适应）"
    v = V_CODE
    return [
        {"rule_id": "CONTEST-JEOPARDY-BASE", "contest_type": "jeopardy",
         "name": "Jeopardy 解题赛基线",
         "rules": {"调度": "6.7 优先级公式：分值×成功率预估×时间衰减÷资源消耗",
                   "提交": "按赛方 rate_limit_per_minute 限频，格式校验+去重",
                   "资源": "max_parallel 并发题数，解出即转移资源"},
         "source": src, "last_verified": v},
        {"rule_id": "CONTEST-AWD-BASE", "contest_type": "awd",
         "name": "AWD 攻防赛基线",
         "rules": {"边界": "仅在赛方授权 CIDR 内攻击；禁对平台基础设施发起 DoS",
                   "调度": "awd_rebalance 按得分/失分动态调整攻防配比",
                   "提交": "按轮次提交 Flag，频率限制从赛方规则解析"},
         "source": src, "last_verified": v},
        {"rule_id": "CONTEST-RANGE-BASE", "contest_type": "range",
         "name": "靶场渗透赛基线",
         "rules": {"链路": "SRC 链路：侦察→探测→全链路核验→渗透报告",
                   "交付": "渗透报告（非 WriteUp）",
                   "边界": "按赛方划定靶标范围，Flag 与漏洞双产出"},
         "source": src, "last_verified": v},
    ]


# ================================================================ 资产库（攻击面沉淀）


def _assets() -> list[dict[str, Any]]:
    src = "设计书附录C-三 场景1（攻击面梳理步骤 4-5/场景10）"
    v = V_SRC

    def a(pid: str, name: str, data: dict) -> dict:
        return {"pattern_id": pid, "category": "attack_surface", "name": name,
                "data": data, "source": src, "last_verified": v}

    return [
        a("ASSET-API-DOCS", "未授权 API/文档常见路径",
          {"paths": ["/api/", "/v1/", "/swagger-ui.html", "/v2/api-docs", "/actuator"],
           "用途": "Dirsearch/Gobuster 扫未授权接口文档"}),
        a("ASSET-SENSITIVE-FILES", "敏感文件常见路径",
          {"paths": ["/robots.txt", "/backup/", "/config.php", "/.git/",
                     "/db_backup.sql", "/README.md"],
           "用途": "敏感信息泄露面"}),
        a("ASSET-TEST-SUBDOMAINS", "测试/预发环境子域前缀",
          {"prefixes": ["test.", "uat.", "pre.", "dev."],
           "用途": "常缺 WAF/弱口令/调试接口；注意可能被平台判测试环境无效"}),
        a("ASSET-INTERNAL-SYSTEMS", "内部系统子域前缀",
          {"prefixes": ["oa.", "admin.", "crm.", "jenkins.", "gitlab.", "wiki."],
           "用途": "未授权访问或弱口令高发面"}),
    ]


# ================================================================ 报告模板与驳回原因库


def _report_templates() -> list[dict[str, Any]]:
    v = V_SRC
    src42 = "设计书附录C-4.2（Bug Bounty Playbook + 各平台规则）"
    src44 = "设计书附录C-4.4 常见被驳回原因与规避"

    six = [
        {"name": "标题", "guidance": "[平台/业务]+漏洞类型+影响对象，"
         "如「抖音某用户中心接口存在水平越权可遍历任意用户手机号」；"
         "带「任意用户/批量/未授权/RCE/越权」关键词"},
        {"name": "漏洞描述", "guidance": "非技术语言讲清「在哪发现→怎么利用→影响范围」，"
         "点明业务危害（可泄露哪些敏感数据/可造成多少资损）"},
        {"name": "危害等级判定", "guidance": "对照平台标准自评严重/高/中/低并给理由；"
         "照抄平台资产档×漏洞类型表对应奖励档自评（别夸大也别压低）"},
        {"name": "复现步骤", "guidance": "编号分步，每步配 HTTP 请求包文本+截图"
         "（含 URL、Payload、响应结果、时间戳），审核员照做即可复现"},
        {"name": "影响范围证明", "guidance": "数据打码后展示泄露字段（证明≥3 敏感字段）/"
         "whoami 回显/订单金额篡改前后对比"},
        {"name": "修复建议", "guidance": "可落地：接口加 user_id 与登录态绑定校验/"
         "金额服务端重算/SSRF 目标地址白名单等"},
    ]

    def rej(tid: str, reason: str, avoid: str) -> dict:
        return {"template_id": tid, "kind": "rejection", "platform": "",
                "title": reason, "structure": [{"name": "规避方法", "guidance": avoid}],
                "example": "", "source": src44, "last_verified": v}

    return [
        {"template_id": "RPT-SIX-SECTION-001", "kind": "six_section", "platform": "",
         "title": "SRC 漏洞报告六段式标准模板", "structure": six,
         "example": "标题示例：某接口越权导致任意用户订单信息泄露",
         "source": src42, "last_verified": v},
        {"template_id": "RPT-WRITING-001", "kind": "writing_rule", "platform": "",
         "title": "撰写规范（标题/定级/截图/PoC/AI 标记）",
         "structure": [
             {"name": "标题命名", "guidance": "带高价值关键词，避免模糊标题"},
             {"name": "定级对照", "guidance": "能刷钱/接管主账号/核心库拖数据→严重；"
              "越权改核心数据/RCE 生产机→高危；少量敏感数据/需交互→中危"},
             {"name": "截图要求", "guidance": "带时间戳、敏感数据打码"},
             {"name": "PoC 要求", "guidance": "可一键复现脚本/完整请求包"
              "（TSRC 复杂链路+PoC 可拿最高 5000 高质量报告奖）"},
             {"name": "AI 报告标记", "guidance": "AI 辅助生成须经人工复现并附完整 PoC+截图"
              "（字节 2026 立规，未人工复现无效）"},
         ], "example": "", "source": "设计书附录C-4.3/4.4/5.4", "last_verified": v},
        rej("RPT-REJECT-001", "重复提交",
            "提交前查平台已有漏洞列表/已公开细节；同漏洞源只报 1 个；"
            "被标重复要原报告号，参数/端点/链路不同则具体申诉"),
        rej("RPT-REJECT-002", "无实际危害 / 自证型",
            "必须给成功案例+危害量化（截图证明拿到敏感数据/造成资损），不只写「理论可行」"),
        rej("RPT-REJECT-003", "不可复现",
            "自己先完整跑一遍；步骤+请求包+截图齐全含时间戳；不直接交 AI 草稿"
            "（字节明确 AI 报告须人工复现）"),
        rej("RPT-REJECT-004", "配置类 / 纯信息泄露",
            "只提交能进一步利用的泄露（含敏感数据≥3 字段），纯 phpinfo/目录遍历不提交"),
        rej("RPT-REJECT-005", "超出授权范围",
            "严格在 SRC 公告资产内测；敏感测试先报备（字节）；不碰未授权资产"),
        rej("RPT-REJECT-006", "拖库/下载源码/高并发",
            "只注 1 条数据；拿 webshell 先问平台；禁高并发与 DoS"),
        rej("RPT-REJECT-007", "与已有漏洞源合并",
            "同接口/同上传组件/同验证码失效导致的多洞合并报 1 个，避免被降权"),
    ]
