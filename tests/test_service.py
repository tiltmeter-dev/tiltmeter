from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from driftwatch.core.models import ChangeKind, Severity
from driftwatch.errors import DriftwatchError, UnknownEndpointError
from driftwatch.service import NO_API_KEY, DriftService
from tests.helpers import (
    API,
    FakeClock,
    FakeExplainer,
    RecordingNotifier,
    make_endpoint,
)
from tests.service_kit import make_service

URL = f"{API}/v1/items"
BEFORE = {"data": [{"id": 1, "phone": "555"}], "has_more": False}
AFTER = {"data": [{"id": 1, "phone_number": "555"}], "has_more": False}


@respx.mock
async def test_first_check_records_the_baseline() -> None:
    respx.get(URL).respond(200, json=BEFORE)
    explainer, notifier = FakeExplainer(), RecordingNotifier()
    async with make_service(explainer=explainer, notifier=notifier) as service:
        result = await service.check_endpoint("items")
        assert result.outcome == "baseline"
        assert result.check_id == 1
        assert result.status_code == 200
        assert result.changes == []
        assert explainer.requests == []
        assert notifier.sent == []
        assert service.get_check(1) == result


@respx.mock
async def test_drift_is_explained_persisted_and_notified() -> None:
    route = respx.get(URL).respond(200, json=BEFORE)
    explainer, notifier = FakeExplainer(), RecordingNotifier()
    clock = FakeClock()
    async with make_service(explainer=explainer, notifier=notifier, clock=clock) as service:
        await service.check()
        route.respond(200, json=AFTER)
        clock.advance(minutes=5)
        [result] = await service.check()
        assert result.outcome == "drift"
        assert result.max_severity is Severity.BREAKING
        assert [c.kind for c in result.changes] == [
            ChangeKind.FIELD_REMOVED,
            ChangeKind.FIELD_ADDED,
        ]
        assert result.changes[0].hint == "possibly renamed to $.data[*].phone_number"
        assert result.explanation == explainer.result
        assert result.explanation_model == "fake-model"
        assert result.checked_at == "2026-10-20T12:05:00Z"
        [request] = explainer.requests
        assert request.affected[0].path == "$.data[*]"
        assert notifier.sent == [result]
        stored = service.get_check(result.check_id or 0)
        assert stored is not None
        assert stored.explanation == explainer.result

        # Reported once: the new shape is now part of the baseline.
        clock.advance(minutes=5)
        again = await service.check_endpoint("items")
        assert again.outcome == "clean"
        assert len(explainer.requests) == 1


@respx.mock
async def test_fetch_errors_are_recorded_and_optionally_notified() -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("refused"))
    quiet, loud = RecordingNotifier(), RecordingNotifier(on_error=True)
    async with make_service(notifier=quiet) as service:
        result = await service.check_endpoint("items")
        assert result.outcome == "error"
        assert result.error is not None
        assert "ConnectError" in result.error
        assert service.history()[0].error == result.error
    assert quiet.sent == []
    async with make_service(notifier=loud) as service:
        await service.check_endpoint("items")
    assert len(loud.sent) == 1


@respx.mock
async def test_explanations_are_skipped_or_fail_softly() -> None:
    route = respx.get(URL).respond(200, json=BEFORE)

    async def drift(service: DriftService, **kwargs: bool) -> object:
        route.respond(200, json=BEFORE)
        await service.check_endpoint("items", **kwargs)
        route.respond(200, json=AFTER)
        return await service.check_endpoint("items", **kwargs)

    explainer = FakeExplainer()
    async with make_service(explainer=explainer) as service:
        result = await drift(service, explain=False)
        assert result.explanation is None  # type: ignore[attr-defined]
        assert explainer.requests == []

    async with make_service(detect_explainer=True) as service:  # no ANTHROPIC_API_KEY
        result = await drift(service)
        assert result.explanation is None  # type: ignore[attr-defined]
        assert result.notes == [NO_API_KEY]  # type: ignore[attr-defined]

    failing = FakeExplainer(error=RuntimeError("rate limited"))
    async with make_service(explainer=failing) as service:
        result = await drift(service)
        assert result.outcome == "drift"  # type: ignore[attr-defined]
        assert result.notes == ["LLM explanation failed: rate limited"]  # type: ignore[attr-defined]

    disabled = FakeExplainer()
    async with make_service(explainer=disabled, llm={"enabled": False}) as service:
        await drift(service)
        assert disabled.requests == []


@respx.mock
async def test_info_only_changes_are_not_explained_or_notified() -> None:
    route = respx.get(URL).respond(200, json={"id": 1})
    explainer, notifier = FakeExplainer(), RecordingNotifier()
    async with make_service(explainer=explainer, notifier=notifier) as service:
        await service.check()
        route.respond(200, json={"id": 1, "extra": True})
        result = await service.check_endpoint("items")
        assert result.outcome == "changed"
    assert explainer.requests == []
    assert notifier.sent == []


