# -*- coding: utf-8 -*-
"""从「工商注册企业全量信息（更新2023.11）」按信用代码补全主库空字段，并追加联系方式。

不整库替换：已有非空字段保留；源侧非空且主库为空 → 写入；电话/邮箱追加到 contacts。

用法：
    python etl/enrich_national_2023.py --db qcc_lab --limit 5   # 实验室样本（推荐先跑）
    python etl/enrich_national_2023.py --limit 2
    python etl/enrich_national_2023.py                         # 全量断点续跑（默认写 qcc）
    python etl/enrich_national_2023.py --reset
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
    PROVINCE_MAP,
    STATUS_MAP,
    clean,
    is_empty,
    norm_landline,
    parse_capital,
    parse_date,
    split_multi,
    tb,
)
from scripts.xlsx_stream import iter_rows, load_shared_strings, sheet_map

# 默认指向 raw_datasets/01（853 分片）；旧目录可作为兼容回退
_RAW01 = Path(r"F:\企查查大数据\raw_datasets\01_全国企业工商底册库_2023.11\全国数据")
_LEGACY = Path(r"F:\企查查大数据\工商注册企业全量信息（更新2023.11）\全国数据")
ROOT = _RAW01 if _RAW01.is_dir() else _LEGACY
PLATFORM = Path(__file__).resolve().parent.parent
SOURCE = "national2023"
COLLECTED_AT = "2023-11-01"
BATCH = 800

# 运行期由 main() 注入；默认生产库，实验室必须显式 --db qcc_lab
TARGET_DB = "qcc"
STATE = PLATFORM / "output" / "etl_state" / "national2023"

COL = {
    "企业名称": "name", "公司名称": "name",
    "经营状态": "status", "登记状态": "status",
    "法定代表人": "legal",
    "注册资本": "capital", "实缴资本": "capital_paid",
    "成立日期": "established", "核准日期": "approved", "营业期限": "term",
    "所属省份": "province", "所属城市": "city", "所属区县": "district",
    "统一社会信用代码": "credit", "纳税人识别号": "tax_id",
    "工商注册号": "reg_no", "注册号": "reg_no", "组织机构代码": "org_code",
    "参保人数": "insured",
    "企业类型": "type", "公司类型": "type",
    "所属行业": "industry",  # 单列 → industry_l1
    "曾用名": "former",
    "注册地址": "address", "网址": "website",
    "联系电话": "phone", "邮箱": "email",
    "经营范围": "scope",
}

ENRICH_FIELDS = [
    "company_name", "status", "legal_person",
    "capital_wan", "capital_raw", "capital_currency", "capital_paid_wan",
    "established", "approved", "term_raw",
    "province", "city", "district", "company_type",
    "industry_l1", "insured_count", "former_name",
    "tax_id", "reg_no", "org_code",
    "address", "website", "scope",
]


def map_header(header: list) -> dict[str, int]:
    pos: dict[str, int] = {}
    for i, h in enumerate(header):
        key = COL.get((h or "").strip())
        if key and key not in pos:
            pos[key] = i
    return pos


def state_key(path: Path) -> str:
    return f"{path.stem}__{path.stat().st_size}"


def parse_row(pos: dict[str, int], row: list) -> dict | None:
    n = len(row)

    def g(f: str) -> str:
        i = pos.get(f, -1)
        return row[i].strip() if 0 <= i < n and row[i] else ""

    name = clean(g("name"))
    code = g("credit").upper().replace(" ", "")
    if not name or not CREDIT_RE.match(code):
        return None
    name = tb(name, 300)
    cap_wan, cap_cur = parse_capital(g("capital"), name)
    paid_wan, _ = parse_capital(g("capital_paid"), name)
    status_raw = clean(g("status")) or ""
    status = STATUS_MAP.get(status_raw, status_raw) or None
    prov = clean(g("province"))
    if prov:
        prov = PROVINCE_MAP.get(prov, prov)

    insured = None
    if not is_empty(g("insured")):
        digits = "".join(re.findall(r"\d", g("insured")))
        if digits:
            try:
                insured = int(digits)
            except ValueError:
                insured = None

    phones = split_multi(g("phone"))
    emails = split_multi(g("email"))
    mobiles, landlines = [], []
    for p in phones:
        digits = "".join(re.findall(r"\d", p))
        if MOBILE_RE.match(digits):
            mobiles.append(digits)
        else:
            ll = norm_landline(p)
            if ll:
                landlines.append(ll)

    return {
        "credit_code": code,
        "company_name": name,
        "status": status,
        "legal_person": tb(clean(g("legal")), 128),
        "capital_wan": cap_wan,
        "capital_raw": tb(clean(g("capital")), 64),
        "capital_currency": cap_cur,
        "capital_paid_wan": paid_wan,
        "established": parse_date(g("established")),
        "approved": parse_date(g("approved")),
        "term_raw": tb(clean(g("term")), 64),
        "province": prov,
        "city": clean(g("city")),
        "district": clean(g("district")),
        "company_type": tb(clean(g("type")), 128),
        "industry_l1": tb(clean(g("industry")), 64),
        "insured_count": insured,
        "former_name": tb(clean(g("former")), 500),
        "tax_id": tb(clean(g("tax_id")), 32),
        "reg_no": tb(clean(g("reg_no")), 32),
        "org_code": tb(clean(g("org_code")), 32),
        "address": tb(clean(g("address")), 500),
        "website": tb(clean(g("website")), 300),
        "scope": clean(g("scope")),
        "_mobiles": mobiles,
        "_landlines": landlines,
        "_emails": [e.strip().lower() for e in emails if EMAIL_RE.match(e.strip().lower())],
    }


def refresh_flags(codes: list[str]) -> None:
    if not codes:
        return
    rows_out = []
    for i in range(0, len(codes), BATCH):
        part = codes[i:i + BATCH]
        inlist = ",".join(f"'{c}'" for c in part)
        stats = {
            r["credit_code"]: r for r in dc.query(
                f"SELECT credit_code, "
                f"SUM(CASE WHEN contact_type='mobile' THEN 1 ELSE 0 END) AS m, "
                f"SUM(CASE WHEN contact_type='landline' THEN 1 ELSE 0 END) AS l, "
                f"SUM(CASE WHEN contact_type='email' THEN 1 ELSE 0 END) AS e, "
                f"COUNT(*) AS n "
                f"FROM contacts WHERE credit_code IN ({inlist}) GROUP BY credit_code",
                db=TARGET_DB,
            )
        }
        old_fs = {
            r["credit_code"]: int(r.get("fill_score") or 0)
            for r in dc.query(
                f"SELECT credit_code, fill_score FROM companies WHERE credit_code IN ({inlist})",
                db=TARGET_DB,
            )
        }
        for code in part:
            s = stats.get(code)
            if not s:
                continue
            m, l, e, n = int(s["m"] or 0), int(s["l"] or 0), int(s["e"] or 0), int(s["n"] or 0)
            rows_out.append([
                code, 1 if m else 0, 1 if l else 0, 1 if e else 0, 1 if n else 0,
                m, e, old_fs.get(code, 0) + 1,
            ])
    if rows_out:
        dc.stream_load(
            "companies",
            ["credit_code", "has_mobile", "has_landline", "has_email", "has_contact",
             "mobile_count", "email_count", "fill_score"],
            dc.make_csv(rows_out),
            label=f"n2023_flags_{int(time.time())}",
            max_filter_ratio=0.05,
            partial_columns=True,
            db=TARGET_DB,
        )


def process_file(path: Path) -> dict:
    key = state_key(path)
    done = STATE / f"{key}.json"
    if done.exists():
        st = json.loads(done.read_text(encoding="utf-8"))
        st["skipped"] = True
        return st

    t0 = time.time()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st = {"file": path.name, "rows": 0, "parsed": 0, "enrich": 0, "contacts": 0,
          "new_co": 0, "bad": 0}

    best: dict[str, dict] = {}
    with zipfile.ZipFile(path) as z:
        sst = load_shared_strings(z)
        mapping = sheet_map(z)
        rows = iter_rows(z, next(iter(mapping.values())), sst)
        header = next(rows)
        pos = map_header(header)
        if "credit" not in pos or "name" not in pos:
            st["error"] = f"bad header {header[:5]}"
            STATE.mkdir(parents=True, exist_ok=True)
            done.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
            return st
        for row in rows:
            st["rows"] += 1
            rec = parse_row(pos, row)
            if not rec:
                st["bad"] += 1
                continue
            st["parsed"] += 1
            score = sum(1 for k, v in rec.items() if not k.startswith("_") and v not in (None, ""))
            prev = best.get(rec["credit_code"])
            if not prev or score > prev.get("_score", 0):
                rec["_score"] = score
                best[rec["credit_code"]] = rec

    codes = list(best.keys())
    existing: dict[str, dict] = {}
    for i in range(0, len(codes), BATCH):
        part = codes[i:i + BATCH]
        inlist = ",".join(f"'{c}'" for c in part)
        cols = ",".join(["credit_code", "fill_score"] + ENRICH_FIELDS)
        for r in dc.query(f"SELECT {cols} FROM companies WHERE credit_code IN ({inlist})", db=TARGET_DB):
            existing[r["credit_code"]] = r

    buckets: dict[tuple[str, ...], list[list]] = defaultdict(list)
    contact_rows: list[list] = []
    touched = set()

    for code, src in best.items():
        name = src["company_name"]
        for i, m in enumerate(src.get("_mobiles") or []):
            contact_rows.append([code, "mobile", m, SOURCE, name, 1 if i == 0 else 0, COLLECTED_AT, now])
        for ll in src.get("_landlines") or []:
            contact_rows.append([code, "landline", ll, SOURCE, name, 0, COLLECTED_AT, now])
        for e in src.get("_emails") or []:
            contact_rows.append([code, "email", tb(e, 200), SOURCE, name, 0, COLLECTED_AT, now])

        old = existing.get(code)
        if not old:
            patch = {"credit_code": code, "fill_score": int(src.get("_score") or 20)}
            for f in ENRICH_FIELDS:
                v = src.get(f)
                if v not in (None, ""):
                    patch[f] = v
            keys = tuple(["credit_code"] + [k for k in patch if k != "credit_code"])
            buckets[keys].append([patch[k] for k in keys])
            st["new_co"] += 1
            touched.add(code)
            continue

        patch = {"credit_code": code}
        filled = 0
        for f in ENRICH_FIELDS:
            new_v = src.get(f)
            if new_v in (None, ""):
                continue
            if old.get(f) in (None, ""):
                patch[f] = new_v
                filled += 1
        if filled:
            patch["fill_score"] = int(old.get("fill_score") or 0) + filled
            keys = tuple(["credit_code"] + [k for k in patch if k != "credit_code"])
            buckets[keys].append([patch[k] for k in keys])
            touched.add(code)

    loaded = 0
    for i, (keys, rows) in enumerate(buckets.items()):
        r = dc.stream_load(
            "companies", list(keys), dc.make_csv(rows),
            label=f"n2023_en_{hashlib.md5((key+str(i)).encode()).hexdigest()[:14]}",
            max_filter_ratio=0.05, partial_columns=True,
            db=TARGET_DB,
        )
        loaded += int(r.get("NumberLoadedRows") or 0)
    st["enrich"] = loaded

    if contact_rows:
        uniq = {(r[0], r[1], r[2]): r for r in contact_rows}
        r = dc.stream_load(
            "contacts", CONTACT_COLS, dc.make_csv(uniq.values()),
            label=f"n2023_ct_{hashlib.md5(key.encode()).hexdigest()[:16]}",
            max_filter_ratio=0.05,
            db=TARGET_DB,
        )
        st["contacts"] = int(r.get("NumberLoadedRows") or 0)
        refresh_flags(sorted({r[0] for r in uniq}))

    st["seconds"] = round(time.time() - t0, 2)
    STATE.mkdir(parents=True, exist_ok=True)
    done.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    return st


def main() -> None:
    global TARGET_DB, STATE, ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--db", default="qcc", help="目标库；实验室请显式传 qcc_lab")
    ap.add_argument("--root", default="", help="xlsx 根目录，默认 raw_datasets/01")
    ap.add_argument(
        "--files", default="",
        help="逗号分隔文件名（不含路径），用于精确抽样本；与 --limit 二选一优先",
    )
    args = ap.parse_args()

    TARGET_DB = args.db.strip() or "qcc"
    if TARGET_DB not in ("qcc", "qcc_lab"):
        print(f"拒绝：未知目标库 {TARGET_DB!r}，只允许 qcc / qcc_lab")
        sys.exit(2)
    if args.root:
        ROOT = Path(args.root)
    # 实验室断点与生产隔离，避免互相 SKIP
    STATE = PLATFORM / "output" / "etl_state" / (
        "national2023_lab" if TARGET_DB == "qcc_lab" else "national2023"
    )

    if not dc.wait_ready(timeout=60):
        print("Doris 未就绪")
        sys.exit(1)
    if args.reset and STATE.exists():
        import shutil
        shutil.rmtree(STATE)
        print("已清空断点", STATE)

    files = sorted(ROOT.glob("*.xlsx")) if ROOT.is_dir() else []
    if not files:
        print(f"未找到 xlsx：{ROOT}")
        sys.exit(1)
    if args.files:
        want = {x.strip() for x in args.files.split(",") if x.strip()}
        files = [f for f in files if f.name in want]
        missing = want - {f.name for f in files}
        if missing:
            print("未找到文件:", sorted(missing))
            sys.exit(1)
    elif args.limit:
        files = files[: args.limit]

    print(f"目标库={TARGET_DB}  ROOT={ROOT}")
    print(f"断点目录={STATE}")
    if TARGET_DB == "qcc":
        print("⚠️  正在写入生产库 qcc；实验室请加 --db qcc_lab")
    print(f"待处理 {len(files)} 个文件（补空字段 + 追加联系方式）")
    agg = defaultdict(int)
    t0 = time.time()
    for i, path in enumerate(files, 1):
        st = process_file(path)
        for k in ("rows", "parsed", "enrich", "contacts", "new_co", "bad"):
            agg[k] += int(st.get(k) or 0)
        flag = "SKIP" if st.get("skipped") else "OK"
        print(
            f"[{i}/{len(files)}] {flag} {path.name} "
            f"rows={st.get('rows', 0):,} en+={st.get('enrich', 0)} "
            f"ct+={st.get('contacts', 0)} new={st.get('new_co', 0)} {st.get('seconds', 0)}s"
        )
    print("\n==== 汇总 ====")
    for k, v in agg.items():
        print(f"  {k}: {v:,}")
    print(f"  耗时: {time.time() - t0:.1f}s")
    print(f"  目标库: {TARGET_DB}")


if __name__ == "__main__":
    main()
