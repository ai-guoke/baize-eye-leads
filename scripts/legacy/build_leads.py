# -*- coding: utf-8 -*-
"""
合并 94 个产业链 xlsx → 清洗 / 去重 / 打标签 / LeadScore → SQLite + CSV。

用法：
  python build_leads.py              # 全量
  python build_leads.py --max-files 3
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _SCRIPTS, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


from config import (
    BIZ_LINES,
    CITY_CENTROID,
    DB_PATH,
    EMPTY_TOKENS,
    FX_TO_CNY,
    HONOR_SHEET_HINTS,
    LOG_PATH,
    OUTPUT_DIR,
    PROVINCE_CENTROID,
    SRC_INDUSTRY,
    CSV_PATH,
    TOP_CSV_PATH,
    STATS_PATH,
)
from scripts.xlsx_stream import iter_workbook_sheets
from add_street import extract_street

TODAY = date(2026, 9, 1)
CREDIT_RE = re.compile(r"^[0-9A-Z]{18}$")
CAPITAL_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(万|亿)?\s*(人民币|美元|美金|港元|港币|欧元|日元|英镑|新加坡元|澳元|加元|韩元|台币|新台币)?"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_SPLIT_RE = re.compile(r"[;；,，、|/\s]+")
STATUS_SET = {"存续", "在业", "注销", "吊销", "迁出", "撤销", "停业", "清算", "歇业", "吊销，未注销", "注销企业"}

CANON_BASE_37 = [
    "原文件导入名称", "登记状态", "法定代表人", "企业规模", "注册资本", "实缴资本",
    "成立日期", "核准日期", "营业期限", "所属省份", "所属城市", "所属区县",
    "企业(机构)类型", "国标行业门类", "国标行业大类", "国标行业中类", "国标行业小类",
    "曾用名", "英文名", "统一社会信用代码", "纳税人识别号", "注册号", "组织机构代码",
    "参保人数", "参保人数所属年报", "电话", "更多电话", "企业地址", "通信地址",
    "官网", "邮箱", "更多邮箱", "经营范围", "登记机关", "纳税人资质", "企业简介", "最新年报年份",
]

NAME_KEYS = ["系统匹配企业名称", "公司名称", "原文件导入名称", "公司"]
PHONE_KEYS = ["电话", "更多电话", "可用电话", "其他电话"]
EMAIL_KEYS = ["邮箱", "更多邮箱", "其他邮箱"]
WEB_KEYS = ["官网", "网址"]
ADDR_KEYS = ["企业地址", "注册地址", "通信地址", "最新年报地址"]


def log(msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S}  {msg}"
    print(line, flush=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def clean(val) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in EMPTY_TOKENS or s in EMPTY_TOKENS:
        return ""
    return s


def folder_industry(folder: str) -> str:
    s = folder
    for tok in ("产业链企查查", "企查查", "产业链汇总", "汇总匹配版", "企业荣誉资质汇总",
                "企业荣誉资质", "资质荣誉汇总", "荣誉汇总匹配"):
        s = s.replace(tok, "")
    return s.strip() or folder


def valid_credit(code: str) -> bool:
    c = code.strip().upper().replace(" ", "")
    return bool(CREDIT_RE.match(c)) and c not in {"000000000000000000"}


def credit_key(code: str, name: str) -> str:
    c = clean(code).upper().replace(" ", "")
    if valid_credit(c):
        return c
    n = clean(name)
    return f"N:{n}" if n else ""


def split_list(raw: str) -> list[str]:
    s = clean(raw)
    if not s:
        return []
    return [p for p in PHONE_SPLIT_RE.split(s) if clean(p)]


def normalize_phones(*raws: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in raws:
        for p in split_list(raw):
            digits = re.sub(r"\D", "", p.replace("+86", "").replace("0086", ""))
            if digits.startswith("86") and len(digits) >= 13:
                digits = digits[2:]
            if len(digits) == 11 and digits.startswith("1"):
                key = digits
            elif 10 <= len(digits) <= 12:
                key = digits
            else:
                continue
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def normalize_emails(*raws: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in raws:
        if not raw:
            continue
        for m in EMAIL_RE.findall(str(raw)):
            e = m.lower()
            if e not in seen and "example.com" not in e:
                seen.add(e)
                out.append(e)
    return out


def parse_capital(raw: str) -> tuple[float | None, str, str]:
    """返回 (人民币元, 原币种, 原单位)。"""
    s = clean(raw).replace(",", "")
    if not s:
        return None, "", ""
    m = CAPITAL_RE.search(s)
    if not m:
        return None, "", ""
    num = float(m.group(1))
    unit = m.group(2) or ""
    ccy = m.group(3) or "人民币"
    mult = {"万": 10_000, "亿": 100_000_000}.get(unit, 1)
    fx = FX_TO_CNY.get(ccy, 1.0)
    return num * mult * fx, ccy, unit or "元"


def parse_date(raw: str) -> date | None:
    s = clean(raw)
    if not s:
        return None
    if re.fullmatch(r"\d{4,6}(\.\d+)?", s):
        try:
            serial = int(float(s))
            if 20000 < serial < 60000:
                return (datetime(1899, 12, 30) + timedelta(days=serial)).date()
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s[:10].replace("年", "-").replace("月", "-").replace("日", ""), "%Y-%m-%d").date()
        except ValueError:
            try:
                return datetime.strptime(s[:10], fmt).date()
            except ValueError:
                continue
    m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_int(raw: str) -> int | None:
    s = clean(raw).replace(",", "")
    if not s:
        return None
    m = re.search(r"\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def row_to_dict(header: list[str], row: list[str]) -> dict[str, str]:
    d: dict[str, str] = {}
    for i, h in enumerate(header):
        key = (h or "").strip()
        if not key:
            continue
        val = row[i] if i < len(row) else ""
        if key in d and clean(d[key]) and not clean(val):
            continue
        if key not in d or not clean(d[key]):
            d[key] = val
    return d


def pick(d: dict[str, str], *keys: str) -> str:
    for k in keys:
        v = clean(d.get(k, ""))
        if v:
            return v
    return ""


def classify_sheet(name: str, header: list[str]) -> str:
    hs = {h.strip() for h in header if h and str(h).strip()}
    n = (name or "").strip()
    if n.lower() in {"sheet3"}:
        return "skip"
    if "登记状态" in hs and ("法定代表人" in hs or "注册资本" in hs):
        return "base"
    if header and clean(header[0]) and (len(header) > 1 and clean(header[1]) in STATUS_SET):
        return "base_no_header"
    if "一级分类" in hs and ({"公司名称", "公司", "公司 "} & hs or any("公司" in h for h in hs)):
        return "chain"
    if n in HONOR_SHEET_HINTS:
        return "honor"
    honor_cols = hs & {"荣誉", "企业荣誉", "荣誉资质", "资质荣誉", "资质"}
    if honor_cols and ({"公司名称", "公司", "ID"} & hs or any("公司" in h for h in hs)):
        return "honor"
    if n in {"荣誉匹配", "类似相关企业", "相关企业", "机器人全部企业"}:
        return "honor"
    return "skip"


def geo_of(province: str, city: str) -> tuple[float | None, float | None]:
    for key in (city, city.replace("市", "") + "市" if city else ""):
        if key and key in CITY_CENTROID:
            return CITY_CENTROID[key]
    if province in PROVINCE_CENTROID:
        return PROVINCE_CENTROID[province]
    return None, None


def connect() -> sqlite3.Connection:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-250000")
    conn.executescript(
        """
        CREATE TABLE staging_base (
            credit_key TEXT,
            company_name TEXT,
            status TEXT,
            legal_person TEXT,
            scale_raw TEXT,
            capital_raw TEXT,
            capital_paid_raw TEXT,
            established TEXT,
            approved TEXT,
            business_term TEXT,
            province TEXT,
            city TEXT,
            district TEXT,
            company_type TEXT,
            gb_cat TEXT,
            gb_major TEXT,
            gb_mid TEXT,
            gb_minor TEXT,
            former_name TEXT,
            en_name TEXT,
            credit_code TEXT,
            tax_id TEXT,
            reg_no TEXT,
            org_code TEXT,
            insured TEXT,
            insured_year TEXT,
            phones TEXT,
            emails TEXT,
            address TEXT,
            address_mail TEXT,
            website TEXT,
            scope TEXT,
            registrar TEXT,
            taxpayer_qual TEXT,
            intro TEXT,
            report_year TEXT,
            fill_score INTEGER,
            source_file TEXT,
            source_industry TEXT
        );
        CREATE TABLE staging_chain (
            credit_key TEXT,
            company_name TEXT,
            credit_code TEXT,
            l1 TEXT, l2 TEXT, l3 TEXT, l4 TEXT, l5 TEXT, l6 TEXT, l7 TEXT, l8 TEXT,
            node TEXT,
            position TEXT,
            relatedness TEXT,
            financing TEXT,
            source_file TEXT,
            source_industry TEXT
        );
        CREATE TABLE staging_honor (
            credit_key TEXT,
            company_name TEXT,
            credit_code TEXT,
            honor TEXT,
            source_file TEXT
        );
        """
    )
    return conn


def fill_score(d: dict) -> int:
    return sum(1 for v in d.values() if clean(str(v) if v is not None else ""))


def ingest_base(conn, header, rows, src_file, industry, no_header_first=None):
    cur = conn.cursor()
    n = 0

    def handle_dict(d: dict[str, str]):
        nonlocal n
        name = pick(d, *NAME_KEYS)
        code = pick(d, "统一社会信用代码", "ID")
        key = credit_key(code, name)
        if not key:
            return
        phones = normalize_phones(*(d.get(k, "") for k in PHONE_KEYS))
        emails = normalize_emails(*(d.get(k, "") for k in EMAIL_KEYS))
        rec = {
            "credit_key": key,
            "company_name": name,
            "status": pick(d, "登记状态"),
            "legal_person": pick(d, "法定代表人"),
            "scale_raw": pick(d, "企业规模"),
            "capital_raw": pick(d, "注册资本"),
            "capital_paid_raw": pick(d, "实缴资本"),
            "established": pick(d, "成立日期"),
            "approved": pick(d, "核准日期"),
            "business_term": pick(d, "营业期限"),
            "province": pick(d, "所属省份"),
            "city": pick(d, "所属城市"),
            "district": pick(d, "所属区县"),
            "company_type": pick(d, "企业(机构)类型", "公司类型"),
            "gb_cat": pick(d, "国标行业门类"),
            "gb_major": pick(d, "国标行业大类"),
            "gb_mid": pick(d, "国标行业中类"),
            "gb_minor": pick(d, "国标行业小类"),
            "former_name": pick(d, "曾用名"),
            "en_name": pick(d, "英文名"),
            "credit_code": code if valid_credit(code.upper().replace(" ", "")) else "",
            "tax_id": pick(d, "纳税人识别号"),
            "reg_no": pick(d, "注册号"),
            "org_code": pick(d, "组织机构代码"),
            "insured": pick(d, "参保人数"),
            "insured_year": pick(d, "参保人数所属年报"),
            "phones": json.dumps(phones, ensure_ascii=False),
            "emails": json.dumps(emails, ensure_ascii=False),
            "address": pick(d, *ADDR_KEYS),
            "address_mail": pick(d, "通信地址"),
            "website": pick(d, *WEB_KEYS),
            "scope": pick(d, "经营范围"),
            "registrar": pick(d, "登记机关"),
            "taxpayer_qual": pick(d, "纳税人资质"),
            "intro": pick(d, "企业简介"),
            "report_year": pick(d, "最新年报年份"),
            "fill_score": 0,
            "source_file": src_file,
            "source_industry": industry,
        }
        rec["fill_score"] = fill_score(rec)
        cur.execute(
            f"INSERT INTO staging_base ({','.join(rec.keys())}) VALUES ({','.join('?' for _ in rec)})",
            list(rec.values()),
        )
        n += 1

    if no_header_first is not None:
        handle_dict(row_to_dict(CANON_BASE_37, no_header_first))
        for row in rows:
            handle_dict(row_to_dict(CANON_BASE_37, row))
    else:
        for row in rows:
            handle_dict(row_to_dict(header, row))
    return n


def ingest_chain(conn, header, rows, src_file, industry):
    cur = conn.cursor()
    n = 0
    for row in rows:
        d = row_to_dict(header, row)
        name = pick(d, "公司名称", "公司", "公司 ")
        code = pick(d, "ID", "统一社会信用代码")
        key = credit_key(code, name)
        if not key:
            continue
        cur.execute(
            """INSERT INTO staging_chain
               (credit_key,company_name,credit_code,l1,l2,l3,l4,l5,l6,l7,l8,node,position,relatedness,financing,source_file,source_industry)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key, name, code if valid_credit(code.upper().replace(" ", "")) else "",
                pick(d, "一级分类"), pick(d, "二级分类"), pick(d, "三级分类"),
                pick(d, "四级分类"), pick(d, "五级分类"), pick(d, "六级分类", "六级分离"),
                pick(d, "七级分类"), pick(d, "八级分类"),
                pick(d, "产业链节点"), pick(d, "产业位置"), pick(d, "产业关联性"),
                pick(d, "融资上市", "资质", "荣誉"),
                src_file, industry,
            ),
        )
        n += 1
    return n


