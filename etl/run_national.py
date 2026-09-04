# -*- coding: utf-8 -*-
"""全国工商入库流水线：区划库 → 导入 → 街道回填 → 坐标回填。

用法：
  python etl/run_national.py --dry-run          # 只看待处理文件数
  python etl/run_national.py --step admin       # 仅拉全国标准区划
  python etl/run_national.py --step import      # 仅导入（跳过江浙沪皖）
  python etl/run_national.py --step street      # 仅街道回填
  python etl/run_national.py --step geo         # 仅坐标回填
  python etl/run_national.py                      # 全流程（import 后 street/geo 需另跑或 --follow）
  python etl/run_national.py --follow             # 全流程且 import 完成后自动 street + geo
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import app.doris_client as dc
from etl.load_jzh import ROOT, STATE, discover_province_folders, JZH_GROUPS, state_key


def run(cmd: list[str], label: str) -> int:
    print(f"\n{'='*60}\n>>> {label}\n    {' '.join(cmd)}\n{'='*60}", flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


def pending_import_count(skip_jzh: bool) -> tuple[int, int]:
    groups = discover_province_folders()
    if skip_jzh:
        groups -= JZH_GROUPS
    files = [f for f in ROOT.glob("*/*.xlsx") if f.parent.name in groups]
    pending = [f for f in files if not (STATE / f"{state_key(f)}.json").exists()]
    return len(files), len(pending)


def main() -> None:
    ap = argparse.ArgumentParser(description="全国工商入库流水线")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--follow", action="store_true",
                    help="import 完成后自动跑 street + geo")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--step", choices=["admin", "import", "street", "geo", "all"], default="all")
    ap.add_argument("--skip-jzh", action="store_true", default=True,
                    help="跳过江浙沪皖（默认开启）")
    ap.add_argument("--no-skip-jzh", action="store_true", help="重新导入江浙沪皖")
    args = ap.parse_args()
    skip_jzh = args.skip_jzh and not args.no_skip_jzh

    total, pending = pending_import_count(skip_jzh)
    print(f"数据根目录: {ROOT}")
    print(f"匹配文件 {total} 个，待导入 {pending} 个"
          + ("（已排除江浙沪皖）" if skip_jzh else ""))

    if args.dry_run:
        if dc.wait_ready(timeout=10):
            n = dc.query("SELECT COUNT(*) AS n FROM companies")[0]["n"]
            g = dc.query("SELECT COUNT(*) AS n FROM companies WHERE lng IS NOT NULL")[0]["n"]
            print(f"当前 companies {n:,}，已定位 {g:,}")
        return

    if not dc.wait_ready(timeout=60):
        print("Doris 未就绪")
        sys.exit(1)

    py = sys.executable
    steps = [args.step] if args.step != "all" else ["admin", "import"]
    if args.step == "all" and args.follow:
        steps = ["admin", "import", "street", "geo"]

    for step in steps:
        if step == "admin":
            rc = run([py, "etl/load_admin_amap.py", "--all"], "1/4 拉取全国标准区划（含街道中心点）")
            if rc != 0:
                sys.exit(rc)
        elif step == "import":
            cmd = [py, "etl/load_jzh.py", "--all", "--workers", str(args.workers)]
            if skip_jzh:
                cmd.append("--skip-jzh")
            rc = run(cmd, "2/4 全国工商清洗入库（断点续跑）")
            if rc != 0:
                sys.exit(rc)
        elif step == "street":
            rc = run([py, "etl/backfill_street.py"], "3/4 标准库街道回填")
            if rc != 0:
                sys.exit(rc)
        elif step == "geo":
            rc = run([
                py, "etl/geocode_by_street.py",
                "--loop", "--include-no-street",
                "--limit", "500000", "--batch", "8000", "--max-rounds", "300",
            ], "4/4 街道/区县中心坐标回填")
            if rc != 0:
                sys.exit(rc)

    print("\n流水线阶段完成。")
    if args.step == "all" and not args.follow:
        print("提示：import 完成后请执行：")
        print("  python etl/run_national.py --step street")
        print("  python etl/run_national.py --step geo")


if __name__ == "__main__":
    main()
