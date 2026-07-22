"""Real API keys for the HTTP transport (P3 Stage 5) — hashing, scopes, rate limits.

``../Rag/reference/reference-security-model.md``: *"bearer keys, argon2 at rest,
per-project scopes, read vs write vs admin scopes"*. Stage 4 shipped the cheapest thing
that satisfied "the HTTP transport always requires a key" — one shared secret, compared
with :func:`secrets.compare_digest` — precisely so this stage could replace its *body*
without moving the enforcement point (``api/deps.py``'s ``require_key``/``RequiresKey``
stay wired into every route the same way). This module is the real model that grows there:
every key is its own row, hashed at rest, scoped per project, rate-limited independently.

**Key format is ``id.secret``, not one opaque token.** Argon2 is deliberately slow (that
is what makes it a real password hash rather than a fast digest an attacker can brute-force
offline), so authenticating a request by re-hashing the presented secret and comparing it
against *every* stored key does not scale past a handful of rows — the constant per-check
cost the reference note actually asks for is O(1) key lookup, not O(n) re-hashing. Splitting
the raw key into a public, non-secret ``id`` (an indexed lookup) and a secret half (the
only thing argon2 ever sees) gives that: one PK read, then one hash verification, same as
how most providers with hashed-at-rest keys structure theirs (Stripe's ``sk_live_…`` prefix
is the same idea in spirit, just not a delimited id).

**Enforcement is two questions, asked in order, both at the transport (not the service):**
authentication ("is this a key this server minted and has not revoked") and authorization
("does it hold at least this level on the project this route names"). ``api/deps.py``
composes them as two dependencies — :func:`require_key` for the first,
``require_scope(level)`` for the second — so a route that forgets to add a scope
dependency is still authenticated by the router-wide one, and the two failure modes render
as 401 vs 403 rather than being conflated into one "no" (RFC 9110 draws exactly that line:
401 means re-authenticate might help, 403 means it would not).

Rate limiting is a per-key token bucket (:class:`TokenBucket`), because a fixed window lets
a key burst its whole quota at the window boundary and then again immediately after — a
token bucket smooths that into a genuine sustained rate. Buckets live in process memory,
keyed by key id, and do not survive a restart; that is a feature, not a gap, for a
single-box deployment where a restart is already a discontinuity in everything else being
measured (search latency, ingest lag).
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from vaultrag.logging import get_logger
from vaultrag.metrics import record_key_request
from vaultrag.store.base import ApiKeyRow

log = get_logger(__name__)

__all__ = [
    "AuthenticatedKey",
    "InsufficientScope",
    "KeyAuthenticator",
    "KeyMaterial",
    "KeyStore",
    "RateLimited",
    "Scope",
    "TokenBucket",
    "Unauthorized",
    "generate_key",
    "hash_secret",
    "parse_presented_key",
    "scope_at_least",
    "verify_secret",
]

#: Bytes of entropy in a minted secret. 32 bytes -> 43 url-safe characters — the same
#: sizing Stage 4's bootstrap key used, past any brute-force concern for a loopback-bound
#: port and still one copy-paste.
_SECRET_BYTES = 32
#: Bytes of entropy in a minted id. Not a secret (argon2 never sees it, and it is safe to
#: log/print/list) — just needs to be practically collision-free across the keys one
#: deployment mints; 9 bytes -> 12 url-safe characters is generous for that.
_ID_BYTES = 9
_SEP = "."

#: Default requests/minute for a newly minted key when the operator does not name one.
DEFAULT_RATE_LIMIT = 60


class Scope(StrEnum):
    """Per-project capability a key can hold, ordered read < write < admin.

    Each level implies every level below it (an admin key can also read and write) —
    :func:`scope_at_least` is the one place that ordering is stated, so nothing else has to
    re-derive it from enum member order.
    """

    read = "read"
    write = "write"
    admin = "admin"


_SCOPE_RANK: dict[Scope, int] = {Scope.read: 1, Scope.write: 2, Scope.admin: 3}


def scope_at_least(granted: Scope, required: Scope) -> bool:
    """Whether ``granted`` covers ``required`` under the read < write < admin ordering."""
    return _SCOPE_RANK[granted] >= _SCOPE_RANK[required]


# ---------------------------------------------------------------------------
# Exceptions. Deliberately not vaultrag.service.ServiceError subclasses: these are
# transport-layer refusals (the request never reached the service), so they render
# through their own handlers in api/deps.py rather than api/errors.py's service mapping —
# the same split Stage 4's Unauthorized already drew, extended to the two new cases.
# ---------------------------------------------------------------------------
class Unauthorized(Exception):
    """No usable bearer key was presented, or it does not match a live one. → 401."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class InsufficientScope(Exception):
    """The key is real, but does not hold the level this route requires. → 403."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class RateLimited(Exception):
    """The key's token bucket is empty. → 429, with ``Retry-After``."""

    def __init__(self, detail: str, *, retry_after: float) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Key material: mint, hash, verify.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class KeyMaterial:
    """A freshly minted key. ``raw`` is shown to the operator exactly once — nothing on
    this process, including the store it is about to be written to, holds it again."""

    id: str
    secret: str

    @property
    def raw(self) -> str:
        return f"{self.id}{_SEP}{self.secret}"


