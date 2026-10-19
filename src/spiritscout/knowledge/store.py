"""知识库存储：五库 + 两经验库（设计书 3.5），SQLite 结构化 + FTS5 关键词检索。

库表映射（3.5「五库+两经验库」）：
- 五库：base_assets（资产库·跨目标攻击面沉淀）/ base_vuln_standards（漏洞标准库·
  多平台可配置规则）/ base_fp_signatures（误报特征库）/ base_poc_templates（PoC
  模板库）/ base_competition_rules（赛制规则库）；
- 红队经验库（3.3）：exp_attack_map（ATT&CK 27 行映射）/ exp_redteam_entries
  （checklist 条目，字段对标 Atomic Red Team + 红队实战扩展）/ exp_attack_paths
  （6 类攻击路径模板）；
- SRC 经验库（3.4）：exp_vuln_priority（P0~P4 分级含权重）/ exp_hunt_templates
  （10 类挖洞思路模板）/ exp_report_templates（六段式报告模板 + 驳回原因库）。

引用规范（3.5）：每条种子数据带 source + last_verified；过期条目标 deprecated
保留审计，不物理删除。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from spiritscout.core.exceptions import ConfigurationError
from spiritscout.policy.engine import PlatformRule

# ---------------------------------------------------------------- schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 五库-1 资产库：跨目标长期沉淀（攻击面模式/敏感路径/指纹，3.2.4-7 双层记忆的长期层）
CREATE TABLE IF NOT EXISTS base_assets (
    pattern_id    TEXT PRIMARY KEY,
    category      TEXT NOT NULL,          -- attack_surface / fingerprint / bypass_pattern
    name          TEXT NOT NULL,
    data          TEXT NOT NULL,          -- JSON
    source        TEXT NOT NULL,
    last_verified TEXT NOT NULL,
    deprecated    INTEGER NOT NULL DEFAULT 0
);

-- 五库-2 漏洞标准库：多平台可配置规则（3.4.5 / 附录C-5.1 字段）
CREATE TABLE IF NOT EXISTS base_vuln_standards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,          -- asrc/tsrc/bytesrc/butian/vulbox/cnvd/cnnvd
    asset_tier    TEXT NOT NULL DEFAULT '',
    rule_type     TEXT NOT NULL,          -- accept/exclude/level_mapping/reward/redline/merge_dedup/approval
    include_condition TEXT NOT NULL DEFAULT '',
    exclude_condition TEXT NOT NULL DEFAULT '',
    level_mapping TEXT NOT NULL DEFAULT '{}',   -- JSON
    reward_range  TEXT NOT NULL DEFAULT '',
    auto_test_allowed INTEGER,            -- 1/0/NULL(未明确)
    report_required TEXT NOT NULL DEFAULT '{}', -- JSON：报备要求
    source_url    TEXT NOT NULL,
    verified_at   TEXT NOT NULL,
    effective_date TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    deprecated    INTEGER NOT NULL DEFAULT 0
);

-- 五库-3 误报特征库
CREATE TABLE IF NOT EXISTS base_fp_signatures (
    fp_id         TEXT PRIMARY KEY,
    vuln_type     TEXT NOT NULL,          -- * 表示通用
    desc          TEXT NOT NULL,
    check_kind    TEXT NOT NULL,          -- code（命名代码检查）/ pattern（正则）
    check_ref     TEXT NOT NULL,          -- code 检查名 或 正则表达式
    action        TEXT NOT NULL DEFAULT 'reject',   -- reject / downgrade
    source        TEXT NOT NULL,
    last_verified TEXT NOT NULL,
    deprecated    INTEGER NOT NULL DEFAULT 0
);

-- 五库-4 PoC 模板库
CREATE TABLE IF NOT EXISTS base_poc_templates (
    template_id   TEXT PRIMARY KEY,
    vuln_type     TEXT NOT NULL,
    name          TEXT NOT NULL,
    request_spec  TEXT NOT NULL,          -- JSON：method/params_from/expect/substring/canary
    verify_method TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL,
    last_verified TEXT NOT NULL,
    deprecated    INTEGER NOT NULL DEFAULT 0
);

-- 五库-5 赛制规则库
CREATE TABLE IF NOT EXISTS base_competition_rules (
    rule_id       TEXT PRIMARY KEY,
    contest_type  TEXT NOT NULL,          -- jeopardy/awd/range
    name          TEXT NOT NULL,
    rules         TEXT NOT NULL,          -- JSON
    source        TEXT NOT NULL,
    last_verified TEXT NOT NULL,
    deprecated    INTEGER NOT NULL DEFAULT 0
);

-- 五库-6 运行环境能力事实（「地图」条目：只陈述存在什么，供 kb_search 命中）
CREATE TABLE IF NOT EXISTS base_env_capabilities (
    fact_id       TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    facts         TEXT NOT NULL,          -- JSON list
    source        TEXT NOT NULL,
    last_verified TEXT NOT NULL
);

-- 红队经验库：ATT&CK 战术-技术映射（附录B-三，27 行真实 ID）
CREATE TABLE IF NOT EXISTS exp_attack_map (
    id             INTEGER PRIMARY KEY,
    tactic_id      TEXT NOT NULL,
    tactic_name    TEXT NOT NULL,
    technique_id   TEXT NOT NULL,
    technique_name TEXT NOT NULL,
    sub_techniques TEXT NOT NULL DEFAULT '',
    operation      TEXT NOT NULL,
    tools          TEXT NOT NULL DEFAULT '',
    agent_type     TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL,
    last_verified  TEXT NOT NULL
);

-- 红队经验库：checklist 条目（3.3.5 字段，对标 Atomic Red Team + 红队实战扩展）
CREATE TABLE IF NOT EXISTS exp_redteam_entries (
    entry_id      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    tactic_ids    TEXT NOT NULL,          -- JSON list
    technique_id  TEXT NOT NULL,
    os_platform   TEXT NOT NULL,          -- JSON list
    preconditions TEXT NOT NULL,          -- JSON list（硬门槛，3.3.6）
    steps         TEXT NOT NULL,          -- JSON list of {step, action, command, expected}
    tools         TEXT NOT NULL,          -- JSON list
    verify_method TEXT NOT NULL,          -- 喂给漏洞全链路核验（3.3.6 核验闭环）
    expected_result TEXT NOT NULL DEFAULT '',
    fail_handling TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL,
    severity      TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT '',
    last_verified TEXT NOT NULL,
    deprecated    INTEGER NOT NULL DEFAULT 0
);

-- 红队经验库：攻击路径模板（3.3.4 / 附录B-四，6 类）
CREATE TABLE IF NOT EXISTS exp_attack_paths (
    path_id       TEXT PRIMARY KEY,
    scenario      TEXT NOT NULL,
    entry_conditions TEXT NOT NULL,       -- 入口条件（主 Agent 据此判断是否启用）
    step_sequence TEXT NOT NULL,          -- JSON list of {step, action, precondition, tools, verify}
    decision_points TEXT NOT NULL DEFAULT '',
    failure_handling TEXT NOT NULL DEFAULT '',
    related_tactics TEXT NOT NULL,        -- JSON list（用于 DAG 生成）
    prerequisite_knowledge TEXT NOT NULL DEFAULT '',
    sensitivity   TEXT NOT NULL DEFAULT 'standard',  -- sensitive=钓鱼/外泄，默认禁用
    source        TEXT NOT NULL,
    last_verified TEXT NOT NULL,
    deprecated    INTEGER NOT NULL DEFAULT 0
);

-- SRC 经验库：漏洞优先级分级（附录C-2.1/2.2/5.2，含权重字段）
CREATE TABLE IF NOT EXISTS exp_vuln_priority (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    vuln_type     TEXT NOT NULL,
    priority      TEXT NOT NULL,          -- P0/P1/P2/P3/P4
    harm_keywords TEXT NOT NULL DEFAULT '[]',   -- JSON
    weight        INTEGER NOT NULL,       -- P0=10,P1=7,P2=4,P3=1,P4=0（附录C-5.2）
    src_tendency  TEXT NOT NULL DEFAULT '',
    detect_points TEXT NOT NULL DEFAULT '[]',   -- JSON
    reject_if     TEXT NOT NULL DEFAULT '',
    representative_scene TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL,
    last_verified TEXT NOT NULL
);

-- SRC 经验库：挖洞思路模板（附录C-三，10 类场景）
CREATE TABLE IF NOT EXISTS exp_hunt_templates (
    template_id   TEXT PRIMARY KEY,
    scene         TEXT NOT NULL,
    precondition  TEXT NOT NULL,
    step_sequence TEXT NOT NULL,          -- JSON list of str
    tools         TEXT NOT NULL,          -- JSON list
    common_pitfall TEXT NOT NULL DEFAULT '',
    related_vuln_type TEXT NOT NULL DEFAULT '',
    compliance_redline TEXT NOT NULL DEFAULT '',
    expected_priority TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL,
    last_verified TEXT NOT NULL
);

-- SRC 经验库：报告模板 / 撰写规范 / 驳回原因库（附录C-4.2/4.4/5.4）
CREATE TABLE IF NOT EXISTS exp_report_templates (
    template_id   TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,          -- six_section / writing_rule / rejection
    platform      TEXT NOT NULL DEFAULT '',   -- 空=通用
    title         TEXT NOT NULL,
    structure     TEXT NOT NULL,          -- JSON
    example       TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL,
    last_verified TEXT NOT NULL
);
"""

