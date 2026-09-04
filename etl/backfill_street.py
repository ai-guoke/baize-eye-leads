# -*- coding: utf-8 -*-
"""全量/按省回填 companies.street（从地址解析）。

用 partial Stream Load 只更新 credit_code/street/fill_score，
fill_score+1 保证 sequence 列覆盖旧行。

示例：
  python etl/backfill_street.py
  python etl/backfill_street.py --province 北京市 --province 天津市
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import doris_client as dc
from street_utils import extract_street, load_admin_streets

BATCH = 20000
COLS = ["credit_code", "street", "fill_score"]


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("'", "\\'")


def main() -> None:
    ap = argparse.ArgumentParser(description="回填 companies.street")
    ap.add_argument(
        "--province", action="append", default=[],
        help="仅处理指定省（可多次）；默认全国",
    )
    ap.add_argument(
        "--only-empty", action="store_true",
        help="仅回填 street 为空的企业",
    )
    args = ap.parse_args()

    if not dc.wait_ready(timeout=60):
        print("Doris 未就绪")
        sys.exit(1)

    n_admin = load_admin_streets(force=True)
    print(f"已加载标准街道索引键合计 {n_admin:,}（含直辖市别名）")
    if n_admin < 100:
        print("标准库过少，请先运行：python etl/load_admin_amap.py")
        sys.exit(1)

    where_extra = []
    if args.province:
        vals = ", ".join(f"'{_esc(p)}'" for p in args.province)
        where_extra.append(f"province IN ({vals})")
        print("限定省份:", "、".join(args.province))
    if args.only_empty:
        where_extra.append("(street IS NULL OR street = '')")
    where_sql = (" AND " + " AND ".join(where_extra)) if where_extra else ""

    total = dc.query(f"SELECT COUNT(*) AS n FROM companies WHERE 1=1{where_sql}")[0]["n"]
    print(f"待扫描主体 {total:,}，按 credit_code 分批回填 street…")

    last_code = ""
    done = 0
    filled = 0
    t0 = time.time()
    batch_i = 0

    while True:
        rows = dc.query(
            "SELECT credit_code, province, city, district, address, "
            "IFNULL(fill_score,0) AS fill_score "
            f"FROM companies WHERE credit_code > '{_esc(last_code)}'{where_sql} "
            f"ORDER BY credit_code LIMIT {BATCH}"
        )
        if not rows:
            break

        payload_rows = []
        for r in rows:
            st = extract_street(
                r.get("province") or "",
                r.get("city") or "",
                r.get("district") or "",
                r.get("address") or "",
                allow_regex_fallback=False,
            )
            if st:
                filled += 1
            fs = int(r.get("fill_score") or 0) + 1
            payload_rows.append([r["credit_code"], st or "", fs])

        batch_i += 1
        label_code = "".join(c for c in payload_rows[0][0] if c.isalnum())[:12]
        js = dc.stream_load(
            "companies", COLS, dc.make_csv(payload_rows),
            label=f"street_adm_{batch_i}_{label_code}",
            partial_columns=True,
            max_filter_ratio=0.01,
        )
        done += len(rows)
        last_code = rows[-1]["credit_code"]
        el = time.time() - t0
        rate = done / max(el, 0.1)
        eta = (total - done) / max(rate, 1) / 60
        print(
            f"  [{done:,}/{total:,}] +street {filled:,}  "
            f"{js.get('Status')}  {rate:,.0f} 行/s  ETA {eta:.1f}分",
            flush=True,
        )
        if len(rows) < BATCH:
            break

    print(f"\n完成：扫描 {done:,}，标准街道命中 {filled:,}，耗时 {(time.time()-t0)/60:.1f} 分")
    sample_where = where_sql if where_sql else ""
    sample = dc.query(
        "SELECT street, COUNT(*) AS n FROM companies "
        f"WHERE street IS NOT NULL AND street != ''{sample_where} "
        "GROUP BY street ORDER BY n DESC LIMIT 15"
    )
    print("街道 Top15：")
    for r in sample:
        print(f"  {r['n']:>8,}  {r['street']}")


if __name__ == "__main__":
    main()
