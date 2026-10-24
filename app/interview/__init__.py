"""Interview mode: invite-only links that let one visitor receive one real email.

- ``invites``: single-use, short-lived tokens, stored only as hashes.
- ``mailer``: the demo email's content and the real email backends (SMTP, Gmail API skeleton).
- ``routing``: decides, per message, whether the real sender or the mock is used (caps).
- ``panel``: the live "what happened to my lead" view for the visitor.

SMS is never real here, and nothing outside an invite ever reaches a real sender.
"""

INTERVIEW_SOURCE = "interview_demo"
