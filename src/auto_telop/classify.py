#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
classify.py
===========

プレーンテキスト台本モード(auto-telop -s script.txt)向けの、Claude API
による自動判定。台本全文を文脈として渡し、キューごとに
  - 強調すべき語句(0〜2個、原文の厳密な部分文字列)と、それぞれに
    適用するスタイル名(・SE)
  - 画像を配置すべきキューかどうか、そうなら検索キーワード
を一括で判定し、align.py --emphasis-json がそのまま読み込める形式の
JSONを出力する。

RTFモード(手動色分け)ではこのモジュールは使わない
(emphasis_telop.py の元々のtool-useパターンを踏襲・拡張したもの)。
"""
from __future__ import annotations

import json
import os
import random
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class CueLike:
    index: int
    text: str


SYSTEM_PROMPT_TEMPLATE = """あなたは動画のテロップ制作者です。台本全体を読み、各キュー(字幕1個分)について次の3つを判定してください。

1. 強調語句(0〜2個)
   - 視聴者の注意を引くために強調表示すべき語句。多くの行では0個(強調なし)が適切です。
   - 「でもですね」「それではですね」のようなフィラー・つなぎだけの短い行には強調をつけない。
   - 強調語句は必ずキューのテキストに完全一致する連続した部分文字列にすること(言い換え・要約・表記ゆれは禁止)。
   - 句読点や改行をまたがず、1〜10文字程度の短いフレーズにする。
   - キーメッセージ、対比構造、数字、結論、感情の起伏が大きい部分を優先的に強調する。

2. 強調語句ごとのスタイル
{style_instructions}

3. 画像配置
   - そのキューの内容を視覚的に補強する画像(写真)を配置すべきかどうか。
   - 全キューのうち、明確に画像で伝わりやすい具体的な状況・情景を描写している行にのみ画像を割り当てる
     (抽象的な感情の吐露や短いつなぎの相槌には割り当てない)。
   - 画像を配置する場合は、ストックフォト検索に使える日本語の検索キーワード(1〜4語程度、簡潔に)を
     image_queryに入れる。不要なら省略する。

