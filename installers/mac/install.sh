#!/usr/bin/env bash
# auto-telop Mac用インストーラー
#
# 使い方(お客さんのTerminalに1行貼り付けて実行):
#   curl -fsSL https://raw.githubusercontent.com/<org>/<repo>/main/installers/mac/install.sh | bash
#
# 行うこと:
#   1. Homebrewが無ければ導入
#   2. ffmpeg / Python 3.11 を導入(brew)
#   3. 専用の仮想環境(~/.auto_telop/venv)にauto-telop本体を導入
#      (システムのPythonやPATHに依存しないようにするため)
#   4. faster-whisperのmediumモデルを事前ダウンロード(初回実行の待ち時間短縮)
#   5. ~/Desktop/AutoTelop/ フォルダ一式を作成
#   6. ダブルクリックで実行できる AutoTelop.command をローカルで生成し
#      Desktopへ書き出す(ローカル生成のため、curlでダウンロードした場合と違い
#      2回目以降のダブルクリックはmacOSの検疫(Gatekeeper)でブロックされない)
#
# ANTHROPIC_API_KEY は一切設定しない。RTFモードはキー未設定時、
# スタイルをランダムに選ぶ無料フォールバックで動作する。

set -euo pipefail

REPO_URL="https://github.com/t12m07duo2017-byte/auto-telop.git"
INSTALL_DIR="$HOME/.auto_telop"
VENV_DIR="$INSTALL_DIR/venv"
APP_DIR="$HOME/Desktop/AutoTelop"

echo "=========================================="
echo " auto-telop セットアップを開始します"
echo "=========================================="

# --- 1. Homebrew ---------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
    echo "[1/6] Homebrewが見つからないため導入します(パスワードの入力を求められる場合があります)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    if [[ -d /opt/homebrew/bin ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -d /usr/local/bin ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
else
    echo "[1/6] Homebrewは導入済みです"
fi

# --- 2. ffmpeg / python ---------------------------------------------------
echo "[2/6] ffmpeg / Python を確認・導入します..."
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
brew list python@3.11 >/dev/null 2>&1 || brew install python@3.11
PYTHON_BIN="$(brew --prefix python@3.11)/bin/python3.11"

# --- 3. 専用の仮想環境にauto-telopを導入 ----------------------------------
echo "[3/6] auto-telop本体を導入します..."
mkdir -p "$INSTALL_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install --upgrade "git+${REPO_URL}" --quiet
echo "  導入完了: $("$VENV_DIR/bin/auto-telop" --help >/dev/null 2>&1 && echo OK)"

# --- 4. Whisperモデルの事前ダウンロード -------------------------------------
echo "[4/6] 音声認識モデル(medium, 初回のみ・数分かかります)を準備します..."
"$VENV_DIR/bin/python" -c "
from faster_whisper import WhisperModel
print('  ダウンロード中...')
WhisperModel('medium', device='cpu', compute_type='int8')
print('  完了')
"

# --- 5. フォルダ構成の作成 -------------------------------------------------
echo "[5/6] 作業フォルダを作成します: $APP_DIR"
mkdir -p "$APP_DIR/入力" "$APP_DIR/customer_package" "$APP_DIR/出力" "$APP_DIR/logs"

if [[ ! -f "$APP_DIR/customer_package/README.txt" ]]; then
cat > "$APP_DIR/customer_package/README.txt" <<'EOF'
このフォルダには、担当者から届いた3つのファイルを入れてください:
  - template.prproj
  - style_analysis.json
  - style_se_categories.json
  - license.key
これらが揃っていないと実行できません。
EOF
fi

# --- 6. ダブルクリック起動用アイコンの生成 ----------------------------------
echo "[6/6] 実行用アイコンを作成します: $HOME/Desktop/AutoTelop.command"
cat > "$HOME/Desktop/AutoTelop.command" <<LAUNCHER
#!/usr/bin/env bash
# auto-telop 実行用アイコン(このファイルはinstall.shがローカルで生成したものです)
set -uo pipefail

