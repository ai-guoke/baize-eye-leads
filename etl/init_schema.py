# -*- coding: utf-8 -*-
"""建库建表 + 健康检查。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import app.doris_client as dc

SCHEMA = Path(__file__).resolve().parent.parent / "sql" / "01_schema.sql"


def main() -> None:
    print("等待 Doris 就绪…")
    if not dc.wait_ready(timeout=600):
        print("Doris 未就绪")
        sys.exit(1)
    print("执行建表脚本…")
    dc.run_script(SCHEMA, db=None)
    print("\n校验：")
    for row in dc.query("SHOW TABLES", db="qcc"):
        print(f"  表 {list(row.values())[0]}")
    for t in ("companies", "contacts", "tags"):
        rows = dc.query(f"SHOW CREATE TABLE {t}", db="qcc")
        if rows:
            ddl = list(rows[0].values())[-1]
            has_inv = "INVERTED" in ddl
            print(f"  {t}: inverted={has_inv}")
    print("建表完成")


if __name__ == "__main__":
    main()
