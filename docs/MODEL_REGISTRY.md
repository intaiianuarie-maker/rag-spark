# Model Registry + ModelConfig (M7-001a)

## Provider registry

`packages/models/registry.py` exposes `ModelRegistry` with:

- `register(model, provider | **kwargs)` — register an LLM/embedding provider for a model name.
- `resolve(model)` — return the provider or raise `UnknownModelError`.
- `capability_query(capability)` / `models_with_capability(capability)` — list models supporting a capability.
- `list_models(kind=None)` — list registered models.

Providers implement the existing seams:
- `LLMProvider` (`packages/models/interfaces.py`): `generate(request: GenerationRequest) -> GenerationResult`, `stream(request)`.
- `EmbeddingProvider` (`packages/retrieval/interfaces.py`): `embed_documents(texts)`, `embed_query(text)`.

`OpenAICompatProvider` (`packages/models/providers/openai_compat.py`) is an
OpenAI-compatible HTTP implementation (chat + embeddings, no vendor SDK).

## Per-workspace ModelConfig CRUD

HTTP: `/v1/workspaces/{workspace_id}/model-configs` (create/list/get/patch/delete).

Fields: `name`, `preferred_model`, `override_model`, `routing_rules`,
`rate_limit_policy`, `verify_uniform_cost`, `sensitive`, `extra_models`.

Persistence: `model_configs` table (migration `0012_model_configs`), workspace-isolated via FK + authz (`workspace.read`/`workspace.manage`).

## Configuration

- `WORKSPACE_MODEL_CONFIG` env var (JSON keyed by workspace_id) seeds routing rules. Durable CRUD rows are persisted and readable via the API; runtime precedence wiring is NOT yet implemented (tracked separately).
- Provider credentials via environment only (never in rows/logs).
