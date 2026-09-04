# -*- coding: utf-8 -*-
"""工商数据质量扫描（多数据集）。

产出：真实行数、去重后主体数、字段非空率、经营状态与地域分布、
触达能力分档（有手机/座机/邮箱）、与现有线索库的重叠率，以及建表所需的长度分布。

支持断点续跑：每个文件的结果缓存在 output/quality_cache/<数据集>/ 下。

用法：
  python quality_scan.py --dataset jzh              # 江浙沪皖 33 列（默认）
  python quality_scan.py --dataset jzh --provinces 上海,江苏,浙江
  python quality_scan.py --dataset national2023     # 2023.11 全国 24 列
  python quality_scan.py --dataset jzh --limit 5    # 只扫最小的 5 个文件做验证
  python quality_scan.py --dataset jzh --report-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zipfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

from xlsx_stream import iter_rows, load_shared_strings, sheet_map

PLATFORM_DIR = Path(__file__).resolve().parent
OUT = PLATFORM_DIR / "output"
LEADS_DB = OUT / "leads.db"

JZH_COLUMNS = [
    "公司名称", "登记状态", "法定代表人", "企业规模", "注册资本", "实缴资本",
    "成立日期", "核准日期", "营业期限", "所属省份", "所属城市", "所属区县",
    "公司类型", "国标行业门类", "国标行业大类", "国标行业中类", "曾用名", "英文名",
    "统一社会信用代码", "纳税人识别号", "注册号", "组织机构代码", "参保人数",
    "参保人数所属年报", "有效手机号", "更多电话", "注册地址", "最新年报地址",
    "通信地址", "网址", "邮箱", "其他邮箱", "经营范围",
]

NAT_COLUMNS = [
    "企业名称", "经营状态", "法定代表人", "注册资本", "实缴资本", "成立日期",
    "核准日期", "营业期限", "所属省份", "所属城市", "所属区县", "统一社会信用代码",
    "纳税人识别号", "工商注册号", "组织机构代码", "参保人数", "企业类型", "所属行业",
    "曾用名", "注册地址", "网址", "联系电话", "邮箱", "经营范围",
]

DATASETS: dict[str, dict] = {
    "jzh": {
        "label": "江浙沪皖全量工商（33 列，数据截至 2024.11）",
        "root": Path(r"F:\企查查大数据\全国所有企业工商信息(1)"),
        "pattern": "*/*.xlsx",  # 只取子目录，跳过根目录无表头的 2025.04月.xlsx
        "columns": JZH_COLUMNS,
        "map": {
            "name": "公司名称", "status": "登记状态", "legal": "法定代表人",
            "scale": "企业规模", "capital": "注册资本", "established": "成立日期",
            "province": "所属省份", "city": "所属城市", "district": "所属区县",
            "credit": "统一社会信用代码", "type": "公司类型",
            "industry": "国标行业门类", "address": "注册地址",
            "scope": "经营范围", "insured": "参保人数",
        },
        "mobile": ["有效手机号"],
        "landline": ["更多电话"],
        "email": ["邮箱", "其他邮箱"],
    },
    "national2023": {
        "label": "全国工商全量（24 列，数据截至 2023.11）",
        "root": Path(r"F:\企查查大数据\工商注册企业全量信息（更新2023.11）\全国数据"),
        "pattern": "*.xlsx",
        "columns": NAT_COLUMNS,
        "map": {
            "name": "企业名称", "status": "经营状态", "legal": "法定代表人",
            "scale": None, "capital": "注册资本", "established": "成立日期",
            "province": "所属省份", "city": "所属城市", "district": "所属区县",
            "credit": "统一社会信用代码", "type": "企业类型",
            "industry": "所属行业", "address": "注册地址",
            "scope": "经营范围", "insured": "参保人数",
        },
        "mobile": [],
        "landline": ["联系电话"],
        "email": ["邮箱"],
    },
}

EMPTY = {"", "-", "--", "—", "–", "无", "null", "none", "n/a", "na", "/", "\\", "暂无", "未知"}
CREDIT_RE = re.compile(r"^[0-9A-Z]{18}$")
CAPITAL_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(万|亿)?\s*([\u4e00-\u9fa5]{2,4})?")
MULTI_SPLIT = re.compile(r"[,，;；、\s|]+")
MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")

LEN_BUCKETS = [0, 1, 50, 100, 200, 400, 800, 1600]
CAP_BUCKETS = [0, 10, 50, 100, 500, 1000, 5000, 10000]

FX = {
    "人民币": 1.0, "美元": 7.2, "美金": 7.2, "港元": 0.92, "港币": 0.92,
    "欧元": 8.0, "日元": 0.048, "英镑": 9.5, "新加坡元": 5.5, "澳元": 4.7,
    "加元": 5.3, "韩元": 0.0052, "台币": 0.23, "新台币": 0.23,
}


def is_empty(v: str) -> bool:
    return v.strip().lower() in EMPTY


def bucket_of(n: int, edges: list[int]) -> str:
    for i in range(len(edges) - 1, -1, -1):
        if n >= edges[i]:
            hi = edges[i + 1] if i + 1 < len(edges) else None
            return f">={edges[i]}" if hi is None else f"{edges[i]}-{hi - 1}"
    return "0"


def parse_capital_wan(raw: str) -> float | None:
    s = raw.strip().replace(",", "")
    if is_empty(s):
        return None
    m = CAPITAL_RE.search(s)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    mult = {"万": 1.0, "亿": 10000.0}.get(m.group(2) or "", 0.0001)
    return num * mult * FX.get(m.group(3) or "人民币", 1.0)


def split_multi(raw: str) -> list[str]:
    if is_empty(raw):
        return []
    return [p for p in MULTI_SPLIT.split(raw.strip()) if p and not is_empty(p)]


def cache_dir(ds_key: str) -> Path:
    d = OUT / "quality_cache" / ds_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_key(path: Path) -> str:
    return f"{path.parent.name}__{path.stem}__{path.stat().st_size}"


def scan_one(args: tuple[str, str]) -> dict:
    """worker：扫描一个 xlsx，写出信用代码数组，返回统计摘要。"""
    path_str, ds_key = args
    path = Path(path_str)
    ds = DATASETS[ds_key]
    cd = cache_dir(ds_key)
    key = cache_key(path)
    npy_path = cd / f"{key}.npy"
    json_path = cd / f"{key}.json"
    if json_path.exists() and npy_path.exists():
        return json.loads(json_path.read_text(encoding="utf-8"))

    cols = ds["columns"]
    m = ds["map"]
    t0 = time.time()
    st = {
        "file": path.name,
        "group": path.parent.name,
        "size_mb": round(path.stat().st_size / 1024**2, 1),
        "rows": 0,
        "credit_valid": 0,
        "credit_empty": 0,
        "credit_malformed": 0,
    }
    nonempty = Counter()
    nbytes = Counter()
    c = {k: Counter() for k in (
        "status", "province", "city", "type", "industry", "est_year",
        "scope_len", "addr_len", "capital_wan", "currency", "scale",
        "reach", "mobile_cnt", "insured",
    )}
    codes: list[bytes] = []

    with zipfile.ZipFile(path) as z:
        sst = load_shared_strings(z)
        mapping = sheet_map(z)
        if not mapping:
            st["error"] = "no sheet"
            return st
        rows = iter_rows(z, next(iter(mapping.values())), sst)
        try:
            header = next(rows)
        except StopIteration:
            st["error"] = "empty sheet"
            return st
        idx = {h.strip(): i for i, h in enumerate(header) if h and h.strip()}
        miss = [x for x in cols if x not in idx]
        if miss:
            st["missing_columns"] = miss
        pos = {x: idx.get(x, -1) for x in cols}

        for row in rows:
            n = len(row)
            st["rows"] += 1

            def val(col: str | None) -> str:
                if not col:
                    return ""
                i = pos.get(col, -1)
                return row[i].strip() if 0 <= i < n else ""

            for col in cols:
                i = pos[col]
                if i < 0 or i >= n:
                    continue
                v = row[i]
                if not v:
                    continue
                nbytes[col] += len(v.encode("utf-8"))
                if not is_empty(v):
                    nonempty[col] += 1

            code = val(m["credit"]).upper().replace(" ", "")
            if not code or is_empty(code):
                st["credit_empty"] += 1
            elif CREDIT_RE.match(code):
                st["credit_valid"] += 1
                codes.append(code.encode("ascii"))
            else:
                st["credit_malformed"] += 1

            for key_, col in (("status", m["status"]), ("province", m["province"]),
                              ("type", m["type"]), ("industry", m["industry"]),
                              ("scale", m["scale"])):
                v = val(col)
                if v and not is_empty(v):
                    c[key_][v] += 1
            prov, city = val(m["province"]), val(m["city"])
            if city and not is_empty(city):
                c["city"][f"{prov}|{city}"] += 1

            est = val(m["established"])
            if len(est) >= 4 and est[:4].isdigit():
                c["est_year"][est[:4]] += 1

            scope = val(m["scope"])
            c["scope_len"][bucket_of(0 if is_empty(scope) else len(scope), LEN_BUCKETS)] += 1
            addr = val(m["address"])
            c["addr_len"][bucket_of(0 if is_empty(addr) else len(addr), LEN_BUCKETS)] += 1

            cap_raw = val(m["capital"])
            wan = parse_capital_wan(cap_raw)
            c["capital_wan"]["未知" if wan is None else bucket_of(int(wan), CAP_BUCKETS)] += 1
            mc = CAPITAL_RE.search(cap_raw.replace(",", "")) if not is_empty(cap_raw) else None
            c["currency"][(mc.group(3) or "人民币") if mc else "未填"] += 1

            ins = val(m["insured"])
            if ins and not is_empty(ins) and ins.isdigit():
                v = int(ins)
                c["insured"]["0" if v == 0 else bucket_of(v, [1, 5, 20, 50, 200, 1000])] += 1
            else:
                c["insured"]["未知"] += 1

            mobiles: list[str] = []
            for col in ds["mobile"]:
                mobiles.extend(p for p in split_multi(val(col)) if MOBILE_RE.match(p))
            has_land = any(split_multi(val(col)) for col in ds["landline"])
            has_mail = any(split_multi(val(col)) for col in ds["email"])
            has_mob = bool(mobiles)
            c["mobile_cnt"][str(min(len(mobiles), 5))] += 1
            if has_mob:
                c["reach"]["有手机"] += 1
            if has_land:
                c["reach"]["有座机"] += 1
            if has_mail:
                c["reach"]["有邮箱"] += 1
            if has_mob or has_land:
                c["reach"]["有电话"] += 1
            if has_mob or has_land or has_mail:
                c["reach"]["有任一"] += 1
            else:
                c["reach"]["无任何"] += 1
            if has_mob and has_mail:
                c["reach"]["手机+邮箱"] += 1

    st["nonempty"] = dict(nonempty)
    st["bytes"] = dict(nbytes)
    st["status"] = dict(c["status"].most_common(60))
    st["province"] = dict(c["province"])
    st["city"] = dict(c["city"].most_common(400))
    st["company_type"] = dict(c["type"].most_common(200))
    st["industry"] = dict(c["industry"].most_common(300))
    st["scale"] = dict(c["scale"])
    st["est_year"] = dict(c["est_year"])
    st["scope_len"] = dict(c["scope_len"])
    st["addr_len"] = dict(c["addr_len"])
    st["capital_wan"] = dict(c["capital_wan"])
    st["capital_currency"] = dict(c["currency"].most_common(40))
    st["insured"] = dict(c["insured"])
    st["reach"] = dict(c["reach"])
    st["mobile_cnt"] = dict(c["mobile_cnt"])
    st["elapsed_s"] = round(time.time() - t0, 1)

    np.save(npy_path, np.array(codes, dtype="S18") if codes else np.empty(0, dtype="S18"))
    json_path.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return st


def merge(dst: Counter, src: dict | None) -> None:
    if src:
        for k, v in src.items():
            dst[k] += v


def run(ds_key: str, workers: int, limit: int, report_only: bool, provinces: list[str]) -> None:
    ds = DATASETS[ds_key]
    root: Path = ds["root"]
    if not root.exists():
        print(f"数据集根目录不存在：{root}")
        sys.exit(1)

    files = sorted(root.glob(ds["pattern"]))
    if provinces:
        files = [f for f in files if any(p in f.parent.name or p in f.name for p in provinces)]
    if limit:
        files = sorted(files, key=lambda f: f.stat().st_size)[:limit]
    else:
        files = sorted(files, key=lambda f: -f.stat().st_size)
    if not files:
        print("没有匹配到文件")
        sys.exit(1)

    cd = cache_dir(ds_key)
    total_gb = sum(f.stat().st_size for f in files) / 1024**3
    done = {f for f in files if (cd / f"{cache_key(f)}.json").exists()}
    todo = [f for f in files if f not in done]
    print(f"数据集：{ds['label']}")
    print(f"根目录：{root}")
    if provinces:
        print(f"省份过滤：{'、'.join(provinces)}")
    print(f"文件 {len(files)} 个｜{total_gb:.2f} GB｜已缓存 {len(done)}｜待扫 {len(todo)}\n")

    if not report_only and todo:
        t0 = time.time()
        rows_done = 0
        fin = len(done)
        todo_gb = sum(f.stat().st_size for f in todo) / 1024**3
        gb_done = 0.0
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(scan_one, (str(f), ds_key)): f for f in todo}
            for fut in as_completed(futs):
                f = futs[fut]
                fin += 1
                try:
                    st = fut.result()
                except Exception as e:
                    print(f"  !! {f.name} 失败：{e}")
                    continue
                rows_done += st["rows"]
                gb_done += f.stat().st_size / 1024**3
                el = time.time() - t0
                eta = (todo_gb - gb_done) / (gb_done / el) / 60 if gb_done > 0 else 0
                print(
                    f"  [{fin}/{len(files)}] {st['group'][:10]:<12}{st['file'][:24]:<26}"
                    f" {st['rows']:>9,} 行 {st['elapsed_s']:>5.1f}s"
                    f"   累计 {rows_done:>11,} 行   ETA {eta:.1f} 分钟",
                    flush=True,
                )
        print(f"\n扫描耗时 {(time.time() - t0) / 60:.1f} 分钟")

    report(ds_key, files)


def report(ds_key: str, files: list[Path]) -> None:
    ds = DATASETS[ds_key]
    cols = ds["columns"]
    cd = cache_dir(ds_key)
    print("\n汇总统计…")

    tot = {"files": 0, "rows": 0, "credit_valid": 0, "credit_empty": 0, "credit_malformed": 0}
    nonempty, nbytes = Counter(), Counter()
    agg = {k: Counter() for k in (
        "status", "province", "city", "company_type", "industry", "est_year",
        "scope_len", "addr_len", "capital_wan", "capital_currency", "scale",
        "reach", "mobile_cnt", "insured",
    )}
    by_group: dict[str, Counter] = {}

    for f in files:
        jp = cd / f"{cache_key(f)}.json"
        if not jp.exists():
            continue
        st = json.loads(jp.read_text(encoding="utf-8"))
        tot["files"] += 1
        for k in ("rows", "credit_valid", "credit_empty", "credit_malformed"):
            tot[k] += st.get(k, 0)
        merge(nonempty, st.get("nonempty"))
        merge(nbytes, st.get("bytes"))
        for k in agg:
            merge(agg[k], st.get(k))
        g = by_group.setdefault(st.get("group", "?"), Counter())
        g["rows"] += st.get("rows", 0)
        for rk, rv in (st.get("reach") or {}).items():
            g[rk] += rv

    print("合并信用代码做精确去重…")
    parts = [np.load(cd / f"{cache_key(f)}.npy")
             for f in files if (cd / f"{cache_key(f)}.npy").exists()]
    all_codes = np.concatenate(parts) if parts else np.empty(0, dtype="S18")
    del parts
    uniq = np.unique(all_codes)
    dup = len(all_codes) - len(uniq)
    print(f"  信用代码 {len(all_codes):,} 条，去重后 {len(uniq):,}，重复 {dup:,}")

    overlap = None
    if LEADS_DB.exists():
        import sqlite3
        conn = sqlite3.connect(f"file:{LEADS_DB}?mode=ro", uri=True)
        leads = [r[0].encode("ascii") for r in
                 conn.execute("SELECT credit_code FROM companies WHERE credit_code != ''")
                 if r[0] and CREDIT_RE.match(r[0])]
        conn.close()
        la = np.unique(np.array(leads, dtype="S18"))
        inter = np.intersect1d(uniq, la, assume_unique=True)
        overlap = {
            "leads_total": int(len(la)), "matched": int(len(inter)),
            "coverage_pct": round(len(inter) / max(len(la), 1) * 100, 2),
        }
        print(f"  现有线索库 {len(la):,} 个主体，命中 {len(inter):,}（{overlap['coverage_pct']}%）")

    rows = max(tot["rows"], 1)
    total_bytes = sum(nbytes.values())
    payload = {
        "dataset": ds_key,
        "label": ds["label"],
        "files": tot["files"],
        "rows": tot["rows"],
        "unique_entities": int(len(uniq)),
        "duplicate_rows": int(dup),
        "credit": {k: tot[f"credit_{k}"] for k in ("valid", "empty", "malformed")},
        "avg_bytes_per_row": round(total_bytes / rows, 1),
        "total_raw_gb": round(total_bytes / 1024**3, 2),
        "nonempty_pct": {c: round(nonempty[c] / rows * 100, 2) for c in cols},
        "avg_bytes_per_col": {c: round(nbytes[c] / rows, 1) for c in cols},
        "reach": dict(agg["reach"]),
        "reach_pct": {k: round(v / rows * 100, 2) for k, v in agg["reach"].items()},
        "mobile_cnt": dict(agg["mobile_cnt"]),
        "by_group": {g: dict(v) for g, v in by_group.items()},
        "status": dict(agg["status"].most_common(30)),
        "province": dict(agg["province"].most_common()),
        "city_top50": dict(agg["city"].most_common(50)),
        "scale": dict(agg["scale"]),
        "company_type_top30": dict(agg["company_type"].most_common(30)),
        "industry_top40": dict(agg["industry"].most_common(40)),
        "est_year": dict(sorted(agg["est_year"].items())),
        "scope_len": dict(agg["scope_len"]),
        "addr_len": dict(agg["addr_len"]),
        "capital_wan": dict(agg["capital_wan"]),
        "capital_currency": dict(agg["capital_currency"].most_common(15)),
        "insured": dict(agg["insured"]),
        "overlap_with_leads": overlap,
    }
    jp = OUT / f"quality_report_{ds_key}.json"
    tp = OUT / f"quality_report_{ds_key}.txt"
    jp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    text = render(payload, cols)
    tp.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n报告：\n  {jp}\n  {tp}")


def render(p: dict, cols: list[str]) -> str:
    L: list[str] = []
    add = L.append
    rows = max(p["rows"], 1)
    add("=" * 74)
    add(f"数据质量报告 · {p['label']}")
    add("=" * 74)
    add(f"文件数            {p['files']:,}")
    add(f"数据行数          {p['rows']:,}")
    add(f"去重后主体数      {p['unique_entities']:,}")
    add(f"跨文件重复行      {p['duplicate_rows']:,}（{p['duplicate_rows'] / rows * 100:.2f}%）")
    add(f"平均字节/行       {p['avg_bytes_per_row']:,.0f}")
    add(f"原始文本总量      {p['total_raw_gb']:,.2f} GB")
    add(f"信用代码          合规 {p['credit']['valid']:,}｜缺失 {p['credit']['empty']:,}"
        f"｜异常 {p['credit']['malformed']:,}")
    if p["overlap_with_leads"]:
        o = p["overlap_with_leads"]
        add(f"与现有线索库      {o['leads_total']:,} 个主体中命中 {o['matched']:,}"
            f"（{o['coverage_pct']}%）")
    add("")
    add("-" * 74)
    add("触达能力（决定这批数据能不能直接派给销售）")
    add("-" * 74)
    for k in ("有手机", "有座机", "有电话", "有邮箱", "手机+邮箱", "有任一", "无任何"):
        v = p["reach"].get(k, 0)
        add(f"  {k:<10} {v:>12,}   {v / rows * 100:>6.2f}%")
    add("")
    add("  每家手机号个数：" + "｜".join(
        f"{k}个 {v:,}" for k, v in sorted(p["mobile_cnt"].items())))
    add("")
    if p["by_group"]:
        add("-" * 74)
        add("分省触达率")
        add("-" * 74)
        add(f"  {'省份目录':<22}{'行数':>12}{'有电话':>11}{'有邮箱':>11}{'有任一':>11}")
        for g, v in sorted(p["by_group"].items(), key=lambda kv: -kv[1]["rows"]):
            r = max(v["rows"], 1)
            add(f"  {g[:20]:<22}{v['rows']:>12,}"
                f"{v.get('有电话', 0) / r * 100:>10.1f}%"
                f"{v.get('有邮箱', 0) / r * 100:>10.1f}%"
                f"{v.get('有任一', 0) / r * 100:>10.1f}%")
        add("")
    add("-" * 74)
    add("字段非空率 / 平均字节")
    add("-" * 74)
    for c in cols:
        add(f"  {c:<16} {p['nonempty_pct'][c]:>7.2f}%   {p['avg_bytes_per_col'][c]:>8.1f} B")
    add("")
    for title, key, top in (
        ("登记状态", "status", 15),
        ("省份", "province", 10),
        ("城市 Top20", "city_top50", 20),
        ("企业规模", "scale", 10),
        ("公司类型 Top15", "company_type_top30", 15),
        ("国标行业门类 Top20", "industry_top40", 20),
        ("参保人数分档", "insured", 12),
        ("经营范围长度（字符）", "scope_len", 12),
        ("注册资本（人民币万元）", "capital_wan", 12),
        ("注册币种", "capital_currency", 8),
    ):
        add("-" * 74)
        add(title)
        add("-" * 74)
        items = list(p.get(key, {}).items())
        if key in ("scope_len", "capital_wan", "insured", "addr_len"):
            items.sort(key=lambda kv: (kv[0] in ("未知", "0"), _sk(kv[0])))
        else:
            items.sort(key=lambda kv: -kv[1])
        for k, v in items[:top]:
            add(f"  {k[:34]:<36} {v:>12,}  {v / rows * 100:>6.2f}%")
        add("")
    add("-" * 74)
    add("成立年份分布（近 25 年）")
    add("-" * 74)
    for y, v in sorted(p["est_year"].items())[-25:]:
        add(f"  {y}  {v:>12,}  {v / rows * 100:>6.2f}%")
    return "\n".join(L)


def _sk(label: str) -> float:
    m = re.match(r">?=?(\d+)", label)
    return float(m.group(1)) if m else 1e18


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="jzh", choices=list(DATASETS))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--provinces", default="", help="逗号分隔，按目录名/文件名过滤")
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()
    provs = [x.strip() for x in a.provinces.split(",") if x.strip()]
    run(a.dataset, max(1, min(a.workers, os.cpu_count() or 4)), a.limit, a.report_only, provs)


if __name__ == "__main__":
    main()