迷ったら「なし」を選んでください。テロップ・画像は入れすぎると逆効果になります。
"""

STYLE_INSTRUCTIONS_WITH_CATEGORIES = """   - 強調したい理由に応じて、以下のカテゴリから最も合うスタイルを1つ選んでください。
{category_block}
   - SEはこちらで指定したスタイルに応じて自動的に決まるため、se_fileは省略してよい。"""

STYLE_INSTRUCTIONS_FREEFORM = """   - 以下のスタイル一覧から最も合うものを1つ選んでください: {style_list}
   - 対応するSE(効果音)を鳴らしたい場合は、以下のSE一覧から1つ選んでse_fileに入れてください
     (不要ならse_fileは省略): {se_list}"""


CLASSIFY_TOOL = {
    "name": "classify_cues",
    "description": "各キューについて、強調語句・スタイル・SE・画像配置の要否を判定する",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "キュー番号"},
                        "emphasis": {
                            "type": "array",
                            "description": "強調する語句のリスト。無ければ空配列。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "phrase": {"type": "string", "description": "原文の厳密な部分文字列"},
                                    "style_name": {"type": "string"},
                                    "se_file": {"type": "string", "description": "任意。指定しない場合は省略可"},
                                },
                                "required": ["phrase", "style_name"],
                            },
                        },
                        "image_query": {
                            "type": "string",
                            "description": "画像を配置すべき場合の検索キーワード。不要なら省略。",
                        },
                    },
                    "required": ["index", "emphasis"],
                },
            }
        },
        "required": ["results"],
    },
}


def _build_style_instructions(style_names: List[str], se_files: List[str],
                               categories: Optional[dict]) -> str:
    if categories:
        lines = []
        for cat_name, cat in categories.items():
            note = cat.get("note", "")
            names = ", ".join(cat.get("styles", {}).keys())
            lines.append(f"     * {cat_name}({note}): {names}")
        return STYLE_INSTRUCTIONS_WITH_CATEGORIES.format(category_block="\n".join(lines))
    return STYLE_INSTRUCTIONS_FREEFORM.format(
        style_list=", ".join(style_names), se_list=", ".join(se_files),
    )


def _cue_text(cue: CueLike) -> str:
    return cue.text


def call_claude_for_batch(client, model: str, full_script: str, batch: List[CueLike],
                           system_prompt: str) -> dict:
    cue_block = "\n".join(f"[{c.index}] {_cue_text(c)}" for c in batch)
    user_msg = (
        f"### 台本全文(文脈把握用)\n{full_script}\n\n"
        f"### 判定対象のキュー\n{cue_block}\n\n"
        "上記の判定対象キューそれぞれについて、classify_cuesツールで結果を返してください。"
        "判定対象キュー以外のindexは含めないでください。"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=system_prompt,
        tools=[CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_cues"},
        messages=[{"role": "user", "content": user_msg}],
    )
    results: Dict[int, list] = {}
    image_queries: Dict[int, str] = {}
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "classify_cues":
            for item in block.input.get("results", []):
                idx = item.get("index")
                if idx is None:
                    continue
                emph = item.get("emphasis", []) or []
                results[idx] = [e for e in emph if e.get("phrase") and e.get("style_name")]
                query = item.get("image_query")
                if query:
                    image_queries[idx] = query
    return {"results": results, "image_queries": image_queries}


def classify_cues(cues: List[CueLike], full_script: str, style_names: List[str],
                   se_files: List[str], categories: Optional[dict],
                   model: str = "claude-sonnet-5", batch_size: int = 20) -> dict:
    """cues(index/textを持つオブジェクトのリスト)をClaude APIで一括判定し、
    align.py --emphasis-json にそのまま渡せる辞書を返す。"""
    try:
        import anthropic
    except ImportError:
        sys.exit(
            "anthropic パッケージが見つかりません。\n"
            "  pip install anthropic\n"
            "を実行してから再度お試しください。"
        )
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit(
            "環境変数 ANTHROPIC_API_KEY が設定されていません。\n"
            "  export ANTHROPIC_API_KEY=\"sk-ant-...\"\n"
            "を実行してから再度お試しください。"
        )
    client = anthropic.Anthropic(api_key=api_key)

    style_instructions = _build_style_instructions(style_names, se_files, categories)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(style_instructions=style_instructions)

    all_results: Dict[int, list] = {}
    all_image_queries: Dict[int, str] = {}
    for i in range(0, len(cues), batch_size):
        batch = cues[i:i + batch_size]
        print(f"  Claude APIで判定中... キュー {batch[0].index}-{batch[-1].index} "
              f"({i + len(batch)}/{len(cues)})", file=sys.stderr)
        batch_result = call_claude_for_batch(client, model, full_script, batch, system_prompt)
        all_results.update(batch_result["results"])
        all_image_queries.update(batch_result["image_queries"])

    return {
        "results": {str(k): v for k, v in all_results.items()},
        "image_queries": {str(k): v for k, v in all_image_queries.items()},
    }


def classify_from_cues_json(cues_json_path: str, style_json_path: str,
                             style_config_path: Optional[str], output_path: str,
                             model: str = "claude-sonnet-5", batch_size: int = 20) -> None:
    with open(cues_json_path, encoding="utf-8") as f:
        cues_data = json.load(f)
    cues = [CueLike(index=c["index"], text=c["text"]) for c in cues_data["cues"]]
    full_script = "\n".join(c.text for c in cues)

    with open(style_json_path, encoding="utf-8") as f:
        style_data = json.load(f)
    style_names = list(style_data.get("style_name_to_uid", {}).keys())
    se_files = list(style_data.get("se_audio_files", []))

    categories = None
    if style_config_path and os.path.exists(style_config_path):
        with open(style_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        categories = cfg.get("categories") or None

    out = classify_cues(cues, full_script, style_names, se_files, categories,
                         model=model, batch_size=batch_size)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"出力しました: {output_path}", file=sys.stderr)


RTF_STYLE_TOOL = {
    "name": "pick_styles",
    "description": "カテゴリが既に決まっている各キューについて、そのカテゴリ内から最も合うスタイルを1つ選ぶ",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "style_name": {"type": "string"},
                    },
                    "required": ["index", "style_name"],
                },
            }
        },
        "required": ["results"],
    },
}

RTF_STYLE_SYSTEM_PROMPT = """あなたは動画のテロップ制作者です。台本全体を読み、各キューについて、
そのキューの色分け(意味合い)に応じて指定済みの「カテゴリ」の中から、内容に最も合うスタイル名を
1つ選んでください。

カテゴリごとのスタイル一覧:
{category_block}

