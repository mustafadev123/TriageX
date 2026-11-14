"""Live staff outreach over Chert iMessage, and verified inbound replies.

Flow: operator approves a staffing proposal -> one "TriageX DEMO" message per explicitly
opted-in phone -> a teammate replies IN -> the signed webhook is verified, deduplicated, and
correlated to the message we sent for the active request -> the confirmation goes through
DemoService.confirm_live, the same path (validation, expiry, recompute) as the simulated
fallback.

Nothing here ever reports success it has not observed: without credentials no message is
sent and the dashboard says live messaging is unavailable.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.config import DEMO_MESSAGE_PREFIX, Settings
from app.integrations.chert import (
    DEFAULT_API_BASE,
    WEBHOOK_PATH,
    CheckResult,
    ChertClient,
    InboundEvent,
    SignatureError,
    mask_phone,
    normalize_phone,
    parse_event,
    phone_hash,
    verify_webhook_signature,
)
from app.services.demo_service import ConflictError, DemoService, NotFoundError, Run

log = logging.getLogger("triagex.outreach")

CONFIRM_REPLY = "in"
SEEN_EVENTS_MAX = 5000


@dataclass(slots=True, frozen=True)
class ChertConfig:
    signing_secret: str | None = None
    tenant: str | None = None
    webhook_secret: str | None = None
    allowed_phones: tuple[str, ...] = ()
    rejected_phone_entries: int = 0
    public_base_url: str | None = None
    api_base: str = DEFAULT_API_BASE

    @classmethod
    def from_settings(cls, s: Settings) -> ChertConfig:
        entries = [e for e in s.demo_allowed_phones.split(",") if e.strip()]
        phones = tuple(dict.fromkeys(p for p in map(normalize_phone, entries) if p))
        return cls(
            signing_secret=s.chert_signing_secret or None,
            tenant=s.chert_tenant or None,
            webhook_secret=s.chert_webhook_secret or None,
            allowed_phones=phones,
            rejected_phone_entries=len(entries) - len(phones),
            public_base_url=(s.public_base_url or "").rstrip("/") or None,
            api_base=s.chert_api_base or DEFAULT_API_BASE,
        )

    @property
    def can_send(self) -> bool:
        return bool(self.signing_secret and self.allowed_phones)

    @property
    def webhook_url(self) -> str | None:
        return f"{self.public_base_url}{WEBHOOK_PATH}" if self.public_base_url else None

    def missing(self) -> list[str]:
        out = []
        if not self.signing_secret:
            out.append("CHERT_SIGNING_SECRET")
        if not self.webhook_secret:
            out.append("CHERT_WEBHOOK_SECRET")
        if not self.allowed_phones:
            out.append("DEMO_ALLOWED_PHONES")
        if not self.public_base_url:
            out.append("PUBLIC_BASE_URL")
        return out


@dataclass(slots=True)
class OutreachRecord:
    outreach_id: str
    run_id: str
    request_id: str
    phone: str  # kept in memory only; storage and the dashboard get the mask and hash
    hospital_id: str
    body: str
    created_at: datetime
    status: str = "sending"  # sending | sent | failed | confirmed
    message_id: str | None = None
    lead_id: str | None = None
    sent_at: datetime | None = None
    error: str | None = None
    send_ms: float | None = None
    confirmed_at: datetime | None = None

    @property
    def masked(self) -> str:
        return mask_phone(self.phone)

    @property
    def recipient_hash(self) -> str:
        return phone_hash(self.phone)

    @property
    def staff_id(self) -> str:
        return f"live-{self.recipient_hash[:8]}"

    @property
    def display_name(self) -> str:
        return f"Teammate {self.masked}"


@dataclass(slots=True)
class WebhookOutcome:
    http_status: int
    outcome: str  # see WEBHOOK_OUTCOMES
    detail: str
    event_id: str | None = None
    event_type: str | None = None
    run: Run | None = None  # set only when a confirmation changed the run
    record: OutreachRecord | None = None
    sender_hash: str | None = None
    signature_valid: bool = False
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    processing_ms: float = 0.0


WEBHOOK_OUTCOMES = (
    "confirmed",
    "duplicate_event",
    "already_confirmed",
    "not_confirmation_text",
    "unknown_sender",
    "stale_request",
    "rejected_request",
    "ignored_event_type",
    "ignored_outbound",
    "invalid_signature",
    "malformed",
    "not_configured",
)


def outreach_message(hospital_short_name: str) -> str:
    return (
        f"{DEMO_MESSAGE_PREFIX}: fictional hackathon scenario, not a real hospital request. "
        f"Simulated surge at {hospital_short_name}. Can you cover a fictional 4-hour shift? "
        f"Reply IN within 10 minutes to confirm. Any other reply is ignored."
    )


class OutreachService:
    def __init__(
        self,
        demo: DemoService,
        config: ChertConfig,
        client: ChertClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.demo = demo
        self.config = config
        if client is None and config.signing_secret:
            client = ChertClient(config.signing_secret, config.tenant, config.api_base)
        self.client = client
        self.clock = clock
        self._lock = threading.RLock()
        self._records: dict[str, OutreachRecord] = {}
        self._seen: OrderedDict[str, str] = OrderedDict()
        self.check_result: CheckResult | None = None
        self.checking = False
        self.checked_at: datetime | None = None
        self.last_webhook: WebhookOutcome | None = None
        self.last_live_confirmation_at: datetime | None = None
        self.counters = {k: 0 for k in WEBHOOK_OUTCOMES}
        demo.messaging = self

    # --- connection check -------------------------------------------------------------
    def check_connection(self) -> CheckResult | None:
        """Ask Chert whether the credentials work. Never raises."""
        if self.client is None:
            return None
        self.checking = True
        try:
            result = self.client.check()
        except Exception as exc:  # defensive: a check must never crash the app
            result = CheckResult(False, None, f"check failed: {type(exc).__name__}")
        finally:
            self.checking = False
        self.check_result, self.checked_at = result, self.clock()
        log.info("chert check", extra={"ok": result.ok, "status_code": result.status_code})
        return result

    def subscription_matches(self) -> bool | None:
        """True/False if the subscription list says whether our webhook URL is subscribed;
        None if that cannot be determined."""
        r, url = self.check_result, self.config.webhook_url
        if r is None or not r.ok or url is None or not r.subscriptions:
            return None if (r is None or not r.subscriptions) else False
        for s in r.subscriptions:
            events = s.get("events") or []
            if (
                s.get("url") == url
                and s.get("is_active", True)
                and (not events or "message.received" in events)
            ):
                return True
        return False

    def health(self) -> tuple[str, str]:
        """(status, detail) for /api/integrations/health. 'ok' only after Chert itself
        accepted the credentials and every inbound prerequisite is present."""
        c = self.config
        if not c.signing_secret:
            return "not_configured", "Missing: " + ", ".join(c.missing())
        if self.check_result is None:
            return "starting", "Checking credentials with Chert…"
        r = self.check_result
        if not r.ok:
            return "error", r.detail
        problems = [m for m in c.missing() if m != "CHERT_SIGNING_SECRET"]
        match = self.subscription_matches()
        parts = [f"Chert accepted the credentials (whoami HTTP {r.status_code})"]
        parts.append(f"{len(c.allowed_phones)} opted-in recipient(s)")
        if c.rejected_phone_entries:
            parts.append(f"{c.rejected_phone_entries} allowlist entr(ies) rejected (not E.164)")
        if match is True:
            parts.append(f"webhook subscribed at {c.webhook_url}")
        elif match is False:
            problems.append(f"no active message.received subscription for {c.webhook_url}")
        else:
            parts.append("webhook subscription could not be listed")
        if self.last_live_confirmation_at:
            parts.append(f"last live reply {self.last_live_confirmation_at:%H:%M:%S} UTC")
        else:
            parts.append("no live reply verified yet")
        if problems:
            return "error", "; ".join(parts + ["missing: " + ", ".join(problems)])
        return "ok", "; ".join(parts)

    # --- outbound ---------------------------------------------------------------------
    def prepare(self, run: Run, request_id: str) -> list[OutreachRecord]:
        """Create one outreach record per opted-in phone for a newly approved request.
        Returns [] (and sends nothing) unless live sending is configured."""
        if not self.config.can_send or self.client is None:
            return []
        request = run.staffing_requests[request_id]
        name = run.network.get(request.hospital_id).short_name
        body = outreach_message(name)
        now = self.clock()
        records = [
            OutreachRecord(
                outreach_id=f"out-{uuid.uuid4().hex[:10]}",
                run_id=run.run_id,
                request_id=request_id,
                phone=phone,
                hospital_id=request.hospital_id,
                body=body,
                created_at=now,
            )
            for phone in self.config.allowed_phones
        ]
        with self._lock:
            for r in records:
                self._records[r.outreach_id] = r
        return records

    def deliver(self, records: list[OutreachRecord]) -> list[OutreachRecord]:
        """Send prepared messages (blocking; run in a worker thread)."""
        for r in records:
            assert self.client is not None
            result = self.client.send(r.phone, r.body)
            with self._lock:
                r.send_ms = result.latency_ms
                if result.ok:
                    r.status, r.sent_at = "sent", self.clock()
                    r.message_id, r.lead_id = result.message_id, result.lead_id
                else:
                    r.status, r.error = "failed", result.error
            log.info(
                "outreach send",
                extra={"outreach_id": r.outreach_id, "ok": result.ok, "ms": result.latency_ms,
                       "recipient": r.masked, "error": result.error},
            )  # fmt: skip
        return records

    def records_for(self, request_id: str) -> list[OutreachRecord]:
        with self._lock:
            return [r for r in self._records.values() if r.request_id == request_id]

    # --- inbound ----------------------------------------------------------------------
    def handle_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> WebhookOutcome:
        started = time.perf_counter()
        outcome = self._handle(raw_body, headers)
        outcome.processing_ms = round((time.perf_counter() - started) * 1000, 2)
        with self._lock:
            self.counters[outcome.outcome] = self.counters.get(outcome.outcome, 0) + 1
            self.last_webhook = outcome
        log.info(
            "chert webhook",
            extra={"outcome": outcome.outcome, "event_id": outcome.event_id,
                   "http_status": outcome.http_status, "ms": outcome.processing_ms},
        )  # fmt: skip
        return outcome

    def _handle(self, raw_body: bytes, headers: Mapping[str, str]) -> WebhookOutcome:
        secret = self.config.webhook_secret
        if not secret:
            return WebhookOutcome(503, "not_configured", "CHERT_WEBHOOK_SECRET is not set")
        try:
            verify_webhook_signature(raw_body, headers, secret)
        except SignatureError as exc:
            return WebhookOutcome(401, "invalid_signature", str(exc))
        try:
            event = parse_event(json.loads(raw_body))
        except (ValueError, TypeError) as exc:
            return WebhookOutcome(400, "malformed", str(exc)[:200], signature_valid=True)

        with self._lock:  # one delivery at a time: dedup and confirmation are atomic
            if event.event_id in self._seen:
                previous = self._seen[event.event_id]
                return self._done(event, 200, "duplicate_event", f"already processed ({previous})")
            result = self._process(event)
            self._seen[event.event_id] = result.outcome
            while len(self._seen) > SEEN_EVENTS_MAX:
                self._seen.popitem(last=False)
            return result

    def _done(self, event: InboundEvent, status: int, outcome: str, detail: str, *,
              record: OutreachRecord | None = None,
              run: Run | None = None) -> WebhookOutcome:  # fmt: skip
        sender = normalize_phone(event.sender) if event.sender else None
        return WebhookOutcome(
            status, outcome, detail, event.event_id, event.event_type, run, record,
            phone_hash(sender) if sender else None, True,
        )  # fmt: skip

    def _process(self, event: InboundEvent) -> WebhookOutcome:
        if event.event_type != "message.received":
            return self._done(event, 200, "ignored_event_type", event.event_type)
        if event.direction not in (None, "inbound") or event.is_me:
            return self._done(event, 200, "ignored_outbound", "not an inbound reply")

        record = self._correlate(event)
        if record is None:
            return self._done(event, 200, "unknown_sender",
                              "no outreach was sent to this sender/chat")  # fmt: skip
        active = self.demo.active_run()
        if record.run_id != active.run_id:
            return self._done(event, 200, "stale_request", "reply to a previous demo run",
                              record=record)  # fmt: skip
        if record.status == "confirmed":
            return self._done(event, 200, "already_confirmed", "recipient already confirmed",
                              record=record)  # fmt: skip
        if event.text.strip().casefold() != CONFIRM_REPLY:
            return self._done(event, 200, "not_confirmation_text", "only 'IN' confirms",
                              record=record)  # fmt: skip
        try:
            run, _ = self.demo.confirm_live(
                record.run_id, record.request_id, record.staff_id, record.display_name, "IN"
            )
        except (ConflictError, NotFoundError) as exc:
            return self._done(event, 200, "rejected_request", str(exc), record=record)
        record.status, record.confirmed_at = "confirmed", self.clock()
        self.last_live_confirmation_at = record.confirmed_at
        return self._done(event, 200, "confirmed", f"{record.display_name} confirmed",
                          record=record, run=run)  # fmt: skip

    def _correlate(self, event: InboundEvent) -> OutreachRecord | None:
        """Match a reply to a message we actually sent: Chert's chat id (our send's lead_id)
        first, then the sender's number against the opted-in allowlist."""
        sent = [r for r in self._records.values() if r.status in ("sent", "confirmed")]
        sent.sort(key=lambda r: r.created_at, reverse=True)
        if event.chat_id:
            for r in sent:
                if r.lead_id and r.lead_id == event.chat_id:
                    return r
        sender = normalize_phone(event.sender)
        if sender and sender in self.config.allowed_phones:
            for r in sent:
                if r.phone == sender:
                    return r
        return None
