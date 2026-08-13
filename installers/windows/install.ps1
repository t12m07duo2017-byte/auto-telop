# auto-telop Windows用インストーラー
#
# 使い方(お客さんのPowerShellに1行貼り付けて実行):
#   irm https://raw.githubusercontent.com/<org>/<repo>/main/installers/windows/install.ps1 | iex
#
# 行うこと:
#   1. winget で Python 3.11 / ffmpeg を導入(未導入時のみ)
#   2. 専用の仮想環境(%USERPROFILE%\.auto_telop\venv)にauto-telop本体を導入
#   3. faster-whisperのmediumモデルを事前ダウンロード(初回実行の待ち時間短縮)
#   4. Desktop\AutoTelop\ フォルダ一式を作成
#   5. ダブルクリックで実行できる AutoTelop.bat をローカルで生成しDesktopへ書き出す
#      (ローカル生成のため、ブラウザでダウンロードした場合と違いSmartScreenの
#      「発行元を確認できません」ブロックの対象にならない)
#
# ANTHROPIC_API_KEY は一切設定しない。RTFモードはキー未設定時、
# スタイルをランダムに選ぶ無料フォールバックで動作する。

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/REPLACE_ME_ORG/REPLACE_ME_REPO.git"
$InstallDir = Join-Path $env:USERPROFILE ".auto_telop"
$VenvDir = Join-Path $InstallDir "venv"
$AppDir = Join-Path $env:USERPROFILE "Desktop\AutoTelop"

function Update-PathFromRegistry {
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

Write-Host "=========================================="
Write-Host " auto-telop セットアップを開始します"
Write-Host "=========================================="

# --- 1. Python / ffmpeg ---------------------------------------------------
Write-Host "[1/5] Python / ffmpeg を確認・導入します..."
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "エラー: winget が見つかりません。Windows 10/11 の最新の状態にしてから再度お試しください。"
    exit 1
}

$pythonInstalled = winget list --id Python.Python.3.11 -e 2>$null | Select-String "Python.Python.3.11"
if (-not $pythonInstalled) {
    winget install -e --id Python.Python.3.11 --scope user --accept-source-agreements --accept-package-agreements
}
$ffmpegInstalled = winget list --id Gyan.FFmpeg -e 2>$null | Select-String "Gyan.FFmpeg"
if (-not $ffmpegInstalled) {
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
}
Update-PathFromRegistry

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    # winget install直後はこのプロセスのPATHにまだ反映されないことがあるため、
    # 既定のインストール先を直接探す。
    $candidate = Join-Path $env:LocalAppData "Programs\Python\Python311\python.exe"
    if (Test-Path $candidate) {
        $pythonExe = $candidate
    } else {
        Write-Host "エラー: Pythonのインストール後、実行ファイルが見つかりませんでした。"
        Write-Host "PowerShellを一度閉じて再度開き、このコマンドをもう一度実行してください。"
        exit 1
    }
} else {
    $pythonExe = $pythonCmd.Source
}

# --- 2. 専用の仮想環境にauto-telopを導入 ----------------------------------
Write-Host "[2/5] auto-telop本体を導入します..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
if (-not (Test-Path $VenvDir)) {
    & $pythonExe -m venv $VenvDir
}
$venvPip = Join-Path $VenvDir "Scripts\pip.exe"
$venvPython = Join-Path $VenvDir "Scripts\python.exe"
& $venvPip install --upgrade pip --quiet
& $venvPip install --upgrade "git+$RepoUrl" --quiet

# --- 3. Whisperモデルの事前ダウンロード -------------------------------------
Write-Host "[3/5] 音声認識モデル(medium, 初回のみ・数分かかります)を準備します..."
& $venvPython -c "
from faster_whisper import WhisperModel
print('  ダウンロード中...')
WhisperModel('medium', device='cpu', compute_type='int8')
print('  完了')
"

# --- 4. フォルダ構成の作成 -------------------------------------------------
Write-Host "[4/5] 作業フォルダを作成します: $AppDir"
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "入力") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "customer_package") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "出力") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "logs") | Out-Null

$readmePath = Join-Path $AppDir "customer_package\README.txt"
if (-not (Test-Path $readmePath)) {
    @"
このフォルダには、担当者から届いた4つのファイルを入れてください:
  - template.prproj
  - style_analysis.json
  - style_se_categories.json
  - license.key
これらが揃っていないと実行できません。
"@ | Out-File -FilePath $readmePath -Encoding utf8
}

