# Recommended RAG Architecture — Contabo Server + OpenRouter

**Updated: October 16, 2026**\
**Deployment:** Contabo hosts the RAG application and data infrastructure. All AI inference uses OpenRouter, with free endpoints first **when available and approved**, followed by paid endpoints when appropriate. NVIDIA DGX Spark is not required for this configuration.

> **Important:** Free-model availability, OpenRouter model IDs, provider support, pricing, policies, and rate limits can change. Validate them before deployment. A free endpoint is never a justification to bypass the non-China provider and privacy requirements.

## 1. Main RAG Pipeline

| Stage | Recommended model / tool | LLM | Location |
|---|---|---|---|
| 1. Document collection | Existing connectors | No | Contabo Server |
| 2. Original storage | SeaweedFS + PostgreSQL | No | Contabo Server |
| 3. Document parsing | Docling + IBM TableFormer | No | Contabo Server |
| 4. OCR | Tesseract (`ron+eng+por`) | No | Contabo Server |
| 5. Language detection | Lingua | No | Contabo Server |
| 6. Chunking | Docling HybridChunker | No | Contabo Server |
| 7. Embeddings | Google Gemini Embedding 2 | No | OpenRouter — paid |
| 8. Retrieval | Qdrant + PostgreSQL FTS + RRF | No | Contabo Server |
| 9. Reranking | NVIDIA Nemotron Rerank VL 1B V2 → Cohere Rerank 4 Fast | No | OpenRouter — free → paid |
| 10. Generation — Basic | Google Gemma 4 26B A4B IT | Yes | OpenRouter — free → paid |
| 10. Generation — Standard | Google Gemma 4 26B A4B IT | Yes | OpenRouter — free → paid |
| 10. Generation — Advanced | Google Gemma 4 26B A4B IT | Yes | OpenRouter — free → paid |
| 11. Citation verification | Application code + citation integrity validator | No | Contabo Server |
| 12. Semantic citation verification | Google Gemma 4 26B A4B IT | Yes | OpenRouter — free → paid |

**Model substitutions from the earlier DGX Spark setup:**

- **Google EmbeddingGemma 2** was the locally hosted proposal. Its exact model listing was not verified on OpenRouter. This architecture instead uses **Google Gemini Embedding 2** via OpenRouter (paid).
- **NVIDIA Llama Nemotron Rerank 1B v2** was the locally hosted proposal. This architecture uses the separately named **NVIDIA Nemotron Rerank VL 1B V2** free service via OpenRouter where eligible, falling back to **Cohere Rerank 4 Fast** for text.
- The main generative model remains **Google Gemma 4 26B A4B IT**, with free-first/paid-fallback routing.

## 2. Additional Layers

| Layer | Recommended model / tool | LLM | Location |
|---|---|---|---|
| V2. Knowledge graph | Google Gemma 4 26B A4B IT | Yes | OpenRouter — free → paid |
| V3. Agents / research | Google Gemma 4 26B A4B IT | Yes | OpenRouter — free → paid |
| V4. Workflows | Google Gemma 4 26B A4B IT | Yes | OpenRouter — free → paid |
| Vision | Google Gemma 4 26B A4B IT | Yes | OpenRouter — free → paid |
| Optional multimodal embeddings | Google Gemini Embedding 2 | No | OpenRouter — paid |

**Vision implementation note:** The application currently has a text-only message contract. The model's multimodal capability does not automatically create working vision ingestion. Extend the contract to carry images, page/figure identifiers, and citable metadata.

## 3. OpenRouter Model Routing and IDs

| Purpose | Primary | Fallback |
|---|---|---|
| Embeddings | `google/gemini-embedding-2` (paid) | Retry same model through another vetted provider, if available |
| Reranking | `nvidia/llama-nemotron-rerank-vl-1b-v2:free` | `cohere/rerank-4-fast` (paid, text) |
| Answer generation | `google/gemma-4-26b-a4b-it:free` | `google/gemma-4-26b-a4b-it` (paid) |
| Semantic verification | `google/gemma-4-26b-a4b-it:free` | `google/gemma-4-26b-a4b-it` (paid) |
| Graph extraction | `google/gemma-4-26b-a4b-it:free` | `google/gemma-4-26b-a4b-it` (paid) |
| Agents / workflows | `google/gemma-4-26b-a4b-it:free` | `google/gemma-4-26b-a4b-it` (paid) |
| Vision | `google/gemma-4-26b-a4b-it:free` | `google/gemma-4-26b-a4b-it` (paid) |

**Deployment rule:** Confirm exact model slugs and API support with the provider's current catalogue. Do not silently replace a configured model with a similarly named model.

## 4. Free-First / Paid-Fallback Rules