def generate_key() -> KeyMaterial:
    """Mint a new ``id``/``secret`` pair. The id is not secret; the secret is what gets
    hashed."""
    return KeyMaterial(
        id=secrets.token_urlsafe(_ID_BYTES), secret=secrets.token_urlsafe(_SECRET_BYTES)
    )


def parse_presented_key(raw: str) -> tuple[str, str] | None:
    """Split a presented ``Authorization: Bearer`` value into ``(id, secret)``.

    ``None`` on anything that is not exactly one ``.``-delimited id and a non-empty
    secret — a malformed key is indistinguishable from a wrong one at this layer, and both
    resolve to the same 401.
    """
    key_id, sep, secret = raw.partition(_SEP)
    if not sep or not key_id or not secret:
        return None
    return key_id, secret


_hasher = PasswordHasher()


def hash_secret(secret: str) -> str:
    """Argon2id-hash a key's secret half for storage."""
    return _hasher.hash(secret)


def verify_secret(secret: str, key_hash: str) -> bool:
    """Constant-time (argon2's own) check of a presented secret against its stored hash.

    Catches ``InvalidHashError`` alongside the ordinary mismatch: a corrupted or
    hand-edited ``key_hash`` column should read as "this key doesn't verify" (401),
    not crash the request that happened to try it.
    """
    try:
        _hasher.verify(key_hash, secret)
    except (VerificationError, InvalidHashError):
        return False
    return True


# ---------------------------------------------------------------------------
# Rate limiting.
# ---------------------------------------------------------------------------
class TokenBucket:
    """A classic token bucket: ``capacity`` tokens, refilled continuously at
    ``refill_per_second``, one token spent per :meth:`take`.

    Continuous refill (not "reset every N seconds") is what keeps a sustained rate honest
    across a window boundary — a fixed window lets a caller spend a whole minute's quota in
    its last second and the next minute's in its first, i.e. 2x the intended rate for one
    instant. ``now`` is injectable so tests can advance time deterministically instead of
    sleeping.
    """

    def __init__(
        self,
        capacity: float,
        refill_per_second: float,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._tokens = capacity
        self._refill = refill_per_second
        self._now = now
        self._updated = now()

    def _refill_tokens(self) -> None:
        current = self._now()
        elapsed = max(0.0, current - self._updated)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill)
        self._updated = current

    def take(self) -> bool:
        """Spend one token if available. ``False`` means the caller is over its rate."""
        self._refill_tokens()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    def retry_after(self) -> float:
        """Seconds until :meth:`take` would next succeed (0 if it would now)."""
        self._refill_tokens()
        deficit = 1 - self._tokens
        if deficit <= 0:
            return 0.0
        if self._refill <= 0:
            return float("inf")
        return deficit / self._refill


# ---------------------------------------------------------------------------
# The authenticator.
# ---------------------------------------------------------------------------
@runtime_checkable
class KeyStore(Protocol):
    """The slice of :class:`~vaultrag.store.sqlite.SQLiteMetaStore` auth needs — the same
    narrow-protocol move as :class:`~vaultrag.service.OverviewStore`, so a test can hand
    :class:`KeyAuthenticator` a fake instead of a real database."""

    def create_api_key(
        self,
        *,
        key_id: str,
        name: str | None,
        key_hash: str,
        scopes: Mapping[str, str],
        rate_limit: int,
    ) -> None: ...

    def get_api_key(self, key_id: str) -> ApiKeyRow | None: ...

    def list_api_keys(self) -> list[ApiKeyRow]: ...

    def delete_api_key(self, key_id: str) -> bool: ...

    def touch_api_key(self, key_id: str, *, used_ts: int) -> None: ...


