import pytest
from cryptography.fernet import InvalidToken
from app.crypto import Cipher, generate_key


def test_roundtrip():
    c = Cipher(generate_key())
    enc = c.encrypt("sk-ant-ort01-secret")
    assert "secret" not in enc
    assert c.decrypt(enc) == "sk-ant-ort01-secret"


def test_wrong_key_fails():
    enc = Cipher(generate_key()).encrypt("x")
    with pytest.raises(InvalidToken):
        Cipher(generate_key()).decrypt(enc)
