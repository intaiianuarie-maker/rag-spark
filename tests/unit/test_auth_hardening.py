"""T-R01 (P0) — shipped-default secrets and session-owner binding.

Three ticket metrics, each structural rather than asserted by hand:

* **shipped defaults accepted: 0** — both literals the repo has ever shipped
  (the code default and the ``docker-compose.yml`` default) are refused at
  settings construction in EVERY environment, including the one the old
  validator forgot.
* **cross-session forgery accepted: 0** — a live session plus a token whose
  ``sub`` is somebody else is rejected by ``app/authz/principal.py`` and by the
  refresh path; the matching pair still authenticates.
* **login/refresh/logout regressions: 0** — the existing auth suites stay green.

Scratch secrets only: every secret in this file is generated per-test or is a
deliberate literal under test (the shipped ones, which are the point).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

# A scratch secret for this process: >= 32 chars, not a shipped/placeholder value.
SCRATCH_SECRET = "t-r01-scratch-" + "9f4c1a7b2e8d6035" + "c41d9e7b3a55f208"
os.environ.setdefault("AUTH_JWT_SECRET", SCRATCH_SECRET)
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.unit

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
COMPOSE_PATH = os.path.join(REPO_ROOT, "docker-compose.yml")

EVERY_ENV = ["local", "test", "development", "dev", "staging", "production", "prod"]


# --------------------------------------------------------------------------- #
# Metric 1: shipped defaults accepted: 0, in every env
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("app_env", EVERY_ENV)
@pytest.mark.parametrize(
    "literal_name", ["DEV_JWT_SECRET", "COMPOSE_DEFAULT_JWT_SECRET"]
)
def test_both_shipped_literals_are_rejected_in_every_env(
    app_env: str, literal_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.auth.config import SHIPPED_DEFAULT_SECRETS, AuthSettings, signing_secret_problem
    from core.config import get_settings

    literal = {
        "DEV_JWT_SECRET": "dev-insecure-change-me",
        "COMPOSE_DEFAULT_JWT_SECRET": "dev-only-insecure-jwt-secret-change-me-0123456789",
    }[literal_name]
    # The two literals are the ones actually shipped (and both are still denied).
    assert literal in SHIPPED_DEFAULT_SECRETS

    assert signing_secret_problem(literal) != ""

    monkeypatch.setenv("APP_ENV", app_env)
    get_settings.cache_clear()
    try:
        # Pydantic wraps the ValueError subclass in its ValidationError, which
        # is itself a ValueError; the message must name the literal.
        with pytest.raises(ValueError) as exc:
            AuthSettings(jwt_secret=literal, _env_file=None)  # type: ignore[call-arg]
        assert literal in str(exc.value)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("app_env", EVERY_ENV)
def test_unset_secret_refuses_to_boot_in_every_env(
    app_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No code default survives: an absent secret is a boot refusal."""
    from app.auth.config import AuthSettings
    from core.config import get_settings

    monkeypatch.setenv("APP_ENV", app_env)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError):
            AuthSettings(jwt_secret="", _env_file=None)  # type: ignore[call-arg]
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "weak",
    ["changeme", "secret", "password", "change-me", "insecure", "SHORT", "x" * 31],
)
def test_weak_and_placeholder_secrets_are_refused(weak: str) -> None:
    from app.auth.config import signing_secret_problem

    assert signing_secret_problem(weak) != ""


def test_strong_scratch_secret_is_accepted() -> None:
    """No false positives: real secrets still boot."""
    from app.auth.config import signing_secret_problem

    assert signing_secret_problem(SCRATCH_SECRET) == ""
    assert signing_secret_problem("test-secret-not-for-production-0123456789") == ""
    assert signing_secret_problem("  " + SCRATCH_SECRET + "  ") == ""


def test_compose_no_longer_ships_a_default_secret() -> None:
    """The literal gap is closed at the source that created it."""
    with open(COMPOSE_PATH, encoding="utf-8") as handle:
        compose = handle.read()
    assert "dev-only-insecure-jwt-secret-change-me-0123456789" not in compose
    assert "AUTH_JWT_SECRET: ${AUTH_JWT_SECRET:-" not in compose
    # Compose itself fails closed when the variable is absent.
    assert "AUTH_JWT_SECRET: ${AUTH_JWT_SECRET:?" in compose


def test_no_shipped_literal_survives_as_an_auth_default() -> None:
    """Class check: the literals exist only in the denylist, never as defaults."""
    import inspect

    from app.auth import config

    source = inspect.getsource(config)
    field_default = "jwt_secret: str = \"\""
    assert field_default in source
    # Each literal appears exactly once in the module: in SHIPPED_DEFAULT_SECRETS.
    assert source.count('"dev-insecure-change-me"') == 1
    assert source.count("dev-only-insecure-jwt-secret-change-me-0123456789") == 1


# --------------------------------------------------------------------------- #
# Metric 2: cross-session forgery accepted: 0
# --------------------------------------------------------------------------- #


