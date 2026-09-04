# -*- coding: utf-8 -*-
"""修复直辖市/省直辖区划别名导致的街道与坐标缺口。

默认顺序（先出图、再精细）：
1) 区县中心坐标回填（不依赖 street）
2) 地址解析回填 street
3) 再跑一轮坐标（优先街道中心）

用法：
  python etl/repair_muni_geo.py
  python etl/repair_muni_geo.py --provinces 北京市,上海市
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_PROVINCES = ["北京市", "天津市", "上海市", "重庆市"]


def run(cmd: list[str], title: str) -> int:
    print(f"\n===== {title} =====", flush=True)
    # 保证子进程日志即时落盘
    if cmd and cmd[0] == sys.executable and "-u" not in cmd[:3]:
        cmd = [cmd[0], "-u", *cmd[1:]]
    print("+", " ".join(cmd), flush=True)
    p = subprocess.run(cmd, cwd=str(ROOT))
    if p.returncode != 0:
        print(f"失败 exit={p.returncode}: {title}", flush=True)
    return p.returncode


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provinces", default=",".join(DEFAULT_PROVINCES))
    ap.add_argument("--skip-street", action="store_true")
    ap.add_argument("--skip-geo", action="store_true")
    ap.add_argument("--limit", type=int, default=400000)
    args = ap.parse_args()
    provinces = [p.strip() for p in args.provinces.split(",") if p.strip()]
    py = sys.executable

    # 分省处理，避免有手机省份占满 LIMIT 导致京/津迟迟轮不到
    for prov in provinces:
        print(f"\n######## 处理 {prov} ########", flush=True)
        if not args.skip_geo:
            cmd = [
                py, "etl/geocode_by_street.py",
                "--loop", "--include-no-street",
                "--limit", str(args.limit),
                "--batch", "8000",
                "--max-rounds", "40",
                "--province", prov,
            ]
            if run(cmd, f"{prov} · 区县中心坐标回填") != 0:
                sys.exit(1)

        if not args.skip_street:
            cmd = [py, "etl/backfill_street.py", "--only-empty", "--province", prov]
            if run(cmd, f"{prov} · 回填街道") != 0:
                sys.exit(1)

        if not args.skip_geo and not args.skip_street:
            cmd = [
                py, "etl/geocode_by_street.py",
                "--loop",
                "--limit", str(args.limit),
                "--batch", "8000",
                "--max-rounds", "40",
                "--province", prov,
            ]
            if run(cmd, f"{prov} · 街道中心坐标精修") != 0:
                sys.exit(1)

    print("\n完成。", flush=True)


if __name__ == "__main__":
    main()
