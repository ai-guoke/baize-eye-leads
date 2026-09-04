# -*- coding: utf-8 -*-
"""导入后校验：主体数、触达率、倒排检索冒烟。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import doris_client as dc

TARGET_ENTITIES = 12_958_769
TARGET_REACH = 85.07


def main() -> None:
    if not dc.wait_ready(timeout=30):
        print("Doris 未就绪")
        sys.exit(1)

    n = dc.query("SELECT COUNT(*) AS n FROM companies")[0]["n"]
    print(f"companies 主体数：{n:,}  （目标 {TARGET_ENTITIES:,}，差 {n - TARGET_ENTITIES:+,}）")

    reach = dc.query("""
        SELECT
          ROUND(SUM(has_mobile)/COUNT(*)*100, 2) AS mobile_pct,
          ROUND(SUM(has_landline)/COUNT(*)*100, 2) AS landline_pct,
          ROUND(SUM(has_email)/COUNT(*)*100, 2) AS email_pct,
          ROUND(SUM(has_contact)/COUNT(*)*100, 2) AS any_pct
        FROM companies
    """)[0]
    print(f"触达率：手机 {reach['mobile_pct']}%｜座机 {reach['landline_pct']}%"
          f"｜邮箱 {reach['email_pct']}%｜任一 {reach['any_pct']}%  （目标任一≈{TARGET_REACH}%）")

    by_prov = dc.query("""
        SELECT province, COUNT(*) AS n, ROUND(SUM(has_contact)/COUNT(*)*100,1) AS reach
        FROM companies GROUP BY province ORDER BY n DESC
    """)
    print("分省：")
    for r in by_prov:
        print(f"  {r['province']}: {r['n']:,}  触达 {r['reach']}%")

    c = dc.query("SELECT COUNT(*) AS n FROM contacts")[0]["n"]
    print(f"contacts：{c:,}")

    # 全文检索冒烟
    try:
        hit = dc.query("SELECT COUNT(*) AS n FROM companies WHERE scope MATCH_ANY '半导体'")[0]["n"]
        print(f"经营范围 MATCH '半导体'：{hit:,}")
    except Exception as e:
        print(f"全文检索失败：{e}")

    try:
        hit = dc.query("SELECT COUNT(*) AS n FROM companies WHERE company_name MATCH_ANY '科技'")[0]["n"]
        print(f"公司名 MATCH '科技'：{hit:,}")
    except Exception as e:
        print(f"公司名检索失败：{e}")


if __name__ == "__main__":
    main()
