# -*- coding: utf-8 -*-
"""探测根目录散件 2025.04月.xlsx —— 无表头，需要推断列语义。"""
from __future__ import annotations

import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from xlsx_stream import iter_rows, load_shared_strings, sheet_map

PATH = Path(r"F:\企查查大数据\全国所有企业工商信息(1)\2025.04月.xlsx")
EMPTY = {"", "-", "--", "—", "无", "null"}
MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
CREDIT_RE = re.compile(r"^[0-9A-Z]{18}$")


def main() -> None:
    print(f"文件：{PATH.name}  {PATH.stat().st_size / 1024**2:.1f} MB\n")
    with zipfile.ZipFile(PATH) as z:
        sst = load_shared_strings(z)
        rows_iter = iter_rows(z, next(iter(sheet_map(z).values())), sst)
        rows = list(rows_iter)

    print(f"总行数（含首行数据）：{len(rows):,}")
    ncol = max(len(r) for r in rows)
    print(f"列数：{ncol}\n")

    fill = Counter()
    mobiles = Counter()
    credits = Counter()
    regions = Counter()
    dates = []
    nature = Counter()
    industry = Counter()
    scope_len = []
    codes = set()

    for r in rows:
        for i in range(ncol):
            v = str(r[i]).strip() if i < len(r) else ""
            if v and v.lower() not in EMPTY:
                fill[i] += 1
        def g(i: int) -> str:
            return str(r[i]).strip() if i < len(r) else ""

        if MOBILE_RE.match(g(10)):
            mobiles["合法手机号"] += 1
        if CREDIT_RE.match(g(5)):
            credits["合规信用代码"] += 1
            codes.add(g(5))
        if re.match(r"^\d{4}-\d{2}-\d{2}", g(2)):
            dates.append(g(2)[:10])
        if g(4):
            nature[g(4)] += 1
        if g(6):
            industry[g(6)] += 1
        if g(7):
            regions[g(7).split("-")[0]] += 1
        scope_len.append(len(g(8)))

    n = len(rows)
    print("推断的列语义与填充率：")
    guess = ["公司名称", "法定代表人", "成立/登记日期", "（空）", "企业性质",
             "统一社会信用代码", "所属行业", "地区(省-市-区)", "经营范围", "?", "手机号"]
    for i in range(ncol):
        label = guess[i] if i < len(guess) else "?"
        print(f"  [{i:2d}] {label:<16} {fill[i] / n * 100:>6.2f}%")

    print(f"\n合法手机号     {mobiles['合法手机号']:,}（{mobiles['合法手机号'] / n * 100:.2f}%）")
    print(f"合规信用代码   {credits['合规信用代码']:,}（{credits['合规信用代码'] / n * 100:.2f}%）")
    print(f"去重后主体     {len(codes):,}")
    if dates:
        print(f"日期范围       {min(dates)} ~ {max(dates)}")
    print(f"经营范围均长   {sum(scope_len) / max(n, 1):.0f} 字符")

    print("\n地区分布 Top15：")
    for k, v in regions.most_common(15):
        print(f"  {k:<10} {v:>8,}  {v / n * 100:>6.2f}%")
    print("\n企业性质：")
    for k, v in nature.most_common(10):
        print(f"  {k:<14} {v:>8,}  {v / n * 100:>6.2f}%")
    print("\n所属行业 Top10：")
    for k, v in industry.most_common(10):
        print(f"  {k:<24} {v:>8,}  {v / n * 100:>6.2f}%")

    # 与江浙沪皖主库的重叠
    import numpy as np
    cache = Path(__file__).resolve().parent / "output" / "quality_cache" / "jzh"
    parts = [np.load(p) for p in cache.glob("*.npy")]
    if parts:
        base = np.unique(np.concatenate(parts))
        mine = np.unique(np.array([c.encode("ascii") for c in codes], dtype="S18"))
        inter = np.intersect1d(base, mine, assume_unique=True)
        print(f"\n与江浙沪皖主库重叠：{len(inter):,} / {len(mine):,}"
              f"（{len(inter) / max(len(mine), 1) * 100:.2f}%）"
              f"，纯新增 {len(mine) - len(inter):,}")


if __name__ == "__main__":
    main()
