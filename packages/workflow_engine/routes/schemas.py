"""HTTP contracts for the workspace draft endpoints (T-V4-019 / W4).

Pure contract types. Nothing here routes, handles, or persists:

* **no route decorator** — no ``APIRouter``, no ``@router.post``;
* **no store import** — the draft store (T-V4-017) is not imported here, so these
  schemas stay valid whether the backend is in-memory or the durable one from
  the T-V4-018 migration.

A handler arriving later must typecheck against these models unchanged. If a
handler needs a field that is not here, that is a **contract change** (a new
versioned field), not a handler detail.

House conventions mirrored from ``apps/api/app/auth/workspaces/schemas.py``
(M1-001b):

* request bodies are flat ``BaseModel`` schemas with explicit bounds;
* error bodies are ``{"detail": {"error", "message"}}``;
* 422 bodies are the lean redacted shape (no ``input``/``ctx`` echoed);
* unknown request fields are **rejected** (``extra="forbid"``) so a typo never
  silently becomes a default.

Reuse boundary
--------------
The payload is the engine's own :class:`~workflow_engine.interfaces.WorkflowDefinition`,
so node/port/edge validation is the shared model's job (not re-implemented
here). What this module adds is the *wire* contract the shared model cannot
express: size caps, path/body scope agreement, the per-route auth vocabulary,
the error catalogue, and the route table a handler must satisfy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from ..interfaces import WorkflowDefinition

# --------------------------------------------------------------------------- #
# Wire-level limits (the API boundary states its own caps)
# --------------------------------------------------------------------------- #

#: Mirrors the store's payload bound (store_discipline.MAX_PAYLOAD_BYTES) without
#: importing it: the API refuses oversized bodies before they reach the store.
MAX_SPEC_BYTES = 262_144
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 4_096
MAX_NODES = 200
MAX_EDGES = 500
MAX_SUMMARY_CHARS = 1_024
WORKFLOW_ID_MAX = 128
REVISION_MIN = 1
REVISION_MAX = 1_000_000
CONTENT_HASH_CHARS = 64
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200


#: A path/body identifier: no blank padding, bounded length.
type NonBlankId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=WORKFLOW_ID_MAX)
]

#: A revision number: strictly an integer — no bool, no float, no numeric string.
type RevisionNumber = Annotated[int, StrictInt]


# --------------------------------------------------------------------------- #
# Errors — the app's nested envelope, plus the draft error catalogue
# --------------------------------------------------------------------------- #


class DraftErrorCode(StrEnum):
    """Every failure a draft handler can produce (nothing else is invented)."""

    DRAFT_VALIDATION_FAILED = "draft_validation_failed"
    DRAFT_NOT_FOUND = "draft_not_found"
    DRAFT_CONFLICT = "draft_conflict"
    DRAFT_IDENTITY_MISMATCH = "draft_identity_mismatch"
    DRAFT_WORKSPACE_MISMATCH = "draft_workspace_mismatch"
    DRAFT_TOO_LARGE = "draft_too_large"
    DRAFT_SECRET_REFUSED = "draft_secret_refused"
    DRAFT_FORBIDDEN = "draft_forbidden"


class ApiErrorDetail(BaseModel):
    """The ``detail`` object of an error body."""

    model_config = ConfigDict(extra="forbid")

    error: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2_048)


class ApiErrorBody(BaseModel):
    """The error envelope: ``{"detail": {"error", "message"}}``."""

    model_config = ConfigDict(extra="forbid")

    detail: ApiErrorDetail


class DraftErrorBody(ApiErrorBody):
    """Error body whose ``error`` is one of :class:`DraftErrorCode`."""

    detail: ApiErrorDetail = Field(
        description=f"error must be one of {[c.value for c in DraftErrorCode]}"
    )


@dataclass(frozen=True)
class DraftErrorSpec:
    """One documented failure of a route: status, code, and when it happens."""

    status_code: int
    code: DraftErrorCode
    when: str


# --------------------------------------------------------------------------- #
# Path and query contracts
# --------------------------------------------------------------------------- #


class DraftWorkflowPath(BaseModel):
    """``/v1/workspaces/{workspace_id}/drafts/{workflow_id}`` — shared segment."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: NonBlankId

    @field_validator("workflow_id", mode="before")
    @classmethod
    def _must_be_text(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("identifier must be a string")
        return value


def _must_be_integer(value: object) -> int:
    """Accept only a real integer — never a bool, float, or numeric string.

    ``StrictInt`` alone still coerces ``"1"`` under lax validation, so a path
    param of ``revision: "1"`` would silently become 1. A revision number is
    an identity, not a value to coerce.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("revision must be an integer, not a bool/float/string")
    return value


class DraftRevisionPath(DraftWorkflowPath):
    """``.../revisions/{revision}`` — the path of the get-revision route."""

    revision: RevisionNumber = Field(ge=REVISION_MIN, le=REVISION_MAX)

    @field_validator("revision", mode="before")
    @classmethod
    def _strict_revision(cls, value: object) -> int:
        return _must_be_integer(value)


class DraftHistoryQuery(BaseModel):
    """Query params for history. No cursor: history is complete by contract.

    The append-only store returns every revision, so a cursor would promise
    pagination the backend cannot honour — history is all-or-nothing and
    ``count`` says how many there are.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT)


