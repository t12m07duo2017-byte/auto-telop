#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_telop.license_check
=========================

顧客ごとの実行許可を、サーバー不要のオフライン公開鍵署名検証で行う。

- 開発者は秘密鍵を1つだけ持つ(このリポジトリには含まれない。
  既定では `~/.auto_telop/signing_key.pem` に保管する)。
- 顧客ごとに、この秘密鍵で署名したトークンを `customers/<顧客名>/license.key`
  として発行する(`auto-telop new-customer` が自動生成する)。
- ツール起動時、公開鍵(下記 `EMBEDDED_PUBLIC_KEY_HEX`。公開鍵なので
  ソースに埋め込んで配布して問題ない)でこのトークンを検証する。
  検証に失敗したら実行を拒否する。

有効期限・利用回数の制限は持たない(単純な署名検証のみ)。
"""
from __future__ import annotations

import base64
import json
import os
from typing import Optional

# このツールの署名用公開鍵(Ed25519, 32byte, hex)。秘密鍵は
# ~/.auto_telop/signing_key.pem に開発者だけが保管しており、リポジトリには含まれない。
EMBEDDED_PUBLIC_KEY_HEX = "e90253954e8d0e444407c9d8c71ebb16b1eb4cf14016b51709229609f2e5fe7c"

DEFAULT_SIGNING_KEY_PATH = os.path.expanduser("~/.auto_telop/signing_key.pem")

LICENSE_FILENAME = "license.key"


class LicenseError(Exception):
    """ライセンスキーが無い/壊れている/署名検証に失敗した場合に送出する。"""


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _canonical_json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")


def sign_license(payload: dict, signing_key_path: str = DEFAULT_SIGNING_KEY_PATH) -> str:
    """開発者用: 秘密鍵でpayload(例: {"customer": "yamaさん", "issued": "2026-08-10"})に
    署名し、license.keyへそのまま書き込めるトークン文字列を返す。
    秘密鍵ファイルが無い場合は分かりやすいエラーで案内する(顧客配布物からは呼ばれない
    経路だが、開発者の手元にも鍵が無いケースに備える)。"""
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError:
        raise LicenseError(
            "cryptography パッケージが見つかりません。 pip install cryptography を実行してください。")

    if not os.path.exists(signing_key_path):
        raise LicenseError(
            f"署名用の秘密鍵が見つかりません: {signing_key_path}\n"
            "  auto-telop gen-signing-key を実行して新規発行してください"
            "(既に鍵をお持ちの場合は --signing-key でパスを指定してください)。")

    with open(signing_key_path, "rb") as f:
        priv = load_pem_private_key(f.read(), password=None)

    payload_bytes = _canonical_json_bytes(payload)
    signature = priv.sign(payload_bytes)
    return f"{_b64u_encode(payload_bytes)}.{_b64u_encode(signature)}"


def verify_license_token(token: str, public_key_hex: str = EMBEDDED_PUBLIC_KEY_HEX) -> dict:
    """トークン文字列を検証し、成功したらpayload(dict)を返す。失敗したらLicenseError。"""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        raise LicenseError(
            "cryptography パッケージが見つかりません。 pip install cryptography を実行してください。")

    token = token.strip()
    parts = token.split(".")
    if len(parts) != 2:
        raise LicenseError("ライセンスキーの形式が不正です(壊れているか、コピーミスの可能性があります)。")
    payload_b64, sig_b64 = parts

    try:
        payload_bytes = _b64u_decode(payload_b64)
        signature = _b64u_decode(sig_b64)
    except Exception:
        raise LicenseError("ライセンスキーの形式が不正です(壊れているか、コピーミスの可能性があります)。")

    pubkey = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
    try:
        pubkey.verify(signature, payload_bytes)
    except InvalidSignature:
        raise LicenseError("ライセンスキーの署名が正しくありません(改ざん、または他のツール/顧客用のキーの可能性があります)。")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        raise LicenseError("ライセンスキーの内容を読み取れませんでした。")
    return payload


def verify_license_file(customer_dir: str) -> dict:
    """customer_dir/license.key を読み込んで検証する。ファイルが無い/検証に失敗した場合は
    LicenseErrorを送出する(呼び出し側で捕捉し、処理を中断すること)。"""
    path = os.path.join(customer_dir, LICENSE_FILENAME)
    if not os.path.exists(path):
        raise LicenseError(
            f"ライセンスキーが見つかりません: {path}\n"
            "  お手数ですが、このツールを配布した担当者にお問い合わせください。")
    with open(path, encoding="utf-8") as f:
        token = f.read()
    return verify_license_token(token)


def generate_signing_key(out_path: str = DEFAULT_SIGNING_KEY_PATH) -> str:
    """開発者用: 新しい署名鍵ペアを生成し、秘密鍵をout_pathへ保存する。
    戻り値: 公開鍵のhex文字列(license_check.pyのEMBEDDED_PUBLIC_KEY_HEXへ
    手動で反映すること)。既にout_pathに鍵が存在する場合は上書きしない。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    if os.path.exists(out_path):
        raise LicenseError(f"既に鍵が存在します(上書きを避けるため中断しました): {out_path}")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(out_path, "wb") as f:
        f.write(pem)
    os.chmod(out_path, 0o600)

    pub = priv.public_key()
    raw_pub = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return raw_pub.hex()
