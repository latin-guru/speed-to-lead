"""Interview mode email: content, the SMTP backend (against a local fake server, never the
network), the Gmail API skeleton (mocked HTTP), and the settings that guard real email."""

from __future__ import annotations

import base64
import re
import smtplib
import socket
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import Any, ClassVar

import httpx
import pytest
import respx
from pydantic import SecretStr, ValidationError

from app.interview.mailer import (
    FOOTER,
    GMAIL_SEND_URL,
    GOOGLE_TOKEN_URL,
    SUBJECT,
    EmailDeliveryError,
    EmailNotConfiguredError,
    GmailApiBackend,
    SmtpBackend,
    build_backend,
    build_message,
    build_real_sender,
    compose,
)
from tests.conftest import make_settings

NOW = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
TO = "visitor@example.com"
PROJECT = "https://github.com/latin-guru/speed-to-lead"


def _email() -> Any:
    return compose(
        name="Avery Quinn",
        company="Copperline Roofing & Solar (fictional)",
        callback_minutes=15,
        project_url=PROJECT,
    )


def _message() -> EmailMessage:
    return build_message(
        _email(), to=TO, from_addr="sender@example.com", from_name="Copperline (demo)", now=NOW
    )


# --------------------------------------------------------------------------- content


def test_subject_and_footer_are_exact() -> None:
    email = _email()
    assert email.subject == "We got your request — Copperline Roofing & Solar (demo)"
    assert email.subject == SUBJECT
    assert FOOTER.startswith("This is a one-time demo email from Kevin ")
    assert "No further emails will be sent." in FOOTER
    assert FOOTER in email.text
    assert "Hi Avery," in email.text
    assert "Copperline Roofing & Solar is a fictional company" in email.text


def test_no_tracking_and_no_links_but_the_project() -> None:
    email = _email()
    assert "<img" not in email.html.lower()
    assert "<script" not in email.html.lower()
    assert re.findall(r'href="([^"]+)"', email.html) == [PROJECT]
    assert re.findall(r"https?://\S+", email.text) == [PROJECT]
    # The footer is in the HTML part too (escaped).
    assert "one-time demo email" in email.html
    assert "No further emails will be sent." in email.html


def test_the_name_is_escaped_in_html() -> None:
    email = compose(name="<b>Eve</b>", company="X", callback_minutes=15, project_url=PROJECT)
    assert "<b>" not in email.html
    assert "&lt;b&gt;Eve&lt;/b&gt;" in email.html


def test_message_headers_and_parts() -> None:
    msg = _message()
    assert msg["To"] == TO
    assert msg["Subject"] == SUBJECT
    assert msg["From"] == '"Copperline (demo)" <sender@example.com>'
    assert msg["Message-ID"].endswith("@example.com>")
    assert msg["Auto-Submitted"] == "auto-replied"
    types = [part.get_content_type() for part in msg.walk()]
    assert types == ["multipart/alternative", "text/plain", "text/html"]
    assert "List-Unsubscribe" not in msg  # one email, nothing to unsubscribe from


# --------------------------------------------------------------------------- SMTP


class FakeSMTPServer:
    """A tiny SMTP server on 127.0.0.1 (one connection at a time, plain text, no AUTH)."""

    def __init__(self, *, reject_rcpt: bool = False) -> None:
        self.reject_rcpt = reject_rcpt
        self.messages: list[bytes] = []
        self.commands: list[str] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            f = conn.makefile("rb")

            def reply(line: str) -> None:
                conn.sendall(line.encode() + b"\r\n")

            reply("220 fake ESMTP")
            while True:
                raw = f.readline()
                if not raw:
                    return
                line = raw.decode().rstrip("\r\n")
                verb = line.split(" ", 1)[0].upper()
                self.commands.append(verb)
                if verb in ("EHLO", "HELO"):
                    reply("250-fake")
                    reply("250 8BITMIME")
                elif verb == "MAIL":
                    reply("250 OK")
                elif verb == "RCPT":
                    address = line.split(":", 1)[1]
                    reply(f"550 no such user {address}" if self.reject_rcpt else "250 OK")
                elif verb == "DATA":
                    reply("354 go ahead")
                    data = b""
                    while not data.endswith(b"\r\n.\r\n"):
                        chunk = f.readline()
                        if not chunk:
                            return
                        data += chunk
                    self.messages.append(data[: -len(b".\r\n")])
                    reply("250 queued")
                elif verb == "RSET":
                    reply("250 OK")
                elif verb == "QUIT":
                    reply("221 bye")
                    return
                else:
                    reply("502 not implemented")

    def close(self) -> None:
        self.sock.close()
        self.thread.join(5)