| Situation | Action |
|---|---|
| Free model available and approved for the data | Use free model first |
| Free model rate-limited (`429`) | Switch to an approved paid model |
| Free model unavailable (`5xx`) | Switch to an approved paid model |
| Free model times out | Switch to paid model under bounded retry/timeout policy |
| Free model returns malformed output | Validate and retry according to task policy; never accept invalid output |
| Confidential content and free endpoint is not approved | Skip free; route directly to approved paid provider (or reject request) |
| Safety refusal or invalid credentials | Do not blindly trigger fallback |
| Paid model also fails | Controlled error or safe degraded result, with logging/alerting |
| No approved non-China provider available | Fail closed; do not bypass restrictions |

**Implementation:** OpenRouter supports an ordered fallback list for generative chat requests. For stricter handling (e.g. not retrying after refusals), control fallback in the Contabo application/gateway. Reranking and embeddings use their own adapters and failure policies.

## 5. OpenRouter API Configuration

| Service | API endpoint | Model / routing |
|---|---|---|
| Embeddings | `POST /api/v1/embeddings` | Gemini Embedding 2 — paid |
| Reranking | `POST /api/v1/rerank` | Nemotron Free → Cohere Paid |
| Answer generation | `POST /api/v1/chat/completions` | Gemma Free → Paid |
| Agents / workflows | `POST /api/v1/chat/completions` | Gemma Free → Paid |
| Semantic verification | `POST /api/v1/chat/completions` | Gemma Free → Paid |
| Vision | `POST /api/v1/chat/completions` | Gemma Free → Paid, multimodal payload |

### Illustrative free-first LLM routing request

```json
{
  "models": [
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-26b-a4b-it"
  ],
  "messages": [
    {
      "role": "user",
      "content": "Answer using the supplied evidence."
    }
  ],
  "provider": {
    "data_collection": "deny",
    "zdr": true,
    "require_parameters": true
  }
}
```

**Caution:** This sample illustrates routing and privacy filters, but **does not include the required non-China provider allowlist**. Configure a vetted `provider.only` list or an equivalent policy. Filters can make the free model unavailable; if so, use an approved paid endpoint or fail closed.

## 6. Citation Verification

### Stage 11 — Deterministic citation verification

**Execution:** Contabo Server · No LLM · Every answer

| Validation | Implementation |
|---|---|
| Citation ID verification | Application code |
| Document existence | PostgreSQL + SeaweedFS |
| Page number verification | Docling metadata |
| Workspace access control | Application permissions |
| Document version checks | PostgreSQL |
| Exact quotation matching | Application code |
| Missing citation markers | Deterministic coverage checks |
| Unreadable/omitted pages | Parsing/OCR metadata |

A deterministic coverage check can find absent citation markers, but cannot by itself prove that a cited passage supports every factual statement.

### Stage 12 — Semantic citation verification

**Execution:** OpenRouter · Gemma 4 26B A4B IT · Free → Paid, only on approved endpoints

| Validation | Implementation |
|---|---|
| Claim-level evidence verification | Gemma 4 |
| Contradiction detection | Gemma 4 |
| Unsupported statements | Gemma 4 |
| Partial claim support | Gemma 4 |
| Evidence-to-claim alignment | Gemma 4 |
| Answer correction | Controlled regeneration |
| Verification classification | Supported / Partially supported / Unsupported |

Run Stage 11 for every response. Run Stage 12 selectively for complex, multi-document, or high-risk answers. Model-based semantic verification is an additional check, not proof of correctness.

## 7. Indicative OpenRouter Pricing

**Price snapshot from the discussed architecture (October 16, 2026); verify before deployment.**

| Model | Listed cost |
|---|---|
| Google Gemini Embedding 2 | $0.20 / 1M text input tokens |
| NVIDIA Nemotron Rerank VL 1B V2 — Free | Free, subject to limits and privacy terms |
| Cohere Rerank 4 Fast | $0.002 / search |
| Google Gemma 4 26B A4B IT — Free | Free, subject to limits and privacy terms |
| Google Gemma 4 26B A4B IT — Paid | $0.042 / 1M input tokens; $0.22 / 1M output tokens |

The previously noted OpenRouter free-model limit was generally 50 requests/day, or up to 1,000/day after at least $10 in purchased credits, with additional rate limits. Verify current limits rather than relying on this as a service-level commitment.

Track paid fallbacks, per-workspace spending, provider used, model used, tokens, rerank charges, errors, and latency.

## 8. Infrastructure Responsibilities

| Infrastructure | Responsibilities |
|---|---|
| **Contabo Server** | API, authentication, collection, SeaweedFS, PostgreSQL, Docling, Tesseract OCR, Lingua, chunking, Qdrant, FTS, RRF, permissions, orchestration, citation integrity, logs |
| **OpenRouter — Embeddings** | Google Gemini Embedding 2 |
| **OpenRouter — Reranking** | NVIDIA Nemotron Rerank Free → Cohere Paid (text) |
| **OpenRouter — Generative LLM** | Google Gemma 4 26B A4B IT Free → Paid; generation, graph, agents, workflows, vision, semantic verification |
| **NVIDIA DGX Spark** | No models needed in this deployment; optional future local/private fallback |

## 9. End-to-End Data Flow

