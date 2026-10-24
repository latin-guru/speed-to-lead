"""Message senders. **Only MOCK senders exist in this project.**

``MockSmsSender`` and ``MockEmailSender`` never touch the network: "sending" means writing a
row to ``outbox_messages``. A real sender would implement the same protocol and pass
``idempotency_key`` to its provider (most SMS and email APIs accept one), so a crash between
"provider accepted it" and "we committed" cannot produce a second message either.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from app.followups.sequence import SMS_MAX, Channel
from app.jobs.queue import insert_ignore
from app.models import OutboxMessage
from app.security import fields


@dataclass(frozen=True)
class OutboundMessage:
    lead_id: uuid.UUID
    step: str
    channel: Channel
    to_address: str
    body: str
    idempotency_key: str
    transactional: bool
    subject: str | None = None


class MessageSender(Protocol):
    channel: Channel
    name: str

    def send(self, session: Session, message: OutboundMessage, *, now: datetime) -> bool:
        """Send at most once per ``idempotency_key``. True if this call sent it."""
        ...


class _MockOutboxSender:
    channel: Channel
    name: str

    def send(self, session: Session, message: OutboundMessage, *, now: datetime) -> bool:
        if message.channel is not self.channel:
            raise ValueError(f"{self.name} cannot send {message.channel} messages")
        body = message.body if self.channel is Channel.EMAIL else message.body[:SMS_MAX]
        return insert_ignore(
            session,
            OutboxMessage,
            {
                "lead_id": message.lead_id,
                "step": message.step,
                "channel": message.channel.value,
                "to_address_enc": fields.active().encrypt(message.to_address),
                "subject": message.subject,
                "body": body,
                "sender": self.name,
                "idempotency_key": message.idempotency_key,
                "transactional": message.transactional,
                "sent_at": now,
            },
            "idempotency_key",
        )


class MockSmsSender(_MockOutboxSender):
    """MOCK: records the SMS in the outbox table. No network, nothing is delivered."""

    channel = Channel.SMS
    name = "MOCK-sms"


class MockEmailSender(_MockOutboxSender):
    """MOCK: records the email in the outbox table. No network, nothing is delivered."""

    channel = Channel.EMAIL
    name = "MOCK-email"


def mock_senders() -> dict[Channel, MessageSender]:
    return {Channel.SMS: MockSmsSender(), Channel.EMAIL: MockEmailSender()}
