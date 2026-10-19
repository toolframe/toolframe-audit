# Verification guide

This guide describes reproducible package checks for contributors. Run them
from the repository root with Python 3.14 or later. The commands below use the
project's `.venv`; generated wheels and reports stay under ignored `build/`,
`dist/` or `.local/` directories. Until the packages are published, use matching
sibling checkouts for local development:

## Package checks

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python --no-sources --group dev \
  -e . ../toolframe-sdk ../toolframe-sql-sqlite ../toolframe-core ../fastapi-cordis
uv pip install --python .venv/bin/python 'streamlit>=1.61,<2'
.venv/bin/pytest
.venv/bin/ruff check .
uv build --out-dir dist
```

The test suite should exercise a real SQLite storage provider for persistence,
transactions, limits, filters, deduplication, conflicting IDs, owner
isolation, retention, and outbox replay. It should also cover packaged SQL,
schemas, public exports, and the independently packaged outbox-producer
fixture. Keep historical test counts and environment-specific results in local
working notes rather than turning them into support claims.

## Clean-wheel smoke check

Build into a clean output directory, install the wheel into a fresh temporary
environment, and import only the public package surface:

```sh
uv build --wheel --out-dir build/wheels ../toolframe-sdk
uv build --out-dir dist
.venv/bin/python scripts/verify_wheel.py \
  dist/toolframe_audit-0.1.0-py3-none-any.whl --find-links build/wheels
```

The smoke script inspects wheel metadata and assets, then creates an isolated
temporary environment and imports the installed public package and entry point.
It verifies that development-only fixture sources remain outside the wheel.

## Optional local Core acceptance

Integrated acceptance requires matching local Core, SDK, and storage-provider
checkouts. These are local-development commands only; they do not describe a
published dependency URL or a release test result. From the Audit repository,
install the sibling sources in editable form and use a fresh temporary
application directory and dynamically selected ports:

```sh
TOOLFRAME_AUDIT_CORE_TESTS=1 .venv/bin/pytest -q tests/test_core.py
```

The Core tests install immutable wheels and exercise service admission and
lifecycle in disposable applications. Page tests use the real Streamlit test
runtime with a simulated public service bridge. Full installed Dashboard and
MCP flows require the corresponding providers in the acceptance application.
The read tool is named `audit__list_events` and the service
route is `/_toolframe/v1/services/audit/audit.events/list_events`:

1. install the built Audit wheel and a SQL storage provider into a fresh app;
2. enable Audit and verify that its migration persists through reload, disable,
   and restart;
3. invoke `audit.events/list_events` with `audit.read` and verify pagination,
   exact filters, empty results, the 512,000-byte response bound, and denied
   scopes;
4. run the Dashboard and inspect bounded rows, errors, JSONL page export, and
   the `system.plugins.manage` preview/apply retention flow;
5. run an independently built producer that commits a business mutation with
   `enqueue`, drain it through the internal dependency, and simulate an
   acknowledgment failure to verify safe replay.

Do not use existing shared applications or fixed ports for this check. Record
the exact package revisions and environment in local notes when reporting an
acceptance result. PostgreSQL migration syntax is packaged, but PostgreSQL
runtime behavior remains a separate verification target for this release.
