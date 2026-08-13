#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
align_script.py
================

生の音声ファイル + 台本テキストだけから、
  1. faster-whisper で単語レベルタイムスタンプ付き音声認識
  2. 台本テキスト(正)への文字単位フォーストアライメント
     (difflib.SequenceMatcher でWhisper認識結果と台本を突き合わせ、
      Whisper側の時刻を台本の各文字へ補間して転写する)
  3. 台本を句読点/改行でキューに分割し、各キューのstart/endを算出
を行い、prproj_generate.py へ渡すJSONを出力する。

強調フレーズ・スタイル・SEの判定はAPI課金が発生するためこのスクリプト
自体では行わない(Claude API呼び出しは削除済み)。2段階で使う:

    # 1. まずキュー(タイムスタンプ付き台本の区切り)だけを出力する
    python3 align_script.py --audio narration.wav --script script.txt -o cues.json

    # 2. cues.jsonを見て(Claude Codeなど)人手/別プロセスで強調割り当てを
    #    決め、{"1": [{"phrase":"...","style_name":"...","se_file":"..."}], ...}
    #    という cue index -> 強調リスト の辞書(emphasis_input.json)を作る

    # 3. emphasis_input.jsonを渡して最終的なalign.json(フレーズ単位の
    #    タイムスタンプ付き)を生成する。音声認識はやり直さず高速。
    python3 align_script.py --audio narration.wav --script script.txt \
        --emphasis-json emphasis_input.json -o align.json

既知の限界
----------
ASR+文字単位補間によるタイムスタンプは目安(概ね±100〜300ms程度のズレが
あり得る)。全件を鵜呑みにせず、生成後に数件を実音声(afplay等)と
突き合わせて検算することを推奨する。