def ingest_honor(conn, header, rows, src_file, sheet_name):
    cur = conn.cursor()
    n = 0
    for row in rows:
        d = row_to_dict(header, row)
        name = pick(d, "公司名称", "公司")
        code = pick(d, "ID", "统一社会信用代码")
        honor = pick(d, "荣誉", "企业荣誉", "荣誉资质", "资质荣誉", "资质") or sheet_name
        if not honor and len(row) >= 2:
            # 两列：公司 | 逗号分隔荣誉
            name = name or clean(row[0])
            honor = clean(row[1] if len(row) > 1 else "")
        key = credit_key(code, name)
        if not key or not honor:
            continue
        for h in re.split(r"[,，;；、]", honor):
            h = clean(h)
            if not h:
                continue
            cur.execute(
                "INSERT INTO staging_honor (credit_key,company_name,credit_code,honor,source_file) VALUES (?,?,?,?,?)",
                (key, name, code if valid_credit((code or "").upper().replace(" ", "")) else "", h, src_file),
            )
            n += 1
    return n


def ingest_file(conn, path: Path) -> dict[str, int]:
    stats = {"base": 0, "chain": 0, "honor": 0, "skip": 0}
    industry = folder_industry(path.parent.name)
    src = f"{path.parent.name}/{path.name}"
    try:
        for sheet_name, header, rows in iter_workbook_sheets(path):
            kind = classify_sheet(sheet_name, header)
            if kind == "skip":
                for _ in rows:
                    pass
                stats["skip"] += 1
                continue
            if kind == "base":
                stats["base"] += ingest_base(conn, header, rows, src, industry)
            elif kind == "base_no_header":
                stats["base"] += ingest_base(
                    conn, CANON_BASE_37, rows, src, industry, no_header_first=header
                )
            elif kind == "chain":
                stats["chain"] += ingest_chain(conn, header, rows, src, industry)
            elif kind == "honor":
                stats["honor"] += ingest_honor(conn, header, rows, src, sheet_name)
    except Exception:
        log(f"  !! 解析失败 {src}\n{traceback.format_exc()}")
    conn.commit()
    return stats


