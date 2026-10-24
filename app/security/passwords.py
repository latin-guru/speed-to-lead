"""Password hashing with argon2id (argon2-cffi), and the password policy.

- Hashes use argon2-cffi's defaults: argon2id, RFC 9106 "low memory" profile (t=3, 64 MiB,
  p=4), a random 16-byte salt per hash. Old hashes are upgraded on the next login when the
  parameters change (``needs_rehash``).
- ``dummy_verify`` runs a full verification against a throwaway hash. Login calls it when
  the email is unknown or the account is locked, so response time does not reveal which
  emails have accounts.
- Policy: 12 to 128 characters (the cap stops multi-megabyte inputs from tying up a
  worker), and not the same as the email.
"""

from __future__ import annotations

import contextlib

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 128

_hasher = PasswordHasher()
_dummy_hash: str | None = None


class PasswordPolicyError(ValueError):
    pass


def configure(hasher: PasswordHasher) -> None:
    """Swap the hasher (the test suite uses cheaper parameters to stay fast)."""
    global _hasher, _dummy_hash
    _hasher = hasher
    _dummy_hash = None


def check_policy(password: str, *, email: str | None = None) -> None:
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"password must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(f"password must be at most {MAX_LENGTH} characters")
    if email and password.strip().lower() == email.strip().lower():
        raise PasswordPolicyError("password must not be the email address")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if len(password) > MAX_LENGTH:
        dummy_verify()
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def dummy_verify() -> None:
    """Spend the same time as a real verification (unknown email, locked account)."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _hasher.hash("speed-to-lead dummy password for timing")
    with contextlib.suppress(VerificationError):
        _hasher.verify(_dummy_hash, "not the dummy password")
