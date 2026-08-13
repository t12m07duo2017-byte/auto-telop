#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新パーサー(auto_telop.align.parse_rtf, 純Python)と旧パーサー
(tests.legacy_textutil_rtf.parse_rtf_textutil, textutil依存)の出力を、
実在するRTFサンプル全件で比較する。macOS上でのみ実行可能(textutilが必要)。

使い方:
    PYTHONPATH=src python3 tests/compare_rtf_parsers.py [追加のRTFパス...]

比較対象のサンプルRTFパスは、実在の顧客台本(業務データ)を含むため本リポジトリには
含めない。`tests/rtf_samples.local.txt`(1行1パス、gitignore対象)を用意するか、
コマンドライン引数でパスを渡して実行すること。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from auto_telop.align import parse_rtf  # noqa: E402
from legacy_textutil_rtf import parse_rtf_textutil  # noqa: E402

_LOCAL_SAMPLES_FILE = Path(__file__).resolve().parent / "rtf_samples.local.txt"


def _load_default_samples() -> list[str]:
    if not _LOCAL_SAMPLES_FILE.exists():
        return []
    lines = _LOCAL_SAMPLES_FILE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def compare_one(path: str) -> bool:
    try:
        expected = parse_rtf_textutil(path)
    except Exception as e:
        print(f"[SKIP] {path}: textutil側でエラー ({e})")
        return True
    try:
        actual = parse_rtf(path)
    except Exception as e:
        print(f"[FAIL] {path}: 新パーサーで例外発生: {e!r}")
        return False

    if expected == actual:
        print(f"[OK]   {path}  ({len(expected)}行)")
        return True

    print(f"[FAIL] {path}")
    print(f"       旧: {len(expected)}行 / 新: {len(actual)}行")
    n = max(len(expected), len(actual))
    shown = 0
    for i in range(n):
        e = expected[i] if i < len(expected) else None
        a = actual[i] if i < len(actual) else None
        if e != a:
            print(f"       差分[{i}]:")
            print(f"         旧: {e}")
            print(f"         新: {a}")
            shown += 1
            if shown >= 8:
                print("       (以下省略)")
                break
    return False


def main():
    paths = sys.argv[1:] or _load_default_samples()
    if not paths:
        print(f"比較対象のRTFパスがありません。{_LOCAL_SAMPLES_FILE} を用意するか、"
              "コマンドライン引数でパスを渡してください。")
        sys.exit(1)
    ok_count = 0
    fail_count = 0
    for path in paths:
        if not Path(path).exists():
            print(f"[SKIP] ファイルが見つかりません: {path}")
            continue
        if compare_one(path):
            ok_count += 1
        else:
            fail_count += 1
    print(f"\n合計: OK={ok_count} FAIL={fail_count}")
    sys.exit(1 if fail_count else 0)


if __name__ == "__main__":
    main()
