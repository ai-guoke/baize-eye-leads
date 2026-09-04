# -*- coding: utf-8 -*-
"""只读流式解析 xlsx（不依赖 pandas/openpyxl）。"""
from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
T_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def col_letter_to_idx(cell_ref: str) -> int:
    letters = "".join(c for c in (cell_ref or "") if c.isalpha())
    n = 0
    for c in letters:
        n = n * 26 + (ord(c.upper()) - 64)
    return max(n - 1, 0)


def load_shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    out: list[str] = []
    with z.open("xl/sharedStrings.xml") as fh:
        for _event, elem in ET.iterparse(fh, events=("end",)):
            if elem.tag == T_NS + "si" or elem.tag.endswith("}si") or elem.tag == "si":
                texts = []
                for t in elem.iter():
                    if t.tag == T_NS + "t" or t.tag.endswith("}t") or t.tag == "t":
                        texts.append(t.text or "")
                out.append("".join(texts))
                elem.clear()
    return out


def sheet_map(z: zipfile.ZipFile) -> dict[str, str]:
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid_to_target: dict[str, str] = {}
    for rel in rels:
        rid = rel.get("Id")
        target = rel.get("Target")
        if rid and target:
            # Target 可能写成 "worksheets/sheet1.xml"、"/xl/worksheets/sheet1.xml"
            # 或 "xl/worksheets/sheet1.xml"，先去掉前导斜杠再判断，否则会拼出 xl/xl/…
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            rid_to_target[rid] = target
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    mapping: dict[str, str] = {}
    for sh in wb.findall(".//m:sheet", NS):
        name = sh.get("name") or ""
        rid = sh.get(REL_NS + "id")
        path = rid_to_target.get(rid or "")
        if name and path:
            mapping[name] = path
    return mapping


def _cell_value(c, sst: list[str]) -> str:
    t = c.get("t")
    v = None
    for child in c:
        tag = child.tag.split("}")[-1]
        if tag == "v":
            v = child
            break
    if t == "s" and v is not None and v.text is not None:
        try:
            return sst[int(v.text)]
        except (ValueError, IndexError):
            return v.text or ""
    if t == "inlineStr":
        texts = []
        for tnode in c.iter():
            if tnode.tag.split("}")[-1] == "t":
                texts.append(tnode.text or "")
        return "".join(texts)
    if t == "b" and v is not None:
        return "1" if v.text == "1" else "0"
    if v is not None:
        return v.text or ""
    return ""


def iter_rows(z: zipfile.ZipFile, sheet_xml: str, sst: list[str]) -> Iterator[list[str]]:
    with z.open(sheet_xml) as fh:
        for _event, elem in ET.iterparse(fh, events=("end",)):
            tag = elem.tag.split("}")[-1]
            if tag != "row":
                continue
            cells: dict[int, str] = {}
            max_idx = -1
            col_i = 0
            for c in elem:
                if c.tag.split("}")[-1] != "c":
                    continue
                ref = c.get("r") or ""
                idx = col_letter_to_idx(ref) if ref else col_i
                val = _cell_value(c, sst)
                cells[idx] = val
                max_idx = max(max_idx, idx)
                col_i = idx + 1
            if max_idx >= 0:
                yield [cells.get(i, "") for i in range(max_idx + 1)]
            elem.clear()


def iter_workbook_sheets(path: Path):
    """yield (sheet_name, header, row_iterator). row_iterator yields list[str]."""
    with zipfile.ZipFile(path) as z:
        sst = load_shared_strings(z)
        mapping = sheet_map(z)
        for sheet_name, xml_path in mapping.items():
            rows = iter_rows(z, xml_path, sst)
            try:
                header = next(rows)
            except StopIteration:
                continue
            yield sheet_name, header, rows
