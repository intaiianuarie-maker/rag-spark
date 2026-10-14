"""Editor node palette as DATA (T-V4-022 / W5).

Every placeable node type with its ports, config-schema reference, join/loop
eligibility and documentation — and **no UI**: no component, no canvas, no event
handler. A future canvas renders this catalogue unchanged; that is the whole
point of it being data.

Why this shape
--------------
The palette is not a second source of truth. Every rule it states is one the
validator (T-V4-006/007/008) already enforces, and the covering tests assert
agreement in **both** directions:

* every catalogued node type validates clean against a minimal definition
  built only from the catalogue (a palette entry that cannot be placed is a
  lie), and
* every node type the validator knows about appears in the catalogue (a node
  the engine can run but the palette cannot offer is also a lie).

Join/loop eligibility comes from the port shape, not from a flag someone must
remember to set: a node whose input ports accept more than one inbound edge is
join-eligible, and ``join_strategy`` is what the compiler will demand. Loop
eligibility is likewise derived — a node whose input port type accepts its own
output type can close a cycle.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .interfaces import (
    _SUPPORTED_PORT_TYPES,
    CapabilityManifest,
    NodeType,
    PortSpec,
)

# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #

MAX_DOC_CHARS = 2_048
MAX_LABEL_CHARS = 64
MAX_PORTS = 32
MAX_CONFIG_KEYS = 64
MAX_CAPABILITIES = 16

#: A non-blank, bounded identifier.
NonBlankId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_LABEL_CHARS)
]

#: Port type grammar (T-V4-007). The set is **imported** from
#: ``interfaces._SUPPORTED_PORT_TYPES`` rather than restated here: a palette
#: naming its own types would drift from the validator's, and the first symptom
#: of that drift is an editor offering a node that cannot be placed. (My first
#: draft declared ``bool``/``int``/``float``, none of which the engine accepts
#: — exactly the failure this import prevents.)
_PORT_TYPES = frozenset(_SUPPORTED_PORT_TYPES)
_NULLABLE_SUFFIX = "?"


# --------------------------------------------------------------------------- #
# Palette entry model
# --------------------------------------------------------------------------- #


class PaletteCategory(StrEnum):
    """Where the node sits in the palette's groups."""

    FLOW = "flow"
    DATA = "data"
    MODEL = "model"
    CONTROL = "control"
    EFFECT = "effect"


class PalettePort(BaseModel):
    """One palette port: name, type, required-ness, and what it is for."""

    model_config = ConfigDict(extra="forbid")

    name: NonBlankId
    #: Port type in the T-V4-007 grammar (``string``, ``list<string>``, ``bool?`` …).
    type: str = "any"
    #: Required input ports must be satisfied by an edge or the initial input,
    #: exactly as :class:`~workflow_engine.interfaces.PortSpec` enforces.
    required: bool = False
    #: Present only on input ports: a default satisfies the required rule.
    has_default: bool = False
    description: str = Field(default="", max_length=MAX_DOC_CHARS)

    @field_validator("type")
    @classmethod
    def _known_port_type(cls, value: str) -> str:
        candidate = value[:-1] if value.endswith(_NULLABLE_SUFFIX) else value
        if candidate.startswith("list<") and candidate.endswith(">"):
            candidate = candidate[5:-1]
        if candidate not in _PORT_TYPES:
            raise ValueError(
                f"{value!r} is not a supported port type (T-V4-007); "
                f"expected one of {sorted(_PORT_TYPES)}, list<...> or a trailing ?"
            )
        return value

    def to_port_spec(self) -> PortSpec:
        """The PortSpec the engine validates — built FROM the palette, so the
        two cannot drift (the palette is the editor's view of this spec).
        """
        return PortSpec(
            name=self.name,
            type=self.type,
            required=self.required and not self.has_default,
        )


class ConfigFieldSpec(BaseModel):
    """One config key a node accepts, with its expected JSON type.

    The palette documents the *shape* of a node's config (key names and value
    kinds) so the editor can offer the right fields; the validator enforces the
    per-node config byte cap and the engine reads the values.
    """

    model_config = ConfigDict(extra="forbid")

    name: NonBlankId
    kind: Literal["string", "int", "float", "bool", "object", "list", "any"] = "any"
    required: bool = False
    description: str = Field(default="", max_length=MAX_DOC_CHARS)


