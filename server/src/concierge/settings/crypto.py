from typing import Any

from cryptography.fernet import Fernet

from concierge.core.config import get_settings

SECRET_FIELD = "$secret"
SECRET_MASK = "********"


class SecretKeyNotConfiguredError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().secret_key
    if not key:
        raise SecretKeyNotConfiguredError(
            "CONCIERGE_SECRET_KEY is not set; cannot store encrypted secrets"
        )
    return Fernet(key.encode())


def wrap_secret(plaintext: str) -> dict[str, str]:
    return {SECRET_FIELD: _fernet().encrypt(plaintext.encode()).decode()}


def unwrap_secret(stored: dict[str, Any]) -> str:
    return _fernet().decrypt(stored[SECRET_FIELD].encode()).decode()


def is_secret(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {SECRET_FIELD}


def mask_value(value: Any) -> Any:
    return {"secret": SECRET_MASK} if is_secret(value) else value
