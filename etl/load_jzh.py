# -*- coding: utf-8 -*-
"""江浙沪皖 / 全国工商 → Doris。

并行解析 xlsx，逐行清洗后经 Stream Load 写入 companies / contacts。
默认仅江浙沪皖；加 --all 导入全国 32 省文件夹（已导入的断点文件自动跳过）。

用法：
    python etl/load_jzh.py                 # 江浙沪皖全量（断点续跑）
    python etl/load_jzh.py --all           # 全国 32 省（跳过已完成的 jzh 断点）
    python etl/load_jzh.py --all --skip-jzh   # 仅导入非江浙沪皖的 28 省
    python etl/load_jzh.py --limit 3       # 先拿 3 个小文件验证链路
    python etl/load_jzh.py --workers 6
    python etl/load_jzh.py --reset         # 清空断点重来
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import app.doris_client as dc
from app.street_utils import extract_street, load_admin_streets
from scripts.xlsx_stream import iter_rows, load_shared_strings, sheet_map

ROOT = Path(r"F:\企查查大数据\全国所有企业工商信息(1)")
PLATFORM = Path(__file__).resolve().parent.parent
STATE = PLATFORM / "output" / "etl_state" / "jzh"

# 江浙沪皖（已导入批次）
JZH_GROUPS = {
    "江苏所有企业",
    "浙江所有企业",
    "安徽所有企业",
    "上海所有企业-新版",
}

SOURCE_JZH = "jzh2024"
SOURCE_NATIONAL = "national2024"
COLLECTED_AT = "2024-11-01"   # 该批数据的截止时间，用于判断号码新鲜度
TODAY = date.today()

# ---------------------------------------------------------------- 清洗规则（唯一副本在 baize_core，此处 re-export 兼容旧 import）

from baize_core.cleaning import EMPTY, MULTI_SPLIT, clean, is_empty, split_multi, tb
from baize_core.capital import CAPITAL_RE, FX, SOE_CAPITAL_RE, parse_capital
from baize_core.contact import DIGITS_RE, EMAIL_RE, MOBILE_RE, norm_landline, norm_mail_address
from baize_core.dates import DATE_RE, YEAR_ONLY_RE, parse_date
from baize_core.identity import CREDIT_RE, surrogate_credit
from baize_core.region import (
    FOLDER_PROVINCE_RE,
    GROUP_PROVINCE,
    PROVINCE_MAP,
    STATUS_MAP,
    folder_to_province,
)


def discover_province_folders() -> set[str]:
    """全国所有企业根目录下的省级文件夹。"""
    if not ROOT.is_dir():
        return set()
    return {
        d.name for d in ROOT.iterdir()
        if d.is_dir() and list(d.glob("*.xlsx"))
    }

# xlsx 表头 → 内部字段名
COL = {
    "公司名称": "name", "登记状态": "status", "法定代表人": "legal",
    "企业规模": "scale", "注册资本": "capital", "实缴资本": "capital_paid",
    "成立日期": "established", "核准日期": "approved", "营业期限": "term",
    "所属省份": "province", "所属城市": "city", "所属区县": "district",
    "公司类型": "type", "国标行业门类": "ind1", "国标行业大类": "ind2",
    "国标行业中类": "ind3", "曾用名": "former", "英文名": "en_name",
    "统一社会信用代码": "credit", "纳税人识别号": "tax_id", "注册号": "reg_no",
    "组织机构代码": "org_code", "参保人数": "insured", "参保人数所属年报": "insured_year",
    "有效手机号": "mobile", "更多电话": "landline", "注册地址": "address",
    "最新年报地址": "address_report", "通信地址": "address_mail", "网址": "website",
    "邮箱": "email", "其他邮箱": "email2", "经营范围": "scope",
}

COMPANY_COLS = [
    "credit_code", "company_name", "status", "legal_person", "scale",
    "capital_wan", "capital_raw", "capital_currency", "capital_paid_wan",
    "established", "approved", "established_year", "company_age", "term_raw",
    "province", "city", "district", "street", "company_type",
    "industry_l1", "industry_l2", "industry_l3",
    "insured_count", "insured_year", "former_name", "en_name",
    "tax_id", "reg_no", "org_code",
    "address", "address_report", "address_mail", "website", "scope",
    "has_mobile", "has_landline", "has_email", "has_contact",
    "mobile_count", "email_count", "fill_score",
    "src_group", "src_file", "loaded_at",
]
CONTACT_COLS = [
    "credit_code", "contact_type", "contact_value", "source",
    "company_name", "is_primary", "collected_at", "loaded_at",
]


# ---------------------------------------------------------------- 单文件处理

def state_key(path: Path) -> str:
    return f"{path.parent.name}__{path.stem}__{path.stat().st_size}"


def label_for(kind: str, key: str) -> str:
    return f"jzh_{kind}_{hashlib.md5(key.encode('utf-8')).hexdigest()[:20]}"


def process_file(path_str: str) -> dict:
    path = Path(path_str)
    key = state_key(path)
    done = STATE / f"{key}.json"
    if done.exists():
        st = json.loads(done.read_text(encoding="utf-8"))
        st["skipped"] = True
        return st

    t0 = time.time()
    group = path.parent.name
    fallback_prov = folder_to_province(group)
    source = SOURCE_JZH if group in JZH_GROUPS else SOURCE_NATIONAL
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    st = {
        "file": path.name, "group": group, "rows": 0,
        "companies": 0, "contacts": 0,
        "credit_surrogate": 0, "dedup_in_file": 0,
        "with_mobile": 0, "with_landline": 0, "with_email": 0, "with_any": 0,
        "bad_rows": 0,
    }

    best: dict[str, tuple[int, list]] = {}   # credit_code → (fill_score, row)
    contacts: dict[tuple, list] = {}

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

        pos: dict[str, int] = {}
        for i, h in enumerate(header):
            fname = COL.get((h or "").strip())
            if fname and fname not in pos:
                pos[fname] = i

        if "name" not in pos or "credit" not in pos:
            st["error"] = f"missing key columns, header={header[:6]}"
            return st

        for row in rows:
            n = len(row)
            st["rows"] += 1

            def g(f: str) -> str:
                i = pos.get(f, -1)
                return row[i].strip() if 0 <= i < n and row[i] else ""

            name = clean(g("name"))
            if not name:
                st["bad_rows"] += 1
                continue
            name = tb(name, 300)

            code = g("credit").upper().replace(" ", "")
            if not CREDIT_RE.match(code):
                code = surrogate_credit(name)
                st["credit_surrogate"] += 1

            fill = sum(1 for f in pos if not is_empty(g(f)))

            # --- 联系方式：拆分、校验、去重
            mobiles, landlines, emails = [], [], []
            for p in split_multi(g("mobile")):
                d = "".join(DIGITS_RE.findall(p))
                if MOBILE_RE.match(d) and d not in mobiles:
                    mobiles.append(d)
            for p in split_multi(g("landline")):
                d = "".join(DIGITS_RE.findall(p))
                if MOBILE_RE.match(d):
                    if d not in mobiles:
                        mobiles.append(d)
                    continue
                nl = norm_landline(p)
                if nl and nl not in landlines:
                    landlines.append(nl)
            for col in ("email", "email2"):
                for p in split_multi(g(col)):
                    e = p.lower().strip(".,;")
                    if EMAIL_RE.match(e) and e not in emails and len(e) <= 200:
                        emails.append(tb(e, 200))

            has_mob = 1 if mobiles else 0
            has_land = 1 if landlines else 0
            has_mail = 1 if emails else 0
            has_any = 1 if (has_mob or has_land or has_mail) else 0
            st["with_mobile"] += has_mob
            st["with_landline"] += has_land
            st["with_email"] += has_mail
            st["with_any"] += has_any

            for typ, vals in (("mobile", mobiles), ("landline", landlines), ("email", emails)):
                for i, v in enumerate(vals):
                    ck = (code, typ, v)
                    if ck in contacts:
                        continue
                    contacts[ck] = [code, typ, v, source, name,
                                    1 if i == 0 else 0, COLLECTED_AT, now]

            # --- 工商字段
            cap_wan, cap_cur = parse_capital(g("capital"), name)
            paid_wan, _ = parse_capital(g("capital_paid"), name)
            est = parse_date(g("established"))
            est_year = int(est[:4]) if est else None
            age = None
            if est:
                age = round((TODAY - date(int(est[:4]), int(est[5:7]), int(est[8:10]))).days / 365.25, 1)
                if age < 0:
                    age, est_year, est = None, None, None

            prov = PROVINCE_MAP.get((clean(g("province")) or "").strip(), None)
            if not prov:
                raw_p = clean(g("province"))
                prov = raw_p if raw_p else fallback_prov
            status_raw = clean(g("status")) or ""
            status = STATUS_MAP.get(status_raw, status_raw or None)

            insured = None
            iv = clean(g("insured"))
            if iv and iv.isdigit():
                insured = min(int(iv), 2_000_000)

            addr = clean(g("address"))
            city_v = clean(g("city"))
            dist_v = clean(g("district"))
            street = extract_street(prov or "", city_v or "", dist_v or "", addr or "") or None

            crow = [
                code, name, tb(status, 32), tb(clean(g("legal")), 128), tb(clean(g("scale")), 16),
                cap_wan, tb(clean(g("capital")), 64), tb(cap_cur, 16), paid_wan,
                est, parse_date(g("approved")), est_year, age, tb(clean(g("term")), 64),
                tb(prov, 32), tb(city_v, 64), tb(dist_v, 64), tb(street, 64),
                tb(clean(g("type")), 128),
                tb(clean(g("ind1")), 64), tb(clean(g("ind2")), 64), tb(clean(g("ind3")), 64),
                insured, tb(clean(g("insured_year")), 8),
                tb(clean(g("former")), 500), tb(clean(g("en_name")), 500),
                tb(clean(g("tax_id")), 32), tb(clean(g("reg_no")), 32), tb(clean(g("org_code")), 32),
                tb(addr, 500), tb(clean(g("address_report")), 500),
                norm_mail_address(g("address_mail")), tb(clean(g("website")), 300),
                clean(g("scope")),
                has_mob, has_land, has_mail, has_any,
                len(mobiles), len(emails), fill,
                tb(group, 32), tb(path.name, 128), now,
            ]

            prev = best.get(code)
            if prev is None:
                best[code] = (fill, crow)
            else:
                st["dedup_in_file"] += 1
                if fill > prev[0]:
                    best[code] = (fill, crow)

    comp_rows = [v[1] for v in best.values()]
    st["companies"] = len(comp_rows)
    st["contacts"] = len(contacts)
    st["parse_s"] = round(time.time() - t0, 1)

    t1 = time.time()
    if comp_rows:
        r = dc.stream_load("companies", COMPANY_COLS, dc.make_csv(comp_rows),
                           label=label_for("c", key))
        st["load_companies"] = r.get("Status")
        st["filtered_companies"] = r.get("NumberFilteredRows", 0)
    if contacts:
        r = dc.stream_load("contacts", CONTACT_COLS, dc.make_csv(contacts.values()),
                           label=label_for("t", key))
        st["load_contacts"] = r.get("Status")
        st["filtered_contacts"] = r.get("NumberFilteredRows", 0)
    st["load_s"] = round(time.time() - t1, 1)
    st["elapsed_s"] = round(time.time() - t0, 1)

    STATE.mkdir(parents=True, exist_ok=True)
    done.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return st


# ---------------------------------------------------------------- 主流程

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="只处理最小的 N 个文件，用于验证链路")
    ap.add_argument("--reset", action="store_true", help="清空断点状态")
    ap.add_argument("--all", action="store_true", help="导入全国 32 省文件夹（默认仅江浙沪皖）")
    ap.add_argument("--skip-jzh", action="store_true",
                    help="配合 --all：跳过江浙沪皖（已入库），只导其余省份")
    a = ap.parse_args()

    if a.reset and STATE.exists():
        shutil.rmtree(STATE)
        print("已清空断点状态")
    STATE.mkdir(parents=True, exist_ok=True)

    if not dc.wait_ready(timeout=60):
        print("Doris 未就绪，先启动 docker compose")
        sys.exit(1)
    n = load_admin_streets(force=True)
    print(f"标准街道库 {n:,}（导入时按标准名匹配）")

    if a.all:
        groups = discover_province_folders()
        if a.skip_jzh:
            groups -= JZH_GROUPS
        if not groups:
            print("未找到待导入的省级文件夹")
            sys.exit(1)
        print(f"全国模式：{len(groups)} 个省级文件夹"
              + ("（已排除江浙沪皖）" if a.skip_jzh else ""))
    else:
        groups = JZH_GROUPS

    files = sorted(ROOT.glob("*/*.xlsx"))
    files = [f for f in files if f.parent.name in groups]
    if not files:
        print(f"没有匹配文件：{ROOT} / groups={len(groups)}")
        sys.exit(1)
    if a.limit:
        files = sorted(files, key=lambda f: f.stat().st_size)[:a.limit]
    else:
        files = sorted(files, key=lambda f: -f.stat().st_size)

    total_gb = sum(f.stat().st_size for f in files) / 1024**3
    pending = [f for f in files if not (STATE / f"{state_key(f)}.json").exists()]
    print(f"文件 {len(files)} 个｜{total_gb:.2f} GB｜待导 {len(pending)}｜并发 {a.workers}\n")
    if not pending:
        print("全部已导入")
        return

    agg = {k: 0 for k in ("rows", "companies", "contacts", "credit_surrogate",
                          "dedup_in_file", "with_mobile", "with_landline",
                          "with_email", "with_any", "bad_rows")}
    t0 = time.time()
    fin = 0
    gb_done = 0.0
    todo_gb = sum(f.stat().st_size for f in pending) / 1024**3
    failed: list[str] = []

    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(process_file, str(f)): f for f in pending}
        for fut in as_completed(futs):
            f = futs[fut]
            fin += 1
            gb_done += f.stat().st_size / 1024**3
            try:
                st = fut.result()
            except Exception as e:
                failed.append(f"{f.parent.name}/{f.name}: {type(e).__name__}: {e}")
                print(f"  !! [{fin}/{len(pending)}] {f.name} 失败：{str(e)[:160]}", flush=True)
                continue
            if st.get("error"):
                failed.append(f"{f.parent.name}/{f.name}: {st['error']}")
                print(f"  !! [{fin}/{len(pending)}] {f.name} {st['error']}", flush=True)
                continue
            for k in agg:
                agg[k] += st.get(k, 0)
            el = time.time() - t0
            eta = (todo_gb - gb_done) / max(gb_done / el, 1e-9) / 60 if gb_done > 0 else 0
            print(
                f"  [{fin}/{len(pending)}] {st['group'][:10]:<12}{st['file'][:22]:<24}"
                f"{st['rows']:>8,}行 → {st['companies']:>8,}企 {st['contacts']:>8,}联"
                f"  解析{st.get('parse_s', 0):>5.1f}s 导入{st.get('load_s', 0):>5.1f}s"
                f"  累计{agg['companies']:>10,}  ETA {eta:.0f}分",
                flush=True,
            )

    el = (time.time() - t0) / 60
    print(f"\n{'=' * 66}")
    print(f"导入完成，耗时 {el:.1f} 分钟")
    print(f"  读取行数        {agg['rows']:,}")
    print(f"  文件内去重      {agg['dedup_in_file']:,}")
    print(f"  写入 companies  {agg['companies']:,}（跨文件重复由 Doris 主键合并）")
    print(f"  写入 contacts   {agg['contacts']:,}")
    print(f"  代理键信用代码  {agg['credit_surrogate']:,}")
    print(f"  空名丢弃        {agg['bad_rows']:,}")
    r = max(agg["rows"], 1)
    print(f"  行级触达        手机 {agg['with_mobile'] / r * 100:.2f}%"
          f"｜座机 {agg['with_landline'] / r * 100:.2f}%"
          f"｜邮箱 {agg['with_email'] / r * 100:.2f}%"
          f"｜任一 {agg['with_any'] / r * 100:.2f}%")
    if failed:
        print(f"\n失败 {len(failed)} 个：")
        for x in failed[:20]:
            print(f"  {x}")


if __name__ == "__main__":
    main()
