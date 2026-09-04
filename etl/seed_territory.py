# -*- coding: utf-8 -*-
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")
import doris_client as dc

now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
# Stream Load 会把空串当成 NULL；本表 street 为 NOT NULL，改用 SQL 插入
dc.execute("DELETE FROM sales_territory WHERE user_id = 'default'")
for prov, city, dist, street in [
    ("江苏省", "苏州市", "吴中区", "木渎镇"),
    ("江苏省", "苏州市", "工业园区", ""),
]:
    dc.execute(
        "INSERT INTO sales_territory (user_id, province, city, district, street, loaded_at) VALUES "
        f"('default', '{prov}', '{city}', '{dist}', '{street}', '{now}')"
    )
print("rows", dc.query("SELECT user_id, province, city, district, street FROM sales_territory"))
