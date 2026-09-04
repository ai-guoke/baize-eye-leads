# -*- coding: utf-8 -*-
"""街道解析：优先用标准行政区划库（高德）最长匹配，正则仅作无库时的降级。"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

_SUFFIXES = ("街道办事处", "街道", "镇", "乡", "苏木")
_BAD_IN_PREFIX = re.compile(r"街道办事处|街道|镇|乡|苏木")
_HAS_STREET_TAIL = re.compile(
    r"^[\u4e00-\u9fff0-9]{1,12}(?:街道办事处|街道|镇|乡|苏木)$"
)

# 内存标准库：(city, district) -> 街道名按长度降序
_STREET_INDEX: dict[tuple[str, str], list[str]] = {}
_BASE_ALIAS: dict[tuple[str, str], dict[str, str]] = {}
_LOADED = False


def _short(name: str) -> str:
    n = (name or "").strip()
    for suf in (
        "特别行政区", "维吾尔自治区", "壮族自治区", "回族自治区",
        "自治区", "省", "市", "地区", "盟",
    ):
        if n.endswith(suf) and len(n) > len(suf):
            return n[: -len(suf)]
    return n


def _strip_admin_prefix(s: str, province: str, city: str, district: str) -> str:
    s = (s or "").strip()
    for _ in range(4):
        changed = False
        for part in (province, city, district):
            part = (part or "").strip()
            if not part:
                continue
            for p in (part, _short(part)):
                if p and s.startswith(p):
                    s = s[len(p) :].lstrip("·，,、. ")
                    changed = True
                    break
        if not changed:
            break
    for marker in (
        "高新技术产业开发区", "经济技术开发区", "国家高新区",
        "高新区", "开发区", "工业园区", "科学园", "新区", "园区",
    ):
        idx = s.find(marker)
        if 0 <= idx <= 8:
            s = s[idx + len(marker) :].lstrip("·，,、. ")
            break
    return s.lstrip("·，,、. ")


def _base_name(street: str) -> str:
    s = (street or "").strip()
    for suf in _SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


def load_admin_streets(rows: Iterable[dict] | None = None, force: bool = False) -> int:
    """加载标准街道。rows 项需含 city/district/street；为空则从 Doris 读。"""
    global _STREET_INDEX, _BASE_ALIAS, _LOADED
    if _LOADED and not force and not rows:
        return sum(len(v) for v in _STREET_INDEX.values())

    if rows is None:
        from app import doris_client as dc
        rows = dc.query(
            "SELECT city, district, street FROM admin_divisions "
            "WHERE level = 'street' AND street IS NOT NULL AND street != ''"
        )

    from app.admin_alias import expand_city_names

    idx: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        city = (r.get("city") or "").strip()
        dist = (r.get("district") or "").strip()
        st = (r.get("street") or "").strip()
        if not st:
            continue
        # 高德「北京城区」同时挂到工商「北京市」等别名下
        for city_alias in expand_city_names(city, dist):
            key = (city_alias, dist)
            idx.setdefault(key, set()).add(st)
            # 仅区也能命中（直辖市/省直辖兜底）
            if dist:
                idx.setdefault(("", dist), set()).add(st)
            # 省直辖：县级市名既是 city 也常出现在 district
            if dist and (city_alias == dist or "直辖" in city):
                idx.setdefault((dist, ""), set()).add(st)

    _STREET_INDEX = {
        k: sorted(v, key=lambda x: (-len(x), x)) for k, v in idx.items()
    }
    _BASE_ALIAS = {}
    for key, names in _STREET_INDEX.items():
        amap: dict[str, str] = {}
        for n in names:
            b = _base_name(n)
            if b and b not in amap:
                amap[b] = n
        for n in names:
            b = _base_name(n)
            if b and (b not in amap or len(n) >= len(amap[b])):
                amap[b] = n
        _BASE_ALIAS[key] = amap

    _LOADED = True
    return sum(len(v) for v in _STREET_INDEX.values())


def _hit_in_text(s: str, name: str) -> bool:
    if not name or not s:
        return False
    if s.startswith(name):
        return True
    i = s.find(name)
    if i < 0 or i > 28:
        return False
    end = i + len(name)
    if end >= len(s):
        return True
    nxt = s[end]
    # 后面接门牌/道路等即可；避免命中更长词中间
    if nxt in "0123456789零一二三四五六七八九十百千万号弄巷村组队楼栋室层东南北中":
        return True
    if nxt in "·，,、. 　":
        return True
    if nxt == "路" or nxt == "街" or nxt == "道" or nxt == "大道":
        return True
    # 「街道」后仍跟「办事处」算命中
    if s[end:].startswith("办事处"):
        return True
    return not ("\u4e00" <= nxt <= "\u9fff")


def match_admin_street(province: str, city: str, district: str, address: str) -> str:
    """仅标准库匹配；无命中返回空。"""
    if not _LOADED:
        load_admin_streets()
    s = _strip_admin_prefix(address or "", province, city, district)
    if not s:
        return ""

    from app.admin_alias import street_index_keys

    keys = street_index_keys(city, district)
    # 兼容旧逻辑：空区键
    c, d = (city or "").strip(), (district or "").strip()
    if c and d and (c, d) not in keys:
        keys.append((c, d))

    seen: set[str] = set()
    for key in keys:
        names = _STREET_INDEX.get(key) or []
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            if _hit_in_text(s, name):
                return name
        aliases = _BASE_ALIAS.get(key) or {}
        for suf in ("镇", "乡", "街道", "街道办事处", "苏木"):
            for base, official in aliases.items():
                alias = base + suf
                if alias == official:
                    continue
                if _hit_in_text(s, alias):
                    return official
    return ""


def _normalize_street(street: str, province: str, city: str, district: str) -> str:
    street = (street or "").strip()
    if street.endswith("街道办事处"):
        street = street[: -len("办事处")]
    street = street.replace("街街道", "街道").replace("街道街道", "街道")
    for part in (district, city, province, _short(district), _short(city), _short(province)):
        if not part or len(part) < 2:
            continue
        if street.startswith(part):
            rest = street[len(part) :]
            if _HAS_STREET_TAIL.match(rest):
                street = rest
    if street.count("街道") > 1:
        i = street.index("街道")
        street = street[: i + len("街道")]
    if "街道" in street and street.endswith("镇"):
        street = street[: street.index("街道") + len("街道")]
    if street in {
        province, city, district,
        _short(province), _short(city), _short(district),
        "街道", "镇", "乡",
    }:
        return ""
    if len(street) <= 2 or len(street) > 12:
        return ""
    return street[:64]


def _extract_street_regex(province: str, city: str, district: str, address: str) -> str:
    """无标准库命中时的降级解析（收紧规则）。"""
    s = _strip_admin_prefix(address or "", province, city, district)
    if not s:
        return ""
    candidates: list[tuple[int, int, str]] = []
    scan_n = min(len(s), 32)
    for i in range(scan_n):
        chunk = s[i:]
        for suf in _SUFFIXES:
            m = re.match(rf"^([\u4e00-\u9fff0-9]{{2,5}}?){re.escape(suf)}", chunk)
            if not m:
                continue
            prefix = m.group(1)
            if _BAD_IN_PREFIX.search(prefix):
                continue
            norm = _normalize_street(prefix + suf, province, city, district)
            if not norm:
                continue
            candidates.append((i, len(norm), norm))
    if not candidates:
        return ""
    at_start = [c for c in candidates if c[0] == 0]
    if at_start:
        at_start.sort(key=lambda x: x[1])
        return at_start[0][2]
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][2]


def extract_street(
    province: str,
    city: str,
    district: str,
    address: str,
    *,
    allow_regex_fallback: bool = False,
) -> str:
    """
    默认只返回标准库命中结果（保证下拉/地图干净）。
    allow_regex_fallback=True 时，无命中再用收紧正则。
    """
    try:
        hit = match_admin_street(province, city, district, address)
        if hit:
            return hit
    except Exception:
        hit = ""
    if allow_regex_fallback:
        return _extract_street_regex(province, city, district, address)
    return hit or ""


@lru_cache(maxsize=1)
def admin_street_count() -> int:
    try:
        return load_admin_streets()
    except Exception:
        return 0
