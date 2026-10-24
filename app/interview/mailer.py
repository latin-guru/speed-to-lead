"""The one real email of Interview mode: its content, the backends, and the sender.

``RealEmailSender`` implements the same ``MessageSender`` protocol as the mocks. It is only
ever handed a message by ``app.interview.routing`` (an invite lead's instant
acknowledgment, under the caps); everything else keeps going to the mock senders.

Backends, chosen by ``EMAIL_BACKEND``:

- ``smtp``: SMTP with STARTTLS (port 587) and a login, e.g. Gmail with an app password.
- ``gmail_api``: a SKELETON of the Gmail API (OAuth refresh token -> users.messages.send).
  It shows the request shapes and is tested against mocked HTTP only; it has not been run
  against a live account.

The email is plain text plus a simple HTML part: no images, no tracking pixels, and no
link except the project's. Errors never carry the recipient's address (an SMTP refusal
message would), so job errors and logs cannot leak it.
"""

from __future__ import annotations

import base64
import html
import logging
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import Session

from app.config import Settings
from app.followups.senders import OutboundMessage
from app.followups.sequence import Channel, first_name
from app.jobs.queue import insert_ignore
from app.logging import mask_pii
from app.models import Lead, OutboxMessage
from app.security import fields
from app.security.outbound import check_url

log = logging.getLogger(__name__)

SUBJECT = "We got your request — Copperline Roofing & Solar (demo)"
FOOTER = (
    "This is a one-time demo email from Kevin Gaviria's speed-to-lead project. You received "
    "it because you entered this address on an interview demo link. No further emails will "
    "be sent."
)
SMTP_SENDER = "SMTP-email"
GMAIL_SENDER = "GMAIL-API-email"
REAL_SENDERS = frozenset({SMTP_SENDER, GMAIL_SENDER})
GOOGLE_TOKEN_URL = check_url("https://oauth2.googleapis.com") + "/token"
GMAIL_SEND_URL = check_url("https://gmail.googleapis.com") + "/gmail/v1/users/me/messages/send"


class EmailNotConfiguredError(RuntimeError):
    """The selected backend is missing settings."""


class EmailDeliveryError(RuntimeError):
    """Delivery failed. The message never includes the recipient's address."""


def mask_email(address: str) -> str:
    return mask_pii(address)


@dataclass(frozen=True)
class DemoEmail:
    subject: str
    text: str
    html: str


def compose(*, name: str, company: str, callback_minutes: int, project_url: str) -> DemoEmail:
    """The speed-to-lead acknowledgment for the fictional company, plus the demo footer."""
    first = first_name(name)
    company_plain = company.replace(" (fictional)", "")
    paragraphs = [
        f"Hi {first},",
        (
            f"Thanks for contacting {company_plain}. We got your request, and in the real "
            f"flow a specialist would call you within {callback_minutes} minutes to talk "
            "through your project."
        ),
        (
            "This is the instant reply speed-to-lead sends every new lead. Your request was "
            "captured, scored and synced to a mock CRM in seconds, and this email went out "
            "after the same consent and frequency checks as every other message."
        ),
        f"{company_plain} is a fictional company, so nobody will call you.",
    ]
    text = "\n\n".join(paragraphs) + f"\n\n--\n{FOOTER}\nProject: {project_url}\n"
    body = "".join(f"<p>{html.escape(p)}</p>\n" for p in paragraphs)
    url = html.escape(project_url, quote=True)
    page = (
        '<!doctype html>\n<html lang="en"><body style="font-family:sans-serif;line-height:1.5">\n'
        f"{body}<hr>\n"
        f'<p style="color:#555;font-size:13px">{html.escape(FOOTER)}<br>'
        f'Project: <a href="{url}">{url}</a></p>\n'
        "</body></html>\n"
    )
    return DemoEmail(SUBJECT, text, page)


