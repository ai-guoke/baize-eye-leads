# -*- coding: utf-8 -*-
"""探测「工商注册企业全量信息」目录的规模与字段结构，为数据库选型提供实测依据。

用法：python _probe_national.py
"""
from __future__ import annotations

import sys
import zipfile
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from xlsx_stream import iter_workbook_sheets, iter_rows, load_shared_strings, sheet_map

ROOT = Path(r"F:\企查查大数据\工商注册企业全量信息（更新2023.11）\全国数据")
SAMPLE_ROWS = 3000


def scan_dir() -> list[Path]:
    files = sorted(ROOT.rglob("*.xlsx"))
    total = sum(f.stat().st_size for f in files)
    print(f"文件数：{len(files):,}")
    print(f"总大小：{total / 1024**3:.2f} GB")
    print()

    provinces = Counter()
    for f in files:
        provinces[f.stem.split("_")[0]] += f.stat().st_size
    print(f"省级分区数：{len(provinces)}")
    for name, size in provinces.most_common(10):
        print(f"  {name}  {size / 1024**2:,.0f} MB")
    print()
    return files


def probe_file(path: Path) -> None:
    print(f"===== 抽样文件：{path.name}  ({path.stat().st_size / 1024**2:.1f} MB) =====")
    with zipfile.ZipFile(path) as z:
        inner = sum(i.file_size for i in z.infolist())
        print(f"解压后 XML 体积：{inner / 1024**2:,.0f} MB（膨胀 {inner / path.stat().st_size:.1f}x）")
        sst = load_shared_strings(z)
        print(f"sharedStrings 去重字符串数：{len(sst):,}")
        mapping = sheet_map(z)
        print(f"工作表：{list(mapping.keys())}")

        for sheet_name, xml_path in mapping.items():
            rows = iter_rows(z, xml_path, sst)
            try:
                header = next(rows)
            except StopIteration:
                continue
            print(f"\n--- sheet「{sheet_name}」列数 {len(header)} ---")
            for i, h in enumerate(header):
                print(f"  [{i:2d}] {h}")

            n = 0
            sample_bytes = 0
            col_bytes = [0] * len(header)
            nonempty = [0] * len(header)
            for row in rows:
                n += 1
                if n <= SAMPLE_ROWS:
                    for i in range(min(len(row), len(header))):
                        b = len(str(row[i]).encode("utf-8"))
                        col_bytes[i] += b
                        sample_bytes += b
                        if row[i]:
                            nonempty[i] += 1
                if n % 200_000 == 0:
                    print(f"    …已扫 {n:,} 行")

            print(f"\n  数据行数：{n:,}")
            s = min(n, SAMPLE_ROWS)
            if s:
                print(f"  平均字节/行（UTF-8，前 {s:,} 行）：{sample_bytes / s:,.0f}")
                print(f"\n  各列平均字节 / 填充率（前 {s:,} 行）：")
                order = sorted(range(len(header)), key=lambda i: -col_bytes[i])
                for i in order:
                    print(
                        f"    {header[i][:24]:<26} {col_bytes[i] / s:>8,.0f} B"
                        f"   {nonempty[i] / s * 100:>5.1f}%"
                    )
            break


def main() -> None:
    files = scan_dir()
    if not files:
        print("目录下没有 xlsx")
        return
    # 挑一个体积适中的文件做全量扫描，避免 sharedStrings 撑爆内存
    candidates = [f for f in files if 25 * 1024**2 < f.stat().st_size < 45 * 1024**2]
    target = candidates[0] if candidates else min(files, key=lambda f: f.stat().st_size)
    probe_file(target)


if __name__ == "__main__":
    main()
