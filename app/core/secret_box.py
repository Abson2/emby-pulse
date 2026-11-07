# -*- coding: utf-8 -*-
"""透明字段加解密：DAO 边界对敏感字段加解密，上层无感。

使用已在 pyproject 声明的 pycryptodome（AES-GCM）；密钥存 data/.secret_key，需随 data 一并备份。
历史明文以 enc:v1: 前缀区分，读取时自动兼容（零迁移）。

设计约束（与本项目「绝不崩」原则一致）：
- 加密：空值 / 已是密文（带前缀）直接返回；否则 AES-GCM 加密并加前缀。
- 解密：空值 / 非密文直接返回；密文校验失败降级返回原文，避免读路径崩溃。
- 仅在 user_bot_dao（Emby 口令）/ plugin_dao（插件配置，含第三方凭证）两处边界调用。
"""
import os
import base64

from Crypto.Cipher import AES

_PREFIX = "enc:v1:"
_KEY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", ".secret_key",
)


def _key() -> bytes:
    if not os.path.exists(_KEY_PATH):
        k = os.urandom(32)
        fd = os.open(_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(k)
        os.chmod(_KEY_PATH, 0o600)
        return k
    with open(_KEY_PATH, "rb") as f:
        return f.read()


def encrypt(s: str) -> str:
    """对敏感字段加密；空值或已是密文原样返回。"""
    if not s or s.startswith(_PREFIX):
        return s
    nonce = os.urandom(12)
    cipher = AES.new(_key(), AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(s.encode("utf-8"))
    return _PREFIX + base64.b64encode(nonce + tag + ct).decode("ascii")


def decrypt(s: str) -> str:
    """对敏感字段解密；空值或非密文原样返回；密文校验失败降级返回原文。

    密文布局（base64 解码后）：nonce(12) + tag(16) + ct，必须与 encrypt 对应。
    """
    if not s or not s.startswith(_PREFIX):
        return s
    try:
        raw = base64.b64decode(s[len(_PREFIX):])
        return AES.new(_key(), AES.MODE_GCM, nonce=raw[:12]).decrypt_and_verify(
            raw[28:], raw[12:28]
        ).decode("utf-8")
    except Exception:
        return s