class PaletteNode(BaseModel):
    """One placeable node type: ports, config, eligibility and docs."""

    model_config = ConfigDict(extra="forbid")

    type: NodeType
    label: NonBlankId
    category: PaletteCategory
    description: str = Field(max_length=MAX_DOC_CHARS)
    input_ports: list[PalettePort] = Field(default_factory=list)
    output_ports: list[PalettePort] = Field(default_factory=list)
    #: Config keys the node consumes. The config *values* live with the node.
    config_schema: list[ConfigFieldSpec] = Field(default_factory=list)
    #: Capabilities the node needs enabled in the manifest to place it.
    required_capabilities: list[NonBlankId] = Field(default_factory=list)
    #: True when the node's input ports accept several inbound edges, which is
    #: what makes :class:`~workflow_engine.interfaces.JoinStrategy` applicable.
    join_eligible: bool = False
    #: True when the node's output type can feed its own input (a cycle may
    # close through it).
    loop_eligible: bool = False

    @field_validator("input_ports", "output_ports")
    @classmethod
    def _ports_bounded(cls, value: list[PalettePort]) -> list[PalettePort]:
        if len(value) > MAX_PORTS:
            raise ValueError(f"a node may declare at most {MAX_PORTS} ports")
        return value

    @model_validator(mode="after")
    def _unique_port_names(self) -> PaletteNode:
        for group, ports in (
            ("input", self.input_ports),
            ("output", self.output_ports),
        ):
            names = [p.name for p in ports]
            if len(set(names)) != len(names):
                raise ValueError(f"{group} port names must be unique")
        return self

    @model_validator(mode="after")
    def _eligibility_is_derived_not_asserted(self) -> PaletteNode:
        """Join eligibility must follow from the port shape, not a stale flag.

        The compiler demands a ``join_strategy`` only for nodes with two or more
        inbound edges, so a single-input node claiming join eligibility would
        let the editor offer a control the engine will never ask for.
        """
        multi_input = len(self.input_ports) > 1
        if multi_input and not self.join_eligible:
            raise ValueError(
                f"{self.type.value} declares {len(self.input_ports)} input "
                "ports but is not join_eligible; the editor would offer a node "
                "the compiler then refuses for an undeclared join"
            )
        if self.join_eligible and not multi_input:
            raise ValueError(
                f"{self.type.value} claims join eligibility with "
                f"{len(self.input_ports)} input port(s); it can never receive "
                "two inbound edges"
            )
        return self

    def port_specs(self) -> tuple[list[PortSpec], list[PortSpec]]:
        """(inputs, outputs) as the engine's own PortSpec objects."""
        return (
            [p.to_port_spec() for p in self.input_ports],
            [p.to_port_spec() for p in self.output_ports],
        )

    def config_names(self) -> list[str]:
        return [c.name for c in self.config_schema]


# --------------------------------------------------------------------------- #
# Capability manifest the palette validates against
# --------------------------------------------------------------------------- #

#: The manifest a shipped node must place cleanly under: every node type the
#: engine supports, and the capabilities the palette's nodes need.
PALETTE_MANIFEST = CapabilityManifest(
    supported_node_types=set(NodeType),
    enabled_capabilities={
        "llm",
        "retrieval",
        "http",
        "transform",
        "agent",
        "tool",
    },
)

#: The one manifest the editor may render against. A canvas must not invent a
#: second one: the palette's ``placeable()`` helper uses this exact object.
_PALETTE_MANIFEST = PALETTE_MANIFEST


class Palette(BaseModel):
    """The whole catalogue: ordered, unique, and complete."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[PaletteNode] = Field(default_factory=list)

    @field_validator("nodes")
    @classmethod
    def _unique_types(cls, value: list[PaletteNode]) -> list[PaletteNode]:
        seen = [n.type for n in value]
        if len(set(seen)) != len(seen):
            dupes = sorted({t.value for t in seen if seen.count(t) > 1})
            raise ValueError(f"palette declares a node type more than once: {dupes}")
        return value

    def by_type(self) -> dict[NodeType, PaletteNode]:
        """Lookup by node type — the canvas's primary access path."""
        return {n.type: n for n in self.nodes}

    def types(self) -> list[NodeType]:
        return [n.type for n in self.nodes]

    def categories(self) -> dict[PaletteCategory, list[PaletteNode]]:
        grouped: dict[PaletteCategory, list[PaletteNode]] = {}
        for node in self.nodes:
            grouped.setdefault(node.category, []).append(node)
        return grouped

    def placeable(self, node_type: NodeType) -> PaletteNode:
        """The entry for *node_type*, refusing unknown types.

        A canvas that asks for a type the palette does not carry gets a refusal
        rather than an empty panel that silently hides a placeable node.
        """
        try:
            return self.by_type()[node_type]
        except KeyError as exc:
            raise KeyError(f"palette has no entry for node type {node_type.value!r}") from exc

    def join_eligible_types(self) -> list[NodeType]:
        return [n.type for n in self.nodes if n.join_eligible]

    def loop_eligible_types(self) -> list[NodeType]:
        return [n.type for n in self.nodes if n.loop_eligible]


