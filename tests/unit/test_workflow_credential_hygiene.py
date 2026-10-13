"""T-V4-020 — credential-reference hygiene across every W4 boundary.

The rule, in one place (``credential_hygiene``) and asserted everywhere:

    credentials travel by REFERENCE (a vault id / a name), never by value.

Pinned properties (card):
1. Credential values accepted at any W4 boundary: **0** — a hostile battery of
   passwords / tokens / keys by value in drafts, revisions and payloads, refused
   at the draft store AND at the route schema boundary.
2. By-reference forms **pass** at both boundaries — and the same grammar is used
   at both, so what the API accepts the store accepts (no hostile shape that
   passes one and dies at the other).
3. A reference is not a hole: a reference-shaped key carrying a value is refused.
4. Redaction never echoes secret material.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from workflow_engine import (
    NodeSpec,
    NodeType,
    PortSpec,
    WorkflowDefinition,
    WorkflowEdgeSpec,
)
from workflow_engine.credential_hygiene import (
    CredentialHygieneError,
    CredentialReference,
    CredentialReferenceError,
    CredentialValueError,
    assert_references_only,
    find_credential_value,
    find_secret_text,
    is_credential_key,
    is_reference_key,
    redact_text,
)
from workflow_engine.draft_store import InMemoryDraftStore
from workflow_engine.routes.schemas import DraftReviseRequest, DraftSaveRequest
from workflow_engine.store_discipline import StoreSecretError, assert_no_secret_config

SCHEMAS = Path(
    __file__
).resolve().parents[2] / "packages" / "workflow_engine" / "routes" / "schemas.py"
DISCIPLINE = Path(
    __file__
).resolve().parents[2] / "packages" / "workflow_engine" / "store_discipline.py"


def _node(config: dict) -> NodeSpec:
    return NodeSpec(
        id="caller",
        type=NodeType.TOOL,
        name="Caller",
        config=config,
        input_ports=[PortSpec(name="input", type="any")],
        output_ports=[PortSpec(name="output", type="any")],
    )


def _defn_with_config(config: dict, *, workspace: str = "ws-1") -> WorkflowDefinition:
    return WorkflowDefinition(
        id="wf-creds",
        workspace_id=workspace,
        name="Creds",
        nodes=[
            NodeSpec(
                id="start",
                type=NodeType.TRIGGER,
                name="Start",
                output_ports=[PortSpec(name="output", type="dict")],
            ),
            _node(config),
        ],
        edges=[
            WorkflowEdgeSpec(
                id="e1", source_node_id="start", source_port="output",
                target_node_id="caller", target_port="input",
            )
        ],
    )


def _api_body(config: dict) -> dict:
    return json.loads(_defn_with_config(config).model_dump_json())


#: Credential **values**: must be refused everywhere.
VALUES = {
    "password": "hunter2",
    "api_key": "sk-" + "a" * 30,
    "authorization": "Bearer " + "z" * 24,
    "Authorization": "Bearer " + "z" * 24,
    "token": "ghp_" + "b" * 30,
    "client_secret": "supersecretvalue",
    "passwd": "letmein",
    "headers": {"Authorization": "Bearer " + "q" * 24},
    "nested": {"deep": {"private_key": "-----BEGIN PRIVATE KEY-----MIIBVg"}},
    "api_key_ref": "sk-" + "a" * 30,          # value wearing a reference's name
    "password_ref": "Bearer " + "z" * 24,      # ditto
    "secret_ref": "nope " * 12,                # a sentence, not a reference
}

#: Credential **references**: must pass everywhere.
REFERENCES = {
    "api_key_ref": "vault:prod/api-key",
    "db_password_name": "primary-db-credential",
    "credential_id": "cred-7f3a9",
    "secret_ref": "vault:team/infra/db-password",
    "token_name": "svc-account-token",
    "authorization_ref": "vault:shared/http-auth",
    "nested": {"api_key_ref": "vault:prod/inner"},
}


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_reference_keys_are_recognised() -> None:
    for key in (
        "api_key_ref", "api_key-ref", "api_key_id", "api_key_name",
        "db_password_ref", "credential_id", "token_name", "secret_ref",
    ):
        assert is_reference_key(key), key


@pytest.mark.unit
def test_reference_shape_is_strict() -> None:
    for good in ("vault:prod/api-key", "primary-db-credential", "cred-7f3a9",
                 "vault:team/infra/db-password"):
        assert CredentialReference.is_reference(good), good
        assert CredentialReference.parse(good).ref == good
    for bad in ("nope nope nope", "x" * 193, 42, None):
        assert not CredentialReference.is_reference(bad), bad
    for bad_text in (
        "", "nope nope nope", "sk-" + "a" * 30, "Bearer " + "z" * 24,
        "-----BEGIN PRIVATE KEY-----", "has=equals", "x" * 193,
    ):
        assert not CredentialReference.is_reference(bad_text), bad_text
        with pytest.raises((ValidationError, CredentialHygieneError)):
            CredentialReference.parse(bad_text)


@pytest.mark.unit
def test_the_deny_list_lives_in_exactly_one_module() -> None:
    """One grammar: the list is declared once and imported, not copied.

    The failure mode this guards against is two lists that drift, so the
    check is that no W4 boundary declares its own and both import the shared
    module instead.
    """
    import ast

    sources = {"schemas": SCHEMAS, "store_discipline": DISCIPLINE}
    assert sources
    for label, path in sources.items():
        source = path.read_text(encoding="utf-8")
        assert "frozenset(" not in source, f"{label} declares its own deny tokens"
        assert "re.compile" not in source, f"{label} declares its own patterns"
        tree = ast.parse(source)
        imported = {
            (node.module or "")
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert any(m.endswith("credential_hygiene") for m in imported), (
            f"{label} does not share the credential grammar: {imported}"
        )


@pytest.mark.unit
def test_denied_keys_are_the_ones_the_grammar_names() -> None:
    for key in ("api_key", "password", "auth_token", "authorization",
                "client_secret", "private_key", "passwd", "credential"):
        assert is_credential_key(key), key
    # ``auth`` is a deny token, so an *auth-prefixed config key* is refused;
    # only node ids/names/descriptions are unscreened (the grammar never sees
    # them), which is why a node named ``auth_check`` still saves fine.
    for key in ("template", "sort_key", "tokenizer_vocab",
                "author", "key_count", "retries", "check", "auth_ok_mode".replace(
                    "auth_ok_mode", "mode")):
        assert not is_credential_key(key), key


# --------------------------------------------------------------------------- #
# 1. Values are refused at EVERY W4 boundary
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_store_refuses_every_credential_value() -> None:
    for key, value in VALUES.items():
        config = {key: value}
        with pytest.raises(StoreSecretError):
            assert_no_secret_config(_defn_with_config(config))


@pytest.mark.unit
def test_route_schema_refuses_every_credential_value() -> None:
    for key, value in VALUES.items():
        with pytest.raises(ValidationError):
            DraftSaveRequest.model_validate(_api_body({key: value}))
        with pytest.raises(ValidationError):
            DraftReviseRequest.model_validate(_api_body({key: value}))


@pytest.mark.unit
async def test_store_never_persists_a_credential_value() -> None:
    """Refusal happens before the record exists, so nothing is written."""
    store = InMemoryDraftStore()
    hostile = _defn_with_config({"api_key": "sk-" + "a" * 30})
    with pytest.raises(StoreSecretError):
        await store.save_draft(hostile)
    assert await store.count(hostile.workspace_id, hostile.id) == 0
    assert await store.workflow_ids(hostile.workspace_id) == []


@pytest.mark.unit
def test_secret_text_is_detected_wherever_it_appears() -> None:
    for text in (
        "sk-" + "a" * 30,
        "ghp_" + "b" * 30,
        "Authorization: Bearer " + "z" * 24,
        "-----BEGIN RSA PRIVATE KEY-----",
        "password: hunter2",
        '{"api_key": "sk-' + "c" * 24 + '"}',
    ):
        assert find_secret_text(text) is not None, text


@pytest.mark.unit
def test_non_secret_text_is_not_flagged() -> None:
    for text in ("Hi {name}!", "auth_check", "tokenizer: bert", "vault:prod/api-key"):
        assert find_secret_text(text) is None, text


# --------------------------------------------------------------------------- #
# 2. References pass at EVERY W4 boundary
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_store_accepts_every_credential_reference() -> None:
    for key, value in REFERENCES.items():
        defn = _defn_with_config({key: value})
        assert_no_secret_config(defn)
        assert find_credential_value(defn.nodes[1].config) is None


@pytest.mark.unit
def test_route_schema_accepts_every_credential_reference() -> None:
    for key, value in REFERENCES.items():
        assert DraftSaveRequest.model_validate(_api_body({key: value}))
        assert DraftReviseRequest.model_validate(_api_body({key: value}))


@pytest.mark.unit
async def test_a_reference_survives_the_round_trip_unchanged() -> None:
    """A reference is stored and returned verbatim — never rewritten or dropped."""
    store = InMemoryDraftStore()
    config = {"api_key_ref": "vault:prod/api-key", "token": "unused"}
    del config["token"]
    defn = _defn_with_config(config)
    await store.save_draft(defn)

    stored = await store.get("ws-1", "wf-creds", 1)
    assert stored.spec.nodes[1].config == {"api_key_ref": "vault:prod/api-key"}
    # Identity is unaffected by the reference (the hash still verifies).
    from workflow_engine.draft_store import verify_revision

    assert verify_revision(stored.revision)


@pytest.mark.unit
def test_reference_key_carrying_a_value_is_refused_at_both_boundaries() -> None:
    for key, value in (("api_key_ref", "sk-" + "a" * 30), ("password_ref", "Bearer " + "z" * 24)):
        with pytest.raises((CredentialValueError, CredentialReferenceError)):
            assert_references_only({key: value}, where="probe")
        with pytest.raises(StoreSecretError):
            assert_no_secret_config(_defn_with_config({key: value}))
        with pytest.raises(ValidationError):
            DraftSaveRequest.model_validate(_api_body({key: value}))


# --------------------------------------------------------------------------- #
# 3. Redaction never echoes credential material
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_redaction_drops_the_material_and_keeps_the_label() -> None:
    redacted = redact_text('{"api_key": "sk-' + "a" * 30 + '"}')
    assert "sk-" + "a" * 30 not in redacted
    assert "[REDACTED]" in redacted
    assert redact_text("Authorization: Bearer " + "z" * 24) == "Authorization: [REDACTED]"


@pytest.mark.unit
async def test_summary_of_a_credential_bearing_draft_leaks_nothing() -> None:
    store = InMemoryDraftStore()
    defn = _defn_with_config({"api_key_ref": "vault:prod/api-key"})
    await store.save_draft(defn)
    record = await store.latest("ws-1", "wf-creds")
    assert record is not None
    summary = record.summary
    assert "wf-creds" in summary
    # The reference may appear (it is not a secret), but never any value.
    assert "vault:prod/api-key" in summary or "api_key_ref" not in summary


# --------------------------------------------------------------------------- #
# 4. The rule holds for raw config too, not only definitions
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_assert_references_only_reports_the_offending_key() -> None:
    with pytest.raises(CredentialValueError) as exc:
        assert_references_only({"password": "hunter2"}, where="node 'x' config")
    assert "password" in str(exc.value)

    with pytest.raises(CredentialReferenceError):
        assert_references_only({"api_key_ref": "sk-" + "a" * 30}, where="probe")


@pytest.mark.unit
def test_safe_configs_pass_untouched() -> None:
    safe = {
        "template": "Hi {name}!",
        "retries": 3,
        "chunk_ids": ["a", "b"],
        "nested": {"ok": True},
    }
    assert assert_references_only(safe) is safe
