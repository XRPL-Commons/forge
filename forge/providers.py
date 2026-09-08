"""Availability-aware stage alternation; policy/task failures never become outages."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import time

from .state import AtomicStore, PROVIDERS, ROLES


@dataclass(frozen=True)
class Routing:
    provider: str | None
    degraded: bool
    reason: str
    retry_after: float | None = None


def opposite(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError("unknown provider")
    return "claude" if provider == "codex" else "codex"


class ProviderRouter:
    def __init__(self, state_root: str | Path, *, preferred_builder: str = "codex",
                 accounts: dict[str, str] | None = None,
                 available: set[str] | None = None, cooldown: float = 900,
                 transient_delay: float = 15):
        if preferred_builder not in PROVIDERS or cooldown <= 0 or transient_delay <= 0:
            raise ValueError("invalid provider configuration")
        self.preferred = preferred_builder
        self.accounts = accounts or {p: "default" for p in PROVIDERS}
        if set(self.accounts) != PROVIDERS or any(not a for a in self.accounts.values()):
            raise ValueError("configure an account identity for each provider")
        self.available = PROVIDERS if available is None else set(available)
        if not self.available <= PROVIDERS:
            raise ValueError("unknown available provider")
        self.cooldown = cooldown
        self.transient_delay = transient_delay
        self.store = AtomicStore(state_root, "providers")

    def _key(self, provider):
        if provider not in PROVIDERS:
            raise ValueError("unknown provider")
        return provider + ":" + self.accounts[provider]

    def choose(self, role: str, *, successful_builder: str | None = None,
               successful_reviewer: str | None = None, now: float | None = None) -> Routing:
        if role not in ROLES:
            raise ValueError("unknown role")
        now = time.time() if now is None else now
        preferred = self.preferred
        if role == "reviewer":
            preferred = opposite(successful_builder or self.preferred)
        elif role == "auditor":
            preferred = opposite(successful_reviewer or opposite(successful_builder or self.preferred))
        with self.store.transaction() as data:
            circuits = data.get("circuits", {})
            healthy = [p for p in PROVIDERS if p in self.available
                       and circuits.get(self._key(p), {}).get("retry_after", 0) <= now]
            if preferred in healthy:
                return Routing(preferred, len(healthy) < 2, "preferred stage provider")
            alternate = opposite(preferred)
            if alternate in healthy:
                return Routing(alternate, True, "preferred provider unavailable")
            retry = [circuits.get(self._key(p), {}).get("retry_after", 0)
                     for p in self.available]
            return Routing(None, True, "no provider available", min(retry) if retry else None)

    def record_failure(self, provider: str, failure_class: str, *, now: float | None = None):
        key = self._key(provider)
        # No provider-level circuit for permission, audit findings, tests, or host problems.
        if failure_class not in {"blocked_auth", "blocked_quota", "failed_retryable"}:
            return
        now = time.time() if now is None else now
        with self.store.transaction() as data:
            circuit = data.setdefault("circuits", {}).setdefault(key, {})
            count = circuit.get("failures", 0) + 1
            delay = self.cooldown if failure_class != "failed_retryable" else min(
                self.cooldown, self.transient_delay * 2 ** min(count - 1, 16))
            circuit.update(failures=count, failure_class=failure_class,
                           retry_after=now + delay, last_failure=now)

    def record_success(self, provider: str, *, attempt_started_at: float) -> bool:
        """Only a successful attempt started after an outage can close its circuit.

        The timestamp is supplied by the trusted attempt ledger, never by model
        output. An older concurrent task succeeding is not an account-health probe.
        """
        if type(attempt_started_at) not in (int, float) or not math.isfinite(attempt_started_at):
            raise ValueError("finite trusted attempt start timestamp required")
        with self.store.transaction() as data:
            circuits = data.setdefault("circuits", {})
            key = self._key(provider)
            circuit = circuits.get(key)
            if circuit is not None and attempt_started_at <= circuit.get("last_failure", 0):
                return False
            circuits.pop(key, None)
            return True

    def reserve_probe(self, provider: str, *, now: float | None = None,
                      lease: float = 60) -> bool:
        """Only one eligible half-open probe across controller processes."""
        now = time.time() if now is None else now
        if lease <= 0:
            raise ValueError("positive probe lease required")
        with self.store.transaction() as data:
            circuit = data.setdefault("circuits", {}).get(self._key(provider))
            if circuit is None:
                return True
            if circuit.get("retry_after", 0) > now:
                return False
            circuit["retry_after"] = now + lease
            return True