#: The catalogue. Every NodeType the engine supports, in palette order.
PALETTE = Palette(
    nodes=[
        PaletteNode(
            type=NodeType.TRIGGER,
            label="Trigger",
            category=PaletteCategory.FLOW,
            description=(
                "Starts a run with the workflow's initial input. Every workflow "
                "needs exactly one trigger node; it receives initial_input at "
                "run time, so its input ports are never required."
            ),
            input_ports=[],
            output_ports=[
                PalettePort(name="output", type="dict", description="The run's initial input")
            ],
            config_schema=[],
            required_capabilities=[],
            join_eligible=False,
            loop_eligible=False,
        ),
        PaletteNode(
            type=NodeType.RETRIEVER,
            label="Retriever",
            category=PaletteCategory.DATA,
            description=(
                "Fetches chunks from the workspace-scoped store. Retrieval is "
                "authorization-scoped before ranking (R-004): only chunks the "
                "caller's workspace may see are ever returned."
            ),
            input_ports=[
                PalettePort(
                    name="input", type="dict", required=True, description="The query payload"
                )
            ],
            output_ports=[
                PalettePort(name="output", type="list<dict>", description="The retrieved chunks")
            ],
            config_schema=[
                ConfigFieldSpec(name="chunk_ids", kind="list", description="Chunk ids to fetch"),
                ConfigFieldSpec(name="query", kind="string", description="Query text"),
                ConfigFieldSpec(name="top_k", kind="int", description="Maximum chunks returned"),
            ],
            required_capabilities=["retrieval"],
            join_eligible=False,
            loop_eligible=False,
        ),
        PaletteNode(
            type=NodeType.LLM,
            label="LLM",
            category=PaletteCategory.MODEL,
            description=(
                "Calls a model with the supplied prompt. Model origin is guarded "
                "(PD-005 v3): a non-approved foreign model is refused rather "
                "than silently substituted."
            ),
            input_ports=[
                PalettePort(
                    name="input", type="any", required=True, description="The prompt payload"
                ),
                PalettePort(
                    name="context", type="list<dict>?", description="Optional retrieved context"
                ),
            ],
            output_ports=[
                PalettePort(name="output", type="string", description="The model's answer")
            ],
            config_schema=[
                ConfigFieldSpec(name="system_prompt", kind="string", description="System prompt"),
                ConfigFieldSpec(name="model", kind="string", description="Approved model id"),
                ConfigFieldSpec(
                    name="temperature", kind="float", description="Sampling temperature"
                ),
                ConfigFieldSpec(
                    name="max_tokens", kind="int", description="Maximum tokens to emit"
                ),
            ],
            required_capabilities=["llm"],
            join_eligible=True,
            loop_eligible=False,
        ),
        PaletteNode(
            type=NodeType.AGENT,
            label="Agent",
            category=PaletteCategory.MODEL,
            description=(
                "Runs a scoped agent through the V3 runtime seam. Routed through "
                "the flagged-off adapter until the bridge's contract (T-V4-014) "
                "is wired: an agent node must never execute by accident."
            ),
            input_ports=[
                PalettePort(
                    name="input",
                    type="dict",
                    required=True,
                    description="The agent request",
                ),
                PalettePort(
                    name="context",
                    type="any",
                    required=False,
                    has_default=True,
                    description="Optional second inbound payload (a join port)",
                ),
            ],
            output_ports=[PalettePort(name="output", type="any", description="The agent's result")],
            config_schema=[
                ConfigFieldSpec(name="goal", kind="string", description="The agent's goal"),
                ConfigFieldSpec(name="prompt", kind="string", description="Prompt template"),
                ConfigFieldSpec(name="tools", kind="list", description="Permitted tool names"),
            ],
            required_capabilities=["agent"],
            join_eligible=True,
            loop_eligible=True,
        ),
        PaletteNode(
            type=NodeType.TOOL,
            label="Tool",
            category=PaletteCategory.EFFECT,
            description=(
                "Calls a registered tool. A tool the registry does not have, or "
                "one the caller is not permitted, is refused — never downgraded "
                "to a no-op."
            ),
            input_ports=[
                PalettePort(
                    name="input", type="any", required=True, description="The tool arguments"
                )
            ],
            output_ports=[PalettePort(name="output", type="any", description="The tool's result")],
            config_schema=[
                ConfigFieldSpec(
                    name="tool_name",
                    kind="string",
                    required=True,
                    description="Registered tool name",
                ),
                ConfigFieldSpec(
                    name="arguments", kind="object", description="Static tool arguments"
                ),
            ],
            required_capabilities=["tool"],
            join_eligible=False,
            loop_eligible=True,
        ),
        PaletteNode(
            type=NodeType.CONDITION,
            label="Condition",
            category=PaletteCategory.CONTROL,
            description=(
                "Routes on a boolean. The boolean leaves on the ``result`` port; "
                "the original payload continues on ``output``, so a downstream "
                "node keeps its input. Every outgoing edge from a condition "
                "must set condition_value (the validator refuses a null one)."
            ),
            input_ports=[
                PalettePort(
                    name="input", type="any", required=True, description="The value to test"
                )
            ],
            output_ports=[
                PalettePort(
                    name="result", type="boolean", description="The boolean outcome"
                ),
                PalettePort(name="output", type="any", description="The original payload"),
            ],
            config_schema=[
                ConfigFieldSpec(name="field", kind="string", description="Payload field to read"),
                ConfigFieldSpec(
                    name="expected", kind="any", description="Value to compare against"
                ),
            ],
            required_capabilities=[],
            join_eligible=False,
            loop_eligible=False,
        ),
        PaletteNode(
            type=NodeType.TRANSFORM,
            label="Transform",
            category=PaletteCategory.DATA,
            description=(
                "Reshapes or formats a payload with a template. No external "
                "capability: a transform must place under any manifest."
            ),
            input_ports=[
                PalettePort(
                    name="input", type="any", required=True, description="The payload to transform"
                )
            ],
            output_ports=[
                PalettePort(name="output", type="any", description="The transformed payload")
            ],
            config_schema=[
                ConfigFieldSpec(name="template", kind="string", description="Format template"),
                ConfigFieldSpec(name="key", kind="string", description="Payload key to extract"),
            ],
            required_capabilities=["transform"],
            join_eligible=False,
            loop_eligible=True,
        ),
        PaletteNode(
            type=NodeType.HTTP,
            label="HTTP",
            category=PaletteCategory.EFFECT,
            description=(
                "Calls an external HTTP endpoint. Credentials travel by "
                "REFERENCE (a vault id/name) — never by value; a bearer token or "
                "API key in the config is refused at the store boundary."
            ),
            input_ports=[
                PalettePort(
                    name="input", type="any", required=True, description="The request payload"
                )
            ],
            output_ports=[
                PalettePort(name="output", type="any", description="The response payload")
            ],
            config_schema=[
                ConfigFieldSpec(
                    name="url", kind="string", required=True, description="Endpoint URL"
                ),
                ConfigFieldSpec(name="method", kind="string", description="HTTP method"),
                ConfigFieldSpec(
                    name="headers",
                    kind="object",
                    description="Headers; credentials by reference only",
                ),
                ConfigFieldSpec(name="body", kind="any", description="Request body template"),
                ConfigFieldSpec(
                    name="credential_ref", kind="string", description="Vault id/name for auth"
                ),
            ],
            required_capabilities=["http"],
            join_eligible=False,
            loop_eligible=False,
        ),
        PaletteNode(
            type=NodeType.OUTPUT,
            label="Output",
            category=PaletteCategory.EFFECT,
            description=(
                "Emits the run's result. Its completion is also a "
                "completed-community boundary the checkpoint store resumes from "
                "(T-V4-016)."
            ),
            input_ports=[
                PalettePort(
                    name="input", type="any", required=True, description="The value to emit"
                )
            ],
            output_ports=[
                PalettePort(name="output", type="any", description="The emitted value")
            ],
            config_schema=[],
            required_capabilities=[],
            join_eligible=False,
            loop_eligible=False,
        ),
    ]
)

__all__ = [
    "MAX_CAPABILITIES",
    "MAX_CONFIG_KEYS",
    "MAX_DOC_CHARS",
    "MAX_LABEL_CHARS",
    "MAX_PORTS",
    "PALETTE",
    "PALETTE_MANIFEST",
    "ConfigFieldSpec",
    "Palette",
    "PaletteCategory",
    "PaletteNode",
    "PalettePort",
]
