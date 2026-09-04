# -*- coding: utf-8 -*-
"""免费街道级坐标回填（不调用高德地理编码）。

思路：
1. 优先：companies.street 匹配 admin_divisions 街道中心点（拉取区划时已带 center）
2. 次选：同市同区已定位企业的街道均值（可选）
3. 再次：区县中心点

精度为街道/区县级，足够地图拓客气泡；同街道企业会加微小确定性偏移，避免完全重叠。

用法：
  python etl/geocode_by_street.py --dry-run
  python etl/geocode_by_street.py --limit 50000
  python etl/geocode_by_street.py --fallback district --batch 5000
"""
from __future__ import annotations

import argparse
import hashlib
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import doris_client as dc

COLS = ["credit_code", "lng", "lat", "geo_precision", "geocoded_at", "fill_score"]


def jitter(code: str, lng: float, lat: float, meters: float = 120.0) -> tuple[float, float]:
    """同街道企业错开约 ±meters，避免叠成一个点。"""
    h = int(hashlib.md5(code.encode("utf-8")).hexdigest()[:8], 16)
    dx = ((h % 1000) / 999.0 - 0.5) * 2 * meters
    dy = (((h // 1000) % 1000) / 999.0 - 0.5) * 2 * meters
    dlat = dy / 111_000.0
    dlng = dx / (111_000.0 * max(math.cos(math.radians(lat)), 0.2))
    return lng + dlng, lat + dlat


def fetch_candidates(
    limit: int,
    only_mobile: bool,
    include_no_street: bool,
    provinces: list[str] | None = None,
) -> list[dict]:
    """只拉能命中街道/区县中心的企业，避免 miss 反复占满 LIMIT。"""
    from admin_alias import admin_city_match_sql

    mobile = "AND c.has_mobile = 1" if only_mobile else ""
    prov_sql = ""
    if provinces:
        vals = ", ".join(
            "'" + p.replace("\\", "\\\\").replace("'", "\\'") + "'" for p in provinces
        )
        prov_sql = f"AND c.province IN ({vals})"
    city_ok = admin_city_match_sql("c.city", "a.city", lambda s: s.replace("\\", "\\\\").replace("'", "\\'"))
    # 街道命中（含直辖市工商名↔高德城区名）
    sql_street = f"""
        SELECT c.credit_code, c.company_name, c.province, c.city, c.district, c.street,
               c.fill_score, c.address
        FROM companies c
        INNER JOIN admin_divisions a
          ON a.level = 'street'
         AND a.district = c.district AND a.street = c.street
         AND a.center_lng IS NOT NULL
         AND {city_ok}
        WHERE c.lng IS NULL
          AND c.status = '存续'
          AND c.street IS NOT NULL AND c.street != ''
          {mobile}
          {prov_sql}
        ORDER BY c.has_mobile DESC, c.capital_wan DESC
        LIMIT {int(limit)}
    """
    rows = dc.query(sql_street)
    if rows or not include_no_street:
        return rows
    # 扫尾：无街道或街道未入库 → 区县中心
    return dc.query(f"""
        SELECT c.credit_code, c.company_name, c.province, c.city, c.district, c.street,
               c.fill_score, c.address
        FROM companies c
        INNER JOIN admin_divisions a
          ON a.level = 'district'
         AND a.district = c.district
         AND a.center_lng IS NOT NULL
         AND {city_ok}
        WHERE c.lng IS NULL
          AND c.status = '存续'
          AND c.district IS NOT NULL AND c.district != ''
          {mobile}
          {prov_sql}
        ORDER BY c.has_mobile DESC, c.capital_wan DESC
        LIMIT {int(limit)}
    """)


def remaining_count(
    only_mobile: bool,
    include_no_street: bool,
    provinces: list[str] | None = None,
) -> int:
    """进度用粗计数（未定位存续），精确可匹配数以本轮候选为准。"""
    mobile = "AND has_mobile = 1" if only_mobile else ""
    prov_sql = ""
    if provinces:
        vals = ", ".join(
            "'" + p.replace("\\", "\\\\").replace("'", "\\'") + "'" for p in provinces
        )
        prov_sql = f"AND province IN ({vals})"
    if include_no_street:
        rows = dc.query(
            f"SELECT COUNT(*) AS n FROM companies "
            f"WHERE lng IS NULL AND status = '存续' {mobile} {prov_sql}"
        )
    else:
        rows = dc.query(
            f"SELECT COUNT(*) AS n FROM companies "
            f"WHERE lng IS NULL AND status = '存续' "
            f"AND street IS NOT NULL AND street != '' {mobile} {prov_sql}"
        )
    return int(rows[0]["n"]) if rows else 0


def load_street_centers() -> dict[tuple[str, str, str, str], tuple[float, float]]:
    from admin_alias import expand_city_names

    rows = dc.query("""
        SELECT province, city, district, street, center_lng, center_lat
        FROM admin_divisions
        WHERE level = 'street'
          AND center_lng IS NOT NULL AND center_lat IS NOT NULL
          AND street IS NOT NULL AND street != ''
    """)
    out: dict[tuple[str, str, str, str], tuple[float, float]] = {}
    for r in rows:
        prov = (r.get("province") or "").strip()
        city = (r.get("city") or "").strip()
        dist = (r.get("district") or "").strip()
        street = (r.get("street") or "").strip()
        pt = (float(r["center_lng"]), float(r["center_lat"]))
        for city_alias in expand_city_names(city, dist):
            out[(prov, city_alias, dist, street)] = pt
    return out


def load_district_centers() -> dict[tuple[str, str, str], tuple[float, float]]:
    from admin_alias import expand_city_names

    rows = dc.query("""
        SELECT province, city, district, center_lng, center_lat
        FROM admin_divisions
        WHERE level = 'district'
          AND center_lng IS NOT NULL AND center_lat IS NOT NULL
    """)
    out: dict[tuple[str, str, str], tuple[float, float]] = {}
    for r in rows:
        prov = (r.get("province") or "").strip()
        city = (r.get("city") or "").strip()
        dist = (r.get("district") or "").strip()
        pt = (float(r["center_lng"]), float(r["center_lat"]))
        for city_alias in expand_city_names(city, dist):
            out[(prov, city_alias, dist)] = pt
            # 省直辖：工商 city=占位、district=县级市名
            out[(prov, dist, dist)] = pt
    return out


def resolve_point(
    row: dict,
    streets: dict,
    districts: dict,
    fallback: str,
) -> tuple[float, float, str] | None:
    from admin_alias import expand_city_names, is_direct_admin_city

    prov = (row.get("province") or "").strip()
    city = (row.get("city") or "").strip()
    dist = (row.get("district") or "").strip()
    street = (row.get("street") or "").strip()
    city_names = expand_city_names(city, dist)

    if street:
        for c in city_names:
            pt = streets.get((prov, c, dist, street))
            if pt:
                return pt[0], pt[1], "street"
        for (p, c, d, s), v in streets.items():
            if d == dist and s == street and c in city_names:
                return v[0], v[1], "street"
            # 省直辖：admin.city == company.district
            if is_direct_admin_city(city) and c == dist and s == street:
                return v[0], v[1], "street"

    if fallback == "district" and dist:
        for c in city_names:
            pt = districts.get((prov, c, dist))
            if pt:
                return pt[0], pt[1], "district"
        for (p, c, d), v in districts.items():
            if d == dist and c in city_names:
                return v[0], v[1], "district"
            if is_direct_admin_city(city) and c == dist:
                return v[0], v[1], "district"
    return None


def stream_batch(rows: list[list], label: str) -> dict:
    return dc.stream_load(
        "companies",
        COLS,
        dc.make_csv(rows),
        label=label,
        partial_columns=True,
        max_filter_ratio=0.2,
    )


def run_batch(
    streets: dict,
    districts: dict,
    limit: int,
    batch: int,
    fallback: str,
    only_mobile: bool,
    include_no_street: bool,
    jitter_m: float,
    dry_run: bool,
    provinces: list[str] | None = None,
) -> tuple[int, int, int]:
    """返回 (street命中, district命中, 未命中)。写入失败时抛错。"""
    cands = fetch_candidates(limit, only_mobile, include_no_street, provinces)
    print(f"待回填候选 {len(cands)} 家")
    if not cands:
        return 0, 0, 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ok_street = ok_district = miss = 0
    payload: list[list] = []
    samples: list[str] = []

    for r in cands:
        hit = resolve_point(r, streets, districts, fallback)
        if not hit:
            miss += 1
            continue
        lng, lat, prec = hit
        code = r["credit_code"]
        if jitter_m > 0:
            lng, lat = jitter(code, lng, lat, jitter_m)
        fill = int(r.get("fill_score") or 0) + 1
        payload.append([code, round(lng, 6), round(lat, 6), prec, now, fill])
        if prec == "street":
            ok_street += 1
        else:
            ok_district += 1
        if len(samples) < 5:
            samples.append(f"{r.get('company_name')} → {prec} ({lng:.5f},{lat:.5f})")

    print(f"可回填 street={ok_street} district={ok_district} 未命中={miss}")
    for s in samples:
        print("  例:", s)

    if dry_run:
        print("dry-run 结束，未写入")
        return ok_street, ok_district, miss
    if not payload:
        print("无数据可写")
        return ok_street, ok_district, miss

    written = 0
    for i in range(0, len(payload), batch):
        chunk = payload[i : i + batch]
        label = f"geo_street_{int(datetime.now().timestamp())}_{i}"
        js = stream_batch(chunk, label)
        status = str(js.get("Status", "")).lower()
        print(f"  batch {i}-{i+len(chunk)} status={status} loaded={js.get('NumberLoadedRows')}")
        if status not in {"success", "publish timeout"}:
            print("  失败详情:", js)
            raise RuntimeError(f"stream load failed: {status}")
        written += len(chunk)

    print(f"完成写入约 {written} 行。")
    return ok_street, ok_district, miss


def main() -> None:
    ap = argparse.ArgumentParser(description="按街道中心点免费回填坐标")
    ap.add_argument("--limit", type=int, default=100000, help="每轮最多处理企业数")
    ap.add_argument("--batch", type=int, default=5000, help="Stream Load 每批行数")
    ap.add_argument("--fallback", choices=["none", "district"], default="district",
                    help="街道未命中时是否回退区县中心")
    ap.add_argument("--only-mobile", action="store_true", help="仅有手机企业")
    ap.add_argument("--include-no-street", action="store_true",
                    help="含无街道企业（仅区县中心）")
    ap.add_argument("--loop", action="store_true",
                    help="循环直到本轮无可写，或连续两轮 0 写入")
    ap.add_argument("--max-rounds", type=int, default=200, help="--loop 最大轮数")
    ap.add_argument("--jitter", type=float, default=120.0, help="同点错开半径（米），0 关闭")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--province", action="append", default=[],
        help="仅处理指定省（可多次）；默认全国",
    )
    args = ap.parse_args()

    provinces = args.province or None
    print("加载街道/区县中心点…")
    streets = load_street_centers()
    districts = load_district_centers() if args.fallback == "district" else {}
    print(f"  街道中心 {len(streets)} · 区县中心 {len(districts)}")
    if provinces:
        print("限定省份:", "、".join(provinces))
    left0 = remaining_count(args.only_mobile, args.include_no_street, provinces)
    print(f"剩余待定位（本模式）: {left0:,}")

    rounds = args.max_rounds if args.loop else 1
    total_s = total_d = total_m = 0
    zero_write_streak = 0
    for rd in range(1, rounds + 1):
        print(f"\n===== 第 {rd}/{rounds} 轮 =====")
        s, d, m = run_batch(
            streets, districts,
            limit=args.limit,
            batch=args.batch,
            fallback=args.fallback,
            only_mobile=args.only_mobile,
            include_no_street=args.include_no_street,
            jitter_m=args.jitter,
            dry_run=args.dry_run,
            provinces=provinces,
        )
        total_s += s
        total_d += d
        total_m += m
        wrote = s + d
        if args.dry_run:
            break
        if wrote == 0:
            zero_write_streak += 1
            print("本轮无写入")
            if zero_write_streak >= 2 or m == 0:
                print("停止：连续无写入或无候选")
                break
            # 若全是未命中，继续下一轮也没用（同样的 ORDER 会重复 miss）
            if m > 0 and s + d == 0:
                print("停止：候选均未命中中心点（可能缺区县中心）")
                break
        else:
            zero_write_streak = 0
        left = remaining_count(args.only_mobile, args.include_no_street, provinces)
        print(f"剩余待定位（本模式）: {left:,}")
        if left == 0:
            print("全部完成")
            break

    print(f"\n合计 street={total_s} district={total_d} miss累计≈{total_m}")
    left = dc.query(
        "SELECT COUNT(*) AS n FROM companies "
        "WHERE lng IS NULL AND street IS NOT NULL AND street != '' AND status = '存续'"
    )
    print("剩余有街道无坐标（存续）:", left[0]["n"] if left else "?")
    left2 = dc.query(
        "SELECT COUNT(*) AS n FROM companies WHERE lng IS NULL AND status = '存续'"
    )
    print("剩余全部无坐标（存续）:", left2[0]["n"] if left2 else "?")


if __name__ == "__main__":
    main()
