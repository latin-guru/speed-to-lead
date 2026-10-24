import hashlib
import hmac

from app.security.signatures import sign_body, verify_signature

SECRET = b"sig-test-secret"  # pragma: allowlist secret
BODY = b'{"object":"page","entry":[]}'


def test_valid_signature() -> None:
    header = "sha256=" + hmac.new(SECRET, BODY, hashlib.sha256).hexdigest()
    assert sign_body(SECRET, BODY) == header
    assert verify_signature(SECRET, BODY, header)


def test_tampered_body_fails() -> None:
    header = sign_body(SECRET, BODY)
    assert not verify_signature(SECRET, BODY.replace(b"[]", b"[1]"), header)
    assert not verify_signature(SECRET, BODY + b" ", header)  # even whitespace matters


def test_wrong_secret_fails() -> None:
    assert not verify_signature(SECRET, BODY, sign_body(b"other", BODY))


def test_missing_or_malformed_header_fails() -> None:
    digest = hmac.new(SECRET, BODY, hashlib.sha256).hexdigest()
    assert not verify_signature(SECRET, BODY, None)
    assert not verify_signature(SECRET, BODY, "")
    assert not verify_signature(SECRET, BODY, digest)  # no prefix
    assert not verify_signature(SECRET, BODY, "sha1=" + digest)
    assert not verify_signature(b"", BODY, "sha256=" + digest)  # unconfigured secret
