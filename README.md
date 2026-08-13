# auto-telop

台本ファイル(プレーンテキスト or 手動色分け済みRTF)と動画/音声ファイルを渡すだけで、
Adobe Premiere Pro 用の `.prproj` ファイル(ナレーション・逐語テロップ・強調テロップ・
効果音(SE)・画像入りタイムライン)を自動生成するCLIツールです。

```
auto-telop -s script.txt -v video.mp4 --template template.prproj -o output.prproj
```

## できること

- 音声認識(faster-whisper)+フォーストアライメントで、台本の各行が実際に
  いつ話されているかをフレーム単位で検出
- (プレーンテキスト台本の場合)Claude APIが台本全体を読み、強調すべき語句・
  スタイル・効果音・画像を配置すべき行を自動判定
- (RTF台本の場合)あらかじめ手動で色分けした行(黒=基本テロップ、
  色付き=ポジティブ/ネガティブ/強調テロップ、背景緑=画像配置)をそのまま使用
  (スタイル名の最終選定のみClaude APIが担当)
- 指定したテンプレ `.prproj`(Adobe Premiere Proの Essential Graphics スタイル
  カタログを含むプロジェクト)を複製し、ナレーション音声・基本テロップ・強調
  テロップ・SE・画像を新規トラックへ配置した `.prproj` を書き出す
- (Cookie設定時)画像配置が必要な行について、photo-ac.com(写真AC)から
  自動で画像を検索・ダウンロードして配置

生成された `.prproj` はAdobe Premiere Proでそのまま開けます(内部的には
gzip圧縮されたXMLで、Premiere自身の保存形式と同じです)。

## 必要なもの

