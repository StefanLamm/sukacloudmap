import hashlib
import hmac
import secrets

from pwdlib import PasswordHash

password_hasher = PasswordHash.recommended()
DUMMY_HASH = password_hasher.hash(secrets.token_urlsafe(32))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_for_session(token: str) -> str:
    return hmac.new(token.encode("utf-8"), b"suka-csrf-v1", hashlib.sha256).hexdigest()


def check_password(password: str, encoded: str | None):
    return password_hasher.verify_and_update(password, encoded or DUMMY_HASH)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)

