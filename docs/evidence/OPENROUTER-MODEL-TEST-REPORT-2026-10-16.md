# OpenRouter model smoke tests — 2026-10-16

Executed from the running Contabo API container using its existing OpenRouter credential, without printing or persisting the key. All inputs were synthetic and contained no user documents or personal/confidential information. Model/provider restrictions were explicit and automatic provider fallback was disabled, making each result attributable to its selected route. No production model configuration or data index was changed.

## Final availability and functional results

| Model / purpose | Free result | Paid result |
|---|---|---|
| Gemma 4 26B A4B generation | Google AI Studio returned upstream shared-pool HTTP 429 in both runs | CoreWeave HTTP 200; correct JSON, count 17, English and Romanian answers |
| Gemma semantic verification | Google AI Studio returned upstream shared-pool HTTP 429 in both runs | CoreWeave HTTP 200; supported claim accepted and changed-number claim rejected |
| Gemma vision | Not called: latest user instruction specifies paid vision | CoreWeave HTTP 200; correctly identified a synthetic red image |
| Gemini Embedding 2 | No free endpoint listed for the `:free` slug; no fabricated free counterpart tested | AI Studio batch: two finite, nonzero 3072-d vectors in correct index order; Vertex EU single: one valid 3072-d vector |
| Nemotron Rerank VL 1B V2 | NVIDIA HTTP 200; correct document ranked first for a Romanian question, relevance 0.980205 | Unqualified paid slug had zero listed endpoints; no paid service presumed or substituted |

The absence of catalogue endpoints means no usable route was discovered for that slug at the time; it does not prove that a paid service can never be offered. Free Gemma was tested but did not produce outputs, so its functional quality was not validated.

## Successful-call measurements

| Test | Provider | Wall time | Reported cost USD |
|---|---|---:|---:|
| Romanian/English generation | CoreWeave | 0.708 s | 0.00001870 |
| Semantic support verification | CoreWeave | 0.333 s | 0.00001300 |
| Vision color recognition | CoreWeave | 0.320 s | 0.00002910 |
| Two-input embeddings | Google AI Studio | 0.446 s | 0.00000360 |
| Single-input embedding | Google, requested `google-vertex/eu` | 0.277 s | 0.00000176 |
| Romanian reranking | NVIDIA | 0.244 s | 0.00000000 |
| **Successful-call reported total** | | | **0.00006616** |

These are one-call measurements, not averages, p95 estimates or capacity results. Errors returned no usage/cost field; the reported total is not a reconciled account-wide bill.

## First-run failures and targeted follow-up

1. Free Gemma generation and semantic verification returned 429 attributed to Google's upstream shared pool. The follow-up still returned 429. No success is claimed.
2. Paid Gemma text through Cloudflare also returned upstream 429. Changing the explicitly selected paid test provider to CoreWeave produced valid outputs; this does not prove unrestricted provider fallback is safe or implemented in the application.
3. Paid vision through Cloudflare returned 404 after OpenRouter filtered endpoints by image support. CoreWeave image input passed. Model-level image capability therefore cannot substitute for provider-level image admission.
4. Gemini embeddings under Vertex EU rejected a batch of two inputs, with routing metadata identifying the Vertex batch-support filter. Google AI Studio accepted that batch; Vertex EU accepted a single-input request. A Vertex-only deployment should split batches rather than fall through to an unapproved provider.
5. Embedding responses returned `gemini-embedding-2` while the request used `google/gemini-embedding-2`; add a narrowly defined alias to response identity validation.

## Exact tested payload policies

- Generation/verification: explicit free or paid Gemma slug; Google AI Studio for free; Cloudflare initially and CoreWeave in follow-up for paid; `temperature=0`, `max_tokens=192`, reasoning disabled. Text tests requested JSON output.
- Vision: paid Gemma, one inline 64×64 RGB red PNG, bounded output; no document OCR accuracy tested.
- Embeddings: paid Gemini slug with `dimensions=3072`; explicit Vertex EU or AI Studio route; one English and one Romanian synthetic sentence for the batch.
- Reranking: free Nemotron slug, explicit NVIDIA route, Romanian query and three synthetic passages, `top_n=3`. Response indices were unique/in-range and scores finite.
- Every request set `provider.only` and `allow_fallbacks=false`. Production privacy/ZDR constraints were not exercised by these public synthetic smoke tests.

## Interpretation for the implementation plan

Use Gemma free → approved paid for generation and Stage 12, with controlled 429 handling. The free shared pool cannot be the availability foundation for mandatory verification. Use a separately vetted image-capable paid Gemma route for vision. Keep Gemini at 3072 dimensions and respect endpoint batching constraints. Keep Nemotron as the only reranker.

NVIDIA's free listing prohibits confidential/personal inputs; these tests complied by using synthetic public data. A successful request does not authorize that endpoint for real sensitive documents. No approved paid Nemotron route was discovered, so sensitive requests need an explicit no-rerank or stop policy until one is verified.

Stage 11 was not modified or end-to-end tested here. Stage 12 tests exercised the remote model on one supported/unsupported pair; the actual application verification stage, schemas, evidence hydration and publication gate remain implementation work. Vision API success likewise does not enable application image inputs without changing its text-only message contract.

## Evidence files

- `openrouter-model-smoke-2026-10-16.json`: initial seven synthetic calls, including route-specific errors.
- `openrouter-model-smoke-followup-2026-10-16.json`: targeted follow-up on remaining unresolved routes.
- `openrouter-catalogue-2026-10-16.json`: model/provider endpoint snapshots and pricing/parameter metadata.

## References

- [Provider selection](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Gemma free](https://openrouter.ai/google/gemma-4-26b-a4b-it:free) and [paid](https://openrouter.ai/google/gemma-4-26b-a4b-it)
- [Gemini Embedding 2](https://openrouter.ai/google/gemini-embedding-2)
- [Nemotron free endpoint and data restrictions](https://openrouter.ai/nvidia/llama-nemotron-rerank-vl-1b-v2:free)