class DraftLookupScope(BaseModel):
    """The scope a draft read is addressed by: workspace **and** workflow.

    Exists so an unscoped read is not expressible in a handler's type
    signatures (T-V4-020 / R-004): the store's read API takes
    ``(workspace_id, workflow_id, …)``, and this is the wire-shaped mirror of
    that requirement. A lookup scoped only by workflow id has no field to live
    in, so it cannot be written by accident.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: NonBlankId
    workflow_id: NonBlankId


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class DraftDefinitionRequest(WorkflowDefinition):
    """Body of a save/revise call: the definition itself, flat (house style).

    The payload **is** the engine's :class:`WorkflowDefinition`, so node/port/
    edge validation is inherited rather than re-implemented — a handler gets a
    validated definition, not a dict. What this body adds is the wire-level
    caps, structural invariants and the credential rule (T-V4-020), all of
    which must fail at the boundary rather than deeper in.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("id", "workspace_id")
    @classmethod
    def _identifiers_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("identifier must not be blank")
        return stripped

    @model_validator(mode="after")
    def _no_credential_values(self) -> DraftDefinitionRequest:
        """Credentials travel by REFERENCE (vault id / name), never by value.

        Shares one grammar with the draft store via :mod:`credential_hygiene`
        (a pure module, so the schema layer does not import the store), so the
        API refuses exactly what the store would refuse and accepts exactly the
        reference forms the store accepts. A reference-shaped key carrying a
        value — ``api_key_ref: sk-live-…`` — is refused too: that is a value
        wearing a reference's name.
        """
        from ..credential_hygiene import (
            CredentialHygieneError,
            assert_references_only,
        )

        for node in self.nodes or []:
            try:
                assert_references_only(node.config, where=f"node {node.id!r} config")
            except CredentialHygieneError as exc:
                # Both a value-shaped credential and a reference-shaped key
                # carrying a value are 422s at this boundary.
                raise ValueError(str(exc)[:160]) from exc
        return self

    @model_validator(mode="after")
    def _within_caps(self) -> DraftDefinitionRequest:
        if len(self.nodes or []) > MAX_NODES:
            raise ValueError(f"a draft may declare at most {MAX_NODES} nodes")
        if len(self.edges or []) > MAX_EDGES:
            raise ValueError(f"a draft may declare at most {MAX_EDGES} edges")
        if len(self.name or "") > MAX_NAME_CHARS:
            raise ValueError(f"name may be at most {MAX_NAME_CHARS} characters")
        if len(self.description or "") > MAX_DESCRIPTION_CHARS:
            raise ValueError(
                f"description may be at most {MAX_DESCRIPTION_CHARS} characters"
            )
        return self

    @model_validator(mode="after")
    def _structurally_sound(self) -> DraftDefinitionRequest:
        node_ids = [node.id for node in self.nodes or []]
        if not node_ids:
            raise ValueError("a draft must declare at least one node")
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node ids must be unique within a draft")
        known = set(node_ids)
        edge_ids = [edge.id for edge in self.edges or []]
        if len(set(edge_ids)) != len(edge_ids):
            raise ValueError("edge ids must be unique within a draft")
        for edge in self.edges or []:
            if edge.source_node_id not in known or edge.target_node_id not in known:
                raise ValueError(
                    f"edge {edge.id!r} references a node that is not declared"
                )
        return self

    @model_validator(mode="after")
    def _within_payload_cap(self) -> DraftDefinitionRequest:
        if self.canonical_bytes() > MAX_SPEC_BYTES:
            raise ValueError(
                f"definition serialises to more than {MAX_SPEC_BYTES} bytes"
            )
        return self

    def canonical_bytes(self) -> int:
        """Size of the definition on the wire, used for the payload cap."""
        import json

        return len(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        )


