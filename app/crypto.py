from cryptography.fernet import Fernet


class Cipher:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()


def generate_key() -> str:
    return Fernet.generate_key().decode()


if __name__ == "__main__":
    # python -m app.crypto — сгенерировать ENCRYPTION_KEY
    print(generate_key())
