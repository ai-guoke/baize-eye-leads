# -*- coding: utf-8 -*-
"""注册资本解析与脏数据纠偏。"""
from __future__ import annotations

import re

from baize_core.cleaning import is_empty

CAPITAL_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(万|亿)?\s*([\u4e00-\u9fa5]{2,4})?")

FX = {
    "人民币": 1.0, "美元": 7.2, "美金": 7.2, "港元": 0.92, "港币": 0.92,
    "欧元": 8.0, "日元": 0.048, "英镑": 9.5, "新加坡元": 5.5, "新元": 5.5,
    "澳元": 4.7, "加元": 5.3, "韩元": 0.0052, "台币": 0.23, "新台币": 0.23,
    "瑞士法郎": 8.1, "瑞典克朗": 0.68, "泰铢": 0.2, "卢布": 0.075,
}

SOE_CAPITAL_RE = re.compile(
    r"(国家电网|中央汇金|中国石油|中国石化|中国海油|国家开发银行|中国铁路|"
    r"中国烟草|中国移动|中国电信|中国联通|工商银行|建设银行|农业银行|"
    r"中国银行股份|交通银行|国家能源|南方电网|中国华能|华能集团|大唐集团|"
    r"中国华电|华电集团|国家电投|中核集团|航天科技|航天科工|航空工业|"
    r"中国中车|中国建筑|中国中铁|中国铁建|中国交建|中国电建|中国能建|"
    r"中国铝业|中国宝武|招商局|中粮集团|保利集团|华润集团|华润\(|"
    r"中国人寿|中国平安|中国人保|中国邮政|全国社保|国家管网|"
    r"国家石油天然气|中国投资有限责任|国家集成电路|产业投资基金|"
    r"山东高速|中国融通|中国诚通|中国国新|中国烟草总公司|"
    r"中国铁路投资|中国铁路发展基金|中国工商银行|中国农业银行|"
    r"中国建设银行|中国银行有限)"
)


def parse_capital(raw: str, company_name: str = "") -> tuple[float | None, str | None]:
    """解析注册资本 → (人民币万元, 币种)。

    例：'1000万美元' → (7200.00, '美元')；'500万元' → (500, '人民币')。
    """
    if is_empty(raw):
        return None, None
    m = CAPITAL_RE.search(raw.strip().replace(",", ""))
    if not m:
        return None, None
    try:
        num = float(m.group(1))
    except ValueError:
        return None, None
    unit = m.group(2) or ""
    cur = m.group(3) or "人民币"
    fx = FX.get(cur, 1.0)
    mult = {"万": 1.0, "亿": 10000.0}.get(unit, 0.0001)
    if unit == "万":
        scaled = num * fx
        is_soe = bool(SOE_CAPITAL_RE.search(company_name or ""))
        if scaled >= 1e8 or (scaled >= 1e7 and not is_soe):
            mult = 0.0001
    wan = num * mult * fx
    if wan > 5e8:
        return None, cur
    return round(wan, 2), cur