class DraftSaveRequest(DraftDefinitionRequest):
    """``POST /v1/workspaces/{workspace_id}/drafts`` — save a new draft.

    Saves the definition as revision 1 of a workflow. Saving a workflow that
    already has revisions is a ``409`` — callers must revise instead.
    """


class DraftReviseRequest(DraftDefinitionRequest):
    """``POST /v1/workspaces/{workspace_id}/drafts/{workflow_id}/revisions``.

    Appends a revision of an existing workflow. Revising an *unknown* workflow
    initialises it as revision 1 — the T-V4-017 store's verified behaviour,
    stated here so a handler cannot claim a 404 it will never get.
    """


# --------------------------------------------------------------------------- #
# Response bodies
# --------------------------------------------------------------------------- #


class DraftRevisionMeta(BaseModel):
    """Revision identity + provenance, without the definition.

    This is what a history listing returns: enough to reason about provenance,
    small enough that a long history stays readable.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: NonBlankId
    workspace_id: NonBlankId
    revision: RevisionNumber = Field(ge=REVISION_MIN, le=REVISION_MAX)
    content_hash: str = Field(
        min_length=CONTENT_HASH_CHARS, max_length=CONTENT_HASH_CHARS,
        pattern=r"^[0-9a-f]+$",
    )
    summary: str = Field(max_length=MAX_SUMMARY_CHARS)
    parent_revision: RevisionNumber | None = Field(default=None, ge=REVISION_MIN, le=REVISION_MAX)
    parent_hash: str | None = Field(
        default=None,
        min_length=CONTENT_HASH_CHARS,
        max_length=CONTENT_HASH_CHARS,
        pattern=r"^[0-9a-f]+$",
    )
    created_at: str = Field(min_length=20, max_length=40)

    @field_validator("revision", "parent_revision", mode="before")
    @classmethod
    def _strict_revisions(cls, value: object) -> object:
        return None if value is None else _must_be_integer(value)


class DraftRevisionDetail(DraftRevisionMeta):
    """A revision with its definition — the get-revision response shape."""

    spec: WorkflowDefinition


class DraftMutationResponse(DraftRevisionDetail):
    """Save/revise response: the stored revision plus idempotency of the write.

    ``idempotent`` is ``True`` when the caller's content matched the current
    head, so the store returned that head unchanged rather than appending a
    revision (T-V4-017 behaviour: identical content is never duplicated).
    """

    idempotent: bool


class DraftHistoryResponse(BaseModel):
    """History listing: complete, ordered, and counted."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1, max_length=WORKFLOW_ID_MAX)
    workspace_id: str = Field(min_length=1, max_length=WORKFLOW_ID_MAX)
    count: int = Field(ge=0, le=REVISION_MAX)
    revisions: list[DraftRevisionMeta] = Field(default_factory=list)

    @model_validator(mode="after")
    def _count_matches(self) -> DraftHistoryResponse:
        if self.count != len(self.revisions):
            raise ValueError("count must equal the number of revisions returned")
        return self


