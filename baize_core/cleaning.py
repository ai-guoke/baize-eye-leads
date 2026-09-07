# -*- coding: utf-8 -*-
"""空值哨兵、多值拆分、字节截断。"""
from __future__ import annotations

import re

EMPTY = {
    "", "-", "--", "—", "–", "无", "null", "none", "n/a", "na", "/", "\\",
    "暂无", "未知", "nan", "null值", "*", "**", "???",
}

MULTI_SPLIT = re.compile(r"[,，;；、\s|]+")


def is_empty(v: str | None) -> bool:
    return (not v) or v.strip().lower() in EMPTY


def clean(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip()
    return None if s.lower() in EMPTY else s


def tb(s: str | None, n: int) -> str | None:
    """按字节截断——Doris VARCHAR(n) 限字节数，不是字符数。"""
    if s is None:
        return None
    b = s.encode("utf-8")
    if len(b) <= n:
        return s
    return b[:n].decode("utf-8", errors="ignore")


def split_multi(raw: str | None) -> list[str]:
    if is_empty(raw):
        return []
    return [p for p in MULTI_SPLIT.split(raw.strip()) if p and not is_empty(p)]