# 各表 JSON 列（写入序列化、读出解析）
_JSON_COLUMNS: dict[str, frozenset[str]] = {
    "base_assets": frozenset({"data"}),
    "base_vuln_standards": frozenset({"level_mapping", "report_required"}),
    "base_poc_templates": frozenset({"request_spec"}),
    "base_competition_rules": frozenset({"rules"}),
    "base_env_capabilities": frozenset({"facts"}),
    "exp_redteam_entries": frozenset(
        {"tactic_ids", "os_platform", "preconditions", "steps", "tools"}),
    "exp_attack_paths": frozenset({"step_sequence", "related_tactics"}),
    "exp_vuln_priority": frozenset({"harm_keywords", "detect_points"}),
    "exp_hunt_templates": frozenset({"step_sequence", "tools"}),
    "exp_report_templates": frozenset({"structure"}),
}

# 库名 → 表名（对外检索/统计口径）
BASES: dict[str, str] = {
    "assets": "base_assets",
    "vuln_standards": "base_vuln_standards",
    "fp_signatures": "base_fp_signatures",
    "poc_templates": "base_poc_templates",
    "competition_rules": "base_competition_rules",
    "env_capabilities": "base_env_capabilities",
    "attack_map": "exp_attack_map",
    "redteam_entries": "exp_redteam_entries",
    "attack_paths": "exp_attack_paths",
    "vuln_priority": "exp_vuln_priority",
    "hunt_templates": "exp_hunt_templates",
    "report_templates": "exp_report_templates",
}

