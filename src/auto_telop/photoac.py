#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
photoac.py
==========

photo-ac.com(写真AC)の検索・ダウンロード(オプション機能)。
ログイン済みセッションのCookieヘッダー文字列が必要(ブラウザの開発者
ツール > Network タブで、ログイン後のリクエストのRequest Headersから
"Cookie:" の値をコピーする)。

Cookieが設定されていない場合、search()/download()は例外を投げず
Noneを返す(呼び出し側=cli.pyは画像配置をスキップして警告を出す)。

検索: GET /main/search?q=<keyword> のHTML内、Pinterest共有ボタンの
      bookmarkletリンクから (detail_id, file_hash, title) を抜き出す。
ダウンロード: GET /dl/?p_id=<detail_id>&sz=<s|m|l>&f=<file_hash>
      Cookie送信で 302 -> CloudFront署名URL -> 実ファイル が返る
      (ログイン会員なら透かし無し・高解像度)。
"""
from __future__ import annotations

import os
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional, Set

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

_PIN_RE = re.compile(
    r"https://pinterest\.com/pin/create/bookmarklet/\?media=https://thumb\.photo-ac\.com/[0-9a-f]{2}/"
    r"([0-9a-f]+)_w\.jpe?g&url=https://www\.photo-ac\.com/main/detail/(\d+)&title=([^&]*)&description="
)


@dataclass
class PhotoResult:
    detail_id: str
    file_hash: str
    title: str


class DownloadBlocked(Exception):
    """ダウンロードURLが画像ファイルではなくHTMLページを返した場合に送出する
    (ダウンロード回数上限到達・セッション無効化等でこうなる)。
    呼び出し側はこれを検知して以降のダウンロード試行を打ち切る判断に使える。"""


def load_cookie(cookie_file: Optional[str] = None) -> Optional[str]:
    """Cookie文字列を取得する。優先順位:
    1. 引数cookie_file(ファイルパス)
    2. 環境変数 PHOTOAC_COOKIE_FILE (ファイルパス)
    3. 環境変数 PHOTOAC_COOKIE (Cookie文字列そのもの)
    どれも無ければNone(=機能を使わない)。"""
    path = cookie_file or os.environ.get("PHOTOAC_COOKIE_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    inline = os.environ.get("PHOTOAC_COOKIE")
    if inline:
        return inline.strip()
    return None


def search(query: str, cookie: str, limit: int = 20) -> List[PhotoResult]:
    result = subprocess.run(
        ["curl", "-s", "-L", "-G", "https://www.photo-ac.com/main/search",
         "--data-urlencode", f"q={query}",
         "-H", f"Cookie: {cookie}", "-A", UA],
        capture_output=True, check=True,
    )
    html = result.stdout.decode("utf-8", errors="replace")

    seen = set()
    out: List[PhotoResult] = []
    for m in _PIN_RE.finditer(html):
        file_hash, detail_id, title_enc = m.group(1), m.group(2), m.group(3)
        if detail_id in seen:
            continue
        seen.add(detail_id)
        title = urllib.parse.unquote(title_enc)
        out.append(PhotoResult(detail_id=detail_id, file_hash=file_hash, title=title))
        if len(out) >= limit:
            break
    return out


def download(detail_id: str, file_hash: str, out_path: str, cookie: str, size: str = "l") -> bool:
    url = f"https://www.photo-ac.com/dl/?p_id={detail_id}&sz={size}&f={file_hash}&openEditorAfterDownload=true"
    result = subprocess.run(
        ["curl", "-s", "-L", url,
         "-H", f"Cookie: {cookie}",
         "-e", f"https://www.photo-ac.com/main/detail/{detail_id}",
         "-A", UA,
         "-o", out_path,
         # "|"区切りにする: content_typeは"text/html; charset=UTF-8"のように
         # 空白を含むことがあり、空白区切りだとsize_downloadの位置がズレる。
         "-w", "%{http_code}|%{content_type}|%{size_download}"],
        capture_output=True, check=True,
    )
    parts = result.stdout.decode().split("|")
    if len(parts) != 3:
        return False
    http_code, content_type, size_str = parts
    try:
        size_bytes = int(size_str)
    except ValueError:
        return False
    if content_type.startswith("text/html"):
        # ダウンロード回数上限到達・セッション無効化等の場合、200 OKのまま
        # 画像ではなくHTMLページ(トップページ/エラーページ)が返ってくる。
        raise DownloadBlocked(
            f"画像の代わりにHTMLページが返されました(ダウンロード上限到達等の可能性): "
            f"http_code={http_code}"
        )
    # 実測: 成功時のcontent-typeは"application/octet-stream"(image/*ではない)
    # ため、ステータス200かつ十分なサイズがあることで成功を判定する。
    return http_code == "200" and size_bytes > 5000


def search_and_download(query: str, out_path: str, cookie: str, min_width: int = 1200,
                         exclude_detail_ids: Optional[Set[str]] = None) -> Optional[str]:
    """queryで検索し、十分な解像度でexclude_detail_ids未使用の最初の候補を
    out_pathへダウンロードする。成功したらdetail_id(呼び出し側が
    exclude_detail_idsに足して以降の行で使い回されないようにする用)、
    見つからなければNoneを返す。"""
    results = search(query, cookie, limit=10)
    for r in results:
        if exclude_detail_ids and r.detail_id in exclude_detail_ids:
            continue
        if download(r.detail_id, r.file_hash, out_path, cookie, size="l"):
            width = _probe_width(out_path)
            if width is None or width >= min_width:
                return r.detail_id
    return None


def _probe_width(path: str) -> Optional[int]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width", "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True,
        )
        return int(out.stdout.strip())
    except Exception:
        return None
