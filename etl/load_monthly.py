# -*- coding: utf-8 -*-
"""导入 2025.04 月度增量（无表头，一企多行，需聚合手机号）。

列序（按原始行实测，已修正）：
  0公司名 1法人 2成立日期 3地址 4企业性质 5信用代码
  6注册资本 7国标行业门类 8地区(省-市-区) 9经营范围 10手机号
"""
from __future__ import annotations

import hashlib
import re
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import app.doris_client as dc
from scripts.xlsx_stream import iter_rows, load_shared_strings, sheet_map
from etl.load_jzh import (
    COMPANY_COLS, CONTACT_COLS, CREDIT_RE, MOBILE_RE, DIGITS_RE,
    PROVINCE_MAP, clean, tb, parse_date, parse_capital, split_multi,
    surrogate_credit, TODAY,
)
from app.street_utils import extract_street, load_admin_streets

FILE = Path(r"F:\企查查大数据\全国所有企业工商信息(1)\2025.04月.xlsx")
SOURCE = "monthly2025"
COLLECTED_AT = "2025-04-01"


def parse_region(raw: str) -> tuple[str | None, str | None, str | None]:
    """'上海-上海市-宝山区' / '江苏-苏州市-吴中区' → 省市区。"""
    if not raw:
        return None, None, None
    parts = [p.strip() for p in re.split(r"[-—/]", raw) if p.strip()]
    if not parts:
        return None, None, None
    prov = PROVINCE_MAP.get(parts[0], parts[0])
    # 已是标准名则保留；短名如「上海」走上面的 map
    if prov and not any(prov.endswith(s) for s in ("省", "市", "区")):
        # 兜底：带「省/市」后缀
        for suf in ("省", "市"):
            if PROVINCE_MAP.get(parts[0] + suf):
                prov = PROVINCE_MAP[parts[0] + suf]
                break
    city = parts[1] if len(parts) > 1 else None
    dist = parts[2] if len(parts) > 2 else None
    if prov in ("上海市", "北京市", "天津市", "重庆市"):
        if len(parts) >= 3:
            city, dist = parts[1], parts[2]
        elif len(parts) == 2:
            city, dist = prov, parts[1]
    return prov, city, dist


def main() -> None:
    if not FILE.exists():
        print(f"文件不存在：{FILE}")
        sys.exit(1)
    if not dc.wait_ready(timeout=60):
        print("Doris 未就绪")
        sys.exit(1)
    n = load_admin_streets(force=True)
    print(f"标准街道库 {n:,}（导入时按标准名匹配）")

    # 先清掉历史错误映射写入的月度行，避免 sequence 冲突
    n0 = dc.query("SELECT COUNT(*) AS n FROM companies WHERE src_group = '月度增量'")[0]["n"]
    if n0:
        print(f"清理旧月度增量 {n0:,} 行…")
        dc.execute("DELETE FROM companies WHERE src_group = '月度增量'")
        dc.execute("DELETE FROM contacts WHERE source = 'monthly2025'")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    companies: dict[str, dict] = {}
    contacts: dict[tuple, list] = {}

    with zipfile.ZipFile(FILE) as z:
        sst = load_shared_strings(z)
        mapping = sheet_map(z)
        rows = iter_rows(z, next(iter(mapping.values())), sst)
        n_rows = 0
        for row in rows:
            n_rows += 1

            def g(i: int) -> str:
                return row[i].strip() if i < len(row) and row[i] else ""

            name = clean(g(0))
            if not name:
                continue
            name = tb(name, 300)
            code = g(5).upper().replace(" ", "")
            if not CREDIT_RE.match(code):
                code = surrogate_credit(name)

            mobiles: list[str] = []
            for p in split_multi(g(10)):
                d = "".join(DIGITS_RE.findall(p))
                if MOBILE_RE.match(d) and d not in mobiles:
                    mobiles.append(d)

            for i, v in enumerate(mobiles):
                ck = (code, "mobile", v)
                if ck not in contacts:
                    contacts[ck] = [code, "mobile", v, SOURCE, name,
                                    1 if i == 0 else 0, COLLECTED_AT, now]

            prev = companies.get(code)
            fill = 300 + sum(1 for i in range(11) if clean(g(i)))
            if prev and prev["fill"] >= fill:
                prev["mobiles"] = list(dict.fromkeys(prev["mobiles"] + mobiles))
                continue

            est = parse_date(g(2))
            est_year = int(est[:4]) if est else None
            age = None
            if est:
                try:
                    age = round(
                        (TODAY - date(int(est[:4]), int(est[5:7]), int(est[8:10]))).days / 365.25,
                        1,
                    )
                except ValueError:
                    age = None
            prov, city, dist = parse_region(g(8))
            cap_wan, cap_cur = parse_capital(g(6), name)
            addr = clean(g(3))
            street = extract_street(prov or "", city or "", dist or "", addr or "") or None

            companies[code] = {
                "fill": fill,
                "mobiles": list(mobiles),
                "row": [
                    code, name, None, tb(clean(g(1)), 128), None,
                    cap_wan, tb(clean(g(6)), 64), tb(cap_cur, 16), None,
                    est, None, est_year, age, None,
                    tb(prov, 32), tb(city, 64), tb(dist, 64), tb(street, 64),
                    tb(clean(g(4)), 128),
                    tb(clean(g(7)), 64), None, None,
                    None, None, None, None, None, None, None,
                    tb(addr, 500), None, None, None, clean(g(9)),
                    1 if mobiles else 0, 0, 0, 1 if mobiles else 0,
                    len(mobiles), 0, fill,
                    "月度增量", tb(FILE.name, 128), now,
                ],
            }

    for code, ag in companies.items():
        r = ag["row"]
        # has_mobile / has_contact / mobile_count after street 插入
        r[34] = 1 if ag["mobiles"] else 0
        r[37] = 1 if ag["mobiles"] else 0
        r[38] = len(ag["mobiles"])

    print(f"读取 {n_rows:,} 行 → {len(companies):,} 家企业 → {len(contacts):,} 条联系方式")
    # 抽查 3 条映射
    for i, (code, ag) in enumerate(companies.items()):
        if i >= 3:
            break
        r = ag["row"]
        print(f"  样例: {r[1]} | 省={r[14]} 市={r[15]} 区={r[16]} 街={r[17]} | 行业={r[19]} | 资本={r[6]}")

    tag = hashlib.md5(f"{FILE.stat().st_size}-v4".encode()).hexdigest()[:12]
    if companies:
        r = dc.stream_load(
            "companies", COMPANY_COLS,
            dc.make_csv(ag["row"] for ag in companies.values()),
            label=f"monthly2025_c4_{tag}",
        )
        print("companies:", r.get("Status"), "loaded=", r.get("NumberLoadedRows"))
    if contacts:
        r = dc.stream_load(
            "contacts", CONTACT_COLS, dc.make_csv(contacts.values()),
            label=f"monthly2025_t4_{tag}",
        )
        print("contacts:", r.get("Status"), "loaded=", r.get("NumberLoadedRows"))
    else:
        print("警告：未解析到任何手机号")
    print("月度增量导入完成")


if __name__ == "__main__":
    main()
