# -*- coding: utf-8 -*-
"""探测「全国所有企业工商信息(1)」江浙沪皖数据集：表头、联系方式填充率、时效性。"""
from __future__ import annotations

import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from xlsx_stream import iter_rows, load_shared_strings, sheet_map

ROOT = Path(r"F:\企查查大数据\全国所有企业工商信息(1)")
EMPTY = {"", "-", "—", "–", "无", "null", "none", "n/a", "na", "/", "暂无", "未知"}
HEAD_ROWS = 500


def read_head(path: Path, limit: int = HEAD_ROWS):
    with zipfile.ZipFile(path) as z:
        sst = load_shared_strings(z)
        mapping = sheet_map(z)
        if not mapping:
            return [], [], {}
        out_rows = []
        header = []
        for name, xml in mapping.items():
            rows = iter_rows(z, xml, sst)
            try:
                header = next(rows)
            except StopIteration:
                continue
            for i, r in enumerate(rows):
                if i >= limit:
                    break
                out_rows.append(r)
            break
        return header, out_rows, mapping


def summarize(path: Path, label: str) -> list[str] | None:
    print("=" * 78)
    print(f"{label}  ·  {path.name}  ({path.stat().st_size / 1024**2:.1f} MB)")
    print("=" * 78)
    header, rows, mapping = read_head(path)
    if not header:
        print("  空文件\n")
        return None
    print(f"工作表：{list(mapping.keys())}｜列数 {len(header)}｜抽样 {len(rows)} 行\n")

    idx = {h.strip(): i for i, h in enumerate(header) if h.strip()}
    fill = Counter()
    blen = Counter()
    for r in rows:
        for col, i in idx.items():
            if i >= len(r):
                continue
            v = str(r[i]).strip()
            blen[col] += len(v.encode("utf-8"))
            if v and v.lower() not in EMPTY:
                fill[col] += 1

    n = max(len(rows), 1)
    print(f"{'列名':<22}{'非空率':>9}{'平均字节':>10}")
    print("-" * 46)
    for col, i in idx.items():
        print(f"  {col[:20]:<20} {fill[col] / n * 100:>7.1f}% {blen[col] / n:>9.0f}")
    print(f"\n  平均字节/行：{sum(blen.values()) / n:,.0f}")

    # 时效性：核准日期 / 成立日期的最大值
    for col in ("核准日期", "成立日期", "更新日期", "登记日期"):
        if col in idx:
            vals = []
            for r in rows:
                i = idx[col]
                if i < len(r):
                    v = str(r[i]).strip()
                    if re.match(r"^\d{4}-\d{2}-\d{2}", v):
                        vals.append(v[:10])
            if vals:
                print(f"  {col}：最早 {min(vals)}｜最晚 {max(vals)}")

    print("\n--- 首行样本 ---")
    if rows:
        for col, i in idx.items():
            v = str(rows[0][i]) if i < len(rows[0]) else ""
            if len(v) > 60:
                v = v[:60] + f"…（共 {len(str(rows[0][i]))} 字）"
            print(f"  {col[:18]:<20} = {v!r}")
    print()
    return [h.strip() for h in header]


def main() -> None:
    print(f"根目录：{ROOT}\n")
    signatures: dict[str, list[str]] = {}

    for sub in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        files = sorted(sub.glob("*.xlsx"))
        total = sum(f.stat().st_size for f in files)
        print(f"\n### {sub.name}：{len(files)} 个文件，{total / 1024**3:.2f} GB")
        print("   文件名样例：", "、".join(f.stem for f in files[:6]))
        if files:
            pick = min(files, key=lambda f: f.stat().st_size)
            sig = summarize(pick, sub.name)
            if sig:
                signatures[sub.name] = sig

    loose = sorted(p for p in ROOT.glob("*.xlsx"))
    for f in loose:
        sig = summarize(f, "根目录散件")
        if sig:
            signatures[f.name] = sig

    print("\n" + "=" * 78)
    print("表头一致性对比")
    print("=" * 78)
    base_name, base = next(iter(signatures.items()))
    for name, sig in signatures.items():
        same = "一致" if sig == base else "不同"
        print(f"  {name:<24} {len(sig):>3} 列   与「{base_name}」{same}")
        if sig != base:
            only_here = [c for c in sig if c not in base]
            only_base = [c for c in base if c not in sig]
            if only_here:
                print(f"      仅此有：{only_here}")
            if only_base:
                print(f"      此缺失：{only_base}")

    print("\n2023.11 版全国数据的 24 列参照：")
    print("  企业名称/经营状态/法定代表人/注册资本/实缴资本/成立日期/核准日期/营业期限/"
          "所属省份/所属城市/所属区县/统一社会信用代码/纳税人识别号/工商注册号/组织机构代码/"
          "参保人数/企业类型/所属行业/曾用名/注册地址/网址/联系电话/邮箱/经营范围")


if __name__ == "__main__":
    main()
