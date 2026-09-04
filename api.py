# -*- coding: utf-8 -*-
"""白泽之眼 · 工商大数据线索池 API（Doris 后端）。

白泽云析旗下产品。启动：uvicorn api:app --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel

import doris_client as dc
from admin_alias import expand_city_names, sql_city_in as _sql_city_in
from amap_config import load_amap_env

PLATFORM = Path(__file__).resolve().parent
TEMPLATE = PLATFORM / "templates" / "index.html"
MAP_TEMPLATE = PLATFORM / "templates" / "map.html"
STATIC = PLATFORM / "static"

app = FastAPI(title="白泽之眼 · 工商大数据线索池", version="2.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
PHONE_DIGITS_RE = re.compile(r"^\d{7,12}$")
CREDIT_RE = re.compile(r"^[0-9A-Za-z]{15,18}$")
PERSON_RE = re.compile(r"^[\u4e00-\u9fff·•]{2,4}$")
HAN_RE = re.compile(r"[\u4e00-\u9fff]")
COMPANY_HINT = (
    "公司", "厂", "店", "中心", "部", "行", "社", "院", "所", "集团", "工作室",
    "有限", "股份", "合伙", "银行", "大学", "学校", "医院",
)


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("'", "\\'")


def city_aliases(city: str, district: str = "") -> list[str]:
    return expand_city_names(city, district)


def sql_city_in(column: str, city: str, district: str = "") -> str:
    """生成 city IN (...)，兼容直辖市工商名与高德城区名。"""
    return _sql_city_in(column, city, _esc, district=district)


def normalize_phone(q: str) -> str:
    s = re.sub(r"[\s\-()+（）]", "", (q or "").strip())
    if s.startswith("0086"):
        s = s[4:]
    elif s.startswith("+86"):
        s = s[3:]
    elif s.startswith("86") and len(s) >= 13:
        s = s[2:]
    return s


def classify_query(q: str) -> tuple[str, str]:
    """返回 (kind, normalized)。

    kind: phone/email/credit/person/company/keyword
    企业名/较长中文短语走 company（名称包含匹配），禁止 MATCH_ANY 拆词误伤。
    """
    raw = (q or "").strip()
    if not raw:
        return "", ""
    if "@" in raw:
        return "email", raw.lower()
    digits = normalize_phone(raw)
    if MOBILE_RE.match(digits) or PHONE_DIGITS_RE.match(digits):
        return "phone", digits
    compact = raw.replace(" ", "")
    if CREDIT_RE.match(compact):
        return "credit", compact.upper()
    han_n = len(HAN_RE.findall(raw))
    # 人名：2–4 字且无企业特征词
    if PERSON_RE.match(raw) and not any(h in raw for h in COMPANY_HINT) and han_n <= 4:
        return "person", raw
    # 企业名 / 品牌短语：含企业特征，或 ≥4 个汉字，或整体较长
    if any(h in raw for h in COMPANY_HINT) or han_n >= 4 or len(compact) >= 6:
        return "company", raw
    return "keyword", raw


Q_FIELDS_ALLOWED = (
    "company", "person", "mobile", "email", "credit", "scope", "address",
)
Q_FIELD_LABEL = {
    "company": "公司名",
    "person": "人名",
    "mobile": "手机号",
    "email": "邮箱",
    "credit": "信用代码",
    "scope": "经营范围",
    "address": "地址",
}


def parse_q_fields(q_fields: str) -> list[str] | None:
    """解析检索维度。None=智能识别；非空 list=显式勾选（OR）。"""
    raw = (q_fields or "").strip().lower().replace("，", ",")
    if not raw or raw in ("auto", "smart", "万能"):
        return None
    out: list[str] = []
    for f in raw.split(","):
        f = f.strip()
        if f in Q_FIELDS_ALLOWED and f not in out:
            out.append(f)
    return out or None


def _q_clause_company(qe: str) -> str:
    # 倒排 MATCH_ALL（快）+ LIKE 保整段；禁止裸 MATCH_ANY
    return (
        f"(c.company_name MATCH_ALL '{qe}' AND c.company_name LIKE '%{qe}%')"
    )


def build_q_condition(q: str, q_fields: str = "") -> tuple[str, bool, str]:
    """构建检索词条件。返回 (sql片段或空, need_contacts, q_kind)。"""
    raw = (q or "").strip()
    if not raw:
        return "", False, ""

    fields = parse_q_fields(q_fields)
    if fields is None:
        # 智能识别：沿用 classify
        kind, norm = classify_query(raw)
        if kind == "phone":
            return f"ct.contact_value = '{_esc(norm)}'", True, kind
        if kind == "email":
            return f"LOWER(ct.contact_value) = '{_esc(norm)}'", True, kind
        if kind == "credit":
            return f"c.credit_code = '{_esc(norm)}'", False, kind
        if kind == "person":
            qe = _esc(norm)
            return f"(c.legal_person = '{qe}')", False, kind
        if kind == "company":
            qe = _esc(norm)
            return f"({_q_clause_company(qe)} OR c.credit_code = '{qe}')", False, kind
        qe = _esc(norm)
        return (
            f"(c.company_name MATCH_ALL '{qe}' OR c.legal_person MATCH_ALL '{qe}' "
            f"OR c.credit_code = '{qe}')",
            False,
            kind,
        )

    # 显式维度：各字段按规则 OR；形态不符的维直接跳过，避免拖慢
    clauses: list[str] = []
    need_contacts = False
    qe = _esc(raw)
    digits = normalize_phone(raw)
    compact = raw.replace(" ", "")
    email_n = raw.lower() if "@" in raw else ""
    contact_only = set(fields).issubset({"mobile", "email"})
    han_n = len(HAN_RE.findall(raw))

    if "company" in fields:
        clauses.append(_q_clause_company(qe))
    if "person" in fields and han_n <= 4 and not any(h in raw for h in COMPANY_HINT):
        clauses.append(f"c.legal_person = '{qe}'")
    if "credit" in fields and (CREDIT_RE.match(compact) or len(compact) >= 15):
        clauses.append(f"c.credit_code = '{_esc(compact.upper())}'")
    if "mobile" in fields:
        if MOBILE_RE.match(digits) or (PHONE_DIGITS_RE.match(digits) and len(digits) >= 7):
            phone_sql = (
                f"ct.contact_type = 'mobile' AND ct.contact_value = '{_esc(digits)}'"
            )
            if contact_only:
                need_contacts = True
                clauses.append(f"({phone_sql})")
            else:
                clauses.append(
                    f"EXISTS (SELECT 1 FROM contacts ct WHERE ct.credit_code = c.credit_code "
                    f"AND {phone_sql})"
                )
    if "email" in fields and email_n:
        mail_sql = (
            f"ct.contact_type = 'email' AND LOWER(ct.contact_value) = '{_esc(email_n)}'"
        )
        if contact_only:
            need_contacts = True
            clauses.append(f"({mail_sql})")
        else:
            clauses.append(
                f"EXISTS (SELECT 1 FROM contacts ct WHERE ct.credit_code = c.credit_code "
                f"AND {mail_sql})"
            )
    if "scope" in fields:
        clauses.append(f"c.scope MATCH_ALL '{qe}'")
    if "address" in fields:
        clauses.append(f"c.address MATCH_ALL '{qe}'")

    if not clauses:
        return "1=0", False, "fields:" + ",".join(fields)
    return "(" + " OR ".join(clauses) + ")", need_contacts, "fields:" + ",".join(fields)


def build_where(
    q: str = "",
    province: str = "",
    city: str = "",
    district: str = "",
    street: str = "",
    industry_l1: str = "",
    industry_l2: str = "",
    industry_l3: str = "",
    scale: str = "",
    status: str = "",
    capital_min: Optional[float] = None,
    capital_max: Optional[float] = None,
    age_min: Optional[float] = None,
    age_max: Optional[float] = None,
    insured_min: Optional[int] = None,
    insured_max: Optional[int] = None,
    has_mobile: Optional[int] = None,
    has_landline: Optional[int] = None,
    has_email: Optional[int] = None,
    has_contact: Optional[int] = None,
    scope_kw: str = "",
    addr_kw: str = "",
    est_year_from: Optional[int] = None,
    est_year_to: Optional[int] = None,
    tag_fit: str = "",
    chain: str = "",
    exclude_blocked: bool = False,
    user_id: str = "default",
    street_empty: bool = False,
    q_fields: str = "",
) -> tuple[str, bool, bool, str]:
    """返回 (where, need_tags, need_contacts, q_kind)。列表/地图共用。"""
    # 地图旧约定：has_mobile=-1 表示不限
    if has_mobile is not None and int(has_mobile) < 0:
        has_mobile = None
    if has_landline is not None and int(has_landline) < 0:
        has_landline = None
    if has_email is not None and int(has_email) < 0:
        has_email = None
    if has_contact is not None and int(has_contact) < 0:
        has_contact = None

    parts = ["1=1"]
    need_tags = False
    need_contacts = False
    q_kind = ""

    if q:
        q_sql, q_need_ct, q_kind = build_q_condition(q, q_fields)
        if q_sql:
            parts.append(q_sql)
        need_contacts = need_contacts or q_need_ct
    if province:
        parts.append(f"c.province = '{_esc(province)}'")
    if city:
        parts.append(f"c.city = '{_esc(city)}'")
    if district:
        parts.append(f"c.district = '{_esc(district)}'")
    if street:
        parts.append(f"c.street = '{_esc(street)}'")
    elif street_empty:
        parts.append("(c.street IS NULL OR c.street = '')")
    if industry_l1:
        parts.append(f"c.industry_l1 = '{_esc(industry_l1)}'")
    if industry_l2:
        parts.append(f"c.industry_l2 = '{_esc(industry_l2)}'")
    if industry_l3:
        parts.append(f"c.industry_l3 = '{_esc(industry_l3)}'")
    if scale:
        parts.append(f"c.scale = '{_esc(scale)}'")
    if status:
        ss = [s.strip() for s in status.split(",") if s.strip()]
        if ss:
            inlist = ",".join(f"'{_esc(s)}'" for s in ss)
            parts.append(f"c.status IN ({inlist})")
    if capital_min is not None:
        parts.append(f"c.capital_wan >= {float(capital_min)}")
    if capital_max is not None:
        parts.append(f"c.capital_wan <= {float(capital_max)}")
    if age_min is not None:
        parts.append(f"c.company_age >= {float(age_min)}")
    if age_max is not None:
        parts.append(f"c.company_age <= {float(age_max)}")
    if insured_min is not None:
        parts.append(f"c.insured_count >= {int(insured_min)}")
    if insured_max is not None:
        parts.append(f"c.insured_count <= {int(insured_max)}")
    if has_mobile is not None:
        parts.append(f"c.has_mobile = {1 if has_mobile else 0}")
    if has_landline is not None:
        parts.append(f"c.has_landline = {1 if has_landline else 0}")
    if has_email is not None:
        parts.append(f"c.has_email = {1 if has_email else 0}")
    if has_contact is not None:
        parts.append(f"c.has_contact = {1 if has_contact else 0}")
    if scope_kw:
        parts.append(f"c.scope MATCH_ANY '{_esc(scope_kw)}'")
    if addr_kw:
        parts.append(f"c.address MATCH_ANY '{_esc(addr_kw)}'")
    if est_year_from is not None:
        parts.append(f"c.established_year >= {int(est_year_from)}")
    if est_year_to is not None:
        parts.append(f"c.established_year <= {int(est_year_to)}")
    if tag_fit:
        need_tags = True
        parts.append(f"t.tag_fit = '{_esc(tag_fit)}'")
    if chain:
        need_tags = True
        parts.append(f"array_contains(t.chain_industries, '{_esc(chain)}')")
    if exclude_blocked:
        parts.append(
            f"c.credit_code NOT IN ("
            f"SELECT credit_code FROM contact_status "
            f"WHERE user_id = '{_esc(user_id)}' AND status = 'blocked')"
        )

    return " AND ".join(parts), need_tags, need_contacts, q_kind


def from_clause(need_tags: bool, need_contacts: bool = False) -> str:
    sql = "FROM companies c"
    if need_contacts:
        sql += " INNER JOIN contacts ct ON c.credit_code = ct.credit_code"
    if need_tags:
        sql += " LEFT JOIN tags t ON c.credit_code = t.credit_code"
    return sql


def build_geo_parts(
    min_lng: Optional[float] = None,
    min_lat: Optional[float] = None,
    max_lng: Optional[float] = None,
    max_lat: Optional[float] = None,
    require_geocoded: bool = True,
    limit_span: bool = False,
) -> tuple[list[str], bool]:
    """地图视野条件；与 build_where 叠加。返回 (parts, has_bbox)。"""
    parts: list[str] = []
    if require_geocoded:
        parts.append("c.lng IS NOT NULL AND c.lat IS NOT NULL")
    has_bbox = None not in (min_lng, min_lat, max_lng, max_lat)
    if has_bbox:
        if float(min_lng) >= float(max_lng) or float(min_lat) >= float(max_lat):
            raise HTTPException(400, "无效视野范围")
        if limit_span and (
            (float(max_lng) - float(min_lng)) > 8 or (float(max_lat) - float(min_lat)) > 6
        ):
            raise HTTPException(400, "请先放大地图到城市级视野")
        parts.append(f"c.lng BETWEEN {float(min_lng)} AND {float(max_lng)}")
        parts.append(f"c.lat BETWEEN {float(min_lat)} AND {float(max_lat)}")
    return parts, has_bbox


def merge_where(base_where: str, extra_parts: list[str]) -> str:
    if not extra_parts:
        return base_where
    return f"{base_where} AND " + " AND ".join(extra_parts)


def attach_contact_preview(rows: list[dict], limit_per: int = 3) -> list[dict]:
    """给结果行挂上手机/座机预览，方便「人名→电话」「电话→公司」闭环。"""
    if not rows:
        return rows
    codes = [r.get("credit_code") for r in rows if r.get("credit_code")]
    if not codes:
        return rows
    inlist = ",".join(f"'{_esc(c)}'" for c in codes)
    cts = dc.query(
        f"SELECT credit_code, contact_type, contact_value FROM contacts "
        f"WHERE credit_code IN ({inlist}) "
        f"AND contact_type IN ('mobile', 'landline') "
        f"ORDER BY is_primary DESC, contact_type"
    )
    bucket: dict[str, list[dict]] = {}
    for x in cts:
        arr = bucket.setdefault(x["credit_code"], [])
        if len(arr) >= limit_per:
            continue
        arr.append({
            "contact_type": x["contact_type"],
            "contact_value": x["contact_value"],
        })
    for r in rows:
        r["contacts_preview"] = bucket.get(r.get("credit_code") or "", [])
    return rows


Q_KIND_LABEL = {
    "phone": "按电话命中",
    "email": "按邮箱命中",
    "credit": "按信用代码命中",
    "person": "按人名命中",
    "company": "按企业名称命中",
    "keyword": "按名称/法人包含命中",
}


def q_kind_label(q_kind: str) -> str:
    if not q_kind:
        return ""
    if q_kind.startswith("fields:"):
        keys = q_kind.split(":", 1)[1].split(",")
        names = [Q_FIELD_LABEL.get(k, k) for k in keys if k]
        if not names:
            return "按勾选维度命中"
        if len(names) >= len(Q_FIELDS_ALLOWED):
            return "万能检索（全维度）"
        return "按" + "+".join(names) + "命中"
    return Q_KIND_LABEL.get(q_kind, "")


def _haversine_m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@app.get("/", response_class=HTMLResponse)
def index():
    if TEMPLATE.exists():
        return TEMPLATE.read_text(encoding="utf-8")
    return HTMLResponse("<h1>白泽之眼 · 工商大数据线索池</h1>")


@app.get("/map", response_class=HTMLResponse)
def map_page():
    if MAP_TEMPLATE.exists():
        return MAP_TEMPLATE.read_text(encoding="utf-8")
    return HTMLResponse("<h1>地图页缺失</h1>")


@app.get("/api/health")
def health():
    ok = dc.wait_ready(timeout=5, quiet=True)
    if not ok:
        raise HTTPException(503, "Doris 未就绪")
    rows = dc.query("SELECT COUNT(*) AS n FROM companies")
    geo = dc.query("SELECT COUNT(*) AS n FROM companies WHERE lng IS NOT NULL AND lat IS NOT NULL")
    street_n = dc.query(
        "SELECT COUNT(*) AS n FROM companies WHERE street IS NOT NULL AND street != ''"
    )
    return {
        "ok": True,
        "companies": rows[0]["n"] if rows else 0,
        "with_street": street_n[0]["n"] if street_n else 0,
        "geocoded": geo[0]["n"] if geo else 0,
    }


@app.get("/api/amap_config")
def amap_config():
    """仅下发 Web JS Key（不暴露 Web 服务 Key）。"""
    cfg = load_amap_env()
    return {
        "js_key": cfg.get("AMAP_JS_KEY") or "",
        "security_code": cfg.get("AMAP_SECURITY_CODE") or "",
        "has_web_key": bool(cfg.get("AMAP_WEB_KEY")),
    }


@app.get("/api/search")
def search(
    q: str = "",
    province: str = "",
    city: str = "",
    district: str = "",
    street: str = "",
    industry_l1: str = "",
    industry_l2: str = "",
    industry_l3: str = "",
    scale: str = "",
    status: str = "存续",
    capital_min: Optional[float] = None,
    capital_max: Optional[float] = None,
    age_min: Optional[float] = None,
    age_max: Optional[float] = None,
    insured_min: Optional[int] = None,
    insured_max: Optional[int] = None,
    has_mobile: Optional[int] = None,
    has_landline: Optional[int] = None,
    has_email: Optional[int] = None,
    has_contact: Optional[int] = None,
    scope_kw: str = "",
    addr_kw: str = "",
    est_year_from: Optional[int] = None,
    est_year_to: Optional[int] = None,
    tag_fit: str = "",
    chain: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort: str = "capital_wan",
    order: str = "desc",
    q_fields: str = Query(
        "",
        description="检索维度：company,person,mobile,email,credit,scope,address；空=智能识别",
    ),
):
    where, need_tags, need_contacts, q_kind = build_where(
        q, province, city, district, street, industry_l1, industry_l2, industry_l3,
        scale, status, capital_min, capital_max, age_min, age_max,
        insured_min, insured_max, has_mobile, has_landline, has_email, has_contact,
        scope_kw, addr_kw, est_year_from, est_year_to, tag_fit, chain,
        q_fields=q_fields,
    )
    sort_col = sort if SAFE_IDENT.match(sort) else "capital_wan"
    allowed = {
        "capital_wan", "company_age", "insured_count", "established_year",
        "company_name", "established", "mobile_count", "fill_score",
    }
    if sort_col not in allowed:
        sort_col = "capital_wan"
    ord_dir = "ASC" if order.lower() == "asc" else "DESC"
    offset = (page - 1) * page_size
    frm = from_clause(need_tags, need_contacts)

    if need_contacts:
        cnt = dc.query(f"SELECT COUNT(DISTINCT c.credit_code) AS n {frm} WHERE {where}")
    else:
        cnt = dc.query(f"SELECT COUNT(*) AS n {frm} WHERE {where}")
    total = int(cnt[0]["n"]) if cnt else 0

    # 名称类检索：先精确/前缀/短名，再按用户选择的业务排序
    name_rank = ""
    use_name_rank = q and (
        q_kind in ("company", "keyword", "person")
        or (q_kind.startswith("fields:") and "company" in q_kind)
    )
    if use_name_rank:
        qe = _esc(q.strip())
        name_rank = (
            f"(c.company_name = '{qe}') DESC, "
            f"(c.company_name LIKE '{qe}%') DESC, "
            f"char_length(c.company_name) ASC, "
        )

    distinct = "DISTINCT" if need_contacts else ""
    sql = f"""
        SELECT {distinct} c.credit_code, c.company_name, c.status, c.legal_person, c.scale,
               c.capital_wan, c.capital_raw, c.established, c.company_age,
               c.province, c.city, c.district, c.street, c.company_type,
               c.industry_l1, c.industry_l2, c.industry_l3,
               c.insured_count, c.address, c.has_mobile, c.has_landline,
               c.has_email, c.has_contact, c.mobile_count, c.email_count,
               c.website, c.lng, c.lat, LEFT(c.scope, 200) AS scope_preview
        {frm}
        WHERE {where}
        ORDER BY {name_rank}(c.{sort_col} IS NULL) ASC, c.{sort_col} {ord_dir}
        LIMIT {page_size} OFFSET {offset}
    """
    rows = attach_contact_preview(dc.query(sql))
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": rows,
        "q_kind": q_kind,
        "q_kind_label": q_kind_label(q_kind),
        "q_fields": parse_q_fields(q_fields),
    }


@app.get("/api/company/{credit_code}")
def company_detail(credit_code: str, user_id: str = "default"):
    code = _esc(credit_code)
    rows = dc.query(f"SELECT * FROM companies WHERE credit_code = '{code}' LIMIT 1")
    if not rows:
        raise HTTPException(404, "未找到")
    company = rows[0]
    contacts = dc.query(
        f"SELECT contact_type, contact_value, source, is_primary, collected_at "
        f"FROM contacts WHERE credit_code = '{code}' ORDER BY contact_type, is_primary DESC"
    )
    tags = dc.query(f"SELECT * FROM tags WHERE credit_code = '{code}' LIMIT 1")
    st = dc.query(
        f"SELECT status, note, updated_at FROM contact_status "
        f"WHERE credit_code = '{code}' AND user_id = '{_esc(user_id)}' LIMIT 1"
    )
    return {
        "company": company,
        "contacts": contacts,
        "tags": tags[0] if tags else None,
        "contact_status": st[0] if st else None,
    }


@app.get("/api/stats")
def stats(
    q: str = "",
    province: str = "",
    city: str = "",
    district: str = "",
    street: str = "",
    industry_l1: str = "",
    industry_l2: str = "",
    industry_l3: str = "",
    scale: str = "",
    status: str = "存续",
    capital_min: Optional[float] = None,
    capital_max: Optional[float] = None,
    age_min: Optional[float] = None,
    age_max: Optional[float] = None,
    insured_min: Optional[int] = None,
    insured_max: Optional[int] = None,
    has_mobile: Optional[int] = None,
    has_landline: Optional[int] = None,
    has_email: Optional[int] = None,
    has_contact: Optional[int] = None,
    scope_kw: str = "",
    addr_kw: str = "",
    est_year_from: Optional[int] = None,
    est_year_to: Optional[int] = None,
    tag_fit: str = "",
    chain: str = "",
    q_fields: str = "",
):
    """统计必须与 /api/search 使用同一套筛选，避免「命中企业」与列表条数不一致。"""
    where, need_tags, need_contacts, _q_kind = build_where(
        q, province, city, district, street, industry_l1, industry_l2, industry_l3,
        scale, status, capital_min, capital_max, age_min, age_max,
        insured_min, insured_max, has_mobile, has_landline, has_email, has_contact,
        scope_kw, addr_kw, est_year_from, est_year_to, tag_fit, chain,
        q_fields=q_fields,
    )
    frm = from_clause(need_tags, need_contacts)
    cnt_expr = "COUNT(DISTINCT c.credit_code)" if need_contacts else "COUNT(*)"
    sum_mobile = (
        "COUNT(DISTINCT CASE WHEN c.has_mobile = 1 THEN c.credit_code END)"
        if need_contacts else "SUM(c.has_mobile)"
    )
    sum_landline = (
        "COUNT(DISTINCT CASE WHEN c.has_landline = 1 THEN c.credit_code END)"
        if need_contacts else "SUM(c.has_landline)"
    )
    sum_email = (
        "COUNT(DISTINCT CASE WHEN c.has_email = 1 THEN c.credit_code END)"
        if need_contacts else "SUM(c.has_email)"
    )
    sum_contact = (
        "COUNT(DISTINCT CASE WHEN c.has_contact = 1 THEN c.credit_code END)"
        if need_contacts else "SUM(c.has_contact)"
    )

    summary = dc.query(f"""
        SELECT {cnt_expr} AS total,
               {sum_mobile} AS with_mobile,
               {sum_landline} AS with_landline,
               {sum_email} AS with_email,
               {sum_contact} AS with_contact,
               AVG(c.capital_wan) AS avg_capital,
               AVG(c.insured_count) AS avg_insured
        {frm} WHERE {where}
    """)[0]

    grp_cnt = "COUNT(DISTINCT c.credit_code)" if need_contacts else "COUNT(*)"
    by_province = dc.query(f"""
        SELECT c.province AS k, {grp_cnt} AS n
        {frm} WHERE {where}
          AND c.province IS NOT NULL AND c.province != ''
        GROUP BY c.province ORDER BY n DESC LIMIT 12
    """)
    by_industry = dc.query(f"""
        SELECT c.industry_l1 AS k, {grp_cnt} AS n
        {frm} WHERE {where}
          AND c.industry_l1 IS NOT NULL AND c.industry_l1 != ''
        GROUP BY c.industry_l1 ORDER BY n DESC LIMIT 12
    """)
    # 列表页仅用 summary / 省分布 / 行业 Top；规模与年份分布暂不算，避免拖慢首屏
    return {
        "summary": summary,
        "by_province": by_province,
        "by_industry": by_industry,
        "by_scale": [],
        "by_year": [],
    }


@app.get("/api/facets")
def facets():
    provinces = dc.query(
        """
        SELECT province AS k, COUNT(*) AS n FROM companies
        WHERE province IN ('江苏省','浙江省','上海市','安徽省',
            '北京市','天津市','重庆市','广东省','福建省','山东省',
            '河南省','河北省','湖北省','湖南省','江西省','四川省',
            '云南省','贵州省','山西省','陕西省','辽宁省','吉林省',
            '黑龙江省','海南省','甘肃省','青海省',
            '广西壮族自治区','内蒙古自治区','新疆维吾尔自治区',
            '宁夏回族自治区','西藏自治区')
        GROUP BY province ORDER BY n DESC
        """
    )
    industries = dc.query(
        """
        SELECT industry_l1 AS k, COUNT(*) AS n FROM companies
        WHERE industry_l1 IN (
            '批发和零售业','制造业','租赁和商务服务业','科学研究和技术服务业',
            '建筑业','信息传输、软件和信息技术服务业','农、林、牧、渔业',
            '文化、体育和娱乐业','交通运输、仓储和邮政业','房地产业',
            '居民服务、修理和其他服务业','住宿和餐饮业','教育','金融业',
            '水利、环境和公共设施管理业','卫生和社会工作',
            '电力、热力、燃气及水生产和供应业','公共管理、社会保障和社会组织',
            '采矿业','国际组织'
        )
        GROUP BY industry_l1 ORDER BY n DESC
        """
    )
    return {
        "provinces": provinces,
        "scales": dc.query(
            "SELECT scale AS k, COUNT(*) AS n FROM companies "
            "WHERE scale IS NOT NULL GROUP BY scale ORDER BY n DESC"
        ),
        "statuses": dc.query(
            "SELECT status AS k, COUNT(*) AS n FROM companies "
            "WHERE status IS NOT NULL GROUP BY status ORDER BY n DESC"
        ),
        "industries": industries,
    }


@app.get("/api/cities")
def cities(province: str = ""):
    if not province:
        # 地图页无省参数时，返回江浙沪皖主要城市
        return dc.query(
            """
            SELECT city AS k, COUNT(*) AS n FROM companies
            WHERE province IN ('江苏省','浙江省','上海市','安徽省')
              AND city IS NOT NULL AND city != ''
            GROUP BY city ORDER BY n DESC LIMIT 80
            """
        )
    return dc.query(
        f"SELECT city AS k, COUNT(*) AS n FROM companies "
        f"WHERE province = '{_esc(province)}' AND city IS NOT NULL AND city != '' "
        f"GROUP BY city ORDER BY n DESC LIMIT 100"
    )


@app.get("/api/districts")
def districts(province: str = "", city: str = ""):
    if not city:
        return []
    return dc.query(
        f"SELECT district AS k, COUNT(*) AS n FROM companies "
        f"WHERE province = '{_esc(province)}' AND city = '{_esc(city)}' "
        f"AND district IS NOT NULL AND district != '' "
        f"GROUP BY district ORDER BY n DESC LIMIT 100"
    )


@app.get("/api/streets")
def streets(province: str = "", city: str = "", district: str = ""):
    if not district:
        return []
    # 优先标准库；附带已落库企业数，保证下拉干净可点
    admin = dc.query(
        f"SELECT street AS k, COUNT(*) AS n FROM admin_divisions "
        f"WHERE level = 'street' "
        f"AND {sql_city_in('city', city, district)} AND district = '{_esc(district)}' "
        f"AND street IS NOT NULL AND street != '' "
        f"GROUP BY street ORDER BY street LIMIT 300"
    )
    if admin:
        counts = {
            r["k"]: int(r["n"])
            for r in dc.query(
                f"SELECT street AS k, COUNT(*) AS n FROM companies "
                f"WHERE province = '{_esc(province)}' AND city = '{_esc(city)}' "
                f"AND district = '{_esc(district)}' "
                f"AND street IS NOT NULL AND street != '' "
                f"GROUP BY street"
            )
        }
        return [{"k": r["k"], "n": counts.get(r["k"], 0)} for r in admin]
    return dc.query(
        f"SELECT street AS k, COUNT(*) AS n FROM companies "
        f"WHERE province = '{_esc(province)}' AND city = '{_esc(city)}' "
        f"AND district = '{_esc(district)}' "
        f"AND street IS NOT NULL AND street != '' "
        f"AND char_length(street) <= 12 "
        f"AND street NOT LIKE '%街道街道%' "
        f"AND street NOT LIKE '%街道%镇' "
        f"AND street NOT LIKE '%镇%街道' "
        f"AND street NOT LIKE '%区%街道' "
        f"AND street NOT LIKE '%区%镇' "
        f"GROUP BY street ORDER BY n DESC LIMIT 200"
    )


@app.get("/api/admin_center")
def admin_center(
    province: str = "",
    city: str = "",
    district: str = "",
    street: str = "",
):
    """返回区划中心点，供地图选中省市区街后飞过去。"""
    zoom = 10
    row = None
    city_sql = sql_city_in("city", city, district) if city else "1=0"
    if street and district and city:
        rows = dc.query(
            f"SELECT province, city, district, street, center_lng, center_lat "
            f"FROM admin_divisions WHERE level = 'street' "
            f"AND {city_sql} AND district = '{_esc(district)}' "
            f"AND street = '{_esc(street)}' "
            f"AND center_lng IS NOT NULL LIMIT 1"
        )
        if not rows:
            rows = dc.query(
                f"SELECT province, city, district, street, center_lng, center_lat "
                f"FROM admin_divisions WHERE level = 'street' "
                f"AND {city_sql} AND street = '{_esc(street)}' "
                f"AND center_lng IS NOT NULL LIMIT 1"
            )
        row = rows[0] if rows else None
        zoom = 15
    if row is None and district and city:
        rows = dc.query(
            f"SELECT province, city, district, CAST(NULL AS VARCHAR(64)) AS street, "
            f"center_lng, center_lat FROM admin_divisions WHERE level = 'district' "
            f"AND {city_sql} AND district = '{_esc(district)}' "
            f"AND center_lng IS NOT NULL LIMIT 1"
        )
        if not rows and province:
            # 直辖市兜底：只按省+区匹配（忽略工商/高德城市名差异）
            rows = dc.query(
                f"SELECT province, city, district, CAST(NULL AS VARCHAR(64)) AS street, "
                f"center_lng, center_lat FROM admin_divisions WHERE level = 'district' "
                f"AND province = '{_esc(province)}' AND district = '{_esc(district)}' "
                f"AND center_lng IS NOT NULL LIMIT 1"
            )
        row = rows[0] if rows else None
        zoom = 13
    if row is None and city:
        rows = dc.query(
            f"SELECT province, city, CAST(NULL AS VARCHAR(64)) AS district, "
            f"CAST(NULL AS VARCHAR(64)) AS street, center_lng, center_lat "
            f"FROM admin_divisions WHERE level = 'city' "
            f"AND {city_sql} AND center_lng IS NOT NULL LIMIT 1"
        )
        if not rows and province:
            rows = dc.query(
                f"SELECT province, city, CAST(NULL AS VARCHAR(64)) AS district, "
                f"CAST(NULL AS VARCHAR(64)) AS street, center_lng, center_lat "
                f"FROM admin_divisions WHERE level = 'city' "
                f"AND province = '{_esc(province)}' AND {city_sql} "
                f"AND center_lng IS NOT NULL LIMIT 1"
            )
        if not rows and province:
            rows = dc.query(
                f"SELECT province, city, CAST(NULL AS VARCHAR(64)) AS district, "
                f"CAST(NULL AS VARCHAR(64)) AS street, center_lng, center_lat "
                f"FROM admin_divisions WHERE level = 'city' "
                f"AND province = '{_esc(province)}' AND center_lng IS NOT NULL "
                f"ORDER BY CASE WHEN city LIKE '%城区%' THEN 0 ELSE 1 END LIMIT 1"
            )
        row = rows[0] if rows else None
        zoom = 11
    if row is None and province:
        rows = dc.query(
            f"SELECT province, CAST(NULL AS VARCHAR(64)) AS city, "
            f"CAST(NULL AS VARCHAR(64)) AS district, "
            f"CAST(NULL AS VARCHAR(64)) AS street, center_lng, center_lat "
            f"FROM admin_divisions WHERE level = 'province' "
            f"AND province = '{_esc(province)}' AND center_lng IS NOT NULL LIMIT 1"
        )
        row = rows[0] if rows else None
        zoom = 8
    if not row or row.get("center_lng") is None or row.get("center_lat") is None:
        return {"ok": False, "lng": None, "lat": None, "zoom": zoom}
    return {
        "ok": True,
        "lng": float(row["center_lng"]),
        "lat": float(row["center_lat"]),
        "zoom": zoom,
        "province": row.get("province") or province,
        "city": row.get("city") or city,
        "district": row.get("district") or district,
        "street": row.get("street") or street,
        "name": street or district or city or province,
    }


@app.get("/api/nearby")
def nearby(
    lng: float = Query(...),
    lat: float = Query(...),
    radius_m: int = Query(1000, ge=100, le=10000),
    has_mobile: Optional[int] = 1,
    status: str = "存续",
    industry_l1: str = "",
    limit: int = Query(100, ge=1, le=500),
    user_id: str = "default",
):
    """矩形粗筛 + 应用层距离精排。"""
    # 约 111km/度；经度随纬度缩放
    dlat = radius_m / 111_000.0
    dlng = radius_m / (111_000.0 * max(math.cos(math.radians(lat)), 0.2))
    parts = [
        f"c.lng IS NOT NULL AND c.lat IS NOT NULL",
        f"c.lng BETWEEN {lng - dlng} AND {lng + dlng}",
        f"c.lat BETWEEN {lat - dlat} AND {lat + dlat}",
    ]
    if status:
        parts.append(f"c.status = '{_esc(status)}'")
    if has_mobile is not None:
        parts.append(f"c.has_mobile = {1 if has_mobile else 0}")
    if industry_l1:
        parts.append(f"c.industry_l1 = '{_esc(industry_l1)}'")
    where = " AND ".join(parts)

    candidates = dc.query(f"""
        SELECT c.credit_code, c.company_name, c.status, c.legal_person, c.scale,
               c.capital_wan, c.province, c.city, c.district, c.street,
               c.industry_l1, c.address, c.has_mobile, c.has_contact,
               c.lng, c.lat, c.insured_count
        FROM companies c
        WHERE {where}
        LIMIT {min(limit * 8, 2000)}
    """)

    blocked = {
        r["credit_code"]
        for r in dc.query(
            f"SELECT credit_code FROM contact_status "
            f"WHERE user_id = '{_esc(user_id)}' AND status IN ('blocked','contacted')"
        )
    }

    scored = []
    for r in candidates:
        try:
            dist = _haversine_m(lng, lat, float(r["lng"]), float(r["lat"]))
        except (TypeError, ValueError):
            continue
        if dist > radius_m:
            continue
        r = dict(r)
        r["distance_m"] = round(dist)
        r["contact_flag"] = "blocked" if r["credit_code"] in blocked else ""
        scored.append(r)

    scored.sort(key=lambda x: (x["contact_flag"] != "", x["distance_m"], -(x.get("capital_wan") or 0)))
    return {
        "center": {"lng": lng, "lat": lat},
        "radius_m": radius_m,
        "total": len(scored),
        "geocoded_available": True,
        "items": scored[:limit],
    }


@app.get("/api/map/clusters")
def map_clusters(
    min_lng: float = Query(...),
    min_lat: float = Query(...),
    max_lng: float = Query(...),
    max_lat: float = Query(...),
    zoom: float = Query(11),
    q: str = "",
    province: str = "",
    city: str = "",
    district: str = "",
    street: str = "",
    industry_l1: str = "",
    industry_l2: str = "",
    industry_l3: str = "",
    scale: str = "",
    status: str = "存续",
    capital_min: Optional[float] = None,
    capital_max: Optional[float] = None,
    age_min: Optional[float] = None,
    age_max: Optional[float] = None,
    insured_min: Optional[int] = None,
    insured_max: Optional[int] = None,
    has_mobile: Optional[int] = 1,
    has_landline: Optional[int] = None,
    has_email: Optional[int] = None,
    has_contact: Optional[int] = None,
    scope_kw: str = "",
    addr_kw: str = "",
):
    """链家式地图气泡：与列表共用 build_where。
    市/区/街道层：按完整行政区聚合（与点开侧栏一致），仅当气泡中心落在视野内才返回；
    街道为最细层：不再下钻网格/单企点（坐标多为街道中心，网格会严重重叠）。
    """
    # 视野过大时不抛 400（全国/省级浏览需要），改为 overview 软提示
    span_lng = float(max_lng) - float(min_lng)
    span_lat = float(max_lat) - float(min_lat)
    if float(min_lng) >= float(max_lng) or float(min_lat) >= float(max_lat):
        raise HTTPException(400, "无效视野范围")
    if span_lng > 12 or span_lat > 9:
        return {
            "level": "overview",
            "zoom": zoom,
            "total": 0,
            "geocoded_hint": 0,
            "clusters": [],
            "count_mode": "admin",
            "hint": "请放大到城市级查看区域气泡，或在顶栏选择省市区",
        }
    base_where, need_tags, need_contacts, _q_kind = build_where(
        q=q, province=province, city=city, district=district, street=street,
        industry_l1=industry_l1, industry_l2=industry_l2, industry_l3=industry_l3,
        scale=scale, status=status, capital_min=capital_min, capital_max=capital_max,
        age_min=age_min, age_max=age_max, insured_min=insured_min, insured_max=insured_max,
        has_mobile=has_mobile, has_landline=has_landline, has_email=has_email,
        has_contact=has_contact, scope_kw=scope_kw, addr_kw=addr_kw,
    )
    geo_only, _ = build_geo_parts(require_geocoded=True)
    bbox_parts, _ = build_geo_parts(
        min_lng, min_lat, max_lng, max_lat, require_geocoded=True, limit_span=False
    )
    frm = from_clause(need_tags, need_contacts)
    cnt_expr = "COUNT(DISTINCT c.credit_code)" if need_contacts else "COUNT(*)"
    mobile_expr = (
        "COUNT(DISTINCT CASE WHEN c.has_mobile = 1 THEN c.credit_code END)"
        if need_contacts else "SUM(c.has_mobile)"
    )
    # 顶栏「可视区域」仍按视野框内已定位企业计
    where_view = merge_where(base_where, bbox_parts)
    total_row = dc.query(f"SELECT {cnt_expr} AS n {frm} WHERE {where_view}")
    total = int(total_row[0]["n"]) if total_row else 0

    # 行政区聚合：不按视野裁剪企业，避免气泡 2 家、侧栏 17 家
    where_admin = merge_where(base_where, geo_only)
    in_view = (
        f"AVG(c.lng) BETWEEN {float(min_lng)} AND {float(max_lng)} "
        f"AND AVG(c.lat) BETWEEN {float(min_lat)} AND {float(max_lat)}"
    )

    # 层级互斥：只聚合当前层，禁止回退成上级行政区名称
    if zoom < 10:
        level = "city"
        sql = f"""
            SELECT
              c.city AS name,
              c.city AS city,
              CAST(NULL AS VARCHAR(64)) AS district,
              {cnt_expr} AS cnt,
              ROUND(AVG(c.capital_wan), 1) AS metric,
              AVG(c.lng) AS lng,
              AVG(c.lat) AS lat,
              {mobile_expr} AS with_mobile
            {frm}
            WHERE {where_admin}
              AND c.city IS NOT NULL AND c.city != ''
            GROUP BY c.city
            HAVING {cnt_expr} > 0 AND {in_view}
            ORDER BY cnt DESC
            LIMIT 40
        """
    elif zoom < 12.5:
        level = "district"
        sql = f"""
            SELECT
              c.district AS name,
              c.city AS city,
              c.district AS district,
              {cnt_expr} AS cnt,
              ROUND(AVG(c.capital_wan), 1) AS metric,
              AVG(c.lng) AS lng,
              AVG(c.lat) AS lat,
              {mobile_expr} AS with_mobile
            {frm}
            WHERE {where_admin}
              AND c.district IS NOT NULL AND c.district != ''
            GROUP BY c.city, c.district
            HAVING {cnt_expr} > 0 AND {in_view}
            ORDER BY cnt DESC
            LIMIT 80
        """
    else:
        # 街道及更细缩放：仍只聚合到街道，不再下钻网格/单企点
        # （坐标多为街道中心+抖动，网格层会在同一中心叠出大量胶囊）
        level = "street"
        sql = f"""
            SELECT
              c.street AS name,
              c.city AS city,
              c.district AS district,
              c.street AS street,
              {cnt_expr} AS cnt,
              ROUND(AVG(c.capital_wan), 1) AS metric,
              AVG(c.lng) AS lng,
              AVG(c.lat) AS lat,
              {mobile_expr} AS with_mobile
            {frm}
            WHERE {where_admin}
              AND c.street IS NOT NULL AND c.street != ''
            GROUP BY c.city, c.district, c.street
            HAVING {cnt_expr} > 0 AND {in_view}
            ORDER BY cnt DESC
            LIMIT 120
        """

    clusters = dc.query(sql)
    return {
        "level": level,
        "zoom": zoom,
        "total": total,
        "geocoded_hint": total,
        "clusters": clusters,
        "count_mode": "admin",
    }


def has_bbox_params(
    min_lng: Optional[float],
    min_lat: Optional[float],
    max_lng: Optional[float],
    max_lat: Optional[float],
) -> bool:
    return None not in (min_lng, min_lat, max_lng, max_lat)


@app.get("/api/map/companies")
def map_companies(
    min_lng: Optional[float] = None,
    min_lat: Optional[float] = None,
    max_lng: Optional[float] = None,
    max_lat: Optional[float] = None,
    q: str = "",
    province: str = "",
    city: str = "",
    district: str = "",
    street: str = "",
    street_empty: int = 0,
    industry_l1: str = "",
    industry_l2: str = "",
    industry_l3: str = "",
    scale: str = "",
    status: str = "存续",
    capital_min: Optional[float] = None,
    capital_max: Optional[float] = None,
    age_min: Optional[float] = None,
    age_max: Optional[float] = None,
    insured_min: Optional[int] = None,
    insured_max: Optional[int] = None,
    has_mobile: Optional[int] = 1,
    has_landline: Optional[int] = None,
    has_email: Optional[int] = None,
    has_contact: Optional[int] = None,
    scope_kw: str = "",
    addr_kw: str = "",
    require_geo: int = Query(
        1, description="1=仅已定位；0=行政区全量（含无坐标，侧栏列表用）"
    ),
    limit: int = Query(200, ge=1, le=500),
):
    """点击气泡后拉线索。行政区点选可 require_geo=0 展示未定位企业。"""
    has_bbox = has_bbox_params(min_lng, min_lat, max_lng, max_lat)
    if not (city or district or street or street_empty) and not has_bbox:
        raise HTTPException(400, "需要视野范围或城市/区县条件")

    # 网格 bbox 必须有坐标；行政区 + require_geo=0 可含未定位
    if has_bbox:
        geo_parts, _ = build_geo_parts(
            min_lng, min_lat, max_lng, max_lat, require_geocoded=True, limit_span=False
        )
        want_geo = True
    elif int(require_geo) != 0:
        geo_parts, _ = build_geo_parts(require_geocoded=True)
        want_geo = True
    else:
        geo_parts, want_geo = [], False

    base_where, need_tags, need_contacts, _q_kind = build_where(
        q=q, province=province, city=city, district=district, street=street,
        industry_l1=industry_l1, industry_l2=industry_l2, industry_l3=industry_l3,
        scale=scale, status=status, capital_min=capital_min, capital_max=capital_max,
        age_min=age_min, age_max=age_max, insured_min=insured_min, insured_max=insured_max,
        has_mobile=has_mobile, has_landline=has_landline, has_email=has_email,
        has_contact=has_contact, scope_kw=scope_kw, addr_kw=addr_kw,
        street_empty=bool(street_empty),
    )
    where = merge_where(base_where, geo_parts)
    frm = from_clause(need_tags, need_contacts)
    distinct = "DISTINCT" if need_contacts else ""
    cnt_expr = "COUNT(DISTINCT c.credit_code)" if need_contacts else "COUNT(*)"

    rows = dc.query(f"""
        SELECT {distinct} c.credit_code, c.company_name, c.legal_person, c.status, c.scale,
               c.capital_wan, c.province, c.city, c.district, c.street,
               c.industry_l1, c.address, c.has_mobile, c.has_contact,
               c.lng, c.lat, c.insured_count
        {frm}
        WHERE {where}
        ORDER BY c.has_mobile DESC, c.capital_wan DESC
        LIMIT {limit}
    """)
    cnt = dc.query(f"SELECT {cnt_expr} AS n {frm} WHERE {where}")
    return {
        "total": int(cnt[0]["n"]) if cnt else 0,
        "items": attach_contact_preview(rows),
        "require_geo": want_geo,
    }


EXPORT_HEADERS = [
    ("credit_code", "统一社会信用代码"),
    ("company_name", "企业名称"),
    ("status", "登记状态"),
    ("legal_person", "法定代表人"),
    ("scale", "企业规模"),
    ("capital_wan", "注册资本(万元)"),
    ("capital_raw", "注册资本原文"),
    ("capital_paid_wan", "实缴资本(万元)"),
    ("established", "成立日期"),
    ("approved", "核准日期"),
    ("company_age", "成立年限"),
    ("province", "省份"),
    ("city", "城市"),
    ("district", "区县"),
    ("street", "街道"),
    ("company_type", "企业类型"),
    ("industry_l1", "行业门类"),
    ("industry_l2", "行业大类"),
    ("industry_l3", "行业中类"),
    ("insured_count", "参保人数"),
    ("former_name", "曾用名"),
    ("tax_id", "纳税人识别号"),
    ("reg_no", "工商注册号"),
    ("org_code", "组织机构代码"),
    ("address", "注册地址"),
    ("address_report", "年报地址"),
    ("website", "官网"),
    ("scope", "经营范围"),
    ("mobiles", "手机"),
    ("landlines", "座机"),
    ("emails", "邮箱"),
    ("has_mobile", "有手机"),
    ("has_landline", "有座机"),
    ("has_email", "有邮箱"),
    ("mobile_count", "手机数"),
    ("email_count", "邮箱数"),
    ("lng", "经度"),
    ("lat", "纬度"),
]


def attach_contacts_for_export(rows: list[dict], chunk: int = 800) -> list[dict]:
    """把 contacts 表中的手机/座机/邮箱聚合成导出列（分号分隔）。"""
    for r in rows:
        r["mobiles"] = ""
        r["landlines"] = ""
        r["emails"] = ""
    codes = [r.get("credit_code") for r in rows if r.get("credit_code")]
    if not codes:
        return rows

    bucket: dict[str, dict[str, list[str]]] = {}
    for i in range(0, len(codes), chunk):
        part = codes[i : i + chunk]
        inlist = ",".join(f"'{_esc(c)}'" for c in part)
        cts = dc.query(
            f"SELECT credit_code, contact_type, contact_value FROM contacts "
            f"WHERE credit_code IN ({inlist}) "
            f"ORDER BY is_primary DESC, contact_type, contact_value"
        )
        for x in cts:
            code = x.get("credit_code") or ""
            t = (x.get("contact_type") or "").lower()
            v = (x.get("contact_value") or "").strip()
            if not code or not v:
                continue
            slot = bucket.setdefault(code, {"mobile": [], "landline": [], "email": []})
            if t in slot and v not in slot[t]:
                slot[t].append(v)

    for r in rows:
        b = bucket.get(r.get("credit_code") or "", {})
        r["mobiles"] = ";".join(b.get("mobile") or [])
        r["landlines"] = ";".join(b.get("landline") or [])
        r["emails"] = ";".join(b.get("email") or [])
    return rows


@app.get("/api/export")
def export_csv(
    q: str = "",
    province: str = "",
    city: str = "",
    district: str = "",
    street: str = "",
    industry_l1: str = "",
    industry_l2: str = "",
    industry_l3: str = "",
    scale: str = "",
    status: str = "存续",
    capital_min: Optional[float] = None,
    capital_max: Optional[float] = None,
    age_min: Optional[float] = None,
    age_max: Optional[float] = None,
    insured_min: Optional[int] = None,
    insured_max: Optional[int] = None,
    has_mobile: Optional[int] = None,
    has_landline: Optional[int] = None,
    has_email: Optional[int] = None,
    has_contact: Optional[int] = None,
    scope_kw: str = "",
    addr_kw: str = "",
    q_fields: str = "",
    limit: int = Query(5000, ge=1, le=50000),
):
    """导出完整企业信息 + 手机/座机/邮箱（从 contacts 聚合）。"""
    where, need_tags, need_contacts, _q_kind = build_where(
        q=q, province=province, city=city, district=district, street=street,
        industry_l1=industry_l1, industry_l2=industry_l2, industry_l3=industry_l3,
        scale=scale, status=status,
        capital_min=capital_min, capital_max=capital_max,
        age_min=age_min, age_max=age_max, insured_min=insured_min, insured_max=insured_max,
        has_mobile=has_mobile, has_landline=has_landline, has_email=has_email,
        has_contact=has_contact, scope_kw=scope_kw, addr_kw=addr_kw,
        q_fields=q_fields,
    )
    frm = from_clause(need_tags, need_contacts)
    distinct = "DISTINCT" if need_contacts else ""
    rows = dc.query(f"""
        SELECT {distinct}
               c.credit_code, c.company_name, c.status, c.legal_person, c.scale,
               c.capital_wan, c.capital_raw, c.capital_paid_wan,
               c.established, c.approved, c.company_age,
               c.province, c.city, c.district, c.street, c.company_type,
               c.industry_l1, c.industry_l2, c.industry_l3,
               c.insured_count, c.former_name, c.tax_id, c.reg_no, c.org_code,
               c.address, c.address_report, c.website, c.scope,
               c.has_mobile, c.has_landline, c.has_email,
               c.mobile_count, c.email_count, c.lng, c.lat
        {frm} WHERE {where}
        ORDER BY c.has_mobile DESC, c.capital_wan DESC
        LIMIT {limit}
    """)
    rows = attach_contacts_for_export(rows)

    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow([h[1] for h in EXPORT_HEADERS])
    for r in rows:
        writer.writerow([
            "" if r.get(k) is None else r.get(k)
            for k, _ in EXPORT_HEADERS
        ])

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=leads_export.csv"},
    )


# ---------- Phase 4: 片区 / 状态 / 今日任务包 ----------

class TerritoryIn(BaseModel):
    user_id: str = "default"
    province: str = ""
    city: str = ""
    district: str = ""
    street: str = ""


class ContactStatusIn(BaseModel):
    credit_code: str
    user_id: str = "default"
    status: str  # contacted / blocked / interested / rejected
    note: str = ""


class CompanyUpdateIn(BaseModel):
    company_name: Optional[str] = None
    legal_person: Optional[str] = None
    status: Optional[str] = None
    scale: Optional[str] = None
    capital_wan: Optional[float] = None
    capital_raw: Optional[str] = None
    address: Optional[str] = None
    website: Optional[str] = None
    scope: Optional[str] = None
    province: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    street: Optional[str] = None
    industry_l1: Optional[str] = None
    industry_l2: Optional[str] = None
    industry_l3: Optional[str] = None


class ContactUpsertIn(BaseModel):
    contact_type: str  # mobile / landline / email
    contact_value: str
    is_primary: int = 0


def _refresh_contact_flags(credit_code: str) -> None:
    """根据 contacts 重算 has_* 标记。"""
    code = _esc(credit_code)
    agg = dc.query(
        f"SELECT "
        f"SUM(CASE WHEN contact_type='mobile' THEN 1 ELSE 0 END) AS m, "
        f"SUM(CASE WHEN contact_type='landline' THEN 1 ELSE 0 END) AS l, "
        f"SUM(CASE WHEN contact_type='email' THEN 1 ELSE 0 END) AS e, "
        f"COUNT(1) AS n "
        f"FROM contacts WHERE credit_code = '{code}'"
    )[0]
    m = 1 if int(agg.get("m") or 0) > 0 else 0
    l = 1 if int(agg.get("l") or 0) > 0 else 0
    e = 1 if int(agg.get("e") or 0) > 0 else 0
    n = int(agg.get("n") or 0)
    fs = dc.query(
        f"SELECT IFNULL(fill_score,0) AS fill_score FROM companies "
        f"WHERE credit_code = '{code}' LIMIT 1"
    )
    fill = int(fs[0]["fill_score"]) + 1 if fs else 1
    js = dc.stream_load(
        "companies",
        ["credit_code", "has_mobile", "has_landline", "has_email", "has_contact",
         "mobile_count", "email_count", "fill_score"],
        dc.make_csv([[
            credit_code, m, l, e, 1 if n > 0 else 0,
            int(agg.get("m") or 0), int(agg.get("e") or 0), fill,
        ]]),
        label=f"flags_{credit_code[-8:]}_{int(datetime.now().timestamp())}",
        partial_columns=True,
        max_filter_ratio=0.1,
    )
    if str(js.get("Status", "")).lower() not in {"success", "publish timeout"}:
        raise HTTPException(500, f"更新触达标记失败：{js}")


@app.put("/api/company/{credit_code}")
def update_company(credit_code: str, body: CompanyUpdateIn):
    code = _esc(credit_code)
    rows = dc.query(f"SELECT fill_score FROM companies WHERE credit_code = '{code}' LIMIT 1")
    if not rows:
        raise HTTPException(404, "未找到企业")
    data = body.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(400, "没有可更新字段")
    allowed = {
        "company_name", "legal_person", "status", "scale", "capital_wan", "capital_raw",
        "address", "website", "scope", "province", "city", "district", "street",
        "industry_l1", "industry_l2", "industry_l3",
    }
    cols = ["credit_code"]
    vals: list = [credit_code]
    for k, v in data.items():
        if k not in allowed:
            continue
        cols.append(k)
        if isinstance(v, str):
            vals.append(v.strip()[:2000] if k == "scope" else v.strip()[:500])
        else:
            vals.append(v)
    if len(cols) == 1:
        raise HTTPException(400, "没有可更新字段")
    fill = int(rows[0].get("fill_score") or 0) + 1
    cols.append("fill_score")
    vals.append(fill)
    js = dc.stream_load(
        "companies", cols, dc.make_csv([vals]),
        label=f"co_upd_{credit_code[-10:]}_{int(datetime.now().timestamp())}",
        partial_columns=True,
        max_filter_ratio=0.1,
    )
    if str(js.get("Status", "")).lower() not in {"success", "publish timeout"}:
        raise HTTPException(500, f"保存失败：{js}")
    return {"ok": True, "updated": [c for c in cols if c not in {"credit_code", "fill_score"}]}


@app.post("/api/company/{credit_code}/contacts")
def add_company_contact(credit_code: str, body: ContactUpsertIn):
    code = _esc(credit_code)
    exists = dc.query(f"SELECT 1 AS x FROM companies WHERE credit_code = '{code}' LIMIT 1")
    if not exists:
        raise HTTPException(404, "未找到企业")
    ctype = (body.contact_type or "").strip().lower()
    if ctype not in {"mobile", "landline", "email"}:
        raise HTTPException(400, "contact_type 须为 mobile/landline/email")
    raw = (body.contact_value or "").strip()
    if not raw:
        raise HTTPException(400, "联系方式不能为空")
    if ctype == "mobile":
        value = normalize_phone(raw)
        if not MOBILE_RE.match(value) and not PHONE_DIGITS_RE.match(value):
            raise HTTPException(400, "手机号格式不正确")
    elif ctype == "email":
        value = raw.lower()
        if "@" not in value:
            raise HTTPException(400, "邮箱格式不正确")
    else:
        value = re.sub(r"[\s]", "", raw)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%d")
    name_row = dc.query(
        f"SELECT company_name FROM companies WHERE credit_code = '{code}' LIMIT 1"
    )
    cname = (name_row[0].get("company_name") if name_row else "") or ""
    js = dc.stream_load(
        "contacts",
        ["credit_code", "contact_type", "contact_value", "source", "company_name",
         "is_primary", "collected_at", "loaded_at"],
        dc.make_csv([[
            credit_code, ctype, value, "manual", cname[:300],
            1 if body.is_primary else 0, today, now,
        ]]),
        label=f"ct_add_{credit_code[-8:]}_{int(datetime.now().timestamp())}",
        max_filter_ratio=0.1,
    )
    if str(js.get("Status", "")).lower() not in {"success", "publish timeout"}:
        raise HTTPException(500, f"添加联系方式失败：{js}")
    _refresh_contact_flags(credit_code)
    return {"ok": True, "contact_type": ctype, "contact_value": value}


@app.delete("/api/company/{credit_code}/contacts")
def delete_company_contact(
    credit_code: str,
    contact_type: str = Query(...),
    contact_value: str = Query(...),
):
    ctype = (contact_type or "").strip().lower()
    value = (contact_value or "").strip()
    if ctype not in {"mobile", "landline", "email"} or not value:
        raise HTTPException(400, "参数不完整")
    dc.execute(
        f"DELETE FROM contacts WHERE credit_code = '{_esc(credit_code)}' "
        f"AND contact_type = '{_esc(ctype)}' AND contact_value = '{_esc(value)}'"
    )
    _refresh_contact_flags(credit_code)
    return {"ok": True}


@app.get("/api/territory")
def get_territory(user_id: str = "default"):
    return dc.query(
        f"SELECT * FROM sales_territory WHERE user_id = '{_esc(user_id)}' "
        f"ORDER BY province, city, district, street"
    )


@app.post("/api/territory")
def set_territory(body: TerritoryIn):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dc.execute(
        "INSERT INTO sales_territory (user_id, province, city, district, street, loaded_at) VALUES "
        f"('{_esc(body.user_id)}', '{_esc(body.province)}', '{_esc(body.city)}', "
        f"'{_esc(body.district)}', '{_esc(body.street)}', '{now}')"
    )
    return {"ok": True}


@app.post("/api/contact_status")
def set_contact_status(body: ContactStatusIn):
    if body.status not in {"contacted", "blocked", "interested", "rejected"}:
        raise HTTPException(400, "非法 status")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    note = _esc((body.note or "")[:500])
    dc.execute(
        "INSERT INTO contact_status (credit_code, user_id, status, note, updated_at) VALUES "
        f"('{_esc(body.credit_code)}', '{_esc(body.user_id)}', '{_esc(body.status)}', "
        f"'{note}', '{now}')"
    )
    return {"ok": True}



@app.get("/api/tasks/today")
def tasks_today(
    user_id: str = "default",
    limit: int = Query(50, ge=1, le=200),
    has_mobile: int = 1,
    age_min: float = 1,
    age_max: float = 8,
    status: str = "存续",
):
    """今日任务包：片区内 + 有手机 + 未联系/未禁打 + 成立年限。"""
    terr = dc.query(
        f"SELECT province, city, district, street FROM sales_territory "
        f"WHERE user_id = '{_esc(user_id)}' LIMIT 20"
    )
    geo_parts = []
    for t in terr:
        bits = []
        if t.get("province"):
            bits.append(f"c.province = '{_esc(t['province'])}'")
        if t.get("city"):
            bits.append(f"c.city = '{_esc(t['city'])}'")
        if t.get("district"):
            bits.append(f"c.district = '{_esc(t['district'])}'")
        if t.get("street"):
            bits.append(f"c.street = '{_esc(t['street'])}'")
        if bits:
            geo_parts.append("(" + " AND ".join(bits) + ")")
    geo_sql = ("(" + " OR ".join(geo_parts) + ")") if geo_parts else "1=1"

    where = (
        f"{geo_sql} AND c.status = '{_esc(status)}' "
        f"AND c.has_mobile = {1 if has_mobile else 0} "
        f"AND c.company_age >= {float(age_min)} AND c.company_age <= {float(age_max)} "
        f"AND c.credit_code NOT IN ("
        f"  SELECT credit_code FROM contact_status "
        f"  WHERE user_id = '{_esc(user_id)}' AND status IN ('blocked','contacted')"
        f")"
    )
    rows = dc.query(f"""
        SELECT c.credit_code, c.company_name, c.legal_person, c.province, c.city,
               c.district, c.street, c.capital_wan, c.company_age, c.industry_l1,
               c.address, c.has_mobile, c.insured_count
        FROM companies c
        WHERE {where}
        ORDER BY c.insured_count DESC, c.capital_wan DESC
        LIMIT {limit}
    """)
    return {
        "user_id": user_id,
        "territory_rules": len(terr),
        "total": len(rows),
        "items": rows,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8765, reload=False)