class _FakeRedis:
    """Enough of ``redis.asyncio.Redis`` for session + refresh state."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.calls: list[str] = []

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.calls.append(f"set {key}")
        self.store[key] = value

    async def get(self, key: str) -> str | None:
        self.calls.append(f"get {key}")
        return self.store.get(key)

    async def exists(self, key: str) -> int:
        self.calls.append(f"exists {key}")
        return 1 if key in self.store else 0

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            self.calls.append(f"delete {key}")
            if self.store.pop(key, None) is not None:
                removed += 1
            self.sets.pop(key, None)
        return removed

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def scan_iter(self, match: str = "*", count: int = 100):
        prefix = match[:-1] if match.endswith("*") else match
        for key in sorted(self.store):
            if key.startswith(prefix):
                yield key


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    from app.auth.adapters import redis_sessions, redis_store

    client = _FakeRedis()
    monkeypatch.setattr(redis_sessions, "get_redis", lambda: client)
    monkeypatch.setattr(redis_store, "get_redis", lambda: client)
    return client


def test_session_owner_is_bound_to_the_token_subject(
    fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live session + a foreign ``sub`` is a forgery and must be refused."""
    from app.auth.sessions import create_session, session_owner
    from app.auth.tokens import issue_access_token
    from app.authz import principal

    owner_id = uuid.uuid4()
    victim_id = uuid.uuid4()
    session_id = "session-under-test"
    asyncio.run(create_session(session_id, str(owner_id)))

    # The store knows who owns the session.
    assert asyncio.run(session_owner(session_id)) == str(owner_id)

    class _User:
        def __init__(self, uid: uuid.UUID) -> None:
            self.id = uid
            self.email = "u@example.com"
            self.is_active = True

    class _FakeDBSession:
        async def get(self, model: object, uid: uuid.UUID) -> _User:
            return _User(uid)

    class _Roles:
        async def all(self) -> list[object]:
            return []

        async def execute(self, *args: object, **kwargs: object) -> _Roles:
            return self

    db = _FakeDBSession()
    monkeypatch.setattr(principal, "_workspace_roles", lambda *a, **k: asyncio.sleep(0, result={}))

    # 1. The victim's subject riding another user's session: REFUSED.
    forged = issue_access_token(str(victim_id), session_id=session_id).token
    assert asyncio.run(principal._principal_from_token(db, forged)) is None

    # 2. The owner's own token: accepted.
    genuine = issue_access_token(str(owner_id), session_id=session_id).token
    resolved = asyncio.run(principal._principal_from_token(db, genuine))
    assert resolved is not None
    assert resolved.user_id == str(owner_id)

    # 3. A session whose record names no owner is not honoured either.
    fake_redis.store["auth:session:ownerless"] = "{}"
    ownerless = issue_access_token(str(owner_id), session_id="ownerless").token
    assert asyncio.run(principal._principal_from_token(db, ownerless)) is None

    # 4. A corrupt (unparseable) record is refused rather than treated as owned.
    fake_redis.store["auth:session:corrupt"] = "not-json"
    corrupt = issue_access_token(str(owner_id), session_id="corrupt").token
    assert asyncio.run(principal._principal_from_token(db, corrupt)) is None


