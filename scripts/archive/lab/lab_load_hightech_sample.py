# -*- coding: utf-8 -*-
"""高新样本 → 仅写入 qcc_lab.qualifications / tags，不碰生产 qcc。

用法：
    python etl/lab_load_hightech_sample.py --limit 5000
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import app.doris_client as dc
from etl.load_jzh import CREDIT_RE, EMAIL_RE, MOBILE_RE, clean, is_empty, norm_landline, split_multi, tb

LAB = "qcc_lab"
RAW = Path(r"F:\企查查大数据\raw_datasets")
SOURCE = "hightech2026"
COLLECTED_AT = "2025-01-01"
CREDIT_RE18 = re.compile(r"^[0-9A-Z]{18}$")

QUAL_COLS = [
    "credit_code", "qual_type", "cert_no", "company_name",
    "cert_year", "issue_date", "valid_to", "revoked",
    "batch_no", "source", "loaded_at",
]
CONTACT_COLS = [
    "credit_code", "contact_type", "contact_value", "source",
    "company_name", "is_primary", "collected_at", "loaded_at",
]


def find_csv() -> Path:
    ht = next(RAW.glob("02*"))
    csv_dir = next(ht.glob("03_CSV*"))
    # 优先 2025
    files = sorted(csv_dir.glob("*.csv"))
    for f in files:
        if "2025" in f.name:
            return f
    return files[0]


def load_sample(limit: int) -> None:
    if LAB != "qcc_lab":
        raise SystemExit("拒绝：只能写 qcc_lab")
    path = find_csv()
    print(f"源文件: {path}")
    print(f"目标库: {LAB}（生产 qcc 只读）")

    quals: list[tuple] = []
    contacts: list[tuple] = []
    pending = 0
    matched = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    year = "2025" if "2025" in path.name else "2024"

    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for i, row in enumerate(reader):
            if i >= limit:
                break
            name = clean(row.get("企业名称") or "")
            tax = (clean(row.get("纳税人识别号") or "") or "").upper().replace(" ", "")
            if not name:
                continue
            if not CREDIT_RE18.match(tax):
                pending += 1
                continue

            cert_no = f"HT-{year}-{tax[-8:]}"
            quals.append((
                tax, "hightech", cert_no, tb(name, 300),
                year, None, None, 0,
                path.stem, SOURCE, now,
            ))
            matched += 1

            phone = clean(row.get("联系电话") or "")
            email = clean(row.get("联系邮箱") or "")
            for p in split_multi(phone or ""):
                digits = re.sub(r"\D", "", p)
                if MOBILE_RE.match(digits):
                    contacts.append((tax, "mobile", digits, SOURCE, tb(name, 300), 0, COLLECTED_AT, now))
                else:
                    ll = norm_landline(p)
                    if ll:
                        contacts.append((tax, "landline", ll, SOURCE, tb(name, 300), 0, COLLECTED_AT, now))
            for e in split_multi(email or ""):
                e = e.lower()
                if EMAIL_RE.match(e):
                    contacts.append((tax, "email", e, SOURCE, tb(name, 300), 0, COLLECTED_AT, now))

    print(f"解析: matched={matched} pending_bad_tax={pending} contacts={len(contacts)}")

    if quals:
        payload = dc.make_csv(quals)
        js = dc.stream_load("qualifications", QUAL_COLS, payload, db=LAB, max_filter_ratio=0.05)
        print("qualifications:", js.get("Status"), "loaded", js.get("NumberLoadedRows"))

    if contacts:
        # 去重
        seen = set()
        uniq = []
        for r in contacts:
            k = (r[0], r[1], r[2])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(r)
        payload = dc.make_csv(uniq)
        js = dc.stream_load("contacts", CONTACT_COLS, payload, db=LAB, max_filter_ratio=0.05)
        print("contacts append:", js.get("Status"), "loaded", js.get("NumberLoadedRows"))

    # 命中 lab 已有主体的比例（模拟「高新筛选 JOIN」）
    hit = dc.query(
        """
        SELECT COUNT(DISTINCT q.credit_code) AS hit
        FROM qualifications q
        INNER JOIN companies c ON q.credit_code = c.credit_code
        WHERE q.qual_type = 'hightech' AND q.revoked = 0
        """,
        db=LAB,
    )
    total_q = dc.query("SELECT COUNT(*) c FROM qualifications", db=LAB)[0]["c"]
    print(f"lab qualifications={total_q}, join 命中已有主体={hit[0]['hit']}")

    # 浙江 + 高新 + 有手机（建模查询）
    t0 = datetime.now()
    rows = dc.query(
        """
        SELECT COUNT(*) AS n
        FROM companies c
        WHERE c.province = '浙江省'
          AND c.has_mobile = 1
          AND EXISTS (
            SELECT 1 FROM qualifications q
            WHERE q.credit_code = c.credit_code
              AND q.qual_type = 'hightech'
              AND q.revoked = 0
          )
        """,
        db=LAB,
    )
    ms = (datetime.now() - t0).total_seconds() * 1000
    print(f"筛选「浙江+高新+有手机」: n={rows[0]['n']} 耗时 {ms:.0f}ms（lab 小样本，仅验 SQL 形态）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()
    if not dc.wait_ready(timeout=30):
        raise SystemExit("Doris 未就绪")
    load_sample(args.limit)


if __name__ == "__main__":
    main()
