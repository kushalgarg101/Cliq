from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import parcelpilot.main as main_module
from parcelpilot.agent import Agent
from parcelpilot.db import connect
from parcelpilot.main import app, settings
from parcelpilot.services import (
    _parse_agreement_rules,
    confirm_action,
    lookup_operations,
    prepare_action,
    scan_issue_signals,
    search_knowledge,
)


@pytest.fixture(autouse=True)
def app_uses_offline_agent(monkeypatch):
    """Keep request tests hermetic even when a developer has a live key in .env."""
    offline_settings = replace(settings, llm_mode="offline", llm_api_key=None)
    monkeypatch.setattr(
        main_module,
        "agent",
        Agent(offline_settings, offline_settings.source_db, offline_settings.runtime_db),
    )


def sign_in(client: TestClient, username: str) -> str:
    response = client.post(
        "/login",
        data={"username": username, "password": "parcelpilot-demo"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    csrf_token = client.cookies.get("pp_csrf")
    assert csrf_token
    return csrf_token


def actor(username: str) -> dict:
    with connect(settings.runtime_db) as db:
        return dict(db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())


def test_login_rejects_invalid_password():
    client = TestClient(app)
    response = client.post("/login", data={"username": "northstar", "password": "wrong"})
    assert response.status_code == 401


def test_greeting_is_helpful_without_retrieval_or_escalation():
    client = TestClient(app)
    csrf_token = sign_in(client, "northstar")
    response = client.post(
        "/api/chat",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "hello"},
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "deterministic"
    assert "Hello" in response.json()["answer"]
    assert response.json()["events"] == []


def test_health_check_is_ready_when_the_pack_is_ingested():
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "data_ready": True}


def test_customer_cannot_access_operations_insights():
    client = TestClient(app)
    sign_in(client, "northstar")
    assert client.get("/api/insights").status_code == 403


def test_chat_rejects_missing_csrf_token():
    client = TestClient(app)
    sign_in(client, "northstar")
    response = client.post("/api/chat", json={"message": "Can I cancel ORD-1001?"})
    assert response.status_code == 403


def test_customer_cannot_query_another_account_order():
    client = TestClient(app)
    csrf_token = sign_in(client, "northstar")
    response = client.post(
        "/api/chat",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "Can I cancel ORD-2001?"},
    )
    assert response.status_code == 403


def test_northstar_contract_overrides_default_cancellation_fee():
    client = TestClient(app)
    csrf_token = sign_in(client, "northstar")
    response = client.post(
        "/api/chat",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "Can Northstar cancel ORD-1001 without a cancellation fee?"},
    )
    body = response.json()
    assert response.status_code == 200
    assert "Fee: 0 INR" in body["answer"]
    assert body["mode"] == "offline"
    assert any(event["tool"] == "evaluate_order" for event in body["events"])
    assert all(event["tool"] != "prepare_action" for event in body["events"])


def test_spaced_order_reference_is_normalised_before_evaluation():
    client = TestClient(app)
    csrf_token = sign_in(client, "northstar")
    response = client.post(
        "/api/chat",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "Can Northstar cancel ord -1001 without a fee?"},
    )
    assert response.status_code == 200
    assert "Fee: 0 INR" in response.json()["answer"]
    assert any(event["tool"] == "evaluate_order" for event in response.json()["events"])

def test_lumenworks_credit_uses_contract_threshold_and_amount():
    client = TestClient(app)
    csrf_token = sign_in(client, "lumenworks")
    response = client.post(
        "/api/chat",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "Is ORD-2002 eligible for a service credit?"},
    )
    assert response.status_code == 200
    assert "Credit amount: 300 INR" in response.json()["answer"]


def test_agreement_rules_are_parsed_from_contract_text_not_known_account_ids():
    rules = _parse_agreement_rules(
        "Example Enterprise Agreement.pdf",
        """Account: ACCT-777 Status: ACTIVE
        Support terms: P1: 10 minutes, 24x7 P2: 45 minutes P3: 1 business day
        Customer may cancel any BOOKED shipment before pickup with no cancellation fee.
        If a pickup is more than 6 hours late, the carrier is at fault, and the customer is not at fault,
        the customer receives a fixed INR 450 service credit. This clause replaces the default credit rule.""",
        "ACCT-777",
    )
    assert rules["cancellation_fee_waiver_before_pickup"] is True
    assert rules["failed_pickup_credit"] == {"threshold_minutes": 360, "amount_inr": 450}
    assert rules["support_targets"]["P1"] == {"text": "10 minutes, 24x7", "minutes": 10, "is_24x7": True}
    assert _parse_agreement_rules("mismatch.pdf", "Account: ACCT-777 Status: ACTIVE", "ACCT-778") == {}


