"""Acceptance checks through real Toolframe Core worker processes.

These tests are opt-in because they build immutable plugin wheels and launch a
fresh managed application for every test.  Run them with
``TOOLFRAME_AUDIT_CORE_TESTS=1``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import tomlkit
from toolframe_sdk import PluginError, Principal

pytestmark = pytest.mark.skipif(
    os.environ.get("TOOLFRAME_AUDIT_CORE_TESTS") != "1",
    reason="real Core acceptance tests require TOOLFRAME_AUDIT_CORE_TESTS=1",
)

try:
    from toolframe_core.api import create_app
    from toolframe_core.config import ConfigFile, atomic_write
    from toolframe_core.host import Host
except ImportError as error:  # pragma: no cover - exercised only in incomplete envs
    pytest.skip(f"Toolframe Core is unavailable: {error}", allow_module_level=True)


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
WHEELHOUSE = ROOT / "build" / "wheels"


def _wheel(prefix: str, wheelhouse: Path = WHEELHOUSE) -> Path:
    normalized = prefix.replace("-", "_")
    if not normalized.startswith("toolframe_"):
        normalized = f"toolframe_{normalized}"
    candidates = sorted(
        wheelhouse.glob(f"{normalized}-*.whl"),
        key=lambda item: item.stat().st_mtime_ns,
    )
    if not candidates:
        raise AssertionError(f"No wheel matching {prefix!r} in {wheelhouse}")
    return candidates[-1]


@pytest.fixture(scope="session")
def wheelhouse() -> Path:
    """Build exact local wheels so Core workers never use a live checkout."""

    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("uv is required for real Core acceptance tests")
    WHEELHOUSE.mkdir(parents=True, exist_ok=True)
    sdk_source = Path(
        os.environ.get(
            "TOOLFRAME_AUDIT_SDK_SOURCE",
            str(WORKSPACE / "toolframe-sdk"),
        )
    )
    sources = (
        sdk_source,
        WORKSPACE / "toolframe-sql-sqlite",
        WORKSPACE / "toolframe-auth",
        WORKSPACE / "toolframe-dashboard-auth",
        ROOT,
        ROOT / "examples" / "outbox-producer",
    )
    for source in sources:
        result = subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(WHEELHOUSE)],
            cwd=source,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            pytest.fail(
                f"Could not build {source.name}:\n{result.stdout}\n{result.stderr}"
            )
    for prefix in (
        "toolframe-sdk",
        "toolframe-sql-sqlite",
        "toolframe-auth",
        "toolframe-dashboard-auth",
        "toolframe-audit",
        "toolframe-audit-example",
    ):
        assert _wheel(prefix).is_file()
    return WHEELHOUSE


async def operate(host: Host, action: str, **params: Any) -> dict[str, Any]:
    operation = await host.submit(action, params)
    result = await host.wait_operation(operation["id"])
    assert result["status"] == "succeeded", result
    return result


async def start_host(home: Path, wheelhouse: Path) -> Host:
    config = ConfigFile(home)
    if config.path.exists():
        composition, _ = config.read()
    else:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        composition = config.initialize(host="127.0.0.1", port=port)
        composition.packages.find_links = [str(wheelhouse)]
        composition.packages.offline = True
        composition.runtime.health_interval = 0.2
        atomic_write(config.path, tomlkit.dumps(composition.model_dump()))
    host = await Host.create(home)
    await host.start()
    return host


async def install(host: Host, wheelhouse: Path, name: str) -> None:
    await operate(
        host, "add", id=name, source=str(_wheel(name, wheelhouse)), enable=True
    )


async def install_base(host: Host, wheelhouse: Path) -> None:
    await install(host, wheelhouse, "sql-sqlite")
    await install(host, wheelhouse, "audit")
    await install(host, wheelhouse, "audit-example")


@pytest_asyncio.fixture
async def core_host(tmp_path: Path, wheelhouse: Path):
    host = await start_host(tmp_path / "application", wheelhouse)
    try:
        await install_base(host, wheelhouse)
        yield host
    finally:
        await host.close()


def _principal(
    *scopes: str,
    kind: str = "password",
    user_id: str | None = "acceptance-user",
    client_id: str | None = "acceptance-client",
) -> Principal:
    return Principal(
        kind=kind,
        user_id=user_id,
        client_id=client_id,
        scopes=scopes,
    )


async def audit_list(
    host: Host, *, principal: Principal | None = None, **filters: Any
) -> dict[str, Any]:
    return await host.call_service(
        "audit", "audit.events", "list_events", filters, principal=principal
    )


async def create_record(host: Host, record_id: str, *, principal: Principal) -> dict:
    return await host.call_service(
        "audit-example",
        "audit-example.records",
        "create_record",
        {"record_id": record_id, "label": f"label-{record_id}"},
        principal=principal,
    )


async def drain(host: Host) -> dict:
    # ``drain`` is an internal producer method.  Leaving principal unset is
    # deliberate: the collector must record the admitted delivery actor as
    # background when a finite drain is run from a task/root.
    return await host.call_service(
        "audit-example", "audit-example.records", "drain", {}, principal=None
    )


async def wait_until(predicate, *, timeout: float = 10) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("condition did not become true before timeout")


def _probe_source(directory: Path) -> Path:
    """Create a tiny real producer used to prove source fencing and dedup."""

    source = directory / "audit-probe"
    package = source / "probe"
    package.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        """[build-system]
