"""持久化多来源 CVE 原文、检索索引、同步水位与本地检测模板。"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

CVE_ID = re.compile(r"CVE-\d{4}-\d{4,}", re.I)


def cve_id(value: str) -> str:
    value = value.upper().strip()
    if not CVE_ID.fullmatch(value):
        raise ValueError("需要完整 CVE 编号，如 CVE-2021-44228")
    return value


def normalize(record: dict[str, Any], source: str) -> dict[str, Any]:
    """只投影检索字段；版本区间、CPE 布尔树等完整保留在原文中。"""
    if source == "cvelist":
        meta = record["cveMetadata"]
        containers = record.get("containers", {})
        cna = containers.get("cna", {})
        descriptions = cna.get("descriptions", []) or cna.get("rejectedReasons", [])
        text = " ".join(d.get("value", "") for d in descriptions)
        title = cna.get("title", "")
        affected = cna.get("affected", [])
        products = " ".join(str(a.get(k, "")) for a in affected for k in ("vendor", "product", "packageName"))
        # ADP 补充数据也进入搜索，完整条件仍由模型读取原文判断。
        extra = " ".join(json.dumps(a, ensure_ascii=False) for a in containers.get("adp", []))
        return {"id": cve_id(meta["cveId"]), "modified": meta.get("dateUpdated", ""),
                "state": meta.get("state", "UNKNOWN"), "title": title,
                "description": text, "products": products, "extra": extra}
    if source == "nvd":
        record = record.get("cve", record)
        return {"id": cve_id(record["id"]), "modified": record.get("lastModified", ""),
                "state": record.get("vulnStatus", "UNKNOWN"), "title": "",
                "description": " ".join(d.get("value", "") for d in record.get("descriptions", [])),
                "products": json.dumps(record.get("configurations", []), ensure_ascii=False), "extra": ""}
    raise ValueError("不支持的 CVE 记录来源")


class CVEStore:
    """短连接 + WAL，供多个会话、导入进程与查询共享。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records (
                    cve_id TEXT NOT NULL, source TEXT NOT NULL, modified TEXT NOT NULL,
                    state TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
                    products TEXT NOT NULL, extra TEXT NOT NULL, raw TEXT NOT NULL,
                    fetched REAL NOT NULL, PRIMARY KEY(cve_id, source));
                CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
                    cve_id, title, description, products, extra, content=records,
                    content_rowid=rowid, tokenize='unicode61');
                CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
                    INSERT INTO records_fts(rowid,cve_id,title,description,products,extra)
                    VALUES(new.rowid,new.cve_id,new.title,new.description,new.products,new.extra);
                END;
                CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
                    INSERT INTO records_fts(records_fts,rowid,cve_id,title,description,products,extra)
                    VALUES('delete',old.rowid,old.cve_id,old.title,old.description,old.products,old.extra);
                END;
                CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
                    INSERT INTO records_fts(records_fts,rowid,cve_id,title,description,products,extra)
                    VALUES('delete',old.rowid,old.cve_id,old.title,old.description,old.products,old.extra);
                    INSERT INTO records_fts(rowid,cve_id,title,description,products,extra)
                    VALUES(new.rowid,new.cve_id,new.title,new.description,new.products,new.extra);
                END;
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, data TEXT NOT NULL, fetched REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS templates (
                    path TEXT PRIMARY KEY, template_id TEXT NOT NULL, cves TEXT NOT NULL,
                    name TEXT NOT NULL, severity TEXT NOT NULL, protocol TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=60)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put_many(self, records: list[tuple[str, dict[str, Any]]], *, fetched_at: float | None = None) -> int:
        with self.connect() as db:
            for source, raw in records:
                r = normalize(raw, source)
                # 同源旧快照不能覆盖较新的增量；拒绝记录仍保存并可被准确检索。
                db.execute('''INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(cve_id,source) DO UPDATE SET modified=excluded.modified,
                    state=excluded.state,title=excluded.title,description=excluded.description,
                    products=excluded.products,extra=excluded.extra,raw=excluded.raw,fetched=excluded.fetched
                    WHERE excluded.modified > records.modified
                    OR (excluded.modified = records.modified AND excluded.fetched > records.fetched)''',
                    (r['id'], source, r['modified'], r['state'], r['title'], r['description'],
                     r['products'], r['extra'], json.dumps(raw, ensure_ascii=False), fetched_at if fetched_at is not None else time.time()))
        return len(records)

    def detail(self, ident: str) -> dict[str, Any]:
        ident = cve_id(ident)
        with self.connect() as db:
            records = [{"source": r["source"],
                        "url": (f"https://www.cve.org/CVERecord?id={ident}" if r["source"] == "cvelist"
                                else f"https://nvd.nist.gov/vuln/detail/{ident}"),
                        "modified": r["modified"], "state": r["state"],
                        "fetched_at": r["fetched"], "record": json.loads(r["raw"])}
                       for r in db.execute("SELECT * FROM records WHERE cve_id=? ORDER BY source", (ident,))]
            templates = [dict(r) for r in db.execute(
                "SELECT * FROM templates WHERE cves LIKE ?", (f'%"{ident}"%',))]
        for template in templates:
            template["cves"] = json.loads(template["cves"])
        return {"cve_id": ident, "records": records, "local_templates": templates}

    def search(self, query: str, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        terms = re.findall(r"[^\W_]+", query, re.UNICODE)
        if not terms:
            raise ValueError("检索词不能为空")
        match = " AND ".join('"' + t + '"' for t in terms)
        with self.connect() as db:
            total = db.execute('''SELECT count(DISTINCT r.cve_id) FROM records_fts f
                JOIN records r ON r.rowid=f.rowid WHERE records_fts MATCH ?''', (match,)).fetchone()[0]
            rows = db.execute('''WITH matched AS MATERIALIZED (
                SELECT r.*,bm25(records_fts,10,8,3,6,0.1) rank FROM records_fts f
                JOIN records r ON r.rowid=f.rowid WHERE records_fts MATCH ?)
                SELECT cve_id,group_concat(DISTINCT source) sources,max(title) title,
                max(modified) modified,group_concat(DISTINCT state) states,
                max(substr(description,1,700)) description FROM matched
                GROUP BY cve_id ORDER BY min(rank),modified DESC,cve_id LIMIT ? OFFSET ?''',
                (match, limit, offset)).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "offset": offset,
                "next_offset": offset + len(rows) if offset + len(rows) < total else None,
                "match": "keyword_candidates_only; version applicability requires source conditions"}

    def cache_get(self, key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM cache WHERE key=?", (key,)).fetchone()
        return {"data": json.loads(row["data"]), "fetched_at": row["fetched"]} if row else None

    def cache_put(self, key: str, data: Any) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                       (key, json.dumps(data, ensure_ascii=False), time.time()))

    def meta_put(self, key: str, data: Any) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(data)))

    def meta_get(self, key: str) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT data FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def status(self) -> dict[str, Any]:
        with self.connect() as db:
            return {"cves": db.execute("SELECT count(DISTINCT cve_id) FROM records").fetchone()[0],
                    "sources": [dict(r) for r in db.execute('''SELECT source,count(*) records,
                        max(modified) newest_modified FROM records GROUP BY source''')],
                    "templates": db.execute("SELECT count(*) FROM templates").fetchone()[0],
                    "sync": {r["key"]: json.loads(r["data"]) for r in db.execute("SELECT * FROM meta")},
                    "notice": "本地收录规模不等于全网覆盖率；记录、模板与真实漏洞验证互相独立。"}