def test_provider_receives_one_corrective_round_when_it_omits_retrieval(monkeypatch):
    def response(content=None, tool_calls=None):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))])

    responses = iter(
        [
            response(tool_calls=[SimpleNamespace(id="evaluate", function=SimpleNamespace(name="evaluate_order", arguments='{"order_id":"ORD-1001"}'))]),
            response(content="Northstar can cancel the order."),
            response(tool_calls=[SimpleNamespace(id="sources", function=SimpleNamespace(name="search_knowledge", arguments='{"query":"cancellation fee","account_id":"ACCT-001"}'))]),
            response(content="Northstar can cancel before pickup."),
        ]
    )

    class FakeCompletions:
        @staticmethod
        def create(**_kwargs):
            return next(responses)

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("parcelpilot.agent.OpenAI", FakeOpenAI)
    result = Agent(settings, settings.source_db, settings.runtime_db)._provider_reply(
        "Can Northstar cancel ORD-1001?",
        actor("northstar"),
        [],
    )
    assert result["mode"] == "provider"
    assert [event["tool"] for event in result["events"]] == ["evaluate_order", "search_knowledge"]
    assert "Evidence:" in result["answer"]


def test_provider_explains_precollected_sources_in_one_model_call(monkeypatch):
    calls = []

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            calls.append(kwargs)
            message = SimpleNamespace(content="P1 is the highest support severity.", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("parcelpilot.agent.OpenAI", FakeOpenAI)
    result = Agent(settings, settings.source_db, settings.runtime_db).reply(
        "What is the current standard support response target for a P1 incident?",
        actor("maya"),
    )
    assert result["mode"] == "provider"
    assert [event["tool"] for event in result["events"]] == ["search_knowledge"]
    assert "tools" not in calls[0]
    assert "tool_choice" not in calls[0]
    assert "Evidence:" in result["answer"]


def test_p1_ticket_uses_account_specific_target_and_can_prepare_escalation():
    client = TestClient(app)
    csrf_token = sign_in(client, "maya")
    response = client.post(
        "/api/chat",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "Please escalate TKT-501 immediately."},
    )
    body = response.json()
    assert response.status_code == 200
    assert "P1" in body["answer"]
    assert "15 minutes, 24x7" in body["answer"]
    pending = next(event["result"] for event in body["events"] if event["tool"] == "prepare_action")
    confirmation = client.post(f"/api/actions/{pending['pending_action_id']}/confirm", headers={"X-CSRF-Token": csrf_token})
    assert confirmation.status_code == 200
    assert confirmation.json()["status"] == "confirmed"


def test_business_hours_are_not_invented_for_lumenworks_sla():
    client = TestClient(app)
    csrf_token = sign_in(client, "maya")
    response = client.post(
        "/api/chat",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "Has TKT-502 breached its SLA?"},
    )
    assert response.status_code == 200
    assert "does not define business hours" in response.json()["answer"]


def test_staff_customer_name_query_retrieves_the_matching_agreement():
    client = TestClient(app)
    csrf_token = sign_in(client, "maya")
    response = client.post(
        "/api/chat",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "What support terms apply to Northstar Logistics?"},
    )
    body = response.json()
    assert response.status_code == 200
    evidence = next(event["result"] for event in body["events"] if event["tool"] == "search_knowledge")
    assert any(source["filename"] == "05_Northstar_Logistics_Enterprise_Agreement.pdf" for source in evidence["sources"])


def test_logout_requires_csrf_and_revokes_session():
    client = TestClient(app)
    csrf_token = sign_in(client, "northstar")
    assert client.post("/logout", data={"csrf_token": "invalid"}).status_code == 403
    assert client.post("/logout", data={"csrf_token": csrf_token}, follow_redirects=False).status_code == 303
    assert client.get("/api/me").status_code == 401


def test_chat_enforces_server_side_message_limit():
    client = TestClient(app)
    csrf_token = sign_in(client, "northstar")
    response = client.post("/api/chat", headers={"X-CSRF-Token": csrf_token}, json={"message": "a" * 4001})
    assert response.status_code == 422


def test_chat_rejects_non_string_messages():
    client = TestClient(app)
    csrf_token = sign_in(client, "northstar")
    response = client.post("/api/chat", headers={"X-CSRF-Token": csrf_token}, json={"message": ["ORD-1001"]})
    assert response.status_code == 422
    assert response.json()["detail"] == "Message must be a string."