def merge_json_lists(values: list[str]) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        if not raw:
            continue
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                for x in arr:
                    s = clean(str(x))
                    if s and s not in seen:
                        seen.add(s)
                        out.append(s)
                continue
        except json.JSONDecodeError:
            pass
        s = clean(raw)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return json.dumps(out, ensure_ascii=False)


def unique_join(values: list[str], sep: str = " | ") -> str:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        s = clean(v)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return sep.join(out)


def extract_positions(raw: str) -> list[str]:
    s = clean(raw)
    pos = []
    for p in ("上游", "中游", "下游"):
        if p in s:
            pos.append(p)
    return pos


def relatedness_rank(s: str) -> int:
    if "强" in s:
        return 3
    if "中" in s:
        return 2
    if "弱" in s:
        return 1
    return 0


def tag_scale(scale_raw: str, insured: int | None, capital_cny: float | None) -> str:
    raw = clean(scale_raw)
    if raw in {"大型", "中型", "小型", "微型"}:
        if raw == "大型":
            return "中大型"
        return raw
    score = 0
    if insured is not None:
        if insured >= 1000:
            score = max(score, 4)
        elif insured >= 300:
            score = max(score, 3)
        elif insured >= 50:
            score = max(score, 2)
        elif insured >= 10:
            score = max(score, 1)
    if capital_cny is not None:
        if capital_cny >= 100_000_000:
            score = max(score, 4)
        elif capital_cny >= 10_000_000:
            score = max(score, 3)
        elif capital_cny >= 1_000_000:
            score = max(score, 2)
        elif capital_cny >= 100_000:
            score = max(score, 1)
    return {4: "中大型", 3: "中型", 2: "小型", 1: "微型", 0: "未知"}.get(score, "未知")