# --------------------------------------------------------------------------- #
# Auth vocabulary
# --------------------------------------------------------------------------- #


class DraftRouteName(StrEnum):
    """The four draft endpoints this contract fixes."""

    SAVE_DRAFT = "save_draft"
    REVISE_DRAFT = "revise_draft"
    GET_REVISION = "get_revision"
    LIST_HISTORY = "list_history"


class DraftAuth(StrEnum):
    """Authorization requirement for a route.

    Values are the **existing** ``app.authz.Action`` literals — drafts need no
    new auth actions, they are workspace-scoped resources. The mapping (kept as
    a string so ``packages/`` does not import ``apps/``) is:

    ``workspace.manage`` → ``Action.WORKSPACE_MANAGE`` (writes)
    ``workspace.read``   → ``Action.WORKSPACE_READ``   (reads)

    Every route also requires the caller's scope to match the ``workspace_id``
    in the path (R-004), which is enforced before the store is touched.
    """

    WORKSPACE_WRITE = "workspace.manage"
    WORKSPACE_READ = "workspace.read"


# --------------------------------------------------------------------------- #
# Route contracts — the table a handler must satisfy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RouteContract:
    """What a route must look like: verb, path, schemas, auth, and failures.

    This is metadata, not wiring: importing this table does not register a
    route anywhere. It exists so a handler that drifts from the contract is
    visibly wrong.
    """

    name: DraftRouteName
    method: Literal["GET", "POST"]
    path: str
    auth: DraftAuth
    request: type[BaseModel] | None
    responses: tuple[tuple[int, type[BaseModel]], ...]
    errors: tuple[DraftErrorSpec, ...]

    def error_codes(self) -> tuple[DraftErrorCode, ...]:
        return tuple(e.code for e in self.errors)

    def schema_names(self) -> tuple[str, ...]:
        names = [self.request.__name__] if self.request is not None else []
        names.extend(r.__name__ for _, r in self.responses)
        return tuple(names)


_403 = DraftErrorSpec(403, DraftErrorCode.DRAFT_FORBIDDEN, "caller may not access this workspace")
_404_NOT_FOUND = DraftErrorSpec(
    404, DraftErrorCode.DRAFT_NOT_FOUND, "workflow or revision does not exist"
)
_409_CONFLICT = DraftErrorSpec(
    409, DraftErrorCode.DRAFT_CONFLICT, "workflow already has revisions; revise instead"
)
_413 = DraftErrorSpec(413, DraftErrorCode.DRAFT_TOO_LARGE, "definition exceeds the payload cap")
_422_SHAPE = DraftErrorSpec(
    422, DraftErrorCode.DRAFT_VALIDATION_FAILED, "body failed schema validation"
)
_422_IDENTITY = DraftErrorSpec(
    422,
    DraftErrorCode.DRAFT_IDENTITY_MISMATCH,
    "definition id does not match the workflow_id in the path",
)
_422_WORKSPACE = DraftErrorSpec(
    422,
    DraftErrorCode.DRAFT_WORKSPACE_MISMATCH,
    "definition workspace_id does not match the workspace_id in the path",
)
_422_SECRET = DraftErrorSpec(
    422,
    DraftErrorCode.DRAFT_SECRET_REFUSED,
    "node config carried a secret (refused at the store boundary)",
)

