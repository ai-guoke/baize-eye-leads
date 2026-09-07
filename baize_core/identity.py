# -*- coding: utf-8 -*-
"""信用代码与代理键。入库用严格 18 位；搜索框宽松识别不进本包。"""
from __future__ import annotations

import hashlib
import re

CREDIT_RE = re.compile(r"^[0-9A-Z]{18}$")


def surrogate_credit(name: str) -> str:
    """信用代码缺失时的代理键，长度与真代码一致且不会撞车。"""
    return "N" + hashlib.md5(name.encode("utf-8")).hexdigest()[:17].upper()
