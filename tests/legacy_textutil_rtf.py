#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests.legacy_textutil_rtf
==========================

`align.py` が純Python実装のRTFパーサーに切り替わる前に使っていた、macOS標準の
`textutil` コマンド経由の実装をそのまま保存したもの。配布物には含めず、
`tests/compare_rtf_parsers.py` からの新旧比較テストでのみ使う(macOS上でのみ動作)。
"""
from __future__ import annotations

import re
import subprocess
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

from auto_telop.align import CATEGORY_RGB, IMAGE_BG_RGB, _dominant_match


def _convert_rtf_to_html(path: str) -> str:
    result = subprocess.run(
        ["textutil", "-convert", "html", "-stdout", path],
        capture_output=True, check=True,
    )
    return result.stdout.decode("utf-8")


def _parse_css_classes(doc: str) -> Dict[str, Dict[str, Optional[str]]]:
    m = re.search(r"<style[^>]*>(.*?)</style>", doc, re.S)
    css = m.group(1) if m else ""
    classes: Dict[str, Dict[str, Optional[str]]] = {}
    for rule_m in re.finditer(r"\.(\S+)\s*\{([^}]*)\}", css):
        cls = rule_m.group(1)
        body = rule_m.group(2)
        color_m = re.search(r"(?<!background-)color:\s*(#[0-9a-fA-F]{6})", body)
        bg_m = re.search(r"background-color:\s*(#[0-9a-fA-F]{6})", body)
        classes[cls] = {
            "color": color_m.group(1).lower() if color_m else None,
            "bg": bg_m.group(1).lower() if bg_m else None,
        }
    return classes


class _ParagraphHTMLParser(HTMLParser):
    def __init__(self, classes: Dict[str, Dict[str, Optional[str]]]):
        super().__init__(convert_charrefs=True)
        self.classes = classes
        self.paragraphs: List[Dict[str, object]] = []
        self._in_p = False
        self._p_style: Dict[str, Optional[str]] = {"color": None, "bg": None}
        self._span_stack: List[Dict[str, Optional[str]]] = []
        self._fg_runs: List[Tuple[str, Optional[str]]] = []
        self._bg_runs: List[Tuple[str, Optional[str]]] = []

    def _effective(self, key: str) -> Optional[str]:
        for style in reversed(self._span_stack):
            if style.get(key) is not None:
                return style[key]
        return self._p_style.get(key)

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "p":
            self._in_p = True
            self._p_style = self.classes.get(d.get("class", ""), {"color": None, "bg": None})
            self._span_stack = []
            self._fg_runs = []
            self._bg_runs = []
        elif tag == "span" and self._in_p:
            self._span_stack.append(self.classes.get(d.get("class", ""), {"color": None, "bg": None}))

    def handle_endtag(self, tag):
        if tag == "span" and self._in_p and self._span_stack:
            self._span_stack.pop()
        elif tag == "p" and self._in_p:
            self._in_p = False
            text = "".join(t for t, _ in self._fg_runs)
            self.paragraphs.append({"text": text, "fg_runs": self._fg_runs, "bg_runs": self._bg_runs})

    def handle_data(self, data):
        if not self._in_p:
            return
        self._fg_runs.append((data, self._effective("color")))
        self._bg_runs.append((data, self._effective("bg")))


def parse_rtf_textutil(path: str) -> List[Dict[str, object]]:
    doc = _convert_rtf_to_html(path)
    classes = _parse_css_classes(doc)
    body_m = re.search(r"<body>(.*)</body>", doc, re.S)
    body = body_m.group(1) if body_m else doc

    parser = _ParagraphHTMLParser(classes)
    parser.feed(body)

    lines: List[Dict[str, object]] = []
    for para in parser.paragraphs:
        text = str(para["text"]).strip()
        if not text:
            continue
        category = _dominant_match(para["fg_runs"], CATEGORY_RGB)
        is_image = _dominant_match(para["bg_runs"], {"image": IMAGE_BG_RGB}) == "image"
        lines.append({"text": text, "category": category, "is_image": is_image})
    return lines