ROUTE_CONTRACTS: tuple[RouteContract, ...] = (
    RouteContract(
        name=DraftRouteName.SAVE_DRAFT,
        method="POST",
        path="/v1/workspaces/{workspace_id}/drafts",
        auth=DraftAuth.WORKSPACE_WRITE,
        request=DraftSaveRequest,
        responses=((200, DraftMutationResponse),),
        errors=(_403, _409_CONFLICT, _413, _422_SHAPE, _422_WORKSPACE, _422_SECRET),
    ),
    RouteContract(
        name=DraftRouteName.REVISE_DRAFT,
        method="POST",
        path="/v1/workspaces/{workspace_id}/drafts/{workflow_id}/revisions",
        auth=DraftAuth.WORKSPACE_WRITE,
        request=DraftReviseRequest,
        responses=((200, DraftMutationResponse),),
        errors=(_403, _413, _422_SHAPE, _422_IDENTITY, _422_WORKSPACE, _422_SECRET),
    ),
    RouteContract(
        name=DraftRouteName.GET_REVISION,
        method="GET",
        path="/v1/workspaces/{workspace_id}/drafts/{workflow_id}/revisions/{revision}",
        auth=DraftAuth.WORKSPACE_READ,
        request=None,
        responses=((200, DraftRevisionDetail),),
        errors=(
            _403,
            _404_NOT_FOUND,
            _422_SHAPE,
        ),
    ),
    RouteContract(
        name=DraftRouteName.LIST_HISTORY,
        method="GET",
        path="/v1/workspaces/{workspace_id}/drafts/{workflow_id}/revisions",
        auth=DraftAuth.WORKSPACE_READ,
        request=DraftHistoryQuery,
        responses=((200, DraftHistoryResponse),),
        errors=(
            _403,
            DraftErrorSpec(
                404, DraftErrorCode.DRAFT_NOT_FOUND, "workflow does not exist"
            ),
            _422_SHAPE,
        ),
    ),
)


def contract_for(name: DraftRouteName | str) -> RouteContract:
    """Look up a route contract by name; unknown names raise ``KeyError``."""
    try:
        wanted = DraftRouteName(name)
    except ValueError as exc:  # not a draft route name
        raise KeyError(name) from exc
    for contract in ROUTE_CONTRACTS:
        if contract.name == wanted:
            return contract
    raise KeyError(name)


def schemas_for_route(name: DraftRouteName | str) -> tuple[str, ...]:
    """Request/response schema names a handler for *name* must use, unchanged."""
    return contract_for(name).schema_names()


# --------------------------------------------------------------------------- #
# Scope agreement — the one check that needs the path, as a pure helper
# --------------------------------------------------------------------------- #


def assert_path_scope_matches(
    body: DraftDefinitionRequest,
    *,
    workspace_id: str,
    workflow_id: str | None = None,
) -> None:
    """Refuse a body whose identity disagrees with the route's path (403/422).

    The workspace check is an authorization concern (R-004): a caller must never
    write a draft into a workspace their token is not scoped to, even by
    accident. The workflow check is an identity concern. Both are raised as
    ``ValueError`` with the code the route's error catalogue names, so a handler
    maps them instead of writing its own branch.

    Pure: no I/O, no store, no router. Raises ``ValueError`` only.
    """
    if body.workspace_id != workspace_id:
        raise ValueError(
            f"{DraftErrorCode.DRAFT_WORKSPACE_MISMATCH.value}: "
            f"body workspace {body.workspace_id!r} != path {workspace_id!r}"
        )
    if workflow_id is not None and body.id != workflow_id:
        raise ValueError(
            f"{DraftErrorCode.DRAFT_IDENTITY_MISMATCH.value}: "
            f"body workflow {body.id!r} != path {workflow_id!r}"
        )


__all__ = [
    "CONTENT_HASH_CHARS",
    "DEFAULT_HISTORY_LIMIT",
    "MAX_SPEC_BYTES",
    "ROUTE_CONTRACTS",
    "ApiErrorBody",
    "ApiErrorDetail",
    "DraftAuth",
    "DraftDefinitionRequest",
    "DraftErrorBody",
    "DraftErrorCode",
    "DraftErrorSpec",
    "DraftHistoryQuery",
    "DraftHistoryResponse",
    "DraftLookupScope",
    "DraftMutationResponse",
    "DraftReviseRequest",
    "DraftRevisionDetail",
    "DraftRevisionMeta",
    "DraftRevisionPath",
    "DraftRouteName",
    "DraftSaveRequest",
    "DraftWorkflowPath",
    "RouteContract",
    "assert_path_scope_matches",
    "contract_for",
    "schemas_for_route",
]
