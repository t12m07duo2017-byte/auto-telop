#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_template.py
====================

任意のテンプレ.prproj(Essential Graphicsスタイルカタログを含むもの)から、
- スタイル名 -> UID の対応表(StyleProjectItem一覧)
- ビン内の音声ファイル一覧(SE候補)
を抽出し、prproj_generate.py/align.pyが--style-jsonとして読み込める
JSONを生成する(`auto-telop analyze-template`サブコマンドの実体)。

抽出したse_audio_filesはあくまで「ビン内に存在する音声ファイル全部」の
候補一覧であり、どれが実際にSEとして使うべきかの選別・カテゴリ分けは
含まない。カテゴリ分け・固定SEペアリングをしたい場合は、別途
style_config.example.json を参考にstyle_config.jsonを自分で作る。
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from .prproj_common import load_prproj_text
from .prproj_generate import AUDIO_EXTENSIONS


def extract_style_catalog(text: str) -> dict:
    style_name_to_uid = {}
    for m in re.finditer(r'<StyleProjectItem ObjectUID="([0-9a-fA-F-]{36})"[^>]*>.*?</StyleProjectItem>', text, re.S):
        block = m.group(0)
        name_m = re.search(r"<Name>([^<]*)</Name>", block)
        if name_m:
            style_name_to_uid[name_m.group(1)] = m.group(1)

    se_audio_files = []
    seen = set()
    for m in re.finditer(r"<Name>([^<]*)</Name>\s*</ProjectItem>\s*<MasterClip ObjectURef=", text):
        name = m.group(1)
        if name.lower().endswith(AUDIO_EXTENSIONS) and name not in seen:
            seen.add(name)
            se_audio_files.append(name)

    return {
        "note": "analyze_template.pyで自動抽出したスタイル一覧・音声ファイル一覧。"
                "se_audio_filesはビン内の全音声ファイルの候補一覧であり、"
                "SEとして使うべきものだけに絞られてはいない。",
        "style_name_to_uid": style_name_to_uid,
        "se_audio_files": se_audio_files,
    }


def main():
    parser = argparse.ArgumentParser(description="テンプレ.prprojからスタイルカタログJSONを抽出する")
    parser.add_argument("--template", required=True, help="テンプレ.prprojのパス")
    parser.add_argument("-o", "--output", required=True, help="出力JSONパス")
    args = parser.parse_args()

    text = load_prproj_text(args.template)
    result = extract_style_catalog(text)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"スタイル数: {len(result['style_name_to_uid'])}", file=sys.stderr)
    print(f"音声ファイル候補数: {len(result['se_audio_files'])}", file=sys.stderr)
    print(f"出力しました: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
