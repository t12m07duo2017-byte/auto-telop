#!/usr/bin/env python3
"""
Premiere .prproj 内の AE.ADBE Text (VideoFilterComponent) が持つ
「ソーステキスト」パラメータ (ArbVideoComponentParam, ParameterID=1) の
base64バイナリを読み書きするための最小ライブラリ。

.prproj 自体は gzip 圧縮された XML(PremiereData)。
XMLの中で各オブジェクトは ObjectID を持ち、Params/Param の ObjectRef で
相互参照する「フラットなオブジェクトグラフ」形式になっている。

AE.ADBE Text フィルタの Component/Params の Index="0" が必ず
ParameterID=1 (Name="ソーステキスト") の ArbVideoComponentParam を指す。
その StartKeyframeValue (base64) をデコードしたバイナリの末尾付近に、
表示文字列が次のレイアウトで格納されている(実データ60サンプル中
InstanceNameで検証できた21件全てで確認済み):

    [uint32 LE: UTF-8バイト長 N] [UTF-8本体 Nバイト] [0x00 NUL終端] [0パディング]

パディングはこのレコード末尾がバッファ全体の末尾に一致するように
0-3バイト付与される(バッファ全体の長さを4の倍数にする、ではなく、
単純に「文字列+NUL」の直後がそのままバッファの終端になっている)。

このレコードより前のバイト列(フォント名・色・トランスフォーム等を
含む)は文字列の長さに依存しないオフセットで構成されているため、
このレコードだけを書き換えれば残りは一切変更しなくてよい
(book_replace_source_text で実装しているのはこの「末尾スライス置換」)。

未検証・既知の制限
------------------
- 複数の文字装飾ラン(1つのテキストボックス内で文字ごとに色/太字が
  異なる等)を含む場合の構造は未検証。今回の実データでは単一ランの
  プレーンテキストのみで検証している。改行を含む複数行テキストは
  「お気軽にコメントください」等では改行なしのため未検証。
  複数行/複数ランのテキストを扱う場合は、書き換え前後で
  find_source_text() の再デコード結果を必ず確認すること。
- StartKeyframeValue の BinaryHash 属性はレンダーキャッシュ用と推測
  (サンプルごとの値に規則性がない)。書き換えなくても読み込めるはず
  だが、Premiereで実際に開いて描画が正しいか確認すること。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass


class SourceTextNotFoundError(ValueError):
    pass


@dataclass
class TextFieldLocation:
    len_field_offset: int   # 4バイト長フィールドの開始位置(バッファ先頭からのオフセット)
    text_start: int         # UTF-8本体の開始位置
    text_byte_len: int      # UTF-8本体のバイト長
    text: str               # デコード済み文字列
    buffer_len: int         # 元バッファの全長


def find_source_text(data: bytes) -> TextFieldLocation:
    """バッファ末尾の [len:u32][utf8][NUL][pad] レコードを探して返す。

    末尾に一番近い(オフセットが最大の)候補を採用する。
    """
    n = len(data)
    best = None
    for off in range(0, n - 4):
        L = int.from_bytes(data[off:off + 4], "little")
        if not (0 <= L <= 500):
            continue
        text_start = off + 4
        text_end = text_start + L
        if text_end > n:
            continue
        chunk = data[text_start:text_end]
        try:
            s = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
        rest = data[text_end:]
        if len(rest) == 0 or len(rest) > 8:
            continue
        if any(b != 0 for b in rest):
            continue
        if best is None or off > best.len_field_offset:
            best = TextFieldLocation(off, text_start, L, s, n)
    if best is None:
        raise SourceTextNotFoundError("ソーステキストのフィールドが見つかりませんでした")
    return best


def replace_source_text(data: bytes, new_text: str) -> bytes:
    """データ末尾のソーステキストを new_text に差し替えた新バイト列を返す。

    len_field_offset より前のバイトは一切変更しない。
    """
    loc = find_source_text(data)
    new_utf8 = new_text.encode("utf-8")
    body = struct.pack("<I", len(new_utf8)) + new_utf8 + b"\x00"
    pad = (-len(body)) % 4
    body += b"\x00" * pad
    return data[:loc.len_field_offset] + body
