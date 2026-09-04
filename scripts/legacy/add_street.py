# -*- coding: utf-8 -*-
"""从地址拆出街道/镇/乡，写入 companies.street。"""
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DB = Path(__file__).resolve().parent / "output" / "leads.db"

STREET_RE = re.compile(
    r"^[\s]*"
    r"([\u4e00-\u9fff0-9]{1,8}"
    r"(?:街道办事处|街道|镇|乡|苏木))"
)


def _short(name: str) -> str:
    n = (name or "").strip()
    for suf in (
        "特别行政区", "维吾尔自治区", "壮族自治区", "回族自治区",
        "自治区", "省", "市", "地区", "盟",
    ):
        if n.endswith(suf) and len(n) > len(suf):
            return n[: -len(suf)]
    return n


def extract_street(province: str, city: str, district: str, address: str) -> str:
    s = (address or "").strip()
    if not s:
        return ""
    for part in (province, city, district):
        part = (part or "").strip()
        if not part:
            continue
        if s.startswith(part):
            s = s[len(part) :]
            continue
        short = _short(part)
        if short and s.startswith(short):
            s = s[len(short) :]
    s = s.lstrip("·，,、. ")
    m = STREET_RE.match(s)
    if not m:
        return ""
    street = m.group(1)
    if street in {province, city, district, _short(province), _short(city), _short(district)}:
        return ""
    if len(street) <= 1:
        return ""
    return street


def main():
    conn = sqlite3.connect(str(DB))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(companies)")]
    if "street" not in cols:
        print("ALTER TABLE ADD street ...", flush=True)
        conn.execute("ALTER TABLE companies ADD COLUMN street TEXT DEFAULT ''")
        conn.commit()
    else:
        print("street 列已存在，重新填充", flush=True)

    n = 0
    updated = 0
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT id, province, city, district, address FROM companies"
    )
    batch = []
    for rid, prov, city, dist, addr in rows:
        street = extract_street(prov or "", city or "", dist or "", addr or "")
        batch.append((street, rid))
        n += 1
        if street:
            updated += 1
        if len(batch) >= 5000:
            conn.executemany("UPDATE companies SET street=? WHERE id=?", batch)
            conn.commit()
            batch.clear()
            if n % 100000 == 0:
                print(f"  已处理 {n:,}  解析到街道 {updated:,}", flush=True)
    if batch:
        conn.executemany("UPDATE companies SET street=? WHERE id=?", batch)
        conn.commit()
    print(f"完成 共 {n:,}  有街道 {updated:,}", flush=True)

    print("建索引 ix_geo / ix_district ...", flush=True)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_geo ON companies(province, city, district, street)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_district ON companies(district)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_street ON companies(street)")
    conn.commit()

    print("--- 抽样 ---", flush=True)
    for r in conn.execute(
        "SELECT province, city, district, street, address FROM companies "
        "WHERE street != '' LIMIT 8"
    ):
        print(" / ".join(x or "" for x in r[:4]), "|", r[4][:40] if r[4] else "")
    print("--- 覆盖 ---", flush=True)
    print("street", conn.execute("SELECT COUNT(*) FROM companies WHERE street!=''").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    main()
