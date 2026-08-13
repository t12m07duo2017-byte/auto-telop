#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prproj_generate.py が使う共通ユーティリティ。

方針(check_span_splice.pyで検証済み): .prprojの読み取りはElementTreeで
行ってよいが、書き込みは絶対に全文再シリアライズしない。既存部分は
生テキストのまま保持し、新規追加/書き換えたい箇所だけを正規表現で
ピンポイントに挿入・置換する。
"""
from __future__ import annotations

import gzip
import re
import uuid
from typing import Optional, Tuple

SECONDS_PER_TICK = 254016000000  # Premiereの内部tick単位。実データのSE尺(ffprobe)と一致することを確認済み。


def load_prproj_text(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8")


def save_prproj_text(path: str, text: str, compresslevel: int = 6) -> None:
    with gzip.open(path, "wb", compresslevel=compresslevel) as f:
        f.write(text.encode("utf-8"))


def sec_to_ticks(seconds: float) -> int:
    return int(round(seconds * SECONDS_PER_TICK))


def ticks_to_sec(ticks: int) -> float:
    return ticks / SECONDS_PER_TICK


def new_guid() -> str:
    return str(uuid.uuid4())


class ObjectIdAllocator:
    """ファイル内の最大ObjectID(整数)より大きい未使用IDを払い出す。"""

    def __init__(self, text: str):
        ids = [int(m) for m in re.findall(r'ObjectID="(\d+)"', text)]
        self._next = (max(ids) + 1) if ids else 1

    def next(self) -> int:
        v = self._next
        self._next += 1
        return v


def find_block_by_object_id(text: str, tag: str, object_id: str, start: int = 0) -> Optional[Tuple[int, int]]:
    """<tag ObjectID="object_id" ...> ... </tag> の (start, end) 文字位置を返す。"""
    pat = re.compile(rf'<{tag} ObjectID="{re.escape(str(object_id))}"[^>]*>.*?</{tag}>', re.S)
    m = pat.search(text, start)
    if not m:
        return None
    return m.start(), m.end()


def find_block_by_object_uid(text: str, tag: str, object_uid: str, start: int = 0) -> Optional[Tuple[int, int]]:
    pat = re.compile(rf'<{tag} ObjectUID="{re.escape(object_uid)}"[^>]*>.*?</{tag}>', re.S)
    m = pat.search(text, start)
    if not m:
        return None
    return m.start(), m.end()


def extract_block_by_object_id(text: str, tag: str, object_id: str) -> str:
    span = find_block_by_object_id(text, tag, object_id)
    if span is None:
        raise ValueError(f"block not found: <{tag} ObjectID=\"{object_id}\">")
    return text[span[0]:span[1]]


def extract_block_by_object_uid(text: str, tag: str, object_uid: str) -> str:
    span = find_block_by_object_uid(text, tag, object_uid)
    if span is None:
        raise ValueError(f"block not found: <{tag} ObjectUID=\"{object_uid}\">")
    return text[span[0]:span[1]]


def insert_before(text: str, marker_regex: str, new_content: str, count: int = 1) -> str:
    """marker_regexにマッチする箇所の直前にnew_contentを挿入する。"""
    m = re.search(marker_regex, text, re.S)
    if not m:
        raise ValueError(f"marker not found: {marker_regex!r}")
    pos = m.start()
    return text[:pos] + new_content + text[pos:]


def insert_after(text: str, marker_regex: str, new_content: str) -> str:
    m = re.search(marker_regex, text, re.S)
    if not m:
        raise ValueError(f"marker not found: {marker_regex!r}")
    pos = m.end()
    return text[:pos] + new_content + text[pos:]


def replace_span(text: str, start: int, end: int, new_content: str) -> str:
    return text[:start] + new_content + text[end:]


_OBJECTREF_RE = re.compile(r'ObjectRef="(\d+)"')
_OBJECTUREF_RE = re.compile(r'ObjectURef="([0-9a-fA-F-]{36})"')


class GraphCloner:
    """既存オブジェクトのサブグラフを丸ごと複製し、ObjectID/ObjectUIDだけを
    新規に割り当て直すためのヘルパー。

    使い方:
        cloner = GraphCloner(source_text, allocator)
        cloner.plan("ArbVideoComponentParam", "1609")          # ObjectID
        cloner.plan("MasterClip", "2c6cb90b-...", by_uid=True)  # ObjectUID
        ...
        blocks = cloner.render()   # 新ID適用済みの本文リスト
        new_id = cloner.id_map["1609"]

    plan()で列挙した「自分自身の宣言」だけ新IDに差し替え、ブロック内部の
    ObjectRef/ObjectURef はplan()済みの他オブジェクトを指しているものだけ
    新IDへ付け替える(plan外の参照=意図的に共有し続けたい既存オブジェクトへの
    参照はそのまま残す)。
    """

    def __init__(self, source_text: str, allocator: ObjectIdAllocator):
        self.source_text = source_text
        self.alloc = allocator
        self.id_map: dict = {}
        self.guid_map: dict = {}
        self._plan: list = []  # (tag, old_id, by_uid)

    def plan(self, tag: str, old_id: str, by_uid: bool = False) -> str:
        if by_uid:
            new_id = self.guid_map.setdefault(old_id, new_guid())
        else:
            new_id = self.id_map.setdefault(old_id, str(self.alloc.next()))
        self._plan.append((tag, old_id, by_uid))
        return new_id

    def render(self) -> list:
        blocks = []
        for tag, old_id, by_uid in self._plan:
            if by_uid:
                span = find_block_by_object_uid(self.source_text, tag, old_id)
                attr = "ObjectUID"
                new_self_id = self.guid_map[old_id]
            else:
                span = find_block_by_object_id(self.source_text, tag, old_id)
                attr = "ObjectID"
                new_self_id = self.id_map[old_id]
            if span is None:
                raise ValueError(f"clone対象が見つかりません: <{tag} {attr}=\"{old_id}\">")
            block = self.source_text[span[0]:span[1]]
            block = re.sub(
                rf'(<{tag} {attr}=")' + re.escape(old_id) + r'(")',
                lambda m: m.group(1) + new_self_id + m.group(2),
                block, count=1,
            )
            block = _OBJECTREF_RE.sub(
                lambda m: f'ObjectRef="{self.id_map.get(m.group(1), m.group(1))}"', block)
            block = _OBJECTUREF_RE.sub(
                lambda m: f'ObjectURef="{self.guid_map.get(m.group(1), m.group(1))}"', block)
            blocks.append(block)
        return blocks
