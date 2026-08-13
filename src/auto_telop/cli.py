#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_telop.cli
==============

`auto-telop` コマンドの本体。台本ファイル(.txt/.rtf)と動画/音声ファイルを
指定するだけで、
  1. 音声認識+フォーストアライメント+しゃべり出しタイミング精密化
  2. (未指定時)Claude APIによる強調語句・スタイル・SE・画像配置の自動判定
  3. (Cookie設定時)photo-ac.comからの画像自動検索・ダウンロード
  4. テンプレ.prprojを複製してのPremiere Pro用.prproj生成
を一括で行う。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

from . import align
from . import analyze_template
from . import classify
from . import license_check
from . import photoac
from . import prproj_generate
from .prproj_common import load_prproj_text

AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".aac", ".aif", ".aiff", ".flac")


def _eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)
    sys.stderr.flush()


def extract_audio(video_path: str, out_dir: str) -> str:
    """動画ファイルから音声を抽出する(既に音声ファイルならそのまま返す)。"""
    ext = os.path.splitext(video_path)[1].lower()
    if ext in AUDIO_EXTENSIONS:
        return video_path
    out_path = os.path.join(out_dir, "extracted_audio.wav")
    _eprint(f"動画から音声を抽出中... ({video_path})")
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
         "-ar", "48000", "-ac", "2", out_path, "-hide_banner", "-loglevel", "error"],
        check=True,
    )
    return out_path


def _safe_filename(text: str, limit: int = 20) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "", text)
    return cleaned[:limit] or "image"


def place_images(image_cues: list, out_dir: str, cookie: str = None) -> str:
    """image_cues([{index,text,(query)}, ...])の各行に対応する画像を
    photo-ac.comから検索・ダウンロードし、prproj_generate.pyが読める
    命名規則('<index>_...')でout_dirへ保存する。戻り値: 画像ディレクトリ
    パス(1件もダウンロードできなければNone)。"""
    if not image_cues:
        return None
    if not cookie:
        _eprint(f"  [警告] 画像配置対象が{len(image_cues)}件ありますが、"
                f"photo-ac.comのCookieが未設定のため画像配置をスキップします。"
                f"(--photoac-cookie-file または環境変数 PHOTOAC_COOKIE_FILE / PHOTOAC_COOKIE で設定できます)")
        return None

    os.makedirs(out_dir, exist_ok=True)
    n_ok = 0
    consecutive_blocked = 0
    aborted = False
    used_detail_ids = set()
    for cue in image_cues:
        if aborted:
            continue
        query = cue.get("query") or cue["text"]
        fname = f"{cue['index']:03d}_{_safe_filename(cue['text'])}.jpg"
        out_path = os.path.join(out_dir, fname)
        _eprint(f"  画像検索中... index={cue['index']} query={query!r}")
        try:
            detail_id = photoac.search_and_download(
                query, out_path, cookie, exclude_detail_ids=used_detail_ids)
            consecutive_blocked = 0
        except photoac.DownloadBlocked as e:
            _eprint(f"  [警告] 画像取得に失敗しました index={cue['index']}: {e}")
            detail_id = None
            consecutive_blocked += 1
            if consecutive_blocked >= 3:
                _eprint("  [警告] photo-acのダウンロードが3件連続でブロックされました。"
                         "ダウンロード上限到達等の可能性が高いため、以降の画像取得を中断します。")
                aborted = True
        except Exception as e:
            _eprint(f"  [警告] 画像取得に失敗しました index={cue['index']}: {e}")
            detail_id = None
            consecutive_blocked = 0
        if detail_id:
            used_detail_ids.add(detail_id)
            n_ok += 1
        else:
            _eprint(f"  [警告] 画像が見つかりませんでした index={cue['index']} query={query!r}")
    _eprint(f"画像を{n_ok}/{len(image_cues)}件配置しました。")
    return out_dir if n_ok > 0 else None