requires=["hatchling>=1.27,<2"]
build-backend="hatchling.build"
[project]
name="audit-probe"
version="0.1.0"
dependencies=["toolframe-sdk==0.4.0"]
[project.entry-points."toolframe.plugins"]
probe="probe:plugin"
[tool.hatch.build.targets.wheel]
packages=["probe"]
"""
    )
    (package / "__init__.py").write_text(
        """from toolframe_sdk import AuthorizationSpec, MethodSpec, Plugin, PluginDefinition, Requirement, ServiceSpec

plugin = PluginDefinition(
    id="probe",
    factory="probe:Probe",
    authorization=AuthorizationSpec(),
    requires={"audit": Requirement(service="audit.events")},
    services={"probe.events": ServiceSpec(methods={"emit": MethodSpec(audience="internal")})},
)

class Probe(Plugin):
    async def invoke(self, service, method, params):
        if service != "probe.events" or method != "emit":
            raise RuntimeError("unknown probe method")
        return await self.context.call("audit", "ingest", {"event": params["event"]})
"""
    )
    return source


def _event(event_id: str, *, action: str = "record.created") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "occurred_at": "2026-01-01T00:00:00Z",
        "action": action,
        "entity_type": "record",
        "entity_id": event_id,
        "metadata": {"source": "core-acceptance"},
        "source_attested_actor": {"kind": "user", "user_id": "declared"},
    }


@pytest.mark.asyncio
async def test_core_outbox_background_preserves_original_actor(core_host: Host):
    caller = _principal(
        "audit-example.write", user_id="original-user", client_id="cli-1"
    )
    await create_record(core_host, "background-1", principal=caller)

    queued = await core_host.call_service(
        "audit-example",
        "audit-example.records",
        "drain_background",
        {},
        principal=caller,
    )
    assert queued == {"status": "queued"}

    page = await wait_until(lambda: _page_with_event(core_host, "background-1"))
    row = next(item for item in page["events"] if item["entity_id"] == "background-1")
    assert row["application_id"] == core_host.composition.application.id
    assert row["source_owner"] == "audit-example"
    assert row["source_generation"]
    assert row["source_attested_actor"] == {
        "kind": "password",
        "user_id": "original-user",
        "client_id": "cli-1",
    }
    assert row["delivery_actor"] == {"kind": "background"}
    assert row["metadata"] == {}


async def _page_with_event(host: Host, identifier: str) -> dict[str, Any] | None:
    """Return a page only after the expected event has been delivered."""

    page = await audit_list(host, principal=Principal(kind="operator"), limit=100)
    return (
        page
        if any(
            row["event_id"] == identifier or row["entity_id"] == identifier
            for row in page["events"]
        )
        else None
    )


@pytest.mark.asyncio
async def test_core_replay_after_lost_ack_deduplicates(core_host: Host, monkeypatch):
    caller = _principal("audit-example.write", user_id="replay-user")
    await create_record(core_host, "replayed", principal=caller)

    original_invoke = core_host.invoke
    lost = False

    async def lose_ack(root, target, service, method, payload, **kwargs):
        nonlocal lost
        result = await original_invoke(root, target, service, method, payload, **kwargs)
        if target == "audit" and method == "ingest" and not lost:
            lost = True
            raise PluginError("simulated_ack_failure", "acknowledgment failed")
        return result

    monkeypatch.setattr(core_host, "invoke", lose_ack)
    with pytest.raises(PluginError) as error:
        await drain(core_host)
    assert error.value.code == "simulated_ack_failure"
    monkeypatch.undo()

    replay = await drain(core_host)
    assert replay == {"delivered": 1, "acknowledged": 1, "limit": 25}
    page = await audit_list(core_host, principal=Principal(kind="operator"))
    assert [row["entity_id"] for row in page["events"]] == ["replayed"]


@pytest.mark.asyncio
async def test_core_source_fence_dedup_conflict_and_public_ingest_rejection(
    core_host: Host, tmp_path: Path, wheelhouse: Path
):
    source = _probe_source(tmp_path)
    await operate(core_host, "add", id="probe", source=str(source), enable=True)
    event = _event("same-id")
    accepted = await core_host.call_service(
        "probe", "probe.events", "emit", {"event": event}
    )
    duplicate = await core_host.call_service(
        "probe", "probe.events", "emit", {"event": event}
    )
    assert accepted["status"] == "accepted"
    assert duplicate["status"] == "duplicate"
    page = await audit_list(core_host, principal=Principal(kind="operator"))
    assert page["events"][0]["source_owner"] == "probe"
    assert page["events"][0]["application_id"] == core_host.composition.application.id

    changed = {**event, "action": "record.deleted"}
    with pytest.raises(PluginError) as error:
        await core_host.call_service(
            "probe", "probe.events", "emit", {"event": changed}
        )
    assert error.value.code == "audit_event_conflict"

    forged = {**event, "source_owner": "other-plugin"}
    with pytest.raises(PluginError) as error:
        await core_host.call_service("probe", "probe.events", "emit", {"event": forged})
    assert error.value.code == "handler_failed"
    assert "source_owner" in error.value.message

    with pytest.raises(PluginError) as error:
        await core_host.call_service(
            "audit",
            "audit.events",
            "ingest",
            {"event": _event("direct-audit")},
            principal=Principal(kind="operator"),
        )
    assert error.value.code == "audit_untrusted_context"


@pytest.mark.asyncio
async def test_core_reload_disable_restart_and_keyset_filters(
    core_host: Host, wheelhouse: Path
):
    caller = _principal("audit-example.write", user_id="filter-user")
    for record_id in ("first", "second", "third"):
        await create_record(core_host, record_id, principal=caller)
    assert (await drain(core_host))["acknowledged"] == 3

    first_page = await audit_list(
        core_host, principal=Principal(kind="operator"), limit=2
    )
    assert first_page["count"] == 2
    assert first_page["next_before"] is not None
    second_page = await audit_list(
        core_host,
        principal=Principal(kind="operator"),
        limit=2,
        before=first_page["next_before"],
    )
    assert second_page["count"] == 1 and second_page["next_before"] is None
    filtered = await audit_list(
        core_host,
        principal=Principal(kind="operator"),
        source_owner="audit-example",
        action="record.created",
        entity_type="record",
        entity_id="second",
        since="2025-12-01T00:00:00Z",
        until="2027-01-01T00:00:00Z",
    )
    assert [row["entity_id"] for row in filtered["events"]] == ["second"]
    with pytest.raises(PluginError) as error:
        await audit_list(core_host, principal=Principal(kind="operator"), limit=101)
    assert error.value.code == "handler_failed"
    assert "maximum of 100" in error.value.message

    await operate(core_host, "reload", id="audit")
    retained = await audit_list(core_host, principal=Principal(kind="operator"))
    assert retained["count"] == 3
    await operate(core_host, "disable", id="audit")
    with pytest.raises(PluginError):
        await audit_list(core_host, principal=Principal(kind="operator"))
    await operate(core_host, "enable", id="audit")
    retained = await audit_list(core_host, principal=Principal(kind="operator"))
    assert retained["count"] == 3

    home = core_host.home
    await core_host.close()
    reopened = await start_host(home, wheelhouse)
    try:
        retained = await audit_list(reopened, principal=Principal(kind="operator"))
        assert retained["count"] == 3
    finally:
        await reopened.close()


@pytest_asyncio.fixture
async def scoped_host(tmp_path: Path, wheelhouse: Path):
    host = await start_host(tmp_path / "application", wheelhouse)
    try:
        await install_base(host, wheelhouse)
        await install(host, wheelhouse, "auth")
        await install(host, wheelhouse, "dashboard-auth")
        yield host
    finally:
        await host.close()


async def _password(host: Host, password: str, scopes: list[str]) -> dict[str, Any]:
    from toolframe_core.access import Identity

    return await host.access.manage(
        Identity(local=True),
        "create",
        {
            "label": password,
            "password": password,
            "scopes": scopes,
            "expected_revision": host.config.revision,
        },
    )


def _client(host: Host) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            create_app(host, manage_lifespan=False),
            client=("192.0.2.10", 54321),
        ),
        base_url="http://127.0.0.1:1",
    )


async def _login(host: Host, password: str) -> httpx.AsyncClient:
    client = _client(host)
    response = await client.post(
        "/_toolframe/auth/login",
        json={"password": password},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200, response.text
    return client


@pytest.mark.asyncio
async def test_core_http_scope_separation_and_explicit_retention(scoped_host: Host):
    caller = _principal("audit-example.write", user_id="retention-user")
    await create_record(scoped_host, "retained", principal=caller)
    assert (await drain(scoped_host))["acknowledged"] == 1

    base = "/_toolframe/v1/services/audit/audit.events/"
    anonymous = _client(scoped_host)
    try:
        assert (await anonymous.post(base + "list_events", json={})).status_code == 200
        assert (
            await anonymous.post(
                base + "prune_events", json={"cutoff": "2999-01-01T00:00:00Z"}
            )
        ).status_code == 401
    finally:
        await anonymous.aclose()

    read_password = "read-pass-1"
    manage_password = "manage-pass-1"
    await _password(scoped_host, read_password, ["audit.read"])
    await _password(scoped_host, manage_password, ["system.plugins.manage"])
    assert (await scoped_host.access.mode())["mode"] == "protected"
    read_client = await _login(scoped_host, read_password)
    manage_client = await _login(scoped_host, manage_password)
    try:
        response = await read_client.post(base + "list_events", json={})
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert (
            await read_client.post(
                base + "prune_events", json={"cutoff": "2999-01-01T00:00:00Z"}
            )
        ).status_code == 403
        assert (
            await read_client.post(
                base + "ingest", json={"event": _event("public-ingest")}
            )
        ).status_code == 403
        assert (
            await read_client.post("/api/audit/audit.events/list_events", json={})
        ).status_code == 403

        preview = await manage_client.post(
            base + "prune_events",
            json={"cutoff": "2999-01-01T00:00:00Z", "limit": 100, "preview": True},
        )
        assert preview.status_code == 200
        preview_body = preview.json()
        assert preview_body["matched_count"] == 1
        applied = await manage_client.post(
            base + "prune_events",
            json={
                "cutoff": preview_body["cutoff"],
                "limit": preview_body["limit"],
                "preview": False,
                "through_sequence": preview_body["through_sequence"],
            },
        )
        assert applied.status_code == 200
        assert applied.json()["pruned_count"] == 1
        assert (await read_client.post(base + "list_events", json={})).json()[
            "count"
        ] == 0
    finally:
        await read_client.aclose()
        await manage_client.aclose()
