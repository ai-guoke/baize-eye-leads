# -*- coding: utf-8 -*-
"""实验室库 qcc_lab 引导：建库建表 + 从生产只读抽样，不写 qcc。

用法：
    python etl/lab_bootstrap.py --init-schema          # 建 qcc_lab + 四张表
    python etl/lab_bootstrap.py --seed                 # 从 qcc 只读抽 S4/S5
    python etl/lab_bootstrap.py --probe-hightech       # 只看高新 CSV 表头与样例
    python etl/lab_bootstrap.py --all                  # init + seed + probe
    python etl/lab_bootstrap.py --drop                 # DROP DATABASE qcc_lab

铁律：本脚本对 qcc 只有 SELECT；所有写入目标库固定为 qcc_lab。
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import app.doris_client as dc

LAB = "qcc_lab"
PROD = "qcc"
PLATFORM = Path(__file__).resolve().parent.parent
SCHEMA = PLATFORM / "sql" / "lab" / "01_schema_lab.sql"
RAW = Path(r"F:\企查查大数据\raw_datasets")


def assert_lab_only(target: str) -> None:
    if target != LAB:
        raise SystemExit(f"拒绝：目标库必须是 {LAB}，收到 {target!r}")


def init_schema() -> None:
    assert_lab_only(LAB)
    print(f"== 初始化 {LAB} ==")
    if not dc.wait_ready(timeout=60):
        raise SystemExit("Doris 未就绪")
    dc.run_script(SCHEMA, db=None)
    tables = dc.query("SHOW TABLES", db=LAB)
    print("表：", [list(r.values())[0] for r in tables])


def drop_lab() -> None:
    print(f"== DROP DATABASE {LAB} ==")
    dc.execute(f"DROP DATABASE IF EXISTS {LAB}", db=None)
    print("已删除。生产库 qcc 未动。")


def _count(table: str, db: str = LAB) -> int:
    r = dc.query(f"SELECT COUNT(*) AS c FROM {table}", db=db)
    return int(r[0]["c"]) if r else 0


# 显式列序 = lab 表定义；禁止 SELECT *（生产 ALTER 后列序与 lab 不一致）
COMPANY_COLS = [
    "credit_code", "company_name", "status", "legal_person", "scale",
    "capital_wan", "capital_raw", "capital_currency", "capital_paid_wan",
    "established", "approved", "established_year", "company_age", "term_raw",
    "province", "city", "district", "street",
    "company_type", "industry_l1", "industry_l2", "industry_l3", "industry_l4",
    "insured_count", "insured_year", "former_name", "en_name",
    "tax_id", "reg_no", "org_code",
    "address", "address_report", "address_mail", "website",
    "registrar", "taxpayer_qual", "intro", "report_year", "scope",
    "has_mobile", "has_landline", "has_email", "has_contact",
    "mobile_count", "email_count", "fill_score",
    "src_group", "src_file", "loaded_at",
    "lng", "lat", "geo_precision", "geocoded_at",
]
CONTACT_COLS = [
    "credit_code", "contact_type", "contact_value", "source",
    "company_name", "is_primary", "collected_at", "loaded_at",
]
TAG_COLS = [
    "credit_code", "company_name", "chain_industries", "chain_nodes",
    "chain_positions", "relatedness", "honors", "financing_raw",
    "tag_finance", "tag_fit", "lead_score", "score_reach",
    "score_attract", "score_fit", "score_pay", "loaded_at",
]


def _cols(names: list[str], alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return ", ".join(f"{p}{c}" for c in names)


def seed_from_prod() -> None:
    """从生产只读抽样写入 lab。跨库 INSERT SELECT，不碰生产表结构。"""
    assert_lab_only(LAB)
    print("== 从 qcc 只读抽样 → qcc_lab ==")
    t0 = datetime.now()
    # 清空 lab 业务表，保证可重复跑
    for t in ("contacts", "tags", "qualifications", "companies"):
        dc.execute(f"TRUNCATE TABLE {LAB}.{t}", db=None)
    cc = _cols(COMPANY_COLS)

    # S4 稀疏：无简介，用信用代码尾字符分散抽样（避免全表 ORDER BY）
    print("  S4 稀疏样本（无 intro）…")
    dc.execute(
        f"""
        INSERT INTO {LAB}.companies ({cc})
        SELECT {cc}
        FROM {PROD}.companies
        WHERE IFNULL(intro, '') = ''
          AND status = '存续'
          AND credit_code LIKE '%0'
          AND credit_code LIKE '9%'
        LIMIT 1200
        """,
        db=None,
    )
    print(f"    companies 现 {_count('companies')} 行")

    # S5 富样本：有简介 + 有 tags
    print("  S5 富样本（有 intro + tags）…")
    dc.execute(
        f"""
        INSERT INTO {LAB}.companies ({cc})
        SELECT {_cols(COMPANY_COLS, 'c')}
        FROM {PROD}.companies c
        INNER JOIN {PROD}.tags t ON c.credit_code = t.credit_code
        WHERE IFNULL(c.intro, '') != ''
          AND array_size(t.chain_industries) > 0
        LIMIT 300
        """,
        db=None,
    )
    print(f"    companies 现 {_count('companies')} 行")

    # 再补一批浙江有手机的，方便后面测「省 + 触达」筛选
    print("  浙江有手机补充样本…")
    dc.execute(
        f"""
        INSERT INTO {LAB}.companies ({cc})
        SELECT {cc}
        FROM {PROD}.companies
        WHERE province = '浙江省'
          AND has_mobile = 1
          AND status = '存续'
          AND credit_code LIKE '%1'
        LIMIT 2000
        """,
        db=None,
    )
    print(f"    companies 现 {_count('companies')} 行")

    # 同步 contacts / tags（只拉 lab 里已有的码）
    print("  同步 contacts…")
    dc.execute(
        f"""
        INSERT INTO {LAB}.contacts ({_cols(CONTACT_COLS)})
        SELECT {_cols(CONTACT_COLS, 'ct')}
        FROM {PROD}.contacts ct
        WHERE ct.credit_code IN (SELECT credit_code FROM {LAB}.companies)
        """,
        db=None,
    )
    print(f"    contacts {_count('contacts')} 行")

    print("  同步 tags…")
    dc.execute(
        f"""
        INSERT INTO {LAB}.tags ({_cols(TAG_COLS)})
        SELECT {_cols(TAG_COLS, 't')}
        FROM {PROD}.tags t
        WHERE t.credit_code IN (SELECT credit_code FROM {LAB}.companies)
        """,
        db=None,
    )
    print(f"    tags {_count('tags')} 行")

    # 概况
    stats = dc.query(
        f"""
        SELECT
          COUNT(*) AS n,
          SUM(CASE WHEN IFNULL(intro,'')!='' THEN 1 ELSE 0 END) AS with_intro,
          SUM(CASE WHEN has_mobile=1 THEN 1 ELSE 0 END) AS with_mobile,
          SUM(CASE WHEN province='浙江省' THEN 1 ELSE 0 END) AS zj
        FROM companies
        """,
        db=LAB,
    )
    print("  lab 概况:", stats[0] if stats else None)
    print(f"  耗时 {(datetime.now()-t0).total_seconds():.1f}s")
    print("  生产库 qcc 未写入。")


def probe_hightech(limit_files: int = 2, peek_rows: int = 5) -> None:
    """只读探测高新 CSV / Excel 形态，不入库。"""
    ht = next(RAW.glob("02*"), None)
    if not ht:
        print("未找到 02 高新包")
        return
    print(f"== 高新包: {ht.name} ==")
    csv_dir = next(ht.glob("03_CSV*"), None)
    xls_dir = next(ht.glob("01_Excel*"), None)
    print("  CSV 目录:", csv_dir.name if csv_dir else None)
    print("  Excel 目录:", xls_dir.name if xls_dir else None)

    if csv_dir:
        files = sorted(csv_dir.rglob("*.csv"))[:limit_files]
        print(f"  CSV 文件数(截断展示): {len(list(csv_dir.rglob('*.csv')))}，探 {len(files)} 个")
        for f in files:
            print(f"\n  --- {f.relative_to(ht)} ---")
            for enc in ("utf-8-sig", "gb18030", "utf-8"):
                try:
                    with f.open("r", encoding=enc, newline="") as fp:
                        reader = csv.reader(fp)
                        header = next(reader)
                        print(f"  encoding={enc} 列数={len(header)}")
                        print("  表头:", header[:25], ("..." if len(header) > 25 else ""))
                        for i, row in zip(range(peek_rows), reader):
                            # 只打关键列位置：税号/名称/省
                            preview = {header[j]: row[j] for j in range(min(8, len(header), len(row)))}
                            print(f"    row{i}: {preview}")
                    break
                except Exception as e:
                    print(f"  {enc} 失败: {type(e).__name__}: {e}")

    if xls_dir:
        xls = sorted(xls_dir.rglob("*.xlsx"))[:3]
        print(f"\n  Excel 样例文件: {[x.name for x in xls]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="qcc_lab 引导（不写生产库）")
    ap.add_argument("--init-schema", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--probe-hightech", action="store_true")
    ap.add_argument("--drop", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if not any([args.init_schema, args.seed, args.probe_hightech, args.drop, args.all]):
        ap.print_help()
        return

    if args.drop:
        drop_lab()
        return
    if args.all or args.init_schema:
        init_schema()
    if args.all or args.seed:
        seed_from_prod()
    if args.all or args.probe_hightech:
        probe_hightech()


if __name__ == "__main__":
    main()