def _resolve_customer_dir(args):
    """--customer-dir が指定されていれば、そのフォルダ内の
    template.prproj / style_analysis.json / style_se_categories.json を
    --template / --style-json / --style-config の未指定時デフォルトとして使う
    (個別指定があればそちらを優先する)。あわせて license.key を検証し、
    欠落・検証失敗なら実行を拒否する(高コストな処理に入る前に行う)。"""
    if not args.customer_dir:
        return
    d = args.customer_dir
    if not os.path.isdir(d):
        sys.exit(f"--customer-dir で指定されたフォルダが見つかりません: {d}")

    try:
        payload = license_check.verify_license_file(d)
    except license_check.LicenseError as e:
        sys.exit(f"ライセンスの確認に失敗したため、実行できません。\n\n{e}")
    _eprint(f"ライセンスを確認しました(顧客: {payload.get('customer', '不明')})")

    defaults = {
        "template": os.path.join(d, "template.prproj"),
        "style_json": os.path.join(d, "style_analysis.json"),
        "style_config": os.path.join(d, "style_se_categories.json"),
    }
    for attr, path in defaults.items():
        if not getattr(args, attr) and os.path.exists(path):
            setattr(args, attr, path)


def cmd_run(args):
    _resolve_customer_dir(args)
    if not args.template:
        sys.exit("--template が指定されていません(--customer-dir 内に template.prproj も"
                  "見つかりませんでした)。")

    tmp_dir = args.work_dir or tempfile.mkdtemp(prefix="auto_telop_")
    os.makedirs(tmp_dir, exist_ok=True)
    _eprint(f"作業ディレクトリ: {tmp_dir}")

    try:
        rtf_mode = args.script.lower().endswith(".rtf")

        audio_path = extract_audio(args.video, tmp_dir)

        template_text = load_prproj_text(args.template)
        sequence_uid = prproj_generate.find_sequence_uid(template_text, args.sequence_name)
        frame_rate = prproj_generate.probe_frame_rate_fps(template_text, sequence_uid)
        _eprint(f"テンプレを検出しました: sequence={sequence_uid} frame_rate={frame_rate:.3f}fps")
        del template_text  # 大きいので不要になったら早めに解放

        style_json_path = args.style_json
        if not style_json_path:
            style_json_path = os.path.join(tmp_dir, "style_analysis.json")
            _eprint("スタイルカタログをテンプレから自動抽出中...")
            text = load_prproj_text(args.template)
            result = analyze_template.extract_style_catalog(text)
            with open(style_json_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            _eprint(f"  スタイル数: {len(result['style_name_to_uid'])} "
                    f"音声ファイル候補数: {len(result['se_audio_files'])}")

        onset_checkpoint = os.path.join(tmp_dir, "onset_checkpoint.json")
        prepared = align.prepare_cues(
            audio_path=audio_path,
            script_path=None if rtf_mode else args.script,
            rtf_path=args.script if rtf_mode else None,
            whisper_model=args.whisper_model, device=args.device, compute_type=args.compute_type,
            silence_gap=args.silence_gap, caption_char_cap=args.caption_char_cap,
            refine_onsets=not args.no_refine_onsets, onset_refine_model=args.onset_refine_model,
            onset_checkpoint=onset_checkpoint, frame_rate=frame_rate,
        )

        emphasis_json_raw = None
        if args.emphasis_json:
            _eprint(f"手動指定の強調判定を使用します: {args.emphasis_json}")
            with open(args.emphasis_json, encoding="utf-8") as f:
                emphasis_json_raw = json.load(f)
        elif rtf_mode:
            if args.style_config:
                with open(args.style_config, encoding="utf-8") as f:
                    cfg = json.load(f)
                categories = cfg.get("categories") or {}
                if categories:
                    full_script = "\n".join(c.text for c in prepared.cues)
                    rtf_cues = [classify.RtfCueLike(index=c.index, text=c.text, category=c.category)
                                for c in prepared.cues]
                    emphasis_json_raw = classify.classify_rtf_styles(
                        rtf_cues, full_script, categories, model=args.claude_model,
                    )
                else:
                    _eprint("  [警告] style_configにcategoriesが無いため、RTFの色付き行のスタイル選定をスキップします。")
            else:
                _eprint("  [警告] --style-config未指定のため、RTFの色付き行のスタイル選定をスキップします"
                        "(色分けされた行は強調テロップとして反映されません)。")
        else:
            with open(style_json_path, encoding="utf-8") as f:
                style_data = json.load(f)
            style_names = list(style_data.get("style_name_to_uid", {}).keys())
            se_files = list(style_data.get("se_audio_files", []))
            categories = None
            if args.style_config and os.path.exists(args.style_config):
                with open(args.style_config, encoding="utf-8") as f:
                    categories = json.load(f).get("categories") or None
            full_script = "\n".join(c.text for c in prepared.cues)
            emphasis_json_raw = classify.classify_cues(
                prepared.cues, full_script, style_names, se_files, categories,
                model=args.claude_model,
            )

        out = align.apply_emphasis(prepared, emphasis_json_raw, style_json_path, args.style_config)

        align_json_path = os.path.join(tmp_dir, "align.json")
        with open(align_json_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        _eprint(f"align.jsonを生成しました: {align_json_path} "
                f"(基本テロップ{len(out['base_captions'])}件 / 画像対象{len(out['image_cues'])}件)")

        image_dir = args.image_dir
        if not image_dir and out["image_cues"]:
            cookie = photoac.load_cookie(args.photoac_cookie_file)
            image_dir = place_images(out["image_cues"], os.path.join(tmp_dir, "images"), cookie)

        prproj_generate.generate_prproj(
            template_path=args.template, audio_path=audio_path, output_path=args.output,
            align_json_path=align_json_path, style_json_path=style_json_path,
            style_se_categories_path=args.style_config, image_dir=image_dir,
            sequence_name=args.sequence_name,
        )
        _eprint(f"\n完了しました: {args.output}")
    finally:
        if not args.keep_temp and not args.work_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        elif args.keep_temp:
            _eprint(f"作業ファイルを保持しました: {tmp_dir}")


def cmd_analyze_template(args):
    text = load_prproj_text(args.template)
    result = analyze_template.extract_style_catalog(text)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    _eprint(f"スタイル数: {len(result['style_name_to_uid'])}")
    _eprint(f"音声ファイル候補数: {len(result['se_audio_files'])}")
    _eprint(f"出力しました: {args.output}")


# 新規顧客フォルダの雛形(_template/style_se_categories.json)は、
# `customers/_template/` 側にも同内容を人間向けに置いているが、pip配布物からでも
# `new-customer` が使えるようパッケージ同梱データ側を正とする。
_STYLE_SE_CATEGORIES_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "style_se_categories_template.json")


def cmd_new_customer(args):
    customer_dir = os.path.join(args.customers_dir, args.name)
    os.makedirs(customer_dir, exist_ok=True)
    _eprint(f"顧客フォルダ: {customer_dir}")

    style_config_path = os.path.join(customer_dir, "style_se_categories.json")
    if os.path.exists(style_config_path) and not args.force:
        _eprint(f"  style_se_categories.json は既に存在するためスキップしました: {style_config_path}")
    else:
        shutil.copyfile(_STYLE_SE_CATEGORIES_TEMPLATE_PATH, style_config_path)
        _eprint(f"  style_se_categories.json の雛形を作成しました(要手入力): {style_config_path}")

    license_path = os.path.join(customer_dir, license_check.LICENSE_FILENAME)
    if os.path.exists(license_path) and not args.force:
        _eprint(f"  license.key は既に存在するためスキップしました(--forceで再発行できます): {license_path}")
    else:
        payload = {"customer": args.name,
                   "issued": datetime.date.today().isoformat()}
        try:
            token = license_check.sign_license(payload, signing_key_path=args.signing_key)
        except license_check.LicenseError as e:
            sys.exit(str(e))
        with open(license_path, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        _eprint(f"  license.key を発行しました: {license_path}")

    _eprint("\n残りの手動作業:")
    _eprint(f"  1. お客さん用に作り込んだテンプレを {customer_dir}/template.prproj として配置")
    _eprint(f"  2. auto-telop analyze-template --template {customer_dir}/template.prproj "
            f"-o {customer_dir}/style_analysis.json")
    _eprint(f"  3. {style_config_path} を手入力で編集(docs/新規顧客追加手順.md 参照)")


def cmd_gen_signing_key(args):
    try:
        pub_hex = license_check.generate_signing_key(out_path=args.out)
    except license_check.LicenseError as e:
        sys.exit(str(e))
    _eprint(f"秘密鍵を生成しました: {args.out}")
    _eprint(f"公開鍵(hex): {pub_hex}")
    _eprint("この公開鍵を src/auto_telop/license_check.py の EMBEDDED_PUBLIC_KEY_HEX に反映し、"
            "コミットしてください(秘密鍵の方は絶対にコミットしないこと)。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-telop",
        description="台本+動画からPremiere Pro用.prprojを自動生成するCLIツール",
    )
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="台本+動画から.prprojを生成する(既定のサブコマンド)")
    run_p.add_argument("-s", "--script", required=True,
                        help="台本ファイル(.txt=プレーンテキスト/Claude API自動判定、"
                             ".rtf=手動色分け済み台本)")
    run_p.add_argument("-v", "--video", required=True, help="動画または音声ファイル")
    run_p.add_argument("--customer-dir", help="顧客フォルダ(customers/<顧客名>/)のパス。"
                                               "指定すると、フォルダ内の template.prproj / "
                                               "style_analysis.json / style_se_categories.json を"
                                               "それぞれ --template / --style-json / --style-config の"
                                               "未指定時デフォルトとして使う(個別指定があれば優先)。"
                                               "同フォルダ内の license.key の検証も行う")
    run_p.add_argument("--template", help="テンプレ.prprojのパス"
                                           "(--customer-dir 未指定時、または個別上書き時は必須)")
    run_p.add_argument("-o", "--output", required=True, help="出力.prprojパス")
    run_p.add_argument("--style-json", help="analyze-templateで生成したスタイルカタログJSON"
                                             "(省略時は毎回テンプレから自動抽出)")
    run_p.add_argument("--style-config", help="style_config.json(カテゴリ/SE対応表/フォント設定。省略可)")
    run_p.add_argument("--sequence-name", help="テンプレ内で対象とするSequence名(省略時は最初のもの)")
    run_p.add_argument("--emphasis-json", help="強調判定を手動指定する場合のJSONパス"
                                                "(指定するとClaude API呼び出しをスキップする)")
    run_p.add_argument("--image-dir", help="画像を手動指定する場合のディレクトリ"
                                            "(指定するとphoto-ac.com自動検索をスキップする)")
    run_p.add_argument("--photoac-cookie-file", help="photo-ac.comのログイン済みCookieファイルのパス")
    run_p.add_argument("--claude-model", default="claude-sonnet-5", help="分類に使うClaude モデル名")
    run_p.add_argument("--whisper-model", default="medium", help="faster-whisperのモデルサイズ")
    run_p.add_argument("--device", default="cpu")
    run_p.add_argument("--compute-type", default="int8")
    run_p.add_argument("--silence-gap", type=float, default=0.3)
    run_p.add_argument("--caption-char-cap", type=int, default=16)
    run_p.add_argument("--no-refine-onsets", action="store_true",
                        help="しゃべり出しタイミングの局所再認識精密化(既定でON、時間がかかる)をスキップする")
    run_p.add_argument("--onset-refine-model", default=None)
    run_p.add_argument("--work-dir", help="中間ファイルの作業ディレクトリ(省略時は一時ディレクトリ、"
                                           "終了時に自動削除)")
    run_p.add_argument("--keep-temp", action="store_true", help="一時作業ファイルを削除せず残す")
    run_p.set_defaults(func=cmd_run)

    at_p = sub.add_parser("analyze-template", help="テンプレ.prprojからスタイルカタログJSONを抽出する")
    at_p.add_argument("--template", required=True)
    at_p.add_argument("-o", "--output", required=True)
    at_p.set_defaults(func=cmd_analyze_template)

    nc_p = sub.add_parser("new-customer", help="開発者用: 顧客フォルダ一式(雛形+ライセンスキー)を作成する")
    nc_p.add_argument("name", help="顧客名(customers/<顧客名>/ が作成される)")
    nc_p.add_argument("--customers-dir", default="customers",
                       help="顧客フォルダの親ディレクトリ(既定: customers)")
    nc_p.add_argument("--signing-key", default=license_check.DEFAULT_SIGNING_KEY_PATH,
                       help="署名用秘密鍵のパス(既定: ~/.auto_telop/signing_key.pem)")
    nc_p.add_argument("--force", action="store_true",
                       help="既存の style_se_categories.json / license.key を上書きする")
    nc_p.set_defaults(func=cmd_new_customer)

    gsk_p = sub.add_parser("gen-signing-key", help="開発者用: 新しい署名鍵ペアを生成する(通常は初回のみ)")
    gsk_p.add_argument("--out", default=license_check.DEFAULT_SIGNING_KEY_PATH,
                        help="秘密鍵の保存先(既定: ~/.auto_telop/signing_key.pem)")
    gsk_p.set_defaults(func=cmd_gen_signing_key)

    return parser


_SUBCOMMANDS = ("run", "analyze-template", "new-customer", "gen-signing-key", "-h", "--help")


def _write_error_log(exc: BaseException) -> str:
    log_dir = os.path.expanduser("~/Desktop/AutoTelop/logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        log_dir = tempfile.gettempdir()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"error_{ts}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"auto-telop エラーログ ({datetime.datetime.now().isoformat()})\n")
        f.write(f"コマンド: {' '.join(sys.argv)}\n\n")
        traceback.print_exc(file=f)
    return log_path


def _friendly_message(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "必要なファイルが見つかりませんでした。台本・音声・テンプレのパス指定をご確認ください。"
    if isinstance(exc, subprocess.CalledProcessError):
        cmd0 = str(exc.cmd[0]) if exc.cmd else ""
        if "ffmpeg" in cmd0 or "ffprobe" in cmd0:
            return "ffmpegの実行に失敗しました。ffmpegが正しくインストールされているかご確認ください。"
        return f"外部コマンド({cmd0 or '不明'})の実行に失敗しました。"
    if isinstance(exc, MemoryError):
        return "メモリが不足しています。他のアプリを終了してから再度お試しください。"
    return "処理中に予期しないエラーが発生しました。"


def main():
    sys.stderr.reconfigure(line_buffering=True)
    parser = build_parser()

    argv = sys.argv[1:]
    # サブコマンド省略時は"run"を既定にする(auto-telop -s ... -v ... の
    # ような直感的な呼び出しを許すため)。
    if argv and argv[0] not in _SUBCOMMANDS:
        argv = ["run"] + argv

    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    try:
        args.func(args)
    except KeyboardInterrupt:
        _eprint("\n中断しました。")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        log_path = _write_error_log(e)
        _eprint("\n" + "=" * 60)
        _eprint(f"エラーが発生し、処理を中断しました。\n{_friendly_message(e)}")
        _eprint(f"\n詳しいログを保存しました。サポートに問い合わせる際はこのファイルを"
                f"お送りください:\n  {log_path}")
        _eprint("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
