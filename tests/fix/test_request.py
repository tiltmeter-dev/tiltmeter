from __future__ import annotations

import pytest
import respx

from tests.fix.kit import billing_endpoint, rename_check, rename_request
from tests.helpers import API, explanation
from tests.service_kit import make_service
from tiltmeter.core.models import Change, ChangeKind, CheckResult, RenameHint, Severity
from tiltmeter.errors import TiltmeterError
from tiltmeter.fix.prompts import render_fix_request
from tiltmeter.fix.request import build_fix_request, build_targets, field_name, search_terms


def test_targets_follow_renames_and_skip_what_code_cannot_fix() -> None:
    check = rename_check().model_copy(
        update={
            "changes": [
                *rename_check().changes,
                Change(
                    path="$",
                    kind=ChangeKind.STATUS_CHANGED,
                    severity=Severity.BREAKING,
                    before="200",
                    after="404",
                ),
            ]
        }
    )
    targets = build_targets(check)
    assert [(t.path, t.kind) for t in targets] == [
        ("$.phone", ChangeKind.FIELD_REMOVED),
        ("$.balance", ChangeKind.TYPE_CHANGED),
    ]
    phone, balance = targets
    assert (phone.field, phone.renamed_to, phone.new_field) == (
        "phone",
        "$.phone_number",
        "phone_number",
    )
    assert (balance.before, balance.after, balance.renamed_to) == ("integer", "string", None)


def test_claude_s_renames_fill_in_when_the_diff_had_no_hint() -> None:
    check = CheckResult(
        endpoint_id="billing_customers",
        check_id=4,
        checked_at="t",
        changes=[
            Change(
                path="$.data[*].tel",
                kind=ChangeKind.FIELD_REMOVED,
                severity=Severity.BREAKING,
                before="string",
            )
        ],
        explanation=explanation(
            probable_renames=[
                RenameHint(before_path="$.data[*].tel", after_path="$.data[*].contact.phone")
            ]
        ),
    )
    [target] = build_targets(check)
    assert target.renamed_to == "$.data[*].contact.phone"
    assert (target.field, target.new_field) == ("tel", "phone")


def test_field_names() -> None:
    assert field_name("$.data[*].phone") == "phone"
    assert field_name('$["odd key"]') == "odd key"
    assert field_name("$.items[*]") is None
    assert field_name("$") is None
    assert field_name("not a path") is None


def test_search_terms_skip_placeholders() -> None:
    request = rename_request()
    assert request.search_terms == ["phone", "balance", "api.billing.test", "customers"]
    hidden_host = billing_endpoint(url="${BILLING_URL}/v1/accounts")
    assert search_terms(hidden_host, request.targets) == ["phone", "balance", "accounts"]
    by_id = billing_endpoint(url="https://api.billing.test/v2/customers/12/")
    assert search_terms(by_id, [])[-1] == "customers"
    assert search_terms(billing_endpoint(url="https://api.billing.test/v1"), []) == [
        "api.billing.test"
    ]


def test_the_request_never_carries_secrets() -> None:
    request = rename_request()
    assert request.url == "https://api.billing.test/v1/customers"
    text = request.model_dump_json() + render_fix_request(request)
    assert "BILLING_KEY" not in text
    assert "api_key" not in text


def test_prompt_lists_changes_renames_and_search_terms() -> None:
    text = render_fix_request(rename_request())
    assert "endpoint: billing_customers" in text
    assert "request: GET https://api.billing.test/v1/customers" in text
    assert '"renamed_to": "$.phone_number"' in text
    assert '"kind": "type_changed"' in text
    assert "summary: The phone field was renamed." in text
    assert 'Search terms: "phone", "balance", "api.billing.test", "customers".' in text


def test_unsaved_checks_are_rejected() -> None:
    check = rename_check().model_copy(update={"check_id": None})
    with pytest.raises(ValueError, match="not been saved"):
        build_fix_request(billing_endpoint(), check, baseline=None, observed=None)


@respx.mock
async def test_service_builds_the_request_from_history() -> None:
    url = f"{API}/v1/items"
    route = respx.get(url).respond(200, json={"data": [{"id": 1, "phone": "555"}]})
    async with make_service() as service:
        await service.check()
        route.respond(200, json={"data": [{"id": 1, "phone_number": "555"}]})
        [drifted] = await service.check()
        assert drifted.check_id is not None
        request = service.fix_request(drifted.check_id)
        assert [t.renamed_to for t in request.targets] == ["$.data[*].phone_number"]
        [affected] = request.affected
        assert affected.path == "$.data[*]"
        assert affected.before == {"id": "integer", "phone": "string"}
        assert affected.after == {"id": "integer", "phone_number": "string"}
        with pytest.raises(TiltmeterError, match="no check with id 99"):
            service.fix_request(99)
