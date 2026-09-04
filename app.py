# -*- coding: utf-8 -*-
"""白泽之眼（旧版 SQLite 看板）。用法：python app.py  →  http://127.0.0.1:8765"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.stdout.reconfigure(encoding="utf-8")

from config import DB_PATH, PLATFORM_DIR, STATS_PATH

_STATS_CACHE = None

HOST = "0.0.0.0"
PORT = 8765
TEMPLATE_PATH = PLATFORM_DIR / "templates" / "index.html"


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def has_fts(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='companies_fts'"
    ).fetchone()
    return bool(row)


def parse_json_field(s):
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


def row_to_card(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["phones"] = parse_json_field(d.get("phones"))
    d["emails"] = parse_json_field(d.get("emails"))
    d["honors"] = parse_json_field(d.get("honors"))
    d["chain_nodes"] = parse_json_field(d.get("chain_nodes"))
    return d


def filters_sql(qs: dict) -> tuple[str, list]:
    where = ["1=1"]
    args: list = []
    q = (qs.get("q") or [""])[0].strip()
    if q:
        where.append(
            "(company_name LIKE ? OR legal_person LIKE ? OR credit_code LIKE ? "
            "OR chain_industries LIKE ? OR address LIKE ? OR scope LIKE ?)"
        )
        like = f"%{q}%"
        args.extend([like] * 6)
    for field, col in (
        ("province", "province"),
        ("city", "city"),
        ("district", "district"),
        ("street", "street"),
    ):
        v = (qs.get(field) or [""])[0].strip()
        if v:
            where.append(f"{col} = ?")
            args.append(v)
    addr_kw = (qs.get("addr") or [""])[0].strip()
    if addr_kw:
        where.append("address LIKE ?")
        args.append(f"%{addr_kw}%")
    for field, col in (
        ("scale", "tag_scale"),
        ("lifecycle", "tag_lifecycle"),
        ("digital", "tag_digital"),
        ("finance", "tag_finance"),
        ("position", "chain_positions"),
    ):
        v = (qs.get(field) or [""])[0].strip()
        if v:
            where.append(f"{col} LIKE ?")
            args.append(f"%{v}%")
    industry = (qs.get("industry") or [""])[0].strip()
    if industry:
        where.append("chain_industries LIKE ?")
        args.append(f"%{industry}%")
    fit = (qs.get("fit") or [""])[0].strip()
    if fit:
        where.append("tag_fit LIKE ?")
        args.append(f"%{fit}%")
    honor = (qs.get("honor") or [""])[0].strip()
    if honor:
        where.append("honors LIKE ?")
        args.append(f"%{honor}%")
    min_score = (qs.get("min_score") or [""])[0].strip()
    if min_score.isdigit():
        where.append("lead_score >= ?")
        args.append(int(min_score))
    return " AND ".join(where), args


EXPORT_COLS = [
    "lead_score", "company_name", "credit_code", "legal_person", "phones", "emails",
    "website", "province", "city", "district", "street", "address",
    "tag_scale", "tag_lifecycle", "tag_digital", "tag_finance", "tag_fit",
    "chain_industries", "chain_positions", "relatedness", "honors",
    "capital_raw", "insured_count", "established", "status", "financing_raw",
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code=200, body=b"", content_type="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = unquote(u.path)
        try:
            if path in ("/", "/index.html"):
                if not DB_PATH.exists():
                    self._send(
                        200,
                        "<h2>还没有线索库。请先运行 python build_leads.py</h2>".encode("utf-8"),
                    )
                    return
                self._send(200, TEMPLATE_PATH.read_text(encoding="utf-8").encode("utf-8"))
                return
            if path == "/api/stats":
                self._json(self.stats())
                return
            if path == "/api/geo":
                self._json(self.geo(qs))
                return
            if path == "/api/search":
                self._json(self.search(qs))
                return
            if path == "/api/company":
                key = (qs.get("key") or [""])[0]
                self._json(self.company(key))
                return
            if path == "/api/export":
                self.export(qs)
                return
            self._send(404, b"not found")
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def stats(self):
        global _STATS_CACHE
        if _STATS_CACHE is None and STATS_PATH.exists():
            _STATS_CACHE = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        if _STATS_CACHE is not None:
            return _STATS_CACHE
        conn = db()
        total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        conn.close()
        return {"total": total, "reachable": 0, "high": 0, "listed": 0,
                "provinces": [], "provinces_all": [], "scales": [], "life": [],
                "fit": [], "score_bins": [], "industries": []}

    def search(self, qs):
        page = max(int((qs.get("page") or ["1"])[0] or 1), 1)
        size = min(max(int((qs.get("page_size") or ["50"])[0] or 50), 1), 200)
        where, args = filters_sql(qs)
        conn = db()
        total = conn.execute(f"SELECT COUNT(*) FROM companies WHERE {where}", args).fetchone()[0]
        rows = conn.execute(
            f"""SELECT id, credit_key, company_name, credit_code, legal_person, phones, emails,
                       website, province, city, district, street, tag_scale, tag_lifecycle, tag_digital,
                       tag_finance, tag_fit, chain_industries, chain_positions, relatedness,
                       honors, capital_raw, insured_count, established, lead_score, status,
                       financing_raw
                FROM companies WHERE {where}
                ORDER BY lead_score DESC, id
                LIMIT ? OFFSET ?""",
            args + [size, (page - 1) * size],
        ).fetchall()
        conn.close()
        return {"total": total, "page": page, "page_size": size, "rows": [row_to_card(r) for r in rows]}

    def company(self, key: str):
        conn = db()
        r = conn.execute("SELECT * FROM companies WHERE credit_key = ?", (key,)).fetchone()
        conn.close()
        return row_to_card(r) if r else {}

    def _has_street(self, conn) -> bool:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
        return "street" in cols

    def geo(self, qs):
        """省 → 市 → 区 → 街道 联动选项。"""
        province = (qs.get("province") or [""])[0].strip()
        city = (qs.get("city") or [""])[0].strip()
        district = (qs.get("district") or [""])[0].strip()
        conn = db()
        has_street = self._has_street(conn)
        if not province:
            rows = conn.execute(
                "SELECT province AS name, COUNT(*) AS n FROM companies "
                "WHERE province != '' GROUP BY province ORDER BY n DESC"
            ).fetchall()
            conn.close()
            return {"level": "province", "items": [{"name": r["name"], "n": r["n"]} for r in rows]}
        if not city:
            rows = conn.execute(
                "SELECT city AS name, COUNT(*) AS n FROM companies "
                "WHERE province = ? AND city != '' GROUP BY city ORDER BY n DESC",
                (province,),
            ).fetchall()
            conn.close()
            return {"level": "city", "items": [{"name": r["name"], "n": r["n"]} for r in rows]}
        if not district:
            rows = conn.execute(
                "SELECT district AS name, COUNT(*) AS n FROM companies "
                "WHERE province = ? AND city = ? AND district != '' "
                "GROUP BY district ORDER BY n DESC",
                (province, city),
            ).fetchall()
            conn.close()
            return {"level": "district", "items": [{"name": r["name"], "n": r["n"]} for r in rows]}
        if not has_street:
            conn.close()
            return {"level": "street", "items": []}
        rows = conn.execute(
            "SELECT street AS name, COUNT(*) AS n FROM companies "
            "WHERE province = ? AND city = ? AND district = ? AND street != '' "
            "GROUP BY street ORDER BY n DESC LIMIT 200",
            (province, city, district),
        ).fetchall()
        conn.close()
        return {"level": "street", "items": [{"name": r["name"], "n": r["n"]} for r in rows]}

    def export(self, qs):
        where, args = filters_sql(qs)
        conn = db()
        rows = conn.execute(
            f"SELECT {','.join(EXPORT_COLS)} FROM companies WHERE {where} "
            f"ORDER BY lead_score DESC LIMIT 20000",
            args,
        ).fetchall()
        conn.close()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(EXPORT_COLS)
        for r in rows:
            w.writerow(list(r))
        data = buf.getvalue().encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=leads_export.csv")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    if not DB_PATH.exists():
        print("未找到 output/leads.db，请先运行：python build_leads.py")
        sys.exit(1)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.allow_reuse_address = True
    print(f"白泽之眼已启动： http://127.0.0.1:{PORT}/")
    print("局域网访问：把 127.0.0.1 换成这台电脑的 IP")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
