# Infrastructure Docker notes (M0)

These notes cover the backing services in `docker-compose.yml`. Application
images live next to their code (`apps/api/Dockerfile`, `apps/worker/Dockerfile`);
the frontend image/service is owned by code2 under `apps/web`.

## Pinned images

| Service  | Image | Rationale |
|---|---|---|
| postgres | `postgres:16.15-alpine3.23` | Metadata + lexical FTS (PD-002). |
| redis    | `redis:7.4.11-alpine` | Worker broker/cache. |
| qdrant   | `qdrant/qdrant:v1.19.2` | Dense vector store (PD-002). |
| minio    | `elestio/minio@sha256:25348a...` | S3 originals. See caveat below. |

## MinIO caveat (escalation)

MinIO removed its official images from Docker Hub (Sept 2026) and Quay now
requires authentication for anonymous pulls; MinIO also reached EOL in Feb 2026.
The skeleton therefore uses a **digest-pinned MinIO repackage**
(`elestio/minio`) so `docker compose up` works locally. This is an interim
image choice, not an architecture change:

- Override with `MINIO_IMAGE=<your trusted pinned image>` in `.env`.
- Boss should open a component decision (PD) to either (a) use an internal
  registry mirror of a pinned MinIO release, or (b) replace the object store
  with a maintained S3-compatible project.
- The API only depends on the S3-compatible endpoint and MinIO's
  `/minio/health/live` health path, so a swap is isolated behind the future
  storage adapter.

## No model/parser services

PD-001/PD-002 model servers (embeddings/reranker/LLM) are intentionally **not**
part of M0; they attach later via environment variables.
