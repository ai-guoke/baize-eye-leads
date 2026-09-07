# -*- coding: utf-8 -*-
"""日期解析。"""
from __future__ import annotations

import re
from datetime import date, timedelta

from baize_core.cleaning import is_empty

DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")
YEAR_ONLY_RE = re.compile(r"^(\d{4})$")


def parse_date(raw: str | None) -> str | None:
    if is_empty(raw):
        return None
    s = raw.strip()
    m = DATE_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1800 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return None
    if s.isdigit() and 20000 <= int(s) <= 60000:
        return (date(1899, 12, 30) + timedelta(days=int(s))).isoformat()
    return None