def tag_lifecycle(status: str, est: date | None) -> str:
    st = clean(status)
    if any(x in st for x in ("注销", "吊销", "撤销", "停业", "清算")):
        return "注销"
    if "迁出" in st:
        return "迁出"
    if est:
        years = (TODAY - est).days / 365.25
        if years < 2:
            return "新设"
        if years < 8:
            return "成长"
        return "存续"
    if st in {"存续", "在业"}:
        return "存续"
    return "未知"


def tag_digital(phones: list, emails: list, website: str) -> str:
    n = int(bool(phones)) + int(bool(emails)) + int(bool(clean(website)))
    return {3: "高", 2: "中"}.get(n, "低")


def tag_finance(financing: str, honors: list[str]) -> str:
    blob = financing + " " + " ".join(honors)
    if any(k in blob for k in ("上市", "A股", "港股", "美股", "科创板", "创业板", "北交所")):
        return "已上市/有公开市场"
    if any(k in blob for k in ("融资", "股权", "战略投资", "VC", "PE", "独角兽")):
        return "有融资信号"
    if "未融资" in blob:
        return "未融资"
    return "未知"


def match_fit(industries: list[str], gb_fields: list[str]) -> tuple[str, int]:
    """返回 (命中业务线, 契合分 0-20)。"""
    blob_chain = " ".join(industries)
    blob_gb = " ".join(gb_fields)
    hits = []
    strength = 0
    for line, spec in BIZ_LINES.items():
        chain_hit = any(k in blob_chain for k in spec["chain"])
        gb_hit = any(k in blob_gb for k in spec["gb"])
        if chain_hit:
            hits.append(line)
            strength = max(strength, 20)
        elif gb_hit:
            hits.append(line + "(国标弱匹配)")
            strength = max(strength, 8)
    if not hits:
        return "其他", 3
    return " / ".join(hits), strength


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.startswith("["):
        try:
            x = json.loads(v)
            return x if isinstance(x, list) else []
        except json.JSONDecodeError:
            return []
    return []