APP_DIR="\$HOME/Desktop/AutoTelop"
VENV_DIR="\$HOME/.auto_telop/venv"
IN_DIR="\$APP_DIR/入力"
OUT_DIR="\$APP_DIR/出力"
CUSTOMER_DIR="\$APP_DIR/customer_package"

echo "=========================================="
echo " auto-telop を実行します"
echo "=========================================="

if [[ ! -x "\$VENV_DIR/bin/auto-telop" ]]; then
    echo "エラー: セットアップが完了していないようです。"
    echo "もう一度、担当者から案内されたインストール手順(1行コマンド)を実行してください。"
    read -p "Enterキーを押すと閉じます..." _
    exit 1
fi

rtf_files=("\$IN_DIR"/*.rtf)
if [[ ! -e "\${rtf_files[0]}" ]]; then
    echo "エラー: 「入力」フォルダに台本ファイル(.rtf)が見つかりません。"
    echo "台本ファイルを 入力/ フォルダに入れてから、もう一度実行してください。"
    read -p "Enterキーを押すと閉じます..." _
    exit 1
fi
if [[ "\${#rtf_files[@]}" -gt 1 ]]; then
    echo "エラー: 「入力」フォルダに台本ファイル(.rtf)が複数あります。1つだけにしてください。"
    read -p "Enterキーを押すと閉じます..." _
    exit 1
fi
script_file="\${rtf_files[0]}"

media_files=()
for ext in mp4 mov m4v wav mp3 m4a aac aif aiff flac; do
    for f in "\$IN_DIR"/*."\$ext"; do
        [[ -e "\$f" ]] && media_files+=("\$f")
    done
done
if [[ "\${#media_files[@]}" -eq 0 ]]; then
    echo "エラー: 「入力」フォルダに音声または動画ファイルが見つかりません。"
    echo "音声/動画ファイルを 入力/ フォルダに入れてから、もう一度実行してください。"
    read -p "Enterキーを押すと閉じます..." _
    exit 1
fi
if [[ "\${#media_files[@]}" -gt 1 ]]; then
    echo "エラー: 「入力」フォルダに音声/動画ファイルが複数あります。1つだけにしてください。"
    read -p "Enterキーを押すと閉じます..." _
    exit 1
fi
media_file="\${media_files[0]}"

timestamp="\$(date +%Y%m%d_%H%M%S)"
output_file="\$OUT_DIR/output_\${timestamp}.prproj"

echo "台本: \$script_file"
echo "音声/動画: \$media_file"
echo "出力先: \$output_file"
echo ""

"\$VENV_DIR/bin/auto-telop" \\
    --customer-dir "\$CUSTOMER_DIR" \\
    -s "\$script_file" \\
    -v "\$media_file" \\
    -o "\$output_file"
status=\$?

echo ""
if [[ \$status -eq 0 ]]; then
    echo "完了しました: \$output_file"
    open "\$OUT_DIR"
else
    echo "処理中にエラーが発生しました(詳細は上のメッセージをご確認ください)。"
fi
read -p "Enterキーを押すと閉じます..." _
LAUNCHER
chmod +x "$HOME/Desktop/AutoTelop.command"

echo ""
echo "=========================================="
echo " セットアップが完了しました"
echo "=========================================="
echo "次にやること:"
echo "  1. 担当者から届いた3つのファイル(template.prproj / style_analysis.json /"
echo "     style_se_categories.json / license.key)を"
echo "     $APP_DIR/customer_package フォルダに入れる"
echo "  2. 台本(.rtf)と音声/動画ファイルを"
echo "     $APP_DIR/入力 フォルダに入れる"
echo "  3. デスクトップの「AutoTelop」アイコンをダブルクリックする"
echo "=========================================="