# ---------------------------------------------------------------- FTS 分词

_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
_CJK_RUN_RE = re.compile(r"[一-鿿　-ヿ＀-￯]+")


def fts_terms(text: str) -> list[str]:
    """FTS5 unicode61 不拆中文，索引与查询统一做「ASCII 词 + CJK 二元组」分词。"""
    tokens = _ASCII_TOKEN_RE.findall(text)
    for run in _CJK_RUN_RE.findall(text):
        chars = [c for c in run if not c.isspace()]
        if len(chars) == 1:
            tokens.append(chars[0])
        else:
            tokens.extend("".join(chars[i:i + 2]) for i in range(len(chars) - 1))
    return tokens


def _fts_text(text: str) -> str:
    return " ".join(fts_terms(text))


# ---------------------------------------------------------------- 存储


class KnowledgeStore:
    """五库+两经验库统一存取。线程安全；FTS5 缺失时显式报错（不静默降级）。"""

    def __init__(self, db_path: str | Path, *, auto_seed: bool = True):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5("
                "base, entry_key, title, content, tokenize='unicode61')")
        except sqlite3.OperationalError as exc:
            raise ConfigurationError(
                f"SQLite FTS5 不可用，无法建立知识库关键词索引: {exc}。"
                f"请使用编译了 FTS5 的 Python/SQLite（官方 CPython 默认包含）"
            ) from exc
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        # RLock：ensure_seeded 持锁重建时会经 iter_search_docs→_rows 再次取锁
        self._lock = threading.RLock()
        if auto_seed:
            self.ensure_seeded()

    def _migrate(self) -> None:
        """轻量迁移：为旧库补齐新增列（CREATE IF NOT EXISTS 不会改已有表）。

        迁移项随版本登记；保持「新增列均有默认值」约束，保证 ALTER 幂等。
        """
        migrations = [
            # 敏感路径标记（钓鱼/外泄默认禁用）
            ("exp_attack_paths", "sensitivity",
             ("ALTER TABLE exp_attack_paths ADD COLUMN "
              "sensitivity TEXT NOT NULL DEFAULT 'standard'")),
        ]
        for table, column, ddl in migrations:
            cols = {r["name"] for r in
                    self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(ddl)

    # ------------------------------------------------------------ 种子

    def seed_version(self) -> str:
        row = self._conn.execute(
            "SELECT value FROM kb_meta WHERE key='seed_version'").fetchone()
        return row["value"] if row else ""

    def ensure_seeded(self) -> bool:
        """种子版本不一致则整体重建种子（幂等），返回是否执行了重建。"""
        from spiritscout.knowledge.seed import SEED_VERSION, seed_rows

        if self.seed_version() == SEED_VERSION:
            return False
        rows_by_table = seed_rows()
        with self._lock:
            for table in BASES.values():
                self._conn.execute(f"DELETE FROM {table}")
            for table, rows in rows_by_table.items():
                self._insert_rows(table, rows)
            self._conn.execute(
                "INSERT INTO kb_meta(key, value) VALUES('seed_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SEED_VERSION,))
            self._conn.execute("DELETE FROM kb_fts")
            for doc in self.iter_search_docs():
                self._conn.execute(
                    "INSERT INTO kb_fts(base, entry_key, title, content) "
                    "VALUES(?,?,?,?)",
                    (doc["base"], doc["entry_key"], doc["title"],
                     _fts_text(doc["content"])))
            self._conn.commit()
        return True

    def _insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        json_cols = _JSON_COLUMNS.get(table, frozenset())
        for row in rows:
            cols = list(row)
            values = [
                json.dumps(row[c], ensure_ascii=False) if c in json_cols else row[c]
                for c in cols
            ]
            placeholders = ",".join("?" for _ in cols)
            self._conn.execute(
                f"INSERT OR REPLACE INTO {table}({','.join(cols)}) "
                f"VALUES({placeholders})", values)

    # ------------------------------------------------------------ 内部读取

    def _rows(self, table: str, where: str = "", params: tuple = ()) -> list[dict]:
        sql = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
        with self._lock:
            cur = self._conn.execute(sql, params)
            out = []
            for row in cur.fetchall():
                item = dict(row)
                for col in _JSON_COLUMNS.get(table, frozenset()):
                    if col in item and isinstance(item[col], str):
                        item[col] = json.loads(item[col])
                if "auto_test_allowed" in item and item["auto_test_allowed"] is not None:
                    item["auto_test_allowed"] = bool(item["auto_test_allowed"])
                if "deprecated" in item:
                    item["deprecated"] = bool(item["deprecated"])
                out.append(item)
            return out

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ 统计

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        with self._lock:
            for base, table in BASES.items():
                out[base] = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        out["total"] = sum(out.values())
        return out

    # ------------------------------------------------------------ 红队经验库检索

    def attack_map(self, *, tactic_id: str | None = None,
                   technique_id: str | None = None) -> list[dict]:
        where, params = [], []
        if tactic_id:
            where.append("tactic_id=?")
            params.append(tactic_id)
        if technique_id:
            where.append("technique_id=?")
            params.append(technique_id)
        return self._rows("exp_attack_map",
                          " AND ".join(where), tuple(params))

    def experiences(self, *, tactic_id: str | None = None,
                    technique_id: str | None = None,
                    os_platform: str | None = None,
                    category: str | None = None) -> list[dict]:
        """checklist 条目检索（3.3.6：tactic_id + os_platform + preconditions）。

        preconditions 为自然语言硬门槛，由调用方（子 Agent/主 Agent）结合当前
        情报评估是否满足；库侧返回结构化清单与全部前置条件字段。
        """
        rows = [r for r in self._rows("exp_redteam_entries") if not r["deprecated"]]
        if tactic_id:
            rows = [r for r in rows if tactic_id in r["tactic_ids"]]
        if technique_id:
            rows = [r for r in rows
                    if r["technique_id"] == technique_id
                    or r["technique_id"].split(".")[0] == technique_id.split(".")[0]]
        if os_platform:
            rows = [r for r in rows if os_platform in r["os_platform"]
                    or "通用" in r["os_platform"]]
        if category:
            rows = [r for r in rows if r["category"] == category]
        return rows

    def attack_paths(self, *, path_id: str | None = None,
                     scenario_like: str | None = None,
                     include_sensitive: bool = False) -> list[dict]:
        """攻击路径模板检索（3.3.4）。

        钓鱼/外泄（sensitivity=sensitive）默认不返回，仅当调用方声明
        授权范围允许敏感动作（include_sensitive=True）时才放开。
        """
        rows = [r for r in self._rows("exp_attack_paths") if not r["deprecated"]]
        if not include_sensitive:
            rows = [r for r in rows if r.get("sensitivity", "standard") == "standard"]
        if path_id:
            rows = [r for r in rows if r["path_id"] == path_id]
        if scenario_like:
            rows = [r for r in rows if scenario_like in r["scenario"]]
        return rows

    # ------------------------------------------------------------ SRC 经验库检索

    def platform_rules(self, platform: str, *,
                       rule_type: str | None = None) -> list[dict]:
        where, params = ["platform=?"], [platform.lower()]
        if rule_type:
            where.append("rule_type=?")
            params.append(rule_type)
        return [r for r in self._rows("base_vuln_standards",
                                      " AND ".join(where), tuple(params))
                if not r["deprecated"]]

    def platform_rule_view(self, platform: str | None) -> PlatformRule:
        """聚合平台规则行为策略引擎运行时视图（红线 + 报备项）。"""
        p = (platform or "").lower()
        view = PlatformRule(platform=p)
        for row in self.platform_rules(p) if p else []:
            if row["auto_test_allowed"] is False:
                view.auto_test_allowed = False
            for act in (row["report_required"] or {}).get("敏感测试", []):
                view.requires_approval_for.add(act)
        return view

    def vuln_priority(self, *, vuln_type: str | None = None,
                      priority: str | None = None) -> list[dict]:
        where, params = [], []
        if vuln_type:
            where.append("vuln_type=?")
            params.append(vuln_type)
        if priority:
            where.append("priority=?")
            params.append(priority)
        return self._rows("exp_vuln_priority",
                          " AND ".join(where), tuple(params))

    def priority_for(self, vuln_type: str) -> dict | None:
        """按漏洞类型查分级（精确匹配；未收录类型返回 None 由调用方按基线处理）。"""
        rows = self.vuln_priority(vuln_type=vuln_type)
        return rows[0] if rows else None

    def hunt_templates(self, *, scene: str | None = None,
                       vuln_type: str | None = None) -> list[dict]:
        rows = self._rows("exp_hunt_templates")
        if scene:
            rows = [r for r in rows if scene in r["scene"]]
        if vuln_type:
            rows = [r for r in rows
                    if vuln_type in (r["related_vuln_type"] or "").split(",")]
        return rows

    def report_templates(self, *, kind: str | None = None,
                         platform: str | None = None) -> list[dict]:
        rows = self._rows("exp_report_templates")
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        if platform is not None:
            rows = [r for r in rows if r["platform"] in ("", platform)]
        return rows

    # ------------------------------------------------------------ 五库检索

    def fp_signatures(self, *, vuln_type: str | None = None,
                      active_only: bool = True) -> list[dict]:
        rows = self._rows("base_fp_signatures")
        if active_only:
            rows = [r for r in rows if not r["deprecated"]]
        if vuln_type:
            rows = [r for r in rows if r["vuln_type"] in ("*", vuln_type)]
        return rows

    def poc_templates(self, *, vuln_type: str | None = None) -> list[dict]:
        rows = [r for r in self._rows("base_poc_templates") if not r["deprecated"]]
        if vuln_type:
            rows = [r for r in rows if r["vuln_type"] == vuln_type]
        return rows

    def competition_rules(self, *, contest_type: str | None = None) -> list[dict]:
        rows = self._rows("base_competition_rules")
        if contest_type:
            rows = [r for r in rows if r["contest_type"] == contest_type]
        return rows

    def asset_patterns(self, *, category: str | None = None) -> list[dict]:
        rows = [r for r in self._rows("base_assets") if not r["deprecated"]]
        if category:
            rows = [r for r in rows if r["category"] == category]
        return rows

    # ------------------------------------------------------------ 检索文档投影

    def iter_search_docs(self) -> list[dict[str, str]]:
        """把各库条目投影为统一检索文档（FTS 与 Chroma 共用同一投影）。"""
        docs: list[dict[str, str]] = []

        def add(base: str, key: Any, title: str, *parts: Any) -> None:
            content = " ".join(str(p) for p in parts if p)
            docs.append({"base": base, "entry_key": str(key),
                         "title": title, "content": f"{title} {content}"})

        for r in self._rows("exp_attack_map"):
            add("attack_map", r["id"], f"{r['technique_id']} {r['technique_name']}",
                r["tactic_name"], r["operation"], r["tools"], r["agent_type"],
                r["sub_techniques"])
        for r in self._rows("exp_redteam_entries"):
            steps = " ".join(f"{s.get('action', '')} {s.get('command', '')}"
                             for s in r["steps"])
            add("redteam_entries", r["entry_id"], r["title"],
                r["technique_id"], " ".join(r["preconditions"]), steps,
                r["verify_method"], r["expected_result"], r["fail_handling"],
                r["category"])
        for r in self._rows("exp_attack_paths"):
            steps = " ".join(f"{s.get('action', '')} {s.get('verify', '')}"
                             for s in r["step_sequence"])
            add("attack_paths", r["path_id"], r["scenario"],
                r["entry_conditions"], steps, r["decision_points"],
                r["failure_handling"], r["prerequisite_knowledge"])
        for r in self._rows("base_env_capabilities"):
            add("env_capabilities", r["fact_id"], r["title"],
                " ".join(r["facts"]), r["source"])
        for r in self._rows("base_vuln_standards"):
            add("vuln_standards", f"{r['platform']}:{r['id']}",
                f"{r['platform']} {r['rule_type']}",
                r["include_condition"], r["exclude_condition"],
                r["reward_range"], r["note"], r["asset_tier"])
        for r in self._rows("exp_vuln_priority"):
            add("vuln_priority", f"priority:{r['id']}",
                f"{r['priority']} {r['vuln_type']}",
                " ".join(r["harm_keywords"]), " ".join(r["detect_points"]),
                r["reject_if"], r["representative_scene"], r["src_tendency"])
        for r in self._rows("exp_hunt_templates"):
            add("hunt_templates", r["template_id"], r["scene"],
                r["precondition"], " ".join(r["step_sequence"]),
                " ".join(r["tools"]), r["common_pitfall"],
                r["related_vuln_type"], r["compliance_redline"])
        for r in self._rows("base_fp_signatures"):
            add("fp_signatures", r["fp_id"], r["desc"], r["check_ref"])
        for r in self._rows("base_poc_templates"):
            add("poc_templates", r["template_id"], r["name"], r["verify_method"])
        for r in self._rows("exp_report_templates"):
            struct = " ".join(
                s.get("name", "") + " " + s.get("guidance", "")
                if isinstance(s, dict) else str(s) for s in r["structure"])
            add("report_templates", r["template_id"], r["title"], struct,
                r["example"], r["platform"])
        for r in self._rows("base_competition_rules"):
            add("competition_rules", r["rule_id"], r["name"], r["rules"])
        for r in self._rows("base_assets"):
            add("assets", r["pattern_id"], r["name"], r["data"])
        return docs

    # ------------------------------------------------------------ FTS5 关键词检索

    def search(self, query: str, *, bases: list[str] | None = None,
               limit: int = 10) -> list[dict[str, Any]]:
        """跨库关键词检索（OR 语义 + bm25 排序）。空查询返回空列表。"""
        terms = fts_terms(query)
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        sql = ("SELECT base, entry_key, title, bm25(kb_fts) AS rank "
               "FROM kb_fts WHERE kb_fts MATCH ?")
        params: list[Any] = [match]
        if bases:
            sql += " AND base IN (" + ",".join("?" for _ in bases) + ")"
            params.extend(bases)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_entry(self, base: str, entry_key: str) -> dict | None:
        """按检索结果的 (base, entry_key) 取回完整条目。"""
        table = BASES.get(base)
        if table is None:
            return None
        key_col = {
            "attack_map": "id", "vuln_standards": "id", "vuln_priority": "id",
        }.get(base)
        if base == "attack_map":
            rows = self._rows(table, "id=?", (int(entry_key),))
        elif base == "vuln_standards" or base == "vuln_priority":
            rows = self._rows(table, "id=?", (int(entry_key.rsplit(":", 1)[-1]),))
        else:
            key_col = key_col or {
                "assets": "pattern_id", "fp_signatures": "fp_id",
                "poc_templates": "template_id", "competition_rules": "rule_id",
                "env_capabilities": "fact_id",
                "redteam_entries": "entry_id", "attack_paths": "path_id",
                "hunt_templates": "template_id", "report_templates": "template_id",
            }[base]
            rows = self._rows(table, f"{key_col}=?", (entry_key,))
        return rows[0] if rows else None
