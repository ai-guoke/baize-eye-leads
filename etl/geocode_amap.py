# -*- coding: utf-8 -*-
"""高德 Web 服务地理编码批跑。

优先：has_mobile=1 AND status='存续' 且尚未编码。
需要 secrets/amap.env 中的 AMAP_WEB_KEY（Web服务类型）。

用法：
  python etl/geocode_amap.py --limit 1000
  python etl/geocode_amap.py --limit 50000 --qps 3
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import app.doris_client as dc
from app.amap_config import load_amap_env

GEO_URL = "https://restapi.amap.com/v3/geocode/geo"
COLS = ["credit_code", "lng", "lat", "geo_precision", "geocoded_at", "fill_score"]


def geocode_one(key: str, address: str, city: str = "", retries: int = 3) -> dict | None:
    params = {"key": key, "address": address}
    if city:
        params["city"] = city
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(GEO_URL, params=params, timeout=20)
            js = r.json()
            if str(js.get("status")) != "1" or not js.get("geocodes"):
                return None
            g = js["geocodes"][0]
            loc = (g.get("location") or "").split(",")
            if len(loc) != 2:
                return None
            return {
                "lng": float(loc[0]),
                "lat": float(loc[1]),
                "precision": (g.get("level") or "")[:16],
            }
        except (requests.RequestException, ValueError, KeyError) as e:
            last_err = e
            time.sleep(0.8 * attempt)
    if last_err:
        raise last_err
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--qps", type=float, default=3.0, help="请求速率上限")
    a = ap.parse_args()

    cfg = load_amap_env()
    key = (cfg.get("AMAP_WEB_KEY") or "").strip()
    if not key:
        print("未配置 AMAP_WEB_KEY（需高德「Web服务」Key）。")
        print("请写入 secrets/amap.env 后重试。Web 端 JS Key 不能用于本脚本。")
        sys.exit(2)

    if not dc.wait_ready(timeout=30):
        print("Doris 未就绪")
        sys.exit(1)

    rows = dc.query(f"""
        SELECT credit_code, address, city, IFNULL(fill_score,0) AS fill_score
        FROM companies
        WHERE has_mobile = 1 AND status = '存续'
          AND address IS NOT NULL AND address != ''
          AND (lng IS NULL OR lat IS NULL)
        ORDER BY insured_count DESC, capital_wan DESC
        LIMIT {int(a.limit)}
    """)
    print(f"待编码 {len(rows):,} 条（limit={a.limit}）")
    if not rows:
        return

    interval = 1.0 / max(a.qps, 0.1)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ok = fail = 0
    buf = []
    t_last = 0.0
    cache: dict[str, dict | None] = {}

    def flush(i: int):
        nonlocal buf
        if not buf:
            return
        label = f"geo_{i}_{hashlib.md5(buf[0][0].encode()).hexdigest()[:8]}"
        js = dc.stream_load(
            "companies", COLS, dc.make_csv(buf),
            label=label, partial_columns=True, max_filter_ratio=0.05,
        )
        print(f"  flush {len(buf)} → {js.get('Status')}", flush=True)
        buf = []

    for i, r in enumerate(rows, 1):
        addr = (r.get("address") or "").strip()
        city = (r.get("city") or "").strip()
        ck = f"{city}|{addr}"
        wait = interval - (time.time() - t_last)
        if wait > 0:
            time.sleep(wait)
        t_last = time.time()

        if ck in cache:
            geo = cache[ck]
        else:
            try:
                geo = geocode_one(key, addr, city)
            except Exception as e:
                print(f"  !! {r['credit_code']}: {e}")
                geo = None
            cache[ck] = geo

        if not geo:
            fail += 1
            continue
        ok += 1
        buf.append([
            r["credit_code"], geo["lng"], geo["lat"], geo["precision"],
            now, int(r.get("fill_score") or 0) + 1,
        ])
        if len(buf) >= a.batch:
            flush(i)
        if i % 100 == 0:
            print(f"  [{i}/{len(rows)}] ok={ok} fail={fail} uniq_addr={len(cache)}", flush=True)

    flush(len(rows))
    print(f"完成：成功 {ok:,}，失败 {fail:,}，地址去重缓存 {len(cache):,}")


if __name__ == "__main__":
    main()
