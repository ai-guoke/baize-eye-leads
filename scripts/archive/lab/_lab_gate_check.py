# -*- coding: utf-8 -*-
"""D-00 闸门：补空正确性 + 跨库只读 JOIN 性能。只读 qcc，只写 qcc_lab。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app import doris_client as d
from etl.lab_bootstrap import COMPANY_COLS, _cols, LAB, PROD

PROTECTED = ["company_name", "status", "legal_person", "credit_code"]


def snapshot_overlap(limit: int = 80) -> list[str]:
    """lab 与 prod 重叠的码（用于补空回归）。"""
    rows = d.query(
        f"""
        SELECT l.credit_code
        FROM {LAB}.companies l
        INNER JOIN {PROD}.companies p ON l.credit_code = p.credit_code
        WHERE IFNULL(p.legal_person,'') != ''
        LIMIT {limit}
        """,
        db=None,
    )
    return [r["credit_code"] for r in rows]


def pull_zhoushan_seed() -> None:
    """把浙江舟山存续主体拉进 lab，作为补空对照底。"""
    cc = _cols(COMPANY_COLS)
    print("seed 浙江舟山 from prod → lab …")
    d.execute(
        f"""
        INSERT INTO {LAB}.companies ({cc})
        SELECT {cc}
        FROM {PROD}.companies
        WHERE province = '浙江省' AND city LIKE '%舟山%' AND status = '存续'
        LIMIT 5000
        """,
        db=None,
    )
    n = d.query("SELECT COUNT(*) c FROM companies WHERE city LIKE '%舟山%'", db=LAB)[0]["c"]
    print("  lab 舟山 rows:", n)


def check_partial_preserve(codes: list[str]) -> dict:
    if not codes:
        return {"checked": 0, "violations": []}
    inlist = ",".join(f"'{c}'" for c in codes)
    lab = {
        r["credit_code"]: r
        for r in d.query(
            f"SELECT credit_code, company_name, status, legal_person FROM companies "
            f"WHERE credit_code IN ({inlist})",
            db=LAB,
        )
    }
    prod = {
        r["credit_code"]: r
        for r in d.query(
            f"SELECT credit_code, company_name, status, legal_person FROM companies "
            f"WHERE credit_code IN ({inlist})",
            db=PROD,
        )
    }
    violations = []
    for code in codes:
        a, b = lab.get(code), prod.get(code)
        if not a or not b:
            continue
        for f in ("company_name", "status", "legal_person"):
            pv, lv = b.get(f), a.get(f)
            if pv not in (None, "") and lv not in (None, "") and str(pv) != str(lv):
                # enrich 不应改非空；若 lab 原先来自 prod 同值，enrich 后仍应等于 prod
                violations.append({"credit_code": code, "field": f, "prod": pv, "lab": lv})
    return {"checked": len(codes), "violations": violations[:10], "n_violations": len(violations)}


def bench_cross_db_join(rounds: int = 5) -> dict:
    """生产 companies × lab qualifications，只读 qcc。"""
    sql = f"""
    SELECT COUNT(*) AS n
    FROM {PROD}.companies c
    WHERE c.province = '浙江省'
      AND c.has_mobile = 1
      AND c.status = '存续'
      AND EXISTS (
        SELECT 1 FROM {LAB}.qualifications q
        WHERE q.credit_code = c.credit_code
          AND q.qual_type = 'hightech'
          AND q.revoked = 0
      )
    """
    times = []
    n = None
    for _ in range(rounds):
        t0 = time.time()
        r = d.query(sql, db=None)
        times.append((time.time() - t0) * 1000)
        n = r[0]["n"] if r else None
    times.sort()
    return {
        "n": n,
        "ms": times,
        "p50_ms": times[len(times) // 2],
        "p95_ms": times[-1],
        "pass": times[-1] < 1000,
    }


def main() -> None:
    print("== D-00 gate checks ==")
    pull_zhoushan_seed()
    codes = snapshot_overlap(100)
    print("overlap codes for preserve check:", len(codes))
    before = {
        r["credit_code"]: r
        for r in d.query(
            "SELECT credit_code, company_name, status, legal_person, website, scope "
            "FROM companies WHERE city LIKE '%舟山%' LIMIT 200",
            db=LAB,
        )
    }
    print("lab 舟山 snapshot:", len(before))

    join = bench_cross_db_join()
    print("cross-db JOIN 浙江+高新+手机:", join)

    # 空区块判定（数据层）
    sparse = d.query(
        """
        SELECT COUNT(*) n FROM companies c
        WHERE IFNULL(c.intro,'') = ''
          AND c.credit_code NOT IN (SELECT credit_code FROM tags)
        """,
        db=LAB,
    )[0]["n"]
    rich = d.query(
        """
        SELECT COUNT(*) n FROM companies c
        INNER JOIN tags t ON c.credit_code = t.credit_code
        WHERE IFNULL(c.intro,'') != ''
        """,
        db=LAB,
    )[0]["n"]
    print(f"display samples sparse={sparse} rich={rich}")

    prod_n = d.query("SELECT COUNT(*) c FROM companies", db=PROD)[0]["c"]
    print("prod companies still:", prod_n)
    print("DONE")


if __name__ == "__main__":
    main()
