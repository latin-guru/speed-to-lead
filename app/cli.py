"""Operator commands: ``python -m app.cli <command>``.

- ``create-user --email E --role admin|rep|viewer``: add a dashboard account. The password
  is read from a prompt (twice), or from stdin with ``--password-stdin`` (for scripts).
  It is never accepted as a command-line argument, which would leak it into shell history
  and the process list.
- ``generate-key``: print a new Fernet key for FIELD_ENCRYPTION_KEY.
- ``rotate-keys``: re-encrypt every stored contact detail with the first FIELD_ENCRYPTION_KEY.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable
from typing import TextIO

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.logging import configure_logging
from app.models import Lead, OutboxMessage, UserRole
from app.security import fields
from app.services import audit
from app.services.users import UserError, create_user

Command = Callable[[argparse.Namespace, Settings, Session, TextIO], int]


def cmd_generate_key(args: argparse.Namespace, out: TextIO) -> int:
    out.write(Fernet.generate_key().decode() + "\n")
    return 0


def rotate_contact_keys(session: Session) -> int:
    """Re-encrypt every encrypted column under the primary key. Returns values rewritten."""
    crypto = fields.active()
    rewritten = 0
    for lead in session.scalars(select(Lead)):
        if lead.phone_enc:
            lead.phone_enc = crypto.rotate(lead.phone_enc)
            rewritten += 1
        if lead.email_enc:
            lead.email_enc = crypto.rotate(lead.email_enc)
            rewritten += 1
    for message in session.scalars(select(OutboxMessage)):
        message.to_address_enc = crypto.rotate(message.to_address_enc)
        rewritten += 1
    return rewritten


def _read_password(args: argparse.Namespace, stdin: TextIO) -> str:
    if args.password_stdin:
        return stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != first:
        raise UserError("mismatch", "the passwords did not match")
    return first


def cmd_create_user(
    args: argparse.Namespace, settings: Settings, session: Session, out: TextIO
) -> int:
    try:
        password = _read_password(args, args.stdin)
        user = create_user(session, args.email, password, args.role)
    except UserError as exc:
        out.write(f"error: {exc}\n")
        return 1
    audit.record(
        session,
        actor="cli",
        role=None,
        action="user_created",
        target_type="user",
        target_id=user.id,
        summary={"email": user.email, "role": user.role},
    )
    session.commit()
    out.write(f"created {user.role} {user.email} (id {user.id})\n")
    return 0


def cmd_rotate_keys(
    args: argparse.Namespace, settings: Settings, session: Session, out: TextIO
) -> int:
    count = rotate_contact_keys(session)
    session.commit()
    out.write(f"re-encrypted {count} values with the first FIELD_ENCRYPTION_KEY\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user", help="add a dashboard user")
    create.add_argument("--email", required=True)
    create.add_argument("--role", required=True, choices=[r.value for r in UserRole])
    create.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin"
    )
    sub.add_parser("generate-key", help="print a new FIELD_ENCRYPTION_KEY value")
    sub.add_parser("rotate-keys", help="re-encrypt contact details with the newest key")
    return parser


DB_COMMANDS: dict[str, Command] = {
    "create-user": cmd_create_user,
    "rotate-keys": cmd_rotate_keys,
}


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    out: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    args.stdin = stdin or sys.stdin
    out = out or sys.stdout
    if args.command == "generate-key":
        return cmd_generate_key(args, out)
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    fields.configure(fields.from_settings(settings))
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            return DB_COMMANDS[args.command](args, settings, session, out)
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
