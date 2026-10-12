# Finding 01: infinity-rerank port exposure

**Severity**: P0 (Security)
**Component**: `docker-compose.yml` — `infinity-rerank` service
**Location**: `docker-compose.yml:161`

## Description

The `infinity-rerank` service published its port `7997` on all host interfaces
(`0.0.0.0`), making the reranker model endpoint reachable from the LAN. All
other backing services (PostgreSQL, Redis, Qdrant, SeaweedFS/MinIO) were
already bound to `127.0.0.1` per remediation commit `95c593f`, but
infinity-rerank was missed.

## Fix Applied

Before:
```yaml
    ports:
      - "7997:7997"
```

After:
```yaml
    ports:
      - "127.0.0.1:7997:7997"
```

## Policy Alignment

Per `docker-compose.yml` service-exposure policy: backing services must bind to
loopback only. The API gateway (`apps/api`) is the sole entry point exposed to
the user-facing network.

## Verification

YAML parsed successfully. Port string confirmed as `127.0.0.1:7997:7997`.
