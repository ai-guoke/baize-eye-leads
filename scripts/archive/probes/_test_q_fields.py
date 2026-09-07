# -*- coding: utf-8 -*-
"""检索维度与关联结果联调测试。"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import doris_client as dc

BASE = "http://127.0.0.1:8765"
PASS = FAIL = 0


def ok(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))


def api(path: str, params: dict, timeout: int = 90) -> tuple[dict, float]:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    t0 = time.perf_counter()
    with urllib.request.urlopen(f"{BASE}{path}?{qs}", timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data, (time.perf_counter() - t0) * 1000


def pick_fixtures() -> dict:
    dc.wait_ready(timeout=15)
    fx: dict = {}

    # 公司名：已知样本
    rows = dc.query(
        "SELECT credit_code, company_name, legal_person, province, city "
        "FROM companies WHERE company_name LIKE '%杭州国科智飞%' AND status='存续' LIMIT 1"
    )
    if rows:
        fx["company"] = rows[0]

    # 人名：有明确法人
    rows = dc.query(
        "SELECT credit_code, company_name, legal_person FROM companies "
        "WHERE status='存续' AND legal_person IS NOT NULL "
        "AND char_length(legal_person) BETWEEN 2 AND 3 "
        "AND legal_person NOT LIKE '%公司%' "
        "ORDER BY has_mobile DESC, capital_wan DESC LIMIT 1"
    )
    if rows:
        fx["person"] = rows[0]

    # 手机
    rows = dc.query(
        "SELECT ct.contact_value AS mobile, c.credit_code, c.company_name "
        "FROM contacts ct INNER JOIN companies c ON c.credit_code = ct.credit_code "
        "WHERE ct.contact_type='mobile' AND c.status='存续' "
        "AND ct.contact_value REGEXP '^1[3-9][0-9]{9}$' "
        "LIMIT 1"
    )
    if rows:
        fx["mobile"] = rows[0]

    # 邮箱
    rows = dc.query(
        "SELECT ct.contact_value AS email, c.credit_code, c.company_name "
        "FROM contacts ct INNER JOIN companies c ON c.credit_code = ct.credit_code "
        "WHERE ct.contact_type='email' AND c.status='存续' "
        "AND ct.contact_value LIKE '%@%' "
        "LIMIT 1"
    )
    if rows:
        fx["email"] = rows[0]

    # 信用代码
    rows = dc.query(
        "SELECT credit_code, company_name FROM companies "
        "WHERE status='存续' AND char_length(credit_code) >= 18 LIMIT 1"
    )
    if rows:
        fx["credit"] = rows[0]

    # 经营范围关键词（短词，有倒排更稳）
    rows = dc.query(
        "SELECT credit_code, company_name, LEFT(scope, 40) AS scope_preview FROM companies "
        "WHERE status='存续' AND scope MATCH_ALL '软件开发' LIMIT 1"
    )
    if rows:
        fx["scope"] = rows[0]

    return fx


def names_in(items: list) -> set[str]:
    return {r.get("company_name") or "" for r in items}


def codes_in(items: list) -> set[str]:
    return {r.get("credit_code") or "" for r in items}


def preview_values(item: dict) -> list[str]:
    return [x.get("contact_value") for x in (item.get("contacts_preview") or []) if x.get("contact_value")]


def main():
    global PASS, FAIL
    print("=== 取样 ===")
    fx = pick_fixtures()
    for k, v in fx.items():
        print(f"  {k}: {v}")
    if "company" not in fx:
        print("缺少公司样例，中止")
        sys.exit(1)

    print("\n=== 1) 公司名检索 ===")
    name = fx["company"]["company_name"]
    # 用较短关键词，贴近用户输入
    q_co = "杭州国科智飞"
    d, ms = api("/api/search", {"q": q_co, "q_fields": "company", "status": "存续", "page_size": 10})
    ok("公司名命中", d.get("total", 0) >= 1 and fx["company"]["credit_code"] in codes_in(d.get("items") or []),
       f"{ms:.0f}ms total={d.get('total')} label={d.get('q_kind_label')}")
    ok("公司名耗时<3s", ms < 3000, f"{ms:.0f}ms")

    print("\n=== 2) 人名检索 ===")
    if "person" in fx:
        person = fx["person"]["legal_person"]
        d, ms = api("/api/search", {"q": person, "q_fields": "person", "status": "存续", "page_size": 20})
        hit = any((r.get("legal_person") or "") == person for r in (d.get("items") or []))
        ok("人名精确命中法人", hit and d.get("total", 0) >= 1,
           f"{ms:.0f}ms total={d.get('total')} person={person}")
        # 用公司名当人名应无结果或极少
        d2, ms2 = api("/api/search", {"q": q_co, "q_fields": "person", "status": "存续", "page_size": 5})
        ok("公司名不当人名", d2.get("total", 0) == 0, f"total={d2.get('total')} {ms2:.0f}ms")
    else:
        ok("人名样例", False, "未取到")

    print("\n=== 3) 手机号检索 + 关联公司/预览 ===")
    if "mobile" in fx:
        mobile = fx["mobile"]["mobile"]
        code = fx["mobile"]["credit_code"]
        d, ms = api("/api/search", {"q": mobile, "q_fields": "mobile", "status": "存续", "page_size": 10})
        items = d.get("items") or []
        ok("手机命中公司", code in codes_in(items), f"{ms:.0f}ms total={d.get('total')}")
        row = next((r for r in items if r.get("credit_code") == code), None)
        ok("关联 contacts_preview 含该手机",
           bool(row) and mobile in preview_values(row),
           f"preview={preview_values(row) if row else None}")
        # 详情接口关联
        detail, ms_d = api(f"/api/company/{urllib.parse.quote(code)}", {})
        vals = [c.get("contact_value") for c in (detail.get("contacts") or [])]
        ok("详情页 contacts 含该手机", mobile in vals, f"{ms_d:.0f}ms n={len(vals)}")
    else:
        ok("手机样例", False, "未取到")

    print("\n=== 4) 邮箱检索 + 关联 ===")
    if "email" in fx:
        email = fx["email"]["email"]
        code = fx["email"]["credit_code"]
        d, ms = api("/api/search", {"q": email, "q_fields": "email", "status": "存续", "page_size": 10})
        items = d.get("items") or []
        ok("邮箱命中公司", code in codes_in(items), f"{ms:.0f}ms total={d.get('total')}")
        row = next((r for r in items if r.get("credit_code") == code), None)
        prev = [x.lower() for x in preview_values(row)] if row else []
        ok("关联 preview 含邮箱", email.lower() in prev, f"preview={prev}")
    else:
        ok("邮箱样例", False, "未取到")

    print("\n=== 5) 信用代码检索 ===")
    if "credit" in fx:
        code = fx["credit"]["credit_code"]
        d, ms = api("/api/search", {"q": code, "q_fields": "credit", "status": "存续", "page_size": 5})
        ok("信用代码精确命中", d.get("total") == 1 and code in codes_in(d.get("items") or []),
           f"{ms:.0f}ms")
    else:
        ok("信用代码样例", False, "未取到")

    print("\n=== 6) 经营范围检索 ===")
    if "scope" in fx:
        d, ms = api("/api/search", {"q": "软件开发", "q_fields": "scope", "status": "存续", "page_size": 5})
        ok("经营范围有命中", d.get("total", 0) >= 1, f"{ms:.0f}ms total={d.get('total')}")
    else:
        print("  SKIP  经营范围样例未取到")

    print("\n=== 7) 多维 OR / 形态跳过 ===")
    d, ms = api("/api/search", {
        "q": q_co, "q_fields": "company,person,mobile", "status": "存续", "page_size": 10,
    })
    ok("公司名+人名+手机 仍能命中公司",
       fx["company"]["credit_code"] in codes_in(d.get("items") or []),
       f"{ms:.0f}ms total={d.get('total')} label={d.get('q_kind_label')}")
    ok("多维公司名耗时<3s", ms < 3000, f"{ms:.0f}ms")

    print("\n=== 8) 智能识别 ===")
    d, ms = api("/api/search", {"q": q_co, "q_fields": "auto", "status": "存续", "page_size": 5})
    ok("智能识别公司名", d.get("total", 0) >= 1 and "company" in (d.get("q_kind") or ""),
       f"{ms:.0f}ms kind={d.get('q_kind')} total={d.get('total')}")
    if "mobile" in fx:
        mobile = fx["mobile"]["mobile"]
        d, ms = api("/api/search", {"q": mobile, "q_fields": "auto", "status": "存续", "page_size": 5})
        ok("智能识别手机", d.get("q_kind") == "phone" and d.get("total", 0) >= 1,
           f"{ms:.0f}ms kind={d.get('q_kind')} total={d.get('total')}")

    print("\n=== 9) stats 与 search 一致 ===")
    d, _ = api("/api/search", {"q": q_co, "q_fields": "company", "status": "存续", "page_size": 5})
    st, ms = api("/api/stats", {"q": q_co, "q_fields": "company", "status": "存续"})
    ok("stats.total == search.total",
       int((st.get("summary") or {}).get("total") or -1) == int(d.get("total") or -2),
       f"search={d.get('total')} stats={st.get('summary', {}).get('total')} {ms:.0f}ms")

    print("\n=== 10) 负例：只勾手机却输入公司名 ===")
    d, ms = api("/api/search", {"q": q_co, "q_fields": "mobile", "status": "存续", "page_size": 5})
    ok("非号码+仅手机 => 0", d.get("total", 0) == 0, f"total={d.get('total')} {ms:.0f}ms")

    print(f"\n======== 结果: PASS={PASS} FAIL={FAIL} ========")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
