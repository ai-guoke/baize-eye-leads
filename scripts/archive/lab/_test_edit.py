# -*- coding: utf-8 -*-
import json, urllib.request

code = "91330106MADAYU306C"
# add mobile
req = urllib.request.Request(
    f"http://127.0.0.1:8765/api/company/{code}/contacts",
    data=json.dumps({"contact_type": "mobile", "contact_value": "13800138000"}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print("add", r.read().decode())
except Exception as e:
    print("add err", e)
    if hasattr(e, "read"):
        print(e.read().decode())

with urllib.request.urlopen(f"http://127.0.0.1:8765/api/company/{code}", timeout=30) as r:
    d = json.load(r)
print("contacts", d.get("contacts"))
print("has_mobile", d["company"].get("has_mobile"))

# cleanup delete
req2 = urllib.request.Request(
    f"http://127.0.0.1:8765/api/company/{code}/contacts?contact_type=mobile&contact_value=13800138000",
    method="DELETE",
)
with urllib.request.urlopen(req2, timeout=30) as r:
    print("del", r.read().decode())
