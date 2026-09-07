# -*- coding: utf-8 -*-
"""全国高新技术企业资质 → qualifications + tags.honors + 主表补空 + contacts。

建模决策（PRD D-04）：不加 companies.is_hightech；筛选走 qualifications。

默认目标库 qcc_lab。写生产必须同时传：
    --db qcc --confirm-prod

用法：
    # 实验室验收（推荐）
    python etl/load_hightech.py --mode all --csv-limit 20000 --excel-years 2013,2015,2018 --excel-limit-per 3000

    # 只跑 CSV / 只跑 Excel
    python etl/load_hightech.py --mode csv --csv-limit 5000
    python etl/load_hightech.py --mode excel --excel-years 2015,2018

    # 生产（需显式确认）
    python etl/load_hightech.py --db qcc --confirm-prod --mode all
"""
from __future__ import annotations

import argparse
import csv
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
from baize_core import (
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

RAW = Path(r"F:\企查查大数据\raw_datasets")
PLATFORM = Path(__file__).resolve().parent.parent
SOURCE_CSV = "hightech2026"
SOURCE_XLS = "hightech_excel"
HONOR_LABEL = "高新技术企业"
COLLECTED_AT = "2025-01-01"
BATCH = 800
VALID_TO_RE = re.compile(r"至\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")

QUAL_COLS = [
    "credit_code", "qual_type", "cert_no", "company_name",
    "cert_year", "issue_date", "valid_to", "revoked",
    "batch_no", "source", "loaded_at",
]
CONTACT_COLS = [
    "credit_code", "contact_type", "contact_value", "source",
    "company_name", "is_primary", "collected_at", "loaded_at",
]
TAG_COLS = [
    "credit_code", "company_name", "honors", "loaded_at",
]
ENRICH_FIELDS = [
    "company_name", "status", "legal_person",
    "capital_wan", "capital_raw", "capital_currency",
    "established", "province", "city", "district",
    "company_type", "industry_l1", "industry_l2",
    "address", "website", "scope",
]

TARGET_DB = "qcc_lab"
STATE_DIR = PLATFORM / "output" / "etl_state" / "hightech_lab"
PENDING_DIR = PLATFORM / "output" / "pending_match"


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
            return [str(i).strip() for i in x if str(i).strip()]
    except Exception:
        pass
    return [s]


def uniq_keep(seq: list[str], limit: int = 40) -> list[str]:
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
        if len(out) >= limit:
            break
    return out


def parse_valid_to(raw: str | None) -> str | None:
    if not raw:
        return None
    m = VALID_TO_RE.search(raw)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1800 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return parse_date(raw)


def is_revoked(flag: str | None) -> int:
    v = (flag or "").strip()
    if v in ("", "否", "无", "0", "N", "n", "false", "False"):
        return 0
    if v in ("是", "1", "Y", "y", "true", "True", "取消"):
        return 1
    return 1 if v else 0


def package_root() -> Path:
    return next(RAW.glob("02*"))


def csv_files() -> list[Path]:
    csv_dir = next(package_root().glob("03_CSV*"))
    files = sorted(csv_dir.glob("*.csv"))
    # 2025 优先
    return sorted(files, key=lambda p: (0 if "2025" in p.name else 1, p.name))


def excel_files(years: set[str]) -> list[Path]:
    xdir = next(package_root().glob("01_Excel*"))
    out = []
    for f in sorted(xdir.rglob("*.xlsx")):
        if any(y in f.name for y in years):
            out.append(f)
    return out


def stream_batches(table: str, cols: list[str], rows: list, prefix: str,
                   partial: bool | None = None) -> int:
    if not rows:
        return 0
    if partial is None:
        partial = table in ("companies", "tags")
    loaded = 0
    for i in range(0, len(rows), 5000):
        part = rows[i:i + 5000]
        label = f"{prefix}_{hashlib.md5(f'{i}{time.time()}'.encode()).hexdigest()[:12]}"
        js = dc.stream_load(
            table, cols, dc.make_csv(part),
            label=label, db=TARGET_DB, max_filter_ratio=0.05,
            partial_columns=partial,
        )
        loaded += int(js.get("NumberLoadedRows") or 0)
        print(f"    {table} +{js.get('NumberLoadedRows')} ({js.get('Status')})")
    return loaded


def write_pending(rows: list[dict], stamp: str) -> Path | None:
    if not rows:
        return None
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = PENDING_DIR / f"hightech_pending_{stamp}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=["name", "tax_id", "province", "reason", "source_file"])
        w.writeheader()
        w.writerows(rows)
    return path


def refresh_flags(codes: list[str]) -> None:
    """只更新已存在于 companies 的码，避免 partial 新建缺 NOT NULL 列。"""
    if not codes:
        return
    for i in range(0, len(codes), BATCH):
        part = codes[i:i + BATCH]
        inlist = ",".join(f"'{c}'" for c in part)
        existing = {
            r["credit_code"] for r in dc.query(
                f"SELECT credit_code FROM companies WHERE credit_code IN ({inlist})",
                db=TARGET_DB,
            )
        }
        part = [c for c in part if c in existing]
        if not part:
            continue
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
        out = []
        for code in part:
            s = stats.get(code)
            if not s:
                continue
            m, l, e, n = int(s["m"] or 0), int(s["l"] or 0), int(s["e"] or 0), int(s["n"] or 0)
            out.append([
                code, 1 if m else 0, 1 if l else 0, 1 if e else 0, 1 if n else 0,
                m, e, old_fs.get(code, 0) + 1,
            ])
        if out:
            dc.stream_load(
                "companies",
                ["credit_code", "has_mobile", "has_landline", "has_email", "has_contact",
                 "mobile_count", "email_count", "fill_score"],
                dc.make_csv(out),
                label=f"ht_flags_{int(time.time())}_{i}",
                db=TARGET_DB, max_filter_ratio=0.05, partial_columns=True,
            )


def merge_honors(codes: list[str], names: dict[str, str], now: str) -> int:
    """给已有/新建 tags 追加「高新技术企业」。"""
    if not codes:
        return 0
    tag_rows = []
    for i in range(0, len(codes), BATCH):
        part = codes[i:i + BATCH]
        inlist = ",".join(f"'{c}'" for c in part)
        existing = {
            r["credit_code"]: r for r in dc.query(
                f"SELECT credit_code, company_name, honors FROM tags "
                f"WHERE credit_code IN ({inlist})",
                db=TARGET_DB,
            )
        }
        for code in part:
            old = existing.get(code) or {}
            honors = uniq_keep(parse_arr(old.get("honors")) + [HONOR_LABEL], limit=40)
            tag_rows.append([
                code,
                tb(old.get("company_name") or names.get(code) or "", 300),
                arr_literal(honors),
                now,
            ])
    return stream_batches("tags", TAG_COLS, tag_rows, "ht_tags")


def load_csv(limit: int, do_enrich: bool) -> dict:
    files = csv_files()
    if not files:
        raise SystemExit("未找到高新 CSV")
    # 默认只吃最新一份（2025）；limit=0 表示该文件全量
    path = files[0]
    print(f"\n== CSV == {path.name}  limit={limit or 'ALL'}")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    year = "2025" if "2025" in path.name else "2024"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    quals, contacts, pending = [], [], []
    enrich_src: dict[str, dict] = {}
    names: dict[str, str] = {}
    matched = scanned = 0

    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            if limit and scanned >= limit:
                break
            scanned += 1
            name = clean(row.get("企业名称") or "")
            tax = (clean(row.get("纳税人识别号") or "") or "").upper().replace(" ", "")
            if not name:
                continue
            if not CREDIT_RE.match(tax):
                pending.append({
                    "name": name,
                    "tax_id": tax or "",
                    "province": clean(row.get("所属省份") or "") or "",
                    "reason": "tax_id_not_18",
                    "source_file": path.name,
                })
                continue

            matched += 1
            names[tax] = name
            cert_no = f"HT-{year}-{tax[-8:]}"
            quals.append((
                tax, "hightech", cert_no, tb(name, 300),
                year, None, None, 0,
                path.stem, SOURCE_CSV, now,
            ))

            phone = clean(row.get("联系电话") or "") or ""
            email = clean(row.get("联系邮箱") or "") or ""
            for p in split_multi(phone):
                digits = re.sub(r"\D", "", p)
                if MOBILE_RE.match(digits):
                    contacts.append((tax, "mobile", digits, SOURCE_CSV, tb(name, 300), 0, COLLECTED_AT, now))
                else:
                    ll = norm_landline(p)
                    if ll:
                        contacts.append((tax, "landline", ll, SOURCE_CSV, tb(name, 300), 0, COLLECTED_AT, now))
            for e in split_multi(email):
                e2 = e.lower()
                if EMAIL_RE.match(e2):
                    contacts.append((tax, "email", e2, SOURCE_CSV, tb(name, 300), 0, COLLECTED_AT, now))

            if do_enrich:
                status_raw = clean(row.get("企业状态") or "") or ""
                status = STATUS_MAP.get(status_raw, status_raw) or None
                prov = clean(row.get("所属省份") or "")
                if prov:
                    prov = PROVINCE_MAP.get(prov, prov)
                cap_wan, cap_cur = parse_capital(clean(row.get("注册资本") or "") or "", name)
                ind = clean(row.get("所属行业门类") or "")
                ind2 = clean(row.get("所属行业名称") or "")
                enrich_src[tax] = {
                    "credit_code": tax,
                    "company_name": tb(name, 300),
                    "status": status,
                    "legal_person": tb(clean(row.get("法定代表人") or ""), 128),
                    "capital_wan": cap_wan,
                    "capital_raw": tb(clean(row.get("注册资本") or ""), 64),
                    "capital_currency": cap_cur,
                    "established": parse_date(clean(row.get("成立日期") or "") or ""),
                    "province": prov,
                    "city": clean(row.get("所属城市") or ""),
                    "district": clean(row.get("所属区县") or ""),
                    "company_type": tb(clean(row.get("企业类型") or ""), 128),
                    "industry_l1": tb(ind, 64),
                    "industry_l2": tb(ind2, 64),
                    "address": tb(clean(row.get("企业地址") or ""), 500),
                    "website": tb(clean(row.get("网址") or ""), 300),
                    "scope": clean(row.get("经营范围") or ""),
                }

    print(f"  scanned={scanned} matched={matched} pending={len(pending)} "
          f"quals={len(quals)} contacts={len(contacts)}")
    pend_path = write_pending(pending, stamp)
    if pend_path:
        print(f"  pending → {pend_path}")

    # quals 去重
    uniq_q = list({(r[0], r[1], r[2]): r for r in quals}.values())
    n_q = stream_batches("qualifications", QUAL_COLS, uniq_q, "ht_csv_q")

    n_en = 0
    if do_enrich and enrich_src:
        n_en = enrich_companies(enrich_src, now)

    uniq_c = list({(r[0], r[1], r[2]): r for r in contacts}.values())
    n_c = stream_batches("contacts", CONTACT_COLS, uniq_c, "ht_csv_ct")
    if uniq_c:
        refresh_flags(sorted({r[0] for r in uniq_c}))

    n_tag = merge_honors(sorted(names.keys()), names, now)
    return {
        "csv_file": path.name, "matched": matched, "pending": len(pending),
        "qualifications": n_q, "contacts": n_c, "enrich": n_en, "tags": n_tag,
    }


def enrich_companies(src_map: dict[str, dict], now: str) -> int:
    codes = list(src_map.keys())
    existing: dict[str, dict] = {}
    for i in range(0, len(codes), BATCH):
        part = codes[i:i + BATCH]
        inlist = ",".join(f"'{c}'" for c in part)
        cols = ",".join(["credit_code", "fill_score"] + ENRICH_FIELDS)
        for r in dc.query(
            f"SELECT {cols} FROM companies WHERE credit_code IN ({inlist})",
            db=TARGET_DB,
        ):
            existing[r["credit_code"]] = r

    buckets: dict[tuple[str, ...], list[list]] = defaultdict(list)
    for code, src in src_map.items():
        old = existing.get(code)
        if not old:
            patch = {"credit_code": code, "fill_score": 20, "loaded_at": now,
                     "src_group": "hightech", "src_file": SOURCE_CSV}
            for f in ENRICH_FIELDS:
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
            if old.get(f) in (None, ""):
                patch[f] = new_v
                filled += 1
        if filled:
            patch["fill_score"] = int(old.get("fill_score") or 0) + filled
            keys = tuple(["credit_code"] + [k for k in patch if k != "credit_code"])
            buckets[keys].append([patch[k] for k in keys])

    loaded = 0
    for i, (keys, rows) in enumerate(buckets.items()):
        loaded += stream_batches("companies", list(keys), rows, f"ht_en_{i}")
    return loaded


def load_excel(years: set[str], limit_per: int) -> dict:
    files = excel_files(years)
    if not files:
        raise SystemExit(f"未找到 Excel：years={sorted(years)}")
    print(f"\n== Excel == years={sorted(years)} files={len(files)} limit_per={limit_per or 'ALL'}")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_rows: list[tuple] = []
    pending: list[dict] = []
    names: dict[str, str] = {}
    agg = defaultdict(int)

    for path in files:
        st = {"rows": 0, "ok": 0, "revoked": 0, "bad_credit": 0}
        with zipfile.ZipFile(path) as z:
            ss = load_shared_strings(z)
            sm = sheet_map(z)
            it = iter_rows(z, next(iter(sm.values())), ss)
            header = next(it)
            for r in it:
                if limit_per and st["ok"] >= limit_per:
                    break
                st["rows"] += 1
                d = dict(zip(header, r))
                name = clean(d.get("公司名称") or "")
                credit = (clean(d.get("统一信用代码") or "") or "").upper().replace(" ", "")
                cert_no = clean(d.get("证书编号") or "")
                if not name:
                    continue
                if not credit or not CREDIT_RE.match(credit):
                    st["bad_credit"] += 1
                    pending.append({
                        "name": name,
                        "tax_id": credit or "",
                        "province": clean(d.get("所属省份") or "") or "",
                        "reason": "credit_not_18",
                        "source_file": path.name,
                    })
                    continue
                if not cert_no:
                    cert_no = f"HT-{d.get('认定年度') or 'NA'}-{credit[-8:]}"
                revoked = is_revoked(d.get("是否取消资格"))
                if revoked:
                    st["revoked"] += 1
                year = clean(d.get("认定年度") or "") or None
                all_rows.append((
                    credit, "hightech", tb(cert_no, 64), tb(name, 300),
                    year, parse_date(d.get("发证日期") or ""),
                    parse_valid_to(d.get("证书有效期") or ""), revoked,
                    path.stem, SOURCE_XLS, now,
                ))
                names[credit] = name
                st["ok"] += 1
        print(f"  {path.name}: {dict(st)}")
        for k, v in st.items():
            agg[k] += v

    uniq = list({(r[0], r[1], r[2]): r for r in all_rows}.values())
    n_q = stream_batches("qualifications", QUAL_COLS, uniq, "ht_xls_q")
    n_tag = merge_honors(sorted(names.keys()), names, now)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pend_path = write_pending(pending, f"excel_{stamp}")
    if pend_path:
        print(f"  pending → {pend_path}")

    # 时间线冒烟
    samples = dc.query(
        """
        SELECT credit_code, company_name, COUNT(*) AS certs,
               SUM(CASE WHEN revoked=1 THEN 1 ELSE 0 END) AS revoked_n
        FROM qualifications
        WHERE qual_type='hightech'
        GROUP BY credit_code, company_name
        HAVING SUM(CASE WHEN revoked=1 THEN 1 ELSE 0 END) > 0
        ORDER BY certs DESC LIMIT 3
        """,
        db=TARGET_DB,
    )
    print("  撤销时间线样本:")
    for s in samples:
        print("   ", s)

    return {
        "excel_files": len(files), "qualifications": n_q, "tags": n_tag,
        "pending": len(pending), "revoked_rows": agg.get("revoked", 0),
    }


def smoke_join() -> None:
    t0 = time.time()
    # 优先跨库：生产主体 × 目标库资质（目标是 lab 时最有意义）
    if TARGET_DB == "qcc_lab":
        sql = """
        SELECT COUNT(*) AS n FROM qcc.companies c
        WHERE c.province='浙江省' AND c.has_mobile=1 AND c.status='存续'
          AND EXISTS (
            SELECT 1 FROM qcc_lab.qualifications q
            WHERE q.credit_code=c.credit_code AND q.qual_type='hightech' AND q.revoked=0
          )
        """
        r = dc.query(sql, db=None)
    else:
        r = dc.query(
            """
            SELECT COUNT(*) AS n FROM companies c
            WHERE province='浙江省' AND has_mobile=1 AND status='存续'
              AND EXISTS (
                SELECT 1 FROM qualifications q
                WHERE q.credit_code=c.credit_code AND q.qual_type='hightech' AND q.revoked=0
              )
            """,
            db=TARGET_DB,
        )
    ms = (time.time() - t0) * 1000
    print(f"\n筛选「浙江+有效高新+有手机」: n={r[0]['n'] if r else None}  {ms:.0f}ms")


def ensure_target_ready() -> None:
    tables = {list(r.values())[0] for r in dc.query("SHOW TABLES", db=TARGET_DB)}
    if "qualifications" not in tables:
        if TARGET_DB == "qcc_lab":
            raise SystemExit("qcc_lab 缺少 qualifications，请先: python etl/lab_bootstrap.py --init-schema")
        raise SystemExit(
            "生产库缺少 qualifications。请先执行: "
            "python -c \"import app.doris_client as dc; dc.run_script('sql/05_qualifications.sql', db='qcc')\""
        )


def main() -> None:
    global TARGET_DB, STATE_DIR
    ap = argparse.ArgumentParser(description="高新资质入库（默认 qcc_lab）")
    ap.add_argument("--db", default="qcc_lab", choices=["qcc_lab", "qcc"])
    ap.add_argument("--confirm-prod", action="store_true",
                    help="写生产库 qcc 时必须同时打开")
    ap.add_argument("--mode", default="all", choices=["csv", "excel", "all"])
    ap.add_argument("--csv-limit", type=int, default=20000,
                    help="CSV 最多读取行数；0=全文件")
    ap.add_argument("--excel-years", default="2013,2015,2018")
    ap.add_argument("--excel-limit-per", type=int, default=3000,
                    help="每个 Excel 最多写入行数；0=全文件")
    ap.add_argument("--skip-enrich", action="store_true",
                    help="跳过主表补空（仍写资质/联系方式/荣誉）")
    args = ap.parse_args()

    TARGET_DB = args.db
    if TARGET_DB == "qcc" and not args.confirm_prod:
        raise SystemExit("拒绝写生产：请同时传 --db qcc --confirm-prod")
    STATE_DIR = PLATFORM / "output" / "etl_state" / (
        "hightech_lab" if TARGET_DB == "qcc_lab" else "hightech"
    )
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if not dc.wait_ready(timeout=60):
        raise SystemExit("Doris 未就绪")
    ensure_target_ready()

    print(f"目标库={TARGET_DB}  mode={args.mode}  skip_enrich={args.skip_enrich}")
    if TARGET_DB == "qcc":
        print("⚠️  正在写入生产库 qcc")

    summary = {}
    t0 = time.time()
    if args.mode in ("csv", "all"):
        summary["csv"] = load_csv(args.csv_limit, do_enrich=not args.skip_enrich)
    if args.mode in ("excel", "all"):
        years = {y.strip() for y in args.excel_years.split(",") if y.strip()}
        summary["excel"] = load_excel(years, args.excel_limit_per)

    smoke_join()
    total_q = dc.query("SELECT COUNT(*) c FROM qualifications", db=TARGET_DB)[0]["c"]
    rev = dc.query(
        "SELECT COUNT(*) c FROM qualifications WHERE revoked=1", db=TARGET_DB
    )[0]["c"]
    print(f"\n==== 汇总 ({time.time()-t0:.1f}s) ====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"qualifications 总计={total_q} 撤销={rev}")
    print(f"目标库={TARGET_DB} — 完成")


if __name__ == "__main__":
    main()
