"""网页密码的哈希（总管理员自动生成的密码、群管理员密码共用）。

- 新存的一律 scrypt：`scrypt$<n>$<r>$<p>$<salt hex>$<hash hex>`（n=2^14、r=8、p=1，
  一次约几十毫秒、16MB 内存，拿到库也很难离线暴力猜）。
- 老格式 `sha256$<salt hex>$<hash hex>`（sha256(salt + 密码)）照样认；登录成功时由调用方
  用 `needs_upgrade` 判断、`hash_password` 重算一份换掉（`verify` 本身不写库）。
- 只用标准库 hashlib，不加依赖。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as _secrets

_N, _R, _P = 2**14, 8, 1
_DKLEN = 32
# scrypt 需要 128 * n * r 字节，给一点余量；参数被篡改成离谱的值时 hashlib 会抛错（当不对）
_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = _secrets.token_hex(16)
    digest = hashlib.scrypt(
        str(password).encode("utf-8"), salt=bytes.fromhex(salt), n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM,
    ).hex()
    return f"scrypt${_N}${_R}${_P}${salt}${digest}"


def verify(password: str, stored: str) -> bool:
    """密码对不对得上存的哈希；格式不认识 / 坏数据 → False。"""
    s = str(stored or "")
    pw = str(password).encode("utf-8")
    if s.startswith("scrypt$"):
        try:
            _algo, n, r, p, salt, expect = s.split("$", 5)
            actual = hashlib.scrypt(
                pw, salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(expect) // 2, maxmem=_MAXMEM,
            ).hex()
        except (ValueError, MemoryError):
            return False
        return bool(expect) and hmac.compare_digest(actual, expect)
    if s.startswith("sha256$"):
        try:
            _algo, salt, expect = s.split("$", 2)
        except ValueError:
            return False
        if not salt or not expect:
            return False
        actual = hashlib.sha256(salt.encode("utf-8") + pw).hexdigest()
        return hmac.compare_digest(actual, expect)
    return False


def needs_upgrade(stored: str) -> bool:
    """是不是老的 sha256 格式（登录成功后该换成 scrypt）。"""
    return str(stored or "").startswith("sha256$")
