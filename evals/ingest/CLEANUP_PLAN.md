# Eval ingest cleanup plan

- Work in a scratch workspace only; ingest outputs under a tmp collection.
- Net-zero: drop temp collection and local fixtures after run; prove zero residual rows.
- No production data touched.
