import time

from app.security.csrf import MAX_AGE_SECONDS, check_double_submit, issue_token, token_valid

KEY = b"csrf-test-key"  # pragma: allowlist secret


def test_issue_and_validate() -> None:
    token = issue_token(KEY)
    assert token_valid(KEY, token)
    assert check_double_submit(KEY, token, token)


def test_rejects_wrong_key_tamper_and_garbage() -> None:
    token = issue_token(KEY)
    assert not token_valid(b"other", token)
    issued, nonce, mac = token.split(".")
    assert not token_valid(KEY, f"{issued}.{nonce}x.{mac}")
    assert not token_valid(KEY, "garbage")
    assert not token_valid(KEY, None)


def test_expiry() -> None:
    old = issue_token(KEY, now=time.time() - MAX_AGE_SECONDS - 5)
    assert not token_valid(KEY, old)


def test_double_submit_requires_match() -> None:
    a, b = issue_token(KEY), issue_token(KEY)
    assert not check_double_submit(KEY, a, b)
    assert not check_double_submit(KEY, None, a)
    assert not check_double_submit(KEY, a, "")