@respx.mock
async def test_notification_failures_become_notes() -> None:
    route = respx.get(URL).respond(200, json=BEFORE)
    notifier = RecordingNotifier(fail=True)
    async with make_service(notifier=notifier) as service:
        await service.check()
        route.respond(200, json=AFTER)
        result = await service.check_endpoint("items")
        assert result.notes == ["recording notification failed: boom"]
        stored = service.get_check(result.check_id or 0)
        assert stored is not None
        assert stored.notes == result.notes
        route.respond(200, json=BEFORE)
        silent = await service.check_endpoint("items", notify=False)
        assert silent.notes == []


@respx.mock
async def test_many_endpoints_keep_their_order() -> None:
    for name in ("a", "b", "c"):
        respx.get(f"{API}/{name}").respond(200, json={"name": name})
    endpoints = [make_endpoint(id=name, url=f"{API}/{name}") for name in ("c", "a", "b")]
    async with make_service(*endpoints, concurrency=2) as service:
        results = await service.check()
        assert [r.endpoint_id for r in results] == ["c", "a", "b"]
        assert [r.endpoint_id for r in await service.check(["b"])] == ["b"]
        with pytest.raises(UnknownEndpointError):
            await service.check(["nope"])


@respx.mock
async def test_retention_is_applied_but_never_below_the_baseline_window() -> None:
    respx.get(URL).respond(200, json={"id": 1})
    async with make_service(make_endpoint(baseline_window=5), keep_checks=10) as service:
        for _ in range(13):
            await service.check()
        assert len(service.history(limit=100)) == 10
    async with make_service(make_endpoint(baseline_window=12), keep_checks=10) as service:
        for _ in range(13):
            await service.check()
        assert len(service.history(limit=100)) == 12


@respx.mock
async def test_drift_status_schema_and_endpoints() -> None:
    route = respx.get(URL).respond(200, json=BEFORE)
    clock = FakeClock()
    async with make_service(clock=clock) as service:
        never = service.drift_status("items")
        assert not never.drifted
        assert "never been checked" in never.summary
        assert service.schema("items").json_schema is None
        assert service.endpoints()[0].last_outcome is None

        await service.check()
        clock.advance(hours=1)
        route.respond(200, json=AFTER)
        await service.check()

        clock.advance(hours=2)  # drift happened at 13:00, it is now 15:00
        status = service.drift_status("items")
        assert status.drifted
        assert status.max_severity is Severity.BREAKING
        assert status.since == "2026-10-13T15:00:00Z"
        assert status.changes[0].kind is ChangeKind.FIELD_REMOVED
        assert "worst breaking" in status.summary
        assert service.drift_status("items", since="30m").drifted is False
        assert service.drift_status("items", since="3h").drifted is True

        report = service.schema("items")
        assert report.baseline_checks == 2
        assert report.observations == 2
        assert report.json_schema is not None
        assert report.json_schema["title"] == "items"

        [summary] = service.endpoints()
        assert (summary.last_outcome, summary.last_severity) == ("drift", Severity.BREAKING)
        assert summary.url == URL

        assert [c.path for c in service.changes("items", since="3h")] == [
            "$.data[*].phone_number",
            "$.data[*].phone",
        ]
        assert service.changes(min_severity=Severity.BREAKING)[0].path == "$.data[*].phone"

        route.mock(side_effect=httpx.ConnectError("down"))
        await service.check()
        assert "The last check failed" in service.drift_status("items").summary


@respx.mock
async def test_explain_check_later() -> None:
    route = respx.get(URL).respond(200, json=BEFORE)
    explainer = FakeExplainer()
    async with make_service(explainer=explainer) as service:
        first = await service.check_endpoint("items", explain=False)
        route.respond(200, json=AFTER)
        drifted = await service.check_endpoint("items", explain=False)
        explained = await service.explain_check(drifted.check_id or 0)
        assert explained.explanation == explainer.result
        assert explainer.requests[0].affected[0].before == {"id": "integer", "phone": "string"}
        stored = service.get_check(drifted.check_id or 0)
        assert stored is not None
        assert stored.explanation_model == "fake-model"
        with pytest.raises(DriftwatchError, match="no check"):
            await service.explain_check(999)
        with pytest.raises(DriftwatchError, match="no changes"):
            await service.explain_check(first.check_id or 0)
    async with make_service(detect_explainer=True) as service:
        await service.check()
        route.respond(200, json=BEFORE)
        await service.check()
        route.respond(200, json={"id": 1})
        [result] = await service.check(explain=False)
        with pytest.raises(DriftwatchError, match="ANTHROPIC_API_KEY"):
            await service.explain_check(result.check_id or 0)


@respx.mock
async def test_from_config_file(tmp_path: Path) -> None:
    respx.get(URL).respond(200, json={"id": 1})
    config_path = tmp_path / "driftwatch.yaml"
    config_path.write_text(f"endpoints:\n  - id: items\n    url: {URL}\n")
    service = DriftService.from_config_file(config_path, detect_explainer=False)
    async with service:
        result = await service.check_endpoint("items")
        assert result.outcome == "baseline"
    assert (tmp_path / ".driftwatch" / "driftwatch.db").exists()
