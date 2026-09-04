# -*- coding: utf-8 -*-
"""从高德行政区域 API 拉取省市区街道，写入 admin_divisions。

默认江浙沪皖；--all 拉取全国 31 省市区（坐标回填依赖此表）。

用法：
  python etl/load_admin_amap.py
  python etl/load_admin_amap.py --all
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import argparse

import requests

import app.doris_client as dc
from app.amap_config import load_amap_env

JZH_PROVINCES = ["江苏省", "浙江省", "上海市", "安徽省"]
ALL_PROVINCES = [
    "北京市", "天津市", "河北省", "山西省", "内蒙古自治区",
    "辽宁省", "吉林省", "黑龙江省",
    "上海市", "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省",
    "河南省", "湖北省", "湖南省", "广东省", "广西壮族自治区", "海南省",
    "重庆市", "四川省", "贵州省", "云南省", "西藏自治区",
    "陕西省", "甘肃省", "青海省", "宁夏回族自治区", "新疆维吾尔自治区",
]
API = "https://restapi.amap.com/v3/config/district"
CACHE = Path(__file__).resolve().parent.parent / "output" / "admin_amap_raw.json"
COLS = [
    "row_key", "adcode", "level", "name", "parent_adcode",
    "province", "city", "district", "street",
    "center_lng", "center_lat", "source", "loaded_at",
]


def _center(s: str) -> tuple[float | None, float | None]:
    if not s or "," not in str(s):
        return None, None
    try:
        a, b = str(s).split(",", 1)
        return float(a), float(b)
    except Exception:
        return None, None


def _walk(
    node: dict,
    parent_adcode: str,
    province: str,
    city: str,
    district: str,
    now: str,
    rows: list,
) -> None:
    level = (node.get("level") or "").strip()
    name = (node.get("name") or "").strip()
    adcode = str(node.get("adcode") or "").strip()
    if not name or not level:
        return
    lng, lat = _center(node.get("center") or "")
    p, c, d, st = province, city, district, ""
    if level == "province":
        p = name
    elif level == "city":
        c = name
    elif level == "district":
        d = name
    elif level == "street":
        st = name
    else:
        # 少数返回 country 等，跳过
        pass

    row_key = f"{level}|{adcode}|{parent_adcode}|{name}"
    rows.append([
        row_key, adcode, level, name, parent_adcode or "",
        p or "", c or "", d or "", st or "",
        lng, lat,
        "amap", now,
    ])

    for child in node.get("districts") or []:
        _walk(child, adcode, p, c, d, now, rows)


def fetch_province(key: str, province: str) -> dict:
    r = requests.get(
        API,
        params={
            "key": key,
            "keywords": province,
            "subdistrict": 3,
            "extensions": "base",
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if str(data.get("status")) != "1":
        raise RuntimeError(f"{province}: {data.get('info')} ({data.get('infocode')})")
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="拉取全国 31 省级行政区")
    args = ap.parse_args()
    provinces = ALL_PROVINCES if args.all else JZH_PROVINCES

    env = load_amap_env()
    key = env.get("AMAP_WEB_KEY") or ""
    if not key:
        print("缺少 AMAP_WEB_KEY")
        sys.exit(1)

    if not dc.wait_ready(timeout=60):
        print("Doris 未就绪")
        sys.exit(1)

    schema = Path(__file__).resolve().parent.parent / "sql" / "03_admin_divisions.sql"
    print(f"执行 {schema}", flush=True)
    dc.execute("DROP TABLE IF EXISTS admin_divisions")
    dc.run_script(schema, db="qcc")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_raw: dict[str, dict] = {}
    rows: list = []

    for i, prov in enumerate(provinces):
        print(f"拉取 {prov} ({i+1}/{len(provinces)}) …", flush=True)
        data = fetch_province(key, prov)
        all_raw[prov] = data
        for top in data.get("districts") or []:
            _walk(top, "", "", "", "", now, rows)
        if i < len(provinces) - 1:
            time.sleep(0.35)

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(all_raw, ensure_ascii=False), encoding="utf-8")
    print(f"原始 JSON → {CACHE}  扁平行 {len(rows):,}")

    # Stream Load
    batch = 5000
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        js = dc.stream_load(
            "admin_divisions",
            COLS,
            dc.make_csv(chunk),
            label=f"admin_{i // batch}_{int(time.time())}",
            max_filter_ratio=0.05,
        )
        print(f"  load [{i:,}+{len(chunk)}] {js.get('Status')}", flush=True)

    stats = dc.query(
        "SELECT level, COUNT(*) AS n FROM admin_divisions GROUP BY level ORDER BY n DESC"
    )
    print("入库统计：")
    for r in stats:
        print(f"  {r['level']}: {r['n']:,}")

    sample = dc.query(
        "SELECT province, city, district, street FROM admin_divisions "
        "WHERE level='street' AND district='江宁区' ORDER BY street LIMIT 20"
    )
    print("江宁区街道样例：")
    for r in sample:
        print(f"  {r['city']} / {r['district']} / {r['street']}")


if __name__ == "__main__":
    main()
