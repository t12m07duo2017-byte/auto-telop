#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gemini_image.py
================

Gemini API(Nano Banana系列, 既定モデル gemini-3.1-flash-image)の
"/v1beta/interactions" エンドポイントでテキストプロンプトから画像を1枚
生成する(photo-ac.com検索・ダウンロードに代わる画像調達手段)。

photo-ac.comの自動ダウンロードは、実際のダウンロードボタンが
/ajax/public/chk_premium_downloads_recaptcha への事前検証(ボット対策)を
要求するようになっており、スクリプトからの自動化には対応できないため
廃止した(photoac.pyはそのまま残しているが呼び出さない)。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
from typing import Optional

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
MODEL = "gemini-3.1-flash-image"


class GenerationBlocked(Exception):
    """レート制限・クォータ超過等でAPIが画像を返さなかった場合に送出する。"""


def load_api_key(key_file: Optional[str] = None) -> Optional[str]:
    """APIキーを取得する。優先順位:
    1. 引数key_file(ファイルパス)
    2. 環境変数 GEMINI_API_KEY_FILE(ファイルパス)
    3. 環境変数 GEMINI_API_KEY(キー文字列そのもの)
    どれも無ければNone。"""
    path = key_file or os.environ.get("GEMINI_API_KEY_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    inline = os.environ.get("GEMINI_API_KEY")
    if inline:
        return inline.strip()
    return None


def generate_image(prompt: str, out_path: str, api_key: str,
                    aspect_ratio: str = "16:9", image_size: str = "2K") -> bool:
    """promptから画像を1枚生成しout_pathへ保存する。成功したらTrue、
    画像が得られなければFalse。レート制限/クォータ超過と判断できる
    エラーはGenerationBlockedを送出する(呼び出し側はこれを見て
    以降の生成を打ち切るか判断する)。"""
    body = json.dumps({
        "model": MODEL,
        "input": prompt,
        "response_format": {
            "type": "image",
            "mime_type": "image/jpeg",
            "aspect_ratio": aspect_ratio,
            "image_size": image_size,
        },
    })
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", API_URL,
         "-H", f"x-goog-api-key: {api_key}",
         "-H", "Content-Type: application/json",
         "-d", body,
         "-w", "\n%{http_code}"],
        capture_output=True, check=True,
    )
    raw = result.stdout.decode("utf-8", errors="replace")
    body_text, _, http_code = raw.rpartition("\n")
    http_code = http_code.strip()
    try:
        resp = json.loads(body_text)
    except json.JSONDecodeError:
        return False

    if http_code != "200":
        err = resp.get("error", {}) if isinstance(resp, dict) else {}
        status = str(err.get("status", ""))
        message = str(err.get("message", ""))
        if http_code == "429" or status == "RESOURCE_EXHAUSTED":
            raise GenerationBlocked(
                f"レート制限/クォータ超過の可能性: http_code={http_code} status={status!r} message={message!r}"
            )
        return False

    image_b64 = None
    for step in resp.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for block in step.get("content", []):
            if block.get("type") == "image" and block.get("data"):
                image_b64 = block["data"]
    if not image_b64:
        return False

    with open(out_path, "wb") as f:
        f.write(base64.b64decode(image_b64))
    return True
