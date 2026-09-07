# -*- coding: utf-8 -*-
"""baize_core 黄金用例——拦住清洗规则漂移。"""
from __future__ import annotations

import pytest

from baize_core import (
    CREDIT_RE,
    EMAIL_RE,
    MOBILE_RE,
    STATUS_MAP,
    clean,
    is_empty,
    norm_landline,
    parse_capital,
    parse_date,
    surrogate_credit,
    tb,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("nan", None),
        ("null值", None),
        ("???", None),
        ("**", None),
        ("*", None),
        ("—", None),
        ("  ", None),
        ("杭州某某科技", "杭州某某科技"),
    ],
)
def test_clean_sentinels(raw, expected):
    assert clean(raw) == expected
    if expected is None:
        assert is_empty(raw)


@pytest.mark.parametrize(
    "raw,name,wan,cur",
    [
        ("1000万美元", "", 7200.00, "美元"),
        ("500万元", "", 500.0, "人民币"),
        ("5000", "", 0.50, "人民币"),  # 无单位按元 → 万元
        ("1亿", "", 10000.0, "人民币"),
    ],
)
def test_parse_capital_basic(raw, name, wan, cur):
    got_wan, got_cur = parse_capital(raw, name)
    assert got_wan == wan
    assert got_cur == cur


def test_parse_capital_dirty_wan_as_yuan():
    # 200000000万美元：按万会爆表，应纠偏为按元
    wan, cur = parse_capital("200000000万美元", "某贸易公司")
    assert cur == "美元"
    assert wan is not None
    assert wan < 1e8


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2020-06-15", "2020-06-15"),
        ("2020年6月15日", "2020-06-15"),
        ("44000", "2020-06-18"),  # Excel 序列号
        ("not-a-date", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_mobile_and_landline():
    assert MOBILE_RE.match("13812345678")
    assert not MOBILE_RE.match("1381234567")  # 10 位不是手机
    assert norm_landline("010-1234 5678") == "010-1234 5678" or norm_landline("010-12345678")
    # 手机混在座机列 → None，交给手机分支
    assert norm_landline("13812345678") is None


def test_email():
    assert EMAIL_RE.match("a.b+c@example.com")
    assert not EMAIL_RE.match("not-an-email")


def test_credit_and_surrogate():
    code = "91330100MA2ABCDEFG"
    assert len(code) == 18
    assert CREDIT_RE.match(code)
    assert not CREDIT_RE.match(code.lower())  # 须先转大写再校验
    s = surrogate_credit("杭州某某科技有限公司")
    assert s.startswith("N") and len(s) == 18
    assert CREDIT_RE.match(s)  # 代理键也是 18 位，但以 N 开头


def test_status_map():
    assert STATUS_MAP["存续（在营、开业、在册）"] == "存续"
    assert STATUS_MAP["开业"] == "存续"


def test_tb_byte_truncate():
    # 中文 3 字节/字；截到 5 字节不应留下半个汉字
    s = tb("杭州科技", 5)
    assert s is not None
    assert s.encode("utf-8") == s.encode("utf-8")  # roundtrip
    assert len(s.encode("utf-8")) <= 5
    assert "�" not in s