@pytest.fixture
def smtp_server() -> Iterator[FakeSMTPServer]:
    server = FakeSMTPServer()
    yield server
    server.close()


def test_smtp_backend_delivers_to_a_local_fake_server(smtp_server: FakeSMTPServer) -> None:
    backend = SmtpBackend(
        "127.0.0.1", smtp_server.port, username=None, password=None, starttls=False, timeout=5
    )
    backend.deliver(_message())
    smtp_server.thread.join(5)
    assert len(smtp_server.messages) == 1
    received = message_from_bytes(smtp_server.messages[0], policy=policy.default)
    assert received["To"] == TO
    assert received["Subject"] == SUBJECT
    assert smtp_server.commands[:4] == ["EHLO", "MAIL", "RCPT", "DATA"]
    assert "STARTTLS" not in smtp_server.commands


def test_smtp_refusal_never_carries_the_address() -> None:
    server = FakeSMTPServer(reject_rcpt=True)
    try:
        backend = SmtpBackend(
            "127.0.0.1", server.port, username=None, password=None, starttls=False, timeout=5
        )
        with pytest.raises(EmailDeliveryError) as info:
            backend.deliver(_message())
    finally:
        server.close()
    assert "visitor" not in str(info.value)
    assert str(info.value) == "SMTP error: SMTPRecipientsRefused"
    assert info.value.__suppress_context__  # the original (with the address) is not chained


def test_smtp_connection_failure_is_a_delivery_error() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here now
    backend = SmtpBackend("127.0.0.1", port, username=None, password=None, starttls=False)
    with pytest.raises(EmailDeliveryError, match="connection failed"):
        backend.deliver(_message())


class RecordingSMTP:
    """Stands in for smtplib.SMTP to check the STARTTLS + login sequence."""

    instances: ClassVar[list[RecordingSMTP]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.calls: list[Any] = [("connect", host, port, timeout)]
        self.refused: dict[str, Any] = {}
        RecordingSMTP.instances.append(self)

    def __enter__(self) -> RecordingSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, context: Any) -> None:
        assert context.verify_mode.name == "CERT_REQUIRED"
        assert context.check_hostname
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user))

    def send_message(self, msg: EmailMessage) -> dict[str, Any]:
        self.calls.append(("send", msg["To"]))
        return self.refused


def test_smtp_uses_starttls_then_logs_in() -> None:
    RecordingSMTP.instances.clear()
    backend = SmtpBackend(
        "smtp.example.com",
        587,
        username="sender@example.com",
        password="app-password",  # pragma: allowlist secret
        smtp_factory=RecordingSMTP,  # type: ignore[arg-type]
    )
    backend.deliver(_message())
    calls = RecordingSMTP.instances[0].calls
    assert calls == [
        ("connect", "smtp.example.com", 587, 10.0),
        "ehlo",
        "starttls",
        "ehlo",
        ("login", "sender@example.com"),
        ("send", TO),
        "quit",
    ]


def test_smtp_partial_refusal_is_an_error() -> None:
    class Refusing(RecordingSMTP):
        def send_message(self, msg: EmailMessage) -> dict[str, Any]:
            return {TO: (550, b"no")}

    backend = SmtpBackend(
        "smtp.example.com",
        587,
        username=None,
        password=None,
        smtp_factory=Refusing,  # type: ignore[arg-type]
    )
    with pytest.raises(EmailDeliveryError, match="refused the recipient") as info:
        backend.deliver(_message())
    assert TO not in str(info.value)


