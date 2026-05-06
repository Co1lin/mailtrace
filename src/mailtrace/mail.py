"""SMTP send wrapper.

A thin async wrapper around aiosmtplib that knows how to read our
SmtpConfig row, build a multipart email, and surface useful errors back to
the admin SMTP test page.

Tests inject a `FakeMailer` via app.state.mailer instead of touching the
network — see conftest.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

import aiosmtplib

from .models import SmtpConfig

log = logging.getLogger(__name__)


class MailerError(RuntimeError):
    """Raised when SMTP delivery fails. Carries a user-facing message."""


@dataclass
class OutgoingMessage:
    to: str
    subject: str
    body_text: str
    body_html: str | None = None


class Mailer:
    """Sends mail via the SMTP server configured in SmtpConfig."""

    def __init__(self, config: SmtpConfig) -> None:
        self.config = config

    async def send(self, msg: OutgoingMessage) -> None:
        if not self.config.enabled:
            raise MailerError("SMTP is not enabled. Configure it in /admin/email first.")
        if not self.config.host or not self.config.from_address:
            raise MailerError("SMTP host or from-address is not set.")

        em = EmailMessage()
        em["From"] = (
            formataddr((self.config.from_name, self.config.from_address))
            if self.config.from_name
            else self.config.from_address
        )
        em["To"] = msg.to
        em["Subject"] = msg.subject
        em.set_content(msg.body_text)
        if msg.body_html:
            em.add_alternative(msg.body_html, subtype="html")

        encryption = (self.config.encryption or "starttls").lower()
        try:
            await aiosmtplib.send(
                em,
                hostname=self.config.host,
                port=self.config.port,
                username=self.config.username or None,
                password=self.config.password or None,
                start_tls=encryption == "starttls",
                use_tls=encryption == "tls",
                timeout=20,
            )
        except (aiosmtplib.SMTPException, OSError) as err:
            raise MailerError(f"SMTP error: {err}") from err


# ---------------------------------------------------------------------------
# Lookup + factory
# ---------------------------------------------------------------------------


async def load_smtp_config(db: object) -> SmtpConfig | None:
    """Return the singleton SmtpConfig row, or None if it has not been
    created yet."""
    from sqlalchemy import select  # local import to keep mail.py optional

    from .models import SmtpConfig as _SmtpConfig

    rows: Iterable[_SmtpConfig] = (
        (await db.execute(select(_SmtpConfig).order_by(_SmtpConfig.id).limit(1))).scalars().all()  # type: ignore[attr-defined]
    )
    for row in rows:
        return row
    return None