@dataclass(frozen=True, slots=True)
class AuthenticatedKey:
    """What a route sees once a request's key has checked out — never the raw key or its
    hash, only what it is allowed to do."""

    id: str
    name: str | None
    scopes: dict[str, Scope]

    def scope_for(self, project: str) -> Scope | None:
        return self.scopes.get(project)

    def has_any(self, level: Scope) -> bool:
        """Whether this key holds ``level`` (or higher) on *any* project — the check for a
        route that names no project in its path (``GET/POST /v1/projects``)."""
        return any(scope_at_least(s, level) for s in self.scopes.values())


class KeyAuthenticator:
    """Turns a presented bearer value into an :class:`AuthenticatedKey`, or refuses.

    Built once per ``serve`` process around the shared metadata store, so revocation and
    rate-limit state are consistent across every request the process handles — there is
    exactly one place a key's standing is decided, the same "one enforcement point" shape
    as :meth:`~vaultrag.service.VaultRAGService.select`.
    """

    def __init__(
        self,
        store: KeyStore,
        *,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._now = now
        self._monotonic = monotonic
        self._buckets: dict[str, TokenBucket] = {}
        self._buckets_lock = threading.Lock()

    def authenticate(self, raw: str) -> AuthenticatedKey:
        """Validate a presented ``id.secret`` value end to end.

        Order: parse -> look up by id (indexed, cheap) -> verify the secret (the one
        argon2-priced step, paid at most once per request) -> rate limit -> stamp
        ``last_used``. Every failure short-circuits to the same class of exception a
        transport already knows how to render; none of them echo what was presented.
        """
        parsed = parse_presented_key(raw)
        if parsed is None:
            raise Unauthorized("authorization must be `Bearer <key>`")
        key_id, secret = parsed
        row = self._store.get_api_key(key_id)
        if row is None or not verify_secret(secret, row.key_hash):
            # Deliberately the same message and the same "no such row vs wrong secret"
            # branch collapse as a wrong id: distinguishing them would let a caller probe
            # for which ids exist.
            log.warning("auth.rejected", key_id=key_id)
            raise Unauthorized("invalid API key")
        self._store.touch_api_key(row.id, used_ts=int(self._now()))
        if row.rate_limit > 0:
            bucket = self._bucket_for(row)
            if not bucket.take():
                raise RateLimited(
                    f"key {row.id!r} exceeded its rate limit ({row.rate_limit}/min)",
                    retry_after=bucket.retry_after(),
                )
        record_key_request(key_id=row.id)
        return AuthenticatedKey(id=row.id, name=row.name, scopes=_parse_scopes(row))

    def _bucket_for(self, row: ApiKeyRow) -> TokenBucket:
        with self._buckets_lock:
            bucket = self._buckets.get(row.id)
            if bucket is None:
                bucket = TokenBucket(
                    capacity=row.rate_limit,
                    refill_per_second=row.rate_limit / 60.0,
                    now=self._monotonic,
                )
                self._buckets[row.id] = bucket
            return bucket

    def bootstrap_if_empty(
        self, project_ids: Sequence[str], *, rate_limit: int = DEFAULT_RATE_LIMIT
    ) -> str | None:
        """Mint one admin-everywhere key iff the store holds none yet.

        The Stage-4 property carried forward: the HTTP transport is never keyless, but a
        deployment with no keys yet is a bootstrapping gap, not a reason to refuse to
        start. ``vaultrag key add`` remains the CLI's own path (talks to the store
        directly, no server or credential needed) — this only covers the very first boot,
        where there is otherwise no way to create the key that would let an operator call
        the API to create a key.
        """
        if self._store.list_api_keys():
            return None
        material = generate_key()
        self._store.create_api_key(
            key_id=material.id,
            name="bootstrap",
            key_hash=hash_secret(material.secret),
            scopes=dict.fromkeys(project_ids, Scope.admin.value),
            rate_limit=rate_limit,
        )
        log.info("auth.bootstrapped", key_id=material.id, projects=list(project_ids))
        return material.raw


def _parse_scopes(row: ApiKeyRow) -> dict[str, Scope]:
    """``ApiKeyRow.scopes`` values are free-form JSON strings; only known levels survive.

    Defensive rather than trusting: a hand-edited database (or a future downgrade) could
    hold a level this build does not recognise, and the fail-safe direction is "this key
    can't reach that project" — silently dropping an unreadable scope entry rather than
    raising and refusing the whole key for an unrelated project's bad row.
    """
    parsed: dict[str, Scope] = {}
    for project_id, level in row.scopes.items():
        try:
            parsed[project_id] = Scope(level)
        except ValueError:
            log.warning("auth.unknown_scope_level", key_id=row.id, project=project_id, level=level)
    return parsed
