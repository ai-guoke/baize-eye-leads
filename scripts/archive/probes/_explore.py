# -*- coding: utf-8 -*-
"""列出任意目录的结构与体积。用法：python _explore.py <路径> [深度]"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    max_show = int(sys.argv[2]) if len(sys.argv) > 2 else 80

    if not root.exists():
        print(f"路径不存在：{root}")
        return

    print(f"目录：{root}\n")

    entries = sorted(root.iterdir(), key=lambda p: p.name)
    dirs = [p for p in entries if p.is_dir()]
    files = [p for p in entries if p.is_file()]
    print(f"直属子目录 {len(dirs)} 个｜直属文件 {len(files)} 个\n")

    for d in dirs[:max_show]:
        sub = list(d.rglob("*"))
        subf = [p for p in sub if p.is_file()]
        size = sum(p.stat().st_size for p in subf)
        ext = Counter(p.suffix.lower() for p in subf)
        top = "、".join(f"{k or '无扩展名'}×{v}" for k, v in ext.most_common(4))
        print(f"  [DIR ] {d.name}")
        print(f"          {len(subf):,} 文件  {size / 1024**3:.2f} GB  {top}")
    if len(dirs) > max_show:
        print(f"  … 另有 {len(dirs) - max_show} 个子目录")

    print()
    for f in files[:max_show]:
        print(f"  [FILE] {f.name}   {f.stat().st_size / 1024**2:,.1f} MB")
    if len(files) > max_show:
        print(f"  … 另有 {len(files) - max_show} 个文件")

    all_files = [p for p in root.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in all_files)
    ext = Counter(p.suffix.lower() for p in all_files)
    print(f"\n递归合计：{len(all_files):,} 个文件，{total / 1024**3:.2f} GB")
    print("扩展名分布：")
    for k, v in ext.most_common(15):
        s = sum(p.stat().st_size for p in all_files if p.suffix.lower() == k)
        print(f"  {k or '(无)':<12} {v:>7,} 个   {s / 1024**3:>8.2f} GB")


if __name__ == "__main__":
    main()
