import pytest
from cryptography.exceptions import InvalidTag

from obot.crypto import TokenCipher


def test_roundtrip(cipher: TokenCipher) -> None:
    blob = cipher.encrypt("123:secret", context="42")

    assert b"secret" not in blob
    assert cipher.decrypt(blob, context="42") == "123:secret"


def test_ciphertext_is_bound_to_context(cipher: TokenCipher) -> None:
    blob = cipher.encrypt("123:secret", context="42")

    with pytest.raises(InvalidTag):
        cipher.decrypt(blob, context="43")


def test_wrong_key_fails(cipher: TokenCipher) -> None:
    blob = cipher.encrypt("123:secret", context="42")
    other = TokenCipher.from_b64(TokenCipher.generate_key_b64())

    with pytest.raises(InvalidTag):
        other.decrypt(blob, context="42")


def test_rejects_short_key() -> None:
    with pytest.raises(ValueError):
        TokenCipher(b"short")
