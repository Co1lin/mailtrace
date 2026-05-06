"""ORM models. Phase 1: User and Address."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(120), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Per-user IMb identity. Issued by USPS BCG; users fill it in on first
    # login (or admin pre-fills it on user creation).
    mailer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    barcode_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    service_type_id: Mapped[int] = mapped_column(Integer, default=40, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_login_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Per-user USPS API credentials (apis.usps.com OAuth2 client_credentials).
    # Used for the in-page "Validate against USPS" button. Each user brings
    # their own developer.usps.com app — credentials are NOT shared across
    # users so quota / rate-limits / data attribution stay per-tenant.
    usps_client_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    usps_client_secret: Mapped[str] = mapped_column(String(256), default="", nullable=False)

    # Per-user Business Customer Gateway login (gateway.usps.com). Used for
    # the legacy IV-MTR pull-tracking API. Optional — the platform's push
    # receiver works without these. Stored plaintext (same threat model as
    # the SMTP password row).
    bcg_username: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    bcg_password: Mapped[str] = mapped_column(String(256), default="", nullable=False)

    # Cached probe results from the per-user setup-page Test buttons.
    # Empty string = never tested. "ok" or "fail: <message>".
    usps_api_last_check: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    bcg_last_check: Mapped[str] = mapped_column(String(512), default="", nullable=False)

    # Email notifications. notify_on_scans is the user's opt-in; notify_email
    # overrides the address we send to (defaults to User.email when null).
    notify_on_scans: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notify_email: Mapped[str | None] = mapped_column(String(254), nullable=True)

    addresses: Mapped[list[Address]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Address(Base):
    __tablename__ = "addresses"
    __table_args__ = (
        Index("ix_addresses_user_label", "user_id", "label"),
        UniqueConstraint("user_id", "label", name="uq_addresses_user_label"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="recipient")
    # role: "sender" | "recipient" | "both"

    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    company: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    street: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    address2: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    city: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    state: Mapped[str] = mapped_column(String(2), default="", nullable=False)
    zip: Mapped[str] = mapped_column(String(11), default="", nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="addresses")

    def to_block(self) -> str:
        return self.to_recipient_block()

    def to_recipient_block(self) -> str:
        # Tolerate None on optional columns when the model is constructed
        # in-memory (not yet persisted): default="" is enforced at the DB
        # layer, not on Python attribute access.
        def s(value: str | None) -> str:
            return (value or "").strip()

        zip_digits = "".join(ch for ch in (self.zip or "") if ch.isdigit())
        if len(zip_digits) >= 9:
            zip_part = f"{zip_digits[:5]}-{zip_digits[5:9]}"
            if len(zip_digits) == 11:
                zip_part += f"-{zip_digits[9:]}"
        else:
            zip_part = zip_digits
        parts = [
            s(self.name),
            s(self.company),
            s(self.street),
            s(self.address2),
            f"{s(self.city)}, {s(self.state)}, {zip_part}".strip(", "),
        ]
        return "\n".join(p for p in parts if p)


# Status values for MailPiece.status. Lifecycle:
#   generated → printed → in_flight → delivered
#                                   ↘ archived (soft delete, orthogonal)
# Only `in_flight` pieces get polled. `generated` and `printed` are stock
# states (IMb encoded but not yet handed to USPS).
STATUS_GENERATED = "generated"
STATUS_PRINTED = "printed"
STATUS_IN_FLIGHT = "in_flight"
STATUS_DELIVERED = "delivered"
STATUS_ARCHIVED = "archived"

ALL_STATUSES = (
    STATUS_GENERATED,
    STATUS_PRINTED,
    STATUS_IN_FLIGHT,
    STATUS_DELIVERED,
    STATUS_ARCHIVED,
)


class MailPiece(Base):
    __tablename__ = "mailpieces"
    __table_args__ = (
        Index("ix_mailpieces_user_status", "user_id", "status"),
        Index("ix_mailpieces_imb_raw", "imb_raw"),
        Index("ix_mailpieces_next_poll", "next_poll_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    label: Mapped[str] = mapped_column(String(80), default="", nullable=False)

    # FK is informational; sender_block / recipient_block are snapshotted so
    # later edits to the address-book entry don't mutate the printed piece.
    sender_address_id: Mapped[int | None] = mapped_column(
        ForeignKey("addresses.id", ondelete="SET NULL"), nullable=True
    )
    recipient_address_id: Mapped[int | None] = mapped_column(
        ForeignKey("addresses.id", ondelete="SET NULL"), nullable=True
    )
    sender_block: Mapped[str] = mapped_column(String(800), default="", nullable=False)
    recipient_block: Mapped[str] = mapped_column(String(800), default="", nullable=False)
    recipient_zip_raw: Mapped[str] = mapped_column(String(16), default="", nullable=False)

    # IMb identity at creation time (snapshotted).
    barcode_id: Mapped[int] = mapped_column(Integer, nullable=False)
    service_type_id: Mapped[int] = mapped_column(Integer, nullable=False)
    mailer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    serial: Mapped[int] = mapped_column(Integer, nullable=False)
    include_zip_in_imb: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    imb_letters: Mapped[str] = mapped_column(String(80), nullable=False)
    imb_raw: Mapped[str] = mapped_column(String(40), nullable=False)

    status: Mapped[str] = mapped_column(String(16), default=STATUS_GENERATED, nullable=False)
    # Restored on unarchive so we don't lose where the piece was in its
    # lifecycle ("" means never been archived, or archived from generated).
    pre_archive_status: Mapped[str] = mapped_column(String(16), default="", nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    printed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mailed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_polled_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_poll_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_poll_errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_notified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship()
    sender_address: Mapped[Address | None] = relationship(foreign_keys=[sender_address_id])
    recipient_address: Mapped[Address | None] = relationship(foreign_keys=[recipient_address_id])
    scans: Mapped[list[Scan]] = relationship(
        back_populates="mailpiece",
        cascade="all, delete-orphan",
        order_by="Scan.scanned_at.desc()",
    )

    def human_readable_imb(self) -> str:
        zip_part = self.recipient_zip_raw if self.include_zip_in_imb else ""
        return (
            f"{self.barcode_id:02d}-{self.service_type_id:03d}-{self.mailer_id:d}-"
            f"{self.serial:06d}-{zip_part}"
        ).rstrip("-")


class Scan(Base):
    __tablename__ = "scans"
    __table_args__ = (
        UniqueConstraint("mailpiece_id", "dedup_hash", name="uq_scans_mailpiece_dedup"),
        Index("ix_scans_mailpiece_scanned", "mailpiece_id", "scanned_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mailpiece_id: Mapped[int] = mapped_column(
        ForeignKey("mailpieces.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(8), default="poll", nullable=False
    )  # "poll" | "feed"

    scanned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_code: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    handling_event_type: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    mail_phase: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    machine_name: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    scanner_type: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    facility_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    facility_locale_key: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    facility_city: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    facility_state: Mapped[str] = mapped_column(String(2), default="", nullable=False)
    facility_zip: Mapped[str] = mapped_column(String(11), default="", nullable=False)

    dedup_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[str] = mapped_column(String(4000), default="", nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    mailpiece: Mapped[MailPiece] = relationship(back_populates="scans")


class AppConfig(Base):
    """Singleton (id=1) row for platform-level operational config.

    USPS credentials are NOT here — those are per-user (see User.usps_*
    and User.bcg_*) because mailtrace is a multi-tenant platform: each
    user brings their own developer.usps.com app and BCG account so
    quota, rate-limits, and audit trails stay separated.

    What does live here is platform-wide operational tuning:
      - poll cadence (interval, max-per-cycle)
      - auto-archive horizon

    Editable live at /admin/settings.
    """

    __tablename__ = "app_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Poll cadence. The background loop reads these each iteration so
    # changes take effect within one cycle.
    poll_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    poll_loop_interval_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    poll_max_per_cycle: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    auto_archive_after_days: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IngestSubscription(Base):
    """Singleton (id=1) configuration for the USPS IV-MTR push receiver.

    USPS' Informed Visibility "Data Delivery" runs on a schedule (e.g. every
    1h) and POSTs JSON over HTTPS with HTTP Basic Auth. Their source IPs
    are not fixed/published, so we authenticate via Basic Auth, not IP
    allowlist. This row is the per-deployment config; only one feed
    subscription is supported (USPS-side limitation: one feed/destination).
    """

    __tablename__ = "ingest_subscription"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    basic_auth_user: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    # Stored plaintext — same threat model as the SMTP password row.
    basic_auth_pass: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    expect_gzip: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_body_mb: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    archive_payloads: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # When blank, the receiver falls back to settings.ingest_archive_dir.
    archive_dir: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    last_received_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IngestLog(Base):
    """Per-POST audit trail. One row per delivery attempt from USPS."""

    __tablename__ = "ingest_logs"
    __table_args__ = (Index("ix_ingest_logs_received", "received_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    source_ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    feed_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_scans: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    orphaned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bytes_received: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    raw_path: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    # "received" | "parsed" | "failed"
    status: Mapped[str] = mapped_column(String(16), default="received", nullable=False)
    error: Mapped[str] = mapped_column(String(2000), default="", nullable=False)


class SmtpConfig(Base):
    """Singleton — only id=1 is ever populated.

    Storing the SMTP password in plaintext. The threat model is "homelab,
    on-disk SQLite, single trusted operator"; encrypting it with a key
    that lives in the same process buys nothing. Operators using SMTP
    relays (SendGrid/Postmark) can scope the API key to "send mail only".
    """

    __tablename__ = "smtp_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column(String(254), default="", nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=587, nullable=False)
    username: Mapped[str] = mapped_column(String(254), default="", nullable=False)
    password: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    encryption: Mapped[str] = mapped_column(
        String(16), default="starttls", nullable=False
    )  # "starttls" | "tls" | "none"
    from_address: Mapped[str] = mapped_column(String(254), default="", nullable=False)
    from_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    public_base_url: Mapped[str] = mapped_column(String(254), default="", nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