def test_knowledge_search_excludes_deprecated_policy():
    result = search_knowledge(settings.source_db, actor("northstar"), "support response targets", "ACCT-001")
    assert result["sources"]
    assert all(source["status"] == "current" for source in result["sources"])
    assert all("DEPRECATED" not in source["filename"] for source in result["sources"])


def test_action_cannot_cross_customer_scope_or_bypass_confirmation():
    northstar = actor("northstar")
    with pytest.raises(PermissionError):
        prepare_action(
            settings.source_db,
            settings.runtime_db,
            northstar,
            "create_escalation",
            {"order_id": "ORD-2001", "reason": "Attempted cross-account escalation"},
        )
    pending = prepare_action(
        settings.source_db,
        settings.runtime_db,
        northstar,
        "create_follow_up",
        {"order_id": "ORD-1001", "reason": "Customer requested cancellation review"},
    )
    lumenworks = actor("lumenworks")
    with pytest.raises(PermissionError):
        confirm_action(settings.source_db, settings.runtime_db, lumenworks, pending["pending_action_id"])
    confirmed = confirm_action(settings.source_db, settings.runtime_db, northstar, pending["pending_action_id"])
    assert confirmed["status"] == "confirmed"


def test_customer_ticket_result_excludes_internal_assignment_and_historical_resolution():
    ticket = lookup_operations(settings.source_db, actor("northstar"), ticket_id="TKT-501")["ticket"]
    assert "assigned_to" not in ticket
    assert "historical_resolution" not in ticket
    assert ticket["historical_resolution_authority"] == "context_only_untrusted"


def test_customer_order_result_excludes_internal_fault_assessment_and_notes():
    order = lookup_operations(settings.source_db, actor("northstar"), order_id="ORD-1001")["order"]
    assert "carrier_fault" not in order
    assert "customer_fault" not in order
    assert "notes" not in order


def test_staff_tools_reject_mixed_account_references():
    with pytest.raises(ValueError, match="different accounts"):
        lookup_operations(settings.source_db, actor("maya"), order_id="ORD-1001", ticket_id="TKT-502")
    with pytest.raises(ValueError, match="same account"):
        prepare_action(
            settings.source_db,
            settings.runtime_db,
            actor("maya"),
            "create_escalation",
            {"order_id": "ORD-1001", "ticket_id": "TKT-502", "reason": "Cross-account action attempt"},
        )


def test_question_about_an_action_does_not_prepare_one():
    client = TestClient(app)
    csrf_token = sign_in(client, "northstar")
    response = client.post(
        "/api/chat",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "Can I create a follow-up for ORD-1001?"},
    )
    assert response.status_code == 200
    assert all(event["tool"] != "prepare_action" for event in response.json()["events"])


def test_explicit_cancellation_request_prepares_confirmable_human_follow_up():
    client = TestClient(app)
    csrf_token = sign_in(client, "northstar")
    response = client.post(
        "/api/chat",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "Please cancel ORD-1001."},
    )
    assert response.status_code == 200
    pending = next(event["result"] for event in response.json()["events"] if event["tool"] == "prepare_action")
    assert pending["action_type"] == "create_follow_up"
    assert pending["requires_confirmation"] is True

def test_cancellation_request_with_pickup_word_still_uses_cancellation_decision():
    client = TestClient(app)
    csrf_token = sign_in(client, "northstar")
    response = client.post(
        "/api/chat",
        headers={"X-CSRF-Token": csrf_token},
        json={"message": "Can I cancel ORD-1001 before pickup?"},
    )
    assert response.status_code == 200
    assert response.json()["answer"].startswith("Cancellation decision")


def test_operations_signals_include_sla_and_recurring_issue_context():
    signals = scan_issue_signals(settings.source_db, actor("opslead"))["signals"]
    assert any(signal.get("ticket_id") == "TKT-501" and "already breached" in signal["reason"] for signal in signals)
    assert any(signal.get("label") == "Recurring issue: bulk upload failures" for signal in signals)


def test_home_has_csp_without_an_inline_config_script():
    client = TestClient(app)
    sign_in(client, "northstar")
    response = client.get("/")
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "window.PP=" not in response.text
    assert 'data-csrf="' in response.text
    if settings.llm_mode != "offline" and settings.llm_api_key:
        assert "Ask about an order, policy, ticket, service credit, or known issue." in response.text
    else:
        assert "Limited fallback is active" in response.text