スタイル -> SE対応表
--------------------
style_se_categories.json にポジティブ/ネガティブ/強調テロップ各カテゴリの
スタイル名 -> SEファイルの固定対応表がある。emphasis_input.json側で
se_fileを指定しても、style_nameがこの表に載っていれば必ず表の値で
上書きする(手動指定より対応表を優先し、スタイル選択とSEの対応関係の
誤りを防ぐ)。
"""

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace as _dataclass_replace
from typing import List, Dict, Optional, Tuple

# シーケンスのフレームレート。テンプレによって異なるため既定値は置かず、
# CLIの--frame-rateで明示指定するか、auto_telop.cli側でテンプレの
# VideoTrackGroup(FrameRateタグ、ticks/フレーム)から自動検出した値を渡す。
# 未指定時は29.97fps(1001/30000秒)にフォールバックする。
FRAME_DURATION_SEC = 1001 / 30000


def snap_to_frame(t: float) -> float:
    return round(t / FRAME_DURATION_SEC) * FRAME_DURATION_SEC

# 文字色(\cfN)のRGB -> カテゴリ名。ユーザーが手動で色分けした台本RTFの
# 色ルールに対応する。RTFのcolortblインデックス番号は、緑ハイライトの
# 追加などでカラーテーブルに新しい色が挿入されると後続のインデックスが
# ずれてしまう(実例: 「コピー2.rtf」では緑追加によりcf3(赤)がcf4に、
# cf4(オレンジ)がcf5にずれていた)。インデックス番号ではなく実際のRGB値で
# 判定することで、この種のズレに影響されないようにする。
CATEGORY_RGB = {
    "ネガティブ": (0, 0, 255),      # 青
    "ポジティブ": (251, 2, 7),      # 赤
    "強調テロップ": (253, 128, 8),  # オレンジ
}
# 背景ハイライト色(\cbN)のRGB。この色の行は「1行=1画像」を配置する対象
# (緑ハイライト)。
IMAGE_BG_RGB = (33, 255, 6)

_COLOR_MATCH_THRESHOLD = 80.0  # RGBユークリッド距離。既知色同士は150以上離れている


def _hex_to_rgb(hexcolor: str) -> Tuple[int, int, int]:
    h = hexcolor.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _closest_match(hexcolor: Optional[str], palette: Dict[str, Tuple[int, int, int]]) -> Optional[str]:
    if not hexcolor:
        return None
    rgb = _hex_to_rgb(hexcolor)
    best_name, best_dist = None, None
    for name, target in palette.items():
        dist = sum((a - b) ** 2 for a, b in zip(rgb, target)) ** 0.5
        if best_dist is None or dist < best_dist:
            best_name, best_dist = name, dist
    if best_dist is not None and best_dist <= _COLOR_MATCH_THRESHOLD:
        return best_name
    return None


def _dominant_match(runs: List[Tuple[str, Optional[str]]], palette: Dict[str, Tuple[int, int, int]]) -> Optional[str]:
    """(text, hexcolor)のリストから、非空白文字数で重み付けした多数決で
    最も優勢なカテゴリを1つ選ぶ(1行内で複数の色ランに分かれていても
    実データは基本的に単一色のため、ほぼ常に1つに定まる)。"""
    counts: Dict[str, int] = {}
    for text, color in runs:
        cat = _closest_match(color, palette)
        if cat is None:
            continue
        counts[cat] = counts.get(cat, 0) + len(text.strip())
    if not counts:
        return None
    return max(counts, key=counts.get)


# ---------------------------------------------------------------------------
# RTF台本パーサー(色分け済み台本を読み込む)
# ---------------------------------------------------------------------------
#
# 純Python実装(OS非依存。以前はmacOS専用の`textutil`コマンドでRTF→HTML変換
# してから解析していたが、Windowsでも動かすため自前でRTFをパースする)。
#
# RTF内の日本語テキストのエスケープ方式はファイルによって異なる(実際に確認済み):
#   (a) \uNNNNN 形式のUnicodeエスケープ(10進コードポイント、\uc で後続の
#       フォールバック文字数を指定。0以上32767以下を超える値は負数表現になりうる)
#   (b) \'XX 形式の2バイトコードページ(cp932)16進エスケープ。連続する
#       \'XX\'XX...をバイト列としてまとめてcp932でデコードする必要がある
#       (1バイトずつ独立にデコードすると文字化けする)。
# 両方式が同一ファイル内に混在するケースも実在するため、両方に対応する。
#
# 色分けの判定は文字色(\cfN)・背景色(\cbN)の実RGB値で行う(colortblの
# インデックス番号は、色の追加/削除でファイルごとにずれるため使わない。
# 実例: 「コピー2.rtf」では緑ハイライト追加によりcf3(赤)がcf4に、
# cf4(オレンジ)がcf5にずれていた)。

_RTF_SKIP_DESTINATIONS = {
    "fonttbl", "colortbl", "stylesheet", "info", "generator", "pict",
    "object", "listtable", "listoverridetable", "revtbl", "latentstyles",
    "themedata", "colorschememapping", "panose", "datastore", "xmlnstbl",
    "rsid", "filetbl", "listtext", "footnote", "bkmkstart", "bkmkend",
    "field", "fldinst", "shp", "shpinst", "nonshppict", "atnid", "atnauthor",
    "atndate", "atnicn", "atnref", "annotation",
}

_RTF_CONTROL_WORD_RE = re.compile(rb"([A-Za-z]+)(-?\d+)?[ ]?")


@dataclass
class _RtfScope:
    cf: int = 0
    cb: int = 0
    uc: int = 1
    skip: bool = False


def _extract_balanced_group(data: bytes, keyword: bytes) -> Optional[bytes]:
    """`{\\keyword ...}` の中身(外側の`{`/`}`を除く)を、`{`/`}`のネストを
    数えながら取り出す(fonttblは`{\\f0 ...;}{\\f1 ...;}`のように
    フォント単位でさらにネストされることがあるため、単純な非貪欲正規表現では
    壊れる場合がある)。"""
    m = re.search(rb"\{\\" + keyword + rb"\b", data)
    if not m:
        return None
    i = m.end()
    n = len(data)
    depth = 1
    start = i
    while i < n and depth > 0:
        c = data[i:i + 1]
        if c == b"\\":
            i += 2
            continue
        if c == b"{":
            depth += 1
        elif c == b"}":
            depth -= 1
            if depth == 0:
                return data[start:i]
        i += 1
    return data[start:i]


def _parse_rtf_colortable(data: bytes) -> Dict[int, Tuple[int, int, int]]:
    """`{\\colortbl;\\red..\\green..\\blue..;...}` からインデックス->RGBの
    対応表を作る(インデックス0は"自動色"を表す空エントリで、表には含めない)。"""
    content = _extract_balanced_group(data, b"colortbl")
    if content is None:
        return {}
    entries = content.split(b";")
    table: Dict[int, Tuple[int, int, int]] = {}
    for idx, entry in enumerate(entries):
        if idx == 0:
            continue  # 先頭の空エントリ = 自動色(インデックス0)
        rgb_m = re.search(rb"\\red(\d+)\\green(\d+)\\blue(\d+)", entry)
        if rgb_m:
            table[idx] = (int(rgb_m.group(1)), int(rgb_m.group(2)), int(rgb_m.group(3)))
    return table


def _decode_rtf_hex_bytes(buf: bytes) -> str:
    """連続する`\\'XX`エスケープのバイト列をデコードする。

    フォント(\\fcharset)の宣言だけでは実際のバイト意味論を判定できない
    (実データで、fcharset0=ANSI指定のフォント配下でも2バイトShift-JIS
    (cp932)の日本語が普通に出てくるケースを確認済み)。そのため、貪欲に
    cp932としてペアリングを試み、有効な文字として解釈できないバイトだけ
    1バイトのcp1252にフォールバックする(実データで確認した挙動: 単独の
    0x85などcp932では未定義のバイトは、Windows-1252での意味(例: …)で
    使われている)。"""
    def _cp932_or_none(chunk: bytes) -> Optional[str]:
        # Pythonのcp932コーデックは、一部の未定義バイト(0x80,0xA0,0xFD-0xFF等)を
        # 例外を出さず私用領域(Private Use Area)へマップしてしまう。実データ上
        # そういったバイトはcp1252側の意味(NBSP等)で使われていたため、
        # PUAへのマップは「デコード失敗」とみなしてcp1252側へフォールバックする。
        try:
            text = chunk.decode("cp932")
        except UnicodeDecodeError:
            return None
        if any(0xE000 <= ord(ch) <= 0xF8FF for ch in text):
            return None
        return text

    result = []
    i = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        if (0x81 <= b <= 0x9F or 0xE0 <= b <= 0xFC) and i + 1 < n:
            pair = _cp932_or_none(buf[i:i + 2])
            if pair is not None:
                result.append(pair)
                i += 2
                continue
        single = _cp932_or_none(bytes([b]))
        if single is not None:
            result.append(single)
        else:
            try:
                result.append(bytes([b]).decode("cp1252"))
            except UnicodeDecodeError:
                result.append("�")
        i += 1
    return "".join(result)


def _rgb_to_hex(rgb: Optional[Tuple[int, int, int]]) -> Optional[str]:
    if rgb is None:
        return None
    r, g, b = rgb
    return f"#{r:02x}{g:02x}{b:02x}"


def _iter_rtf_tokens(data: bytes):
    """RTFバイト列を歩き、(open,)/(close,)/(cw,word,param)/(hex,byte)/
    (lit,bytes) のトークン列を生成する。"""
    i = 0
    n = len(data)
    while i < n:
        c = data[i:i + 1]
        if c == b"\\":
            i += 1
            if i >= n:
                break
            nc = data[i:i + 1]
            if nc == b"'":
                hex_digits = data[i + 1:i + 3]
                i += 3
                try:
                    yield ("hex", int(hex_digits, 16))
                except ValueError:
                    pass
                continue
            if nc in (b"{", b"}", b"\\"):
                i += 1
                yield ("lit", nc)
                continue
            if nc in (b"\r", b"\n"):
                # バックスラッシュ+改行 = \par の省略記法(Mac製RTFで多用される)
                i += 1
                if nc == b"\r" and data[i:i + 1] == b"\n":
                    i += 1
                yield ("cw", "par", None)
                continue
            if nc == b"~":
                i += 1
                yield ("lit", "\u00a0".encode("utf-8"))
                continue
            if nc == b"_":
                i += 1
                yield ("lit", b"-")
                continue
            if nc == b"-":
                i += 1  # optional hyphen: 表示上は不可視なので読み飛ばす
                continue
            if nc == b"*":
                # 拡張destination groupの印({\*\keyword ...})。keywordを
                # 認識できない場合は無視すべき、という指示なので常にskip対象にする。
                i += 1
                yield ("star",)
                continue
            if nc.isalpha():
                m = _RTF_CONTROL_WORD_RE.match(data, i)
                word = m.group(1).decode("ascii")
                param_raw = m.group(2)
                param = int(param_raw) if param_raw else None
                i = m.end()
                yield ("cw", word, param)
                continue
            # 未知の制御記号(1文字)は読み飛ばす
            i += 1
            continue
        elif c == b"{":
            i += 1
            yield ("open",)
        elif c == b"}":
            i += 1
            yield ("close",)
        elif c in (b"\r", b"\n", b"\t"):
            i += 1  # ソース整形用の生の改行/タブは無視(RTF仕様上テキストではない)
            continue
        else:
            j = i
            while j < n and data[j:j + 1] not in (b"\\", b"{", b"}", b"\r", b"\n", b"\t"):
                j += 1
            yield ("lit", data[i:j])
            i = j


def _walk_rtf(data: bytes) -> List[Dict[str, object]]:
    colortable = _parse_rtf_colortable(data)
    paragraphs: List[Dict[str, object]] = []
    fg_runs: List[Tuple[str, Optional[str]]] = []
    bg_runs: List[Tuple[str, Optional[str]]] = []
    stack = [_RtfScope()]
    pending_hex = bytearray()
    uc_skip = 0
    pending_high_surrogate: Optional[int] = None

    def emit(text: str):
        nonlocal uc_skip
        if not text:
            return
        if uc_skip > 0:
            skip_n = min(uc_skip, len(text))
            text = text[skip_n:]
            uc_skip -= skip_n
            if not text:
                return
        if stack[-1].skip:
            return
        scope = stack[-1]
        fg_hex = _rgb_to_hex(colortable.get(scope.cf))
        bg_hex = _rgb_to_hex(colortable.get(scope.cb))
        fg_runs.append((text, fg_hex))
        bg_runs.append((text, bg_hex))

    def flush_surrogate():
        nonlocal pending_high_surrogate
        if pending_high_surrogate is not None:
            emit(chr(pending_high_surrogate))
            pending_high_surrogate = None

    def flush_hex():
        nonlocal pending_hex
        if pending_hex:
            text = _decode_rtf_hex_bytes(bytes(pending_hex))
            pending_hex = bytearray()
            emit(text)

    def end_paragraph():
        nonlocal fg_runs, bg_runs
        flush_surrogate()
        flush_hex()
        text = "".join(t for t, _ in fg_runs)
        paragraphs.append({"text": text, "fg_runs": fg_runs, "bg_runs": bg_runs})
        fg_runs = []
        bg_runs = []

    just_opened = False
    for tok in _iter_rtf_tokens(data):
        kind = tok[0]
        if kind == "open":
            flush_surrogate()
            flush_hex()
            stack.append(_dataclass_replace(stack[-1]))
            just_opened = True
            continue
        if kind == "close":
            flush_surrogate()
            flush_hex()
            if len(stack) > 1:
                stack.pop()
            just_opened = False
            continue
        if kind == "star":
            # {\*\keyword ...} 形式のdestination group。次に来るkeywordが
            # 何であれ本文テキストとしては扱わない。
            if just_opened:
                stack[-1].skip = True
            continue
        if kind == "cw":
            word, param = tok[1], tok[2]
            if just_opened and word in _RTF_SKIP_DESTINATIONS:
                stack[-1].skip = True
            just_opened = False
            if word == "cf":
                flush_surrogate()
                flush_hex()
                stack[-1].cf = param if param is not None else 0
            elif word in ("cb", "highlight"):
                flush_surrogate()
                flush_hex()
                stack[-1].cb = param if param is not None else 0
            elif word == "uc":
                stack[-1].uc = param if param is not None else 1
            elif word == "f":
                # フォント切り替え時にも念のためhexバッファを確定させておく
                # (デコード自体はフォントに依存しない。_decode_rtf_hex_bytes参照)。
                flush_surrogate()
                flush_hex()
            elif word == "u":
                flush_hex()
                cp = param if param is not None else 0
                if cp < 0:
                    cp += 65536
                if 0xD800 <= cp <= 0xDBFF:
                    flush_surrogate()
                    pending_high_surrogate = cp
                elif 0xDC00 <= cp <= 0xDFFF and pending_high_surrogate is not None:
                    combined = 0x10000 + (pending_high_surrogate - 0xD800) * 0x400 + (cp - 0xDC00)
                    pending_high_surrogate = None
                    emit(chr(combined))
                else:
                    flush_surrogate()
                    emit(chr(cp))
                uc_skip = stack[-1].uc
            elif word in ("par", "line"):
                flush_surrogate()
                flush_hex()
                end_paragraph()
            continue
        if kind == "hex":
            just_opened = False
            pending_hex.append(tok[1])
            continue
        if kind == "lit":
            just_opened = False
            flush_surrogate()
            flush_hex()
            raw = tok[1]
            try:
                text = raw.decode("ascii")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            emit(text)
            continue

    flush_surrogate()
    flush_hex()
    if fg_runs or bg_runs:
        end_paragraph()
    return paragraphs


def parse_rtf(path: str) -> List[Dict[str, object]]:
    """手動で色分け済みのRTF台本を読み込み、行(段落)単位のリストを返す。
    各要素: {"text": str, "category": Optional[str], "is_image": bool}
    - category: 文字色から判定した ポジティブ/ネガティブ/強調テロップ (Noneなら基本テロップ)
    - is_image: 背景ハイライト(緑)が付いている行かどうか
      (3行連続で緑になっている場合も1行=1エントリのまま。まとめない)
    """
    with open(path, "rb") as f:
        data = f.read()
    paragraphs = _walk_rtf(data)

    lines: List[Dict[str, object]] = []
    for para in paragraphs:
        text = str(para["text"]).strip()
        if not text:
            continue
        category = _dominant_match(para["fg_runs"], CATEGORY_RGB)
        is_image = _dominant_match(para["bg_runs"], {"image": IMAGE_BG_RGB}) == "image"
        lines.append({"text": text, "category": category, "is_image": is_image})
    return lines


# ---------------------------------------------------------------------------
# 音声認識 (faster-whisper)
# ---------------------------------------------------------------------------

@dataclass
class Word:
    text: str
    start: float
    end: float


def load_whisper_model(model_size: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel
    print(f"  Whisperモデル読み込み中... ({model_size})", file=sys.stderr)
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def transcribe_words_with_model(model, audio_path: str, **transcribe_kwargs) -> List[Word]:
    segments, info = model.transcribe(
        audio_path,
        language="ja",
        word_timestamps=True,
        vad_filter=True,
        **transcribe_kwargs,
    )
    words: List[Word] = []
    for seg in segments:
        if not seg.words:
            continue
        for w in seg.words:
            token = w.word.strip()
            if not token:
                continue
            words.append(Word(text=token, start=w.start, end=w.end))
    return words


def transcribe_words(audio_path: str, model_size: str, device: str, compute_type: str) -> List[Word]:
    model = load_whisper_model(model_size, device, compute_type)
    print("  音声認識中...", file=sys.stderr)
    words = transcribe_words_with_model(model, audio_path)
    print(f"  認識単語数: {len(words)}", file=sys.stderr)
    return words


# ---------------------------------------------------------------------------
# フォーストアライメント (台本文字 <- Whisper単語の文字単位展開)
# ---------------------------------------------------------------------------

def _normalize_for_match(s: str) -> str:
    """比較用の正規化: 空白除去のみ(文字種の変換はしない=誤爆防止)。"""
    return re.sub(r"\s+", "", s)


def build_char_time_map(script_text: str, words: List[Word]) -> List[Optional[float]]:
    """script_text の各文字(正規化前のインデックスに対応)に対する
    推定タイムスタンプ(秒)のリストを返す。

    手順:
      1. Whisper単語列を1文字ずつに展開した「認識文字列」を作り、
         各文字が属する単語のstart/endから線形補間した時刻を持たせる。
      2. script_text から空白を除いた「比較用文字列」を作る。
      3. difflib.SequenceMatcher で 2 と 認識文字列 のマッチブロックを求め、
         一致した文字については認識側の時刻をそのままコピー、
         一致しない区間は前後の既知時刻から線形補間する。
      4. 空白を除いた文字列上のインデックスを元のscript_text上の
         インデックスへ戻す。
    """
    # 認識側: 1文字ずつ展開 + 各文字の時刻(単語内で線形補間)
    rec_chars: List[str] = []
    rec_times: List[float] = []
    for w in words:
        n = len(w.text)
        if n == 0:
            continue
        span = max(w.end - w.start, 0.0)
        for i, ch in enumerate(w.text):
            t = w.start + span * (i / n)
            rec_chars.append(ch)
            rec_times.append(t)
    rec_str = "".join(rec_chars)

    # 台本側: 空白を除いた比較用文字列 + 元インデックスへの対応表
    norm_chars: List[str] = []
    norm_to_orig: List[int] = []
    for i, ch in enumerate(script_text):
        if ch.isspace():
            continue
        norm_chars.append(ch)
        norm_to_orig.append(i)
    norm_str = "".join(norm_chars)

    norm_times: List[Optional[float]] = [None] * len(norm_str)

    sm = difflib.SequenceMatcher(a=norm_str, b=rec_str, autojunk=False)
    for tag, a0, a1, b0, b1 in sm.get_opcodes():
        if tag == "equal":
            for k in range(a1 - a0):
                norm_times[a0 + k] = rec_times[b0 + k]
        elif tag == "replace" and (a1 - a0) > 0 and (b1 - b0) > 0:
            # 文字数が違っても対応区間の時刻レンジを均等割りする
            t0 = rec_times[b0]
            t1 = rec_times[b1 - 1]
            n = a1 - a0
            for k in range(n):
                frac = k / max(n - 1, 1)
                norm_times[a0 + k] = t0 + (t1 - t0) * frac
        # tag == "delete" (台本にあるがWhisperに無い) は後で補間
        # tag == "insert" (Whisperにあるが台本に無い) は無視

    # 未確定箇所(None)を前後の既知時刻から線形補間
    n = len(norm_times)
    i = 0
    while i < n:
        if norm_times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and norm_times[j] is None:
            j += 1
        prev_t = norm_times[i - 1] if i > 0 else None
        next_t = norm_times[j] if j < n else None
        if prev_t is None and next_t is None:
            prev_t = next_t = 0.0
        elif prev_t is None:
            prev_t = next_t
        elif next_t is None:
            next_t = prev_t
        span = j - i + 1
        for k in range(i, j):
            frac = (k - i + 1) / span
            norm_times[k] = prev_t + (next_t - prev_t) * frac
        i = j

    # 元のscript_textのインデックスへ展開(空白文字にはNoneのまま)
    full_times: List[Optional[float]] = [None] * len(script_text)
    for norm_idx, orig_idx in enumerate(norm_to_orig):
        full_times[orig_idx] = norm_times[norm_idx]
    return full_times


def char_time(times: List[Optional[float]], idx: int) -> float:
    """times[idx]がNone(空白等)の場合、前後の非None値から補間して返す。"""
    if 0 <= idx < len(times) and times[idx] is not None:
        return times[idx]
    n = len(times)
    lo = idx
    while lo >= 0 and (lo >= n or times[lo] is None):
        lo -= 1
    hi = idx
    while hi < n and times[hi] is None:
        hi += 1
    lo_t = times[lo] if lo >= 0 else None
    hi_t = times[hi] if hi < n else None
    if lo_t is None and hi_t is None:
        return 0.0
    if lo_t is None:
        return hi_t
    if hi_t is None:
        return lo_t
    return (lo_t + hi_t) / 2


# ---------------------------------------------------------------------------
# しゃべり出しタイミングの精密化(キュー単位の局所再認識)
# ---------------------------------------------------------------------------
#
# フルファイル一括のWhisper認識+文字単位補間だけでは、長尺音声全体に
# わたる時刻のドリフト・補間誤差により、フレーム単位での正確な一致は
# 保証できない(実測でも±0.1〜0.4秒程度のズレが確認された)。
# そのため、各キューの開始位置周辺だけを切り出して個別に再認識する
# ことで精度を上げる(短い区間の方がWhisperの単語タイムスタンプが安定する
# ことを実測で確認済み)。音声波形のエネルギーに基づく無音検出は、
# 自然な会話音声では文と文の間に明確な無音が無いことが多く、直前の単語の
# 減衰(トレイル)を誤検出しやすいため採用しない。

def refine_cue_onsets(model, audio_path: str, cues: list,
                       pad_before: float = 1.2, pad_after: float = 2.5,
                       max_shift: float = 0.7, checkpoint_path: str = None,
                       chunk_size: int = 20) -> None:
    """cues(Cueのリスト、start属性を持つ)の各startを、その付近だけを
    切り出した局所再認識で精密化し、フレーム境界にスナップして
    in-placeで書き換える。

    ラフ推定(全体一括認識ベース)から max_shift 秒以上離れた結果は
    誤検出とみなし、ラフ推定+フレームスナップのみに留める(暴走防止)。

    長尺(数百キュー)だと数十分かかりうる処理のため、
    - chunk_size件ごとに進捗をstderrへflush出力する
      (標準出力がパイプ/ファイルにリダイレクトされているとPythonの
      デフォルトのブロックバッファリングにより、プロセス終了までログが
      一切見えず「フリーズしたように見える」問題があったため、必ず
      flush=Trueで都度出力する)
    - checkpoint_path指定時はchunk_size件処理するごとに、その時点までの
      cueごとのstart(精密化済み/未処理)をJSONに書き出す。処理中に
      強制終了しても、そこまでの進捗をファイルで確認できる。
    """
    tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".onset_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    n = len(cues)
    t_start = time.time()
    try:
        for i, cue in enumerate(cues):
            rough_start = cue.start
            slice_start = max(0.0, rough_start - pad_before)
            slice_end = rough_start + pad_after
            tmp_wav = os.path.join(tmp_dir, f"cue_{i:04d}.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-i", audio_path, "-ss", str(slice_start), "-to", str(slice_end),
                 tmp_wav, "-hide_banner", "-loglevel", "error"],
                check=True,
            )
            local_words = transcribe_words_with_model(model, tmp_wav)
            os.remove(tmp_wav)
            if not local_words:
                cue.start = snap_to_frame(rough_start)
            else:
                local_times = build_char_time_map(cue.text, local_words)
                onset_local = char_time(local_times, 0)
                onset_absolute = slice_start + onset_local
                if abs(onset_absolute - rough_start) > max_shift:
                    print(f"    [警告] index={cue.index} {cue.text!r}: 精密化結果が"
                          f"ラフ推定から{onset_absolute - rough_start:+.3f}秒ズレて"
                          f"おり誤検出とみなして棄却(ラフ推定{rough_start:.3f}sを採用)",
                          file=sys.stderr)
                    onset_absolute = rough_start
                cue.start = snap_to_frame(max(0.0, onset_absolute))

            done = i + 1
            if done % chunk_size == 0 or done == n:
                elapsed = time.time() - t_start
                rate = elapsed / done
                eta = rate * (n - done)
                print(f"    [{done}/{n}] 精密化完了 (経過{elapsed:.0f}秒 / 残り目安{eta:.0f}秒)",
                      file=sys.stderr, flush=True)
                if checkpoint_path:
                    checkpoint = [
                        {"index": c.index, "text": c.text, "start": round(c.start, 6)}
                        for c in cues[:done]
                    ]
                    tmp_ckpt = checkpoint_path + ".tmp"
                    with open(tmp_ckpt, "w", encoding="utf-8") as f:
                        json.dump(checkpoint, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_ckpt, checkpoint_path)
    finally:
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# キュー分割
# ---------------------------------------------------------------------------

@dataclass
class Cue:
    index: int
    text: str
    start_char: int  # script_text内の開始インデックス(inclusive)
    end_char: int     # script_text内の終了インデックス(exclusive)
    start: float = 0.0
    end: float = 0.0
    category: Optional[str] = None  # RTFモードのみ: ポジティブ/ネガティブ/強調テロップ/None(=基本テロップ)
    is_image: bool = False  # RTFモードのみ: 背景ハイライト(緑)行=画像を配置する行


_SPLIT_RE = re.compile(r"([^。！？\n]*[。！？\n]|[^。！？\n]+$)")


def split_into_cues(script_text: str, times: List[Optional[float]]) -> List[Cue]:
    cues: List[Cue] = []
    pos = 0
    index = 1
    for m in _SPLIT_RE.finditer(script_text):
        raw = m.group(0)
        text = raw.strip()
        start_char = m.start()
        end_char = m.end()
        if not text:
            pos = end_char
            continue
        cue = Cue(index=index, text=text, start_char=start_char, end_char=end_char)
        cue.start = char_time(times, start_char)
        cue.end = char_time(times, max(end_char - 1, start_char))
        cues.append(cue)
        index += 1
        pos = end_char
    return cues


def build_script_text_from_rtf_lines(rtf_lines: List[Dict[str, object]]) -> str:
    """RTFの行リストから、Whisperアライメント用の1本の script_text を作る
    (行と行の間は改行区切り、文字位置はそのままCueのstart_char/end_charに
    対応させる)。"""
    return "\n".join(str(line["text"]) for line in rtf_lines)


def build_cues_from_rtf_lines(rtf_lines: List[Dict[str, object]], script_text: str,
                               times: List[Optional[float]]) -> List[Cue]:
    """RTFの行リスト(色分け済み、build_script_text_from_rtf_linesで作った
    script_textと対応させる)から、1行=1Cueとして直接Cueリストを作る。
    通常の split_into_cues と違い、句読点等での再分割はしない
    (RTFの改行そのものが正解の区切りであるため)。"""
    cues: List[Cue] = []
    pos = 0
    for i, line in enumerate(rtf_lines, start=1):
        line_text = str(line["text"])
        start_char = pos
        end_char = pos + len(line_text)
        cue = Cue(index=i, text=line_text, start_char=start_char, end_char=end_char,
                   category=line.get("category"), is_image=bool(line.get("is_image")))
        cue.start = char_time(times, start_char)
        cue.end = char_time(times, max(end_char - 1, start_char))
        cues.append(cue)
        pos = end_char + 1  # "\n" 区切り分を1文字進める
    return cues


# ---------------------------------------------------------------------------
# 基本テロップ(逐語キャプション)用の分割: 無音区間検出 + 文字数上限
# ---------------------------------------------------------------------------

def _make_nearest_char_index_fn(times: List[Optional[float]]):
    known = [(i, t) for i, t in enumerate(times) if t is not None]
    known_times = [t for _, t in known]

    import bisect

    def nearest_char_index(target_time: float):
        if not known:
            return None
        pos = bisect.bisect_left(known_times, target_time)
        if pos <= 0:
            return known[0][0]
        if pos >= len(known_times):
            return known[-1][0]
        before_i, before_t = known[pos - 1]
        after_i, after_t = known[pos]
        return before_i if abs(before_t - target_time) <= abs(after_t - target_time) else after_i

    return nearest_char_index


def find_silence_cut_positions(script_text: str, times: List[Optional[float]],
                                words: List[Word], min_gap: float = 0.3) -> set:
    """Whisperの単語間で min_gap 秒以上の無音区間がある箇所を検出し、
    その無音区間の開始時刻に最も近い script_text 上の文字インデックス
    (その文字の直後で区切ってよい位置)の集合を返す。"""
    nearest_char_index = _make_nearest_char_index_fn(times)
    cut_positions = set()
    for i in range(len(words) - 1):
        gap = words[i + 1].start - words[i].end
        if gap >= min_gap:
            idx = nearest_char_index(words[i].end)
            if idx is not None:
                cut_positions.add(idx)
    return cut_positions


def find_word_boundary_positions(times: List[Optional[float]], words: List[Word]) -> set:
    """Whisperが認識した全単語の境界(区切り候補の無音区間の有無を問わず)を
    script_text上の文字インデックスに変換して返す。無音区間による区切り
    候補が上限文字数以内に見つからない場合の予備の区切り候補として使い、
    単語の途中で強制分割してしまうのを避ける。"""
    nearest_char_index = _make_nearest_char_index_fn(times)
    positions = set()
    for w in words:
        idx = nearest_char_index(w.end)
        if idx is not None:
            positions.add(idx)
    return positions


@dataclass
class CaptionSegment:
    index: int
    text: str
    start_char: int
    end_char: int
    start: float
    end: float


def segment_for_base_captions(cues: List[Cue], times: List[Optional[float]],
                               cut_positions: set, char_cap: int = 16,
                               word_boundary_positions: set = None) -> List[CaptionSegment]:
    """各キュー(文単位)を、無音区間由来の区切り候補を優先しつつ
    char_cap文字程度ごとに分割する(貪欲な行分割アルゴリズム)。
    文をまたいでは絶対に結合しない(キューの境界は必ず区切りにする)。
    word_boundary_positionsを渡すと、無音区間の区切り候補が上限文字数
    以内に見つからない場合の予備の区切り候補として使い、単語の途中で
    強制分割してしまうのを避ける(最後の手段として文字数上限ちょうどで
    強制的に切る)。"""
    word_boundary_positions = word_boundary_positions or set()
    segments: List[CaptionSegment] = []
    seg_index = 1
    for cue in cues:
        text = cue.text
        n = len(text)
        # cue内の相対位置での区切り候補(絶対インデックス - start_char)
        local_cuts = sorted(
            p - cue.start_char for p in cut_positions
            if cue.start_char <= p < cue.end_char
        )
        local_word_cuts = sorted(
            p - cue.start_char for p in word_boundary_positions
            if cue.start_char <= p < cue.end_char
        )
        # 貪欲な行分割: 現在位置から char_cap 以内で一番遠い区切り候補を
        # 使う。優先順位は 無音区間 > 単語境界 > 文字数上限での強制分割。
        # ただし強制分割した場合に残り(孤立した末尾断片)がorphan_max文字
        # 以下になるなら、無理に切らずそのまま末尾まで含める
        # (「13〜17文字程度」という目安のうち上限側に多少はみ出しても、
        # 1〜3文字だけの断片ができるよりまし)。
        orphan_max = 2
        pieces: List[Tuple[int, int]] = []  # (start_rel, end_rel)
        seg_start = 0
        while n - seg_start > char_cap:
            limit = seg_start + char_cap
            if n - limit <= orphan_max:
                break
            best_cut = None
            for c in local_cuts:
                if seg_start < c <= limit:
                    best_cut = c
            if best_cut is None:
                for c in local_word_cuts:
                    if seg_start < c <= limit:
                        best_cut = c
            if best_cut is None:
                best_cut = limit
            pieces.append((seg_start, best_cut))
            seg_start = best_cut
        if seg_start < n:
            pieces.append((seg_start, n))

        for start_rel, end_rel in pieces:
            piece_text = text[start_rel:end_rel].strip()
            if not piece_text:
                continue
            abs_start = cue.start_char + start_rel
            abs_end = cue.start_char + end_rel
            seg = CaptionSegment(
                index=seg_index, text=piece_text,
                start_char=abs_start, end_char=abs_end,
                start=char_time(times, abs_start),
                end=char_time(times, max(abs_end - 1, abs_start)),
            )
            segments.append(seg)
            seg_index += 1
    return segments


def load_style_names(style_json_path: str) -> List[str]:
    with open(style_json_path, encoding="utf-8") as f:
        d = json.load(f)
    return list(d.get("style_name_to_uid", {}).keys())


def load_se_files(style_json_path: str) -> List[str]:
    with open(style_json_path, encoding="utf-8") as f:
        d = json.load(f)
    return list(d.get("se_audio_files", []))


def load_style_se_map(path: str) -> Dict[str, str]:
    """style_se_categories.json を style_name -> se_file のフラットな
    辞書に変換して返す(カテゴリ分けはこの関数の呼び出し側では使わない)。"""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    flat: Dict[str, str] = {}
    for cat in d.get("categories", {}).values():
        flat.update(cat.get("styles", {}))
    return flat


def load_style_category_map(path: str) -> Dict[str, str]:
    """style_se_categories.json を style_name -> カテゴリ名 の辞書に変換する。
    RTFモードで、選択されたスタイルの実カテゴリがRTFの色と一致するか
    検証するために使う。"""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    m: Dict[str, str] = {}
    for cat_name, cat in d.get("categories", {}).items():
        for style_name in cat.get("styles", {}):
            m[style_name] = cat_name
    return m


# ---------------------------------------------------------------------------
# フレーズのタイムスタンプ算出
# ---------------------------------------------------------------------------

def locate_phrase_time(cue: Cue, phrase: str, times: List[Optional[float]]) -> Optional[Tuple[float, float]]:
    idx = cue.text.find(phrase)
    if idx < 0:
        return None
    abs_start = cue.start_char + idx
    abs_end = abs_start + len(phrase)
    if idx == 0:
        # フレーズがキューの先頭と一致する場合(RTFモードでは常にこれ、
        # phraseはcue.textそのもの)は、局所再認識+フレームスナップで
        # 精密化済みのcue.startをそのまま使う。全体一括認識ベースの
        # times配列からの再計算(下のelse節、精度が劣る)には戻さない。
        t0 = cue.start
    else:
        t0 = char_time(times, abs_start)
    t1 = char_time(times, max(abs_end - 1, abs_start))
    return (t0, t1)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

@dataclass
class PreparedCues:
    rtf_mode: bool
    cues: list  # List[Cue]
    times: list
    script_text: str
    base_captions_out: list
    image_cues_out: list


def prepare_cues(audio_path: str, script_path: str = None, rtf_path: str = None,
                  whisper_model: str = "medium", device: str = "cpu", compute_type: str = "int8",
                  silence_gap: float = 0.3, caption_char_cap: int = 16,
                  refine_onsets: bool = True, onset_refine_model: str = None,
                  onset_checkpoint: str = None, frame_rate: float = None,
                  model=None) -> PreparedCues:
    """音声認識・フォーストアライメント・キュー分割・(RTFモードなら)
    しゃべり出し精密化までを行い、cues.json相当の中間状態を返す
    (cli.pyがこの後classify.pyへ渡す/emphasis適用するために使う)。
    modelを渡すとWhisperモデルの再読み込みを省略する。"""
    global FRAME_DURATION_SEC
    if frame_rate:
        FRAME_DURATION_SEC = 1.0 / frame_rate

    if not script_path and not rtf_path:
        raise ValueError("script_path または rtf_path のいずれかを指定してください。")
    rtf_mode = bool(rtf_path)

    if rtf_mode:
        rtf_lines = parse_rtf(rtf_path)
        script_text = build_script_text_from_rtf_lines(rtf_lines)
        print(f"  RTFから{len(rtf_lines)}行を読み込みました。", file=sys.stderr)
    else:
        with open(script_path, encoding="utf-8") as f:
            script_text = f.read()

    if model is None:
        model = load_whisper_model(whisper_model, device, compute_type)
    print("  音声認識中...", file=sys.stderr)
    words = transcribe_words_with_model(model, audio_path)
    print(f"  認識単語数: {len(words)}", file=sys.stderr)
    if not words:
        raise RuntimeError("音声から単語が認識できませんでした。")

    print("  台本とのフォーストアライメント中...", file=sys.stderr)
    times = build_char_time_map(script_text, words)

    if rtf_mode:
        cues = build_cues_from_rtf_lines(rtf_lines, script_text, times)
        print(f"  キュー数(RTF行): {len(cues)}", file=sys.stderr)

        if refine_onsets:
            print(f"  各キューのしゃべり出しタイミングを局所再認識で精密化中... ({len(cues)}件)", file=sys.stderr)
            onset_refine_model_size = onset_refine_model or whisper_model
            if onset_refine_model_size == whisper_model:
                refine_model = model
            else:
                refine_model = load_whisper_model(onset_refine_model_size, device, compute_type)
            refine_cue_onsets(refine_model, audio_path, cues, checkpoint_path=onset_checkpoint)
        else:
            for c in cues:
                c.start = snap_to_frame(c.start)

        # 基本テロップ(V8)同士がフレーム単位で隙間なく繋がるように、
        # 各基本テロップの終了時刻を「次の基本テロップの開始時刻」まで
        # 伸ばす(間に色付き行があってもその区間ごと覆う)。最後の基本
        # テロップだけは次が無いため自身の推定終了時刻のまま。
        base_indices = [i for i, c in enumerate(cues) if c.category is None]
        for a, b in zip(base_indices, base_indices[1:]):
            cues[a].end = cues[b].start

        base_captions_out = [
            {"index": c.index, "text": c.text, "start": round(c.start, 6), "end": round(c.end, 6)}
            for c in cues if c.category is None
        ]
        n_colored = sum(1 for c in cues if c.category is not None)
        print(f"  基本テロップ(黒行): {len(base_captions_out)}件 / 色付き(強調対象)行: {n_colored}件", file=sys.stderr)

        image_cues_out = [
            {"index": c.index, "text": c.text, "start": round(c.start, 6), "end": round(c.end, 6)}
            for c in cues if c.is_image
        ]
        print(f"  画像配置対象(緑ハイライト)行: {len(image_cues_out)}件", file=sys.stderr)
    else:
        cues = split_into_cues(script_text, times)
        print(f"  キュー数: {len(cues)}", file=sys.stderr)

        print(f"  無音区間({silence_gap}秒以上)を検出中...", file=sys.stderr)
        cut_positions = find_silence_cut_positions(script_text, times, words, min_gap=silence_gap)
        word_boundary_positions = find_word_boundary_positions(times, words)
        print(f"  区切り候補: 無音区間{len(cut_positions)}箇所 / 単語境界{len(word_boundary_positions)}箇所", file=sys.stderr)
        caption_segments = segment_for_base_captions(cues, times, cut_positions, char_cap=caption_char_cap,
                                                       word_boundary_positions=word_boundary_positions)
        print(f"  基本テロップ区切り数: {len(caption_segments)}", file=sys.stderr)
        base_captions_out = [
            {"index": s.index, "text": s.text, "start": round(s.start, 6), "end": round(s.end, 6)}
            for s in caption_segments
        ]
        image_cues_out = []

    return PreparedCues(rtf_mode=rtf_mode, cues=cues, times=times, script_text=script_text,
                         base_captions_out=base_captions_out, image_cues_out=image_cues_out)


def apply_emphasis(prepared: PreparedCues, emphasis_json_raw: dict = None,
                    style_json_path: str = None, style_se_categories_path: str = None) -> dict:
    """prepare_cues()の結果に、classify.py等が生成したemphasis_json(または
    手動で書いたもの)を適用し、prproj_generate.pyに渡す最終的な
    align.json相当の辞書を返す。emphasis_json_raw省略時はcues(index/text/
    start/end)だけを返す(stage1相当)。"""
    cues = prepared.cues
    rtf_mode = prepared.rtf_mode
    times = prepared.times
    base_captions_out = list(prepared.base_captions_out)
    image_cues_out = list(prepared.image_cues_out)

    if not emphasis_json_raw:
        out_cues = []
        for c in cues:
            entry = {"index": c.index, "text": c.text, "start": round(c.start, 6), "end": round(c.end, 6), "emphasis": []}
            if rtf_mode and c.category:
                entry["color_category"] = c.category
            out_cues.append(entry)
        return {"cues": out_cues, "base_captions": base_captions_out, "image_cues": image_cues_out}

    if not style_json_path:
        raise ValueError("emphasis_json_raw指定時は style_json_path も必須です。")
    style_names = load_style_names(style_json_path)
    se_files = load_se_files(style_json_path)
    style_se_map = load_style_se_map(style_se_categories_path)
    category_map = load_style_category_map(style_se_categories_path)

    if "results" in emphasis_json_raw:
        emphasis_map_raw = emphasis_json_raw["results"]
        image_queries_raw = emphasis_json_raw.get("image_queries", {})
    else:
        emphasis_map_raw = emphasis_json_raw
        image_queries_raw = {}
    emphasis_map = {int(k): v for k, v in emphasis_map_raw.items()}
    image_queries = {int(k): v for k, v in image_queries_raw.items() if v}

    if image_queries and not rtf_mode:
        cue_by_index = {c.index: c for c in cues}
        for idx, query in image_queries.items():
            c = cue_by_index.get(idx)
            if c is None:
                continue
            image_cues_out.append({
                "index": c.index, "text": c.text, "query": query,
                "start": round(c.start, 6), "end": round(c.end, 6),
            })
        image_cues_out.sort(key=lambda e: e["index"])
        print(f"  画像配置対象(AI判定): {len(image_cues_out)}件", file=sys.stderr)

    out_cues = []
    for c in cues:
        emph_out = []
        for item in emphasis_map.get(c.index, []):
            phrase = item.get("phrase", "")
            style_name = item.get("style_name")
            se_file = item.get("se_file")
            if not phrase or style_name not in style_names:
                print(f"  [警告] キュー{c.index}: 未知のフレーズ/スタイル name={style_name!r} phrase={phrase!r} をスキップ", file=sys.stderr)
                continue
            if rtf_mode and c.category:
                actual_cat = category_map.get(style_name)
                if actual_cat != c.category:
                    print(f"  [警告] キュー{c.index}: RTFの色は{c.category!r}ですが、"
                          f"選択されたスタイル{style_name!r}は{actual_cat!r}カテゴリです。"
                          f"色分けと矛盾しています。", file=sys.stderr)
            loc = locate_phrase_time(c, phrase, times)
            if loc is None:
                print(f"  [警告] キュー{c.index}: フレーズ {phrase!r} がキュー本文に見つからずスキップ", file=sys.stderr)
                continue
            t0, t1 = loc
            entry = {"phrase": phrase, "start": round(t0, 6), "end": round(t1, 6), "style_name": style_name}
            if style_name in style_se_map:
                mapped_se = style_se_map[style_name]
                if se_file and se_file != mapped_se:
                    print(f"  [情報] キュー{c.index}: {style_name!r}のSEを {se_file!r} -> {mapped_se!r} に対応表通り修正", file=sys.stderr)
                entry["se_file"] = mapped_se
            elif se_file and se_file in se_files:
                entry["se_file"] = se_file
            emph_out.append(entry)
        out_cues.append({
            "index": c.index, "text": c.text,
            "start": round(c.start, 6), "end": round(c.end, 6),
            "emphasis": emph_out,
        })
    return {"cues": out_cues, "base_captions": base_captions_out, "image_cues": image_cues_out}


def main():
    parser = argparse.ArgumentParser(description="音声+台本からタイムスタンプ+強調カテゴリのJSONを生成する")
    parser.add_argument("--audio", required=True, help="ナレーション音声ファイル (wav/mp3等)")
    parser.add_argument("--script", help="台本テキストファイル(通常モード)")
    parser.add_argument("--rtf-script",
                         help="色分け済みのRTF台本ファイル(RTFモード。--scriptの代わりに指定する)。"
                              "黒(cf0)=基本テロップ、青(cf2)=ネガティブ、赤(cf3)=ポジティブ、"
                              "オレンジ(cf4)=強調テロップ として各行をそのまま1クリップ単位に使う"
                              "(無音区間検出/文字数上限による自動分割はしない)。")
    parser.add_argument("-o", "--output", required=True, help="出力JSONパス")
    parser.add_argument("--style-json",
                         help="analyze-templateで生成したスタイルカタログJSONのパス"
                              "(--emphasis-json指定時は必須)")
    parser.add_argument("--style-se-categories",
                         help="style_config.jsonのパス(スタイル名->SEファイルの固定対応表、省略可)")
    parser.add_argument("--whisper-model", default="medium", help="faster-whisperのモデルサイズ")
    parser.add_argument("--device", default="cpu", help="faster-whisperのdevice (cpu/cuda)")
    parser.add_argument("--compute-type", default="int8", help="faster-whisperのcompute_type")
    parser.add_argument("--emphasis-json",
                         help="cue index -> [{phrase,style_name,se_file}] の辞書。指定すると"
                              "フレーズ単位のタイムスタンプを計算してalign.jsonを作る。"
                              "省略時はキュー(index/text/start/end)だけを出力する。"
                              "RTFモードではphraseにcueのtextをそのまま指定する想定。")
    parser.add_argument("--silence-gap", type=float, default=0.3,
                         help="[通常モードのみ] 基本テロップの区切り候補とみなす単語間の無音区間の最小秒数")
    parser.add_argument("--caption-char-cap", type=int, default=16,
                         help="[通常モードのみ] 基本テロップ1クリップあたりの目安文字数上限(13〜17文字程度を推奨)")
    parser.add_argument("--no-refine-onsets", action="store_true",
                         help="各キュー開始位置の局所再認識による精密化(既定でON)をスキップする"
                              "(高速だが、しゃべり出しのタイミングがフレーム単位で正確でなくなる)")
    parser.add_argument("--onset-refine-model", default=None,
                         help="しゃべり出し精密化(局所再認識)専用のWhisperモデルサイズ。"
                              "省略時は--whisper-modelと同じものを使う(smallは実測で"
                              "1秒以上の誤検出が発生したため既定では使わない)。")
    parser.add_argument("--onset-checkpoint",
                         help="精密化の進捗チェックポイントJSONの出力先パス"
                              "(省略時は出力パス + '.onset_checkpoint.json')")
    parser.add_argument("--frame-rate", type=float, default=None,
                         help="シーケンスのフレームレート(fps)。指定するとしゃべり出しタイミングを"
                              "このフレーム境界にスナップする。省略時は29.97fps。")
    args = parser.parse_args()

    # 標準エラーがパイプ/ファイルにリダイレクトされるとPythonのデフォルトの
    # ブロックバッファリングにより、プロセス終了までprint()が一切
    # 出力されず「フリーズしたように見える」問題があったため、常に
    # 行バッファリングに切り替えて逐次flushさせる。
    sys.stderr.reconfigure(line_buffering=True)

    if not args.script and not args.rtf_script:
        sys.exit("--script または --rtf-script のいずれかを指定してください。")

    checkpoint_path = args.onset_checkpoint or (args.output + ".onset_checkpoint.json")
    prepared = prepare_cues(
        audio_path=args.audio, script_path=args.script, rtf_path=args.rtf_script,
        whisper_model=args.whisper_model, device=args.device, compute_type=args.compute_type,
        silence_gap=args.silence_gap, caption_char_cap=args.caption_char_cap,
        refine_onsets=not args.no_refine_onsets, onset_refine_model=args.onset_refine_model,
        onset_checkpoint=checkpoint_path, frame_rate=args.frame_rate,
    )

    emphasis_json_raw = None
    if args.emphasis_json:
        if not args.style_json:
            sys.exit("--emphasis-json指定時は --style-json も必須です(analyze-templateで生成してください)。")
        with open(args.emphasis_json, encoding="utf-8") as f:
            emphasis_json_raw = json.load(f)

    out = apply_emphasis(prepared, emphasis_json_raw, args.style_json, args.style_se_categories)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"出力しました: {args.output}", file=sys.stderr)
    print("※ タイムスタンプはASR+補間による目安値です。数件を実音声と突き合わせて確認してください。", file=sys.stderr)


if __name__ == "__main__":
    main()