- Python 3.9以上
- [ffmpeg](https://ffmpeg.org/)(音声抽出・音声解析に使用。`brew install ffmpeg` 等)
- macOS / Windows(RTFモードは純Python実装のためOS非依存。`tests/compare_rtf_parsers.py`
  でmacOS標準の`textutil`との出力一致を検証済み)
- Adobe Premiere Proの Essential Graphics スタイルカタログを含むテンプレ
  `.prproj`(お手持ちのプロジェクトから用意してください。テンプレそのものは
  このツールには同梱していません)
- (任意)Anthropic APIキー(`ANTHROPIC_API_KEY`)。プレーンテキストモードの自動判定で
  使用します。RTFモードはキー未設定時、コスト無料のランダムスタイル選定に自動で
  フォールバックするため、**顧客配布物には含める必要がありません**

お客さん向けの配布は `installers/mac/install.sh` / `installers/windows/install.ps1`
(1行インストーラー)を使います。開発者向けのセットアップ手順は以下のとおりです。

## インストール(開発者向け)

```bash
git clone <このリポジトリ>
cd auto_telop_pkg
pip install -e .
```

`auto-telop` コマンドがインストールされます(`pip install -e .` でインストールした
スクリプトのパスが `$PATH` に無い場合、pipが表示するWARNINGに従ってPATHへ追加
してください。例: `export PATH="$HOME/Library/Python/3.9/bin:$PATH"`)。

プレーンテキストモードの自動判定を使う場合のみ、追加で設定してください:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

## 顧客フォルダ方式(推奨)

複数の顧客を同一コードで扱えるよう、顧客ごとに1フォルダへまとめる規約を使う。

```
customers/<顧客名>/
  template.prproj            # そのお客さん専用のPremiereテンプレ
  style_analysis.json        # analyze-templateの出力
  style_se_categories.json   # カテゴリ→スタイル→SEの対応表(手入力)
  license.key                # new-customerが自動生成する実行許可キー
```

```bash
auto-telop --customer-dir customers/<顧客名> \
  -s script.rtf -v video.mp4 -o output.prproj
```

新規顧客の追加手順は `docs/新規顧客追加手順.md` を参照(`--template`/`--style-json`/
`--style-config` を個別指定する下記の方法もそのまま使えるが、`--customer-dir` の方が
お客さんごとの差し替えが1箇所で済む)。

## クイックスタート(個別ファイル指定)

### 1. テンプレのスタイルカタログを抽出する(初回のみ)

お手持ちのテンプレ `.prproj`(Essential Graphicsのスタイルが登録済みの
プロジェクト)から、スタイル名の一覧を抽出します。

```bash
auto-telop analyze-template --template "テンプレ.prproj" -o style_analysis.json
```

### 2a. プレーンテキスト台本から生成する(全自動)

```bash
auto-telop -s script.txt -v video.mp4 \
  --template "テンプレ.prproj" \
  --style-json style_analysis.json \
  -o output.prproj
```

`script.txt` は普通のテキストファイルで構いません。台本全体を句読点/改行で
自動的にキューへ分割し、無音区間検出で基本テロップ(逐語キャプション)の
区切りを決め、Claude APIが強調語句・スタイル・SE・画像配置行を判定します。

### 2b. 手動色分け済みRTF台本から生成する

macOSの「テキストエディット」等で台本を作成し、行ごとに文字色・背景色で
意味付けしてください:

| 色 | 意味 |
|---|---|
| 黒(通常色) | 基本テロップ(逐語キャプション) |
| 青(文字色) | ネガティブな強調テロップ |
| 赤(文字色) | ポジティブな強調テロップ |
| オレンジ(文字色) | 強調テロップ |
| 緑(背景ハイライト) | この行に画像を1枚配置する(3行連続でも1行=1画像) |

```bash
auto-telop -s script.rtf -v video.mp4 \
  --template "テンプレ.prproj" \
  --style-json style_analysis.json \
  --style-config style_config.json \
  -o output.prproj
```

`--style-config` はカテゴリごとのスタイル一覧・固定SEペアリング・スケール
自動調整用フォント設定をまとめた設定ファイルです。`data/style_config.example.json`
を参考に、お使いのテンプレのスタイル名に合わせて作成してください(省略も
可能ですが、その場合ポジティブ/ネガティブ/強調テロップの自動スタイル選定・
SE固定ペアリング・セーフマージンに収まるスケール自動調整は行われません)。

### 3. (任意)画像自動配置を有効にする

RTFの緑ハイライト行、またはプレーンテキストモードでClaude APIが画像配置
すべきと判定した行について、photo-ac.com(写真AC)から自動で画像を検索・
ダウンロードします。ブラウザの開発者ツールでログイン済みセッションの
`Cookie:` ヘッダーの値をコピーし、ファイルに保存してください。

```bash
auto-telop -s script.txt -v video.mp4 \
  --template "テンプレ.prproj" --style-json style_analysis.json \
  --photoac-cookie-file photoac_cookie.txt \
  -o output.prproj
```

Cookie未設定の場合、画像配置は自動でスキップされます(警告が出るだけで
処理は継続します)。

## 主なオプション

```
auto-telop run -s SCRIPT -v VIDEO --template TEMPLATE -o OUTPUT [options]
```

| オプション | 説明 |
|---|---|
| `-s`, `--script` | 台本ファイル。`.rtf` ならRTFモード、それ以外はプレーンテキストモード |
| `-v`, `--video` | 動画または音声ファイル(動画の場合は自動で音声抽出) |
| `--customer-dir` | 顧客フォルダのパス。中の`template.prproj`/`style_analysis.json`/`style_se_categories.json`を各オプションの未指定時デフォルトにし、`license.key`を検証する |
| `--template` | テンプレ `.prproj` のパス(`--customer-dir`未指定時は必須) |
| `-o`, `--output` | 出力 `.prproj` パス(必須) |
| `--style-json` | `analyze-template` で生成したスタイルカタログJSON(省略時は毎回自動抽出) |
| `--style-config` | カテゴリ/SE対応表/フォント設定(`data/style_config.example.json` 参照、省略可) |
| `--photoac-cookie-file` | photo-ac.comのログイン済みCookieファイル(省略時は画像配置スキップ) |
| `--emphasis-json` | 強調判定を手動で用意したJSONで上書きする(指定するとClaude API呼び出しをスキップ) |
| `--image-dir` | 画像を手動で用意したディレクトリから読み込む(`<index>_....jpg` 命名) |
| `--claude-model` | 分類に使うClaudeモデル名(既定: `claude-sonnet-5`) |
| `--whisper-model` | faster-whisperのモデルサイズ(既定: `medium`) |
| `--no-refine-onsets` | しゃべり出しタイミングの局所再認識精密化(既定でON、処理時間がかかる)をスキップ |
| `--sequence-name` | テンプレ内に複数Sequenceがある場合に対象を名前で指定 |
| `--work-dir` / `--keep-temp` | 中間ファイル(音声認識結果・align.json等)を残したい場合に指定 |

## 処理の流れ(内部)

1. `--video` が動画ファイルの場合、ffmpegで音声を抽出
2. faster-whisperで単語レベルタイムスタンプ付き音声認識
3. 台本テキストとのフォーストアライメント(`difflib` による文字単位マッチング)
4. (既定でON)各行の「しゃべり出し」位置だけを切り出して局所的に再認識し、
   タイムスタンプをテンプレのフレームレートに合わせて精密化
5. 基本テロップ同士がフレーム単位で隙間なく繋がるよう、各クリップの終了時刻を
   次のクリップの開始時刻まで延長
6. (`--emphasis-json` 未指定時)Claude APIで強調語句・スタイル・SE・画像配置を判定
7. (画像配置行があり、Cookie設定時)photo-ac.comから画像を検索・ダウンロード
8. テンプレ `.prproj` を複製し、空いている音声/映像トラックを自動検出して
   ナレーション・基本テロップ(V8相当)・強調テロップ(V9相当)・画像(V7相当)・
   SEを配置

テンプレのSequence・空きトラック・フレームレート・音声インポート用の構造
ドナークリップは、いずれも実行のたびにテンプレファイルから自動検出します
(特定のテンプレファイルにハードコードされたIDには依存しません)。

## 既知の制限・注意点

- タイムスタンプはASR(音声認識)+補間による推定値です。局所再認識による
  精密化でかなり正確になりますが、100%の保証はありません。特に重要な
  シーンは生成後にPremiere上で確認してください。
- ポジティブ/ネガティブスタイルのスケール自動調整(セーフマージンに収まる
  よう文字幅を実測して調整する機能)は、`style_config.json` の `font_paths`
  で該当スタイルのフォントファイルの実パスを指定した場合のみ有効です。
  見つからない場合は自動でスキップされ、テンプレ本来のスケール値のまま
  配置されます(実行時に警告が表示されます)。
- photo-ac.comの画像自動ダウンロードは、ご自身のログインセッション(Cookie)
  を使って行われます。利用は photo-ac.com の利用規約の範囲内で行ってください。
- 効果音(SE)ファイルは、テンプレのプロジェクトパネル内に実在する音声
  ファイルのみ使用できます。

## サブコマンド一覧

```
auto-telop run ...              # 台本+動画から.prprojを生成する(既定)
auto-telop analyze-template ... # テンプレ.prprojからスタイルカタログJSONを抽出する
auto-telop new-customer <名前>  # 顧客フォルダの雛形+ライセンスキーを生成する(開発者用)
auto-telop gen-signing-key      # ライセンス署名用の鍵ペアを新規生成する(通常は初回のみ)
```

`auto-telop -s ... -v ...` のように `run` を省略しても動作します。

## ライセンスキーによる実行制御

コードは公開リポジトリで配布しますが、実行そのものは顧客ごとのライセンスキー
(`license.key`)で制御します。サーバー不要のオフライン公開鍵署名検証方式
(Ed25519)です。`--customer-dir`指定時、対象フォルダの`license.key`を検証し、
欠落・改ざん・不正なキーの場合は実行を拒否します。有効期限・利用回数の制限は
ありません。詳細は `src/auto_telop/license_check.py` および
`docs/新規顧客追加手順.md` を参照してください。

## 顧客向けドキュメント

- `docs/新規顧客追加手順.md` — 開発者向け。新規顧客追加のたびに行う作業
- `docs/お客様向け手順書.md` — 技術用語なしの、お客さん向け最短手順
- `docs/トラブルシューティング.md` — Gatekeeper/SmartScreen回避、エラー対処など
- `installers/mac/install.sh` / `installers/windows/install.ps1` — 1行インストーラー
