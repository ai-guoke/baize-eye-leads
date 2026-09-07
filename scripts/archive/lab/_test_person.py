# -*- coding: utf-8 -*-
import json, urllib.parse, urllib.request, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app.doris_client as dc

name = "朱明确"
print("any status", dc.query(f"SELECT COUNT(1) AS n FROM companies WHERE legal_person = '{name}'"))
print("company like", dc.query(f"SELECT COUNT(1) AS n FROM companies WHERE company_name LIKE '%{name}%'"))
# sample similar names
print("like prefix", dc.query(
    f"SELECT legal_person, COUNT(1) n FROM companies "
    f"WHERE legal_person LIKE '朱明%' GROUP BY legal_person ORDER BY n DESC LIMIT 10"
))

with urllib.request.urlopen(
    "http://127.0.0.1:8765/api/search?" + urllib.parse.urlencode({"q": name, "status": "存续", "page_size": 5}),
    timeout=60,
) as r:
    d = json.load(r)
print("api kind", d.get("q_kind"), "total", d.get("total"))
for it in d.get("items") or []:
    print(" -", it.get("legal_person"), "|", (it.get("company_name") or "")[:30])