# --- 5. ダブルクリック起動用アイコンの生成 ----------------------------------
Write-Host "[5/5] 実行用アイコンを作成します..."
$desktop = Join-Path $env:USERPROFILE "Desktop"
$ps1Path = Join-Path $desktop "AutoTelop.ps1"
$batPath = Join-Path $desktop "AutoTelop.bat"

$launcherScript = @'
$ErrorActionPreference = "Stop"
$AppDir = Join-Path $env:USERPROFILE "Desktop\AutoTelop"
$VenvDir = Join-Path $env:USERPROFILE ".auto_telop\venv"
$InDir = Join-Path $AppDir "入力"
$OutDir = Join-Path $AppDir "出力"
$CustomerDir = Join-Path $AppDir "customer_package"
$AutoTelopExe = Join-Path $VenvDir "Scripts\auto-telop.exe"

Write-Host "=========================================="
Write-Host " auto-telop を実行します"
Write-Host "=========================================="

if (-not (Test-Path $AutoTelopExe)) {
    Write-Host "エラー: セットアップが完了していないようです。"
    Write-Host "もう一度、担当者から案内されたインストール手順(1行コマンド)を実行してください。"
    Read-Host "Enterキーを押すと閉じます"
    exit 1
}

$rtfFiles = @(Get-ChildItem -Path $InDir -Filter "*.rtf" -File -ErrorAction SilentlyContinue)
if ($rtfFiles.Count -eq 0) {
    Write-Host "エラー: 「入力」フォルダに台本ファイル(.rtf)が見つかりません。"
    Write-Host "台本ファイルを 入力 フォルダに入れてから、もう一度実行してください。"
    Read-Host "Enterキーを押すと閉じます"
    exit 1
}
if ($rtfFiles.Count -gt 1) {
    Write-Host "エラー: 「入力」フォルダに台本ファイル(.rtf)が複数あります。1つだけにしてください。"
    Read-Host "Enterキーを押すと閉じます"
    exit 1
}
$scriptFile = $rtfFiles[0].FullName

$mediaExts = @("*.mp4", "*.mov", "*.m4v", "*.wav", "*.mp3", "*.m4a", "*.aac", "*.aif", "*.aiff", "*.flac")
$mediaFiles = @($mediaExts | ForEach-Object { Get-ChildItem -Path $InDir -Filter $_ -File -ErrorAction SilentlyContinue })
if ($mediaFiles.Count -eq 0) {
    Write-Host "エラー: 「入力」フォルダに音声または動画ファイルが見つかりません。"
    Write-Host "音声/動画ファイルを 入力 フォルダに入れてから、もう一度実行してください。"
    Read-Host "Enterキーを押すと閉じます"
    exit 1
}
if ($mediaFiles.Count -gt 1) {
    Write-Host "エラー: 「入力」フォルダに音声/動画ファイルが複数あります。1つだけにしてください。"
    Read-Host "Enterキーを押すと閉じます"
    exit 1
}
$mediaFile = $mediaFiles[0].FullName

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$outputFile = Join-Path $OutDir "output_$timestamp.prproj"

Write-Host "台本: $scriptFile"
Write-Host "音声/動画: $mediaFile"
Write-Host "出力先: $outputFile"
Write-Host ""

& $AutoTelopExe --customer-dir $CustomerDir -s $scriptFile -v $mediaFile -o $outputFile
$exitCode = $LASTEXITCODE

Write-Host ""
if ($exitCode -eq 0) {
    Write-Host "完了しました: $outputFile"
    Invoke-Item $OutDir
} else {
    Write-Host "処理中にエラーが発生しました(詳細は上のメッセージをご確認ください)。"
}
Read-Host "Enterキーを押すと閉じます"
'@
Set-Content -Path $ps1Path -Value $launcherScript -Encoding utf8

$batContent = "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"$ps1Path`"`r`n"
Set-Content -Path $batPath -Value $batContent -Encoding ascii

Write-Host ""
Write-Host "=========================================="
Write-Host " セットアップが完了しました"
Write-Host "=========================================="
Write-Host "次にやること:"
Write-Host "  1. 担当者から届いた4つのファイル(template.prproj / style_analysis.json /"
Write-Host "     style_se_categories.json / license.key)を"
Write-Host "     $AppDir\customer_package フォルダに入れる"
Write-Host "  2. 台本(.rtf)と音声/動画ファイルを"
Write-Host "     $AppDir\入力 フォルダに入れる"
Write-Host "  3. デスクトップの「AutoTelop」アイコン(AutoTelop.bat)をダブルクリックする"
Write-Host "=========================================="
