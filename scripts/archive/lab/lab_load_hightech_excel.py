# -*- coding: utf-8 -*-
"""高新 Excel 证书样本 → 仅写入 qcc_lab.qualifications（含撤销），不碰生产。

用法：
    python etl/lab_load_hightech_excel.py --years 2013,2015,2018 --limit-per 3000
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import app.doris_client as dc
from baize_core import CREDIT_RE, clean, parse_date, tb
from scripts.xlsx_stream import iter_rows, load_shared_strings, sheet_map

LAB = "qcc_lab"
RAW = Path(r"F:\企查查大数据\raw_datasets")
SOURCE = "hightech_excel"
QUAL_COLS = [
    "credit_code", "qual_type", "cert_no", "company_name",
    "cert_year", "issue_date", "valid_to", "revoked",
    "batch_no", "source", "loaded_at",
]
VALID_TO_RE = re.compile(
    r"至\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)


def parse_valid_to(raw: str | None) -> str | None:
    if not raw:
        return None
    m = VALID_TO_RE.search(raw)
    if not m:
        return parse_date(raw)
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if 1800 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def is_revoked(flag: str | None) -> int:
    v = (flag or "").strip()
    if v in ("", "否", "无", "0", "N", "n", "false", "False"):
        return 0
    if v in ("是", "1", "Y", "y", "true", "True", "取消"):
        return 1
    # 其他非空视为取消
    return 1 if v else 0


def excel_files(years: set[str]) -> list[Path]:
    ht = next(RAW.glob("02*"))
    xdir = next(ht.glob("01_Excel*"))
    out = []
    for f in sorted(xdir.rglob("*.xlsx")):
        for y in years:
            if y in f.name:
                out.append(f)
                break
    return out


def load_file(path: Path, limit: int, now: str) -> tuple[list[tuple], dict]:
    stats = {"rows": 0, "ok": 0, "revoked": 0, "bad_credit": 0, "no_cert": 0}
    rows: list[tuple] = []
    with zipfile.ZipFile(path) as z:
        ss = load_shared_strings(z)
        sm = sheet_map(z)
        sheet = list(sm)[0]
        it = iter_rows(z, sm[sheet], ss)
        header = next(it)
        for r in it:
            if stats["ok"] >= limit:
                break
            stats["rows"] += 1
            d = dict(zip(header, r))
            name = clean(d.get("公司名称") or "")
            credit = (clean(d.get("统一信用代码") or "") or "").upper().replace(" ", "")
            cert_no = clean(d.get("证书编号") or "")
            if not name:
                continue
            if not credit or not CREDIT_RE.match(credit):
                stats["bad_credit"] += 1
                continue
            if not cert_no:
                stats["no_cert"] += 1
                cert_no = f"HT-{d.get('认定年度') or 'NA'}-{credit[-8:]}"
            revoked = is_revoked(d.get("是否取消资格"))
            if revoked:
                stats["revoked"] += 1
            year = clean(d.get("认定年度") or "") or None
            issue = parse_date(d.get("发证日期") or "")
            valid_to = parse_valid_to(d.get("证书有效期") or "")
            rows.append((
                credit, "hightech", tb(cert_no, 64), tb(name, 300),
                year, issue, valid_to, revoked,
                path.stem, SOURCE, now,
            ))
            stats["ok"] += 1
    return rows, stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2013,2015,2018", help="逗号分隔年度")
    ap.add_argument("--limit-per", type=int, default=3000)
    args = ap.parse_args()
    years = {y.strip() for y in args.years.split(",") if y.strip()}

    if not dc.wait_ready(timeout=30):
        raise SystemExit("Doris 未就绪")
    print(f"目标库 {LAB}；年度 {sorted(years)}；每文件上限 {args.limit_per}")

    files = excel_files(years)
    if not files:
        raise SystemExit("未找到 Excel 文件")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_rows: list[tuple] = []
    for f in files:
        part, st = load_file(f, args.limit_per, now)
        print(f"  {f.name}: {st}")
        all_rows.extend(part)

    # 同码多证去重：以 (credit, qual_type, cert_no) 为准
    seen = set()
    uniq = []
    for r in all_rows:
        k = (r[0], r[1], r[2])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    print(f"合计写入 {len(uniq)} 行（去重后）")
    if not uniq:
        return
    js = dc.stream_load(
        "qualifications", QUAL_COLS, dc.make_csv(uniq),
        db=LAB, max_filter_ratio=0.05,
    )
    print("stream_load:", js.get("Status"), "loaded", js.get("NumberLoadedRows"))

    # 时间线冒烟：找有撤销记录的企业
    samples = dc.query(
        """
        SELECT credit_code, company_name,
               COUNT(*) AS certs,
               SUM(CASE WHEN revoked=1 THEN 1 ELSE 0 END) AS revoked_n
        FROM qualifications
        WHERE qual_type = 'hightech'
        GROUP BY credit_code, company_name
        HAVING SUM(CASE WHEN revoked=1 THEN 1 ELSE 0 END) > 0
           AND COUNT(*) >= 1
        ORDER BY certs DESC
        LIMIT 5
        """,
        db=LAB,
    )
    print("有撤销记录的样本企业:")
    for s in samples:
        print(" ", s)
        detail = dc.query(
            f"""
            SELECT cert_no, cert_year, issue_date, valid_to, revoked
            FROM qualifications
            WHERE credit_code = '{s['credit_code']}' AND qual_type = 'hightech'
            ORDER BY cert_year, issue_date
            """,
            db=LAB,
        )
        for d in detail:
            print("   ", d)

    total = dc.query("SELECT COUNT(*) c FROM qualifications", db=LAB)[0]["c"]
    rev = dc.query(
        "SELECT COUNT(*) c FROM qualifications WHERE revoked=1", db=LAB
    )[0]["c"]
    print(f"lab qualifications 总计={total} 其中撤销={rev}")
    print("DONE — 未写生产 qcc")


if __name__ == "__main__":
    main()
