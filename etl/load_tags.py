# -*- coding: utf-8 -*-
"""从现有 leads.db / 产业链 xlsx 导入 tags 表。"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import doris_client as dc

LEADS_DB = Path(__file__).resolve().parent.parent / "output" / "leads.db"
BATCH = 5000

TAG_COLS = [
    "credit_code", "company_name", "chain_industries", "chain_nodes",
    "chain_positions", "relatedness", "honors", "financing_raw",
    "tag_finance", "tag_fit", "lead_score",
    "score_reach", "score_attract", "score_fit", "score_pay", "loaded_at",
]


def parse_list(v) -> list | None:
    if not v:
        return None
    if isinstance(v, list):
        return v
    try:
        x = json.loads(v)
        return x if isinstance(x, list) else [str(x)]
    except Exception:
        return [str(v)]


def arr_literal(vals: list | None) -> str | None:
    """Doris ARRAY 经 CSV 导入时用 [\"a\",\"b\"] 文本。"""
    if not vals:
        return None
    inner = ",".join('"' + str(x).replace('"', '\\"') + '"' for x in vals)
    return f"[{inner}]"


def main() -> None:
    if not LEADS_DB.exists():
        print(f"找不到线索库：{LEADS_DB}，跳过 tags 导入")
        return
    if not dc.wait_ready(timeout=60):
        print("Doris 未就绪")
        sys.exit(1)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(f"file:{LEADS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # 尽量兼容现有字段
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    print(f"leads.db 字段数 {len(cols)}")

    cur = conn.execute("SELECT * FROM companies")
    batch, total, loaded = [], 0, 0
    label_i = 0

    def flush():
        nonlocal batch, loaded, label_i
        if not batch:
            return
        label_i += 1
        r = dc.stream_load(
            "tags", TAG_COLS, dc.make_csv(batch),
            label=f"tags_leads_{label_i}",
            max_filter_ratio=0.05,
        )
        loaded += int(r.get("NumberLoadedRows") or 0)
        print(f"  batch {label_i}: {r.get('Status')} loaded={r.get('NumberLoadedRows')}")
        batch = []

    for row in cur:
        total += 1
        d = dict(row)
        code = (d.get("credit_code") or "").strip()
        if not code:
            continue
        batch.append([
            code,
            d.get("company_name"),
            arr_literal(parse_list(d.get("chain_industries"))),
            arr_literal(parse_list(d.get("chain_nodes"))),
            d.get("chain_positions") or d.get("chain_position"),
            d.get("relatedness"),
            arr_literal(parse_list(d.get("honors"))),
            d.get("financing") or d.get("financing_raw"),
            d.get("tag_finance"),
            d.get("tag_fit"),
            d.get("lead_score"),
            d.get("score_reach"),
            d.get("score_attract"),
            d.get("score_fit"),
            d.get("score_pay"),
            now,
        ])
        if len(batch) >= BATCH:
            flush()
    flush()
    conn.close()
    print(f"tags 导入完成：扫描 {total:,} 行，写入 {loaded:,}")


if __name__ == "__main__":
    main()