def test_smtp_auth_error_is_wrapped() -> None:
    class BadLogin(RecordingSMTP):
        def login(self, user: str, password: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    backend = SmtpBackend(
        "smtp.example.com",
        587,
        username="u",
        password="p",  # pragma: allowlist secret
        smtp_factory=BadLogin,  # type: ignore[arg-type]
    )
    with pytest.raises(EmailDeliveryError, match="SMTPAuthenticationError"):
        backend.deliver(_message())


# --------------------------------------------------------------------------- Gmail API


def _gmail(client: httpx.Client) -> GmailApiBackend:
    return GmailApiBackend(
        client_id="client-id",
        client_secret="client-secret",  # pragma: allowlist secret
        refresh_token="refresh-token",  # pragma: allowlist secret
        client=client,
    )


def test_gmail_skeleton_refreshes_a_token_then_sends_raw() -> None:
    with respx.mock(assert_all_called=True) as mock, httpx.Client() as client:
        token = mock.post(GOOGLE_TOKEN_URL).respond(json={"access_token": "at-123"})
        send = mock.post(GMAIL_SEND_URL).respond(json={"id": "m1"})
        _gmail(client).deliver(_message())
    assert b"grant_type=refresh_token" in token.calls[0].request.content
    request = send.calls[0].request
    assert request.headers["authorization"] == "Bearer at-123"
    raw = httpx.Response(200, content=request.content).json()["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    assert message_from_bytes(decoded, policy=policy.default)["Subject"] == SUBJECT
    assert GOOGLE_TOKEN_URL.startswith("https://oauth2.googleapis.com/")
    assert GMAIL_SEND_URL.startswith("https://gmail.googleapis.com/")


@pytest.mark.parametrize(
    ("token_response", "match"),
    [
        (httpx.Response(401, json={"error": "invalid_grant"}), "returned 401"),
        (httpx.Response(200, json={}), "no access_token"),
        (httpx.Response(200, content=b"not json"), "invalid JSON"),
    ],
)
def test_gmail_skeleton_errors(token_response: httpx.Response, match: str) -> None:
    with respx.mock() as mock, httpx.Client() as client:
        mock.post(GOOGLE_TOKEN_URL).mock(return_value=token_response)
        with pytest.raises(EmailDeliveryError, match=match):
            _gmail(client).deliver(_message())


def test_gmail_skeleton_unreachable() -> None:
    with respx.mock() as mock, httpx.Client() as client:
        mock.post(GOOGLE_TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(EmailDeliveryError, match="unreachable"):
            _gmail(client).deliver(_message())


def test_gmail_skeleton_is_not_configured_without_credentials() -> None:
    with pytest.raises(EmailNotConfiguredError):
        GmailApiBackend(client_id="", client_secret="x", refresh_token="y")


# --------------------------------------------------------------------------- settings

SMTP = {
    "email_backend": "smtp",
    "interview_email_enabled": True,
    "email_from": "sender@example.com",
    "smtp_host": "smtp.example.com",
    "smtp_username": "sender@example.com",
    "smtp_password": SecretStr("app-password"),  # pragma: allowlist secret
}


def test_default_is_mock_and_builds_no_real_sender() -> None:
    settings = make_settings()
    assert settings.email_backend == "mock"
    assert not settings.interview_email_enabled
    assert not settings.real_email_configured
    assert build_backend(settings) is None
    assert build_real_sender(settings) is None
    # Enabled alone is not enough: the backend is still the mock.
    assert not make_settings(interview_email_enabled=True).real_email_configured


def test_real_backend_needs_explicit_enable_and_a_from_address() -> None:
    with pytest.raises(ValidationError, match="INTERVIEW_EMAIL_ENABLED=true"):
        make_settings(**{**SMTP, "interview_email_enabled": False})
    with pytest.raises(ValidationError, match="EMAIL_FROM must be set"):
        make_settings(**{**SMTP, "email_from": ""})
    with pytest.raises(ValidationError, match="valid email"):
        make_settings(**{**SMTP, "email_from": "not-an-email"})
    with pytest.raises(ValidationError, match="one line"):
        make_settings(**SMTP, email_from_name="a\nBcc: x@example.com")
    settings = make_settings(**SMTP)
    assert settings.real_email_configured
    backend = build_backend(settings)
    assert isinstance(backend, SmtpBackend)
    assert (backend.host, backend.port, backend.starttls) == ("smtp.example.com", 587, True)
    sender = build_real_sender(settings)
    assert sender is not None
    assert sender.name == "SMTP-email"


def test_smtp_settings_are_checked() -> None:
    with pytest.raises(ValidationError, match="SMTP_HOST"):
        make_settings(**{**SMTP, "smtp_host": ""})
    with pytest.raises(ValidationError, match="loopback"):
        make_settings(**SMTP, smtp_starttls=False)
    assert make_settings(**{**SMTP, "smtp_host": "127.0.0.1"}, smtp_starttls=False).smtp_host
    with pytest.raises(ValidationError, match="both SMTP_USERNAME and SMTP_PASSWORD"):
        make_settings(**{**SMTP, "smtp_password": None})
    with pytest.raises(ValidationError, match="both SMTP_USERNAME and SMTP_PASSWORD"):
        make_settings(**{**SMTP, "smtp_username": None})
    with pytest.raises(ValidationError, match="MAX_PER_HOUR"):
        make_settings(interview_email_max_per_hour=20, interview_email_max_per_day=10)


def test_gmail_settings_are_checked() -> None:
    gmail = {**SMTP, "email_backend": "gmail_api"}
    with pytest.raises(ValidationError, match="GMAIL_CLIENT_ID"):
        make_settings(**gmail)
    settings = make_settings(
        **gmail,
        gmail_client_id="id",
        gmail_client_secret=SecretStr("secret"),  # pragma: allowlist secret
        gmail_refresh_token=SecretStr("refresh"),  # pragma: allowlist secret
    )
    assert isinstance(build_backend(settings), GmailApiBackend)


def test_demo_mode_refuses_real_email_with_public_accounts() -> None:
    with pytest.raises(ValidationError, match="DEMO_ROLES=viewer"):
        make_settings(demo_mode=True, demo_roles="admin,rep,viewer", **SMTP)
    with pytest.raises(ValidationError, match="DEMO_ROLES=viewer"):
        make_settings(demo_mode=True, demo_roles="rep,viewer", **SMTP)
    ok = make_settings(demo_mode=True, demo_roles="viewer", **SMTP)
    assert ok.real_email_configured
    # The existing demo refusals still hold with interview email on.
    with pytest.raises(ValidationError, match="MOCK CRMs"):
        make_settings(demo_mode=True, demo_roles="viewer", crm_mock=False, **SMTP)
    with pytest.raises(ValidationError, match="DEMO_ALLOW_REAL_AI"):
        make_settings(
            demo_mode=True,
            demo_roles="viewer",
            ai_provider="claude",
            anthropic_api_key=SecretStr("not-a-real-key"),
            **SMTP,
        )


OWNER_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # pragma: allowlist secret


def test_demo_owner_settings_are_checked() -> None:
    owner = {
        "demo_owner_email": "Owner@Example.org",
        "demo_owner_password": SecretStr("a long private passphrase"),  # pragma: allowlist secret
        "demo_owner_totp_secret": SecretStr("jbsw y3dp ehpk 3pxp jbsw y3dp ehpk 3pxp"),
    }
    settings = make_settings(demo_mode=True, demo_roles="viewer", **owner)
    assert settings.demo_owner_email == "owner@example.org"
    assert settings.demo_owner_totp_secret is not None
    assert settings.demo_owner_totp_secret.get_secret_value() == OWNER_SECRET
    with pytest.raises(ValidationError, match="only applies in DEMO_MODE"):
        make_settings(**owner)
    with pytest.raises(ValidationError, match="go together"):
        make_settings(demo_mode=True, **{**owner, "demo_owner_totp_secret": None})
    with pytest.raises(ValidationError, match="not a demo account"):
        make_settings(demo_mode=True, **{**owner, "demo_owner_email": "admin@example.com"})
    with pytest.raises(ValidationError, match="12 to 128"):
        make_settings(demo_mode=True, **{**owner, "demo_owner_password": SecretStr("short")})
    with pytest.raises(ValidationError, match="public"):
        make_settings(
            demo_mode=True,
            **{**owner, "demo_owner_password": SecretStr("copperline-demo")},
        )
    for bad in ("not base32 !!", "JBSWY3DP"):
        with pytest.raises(ValidationError, match="80 bits"):
            make_settings(demo_mode=True, **{**owner, "demo_owner_totp_secret": SecretStr(bad)})
