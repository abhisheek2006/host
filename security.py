"""Security - Fernet encryption for environment variables."""

from cryptography.fernet import Fernet
from config import ENV_ENCRYPTION_KEY, logger


_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if not ENV_ENCRYPTION_KEY:
            raise RuntimeError("ENV_ENCRYPTION_KEY not set in .env")
        _fernet = Fernet(ENV_ENCRYPTION_KEY.encode() if isinstance(ENV_ENCRYPTION_KEY, str) else ENV_ENCRYPTION_KEY)
    return _fernet


def encrypt_value(plaintext: str) -> str:
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


def generate_key() -> str:
    return Fernet.generate_key().decode()
