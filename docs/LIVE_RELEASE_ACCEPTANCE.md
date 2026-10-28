# Live release acceptance

`tests/fixtures/scripts/release_journey.sh` drives a real browser against a
running release. It signs in, selects an authorized workspace, uploads the six
committed PDF/DOCX/HTML/CSV/Markdown/TXT fixtures, and observes each upload's own
job reach `ready` through the UI's polling request. It then requires a verified
answer citing the new TXT document, opens its stored source passage, checks
hybrid/semantic/keyword search results, and checks selector keyboard focus and
polite announcements. It deletes only documents created by its unique run.

The stack must have working object storage, ingestion workers, embedding and
answer providers. Install Playwright in a separate QA directory if it is not
already available; `PLAYWRIGHT_MODULE` can point to that installation's
`node_modules/playwright/index.mjs`. Set `CHROME_PATH` to an installed browser,
or install the browser using that Playwright installation. Neither dependency
installation nor the browser should run on the shared host while its resource
guard has paused heavy jobs. Run on an approved compute host with access to the
release, or through `bin/herdr-heavy-run` when the local guard admits it.

Prepare a mode-0600 JSON credential file containing `email` and `password` for
an account with upload permission. Pass its path through `CREDENTIAL_FILE`;
never pass credentials on the command line. Configure these environment values:

| Variable | Value |
| --- | --- |
| `RUN_RELEASE_E2E` | `1` |
| `BASE_URL` | Release web origin; defaults to `http://127.0.0.1:3001` |
| `WORKSPACE_ID` | An existing workspace UUID accessible to the QA account |
| `CREDENTIAL_FILE` | Absolute path to the private credential file |
| `PLAYWRIGHT_MODULE` | Optional absolute module path for a separate QA install |
| `CHROME_PATH` | Optional installed Chromium/Chrome executable |
| `RELEASE_TIMEOUT_MS` | Optional per-step timeout, default 180000, maximum 900000 |
| `RELEASE_REPORT_FILE` | Optional path for a JSON outcome report |

With those environment values set, run:

```bash
bash tests/fixtures/scripts/release_journey.sh
```

Success emits `RELEASE_JOURNEY_BROWSER_OK` with six ready formats, three search
modes, citation-passage and keyboard outcomes. Failure reports only the stage
and exception type to avoid disclosing credentials in browser call logs. A
cleanup failure also fails the gate. The opt-in pytest entry point is
`tests/integration/test_release_journey_e2e.py`.

Syntax checks and mocked browser regressions do not satisfy this live gate.
The multilingual answer and debugger/CLI parity tickets require their separate
live acceptance evidence as well.