def test_refresh_refuses_a_session_owned_by_someone_else(
    fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh path binds too — otherwise the forgery returns on refresh."""
    from app.auth import service as service_module
    from app.auth.sessions import create_session
    from app.auth.tokens import issue_refresh_token

    victim_id = str(uuid.uuid4())
    session_id = "victim-session"
    asyncio.run(create_session(session_id, victim_id))

    class _Store:
        class AuthStoreUnavailable(RuntimeError):
            pass

        @staticmethod
        async def get_refresh_token(jti: str) -> dict[str, str] | None:
            return {"fid": session_id, "user_id": victim_id}

        @staticmethod
        async def revoke_family(family_id: str) -> None:
            return None

    from app.auth.config import AuthSettings

    class _Service(service_module.AuthService):
        def __init__(self) -> None:
            self._settings = AuthSettings(jwt_secret=SCRATCH_SECRET, _env_file=None)

        async def _get_user_by_id(self, user_id: str) -> object:
            class _User:
                id = user_id
                is_active = True

            return _User()

        async def _best_effort_revoke(self, family_id: str | None) -> None:
            return None

    service = _Service()
    token = issue_refresh_token(victim_id, session_id, session_id=session_id).token
    payload = service_module.decode_token(token, expected_type=service_module.REFRESH_TOKEN_TYPE)

    # Rewrite only the token subject: signature stays valid because we re-issue.
    forged = issue_refresh_token(str(uuid.uuid4()), session_id, session_id=session_id).token
    monkeypatch.setattr(service_module, "store", _Store, raising=False)
    monkeypatch.setattr(service_module, "session_is_active", lambda sid: asyncio.sleep(0, True))
    monkeypatch.setattr(service_module, "session_owner", lambda sid: asyncio.sleep(0, victim_id))

    assert payload["sub"] == victim_id
    with pytest.raises(service_module.AuthServiceError):
        asyncio.run(service.refresh(forged))


# --------------------------------------------------------------------------- #
# Metric 3: rotation mechanism (built + tested here; executed by the operator)
# --------------------------------------------------------------------------- #


def test_rotation_plan_counts_and_dry_run_changes_nothing(
    fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.auth import rotation
    from app.auth.adapters.redis_store import FAMILY_KEY_PREFIX, REFRESH_KEY_PREFIX
    from app.auth.sessions import create_session

    for index in range(3):
        asyncio.run(create_session(f"s{index}", str(uuid.uuid4())))
    for index in range(4):
        fake_redis.store[REFRESH_KEY_PREFIX + f"jti{index}"] = "{}"
    for index in range(2):
        fake_redis.store[FAMILY_KEY_PREFIX + f"fam{index}"] = ""

    plan = asyncio.run(rotation.plan_rotation())
    assert (plan.sessions, plan.refresh_tokens, plan.refresh_families) == (3, 4, 2)
    assert plan.total_keys == 9

    monkeypatch.setenv(
        "AUTH_JWT_SECRET", "another-scratch-secret-" + "3a7f19c4b8e2d605" + "f0a3c6d9"
    )
    before = dict(fake_redis.store)
    report = asyncio.run(rotation.rotate_signing_secret(execute=False))
    assert report.executed is False and report.secret_swapped is False
    assert report.sessions_revoked == 0
    assert fake_redis.store == before  # a dry run touches nothing


def test_rotation_revokes_everything_and_swaps_the_secret(
    fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.auth import rotation
    from app.auth.adapters.redis_store import FAMILY_KEY_PREFIX, REFRESH_KEY_PREFIX
    from app.auth.sessions import create_session
    from app.auth.tokens import decode_token, issue_access_token

    old_secret = SCRATCH_SECRET
    new_secret = "rotated-scratch-" + "5e1c8a37f9b204d6" + "a8e3b7c10d4f6925"
    sid = "session-to-die"
    asyncio.run(create_session(sid, str(uuid.uuid4())))
    fake_redis.store[REFRESH_KEY_PREFIX + "jti0"] = "{}"
    fake_redis.store[FAMILY_KEY_PREFIX + "fam0"] = ""

    # Control our own preconditions first: the settings cache is process-global
    # and may hold residue from sibling tests, so set the secret and clear the
    # cache BEFORE minting (coordinator fix — minting first made this test
    # depend on whatever a previous test left behind).
    monkeypatch.setenv("AUTH_JWT_SECRET", old_secret)
    from app.auth.config import get_auth_settings

    get_auth_settings.cache_clear()

    # A token minted under the old secret, presented before rotation.
    old_token = issue_access_token(str(uuid.uuid4()), session_id=sid).token

    assert decode_token(old_token, expected_type="access")["sub"] is not None

    report = asyncio.run(rotation.rotate_signing_secret(secret=new_secret))
    assert report.executed is True and report.secret_swapped is True
    assert (report.sessions_revoked, report.refresh_tokens_revoked) == (1, 1)
    assert report.refresh_families_revoked == 1
    assert report.plan.total_keys == 3

    # Every session and refresh record is gone.
    assert fake_redis.store == {}
    # The new secret is what the process will use now...
    assert get_auth_settings().jwt_secret == new_secret
    assert os.environ["AUTH_JWT_SECRET"] == new_secret
    # ...and the old token no longer verifies.
    from app.auth.tokens import AuthTokenError

    with pytest.raises(AuthTokenError):
        decode_token(old_token, expected_type="access")
    # A freshly minted token round-trips under the new secret.
    fresh = issue_access_token("user-after-rotation", session_id="brand-new")
    assert decode_token(fresh.token, expected_type="access")["sub"] == "user-after-rotation"

    get_auth_settings.cache_clear()
    monkeypatch.setenv("AUTH_JWT_SECRET", old_secret)
    get_auth_settings.cache_clear()


def test_rotation_refuses_to_run_onto_a_bad_secret(fake_redis: _FakeRedis) -> None:
    """Validate BEFORE revoking: a bad secret must not strand every user."""
    from app.auth import rotation
    from app.auth.sessions import create_session

    asyncio.run(create_session("survivor", str(uuid.uuid4())))
    for bad in ("", "dev-insecure-change-me", "too-short"):
        with pytest.raises(ValueError):
            asyncio.run(rotation.rotate_signing_secret(secret=bad))
    assert "auth:session:survivor" in fake_redis.store  # nothing was revoked


def test_rotation_runbook_names_the_operator_steps() -> None:
    from app.auth.rotation import describe_procedure

    text = describe_procedure()
    assert "--plan" in text and "--execute" in text
    assert "token_urlsafe" in text
    assert "coordinator/operator" in text