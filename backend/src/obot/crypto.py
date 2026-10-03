import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_SIZE = 12


class TokenCipher:
    """Encrypts secrets at rest with AES-256-GCM.

    `context` is authenticated but not encrypted (e.g. the bot id), so a ciphertext
    copied onto another row fails to decrypt.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("encryption key must be 32 bytes")
        self._aead = AESGCM(key)

    @classmethod
    def from_b64(cls, key_b64: str) -> "TokenCipher":
        return cls(base64.urlsafe_b64decode(key_b64))

    @staticmethod
    def generate_key_b64() -> str:
        return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()

    def encrypt(self, plaintext: str, context: str) -> bytes:
        nonce = os.urandom(NONCE_SIZE)
        return nonce + self._aead.encrypt(nonce, plaintext.encode(), context.encode())

    def decrypt(self, blob: bytes, context: str) -> str:
        nonce, ciphertext = blob[:NONCE_SIZE], blob[NONCE_SIZE:]
        return self._aead.decrypt(nonce, ciphertext, context.encode()).decode()
