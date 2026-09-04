# -*- coding: utf-8 -*-
"""工商库城市名 ↔ 高德区划城市名 兼容。

常见差异：
- 直辖市：工商写「北京市」，高德写「北京城区」
- 省直辖：工商 city=「省直辖县级行政区划」，真实县级市在 district
- 自治州简称：工商偶发「恩施州」，高德「恩施土家族苗族自治州」
"""
from __future__ import annotations

# 双向：工商名 ↔ 高德名
CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "北京市": ("北京市", "北京城区"),
    "北京城区": ("北京城区", "北京市"),
    "上海市": ("上海市", "上海城区"),
    "上海城区": ("上海城区", "上海市"),
    "天津市": ("天津市", "天津城区"),
    "天津城区": ("天津城区", "天津市"),
    "重庆市": ("重庆市", "重庆城区", "重庆郊县"),
    "重庆城区": ("重庆城区", "重庆市", "重庆郊县"),
    "重庆郊县": ("重庆郊县", "重庆市", "重庆城区"),
}

# 工商简称 → 高德全称（少量脏数据）
SHORT_CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "伊犁州": ("伊犁哈萨克自治州", "伊犁州"),
    "昌吉州": ("昌吉回族自治州", "昌吉州"),
    "巴州": ("巴音郭楞蒙古自治州", "巴州"),
    "恩施州": ("恩施土家族苗族自治州", "恩施州"),
    "湘西州": ("湘西土家族苗族自治州", "湘西州"),
    "黔东南州": ("黔东南苗族侗族自治州", "黔东南州"),
    "黔南州": ("黔南布依族苗族自治州", "黔南州"),
    "黔西南州": ("黔西南布依族苗族自治州", "黔西南州"),
    "大理州": ("大理白族自治州", "大理州"),
    "红河州": ("红河哈尼族彝族自治州", "红河州"),
    "楚雄州": ("楚雄彝族自治州", "楚雄州"),
    "文山州": ("文山壮族苗族自治州", "文山州"),
    "西双版纳州": ("西双版纳傣族自治州", "西双版纳州"),
    "德宏州": ("德宏傣族景颇族自治州", "德宏州"),
    "怒江州": ("怒江傈僳族自治州", "怒江州"),
    "迪庆州": ("迪庆藏族自治州", "迪庆州"),
    "雄安新区": ("雄安新区", "保定市"),  # 高德可能挂在保定，兜底
}

DIRECT_CITY_PLACEHOLDERS = {
    "省直辖县级行政区划",
    "自治区直辖县级行政区划",
}

MUNICIPAL_PROVINCES = {"北京市", "天津市", "上海市", "重庆市"}


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        n = (n or "").strip()
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def expand_city_names(city: str, district: str = "") -> list[str]:
    """给定工商或高德城市名，返回应互相匹配的全部候选。"""
    c = (city or "").strip()
    d = (district or "").strip()
    names: list[str] = []
    if c in DIRECT_CITY_PLACEHOLDERS:
        # 真实「市」常落在 district（如济源市、仙桃市）
        if d:
            names.append(d)
        names.append(c)
    if c in CITY_ALIASES:
        names.extend(CITY_ALIASES[c])
    elif c in SHORT_CITY_ALIASES:
        names.extend(SHORT_CITY_ALIASES[c])
    elif c:
        names.append(c)
    return _dedupe(names)


def is_direct_admin_city(city: str) -> bool:
    return (city or "").strip() in DIRECT_CITY_PLACEHOLDERS


def sql_in(column: str, values: list[str], esc) -> str:
    """生成 column IN ('a','b')；values 空则 1=0。esc 为转义函数。"""
    vals = _dedupe(list(values))
    if not vals:
        return "1=0"
    body = ", ".join(f"'{esc(v)}'" for v in vals)
    return f"{column} IN ({body})"


def sql_city_in(column: str, city: str, esc, district: str = "") -> str:
    return sql_in(column, expand_city_names(city, district), esc)


def street_index_keys(city: str, district: str) -> list[tuple[str, str]]:
    """标准库索引 / 匹配时使用的 (city, district) 键。"""
    c = (city or "").strip()
    d = (district or "").strip()
    keys: list[tuple[str, str]] = []
    for alias in expand_city_names(c, d):
        if alias and d:
            keys.append((alias, d))
        if alias:
            keys.append((alias, ""))
    if d:
        keys.append(("", d))
        if is_direct_admin_city(c):
            # 省直辖：district 当城市用
            keys.insert(0, (d, ""))
            keys.insert(0, (d, d))
    # 去重保序
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for k in keys:
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def admin_city_match_sql(
    company_city_col: str,
    admin_city_col: str,
    esc,
    company_district_col: str = "c.district",
) -> str:
    """SQL：企业 city 与 admin city 兼容（直辖市 + 原样相等）。"""
    parts = [f"{admin_city_col} = {company_city_col}"]
    for company_name, aliases in CITY_ALIASES.items():
        # 只从工商侧常见名出发展开，避免重复
        if company_name.endswith("城区") or company_name.endswith("郊县"):
            continue
        admin_names = [a for a in aliases if a != company_name]
        if not admin_names:
            continue
        adm = ", ".join(f"'{esc(a)}'" for a in admin_names)
        parts.append(
            f"({company_city_col} = '{esc(company_name)}' AND {admin_city_col} IN ({adm}))"
        )
    # 省直辖：admin.city = company.district
    parts.append(
        f"({company_city_col} IN ('省直辖县级行政区划','自治区直辖县级行政区划') "
        f"AND {admin_city_col} = {company_district_col})"
    )
    return "(" + " OR ".join(parts) + ")"
