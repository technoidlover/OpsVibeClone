"""Mã hóa secret lưu trong DB (GitHub token, webhook secret, biến môi trường).

Khóa Fernet được sinh một lần lúc khởi động đầu tiên và lưu ở
`<data_dir>/secret.key` với quyền 0600.
"""

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .i18n import t


class SecretBox:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    @classmethod
    def from_file(cls, path: Path) -> "SecretBox":
        if not path.exists():
            key = Fernet.generate_key()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
        return cls(path.read_bytes().strip())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as e:
            raise ValueError(t("crypto.decrypt_failed")) from e

    def encrypt_optional(self, plaintext: str | None) -> str | None:
        return self.encrypt(plaintext) if plaintext else None

    def decrypt_optional(self, ciphertext: str | None) -> str | None:
        return self.decrypt(ciphertext) if ciphertext else None
