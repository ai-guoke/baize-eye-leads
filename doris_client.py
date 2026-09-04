# -*- coding: utf-8 -*-
"""Doris 连接工具：SQL 执行 + Stream Load 导入。ETL 与 API 层共用。"""
from __future__ import annotations

import base64
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

import pymysql
import requests

FE_HOST = "127.0.0.1"
FE_QUERY_PORT = 9030
FE_HTTP_PORT = 8030
BE_HTTP_PORT = 8040
USER = "root"
PASSWORD = ""
DB = "qcc"

# Stream Load 用不可见字符做分隔，避免和经营范围里的标点冲突
COL_SEP = "\x01"
LINE_SEP = "\x02"
NULL_TOKEN = "\\N"


def connect(db: str | None = DB, timeout: int = 60) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=FE_HOST,
        port=FE_QUERY_PORT,
        user=USER,
        password=PASSWORD,
        database=db,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=timeout,
        read_timeout=timeout * 10,
        write_timeout=timeout,
    )


def query(sql: str, args: Sequence[Any] | None = None, db: str | None = DB) -> list[dict]:
    with connect(db) as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, args)
            try:
                return list(cur.fetchall())
            except Exception:
                return []


def execute(sql: str, db: str | None = DB) -> None:
    with connect(db) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def split_statements(script: str) -> list[str]:
    """按分号切分 SQL 脚本，忽略 -- 行注释。"""
    out: list[str] = []
    buf: list[str] = []
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        buf.append(raw)
        if line.endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            if stmt:
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip().rstrip(";").strip()
    if tail:
        out.append(tail)
    return out


def run_script(path: str | Path, db: str | None = None) -> None:
    """执行 SQL 文件。首条 CREATE DATABASE 之前不指定库，所以默认 db=None。"""
    text = Path(path).read_text(encoding="utf-8")
    stmts = split_statements(text)
    conn = connect(db)
    try:
        with conn.cursor() as cur:
            for i, s in enumerate(stmts, 1):
                head = " ".join(s.split())[:78]
                print(f"  [{i}/{len(stmts)}] {head}")
                cur.execute(s)
    finally:
        conn.close()


def wait_ready(timeout: int = 300, quiet: bool = False) -> bool:
    """等待 FE 可连接且至少一个 BE alive。"""
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        try:
            rows = query("SHOW BACKENDS", db=None)
            alive = [r for r in rows if str(r.get("Alive", "")).lower() == "true"]
            if alive:
                if not quiet:
                    print(f"  Doris 就绪：{len(alive)} 个 BE alive（等待 {time.time() - t0:.0f}s）")
                return True
            last = f"FE 已连接，但 BE 未注册/未存活（共 {len(rows)} 个）"
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:90]}"
        if not quiet:
            print(f"  等待中… {last}", flush=True)
        time.sleep(5)
    if not quiet:
        print(f"  超时未就绪：{last}")
    return False


def _auth_header() -> str:
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    return f"Basic {token}"


def escape(v: Any) -> str:
    """把一个字段值转成 Stream Load CSV 可安全承载的文本。"""
    if v is None or v == "":
        return NULL_TOKEN
    s = str(v)
    # 分隔符与换行必须清掉，否则会撕裂行结构
    for ch in (COL_SEP, LINE_SEP, "\r", "\n", "\t"):
        if ch in s:
            s = s.replace(ch, " ")
    return s


def make_csv(rows: Iterable[Sequence[Any]]) -> bytes:
    return LINE_SEP.join(
        COL_SEP.join(escape(v) for v in row) for row in rows
    ).encode("utf-8")


def stream_load(
    table: str,
    columns: Sequence[str],
    payload: bytes,
    label: str | None = None,
    db: str = DB,
    max_filter_ratio: float = 0.001,
    timeout: int = 1800,
    retries: int = 3,
    partial_columns: bool = False,
) -> dict:
    """通过 Stream Load 导入一批 CSV。返回 Doris 的响应 JSON。

    直连 BE:8040，避开 FE → 容器内网 IP 的 307 重定向
   （宿主机无法访问 172.28.10.x）。
    """
    url = f"http://{FE_HOST}:{BE_HTTP_PORT}/api/{db}/{table}/_stream_load"
    headers = {
        "Authorization": _auth_header(),
        "format": "csv",
        "column_separator": COL_SEP,
        "line_delimiter": LINE_SEP,
        "columns": ",".join(columns),
        "max_filter_ratio": str(max_filter_ratio),
        "label": label or f"{table}_{uuid.uuid4().hex}",
        "timeout": str(timeout),
        "strict_mode": "false",
    }
    if partial_columns:
        headers["partial_columns"] = "true"
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.put(url, data=payload, headers=headers, timeout=timeout)
            # BE 有时返回空体（网关抖动），保留原文便于排查
            if not resp.content:
                last_err = f"empty body HTTP {resp.status_code}"
            else:
                js = resp.json()
                if js.get("Status") in ("Success", "Publish Timeout"):
                    return js
                if "Label Already Exists" in str(js.get("Message", "")):
                    js["Status"] = "AlreadyLoaded"
                    return js
                last_err = f"{js.get('Status')}: {str(js.get('Message'))[:200]}"
                if js.get("ErrorURL"):
                    last_err += f" | {js['ErrorURL']}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:200]}"
        if attempt < retries:
            time.sleep(3 * attempt)
            headers["label"] = f"{headers['label']}_r{attempt}"
    raise RuntimeError(f"Stream Load 失败（{table}，{retries} 次重试）：{last_err}")
