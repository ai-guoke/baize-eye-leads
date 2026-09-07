# -*- coding: utf-8 -*-
"""从「2025年最新产业链企业相关数据」xlsx 打标签，并用「基础信息」补全主表。

逻辑：
1. 产业链 sheet → tags（按信用代码聚合：产业链 / 节点路径 / 位置 / 关联性 / 融资 / 荣誉）
2. 「基础信息」「信息匹配」sheet → companies 空字段补全 + contacts 追加电话邮箱

用法：
    python etl/load_chain_xlsx.py --limit 3          # 先 3 个文件验证
    python etl/load_chain_xlsx.py                   # 全量（断点续跑）
    python etl/load_chain_xlsx.py --tags-only
    python etl/load_chain_xlsx.py --enrich-only
    python etl/load_chain_xlsx.py --reset
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import app.doris_client as dc
from etl.load_jzh import (
    CONTACT_COLS,
    CREDIT_RE,
    EMAIL_RE,
    MOBILE_RE,
    clean,
    is_empty,
    norm_landline,
    norm_mail_address,
    parse_capital,
    parse_date,
    split_multi,
    tb,
)
from scripts.xlsx_stream import iter_rows, load_shared_strings, sheet_map

ROOT = Path(r"F:\企查查大数据\2025年最新产业链企业相关数据")
PLATFORM = Path(__file__).resolve().parent.parent
STATE = PLATFORM / "output" / "etl_state" / "chain_xlsx"
SOURCE = "chain2025"
COLLECTED_AT = "2025-01-01"
BATCH_CODES = 800

# 基础信息 / 信息匹配 表头 → 内部字段
BASE_COL = {
    "原文件导入名称": "name", "公司名称": "name",
    "登记状态": "status", "法定代表人": "legal", "企业规模": "scale",
    "注册资本": "capital", "实缴资本": "capital_paid",
    "成立日期": "established", "核准日期": "approved", "营业期限": "term",
    "所属省份": "province", "所属城市": "city", "所属区县": "district",
    "企业(机构)类型": "type", "公司类型": "type",
    "国标行业门类": "ind1", "国标行业大类": "ind2",
    "国标行业中类": "ind3", "国标行业小类": "ind4",
    "曾用名": "former", "英文名": "en_name",
    "统一社会信用代码": "credit", "纳税人识别号": "tax_id",
    "注册号": "reg_no", "组织机构代码": "org_code",
    "参保人数": "insured", "参保人数所属年报": "insured_year",
    "电话": "mobile", "可用电话": "mobile", "更多电话": "landline",
    "其他电话": "landline", "不可用电话": "_skip",
    "企业地址": "address", "注册地址": "address",
    "最新年报地址": "address_report", "通信地址": "address_mail",
    "官网": "website", "网址": "website",
    "邮箱": "email", "更多邮箱": "email2", "其他邮箱": "email2",
    "经营范围": "scope",
    "登记机关": "registrar", "纳税人资质": "taxpayer_qual",
    "企业简介": "intro", "最新年报年份": "report_year",
}

# 产业链 sheet 表头
CHAIN_COL = {
    "一级分类": "l1", "二级分类": "l2", "三级分类": "l3", "四级分类": "l4",
    "五级分类": "l5", "六级分类": "l6", "七级分类": "l7", "八级分类": "l8",
    "公司名称": "name", "公司": "name",
    "ID": "credit", "统一社会信用代码": "credit",
    "产业链节点": "node", "产业位置": "position",
    "产业关联性": "relatedness", "融资上市": "financing",
    "资质": "honors", "荣誉": "honors",
}

TAG_COLS = [
    "credit_code", "company_name", "chain_industries", "chain_nodes",
    "chain_positions", "relatedness", "honors", "financing_raw",
    "tag_finance", "loaded_at",
]

# 仅补全这些主表字段（partial_columns）
ENRICH_FIELDS = [
    "company_name", "status", "legal_person", "scale",
    "capital_wan", "capital_raw", "capital_currency", "capital_paid_wan",
    "established", "approved", "term_raw",
    "province", "city", "district", "company_type",
    "industry_l1", "industry_l2", "industry_l3", "industry_l4",
    "insured_count", "insured_year", "former_name", "en_name",
    "tax_id", "reg_no", "org_code",
    "address", "address_report", "address_mail", "website", "scope",
    "registrar", "taxpayer_qual", "intro", "report_year",
]

RELATE_RANK = {"强": 3, "较强": 2, "中": 1, "弱": 0}
POS_ORDER = ("上游", "中游", "下游")


def arr_literal(vals: list[str] | None) -> str | None:
    if not vals:
        return None
    inner = ",".join('"' + str(x).replace("\\", "\\\\").replace('"', '\\"') + '"' for x in vals)
    return f"[{inner}]"


def parse_arr(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    if not s:
        return []
    try:
        x = json.loads(s)
        if isinstance(x, list):
            out = []
            for item in x:
                t = str(item).strip()
                if " | " in t:
                    out.extend(p.strip() for p in t.split("|") if p.strip())
                elif t:
                    out.append(t)
            return out
    except Exception:
        pass
    if " | " in s:
        return [p.strip() for p in s.split("|") if p.strip()]
    return [s]


def uniq_keep(seq: list[str], limit: int = 40) -> list[str]:
    seen, out = set(), []
    for x in seq:
        x = (x or "").strip()
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
        if len(out) >= limit:
            break
    return out


def finance_tag(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if is_empty(s) or s in {"未融资", "未知"}:
        return "未融资" if s == "未融资" else "未知"
    if any(k in s for k in ("上市", "主板", "创业板", "科创板", "北交所", "港股", "美股")):
        return "已上市"
    return "有融资信号"


def best_relatedness(vals: list[str]) -> str | None:
    best, score = None, -1
    for v in vals:
        r = RELATE_RANK.get(v, -1)
        if r > score:
            best, score = v, r
    return best


def merge_positions(vals: list[str]) -> str | None:
    found = []
    for v in vals:
        for p in POS_ORDER:
            if p in (v or "") and p not in found:
                found.append(p)
    return "、".join(found) if found else None


def map_header(header: list, mapping: dict[str, str]) -> dict[str, int]:
    pos: dict[str, int] = {}
    for i, h in enumerate(header):
        key = mapping.get((h or "").strip())
        if key and key != "_skip" and key not in pos:
            pos[key] = i
    return pos


def sheet_kind(name: str) -> str:
    n = (name or "").strip()
    if n in {"基础信息", "信息匹配"} or "基础信息" in n or "信息匹配" in n:
        return "base"
    if n in {"Sheet3", "说明", "使用说明"}:
        return "skip"
    return "chain"


def state_key(path: Path) -> str:
    return f"{path.parent.name}__{path.stem}__{path.stat().st_size}"


def discover_files() -> list[Path]:
    if not ROOT.is_dir():
        return []
    return sorted(ROOT.rglob("*.xlsx"))


def extract_contacts(code: str, name: str, mobiles: list[str], landlines: list[str],
                     emails: list[str], now: str) -> list[list]:
    rows = []
    for i, v in enumerate(mobiles):
        digits = "".join(re.findall(r"\d", v))
        if not MOBILE_RE.match(digits):
            # 可能是座机塞进电话列
            ll = norm_landline(v)
            if ll:
                rows.append([code, "landline", ll, SOURCE, name, 0, COLLECTED_AT, now])
            continue
        rows.append([code, "mobile", digits, SOURCE, name, 1 if i == 0 else 0, COLLECTED_AT, now])
    for v in landlines:
        digits = "".join(re.findall(r"\d", v))
        if MOBILE_RE.match(digits):
            rows.append([code, "mobile", digits, SOURCE, name, 0, COLLECTED_AT, now])
            continue
        ll = norm_landline(v)
        if ll:
            rows.append([code, "landline", ll, SOURCE, name, 0, COLLECTED_AT, now])
    for v in emails:
        e = v.strip().lower()
        if EMAIL_RE.match(e):
            rows.append([code, "email", tb(e, 200), SOURCE, name, 0, COLLECTED_AT, now])
    return rows


def parse_base_row(pos: dict[str, int], row: list) -> dict | None:
    n = len(row)

    def g(f: str) -> str:
        i = pos.get(f, -1)
        return row[i].strip() if 0 <= i < n and row[i] else ""

    name = clean(g("name"))
    code = g("credit").upper().replace(" ", "")
    if not name and not code:
        return None
    if not CREDIT_RE.match(code):
        return None
    name = tb(name or code, 300)

    cap_wan, cap_cur = parse_capital(g("capital"), name)
    paid_wan, _ = parse_capital(g("capital_paid"), name)
    est = parse_date(g("established"))
    appr = parse_date(g("approved"))

    mobiles = split_multi(g("mobile"))
    landlines = split_multi(g("landline"))
    emails = split_multi(g("email")) + split_multi(g("email2"))

    insured = None
    if not is_empty(g("insured")):
        digits = "".join(re.findall(r"\d", g("insured")))
        if digits:
            try:
                insured = int(digits)
            except ValueError:
                insured = None

    return {
        "credit_code": code,
        "company_name": name,
        "status": clean(g("status")),
        "legal_person": tb(clean(g("legal")), 128),
        "scale": clean(g("scale")),
        "capital_wan": cap_wan,
        "capital_raw": tb(clean(g("capital")), 64),
        "capital_currency": cap_cur,
        "capital_paid_wan": paid_wan,
        "established": est,
        "approved": appr,
        "term_raw": tb(clean(g("term")), 64),
        "province": clean(g("province")),
        "city": clean(g("city")),
        "district": clean(g("district")),
        "company_type": tb(clean(g("type")), 128),
        "industry_l1": tb(clean(g("ind1")), 64),
        "industry_l2": tb(clean(g("ind2")), 64),
        "industry_l3": tb(clean(g("ind3")), 64),
        "industry_l4": tb(clean(g("ind4")), 64),
        "insured_count": insured,
        "insured_year": tb(clean(g("insured_year")), 8),
        "former_name": tb(clean(g("former")), 500),
        "en_name": tb(clean(g("en_name")), 500),
        "tax_id": tb(clean(g("tax_id")), 32),
        "reg_no": tb(clean(g("reg_no")), 32),
        "org_code": tb(clean(g("org_code")), 32),
        "address": tb(clean(g("address")), 500),
        "address_report": tb(clean(g("address_report")), 500),
        "address_mail": tb(norm_mail_address(g("address_mail")), 65533) if g("address_mail") else None,
        "website": tb(clean(g("website")), 300),
        "scope": clean(g("scope")),
        "registrar": tb(clean(g("registrar")), 200),
        "taxpayer_qual": tb(clean(g("taxpayer_qual")), 64),
        "intro": clean(g("intro")),
        "report_year": tb(clean(g("report_year")), 8),
        "_mobiles": mobiles,
        "_landlines": landlines,
        "_emails": emails,
    }


def parse_chain_row(pos: dict[str, int], row: list, fallback_industry: str) -> dict | None:
    n = len(row)

    def g(f: str) -> str:
        i = pos.get(f, -1)
        return row[i].strip() if 0 <= i < n and row[i] else ""

    name = clean(g("name"))
    code = g("credit").upper().replace(" ", "")
    if not CREDIT_RE.match(code):
        return None
    levels = [clean(g(f"l{i}")) for i in range(1, 9)]
    levels = [x for x in levels if x]
    l1 = levels[0] if levels else clean(fallback_industry)
    node = clean(g("node")) or (levels[-1] if levels else None)
    path_parts = levels[:]
    if node and (not path_parts or path_parts[-1] != node):
        path_parts.append(node)
    path = " / ".join(path_parts) if path_parts else None

    honors_raw = g("honors")
    honors = []
    if not is_empty(honors_raw):
        honors = [p.strip() for p in re.split(r"[,，;；、|/]", honors_raw) if p.strip()]

    return {
        "credit_code": code,
        "company_name": tb(name or code, 300),
        "industry": l1,
        "node_path": path,
        "position": clean(g("position")),
        "relatedness": clean(g("relatedness")),
        "financing": clean(g("financing")),
        "honors": honors,
    }


def process_file(path: Path, do_tags: bool, do_enrich: bool) -> dict:
    key = state_key(path)
    done = STATE / f"{key}.json"
    if done.exists():
        st = json.loads(done.read_text(encoding="utf-8"))
        st["skipped"] = True
        return st

    t0 = time.time()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fallback_industry = path.parent.name.replace("产业链企查查", "").replace("企查查", "").strip()
    try:
        file_label = str(path.relative_to(ROOT))
    except ValueError:
        file_label = path.name
    st = {
        "file": file_label,
        "chain_rows": 0, "base_rows": 0, "tags": 0, "enrich": 0, "contacts": 0,
        "unmatched_name": 0, "bad": 0,
    }

    tag_acc: dict[str, dict] = {}
    base_best: dict[str, dict] = {}

    with zipfile.ZipFile(path) as z:
        sst = load_shared_strings(z)
        for sheet_name, xml_path in sheet_map(z).items():
            kind = sheet_kind(sheet_name)
            if kind == "skip":
                continue
            rows = iter_rows(z, xml_path, sst)
            try:
                header = next(rows)
            except StopIteration:
                continue

            if kind == "chain" and do_tags:
                pos = map_header(header, CHAIN_COL)
                if "credit" not in pos:
                    continue
                for row in rows:
                    st["chain_rows"] += 1
                    rec = parse_chain_row(pos, row, fallback_industry)
                    if not rec:
                        st["bad"] += 1
                        continue
                    code = rec["credit_code"]
                    acc = tag_acc.setdefault(code, {
                        "company_name": rec["company_name"],
                        "industries": [], "nodes": [], "positions": [],
                        "related": [], "financing": [], "honors": [],
                    })
                    if rec["company_name"] and len(rec["company_name"]) > len(acc["company_name"] or ""):
                        acc["company_name"] = rec["company_name"]
                    if rec["industry"]:
                        acc["industries"].append(rec["industry"])
                    if rec["node_path"]:
                        acc["nodes"].append(rec["node_path"])
                    if rec["position"]:
                        acc["positions"].append(rec["position"])
                    if rec["relatedness"]:
                        acc["related"].append(rec["relatedness"])
                    if rec["financing"]:
                        acc["financing"].append(rec["financing"])
                    acc["honors"].extend(rec["honors"])

            elif kind == "base" and do_enrich:
                pos = map_header(header, BASE_COL)
                if "credit" not in pos:
                    continue
                for row in rows:
                    st["base_rows"] += 1
                    rec = parse_base_row(pos, row)
                    if not rec:
                        st["bad"] += 1
                        continue
                    code = rec["credit_code"]
                    # 同文件内保留字段更全的一行
                    score = sum(1 for k, v in rec.items() if not k.startswith("_") and v not in (None, ""))
                    prev = base_best.get(code)
                    if not prev or score > prev["_score"]:
                        rec["_score"] = score
                        base_best[code] = rec

    # ---- 写 tags：与库内已有合并 ----
    if tag_acc:
        codes = list(tag_acc.keys())
        existing: dict[str, dict] = {}
        for i in range(0, len(codes), BATCH_CODES):
            part = codes[i:i + BATCH_CODES]
            inlist = ",".join(f"'{c}'" for c in part)
            for r in dc.query(
                f"SELECT credit_code, company_name, chain_industries, chain_nodes, "
                f"chain_positions, relatedness, honors, financing_raw, tag_finance "
                f"FROM tags WHERE credit_code IN ({inlist})"
            ):
                existing[r["credit_code"]] = r

        tag_rows = []
        for code, acc in tag_acc.items():
            old = existing.get(code) or {}
            industries = uniq_keep(parse_arr(old.get("chain_industries")) + acc["industries"])
            nodes = uniq_keep(parse_arr(old.get("chain_nodes")) + acc["nodes"], limit=60)
            honors = uniq_keep(parse_arr(old.get("honors")) + acc["honors"], limit=40)
            positions = merge_positions(
                ([old.get("chain_positions")] if old.get("chain_positions") else []) + acc["positions"]
            )
            related = best_relatedness(
                ([old.get("relatedness")] if old.get("relatedness") else []) + acc["related"]
            )
            fins = [x for x in ([old.get("financing_raw")] if old.get("financing_raw") else []) + acc["financing"] if x]
            financing = fins[-1] if fins else None
            name = acc["company_name"] or old.get("company_name")
            tag_rows.append([
                code, name,
                arr_literal(industries), arr_literal(nodes),
                positions, related, arr_literal(honors), financing,
                finance_tag(financing) or old.get("tag_finance"),
                now,
            ])

        if tag_rows:
            label = f"chain_tags_{hashlib.md5(key.encode()).hexdigest()[:16]}"
            r = dc.stream_load("tags", TAG_COLS, dc.make_csv(tag_rows),
                               label=label, max_filter_ratio=0.05)
            st["tags"] = int(r.get("NumberLoadedRows") or 0)
            st["load_tags"] = r.get("Status")

    # ---- 写 enrich：只补空 + 追加 contacts ----
    if base_best:
        codes = list(base_best.keys())
        existing: dict[str, dict] = {}
        for i in range(0, len(codes), BATCH_CODES):
            part = codes[i:i + BATCH_CODES]
            inlist = ",".join(f"'{c}'" for c in part)
            cols = ",".join(["credit_code", "fill_score"] + ENRICH_FIELDS)
            for r in dc.query(f"SELECT {cols} FROM companies WHERE credit_code IN ({inlist})"):
                existing[r["credit_code"]] = r

        contact_rows: list[list] = []
        # 按「本批实际更新列集合」分组，避免 partial 列不一致
        buckets: dict[tuple[str, ...], list[list]] = defaultdict(list)

        for code, src in base_best.items():
            old = existing.get(code)
            contact_rows.extend(extract_contacts(
                code, src["company_name"],
                src.get("_mobiles") or [], src.get("_landlines") or [],
                src.get("_emails") or [], now,
            ))
            if not old:
                # 主库无此主体：整行插入用高 fill_score（走完整列更稳，这里用 partial 最小集 + 名称）
                patch = {"credit_code": code, "company_name": src["company_name"], "fill_score": 50}
                for f in ENRICH_FIELDS:
                    if f == "company_name":
                        continue
                    v = src.get(f)
                    if v not in (None, ""):
                        patch[f] = v
                keys = tuple(["credit_code"] + [k for k in patch if k != "credit_code"])
                buckets[keys].append([patch[k] for k in keys])
                continue

            patch = {"credit_code": code}
            filled = 0
            for f in ENRICH_FIELDS:
                new_v = src.get(f)
                if new_v in (None, ""):
                    continue
                old_v = old.get(f)
                if old_v in (None, ""):
                    patch[f] = new_v
                    filled += 1
            if filled:
                patch["fill_score"] = int(old.get("fill_score") or 0) + filled
                keys = tuple(["credit_code"] + [k for k in patch if k != "credit_code"])
                buckets[keys].append([patch[k] for k in keys])

        loaded_co = 0
        for i, (keys, rows) in enumerate(buckets.items()):
            label = f"chain_en_{hashlib.md5((key+str(i)).encode()).hexdigest()[:14]}"
            r = dc.stream_load(
                "companies", list(keys), dc.make_csv(rows),
                label=label, max_filter_ratio=0.05, partial_columns=True,
            )
            loaded_co += int(r.get("NumberLoadedRows") or 0)
        st["enrich"] = loaded_co

        if contact_rows:
            # 联系方式 UNIQUE KEY 去重，重复写入无害
            label = f"chain_ct_{hashlib.md5(key.encode()).hexdigest()[:16]}"
            # 同批内去重
            uniq = {}
            for row in contact_rows:
                uniq[(row[0], row[1], row[2])] = row
            r = dc.stream_load(
                "contacts", CONTACT_COLS, dc.make_csv(uniq.values()),
                label=label, max_filter_ratio=0.05,
            )
            st["contacts"] = int(r.get("NumberLoadedRows") or 0)

            # 刷新 has_*：对有联系方式的企业重算（轻量：只更新标志）
            flag_codes = sorted({r[0] for r in uniq.values()})
            _refresh_flags(flag_codes)

    st["seconds"] = round(time.time() - t0, 2)
    STATE.mkdir(parents=True, exist_ok=True)
    done.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    return st


def _refresh_flags(codes: list[str]) -> None:
    """按 contacts 重算 has_* / counts，partial 写回。"""
    if not codes:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows_out = []
    for i in range(0, len(codes), BATCH_CODES):
        part = codes[i:i + BATCH_CODES]
        inlist = ",".join(f"'{c}'" for c in part)
        stats = {
            r["credit_code"]: r for r in dc.query(
                f"SELECT credit_code, "
                f"SUM(CASE WHEN contact_type='mobile' THEN 1 ELSE 0 END) AS m, "
                f"SUM(CASE WHEN contact_type='landline' THEN 1 ELSE 0 END) AS l, "
                f"SUM(CASE WHEN contact_type='email' THEN 1 ELSE 0 END) AS e, "
                f"COUNT(*) AS n "
                f"FROM contacts WHERE credit_code IN ({inlist}) GROUP BY credit_code"
            )
        }
        old_fs = {
            r["credit_code"]: int(r.get("fill_score") or 0)
            for r in dc.query(
                f"SELECT credit_code, fill_score FROM companies WHERE credit_code IN ({inlist})"
            )
        }
        for code in part:
            s = stats.get(code)
            if not s:
                continue
            m = int(s["m"] or 0)
            l = int(s["l"] or 0)
            e = int(s["e"] or 0)
            n = int(s["n"] or 0)
            rows_out.append([
                code,
                1 if m else 0, 1 if l else 0, 1 if e else 0, 1 if n else 0,
                m, e, old_fs.get(code, 0) + 1,
            ])
    if not rows_out:
        return
    cols = [
        "credit_code", "has_mobile", "has_landline", "has_email", "has_contact",
        "mobile_count", "email_count", "fill_score",
    ]
    label = f"chain_flags_{int(time.time())}_{len(rows_out)}"
    dc.stream_load("companies", cols, dc.make_csv(rows_out),
                   label=label, max_filter_ratio=0.05, partial_columns=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个 xlsx")
    ap.add_argument("--tags-only", action="store_true")
    ap.add_argument("--enrich-only", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    do_tags = not args.enrich_only
    do_enrich = not args.tags_only
    if args.tags_only and args.enrich_only:
        do_tags = do_enrich = True

    if not dc.wait_ready(timeout=60):
        print("Doris 未就绪")
        sys.exit(1)

    if args.reset and STATE.exists():
        import shutil
        shutil.rmtree(STATE)
        print("已清空断点")

    files = discover_files()
    if not files:
        print(f"未找到 xlsx：{ROOT}")
        sys.exit(1)
    if args.limit:
        files = files[: args.limit]

    print(f"待处理 {len(files)} 个文件 · tags={do_tags} enrich={do_enrich}")
    agg = defaultdict(int)
    t0 = time.time()
    for i, path in enumerate(files, 1):
        st = process_file(path, do_tags, do_enrich)
        skipped = st.get("skipped")
        for k in ("chain_rows", "base_rows", "tags", "enrich", "contacts", "bad"):
            agg[k] += int(st.get(k) or 0)
        flag = "SKIP" if skipped else "OK"
        print(
            f"[{i}/{len(files)}] {flag} {st.get('file')} "
            f"chain={st.get('chain_rows', 0)} base={st.get('base_rows', 0)} "
            f"tags+={st.get('tags', 0)} en+={st.get('enrich', 0)} ct+={st.get('contacts', 0)} "
            f"{st.get('seconds', 0)}s"
        )

    print("\n==== 汇总 ====")
    for k, v in agg.items():
        print(f"  {k}: {v:,}")
    print(f"  耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
