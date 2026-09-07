# -*- coding: utf-8 -*-
"""电话 / 邮箱 / 通信地址规范化。"""
from __future__ import annotations

import re

from baize_core.cleaning import is_empty, tb

MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
DIGITS_RE = re.compile(r"\d")


def norm_landline(v: str) -> str | None:
    """座机保留区号-号码原形，只做合法性校验。"""
    digits = "".join(DIGITS_RE.findall(v))
    if not (7 <= len(digits) <= 13):
        return None
    if MOBILE_RE.match(digits):
        return None
    return tb(v.strip(), 200)


def norm_mail_address(raw: str | None) -> str | None:
    """通信地址原文是 '地址A\\t;\\t地址B' 这种多值。"""
    if is_empty(raw):
        return None
    parts = [p.strip() for p in re.split(r"[\t]*;[\t]*|\t", raw) if p.strip()]
    seen, out = set(), []
    for p in parts:
        if is_empty(p) or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return " | ".join(out) if out else None
