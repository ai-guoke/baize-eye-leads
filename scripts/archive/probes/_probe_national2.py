# -*- coding: utf-8 -*-
"""第二轮探测：表头一致性、联系方式填充情况、信用代码质量、总行数外推。"""
from __future__ import annotations

import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from xlsx_stream import iter_rows, load_shared_strings, sheet_map

ROOT = Path(r"F:\企查查大数据\工商注册企业全量信息（更新2023.11）\全国数据")
HEAD_ROWS = 400


def read_head(path: Path, limit: int = HEAD_ROWS):
    """返回 (header, 前 limit 行)。"""
    with zipfile.ZipFile(path) as z:
        sst = load_shared_strings(z)
        mapping = sheet_map(z)
        if not mapping:
            return [], []
        xml_path = next(iter(mapping.values()))
        rows = iter_rows(z, xml_path, sst)
        try:
            header = next(rows)
        except StopIteration:
            return [], []
        out = []
        for i, r in enumerate(rows):
            if i >= limit:
                break
            out.append(r)
        return header, out


def main() -> None:
    files = sorted(ROOT.rglob("*.xlsx"))

    # 每个省挑体积最小的一个文件，快速覆盖所有省
    by_prov: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        by_prov[f.stem.split("_")[0]].append(f)
    picks = [min(v, key=lambda p: p.stat().st_size) for v in by_prov.values()]

    print(f"省级分区 {len(by_prov)} 个，抽检 {len(picks)} 个文件\n")
    print("省份覆盖：", "、".join(sorted(by_prov.keys())))
    print()

    header_sigs = Counter()
    contact_stats = Counter()
    total_sampled = 0
    first_header = None

    for p in picks:
        header, rows = read_head(p)
        if not header:
            print(f"  !! 空文件 {p.name}")
            continue
        sig = "|".join(h.strip() for h in header)
        header_sigs[sig] += 1
        if first_header is None:
            first_header = header
            idx = {h.strip(): i for i, h in enumerate(header)}

        for r in rows:
            total_sampled += 1
            for col in ("联系电话", "邮箱", "网址", "参保人数", "实缴资本", "法定代表人", "经营范围"):
                i = idx.get(col)
                if i is None or i >= len(r):
                    continue
                v = str(r[i]).strip()
                if v and v not in {"-", "—", "无", "null", "None"}:
                    contact_stats[col] += 1

    print(f"表头签名种类：{len(header_sigs)}")
    for sig, cnt in header_sigs.most_common():
        cols = sig.split("|")
        print(f"  出现 {cnt} 次 · {len(cols)} 列 · {cols[:6]} …")
    print()

    print(f"关键字段非空率（抽样 {total_sampled:,} 行，跨 {len(picks)} 省）：")
    for col in ("法定代表人", "经营范围", "联系电话", "邮箱", "网址", "参保人数", "实缴资本"):
        n = contact_stats[col]
        print(f"  {col:<8} {n / max(total_sampled, 1) * 100:>6.2f}%   ({n:,} / {total_sampled:,})")
    print()

    # 看几行真实样本，确认空值到底长什么样
    header, rows = read_head(picks[0], limit=3)
    print(f"===== {picks[0].name} 前 3 行原样 =====")
    for r in rows:
        print("-" * 60)
        for i, h in enumerate(header):
            v = str(r[i]) if i < len(r) else ""
            if len(v) > 70:
                v = v[:70] + f"…（共 {len(str(r[i]))} 字）"
            print(f"  {h.strip() or '(空列名)':<12} = {v!r}")
    print()

    # 行数外推：用第一轮实测的 4,509 行/MB
    total_mb = sum(f.stat().st_size for f in files) / 1024**2
    print(f"总体积 {total_mb / 1024:.2f} GB，按实测 4,509 行/MB 外推：约 {total_mb * 4509 / 1e8:.2f} 亿行")


if __name__ == "__main__":
    main()