```text
DOCUMENT INGESTION
Documents / Connectors
         |
         v
Contabo: SeaweedFS + PostgreSQL
         |
         v
Contabo: Docling -> selective Tesseract OCR -> Lingua
         |
         v
Contabo: Docling HybridChunker
         |
         v
OpenRouter: Google Gemini Embedding 2 (paid)
         |
         v
Contabo: Qdrant vector index + PostgreSQL FTS

QUERY / ANSWERING
User question
         |
         v
Contabo: Auth, workspace permissions, query processing
         |
         v
OpenRouter: Gemini Embedding 2 (same model/configuration)
         |
         v
Contabo: Qdrant + FTS -> RRF
         |
         v
OpenRouter: Nemotron Rerank Free -> Cohere Paid
         |
         v
OpenRouter: Gemma 4 26B A4B IT Free -> Paid
         |
         v
Contabo: Deterministic citation integrity validator
         |
         v
OpenRouter: Optional Gemma semantic verification Free -> Paid
         |
         v
Contabo: Grounded answer with validated citations
```

Image-based documents require an additional page/figure extraction branch, an image-capable message contract, and citable figure identifiers. Image reranking cannot assume a text-only fallback is equivalent to the multimodal reranker.

## 10. Production Requirements

| Area | Recommended configuration |
|---|---|
| Embedding dimensions | Gemini Embedding 2 at 3,072 dimensions |
| Existing Qdrant index | Reuse only if model, dimensions, preprocessing, and input configuration match |
| Query embeddings | Exactly the same embedding model/settings as document indexing |
| Embedding failure | Retry same embedding model through approved provider; never silently change vector model |
| Reranker failure | Approved paid Cohere fallback; optionally proceed without reranking if both fail |
| Image reranking | Separate image-capable handling; Cohere Rerank 4 Fast is text-only |
| LLM fallback | Free Gemma → paid Gemma, only for approved providers and eligible failures |
| Output validation | Schema, evidence IDs, workspace permissions, citations |
| Sensitive data | Privacy-reviewed provider only, respecting retention/location/processing requirements |
| Non-China restriction | Approved model families, lineage, inference hosting providers, and fail-closed routing |
| Monitoring | Per-request provider/model selection, latency, cost, token usage, errors, fallbacks |
| DGX Spark | Unused for AI inference in this OpenRouter version; available if requirements change |

### Data privacy and no-China compliance

1. **Model allowlist:** Only approved non-Chinese-origin model families and specific model versions.
2. **Provider allowlist:** Independently verify each actual inference host, not just OpenRouter itself.
3. **Provenance:** Exclude Chinese foundation models and derivatives if that is the organization's policy.
4. **Data handling:** Enforce acceptable training/data collection, retention, jurisdiction, and security terms; require ZDR where needed.
5. **Free endpoint restrictions:** Do not send confidential/personal data to free endpoints with unacceptable logging or retention. The NVIDIA free reranking endpoint has previously warned against submitting confidential/personal data.
6. **Fallback safety:** Never allow unrestricted routing to an unknown provider, a China-based provider, or an unapproved model.
7. **Failure behavior:** If all approved endpoints are unavailable, fail closed.

## 11. Final Deployment Summary

| Infrastructure | Model / service | Routing |
|---|---|---|
| Contabo Server | RAG application, storage, ingestion, retrieval, verification | Local |
| OpenRouter | Google Gemini Embedding 2 | Paid |
| OpenRouter | NVIDIA Nemotron Rerank VL 1B V2 | Free, only for permitted data |
| OpenRouter | Cohere Rerank 4 Fast | Paid text reranking fallback / confidential text, if approved |
| OpenRouter | Google Gemma 4 26B A4B IT | Free → Paid, subject to provider/privacy policy |
| NVIDIA DGX Spark | No models required | Optional |

**Final decision:** Keep RAG infrastructure on Contabo; route all three AI model *roles* (embeddings, reranking, generation) through OpenRouter. Use a free endpoint when it genuinely exists and is compliant; otherwise use a paid, approved endpoint. Preserve the existing Gemini Embedding 2 vector configuration and the strict no-China provider restrictions.

## Relevant Reference Links

- [OpenRouter](https://openrouter.ai/)
- [OpenRouter documentation](https://openrouter.ai/docs)
- [OpenRouter — provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Google Gemini Embedding 2](https://openrouter.ai/google/gemini-embedding-2)
- [Google Gemma 4 26B A4B IT — free listing](https://openrouter.ai/google/gemma-4-26b-a4b-it:free)
- [Google Gemma 4 26B A4B IT — paid listing](https://openrouter.ai/google/gemma-4-26b-a4b-it)
- [NVIDIA Nemotron Rerank VL 1B V2 — free listing](https://openrouter.ai/nvidia/llama-nemotron-rerank-vl-1b-v2:free)
- [Cohere Rerank 4 Fast listing](https://openrouter.ai/cohere/rerank-4-fast)

> Reference links are included to help validate current availability and terms; they do not guarantee that a model or a particular privacy-compliant hosting provider remains available.