各キューのニュアンス(煽り・喜び・不穏・皮肉っぽさ等)に最も合うものを選ぶこと。
判断に迷う場合は、そのカテゴリの最初のスタイルを選んでよい。"""


@dataclass
class RtfCueLike:
    index: int
    text: str
    category: str


# このカテゴリはランダム選定時、直前に選ばれたスタイルと同じものが
# 連続しないよう優先的に避ける(該当カテゴリ内の使用履歴を見る)。
_NO_CONSECUTIVE_REPEAT_CATEGORIES = {"ポジティブ", "ネガティブ"}


def _random_rtf_styles(cues: List[RtfCueLike], categories: dict) -> dict:
    """ANTHROPIC_API_KEY未設定時のフォールバック: AI判定を行わず、
    各キューのカテゴリ内からスタイルをランダムに1つ選ぶ(コスト削減用)。
    ポジティブ/ネガティブは、そのカテゴリ内の直前の選定と同じスタイルが
    連続しないよう、選択肢が2つ以上あれば直前と異なるものを優先する。"""
    results = {}
    last_style_by_category: Dict[str, str] = {}
    for c in cues:
        if not c.category:
            continue
        cat = categories.get(c.category) or {}
        style_names = list(cat.get("styles", {}).keys())
        if not style_names:
            continue
        if c.category in _NO_CONSECUTIVE_REPEAT_CATEGORIES and len(style_names) > 1:
            last = last_style_by_category.get(c.category)
            candidates = [s for s in style_names if s != last] or style_names
        else:
            candidates = style_names
        chosen = random.choice(candidates)
        last_style_by_category[c.category] = chosen
        results[str(c.index)] = [{"phrase": c.text, "style_name": chosen}]
    return {"results": results}


def classify_rtf_styles(cues: List[RtfCueLike], full_script: str, categories: dict,
                         model: str = "claude-sonnet-5", batch_size: int = 30) -> dict:
    """RTFモード用: 各キューは既にcategoryが確定している(色分け由来)ため、
    そのカテゴリ内でのスタイル選択だけをClaude APIに判定させる。
    戻り値は align.py --emphasis-json にそのまま渡せる形式
    ({"results": {index: [{"phrase": cue.text, "style_name": ...}]}})。
    カテゴリがNone(=基本テロップ)のキューは結果に含めない。
    ANTHROPIC_API_KEYが未設定の場合は、コスト削減のためAI判定を行わず、
    カテゴリ内からランダムに1つ選ぶ。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ANTHROPIC_API_KEY未設定のため、スタイルをランダムに選定します...", file=sys.stderr)
        return _random_rtf_styles(cues, categories)

    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic パッケージが見つかりません。 pip install anthropic を実行してください。")
    client = anthropic.Anthropic(api_key=api_key)

    category_block = "\n".join(
        f"  * {cat_name}({cat.get('note', '')}): {', '.join(cat.get('styles', {}).keys())}"
        for cat_name, cat in categories.items()
    )
    system_prompt = RTF_STYLE_SYSTEM_PROMPT.format(category_block=category_block)

    targets = [c for c in cues if c.category]
    all_style: Dict[int, str] = {}
    for i in range(0, len(targets), batch_size):
        batch = targets[i:i + batch_size]
        print(f"  Claude APIでスタイル選定中... キュー {batch[0].index}-{batch[-1].index} "
              f"({i + len(batch)}/{len(targets)})", file=sys.stderr)
        cue_block = "\n".join(f"[{c.index}] ({c.category}) {c.text}" for c in batch)
        user_msg = (
            f"### 台本全文(文脈把握用)\n{full_script}\n\n"
            f"### 判定対象のキュー(カテゴリ指定済み)\n{cue_block}\n\n"
            "pick_stylesツールで結果を返してください。判定対象キュー以外のindexは含めないでください。"
        )
        resp = client.messages.create(
            model=model, max_tokens=2000, system=system_prompt,
            tools=[RTF_STYLE_TOOL], tool_choice={"type": "tool", "name": "pick_styles"},
            messages=[{"role": "user", "content": user_msg}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "pick_styles":
                for item in block.input.get("results", []):
                    idx = item.get("index")
                    style_name = item.get("style_name")
                    if idx is not None and style_name:
                        all_style[idx] = style_name

    results = {}
    for c in targets:
        style_name = all_style.get(c.index)
        if style_name:
            results[str(c.index)] = [{"phrase": c.text, "style_name": style_name}]
    return {"results": results}