def score_company(rec: dict) -> dict:
    phones = rec.get("phones_list") or _as_list(rec.get("phones"))
    emails = rec.get("emails_list") or _as_list(rec.get("emails"))
    legal = rec["legal_person"]
    website = rec["website"]
    insured = rec["insured_count"]
    capital = rec["capital_cny"]
    digital = rec["tag_digital"]
    scale = rec["tag_scale"]
    financing = rec["tag_finance"]
    lifecycle = rec["tag_lifecycle"]

    # 触达 30
    reach = 0
    if legal:
        reach += 8
    if len(phones) >= 2:
        reach += 12
    elif len(phones) == 1:
        reach += 8
    if emails:
        reach += 10

    # 吸引力 30
    attract = {"中大型": 12, "中型": 8, "小型": 5, "微型": 2, "未知": 3}.get(scale, 3)
    if capital is None:
        attract += 2
    elif capital >= 100_000_000:
        attract += 10
    elif capital >= 10_000_000:
        attract += 7
    elif capital >= 1_000_000:
        attract += 4
    else:
        attract += 2
    attract += {"高": 8, "中": 5, "低": 2}.get(digital, 2)

    fit_label, fit_score = rec["tag_fit"], rec["score_fit"]

    # 付费 20
    pay = 0
    if financing == "已上市/有公开市场":
        pay += 12
    elif financing == "有融资信号":
        pay += 8
    else:
        pay += 3
    if insured is None:
        pay += 2
    elif insured >= 1000:
        pay += 8
    elif insured >= 100:
        pay += 6
    elif insured >= 20:
        pay += 4
    else:
        pay += 2

    total = reach + attract + fit_score + pay
    if lifecycle == "注销":
        total = min(total, 18)
    rec.update(
        {
            "score_reach": reach,
            "score_attract": attract,
            "score_pay": pay,
            "lead_score": int(total),
            "tag_fit": fit_label,
        }
    )
    return rec


