"""P3 Stage 5 verify: hashing, scopes, rate limits — the domain module, not the transport.

``test_api.py`` covers the transport wiring (which route needs which level, tenancy still
refuses the same way); this file covers what :mod:`vaultrag.auth` guarantees on its own:
a wrong secret never authenticates, a revoked key never authenticates again, a corrupted
hash fails closed, and a token bucket actually limits and recovers on schedule.
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeKeyStore
from vaultrag.auth import (
    AuthenticatedKey,
    KeyAuthenticator,
    RateLimited,
    Scope,
    TokenBucket,
    Unauthorized,
    generate_key,
    hash_secret,
    parse_presented_key,
    scope_at_least,
    verify_secret,
)


# ---------------------------------------------------------------------------
# Scope ordering
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("granted", "required", "expected"),
    [
        (Scope.read, Scope.read, True),
        (Scope.read, Scope.write, False),
        (Scope.read, Scope.admin, False),
        (Scope.write, Scope.read, True),
        (Scope.write, Scope.write, True),
        (Scope.write, Scope.admin, False),
        (Scope.admin, Scope.read, True),
        (Scope.admin, Scope.write, True),
        (Scope.admin, Scope.admin, True),
    ],
)
def test_scope_ordering_is_read_write_admin(
    granted: Scope, required: Scope, expected: bool
) -> None:
    """Each level implies every level below it — the whole enforcement model rests on this
    one relation being right in every direction."""
    assert scope_at_least(granted, required) is expected


def test_authenticated_key_has_any_checks_every_project() -> None:
    key = AuthenticatedKey(id="k", name=None, scopes={"a": Scope.read, "b": Scope.admin})
    assert key.has_any(Scope.read)
    assert key.has_any(Scope.admin)  # b covers it
    assert not AuthenticatedKey(id="k", name=None, scopes={"a": Scope.read}).has_any(Scope.admin)
    assert not AuthenticatedKey(id="k", name=None, scopes={}).has_any(Scope.read)


# ---------------------------------------------------------------------------
# Key material: mint, parse, hash, verify
# ---------------------------------------------------------------------------
def test_generate_key_round_trips_through_parse_presented_key() -> None:
    material = generate_key()
    assert material.raw == f"{material.id}.{material.secret}"
    parsed = parse_presented_key(material.raw)
    assert parsed == (material.id, material.secret)


@pytest.mark.parametrize(
    "raw",
    ["no-dot-at-all", "", ".secret-with-no-id", "id-with-no-secret.", "."],
)
def test_parse_presented_key_rejects_malformed_input(raw: str) -> None:
    assert parse_presented_key(raw) is None


def test_hash_and_verify_round_trip() -> None:
    hashed = hash_secret("correct-secret")
    assert verify_secret("correct-secret", hashed) is True
    assert verify_secret("wrong-secret", hashed) is False


def test_verify_secret_is_defensive_against_a_corrupted_hash() -> None:
    """A hand-edited or truncated ``key_hash`` column must fail closed (401), not crash the
    request that happened to try it."""
    assert verify_secret("anything", "not-a-real-argon2-hash") is False


def test_a_hash_never_contains_the_raw_secret() -> None:
    secret = "super-secret-value-xyz"
    assert secret not in hash_secret(secret)


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------
def test_token_bucket_denies_once_capacity_is_spent_and_recovers_after_refill() -> None:
    clock = [0.0]
    bucket = TokenBucket(capacity=2, refill_per_second=1.0, now=lambda: clock[0])
    assert bucket.take() is True
    assert bucket.take() is True
    assert bucket.take() is False  # capacity exhausted, no time has passed

    clock[0] += 0.5  # half a token back
    assert bucket.take() is False
    clock[0] += 0.5  # one full token back (1.0s @ 1/s)
    assert bucket.take() is True


def test_token_bucket_never_exceeds_capacity_even_after_a_long_idle_gap() -> None:
    clock = [0.0]
    bucket = TokenBucket(capacity=3, refill_per_second=1.0, now=lambda: clock[0])
    clock[0] += 1000.0  # idle for a long time
    assert bucket.take() is True
    assert bucket.take() is True
    assert bucket.take() is True
    assert bucket.take() is False  # capacity caps the refill, it doesn't bank forever


def test_retry_after_is_zero_when_a_token_is_available() -> None:
    bucket = TokenBucket(capacity=1, refill_per_second=1.0, now=lambda: 0.0)
    assert bucket.retry_after() == 0.0


def test_retry_after_reports_the_wait_for_the_next_token() -> None:
    clock = [0.0]
    bucket = TokenBucket(capacity=1, refill_per_second=2.0, now=lambda: clock[0])  # 1 per 0.5s
    assert bucket.take() is True
    assert bucket.retry_after() == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# KeyAuthenticator
# ---------------------------------------------------------------------------
def _seed(
    store: FakeKeyStore,
    *,
    key_id: str = "kid",
    secret: str = "shh",
    scopes: dict[str, str] | None = None,
    rate_limit: int = 1_000_000,
) -> None:
    store.create_api_key(
        key_id=key_id,
        name="test",
        key_hash=hash_secret(secret),
        scopes=scopes if scopes is not None else {"proj": "read"},
        rate_limit=rate_limit,
    )


def test_authenticate_accepts_a_valid_key_and_returns_its_scopes() -> None:
    store = FakeKeyStore()
    _seed(store, scopes={"proj": "write"})
    key = KeyAuthenticator(store).authenticate("kid.shh")
    assert key.id == "kid"
    assert key.scope_for("proj") == Scope.write
    assert key.scope_for("other") is None


def test_authenticate_rejects_an_unknown_key_id() -> None:
    store = FakeKeyStore()
    _seed(store)
    with pytest.raises(Unauthorized):
        KeyAuthenticator(store).authenticate("no-such-id.shh")


def test_authenticate_rejects_a_wrong_secret() -> None:
    store = FakeKeyStore()
    _seed(store)
    with pytest.raises(Unauthorized):
        KeyAuthenticator(store).authenticate("kid.wrong-secret")


def test_authenticate_rejects_a_malformed_presented_key() -> None:
    store = FakeKeyStore()
    _seed(store)
    with pytest.raises(Unauthorized):
        KeyAuthenticator(store).authenticate("no-dot-here")


def test_authenticate_stamps_last_used() -> None:
    store = FakeKeyStore()
    _seed(store)
    clock = [1234.0]
    KeyAuthenticator(store, now=lambda: clock[0]).authenticate("kid.shh")
    row = store.get_api_key("kid")
    assert row is not None
    assert row.last_used == 1234


def test_a_revoked_key_is_rejected_on_its_very_next_authenticate_call() -> None:
    """Revocation is a DELETE, not a status flag — the next lookup simply finds nothing,
    which is what makes it take effect immediately rather than at the next restart."""
    store = FakeKeyStore()
    _seed(store)
    authenticator = KeyAuthenticator(store)
    authenticator.authenticate("kid.shh")  # works before revocation
    assert store.delete_api_key("kid") is True
    with pytest.raises(Unauthorized):
        authenticator.authenticate("kid.shh")


def test_rate_limit_exceeded_raises_rate_limited_and_recovers_on_schedule() -> None:
    store = FakeKeyStore()
    _seed(store, rate_limit=2)  # 2/minute -> refills 1 every 30s
    clock = [0.0]
    authenticator = KeyAuthenticator(store, monotonic=lambda: clock[0])
    authenticator.authenticate("kid.shh")
    authenticator.authenticate("kid.shh")
    with pytest.raises(RateLimited) as exc_info:
        authenticator.authenticate("kid.shh")
    assert exc_info.value.retry_after == pytest.approx(30.0, abs=0.1)

    clock[0] += 30.0  # exactly one token back
    authenticator.authenticate("kid.shh")  # no longer raises


def test_rate_limit_zero_means_unlimited() -> None:
    store = FakeKeyStore()
    _seed(store, rate_limit=0)
    authenticator = KeyAuthenticator(store)
    for _ in range(50):
        authenticator.authenticate("kid.shh")  # never raises RateLimited


def test_two_keys_have_independent_rate_limits() -> None:
    store = FakeKeyStore()
    _seed(store, key_id="a", secret="sa", rate_limit=1)
    _seed(store, key_id="b", secret="sb", rate_limit=1)
    clock = [0.0]
    authenticator = KeyAuthenticator(store, monotonic=lambda: clock[0])
    authenticator.authenticate("a.sa")
    authenticator.authenticate("b.sb")  # a's exhausted bucket must not affect b
    with pytest.raises(RateLimited):
        authenticator.authenticate("a.sa")


def test_an_unrecognised_stored_scope_level_is_dropped_not_fatal() -> None:
    """Defensive against a hand-edited or future-downgraded database: an unreadable scope
    entry should shrink what the key can reach, never crash the whole authentication."""
    store = FakeKeyStore()
    _seed(store, scopes={"proj": "read", "corrupted": "superuser"})
    key = KeyAuthenticator(store).authenticate("kid.shh")
    assert key.scope_for("proj") == Scope.read
    assert key.scope_for("corrupted") is None


def test_bootstrap_if_empty_mints_once_and_is_idempotent() -> None:
    store = FakeKeyStore()
    authenticator = KeyAuthenticator(store)
    first = authenticator.bootstrap_if_empty(["proj-a", "proj-b"])
    assert first is not None
    second = authenticator.bootstrap_if_empty(["proj-a", "proj-b"])
    assert second is None  # the table is no longer empty; a second call must not mint again
    assert len(store.list_api_keys()) == 1


def test_bootstrap_if_empty_grants_admin_on_every_named_project() -> None:
    store = FakeKeyStore()
    authenticator = KeyAuthenticator(store)
    raw = authenticator.bootstrap_if_empty(["alpha", "beta"])
    assert raw is not None
    key = authenticator.authenticate(raw)
    assert key.scope_for("alpha") == Scope.admin
    assert key.scope_for("beta") == Scope.admin
