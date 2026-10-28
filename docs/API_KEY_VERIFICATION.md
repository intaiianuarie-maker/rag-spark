# API-key verification policy (PD-007)

API-key authentication performs one prefix lookup and one Argon2 verification
before rejecting a candidate. Eligible, active keys use their stored hash;
unknown, malformed, revoked and expired candidates use a process-initialized
dummy hash generated with the same password-hasher settings. A dummy
verification result can never authenticate a request.

This policy is mandatory for API-key authentication. Workspace model-routing
configuration does not disable it. Existing key material, prefix extraction,
JWT dispatch, creator activity checks and generic 401 responses remain intact.

The policy removes the dominant prefix-existence timing difference caused by
skipping Argon2. It does not promise identical request durations: database
latency and historical hash parameters can vary. No artificial sleeps or
custom cryptography are used, and secrets/hashes are never logged.

Regressions cover unknown and malformed inputs, revoked and expired keys,
incorrect and correct secrets, real Argon2 controls and public rejection
semantics. They assert verification work directly rather than relying on noisy
wall-clock thresholds. Focused verification: 14 passed; lint/types clean.
