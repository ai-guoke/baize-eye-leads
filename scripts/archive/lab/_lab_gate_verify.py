# -*- coding: utf-8 -*-
"""补空回归 + JOIN 再测。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app import doris_client as d

# 1) 重叠主体：lab 与 prod 法人均非空，值应一致（enrich 不得改写非空）
rows = d.query(
    """
    SELECT l.credit_code,
           l.legal_person AS lab_lp, p.legal_person AS prod_lp,
           l.status AS lab_st, p.status AS prod_st,
           l.company_name AS lab_nm, p.company_name AS prod_nm
    FROM qcc_lab.companies l
    INNER JOIN qcc.companies p ON l.credit_code = p.credit_code
    WHERE IFNULL(p.legal_person,'') != ''
      AND l.city LIKE '%舟山%'
    LIMIT 500
    """,
    db=None,
)
viol = []
for r in rows:
    for a, b, f in (
        (r["lab_lp"], r["prod_lp"], "legal_person"),
        (r["lab_st"], r["prod_st"], "status"),
        (r["lab_nm"], r["prod_nm"], "company_name"),
    ):
        if a and b and str(a) != str(b):
            viol.append((r["credit_code"], f, b, a))
print(f"preserve check overlap={len(rows)} violations={len(viol)}")
for v in viol[:5]:
    print(" ", v)

# 2) JOIN bench 21 rounds
sql = """
SELECT COUNT(*) AS n
FROM qcc.companies c
WHERE c.province = '浙江省' AND c.has_mobile = 1 AND c.status = '存续'
  AND EXISTS (
    SELECT 1 FROM qcc_lab.qualifications q
    WHERE q.credit_code = c.credit_code AND q.qual_type = 'hightech' AND q.revoked = 0
  )
"""
times = []
n = None
for _ in range(21):
    t0 = time.time()
    r = d.query(sql, db=None)
    times.append((time.time() - t0) * 1000)
    n = r[0]["n"]
body = sorted(times[1:])
p50 = body[len(body) // 2]
p95 = body[int(len(body) * 0.95)]
print(
    f"JOIN n={n} warmup={times[0]:.0f}ms p50={p50:.0f}ms p95={p95:.0f}ms "
    f"max={body[-1]:.0f}ms pass={p95 < 1000}"
)

# 3) 耗时外推
# 5 文件 24.3s / 50718 行 → 单分片均 4.9s；853 分片粗算
per_file = 24.3 / 5
print(f"S1 timing: {per_file:.1f}s/file × 853 ≈ {per_file*853/3600:.1f}h (小文件偏乐观，大文件另计)")

# 4) 生产未增长确认（相对本脚本读取）
print("prod companies", d.query("SELECT COUNT(*) c FROM companies", db="qcc")[0]["c"])
print("lab companies", d.query("SELECT COUNT(*) c FROM companies", db="qcc_lab")[0]["c"])
