"""Mã hóa file sao lưu bằng passphrase (AES-256-GCM theo từng khối, khóa sinh bằng scrypt).

Định dạng: MAGIC | salt (16) | tiền tố nonce (8) | các khối [độ dài 4 byte][bản mã + tag].
Nonce mỗi khối = tiền tố + số thứ tự; khối cuối mang AAD "last" nên file bị cắt cụt hay đảo khối
đều bị phát hiện khi giải mã. Mã hóa theo khối để file vài GB không phải nạp hết vào bộ nhớ.
"""

import os
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"VPBAK1\n"
CHUNK = 1024 * 1024
MIN_PASSPHRASE = 10


class VaultError(ValueError):
    pass


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode())


def _nonce(prefix: bytes, index: int) -> bytes:
    return prefix + struct.pack(">I", index)


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    salt, prefix = os.urandom(16), os.urandom(8)
    aes = AESGCM(_key(passphrase, salt))
    with src.open("rb") as f, dst.open("wb") as out:
        out.write(MAGIC + salt + prefix)
        index = 0
        chunk = f.read(CHUNK)
        while True:
            following = f.read(CHUNK)
            last = not following
            data = aes.encrypt(_nonce(prefix, index), chunk, b"last" if last else b"")
            out.write(struct.pack(">I", len(data)) + data)
            if last:
                return
            chunk, index = following, index + 1


def decrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    with src.open("rb") as f, dst.open("wb") as out:
        if f.read(len(MAGIC)) != MAGIC:
            raise VaultError("Không phải file sao lưu OpsVibes / not an OpsVibes backup file")
        salt, prefix = f.read(16), f.read(8)
        aes = AESGCM(_key(passphrase, salt))
        index = 0
        while True:
            head = f.read(4)
            if len(head) < 4:
                raise VaultError("File sao lưu bị cắt cụt / backup file is truncated")
            data = f.read(struct.unpack(">I", head)[0])
            last = f.peek(1) == b"" if hasattr(f, "peek") else False
            try:
                out.write(aes.decrypt(_nonce(prefix, index), data, b"last" if last else b""))
            except InvalidTag:
                raise VaultError("Sai passphrase hoặc file hỏng / wrong passphrase or corrupted file") from None
            if last:
                return
            index += 1
