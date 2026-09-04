# -*- coding: utf-8 -*-
"""回修注册资本异常（全量）。

1) 用修正后的 parse_capital 重算（万单位但折合≥1万亿 → 按元）
2) ≥1000 亿且非央企白名单 → 强制按元（私人公司「几千亿」多为脏数据）
3) ≥100 亿且规模=微型、或名称含工会/营业厅等 → 强制按元

用法：
  python etl/repair_capital.py --dry-run
  python etl/repair_capital.py
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import app.doris_client as dc
from etl.load_jzh import parse_capital, CAPITAL_RE, FX

COLS = ["credit_code", "capital_wan", "capital_currency", "fill_score"]

SOE_RE = re.compile(
    r"(国家电网|中央汇金|中国石油|中国石化|中国海油|国家开发银行|中国铁路|"
    r"中国烟草|中国移动|中国电信|中国联通|工商银行|建设银行|农业银行|"
    r"中国银行股份|交通银行|国家能源|南方电网|中国华能|华能集团|大唐集团|"
    r"中国华电|华电集团|国家电投|中核集团|航天科技|航天科工|航空工业|"
    r"中国中车|中国建筑|中国中铁|中国铁建|中国交建|中国电建|中国能建|"
    r"中国铝业|中国宝武|招商局|中粮集团|保利集团|华润集团|"
    r"中国人寿|中国平安|中国人保|中国邮政|全国社保|国家管网|"
    r"国家石油天然气|中国投资有限责任|国家集成电路|产业投资基金|"
    r"山东高速|中国融通|中国诚通|中国国新|中国烟草总公司|"
    r"中国铁路投资|中国铁路发展基金|中国工商银行|中国农业银行|"
    r"中国建设银行|中国银行有限)"
)

BRANCH_NOISE_RE = re.compile(
    r"(工会|委员会|营业厅|小学|中学|幼儿园|学校|体育馆|卫生院|村委会|居委会|"
    r"党支部|派出所|养护段|用水协会)"
)


def force_as_yuan(raw: str) -> tuple[float | None, str | None]:
    m = CAPITAL_RE.search((raw or "").strip().replace(",", ""))
    if not m:
        return None, None
    try:
        num = float(m.group(1))
    except ValueError:
        return None, None
    cur = m.group(3) or "人民币"
    wan = num * 0.0001 * FX.get(cur, 1.0)
    if wan > 5e8:
        return None, cur
    return round(wan, 2), cur


def decide(row: dict) -> tuple[float | None, str | None, str] | None:
    """返回 (new_wan, new_cur, reason)；无需改则 None。

    只允许把异常大额改小/置空，禁止把已纠偏的值再放大回去。
    """
    raw = row.get("capital_raw") or ""
    old = float(row["capital_wan"]) if row.get("capital_wan") is not None else None
    name = row.get("company_name") or ""
    scale = (row.get("scale") or "").strip()
    is_soe = bool(SOE_RE.search(name))

    new_wan, new_cur = parse_capital(raw, name)
    reason = "parse_capital"

    # ≥1000亿：非白名单，或规模为小/微，强制按元
    if old is not None and old >= 10_000_000 and (not is_soe or scale in {"小型", "微型"}):
        fw, fc = force_as_yuan(raw)
        if fw is not None:
            new_wan, new_cur, reason = fw, fc, "ge1000yi_non_soe"

    # ≥100亿：微型，或工会/营业厅/学校等挂靠名
    if old is not None and old >= 1_000_000 and not is_soe:
        if scale == "微型" or BRANCH_NOISE_RE.search(name):
            fw, fc = force_as_yuan(raw)
            if fw is not None:
                new_wan, new_cur, reason = fw, fc, "micro_or_branch"

    # 工会/学校/营业厅等：原文「≥100万 + 万」一律按元（避免挂靠上级资本）
    if not is_soe and BRANCH_NOISE_RE.search(name):
        m = CAPITAL_RE.search(raw.strip().replace(",", ""))
        if m and (m.group(2) or "") == "万":
            try:
                num = float(m.group(1))
            except ValueError:
                num = 0
            if num >= 1_000_000:
                fw, fc = force_as_yuan(raw)
                if fw is not None:
                    new_wan, new_cur, reason = fw, fc, "branch_as_yuan"

    if new_wan is None:
        if old is not None and old >= 1_000_000 and not is_soe:
            return None, new_cur, "nullify"
        return None

    if old is not None and abs(old - new_wan) < 0.02:
        return None
    # 禁止放大：避免把已按元纠偏的值又改回「万」
    if old is not None and new_wan > old + 0.02:
        return None
    if is_soe and old is not None and new_wan < old * 0.5 and scale not in {"小型", "微型"}:
        return None
    return new_wan, new_cur, reason


def fetch_page(limit: int, offset: int) -> list[dict]:
    return dc.query(f"""
        SELECT credit_code, company_name, capital_raw, capital_wan, capital_currency,
               IFNULL(scale,'') AS scale, IFNULL(fill_score,0) AS fill_score
        FROM companies
        WHERE capital_raw IS NOT NULL AND capital_raw != ''
          AND capital_wan IS NOT NULL
          AND (
            capital_wan >= 1000000
            OR capital_raw REGEXP '^[0-9]{{7,}}(\\\\.[0-9]+)?万'
          )
        ORDER BY credit_code
        LIMIT {int(limit)} OFFSET {int(offset)}
    """)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--page", type=int, default=50000)
    args = ap.parse_args()

    if not dc.wait_ready(timeout=60):
        print("Doris 未就绪")
        sys.exit(1)

    offset = scanned = changed = 0
    reasons: dict[str, int] = {}
    buf: list[list] = []
    samples: list[str] = []
    t0 = time.time()

    def flush() -> None:
        nonlocal buf
        if not buf or args.dry_run:
            buf = []
            return
        label = f"fix_cap_{int(time.time())}_{len(buf)}"
        js = dc.stream_load(
            "companies", COLS, dc.make_csv(buf),
            label=label, partial_columns=True, max_filter_ratio=0.05,
        )
        status = str(js.get("Status", "")).lower()
        print(f"  flush {len(buf)} → {status}", flush=True)
        if status not in {"success", "publish timeout"}:
            raise RuntimeError(js)
        buf = []

    while True:
        rows = fetch_page(args.page, offset)
        if not rows:
            break
        for r in rows:
            scanned += 1
            hit = decide(r)
            if not hit:
                continue
            new_wan, new_cur, reason = hit
            reasons[reason] = reasons.get(reason, 0) + 1
            buf.append([
                r["credit_code"],
                new_wan,
                new_cur or r.get("capital_currency"),
                int(r["fill_score"] or 0) + 1,
            ])
            changed += 1
            if len(samples) < 12:
                old = r.get("capital_wan")
                samples.append(
                    f"[{reason}] {r.get('capital_raw')} | {old} → {new_wan} | "
                    f"{(r.get('company_name') or '')[:28]}"
                )
            if len(buf) >= args.batch:
                flush()
        offset += len(rows)
        print(f"  scanned {scanned:,} changed {changed:,}", flush=True)
        if len(rows) < args.page:
            break

    flush()
    print(f"\n完成 scanned={scanned:,} changed={changed:,} reasons={reasons} "
          f"dry_run={args.dry_run} 耗时 {(time.time()-t0)/60:.1f} 分")
    for s in samples:
        print("  例:", s)

    print("\n回修后 capital_wan Top10：")
    for r in dc.query("""
        SELECT company_name, capital_raw, capital_wan, scale
        FROM companies WHERE capital_wan IS NOT NULL
        ORDER BY capital_wan DESC LIMIT 10
    """):
        print(
            f"  {float(r['capital_wan'])/10000:.2f}亿 | {r['scale']} | "
            f"{r['capital_raw']} | {(r['company_name'] or '')[:32]}"
        )

    buckets = dc.query("""
        SELECT
          SUM(CASE WHEN capital_wan >= 100000000 THEN 1 ELSE 0 END) ge1wyi,
          SUM(CASE WHEN capital_wan >= 10000000 AND capital_wan < 100000000 THEN 1 ELSE 0 END) lt1wyi,
          SUM(CASE WHEN capital_wan >= 1000000 AND capital_wan < 10000000 THEN 1 ELSE 0 END) lt1000yi
        FROM companies
    """)[0]
    print("\n分桶剩余:", {k: int(v or 0) for k, v in buckets.items()})


if __name__ == "__main__":
    main()
