# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app.doris_client as dc

print(dc.query("SELECT contact_type, COUNT(1) AS n FROM contacts GROUP BY contact_type"))
print(dc.query(
    "SELECT contact_value, credit_code FROM contacts "
    "WHERE contact_type='mobile' LIMIT 5"
))
# phone lookup speed smoke
import time
t0 = time.time()
phone = dc.query(
    "SELECT contact_value FROM contacts WHERE contact_type='mobile' LIMIT 1"
)[0]["contact_value"]
rows = dc.query(
    f"SELECT c.credit_code, c.company_name, c.legal_person, ct.contact_value "
    f"FROM contacts ct JOIN companies c ON c.credit_code = ct.credit_code "
    f"WHERE ct.contact_value = '{phone}' LIMIT 20"
)
print("sample phone", phone, "hits", len(rows), "ms", int((time.time()-t0)*1000))
print(rows[:2])