def build_master(conn: sqlite3.Connection) -> None:
    log("去重合并主表…")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_base_key ON staging_base(credit_key, fill_score)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_chain_key ON staging_chain(credit_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_honor_key ON staging_honor(credit_key)")

    conn.executescript(
        """
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY,
            credit_key TEXT UNIQUE,
            credit_code TEXT,
            company_name TEXT,
            status TEXT,
            legal_person TEXT,
            scale_raw TEXT,
            capital_raw TEXT,
            capital_cny REAL,
            capital_currency TEXT,
            established TEXT,
            company_age_years REAL,
            province TEXT,
            city TEXT,
            district TEXT,
            street TEXT,
            company_type TEXT,
            gb_cat TEXT, gb_major TEXT, gb_mid TEXT, gb_minor TEXT,
            former_name TEXT, en_name TEXT,
            insured_count INTEGER, insured_year TEXT,
            phones TEXT, emails TEXT,
            address TEXT, website TEXT, scope TEXT, intro TEXT,
            honors TEXT,
            chain_industries TEXT,
            chain_nodes TEXT,
            chain_positions TEXT,
            relatedness TEXT,
            financing_raw TEXT,
            source_files TEXT,
            tag_scale TEXT,
            tag_lifecycle TEXT,
            tag_digital TEXT,
            tag_finance TEXT,
            tag_fit TEXT,
            lat REAL, lng REAL,
            score_reach INTEGER,
            score_attract INTEGER,
            score_fit INTEGER,
            score_pay INTEGER,
            lead_score INTEGER
        );
        """
    )

    keys = [r[0] for r in conn.execute(
        """
        SELECT credit_key FROM staging_base
        UNION SELECT credit_key FROM staging_chain
        UNION SELECT credit_key FROM staging_honor
        """
    ) if r[0]]
    log(f"  唯一主体键：{len(keys):,}")

    # 预聚合 chain / honor
    chain_map: dict[str, dict] = defaultdict(lambda: {
        "names": [], "codes": [], "l1": [], "nodes": [], "pos": [], "rel": [], "fin": [], "files": [], "ind": []
    })
    for row in conn.execute(
        "SELECT credit_key,company_name,credit_code,l1,l2,l3,node,position,relatedness,financing,source_file,source_industry FROM staging_chain"
    ):
        c = chain_map[row[0]]
        c["names"].append(row[1])
        c["codes"].append(row[2])
        if row[3]:
            c["l1"].append(row[3])
        node = " / ".join(x for x in row[3:7] if x)
        if node:
            c["nodes"].append(node)
        c["pos"].extend(extract_positions(row[7] or ""))
        if row[8]:
            c["rel"].append(row[8])
        if row[9]:
            c["fin"].append(row[9])
        c["files"].append(row[10])
        if row[11]:
            c["ind"].append(row[11])

    honor_map: dict[str, list[str]] = defaultdict(list)
    for key, honor in conn.execute("SELECT credit_key, honor FROM staging_honor"):
        if honor:
            honor_map[key].append(honor)

    # 每个 key 取 fill_score 最高的 base 行
    base_best: dict[str, sqlite3.Row] = {}
    conn.row_factory = sqlite3.Row
    for row in conn.execute("SELECT * FROM staging_base ORDER BY fill_score DESC"):
        k = row["credit_key"]
        if k not in base_best:
            base_best[k] = row
    conn.row_factory = None

    cur = conn.cursor()
    batch = []
    n = 0
    for key in keys:
        b = base_best.get(key)
        ch = chain_map.get(key, None)
        honors = sorted(set(honor_map.get(key, [])))

        name = ""
        code = key if not key.startswith("N:") else ""
        phones, emails = [], []
        source_files = []
        industries = []

        rec = {
            "credit_key": key,
            "credit_code": code if valid_credit(code) else "",
            "company_name": "",
            "status": "",
            "legal_person": "",
            "scale_raw": "",
            "capital_raw": "",
            "capital_cny": None,
            "capital_currency": "",
            "established": "",
            "company_age_years": None,
            "province": "",
            "city": "",
            "district": "",
            "company_type": "",
            "gb_cat": "", "gb_major": "", "gb_mid": "", "gb_minor": "",
            "former_name": "", "en_name": "",
            "insured_count": None, "insured_year": "",
            "phones": "[]", "emails": "[]",
            "address": "", "website": "", "scope": "", "intro": "",
            "source_files": "",
        }

        if b:
            rec.update({
                "credit_code": b["credit_code"] or rec["credit_code"],
                "company_name": b["company_name"],
                "status": b["status"],
                "legal_person": b["legal_person"],
                "scale_raw": b["scale_raw"],
                "capital_raw": b["capital_raw"],
                "established": b["established"],
                "province": b["province"],
                "city": b["city"],
                "district": b["district"],
                "company_type": b["company_type"],
                "gb_cat": b["gb_cat"], "gb_major": b["gb_major"],
                "gb_mid": b["gb_mid"], "gb_minor": b["gb_minor"],
                "former_name": b["former_name"], "en_name": b["en_name"],
                "insured_year": b["insured_year"],
                "address": b["address"], "website": b["website"],
                "scope": b["scope"], "intro": b["intro"],
            })
            try:
                phones = json.loads(b["phones"] or "[]")
            except json.JSONDecodeError:
                phones = []
            try:
                emails = json.loads(b["emails"] or "[]")
            except json.JSONDecodeError:
                emails = []
            rec["insured_count"] = parse_int(b["insured"] or "")
            source_files.append(b["source_file"])
            if b["source_industry"]:
                industries.append(b["source_industry"])

        if ch:
            if not rec["company_name"]:
                rec["company_name"] = next((x for x in ch["names"] if x), "")
            if not rec["credit_code"]:
                rec["credit_code"] = next((x for x in ch["codes"] if valid_credit(x or "")), "")
            industries.extend(ch["l1"])
            industries.extend(ch["ind"])
            source_files.extend(ch["files"])

        industries = list(dict.fromkeys([x for x in industries if x]))
        positions = list(dict.fromkeys(ch["pos"] if ch else []))
        rels = ch["rel"] if ch else []
        relatedness = max(rels, key=relatedness_rank) if rels else ""
        fins = [x for x in (ch["fin"] if ch else []) if x]
        # 融资字段：丢掉纯荣誉词，优先含上市/融资
        fin_keep = [x for x in fins if any(k in x for k in ("上市", "融资", "股权", "投资", "未融资"))]
        financing_raw = unique_join(fin_keep or fins)

        cap_cny, ccy, _unit = parse_capital(rec["capital_raw"])
        rec["capital_cny"] = cap_cny
        rec["capital_currency"] = ccy
        est = parse_date(rec["established"])
        rec["established"] = est.isoformat() if est else rec["established"]
        rec["company_age_years"] = round((TODAY - est).days / 365.25, 1) if est else None

        rec["phones"] = json.dumps(phones, ensure_ascii=False)
        rec["emails"] = json.dumps(emails, ensure_ascii=False)
        rec["honors"] = json.dumps(honors, ensure_ascii=False)
        rec["chain_industries"] = " | ".join(industries)
        rec["chain_nodes"] = json.dumps(list(dict.fromkeys(ch["nodes"]))[:30] if ch else [], ensure_ascii=False)
        rec["chain_positions"] = " / ".join(positions)
        rec["relatedness"] = relatedness
        rec["financing_raw"] = financing_raw
        rec["source_files"] = unique_join(source_files)

        rec["tag_scale"] = tag_scale(rec["scale_raw"], rec["insured_count"], cap_cny)
        rec["tag_lifecycle"] = tag_lifecycle(rec["status"], est)
        rec["tag_digital"] = tag_digital(phones, emails, rec["website"])
        rec["tag_finance"] = tag_finance(financing_raw, honors)
        fit_label, fit_score = match_fit(
            industries, [rec["gb_cat"], rec["gb_major"], rec["gb_mid"], rec["gb_minor"]]
        )
        rec["tag_fit"] = fit_label
        rec["score_fit"] = fit_score
        rec["street"] = extract_street(
            rec["province"], rec["city"], rec["district"], rec["address"]
        )
        lat, lng = geo_of(rec["province"], rec["city"])
        rec["lat"], rec["lng"] = lat, lng

        rec["phones_list"] = phones
        rec["emails_list"] = emails
        rec["website"] = rec["website"]
        rec["legal_person"] = rec["legal_person"]
        score_company(rec)
        del rec["phones_list"]
        del rec["emails_list"]

        cols = [
            "credit_key", "credit_code", "company_name", "status", "legal_person",
            "scale_raw", "capital_raw", "capital_cny", "capital_currency",
            "established", "company_age_years", "province", "city", "district", "street",
            "company_type", "gb_cat", "gb_major", "gb_mid", "gb_minor",
            "former_name", "en_name", "insured_count", "insured_year",
            "phones", "emails", "address", "website", "scope", "intro",
            "honors", "chain_industries", "chain_nodes", "chain_positions",
            "relatedness", "financing_raw", "source_files",
            "tag_scale", "tag_lifecycle", "tag_digital", "tag_finance", "tag_fit",
            "lat", "lng", "score_reach", "score_attract", "score_fit", "score_pay", "lead_score",
        ]
        batch.append([rec.get(c) for c in cols])
        n += 1
        if len(batch) >= 2000:
            cur.executemany(
                f"INSERT OR REPLACE INTO companies ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                batch,
            )
            batch.clear()
        if n % 50000 == 0:
            log(f"  已写入 {n:,}")

    if batch:
        cols = [
            "credit_key", "credit_code", "company_name", "status", "legal_person",
            "scale_raw", "capital_raw", "capital_cny", "capital_currency",
            "established", "company_age_years", "province", "city", "district", "street",
            "company_type", "gb_cat", "gb_major", "gb_mid", "gb_minor",
            "former_name", "en_name", "insured_count", "insured_year",
            "phones", "emails", "address", "website", "scope", "intro",
            "honors", "chain_industries", "chain_nodes", "chain_positions",
            "relatedness", "financing_raw", "source_files",
            "tag_scale", "tag_lifecycle", "tag_digital", "tag_finance", "tag_fit",
            "lat", "lng", "score_reach", "score_attract", "score_fit", "score_pay", "lead_score",
        ]
        cur.executemany(
            f"INSERT OR REPLACE INTO companies ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            batch,
        )
    conn.commit()
    log(f"  主表完成 {n:,} 行")

    conn.executescript(
        """
        CREATE INDEX ix_score ON companies(lead_score DESC);
        CREATE INDEX ix_geo ON companies(province, city, district, street);
        CREATE INDEX ix_district ON companies(district);
        CREATE INDEX ix_street ON companies(street);
        CREATE INDEX ix_scale ON companies(tag_scale);
        CREATE INDEX ix_life ON companies(tag_lifecycle);
        CREATE INDEX ix_fit ON companies(tag_fit);
        CREATE INDEX ix_name ON companies(company_name);
        """
    )
    try:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE companies_fts USING fts5(
                company_name, legal_person, address, scope, intro, chain_industries, honors,
                content='companies', content_rowid='id'
            );
            INSERT INTO companies_fts(companies_fts) VALUES('rebuild');
            """
        )
        log("  FTS 全文索引已建")
    except sqlite3.OperationalError as e:
        log(f"  FTS 不可用（{e}），检索走 LIKE")
    conn.commit()


def export_csv(conn: sqlite3.Connection) -> None:
    log("导出 CSV…")
    cols = [
        "lead_score", "company_name", "credit_code", "legal_person", "phones", "emails",
        "website", "province", "city", "district", "address",
        "tag_scale", "tag_lifecycle", "tag_digital", "tag_finance", "tag_fit",
        "chain_industries", "chain_positions", "relatedness", "honors",
        "capital_raw", "capital_cny", "insured_count", "established", "company_age_years",
        "status", "gb_cat", "gb_major", "financing_raw", "score_reach", "score_attract",
        "score_fit", "score_pay",
    ]
    n = 0
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for row in conn.execute(
            f"SELECT {','.join(cols)} FROM companies ORDER BY lead_score DESC"
        ):
            w.writerow(row)
            n += 1
    with TOP_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for row in conn.execute(
            f"SELECT {','.join(cols)} FROM companies "
            f"WHERE tag_lifecycle != '注销' ORDER BY lead_score DESC LIMIT 500"
        ):
            w.writerow(row)
    log(f"  全量 CSV {n:,} 行 → {CSV_PATH.name}")
    log(f"  Top500 → {TOP_CSV_PATH.name}")


def summarize(conn: sqlite3.Connection) -> None:
    def q(sql):
        return conn.execute(sql).fetchall()

    total = q("SELECT COUNT(*) FROM companies")[0][0]
    reachable = q(
        "SELECT COUNT(*) FROM companies WHERE phones != '[]' AND tag_lifecycle != '注销'"
    )[0][0]
    high = q("SELECT COUNT(*) FROM companies WHERE lead_score >= 70")[0][0]
    log("======== 汇总 ========")
    log(f"企业数 {total:,} | 可电话触达(非注销) {reachable:,} | 高分≥70 {high:,}")
    log("规模：")
    for r in q("SELECT tag_scale, COUNT(*) c FROM companies GROUP BY 1 ORDER BY c DESC"):
        log(f"  {r[0]}  {r[1]:,}")
    log("生命周期：")
    for r in q("SELECT tag_lifecycle, COUNT(*) c FROM companies GROUP BY 1 ORDER BY c DESC"):
        log(f"  {r[0]}  {r[1]:,}")
    log("业务线契合：")
    for r in q("SELECT tag_fit, COUNT(*) c FROM companies GROUP BY 1 ORDER BY c DESC LIMIT 12"):
        log(f"  {r[0]}  {r[1]:,}")
    log("产业链 Top10：")
    # crude split
    counter: dict[str, int] = defaultdict(int)
    for (s,) in q("SELECT chain_industries FROM companies"):
        if not s:
            continue
        for p in s.split(" | "):
            if p:
                counter[p] += 1
    for k, v in sorted(counter.items(), key=lambda x: -x[1])[:10]:
        log(f"  {k}  {v:,}")
    dump_stats_json(conn, total, reachable, high, counter)


def dump_stats_json(conn, total, reachable, high, industry_counter) -> None:
    def kv(sql):
        return [{"name": r[0] or "未知", "value": r[1]} for r in conn.execute(sql)]

    listed = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE tag_finance LIKE '已上市%'"
    ).fetchone()[0]
    payload = {
        "total": total,
        "reachable": reachable,
        "high": conn.execute(
            "SELECT COUNT(*) FROM companies WHERE lead_score >= 70 AND tag_lifecycle != '注销'"
        ).fetchone()[0],
        "listed": listed,
        "provinces": kv(
            "SELECT province, COUNT(*) c FROM companies WHERE province != '' "
            "GROUP BY 1 ORDER BY c DESC LIMIT 20"
        ),
        "provinces_all": [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT province FROM companies WHERE province != '' ORDER BY 1"
            )
        ],
        "scales": kv("SELECT tag_scale, COUNT(*) FROM companies GROUP BY 1 ORDER BY 2 DESC"),
        "life": kv("SELECT tag_lifecycle, COUNT(*) FROM companies GROUP BY 1 ORDER BY 2 DESC"),
        "fit": kv("SELECT tag_fit, COUNT(*) FROM companies GROUP BY 1 ORDER BY 2 DESC LIMIT 12"),
        "score_bins": kv(
            """
            SELECT CASE
              WHEN lead_score >= 80 THEN '80-100'
              WHEN lead_score >= 70 THEN '70-79'
              WHEN lead_score >= 60 THEN '60-69'
              WHEN lead_score >= 50 THEN '50-59'
              ELSE '0-49'
            END AS b, COUNT(*) FROM companies GROUP BY 1
            ORDER BY 1 DESC
            """
        ),
        "industries": [
            {"name": k, "value": v}
            for k, v in sorted(industry_counter.items(), key=lambda x: -x[1])[:20]
        ],
    }
    STATS_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    log(f"  看板统计已写入 {STATS_PATH.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-files", type=int, default=0)
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists():
        LOG_PATH.unlink()

    files = sorted(SRC_INDUSTRY.rglob("*.xlsx"))
    if args.max_files:
        files = files[: args.max_files]
    log(f"待处理 xlsx：{len(files)}  总大小 {sum(f.stat().st_size for f in files)/1024/1024:.0f} MB")

    conn = connect()
    tot = {"base": 0, "chain": 0, "honor": 0}
    for i, f in enumerate(files, 1):
        log(f"[{i}/{len(files)}] {f.parent.name}/{f.name}  {f.stat().st_size/1024/1024:.1f}MB")
        st = ingest_file(conn, f)
        for k in tot:
            tot[k] += st[k]
        log(f"     base={st['base']:,} chain={st['chain']:,} honor={st['honor']:,}")

    log(f"staging 合计 base={tot['base']:,} chain={tot['chain']:,} honor={tot['honor']:,}")
    build_master(conn)
    export_csv(conn)
    summarize(conn)
    conn.close()
    log("完成。下一步：python app.py")


if __name__ == "__main__":
    main()
