# -*- coding: utf-8 -*-
"""加载 secrets/amap.env。"""
from __future__ import annotations

import os
from pathlib import Path

# secrets/ 位于仓库根目录（app/ 的上一级）
SECRETS = Path(__file__).resolve().parents[1] / "secrets" / "amap.env"


def load_amap_env() -> dict[str, str]:
    out = {
        "AMAP_JS_KEY": os.environ.get("AMAP_JS_KEY", ""),
        "AMAP_SECURITY_CODE": os.environ.get("AMAP_SECURITY_CODE", ""),
        "AMAP_WEB_KEY": os.environ.get("AMAP_WEB_KEY", ""),
    }
    if SECRETS.exists():
        for line in SECRETS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out
