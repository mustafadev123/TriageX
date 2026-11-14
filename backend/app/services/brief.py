"""Grounded operational brief (handoff Prompt 6, docs/plan.md Section F).

Every number in a brief comes from the dashboard state the API already serves
(build_dashboard), as it is visible at the requested playhead tick: nothing after the playhead
is included (spoiler rule), and the demo-window result only once the playhead reaches the end
of the window.

Grounding: the model never writes a number. It receives a facts JSON (metric_key -> value and
label) and writes text with {metric_key} placeholders; the server substitutes the values.
Any digit left in the model's text, or a placeholder that is not a fact, rejects the output.

Providers (Gemini API free tier, the same key as the FaceTime agent; no paid provider):
primary GEMINI_MODEL (default gemini-3.1-flash-lite), fallback GEMINI_FALLBACK_MODEL (default
gemini-3.5-flash-lite). Per-minute quotas are per model, so neither competes with the agent's
card reader. Fallback on timeout (8 s), 429, 5xx, connection errors, or invalid structure after
one retry; auth/config errors are reported as "primary misconfigured". If both fail, a
deterministic template brief labeled "AI unavailable" is returned. Fallback is never silent.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import httpx

from app.models.dashboard import DashboardState

log = logging.getLogger("triagex.brief")

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_PRIMARY_MODEL = "gemini-3.1-flash-lite"
DEFAULT_FALLBACK_MODEL = "gemini-3.5-flash-lite"
TIMEOUT_S = 8.0
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
# Numbers spelled as words are numbers too ("one" is left out: "no one", "one of").
NUMBER_WORDS = re.compile(
    r"\b(zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundred|thousand|dozen|half)(?:-[a-z]+)?\b",
    re.IGNORECASE,
)

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "One sentence. Numbers only as {metric_key}.",
        },
        "bullets": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 4,
            "description": "Short situation bullets. Numbers only as {metric_key}.",
        },
        "action": {"type": "string", "description": "The next operator action, one sentence."},
    },
    "required": ["headline", "bullets", "action"],
    "additionalProperties": False,
}

SYSTEM = """You write a short operational brief for an emergency-department surge command center.
This is a hackathon demo: all data is synthetic and the hospitals are a simulation.
Use ONLY the facts provided. Never write a digit, and never spell a number as a word. Every
number, time, percentage or name that contains a number (such as the scenario name) must be
written as its placeholder in braces, exactly as the fact key, e.g. {focus_saturation_pct} or
{surge_name}. Do not invent
facts, causes, names or outcomes that are not in the facts. Do not predict results that are not in
the facts. Be calm and concrete. Headline: one sentence. Bullets: two to four short sentences.
Each sentence states facts exactly as their labels describe them; never combine two facts into
a claim the labels do not make (for example, a hospital's forecast is about that hospital only).
Action: restate {next_step} in one sentence; recommend nothing else (no diversions, transfers or
other measures that are not in the facts). If result_saved is among the facts, the headline must
state it ({result_saved}) and, if present, {result_pct}."""


# --- facts -------------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class Fact:
    value: str
    label: str


def _pct(x: float) -> str:
    return f"{x:.0f}%"


def build_facts(state: DashboardState, at_tick: int) -> tuple[int, dict[str, Fact]]:
    """Facts visible at `at_tick` (clamped to the demo window). Returns (tick, facts)."""
    w = state.curves.window
    t = max(w.start_tick, min(at_tick, w.end_tick - 1))
    last = w.end_tick - 1
    clocks = state.timeline.clocks
    focus_id = state.curves.focus_hospital_id
    focus = next(h for h in state.hospitals if h.id == focus_id)
    diverged = state.divergence is not None and t >= state.divergence.tick
    facts: dict[str, Fact] = {
        "clock": Fact(clocks[t], "simulated time now"),
        "focus_hospital": Fact(focus.short_name, "hospital under surge"),
    }

    def point(hid: str, track: str):
        series = next(s for s in state.series if s.hospital_id == hid)
        return getattr(series, track)[t]

    p = point(focus_id, "triagex" if diverged else "baseline")
    facts |= {
        "focus_saturation_pct": Fact(_pct(p.saturation_pct), f"{focus.short_name} occupancy now"),
        "focus_forecast_pct": Fact(
            _pct(p.forecast_saturation_pct),
            f"{focus.short_name}'s own forecast occupancy two hours from now",
        ),
        "focus_patients_waiting": Fact(
            str(p.queue_length), f"patients waiting to be seen at {focus.short_name}"
        ),
        "focus_ambulances_waiting": Fact(
            str(p.ambulances_waiting), f"ambulances waiting to offload at {focus.short_name}"
        ),
        "focus_staff_on_duty": Fact(str(p.staff_on_duty), f"staff on duty at {focus.short_name}"),
    }
    track = "triagex" if diverged else "baseline"
    breach = sum(1 for h in state.hospitals if point(h.id, track).status == "breach")
    facts["hospitals_in_breach"] = Fact(
        str(breach), "how many of the network's hospitals are over capacity right now"
    )
    facts["hospitals_total"] = Fact(str(len(state.hospitals)), "hospitals in the whole network")

    surge = state.run.scenario.surge_onset_tick
    if t >= surge:
        facts["surge_name"] = Fact(state.run.scenario.name, "scenario")
        facts["surge_start"] = Fact(clocks[surge], "surge start time")
    card = next((c for c in state.proposals if c.recommended), None)
    if card is not None and t >= card.tick:
        facts["proposal_staff"] = Fact(str(card.recommended_staff or 0), "staff TriageX proposes")
        facts["proposal_time"] = Fact(card.clock, "time the proposal was made")
    req = next((r for r in state.staffing_requests if r.approved_tick <= t), None)
    if req is not None:
        facts |= {
            "request_approved_at": Fact(req.approved_clock, "time the operator approved outreach"),
            "staff_requested": Fact(str(req.requested_staff), "staff requested"),
            "staff_confirmed": Fact(str(req.confirmed_staff), "staff confirmed"),
            "shift_start": Fact(req.effective_clock, "confirmed staff on the floor from"),
        }
        live = sum(1 for c in req.confirmations if c.source in ("facetime", "imessage", "webhook"))
        facts["staff_confirmed_live"] = Fact(str(live), "confirmations from live channels")
        facts["staff_confirmed_simulated"] = Fact(
            str(req.confirmed_staff - live), "confirmations from the labeled simulated fallback"
        )
    if diverged:
        cp = next(c for c in state.curves.points if c.tick == t)
        facts |= {
            "amb_hours_lost_without": Fact(
                f"{cp.baseline_ambulance_hours_cumulative:.2f} h",
                "ambulance-hours lost so far without TriageX",
            ),
            "amb_hours_lost_with": Fact(
                f"{cp.triagex_ambulance_hours_cumulative:.2f} h",
                "ambulance-hours lost so far with TriageX",
            ),
            "amb_hours_saved_so_far": Fact(
                f"{cp.ambulance_hours_saved_so_far:.2f} h", "ambulance-hours saved so far"
            ),
        }
    if t >= last and state.divergence is not None:  # the demo-window result, only at the end
        m = state.impact.primary
        facts |= {
            "result_lost_without": Fact(f"{m.baseline:.2f} h", f"{m.label} without TriageX"),
            "result_lost_with": Fact(f"{m.triagex:.2f} h", f"{m.label} with TriageX"),
            "result_saved": Fact(f"{abs(m.delta):.2f} h", f"{m.label} saved"),
        }
        if m.pct_change is not None:
            facts["result_pct"] = Fact(f"{abs(m.pct_change):.0f}%", "relative reduction")
        if state.impact.recovery.minutes_sooner:
            facts["recovery_minutes_sooner"] = Fact(
                f"{state.impact.recovery.minutes_sooner} min", "recovered sooner with TriageX"
            )
    facts["next_step"] = Fact(_next_step(facts), "the next operator action (decided by TriageX)")
    return t, facts


def _next_step(facts: dict[str, Fact]) -> str:
    """The next operator action, decided here (never by the model). Text only, no digits."""
    f = {k: v.value for k, v in facts.items()}
    if "staff_requested" not in f and "proposal_staff" in f:
        return f"approve the proposed staff outreach for {f['focus_hospital']}"
    if "staff_requested" in f and f["staff_confirmed"] != f["staff_requested"]:
        return "collect the remaining staff confirmations"
    if "result_saved" in f:
        return "review the outcome and reset the demo"
    return f"keep monitoring {f['focus_hospital']}"


# --- validation and rendering --------------------------------------------------------------
class InvalidBrief(ValueError):
    pass


def render(raw: dict, facts: dict[str, Fact]) -> tuple[dict, list[str]]:
    """Validate the model's structure and grounding, then substitute the fact values."""
    if not isinstance(raw, dict):
        raise InvalidBrief("not an object")
    headline, bullets, action = raw.get("headline"), raw.get("bullets"), raw.get("action")
    if (
        not isinstance(headline, str)
        or not isinstance(action, str)
        or not isinstance(bullets, list)
    ):
        raise InvalidBrief("missing headline, bullets or action")
    if not 1 <= len(bullets) <= 4 or not all(isinstance(b, str) and b.strip() for b in bullets):
        raise InvalidBrief("bullets must be 1 to 4 non-empty strings")
    cited: list[str] = []

    def one(text: str) -> str:
        keys = PLACEHOLDER.findall(text)
        unknown = [k for k in keys if k not in facts]
        if unknown:
            raise InvalidBrief(f"unknown metric_key {unknown[0]!r}")
        bare = PLACEHOLDER.sub("", text)
        # "two-hour" names the system's fixed forecast horizon (as the fact label does).
        bare = re.sub(r"\btwo[- ]hours?\b", "", bare, flags=re.IGNORECASE)
        if re.search(r"\d", bare) or NUMBER_WORDS.search(bare):
            raise InvalidBrief("a number was written without a placeholder")
        if "{" in PLACEHOLDER.sub("", text) or "}" in PLACEHOLDER.sub("", text):
            raise InvalidBrief("malformed placeholder")
        for k in keys:
            if k not in cited:
                cited.append(k)
        return PLACEHOLDER.sub(lambda m: facts[m.group(1)].value, text).strip()

    out = {
        "headline": one(headline),
        "bullets": [one(b) for b in bullets],
        "action": one(action),
    }
    return out, cited


def template(facts: dict[str, Fact]) -> tuple[dict, list[str]]:
    """Deterministic brief from the same facts (used when every AI provider failed)."""
    f = {k: v.value for k, v in facts.items()}
    bullets = [
        f"{f['focus_hospital']} at {f['focus_saturation_pct']} occupancy; "
        f"2-hour forecast {f['focus_forecast_pct']}."
    ]
    bullets.append(
        f"{f['hospitals_in_breach']} of {f['hospitals_total']} hospitals over capacity; "
        f"{f['focus_ambulances_waiting']} ambulances waiting at {f['focus_hospital']}."
    )
    if "staff_requested" in f:
        bullets.append(
            f"Outreach approved at {f['request_approved_at']}: {f['staff_confirmed']} of "
            f"{f['staff_requested']} staff confirmed; shift starts {f['shift_start']}."
        )
    if "result_saved" in f:
        bullets.append(
            f"Result: {f['result_saved']} of ambulance time saved "
            f"({f['result_lost_without']} → {f['result_lost_with']})."
        )
    elif "amb_hours_saved_so_far" in f:
        bullets.append(f"{f['amb_hours_saved_so_far']} of ambulance time saved so far.")
    action = f["next_step"][0].upper() + f["next_step"][1:] + "."
    headline = f"{f['clock']}: {f['focus_hospital']} at {f['focus_saturation_pct']} occupancy."
    if "result_saved" in f:
        headline = f"{f['clock']}: TriageX saved {f['result_saved']} of ambulance time."
    used = [k for k in facts if any(facts[k].value in s for s in (headline, action, *bullets))]
    return {"headline": headline, "bullets": bullets[:4], "action": action}, used


# --- providers ---------------------------------------------------------------------------
class ProviderError(Exception):
    """kind: timeout | rate_limited | server | connection | invalid | auth | config"""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


FALLBACK_KINDS = {"timeout", "rate_limited", "server", "connection", "invalid", "auth", "config"}
MISCONFIGURED = {"auth", "config"}


class AIProvider(Protocol):
    name: str
    model: str

    def generate(self, system: str, facts: dict, schema: dict) -> dict: ...


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = TIMEOUT_S,
    ):
        self.model = model
        self._client = httpx.Client(
            timeout=timeout_s, transport=transport, headers={"x-goog-api-key": api_key}
        )

    def generate(self, system: str, facts: dict, schema: dict) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [
                {"role": "user", "parts": [{"text": "Facts JSON:\n" + json.dumps(facts)}]}
            ],
            # No temperature override: Gemini 3 models were measured to stall at temperature 0.
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "maxOutputTokens": 800,
            },
        }
        try:
            resp = self._client.post(f"{GEMINI_API}/{self.model}:generateContent", json=body)
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("connection", type(exc).__name__) from exc
        if resp.status_code == 429:
            raise ProviderError("rate_limited", "HTTP 429")
        if resp.status_code >= 500:
            raise ProviderError("server", f"HTTP {resp.status_code}")
        if resp.status_code in (401, 403):
            raise ProviderError("auth", f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            msg = ""
            try:
                msg = resp.json().get("error", {}).get("message", "")
            except ValueError:
                pass
            kind = "auth" if "api key" in msg.lower() else "config"
            raise ProviderError(kind, f"HTTP {resp.status_code}: {msg[:160]}")
        try:
            parts = resp.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            return json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("invalid", "response was not the requested JSON") from exc


# --- service -----------------------------------------------------------------------------
@dataclass(slots=True)
class ProviderStatus:
    configured: bool
    model: str | None
    last_ok_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None


@dataclass(slots=True)
class Brief:
    brief_id: str
    run_id: str
    version: int
    at_tick: int
    clock: str
    provider: str  # gemini | gemini_fallback | template
    model: str | None
    latency_ms: float
    fallback_reason: str | None
    headline: str
    bullets: list[str]
    action: str
    metric_keys: list[str]
    facts: dict[str, str]
    generated_at: datetime
    notice: str = "Synthetic simulation data. Numbers are filled in by TriageX, not the model."
    attempts: list[str] = field(default_factory=list)


class BriefService:
    def __init__(
        self,
        primary: AIProvider | None,
        fallback: AIProvider | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.primary, self.fallback, self.clock = primary, fallback, clock
        self.status = {
            "primary": ProviderStatus(primary is not None, getattr(primary, "model", None)),
            "fallback": ProviderStatus(fallback is not None, getattr(fallback, "model", None)),
        }
        self._lock = threading.Lock()
        self._latest: dict[str, Brief] = {}

    def latest(self, run_id: str) -> Brief | None:
        with self._lock:
            return self._latest.get(run_id)

    def generate(self, state: DashboardState, at_tick: int) -> Brief:
        started = time.perf_counter()
        tick, facts = build_facts(state, at_tick)
        payload = {k: {"value": v.value, "label": v.label} for k, v in facts.items()}
        attempts: list[str] = []
        result = None
        for role, provider in (("primary", self.primary), ("fallback", self.fallback)):
            st = self.status[role]
            if provider is None:
                attempts.append(f"{role}: not configured")
                continue
            for attempt in (1, 2):
                try:
                    raw = provider.generate(SYSTEM, payload, BRIEF_SCHEMA)
                    content, cited = render(raw, facts)
                except (ProviderError, InvalidBrief) as exc:
                    kind = exc.kind if isinstance(exc, ProviderError) else "invalid"
                    if kind == "invalid" and attempt == 1:
                        attempts.append(f"{role} {provider.model}: invalid output, retrying")
                        continue  # invalid structure gets exactly one retry
                    st.last_error, st.last_error_at = kind, self.clock()
                    level = logging.ERROR if kind in MISCONFIGURED else logging.WARNING
                    log.log(
                        level,
                        "brief provider failed",
                        extra={"role": role, "model": provider.model, "kind": kind},
                    )
                    attempts.append(f"{role} {provider.model}: {kind}")
                    break
                st.last_ok_at, st.last_error = self.clock(), None
                label = "gemini" if role == "primary" else "gemini_fallback"
                result = (label, provider.model, content, cited)
                break
            if result:
                break
        if result is None:
            content, cited = template(facts)
            result = ("template", None, content, cited)
        provider_label, model, content, cited = result
        brief = Brief(
            brief_id=f"brf-{uuid.uuid4().hex[:10]}",
            run_id=state.run.run_id,
            version=state.run.version,
            at_tick=tick,
            clock=facts["clock"].value,
            provider=provider_label,
            model=model,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            fallback_reason="; ".join(a for a in attempts if "retrying" not in a) or None,
            headline=content["headline"],
            bullets=content["bullets"],
            action=content["action"],
            metric_keys=cited,
            facts={k: v.value for k, v in facts.items()},
            generated_at=self.clock(),
            attempts=attempts,
        )
        with self._lock:
            self._latest[state.run.run_id] = brief
        log.info("brief generated", extra={"provider": brief.provider, "ms": brief.latency_ms})
        return brief

    def health(self, role: str) -> tuple[str, str]:
        st = self.status[role]
        name = f"Gemini {st.model}" if st.model else "Gemini"
        if not st.configured:
            return "not_configured", "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set"
        if st.last_error in MISCONFIGURED:
            return "error", f"{role} misconfigured ({st.last_error}): {name}"
        if st.last_ok_at is None:
            suffix = f"; last attempt failed ({st.last_error})" if st.last_error else ""
            return "starting", f"{name}: no brief generated yet{suffix}"
        tail = f"; last attempt failed ({st.last_error})" if st.last_error else ""
        return "ok", f"{name}: last brief {st.last_ok_at:%H:%M:%S} UTC{tail}"


def brief_service_from_settings(settings) -> BriefService:
    key = settings.gemini_api_key or settings.google_api_key
    if not key:
        return BriefService(None, None)
    primary = GeminiProvider(key, settings.gemini_model or DEFAULT_PRIMARY_MODEL)
    fallback = GeminiProvider(key, settings.gemini_fallback_model or DEFAULT_FALLBACK_MODEL)
    return BriefService(primary, fallback)
