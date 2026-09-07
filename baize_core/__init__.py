# -*- coding: utf-8 -*-
"""白泽共享清洗规则（全平台唯一副本）。

服务与 ETL 只允许从本包 import，禁止本地重定义 EMPTY / CREDIT_RE / parse_capital 等。
"""

__version__ = "0.1.0"

from baize_core.cleaning import EMPTY, clean, is_empty, split_multi, tb
from baize_core.capital import CAPITAL_RE, FX, parse_capital
from baize_core.contact import EMAIL_RE, MOBILE_RE, norm_landline, norm_mail_address
from baize_core.dates import parse_date
from baize_core.identity import CREDIT_RE, surrogate_credit
from baize_core.region import GROUP_PROVINCE, PROVINCE_MAP, STATUS_MAP, folder_to_province

__all__ = [
    "EMPTY",
    "CREDIT_RE",
    "MOBILE_RE",
    "EMAIL_RE",
    "CAPITAL_RE",
    "FX",
    "PROVINCE_MAP",
    "GROUP_PROVINCE",
    "STATUS_MAP",
    "is_empty",
    "clean",
    "tb",
    "split_multi",
    "parse_capital",
    "parse_date",
    "norm_landline",
    "norm_mail_address",
    "surrogate_credit",
    "folder_to_province",
]
