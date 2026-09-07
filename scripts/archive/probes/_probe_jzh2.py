# -*- coding: utf-8 -*-
"""江浙沪皖数据集第二轮：按文件大小分档抽样，识别全量文件与增量文件的差异。"""
from __future__ import annotations

import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from xlsx_stream import iter_rows, load_shared_strings, sheet_map

ROOT = Path(r"F:\企查查大数据\全国所有企业工商信息(1)")
EMPTY = {"", "-", "--", "—", "–", "无", "null", "none", "n/a", "na", "/", "暂无", "未知"}
SAMPLE = 800

KEY_COLS = [
    "公司名称", "登记状态", "企业规模", "注册资本", "参保人数",
    "有效手机号", "更多电话", "邮箱", "其他邮箱",
    "国标行业门类", "统一社会信用代码", "注册地址", "经营范围", "英文名",
]


def head_of(path: Path, limit: int = SAMPLE):
    with zipfile.ZipFile(path) as z:
        sst = load_shared_strings(z)
        mapping = sheet_map(z)
        if not mapping:
            return [], []
        rows = iter_rows(z, next(iter(mapping.values())), sst)
        try:
            header = next(rows)
        except StopIteration:
            return [], []
        out = []
        for i, r in enumerate(rows):
            if i >= limit:
                break
            out.append(r)
        return [h.strip() for h in header], out


def profile(path: Path, tag: str) -> None:
    header, rows = head_of(path)
    if not header or not rows:
        print(f"  {tag:<6} {path.name:<28} 空文件")
        return
    idx = {h: i for i, h in enumerate(header) if h}
    n = len(rows)
    fill = Counter()
    years = []
    for r in rows:
        for c in KEY_COLS:
            i = idx.get(c, -1)
            if 0 <= i < len(r):
                v = str(r[i]).strip()
                if v and v.lower() not in EMPTY:
                    fill[c] += 1
        i = idx.get("成立日期", -1)
        if 0 <= i < len(r):
            v = str(r[i]).strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}", v):
                years.append(v[:4])

    phone = fill["有效手机号"] + fill["更多电话"]
    span = f"{min(years)}~{max(years)}" if years else "?"
    print(
        f"  {tag:<6} {path.name[:26]:<28} {path.stat().st_size / 1024**2:>7.1f}MB"
        f"  手机{fill['有效手机号'] / n * 100:>5.1f}%"
        f"  座机{fill['更多电话'] / n * 100:>5.1f}%"
        f"  邮箱{fill['邮箱'] / n * 100:>5.1f}%"
        f"  参保{fill['参保人数'] / n * 100:>5.1f}%"
        f"  规模{fill['企业规模'] / n * 100:>5.1f}%"
        f"  成立年{span}"
    )


def main() -> None:
    for sub in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        files = sorted(sub.glob("*.xlsx"), key=lambda f: f.stat().st_size)
        total = sum(f.stat().st_size for f in files)
        sizes = [f.stat().st_size / 1024**2 for f in files]
        print(f"\n### {sub.name}｜{len(files)} 文件｜{total / 1024**3:.2f} GB")
        print(
            f"    体积分布：最小 {min(sizes):.1f}MB  中位 {sizes[len(sizes) // 2]:.1f}MB"
            f"  最大 {max(sizes):.1f}MB  平均 {sum(sizes) / len(sizes):.1f}MB"
        )
        # 按编号排序看是否有规律
        def num(f: Path) -> int:
            m = re.search(r"(\d+)$", f.stem)
            return int(m.group(1)) if m else 0

        by_num = sorted(files, key=num)
        print("    按编号的体积序列：",
              " ".join(f"{f.stat().st_size / 1024**2:.0f}" for f in by_num[:24]),
              "…" if len(by_num) > 24 else "")

        picks = [
            ("最小", files[0]),
            ("中位", files[len(files) // 2]),
            ("最大", files[-1]),
        ]
        for tag, f in picks:
            profile(f, tag)


if __name__ == "__main__":
    main()
