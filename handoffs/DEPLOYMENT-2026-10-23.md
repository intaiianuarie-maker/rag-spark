# Deployment — 2026-10-23

Application release: `494886e9e36b510fb5ccf2cd86c13343a60b79d7`.
Web and API are online on ports 3001 and 8000. PostgreSQL, Redis, Qdrant,
object storage, and existing named volumes were preserved.

The API and worker now share the durable agent store and four workflow SQLite
stores. All five files retained their device/inode identities across container
recreation. `/ready` reports ready and all workflow store modes persistent;
`/version` reports the application release SHA. API autoreload is disabled.
The worker mounts the immutable reviewed source snapshot read-only; the API
uses a packaged source image. Existing dependency images were preserved.

Frontend production failures were fixed by moving extra entry-module exports
into ordinary modules and adding a Suspense boundary for the run-id client
component. Next.js and eslint-config-next were patched to 16.4.3. See the
[official advisory](https://github.com/vercel/next.js/security/advisories/GHSA-2464-fhr4-w9xw)
and [release notes](https://github.com/vercel/next.js/releases/tag/v16.4.3).

Verification:

- [Release workflow 40027583283](https://github.com/intaiianuarie-maker/rag-spark/actions/runs/40027583283)
  passed installation, production dependency audit (zero reported
  vulnerabilities), lint, all 242 tests with zero skips, and the production
  build. Chrome is required so browser regressions cannot silently skip.
- The downloaded standalone artifact's SHA256 and embedded source revision
  were verified. It runs as UID 1000 on the existing pinned Node 20.20.2
  runtime, with a read-only application mount and a separate writable cache.
- All 13 live smoke checks passed: readiness, release identity, four page
  requests, login/HttpOnly cookies, account scope, authenticated proxy,
  refresh, proxy after refresh, logout, and rejection after logout.
- Model-origin CI now installs its required python-dotenv 1.2.4 parser and
  runs baseline plus selftest. Model-origin, frontend, and worker-import CI
  jobs pass. Full backend CI still fails its pre-existing vendor SDK import
  guard in rerank/graph/ingestion modules, also reproduced on b11ca54. This
  unrelated architecture check was not weakened or represented as green.

Credential incident and remaining dependency:

A private deployment configuration was mistakenly included in reviewer scope;
the reviewer printed credential values before the no-read correction arrived.
The JWT secret was rotated with the project's rotation mechanism, revoking
17 sessions and their refresh families. Old tokens now return 401 and fresh
login succeeds. Users must sign in again. The database and object-store values
in that output were existing shipped defaults; those services were preserved.

The exposed OpenRouter key was removed from runtime configuration. It must be
revoked in OpenRouter and replaced through a private file on the host; the user
has been asked for that file path. **Model requests remain unavailable until
this is completed.** Readiness verifies gateway reachability, not successful
authenticated model execution, so a green `/ready` does not close this item.
No credential values are included in this record or committed to Git.

Operator files are under `/root/code/deployments/knowledge-os/20261023-b11ca54/`
(directory mode 0700; credential-bearing configurations mode 0600):

- `compose.private.json` is the active application deployment configuration.
- `previous-healthy.private.json` is the previous healthy application rollout
  configuration, preserving the newly rotated JWT and disabled provider key.
- `postgres-before.private.dump` is the pre-rollout PostgreSQL backup.
- `artifact-manifest-final.json`, `final-smoke.json`, `shared-store-identity.json`,
  `jwt-rotation.json`, and `web-release-final-ci.log` contain verification.
- Initial inspection snapshots and retired preparation scripts are historical
  evidence. Do not regenerate configuration from them or restore revoked keys.

Use the active deployment configuration when operating these application
containers. Do not use `--remove-orphans` or `down -v`: backing services are
intentionally outside this application-only Compose file.

Four co-tenant Dockerfile/requirements edits and untracked page-teeth-r3.tsx
were excluded from every commit and release. Herdr sessions were preserved;
all heavy local checks used the host guard. Final build/test work ran on
GitHub-hosted runners when the local guard refused admission.