def build_message(
    email: DemoEmail, *, to: str, from_addr: str, from_name: str, now: datetime
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = email.subject
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to
    msg["Date"] = format_datetime(now)
    msg["Message-ID"] = make_msgid(domain=from_addr.rsplit("@", 1)[-1])
    msg["Auto-Submitted"] = "auto-replied"
    msg.set_content(email.text)
    msg.add_alternative(email.html, subtype="html")
    return msg


class EmailBackend(Protocol):
    name: str

    def deliver(self, message: EmailMessage) -> None: ...


class SmtpBackend:
    """SMTP with STARTTLS and a login (Gmail: smtp.gmail.com:587 with an app password)."""

    name = SMTP_SENDER

    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str | None,
        password: str | None,
        starttls: bool = True,
        timeout: float = 10.0,
        smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.starttls = starttls
        self.timeout = timeout
        self._factory = smtp_factory

    def deliver(self, message: EmailMessage) -> None:
        try:
            with self._factory(self.host, self.port, timeout=self.timeout) as smtp:
                smtp.ehlo()
                if self.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if self.username and self.password:
                    smtp.login(self.username, self.password)
                refused = smtp.send_message(message)
        except smtplib.SMTPException as exc:
            # str(exc) can contain the recipient (SMTPRecipientsRefused): never pass it on.
            raise EmailDeliveryError(f"SMTP error: {type(exc).__name__}") from None
        except OSError as exc:
            raise EmailDeliveryError(f"SMTP connection failed: {type(exc).__name__}") from None
        if refused:
            raise EmailDeliveryError("SMTP server refused the recipient")


class GmailApiBackend:
    """SKELETON: Gmail API ``users.messages.send`` with an OAuth refresh token.

    Not run against a live account. Hosts are on the outbound allowlist and fixed in code.
    """

    name = GMAIL_SENDER

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not (client_id and client_secret and refresh_token):
            raise EmailNotConfiguredError("Gmail API backend needs GMAIL_* settings")
        self._creds = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        self._client = client or httpx.Client(timeout=timeout)

    def _post(self, url: str, **kw: Any) -> dict[str, Any]:
        try:
            response = self._client.post(url, **kw)
        except httpx.HTTPError as exc:
            raise EmailDeliveryError(f"Gmail API unreachable: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise EmailDeliveryError(f"Gmail API returned {response.status_code}")
        try:
            data: dict[str, Any] = response.json()
        except ValueError:
            raise EmailDeliveryError("Gmail API returned invalid JSON") from None
        return data

    def deliver(self, message: EmailMessage) -> None:
        token = self._post(GOOGLE_TOKEN_URL, data=self._creds).get("access_token")
        if not isinstance(token, str) or not token:
            raise EmailDeliveryError("Gmail API token response had no access_token")
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        self._post(GMAIL_SEND_URL, json={"raw": raw}, headers={"Authorization": f"Bearer {token}"})


def build_backend(settings: Settings, client: httpx.Client | None = None) -> EmailBackend | None:
    """The real backend, or None when real email is off (EMAIL_BACKEND=mock or not enabled)."""
    if not settings.real_email_configured:
        return None
    if settings.email_backend == "smtp":
        if settings.smtp_host is None:  # the settings validator already refuses this
            raise EmailNotConfiguredError("SMTP_HOST is not set")
        return SmtpBackend(
            settings.smtp_host,
            settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
            starttls=settings.smtp_starttls,
            timeout=settings.smtp_timeout_seconds,
        )
    return GmailApiBackend(
        client_id=settings.gmail_client_id or "",
        client_secret=(
            settings.gmail_client_secret.get_secret_value() if settings.gmail_client_secret else ""
        ),
        refresh_token=(
            settings.gmail_refresh_token.get_secret_value() if settings.gmail_refresh_token else ""
        ),
        client=client,
        timeout=settings.smtp_timeout_seconds,
    )


class RealEmailSender:
    """Sends the demo email for real, at most once per idempotency key.

    The outbox row is written first (claiming the key inside the job's transaction), then
    the email is delivered. If delivery fails, the job's transaction rolls back and the job
    retries; the row records the content that was actually sent.
    """

    channel = Channel.EMAIL

    def __init__(
        self,
        backend: EmailBackend,
        *,
        from_addr: str,
        from_name: str,
        company: str,
        callback_minutes: int,
        project_url: str,
    ) -> None:
        self.backend = backend
        self.name = backend.name
        self.from_addr = from_addr
        self.from_name = from_name
        self.company = company
        self.callback_minutes = callback_minutes
        self.project_url = project_url

    def send(self, session: Session, message: OutboundMessage, *, now: datetime) -> bool:
        if message.channel is not Channel.EMAIL:
            raise ValueError(f"{self.name} only sends email")
        lead = session.get(Lead, message.lead_id)
        email = compose(
            name=lead.name if lead is not None else "",
            company=self.company,
            callback_minutes=self.callback_minutes,
            project_url=self.project_url,
        )
        inserted = insert_ignore(
            session,
            OutboxMessage,
            {
                "lead_id": message.lead_id,
                "step": message.step,
                "channel": Channel.EMAIL.value,
                "to_address_enc": fields.active().encrypt(message.to_address),
                "subject": email.subject,
                "body": email.text,
                "sender": self.name,
                "idempotency_key": message.idempotency_key,
                "transactional": message.transactional,
                "sent_at": now,
            },
            "idempotency_key",
        )
        if not inserted:
            return False
        self.backend.deliver(
            build_message(
                email,
                to=message.to_address,
                from_addr=self.from_addr,
                from_name=self.from_name,
                now=now,
            )
        )
        log.info(
            "interview_email_sent",
            extra={
                "lead_id": str(message.lead_id),
                "to": mask_email(message.to_address),
                "backend": self.name,
            },
        )
        return True


def build_real_sender(
    settings: Settings, backend: EmailBackend | None = None
) -> RealEmailSender | None:
    backend = backend or build_backend(settings)
    if backend is None or settings.email_from is None:
        return None
    return RealEmailSender(
        backend,
        from_addr=settings.email_from,
        from_name=settings.email_from_name,
        company=settings.company_name,
        callback_minutes=settings.callback_minutes,
        project_url=settings.interview_project_url,
    )
