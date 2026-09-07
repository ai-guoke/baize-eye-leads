# -*- coding: utf-8 -*-
"""把 lab 中高新码对应的生产主体拉进 qcc_lab，并跑筛选冒烟。只读 qcc。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app import doris_client as d
from etl.lab_bootstrap import COMPANY_COLS, CONTACT_COLS, TAG_COLS, LAB, PROD, _cols

cc = _cols(COMPANY_COLS)
print("pull matched companies from prod → lab …")
d.execute(
    f"""
    INSERT INTO {LAB}.companies ({cc})
    SELECT {cc}
    FROM {PROD}.companies
    WHERE credit_code IN (
      SELECT credit_code FROM {LAB}.qualifications WHERE qual_type = 'hightech'
    )
    LIMIT 4000
    """,
    db=None,
)
print("lab companies:", d.query("SELECT COUNT(*) c FROM companies", db=LAB)[0]["c"])

d.execute(
    f"""
    INSERT INTO {LAB}.contacts ({_cols(CONTACT_COLS)})
    SELECT {_cols(CONTACT_COLS, 'ct')}
    FROM {PROD}.contacts ct
    WHERE ct.credit_code IN (
      SELECT credit_code FROM {LAB}.qualifications WHERE qual_type = 'hightech'
    )
    """,
    db=None,
)
print("lab contacts:", d.query("SELECT COUNT(*) c FROM contacts", db=LAB)[0]["c"])

hit = d.query(
    """
    SELECT COUNT(DISTINCT q.credit_code) AS hit
    FROM qualifications q
    INNER JOIN companies c ON q.credit_code = c.credit_code
    WHERE q.qual_type = 'hightech' AND q.revoked = 0
    """,
    db=LAB,
)[0]["hit"]
print("join hit:", hit)

t0 = time.time()
r = d.query(
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
print(f"浙江+高新+有手机: n={r[0]['n']}  {(time.time()-t0)*1000:.0f}ms")

# 空区块建模：稀疏 vs 富
sparse = d.query(
    """
    SELECT COUNT(*) n FROM companies
    WHERE IFNULL(intro,'')='' AND credit_code NOT IN (
      SELECT credit_code FROM tags
    )
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
print(f"稀疏(无intro无tags)≈{sparse}  富(有intro+tags)≈{rich}")
print("DONE — prod qcc not written")
