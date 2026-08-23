from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .db import connect

SNAPSHOT_FORMAT = "%Y-%m-%d %H:%M"
SNAPSHOT_TIMEZONE = ZoneInfo("Asia/Kolkata")
CUSTOMER_ACTIONS = {"create_escalation", "create_follow_up"}
STAFF_ACTIONS = CUSTOMER_ACTIONS | {"update_ticket"}
VALID_ROLES = {"customer", "support_agent", "operations_lead"}


def _allowed_account(actor: dict[str, Any], account_id: str) -> None:
    if actor.get("role") not in VALID_ROLES:
        raise PermissionError("Your role is not authorised to access ParcelPilot data.")
    if actor["role"] == "customer" and actor["account_id"] != account_id:
        raise PermissionError("That record is outside your account scope.")


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    timestamp = value.removesuffix(" Asia/Kolkata")
    try:
        return datetime.strptime(timestamp, SNAPSHOT_FORMAT).replace(tzinfo=SNAPSHOT_TIMEZONE)
    except ValueError:
        return None


def _normalise_document_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[•●]", " ", value)).strip()


def _duration_minutes(target: str) -> int | None:
    match = re.search(r"\b(\d+)\s*(minute|minutes|hour|hours)\b", target, re.IGNORECASE)
    if not match:
        return None
    quantity = int(match.group(1))
    return quantity * 60 if match.group(2).lower().startswith("hour") else quantity


def _parse_agreement_rules(filename: str, text: str, account_id: str) -> dict[str, Any]:
    normalised = _normalise_document_text(text)
    declared_account = re.search(r"\bAccount\s*:\s*(ACCT-\d+)\b", normalised, re.IGNORECASE)
    if not declared_account or declared_account.group(1).upper() != account_id.upper():
        return {}
    if not re.search(r"\bStatus\s*:\s*ACTIVE\b", normalised, re.IGNORECASE):
        return {}

    support_targets: dict[str, dict[str, Any]] = {}
    for severity, target in re.findall(
        r"\b(P[123])\s*:\s*(.*?)(?=\s+P[123]\s*:|\s+\d+\.\s|$)",
        normalised,
        re.IGNORECASE,
    ):
        cleaned_target = re.sub(r"\s+No\s+(?:weekend|after-hours).*", "", target, flags=re.IGNORECASE).strip(" .,;")
        if cleaned_target:
            support_targets[severity.upper()] = {
                "text": cleaned_target,
                "minutes": _duration_minutes(cleaned_target),
                "is_24x7": bool(re.search(r"\b24\s*[x×]\s*7\b", cleaned_target, re.IGNORECASE)),
            }

    lower = normalised.lower()
    cancellation_fee_waiver = bool(
        re.search(
            r"\bmay\s+cancel\s+(?:any\s+)?booked\s+shipment\s+before\s+pickup\s+with\s+no\s+cancellation\s+fee\b",
            normalised,
            re.IGNORECASE,
        )
    )
    credit_match = re.search(
        r"\bpickup\s+is\s+more\s+than\s+(\d+)\s+hours.*?\bfixed\s+INR\s+([\d,]+)\s+service\s+credit\b",
        normalised,
        re.IGNORECASE,
    )
    failed_pickup_credit = None
    if credit_match and "replaces the default" in lower:
        failed_pickup_credit = {
            "threshold_minutes": int(credit_match.group(1)) * 60,
            "amount_inr": int(credit_match.group(2).replace(",", "")),
        }
    return {
        "document": filename,
        "cancellation_fee_waiver_before_pickup": cancellation_fee_waiver,
        "failed_pickup_credit": failed_pickup_credit,
        "support_targets": support_targets,
    }


def _agreement_rules(source_db, account_id: str) -> dict[str, Any]:
    with connect(source_db) as db:
        agreement = db.execute(
            """SELECT filename, text FROM documents
               WHERE category='agreement' AND status='current' AND account_id=?""",
            (account_id,),
        ).fetchone()
    if not agreement:
        return {}
    return _parse_agreement_rules(agreement["filename"], agreement["text"], account_id)


def _is_true(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _account_for_reference(db, payload: dict[str, Any]) -> str:
    account_ids: set[str] = set()
    account_id = str(payload.get("account_id", "")).strip().upper()
    if account_id:
        if not db.execute("SELECT 1 FROM accounts WHERE account_id=?", (account_id,)).fetchone():
            raise ValueError("The supplied account does not exist.")
        account_ids.add(account_id)
    for field, table in (("order_id", "orders"), ("ticket_id", "tickets")):
        value = str(payload.get(field, "")).strip().upper()
        if not value:
            continue
        row = db.execute(f"SELECT account_id FROM {table} WHERE {field}=?", (value,)).fetchone()
        if not row:
            raise ValueError(f"The supplied {field.replace('_', ' ')} does not exist.")
        account_ids.add(row["account_id"])
    if not account_ids:
        raise ValueError("An action must reference an account, order, or ticket.")
    if len(account_ids) != 1:
        raise ValueError("All action references must belong to the same account.")
    return account_ids.pop()


def _ticket_for_actor(ticket: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
    result = dict(ticket)
    result["historical_resolution_authority"] = "context_only_untrusted"
    if actor["role"] == "customer":
        result.pop("assigned_to", None)
        result.pop("historical_resolution", None)
    elif result.get("historical_resolution"):
        result["historical_resolution_context"] = result.pop("historical_resolution")
    else:
        result.pop("historical_resolution", None)
    return result


def _account_for_actor(account: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
    if actor["role"] != "customer":
        return dict(account)
    allowed_fields = {"account_id", "account_name", "plan", "status", "contract_file", "premium_support"}
    return {field: value for field, value in account.items() if field in allowed_fields}


def _order_for_actor(order: dict[str, Any], actor: dict[str, Any], *, include_internal: bool = False) -> dict[str, Any]:
    if actor["role"] != "customer" or include_internal:
        return dict(order)
    internal_fields = {"carrier_fault", "customer_fault", "notes"}
    return {field: value for field, value in order.items() if field not in internal_fields}


def lookup_operations(source_db, actor: dict[str, Any], *, order_id: str | None = None, ticket_id: str | None = None, account_id: str | None = None, account_query: str | None = None, _include_internal: bool = False) -> dict[str, Any]:
    with connect(source_db) as db:
        result: dict[str, Any] = {"snapshot_at": db.execute("SELECT value FROM meta WHERE key='snapshot_at'").fetchone()[0]}
        referenced_accounts: set[str] = set()
        if order_id:
            order = db.execute("SELECT * FROM orders WHERE order_id=?", (order_id.strip().upper(),)).fetchone()
            if not order:
                return {"error": "Order not found"}
            _allowed_account(actor, order["account_id"])
            result["order"] = _order_for_actor(dict(order), actor, include_internal=_include_internal)
            referenced_accounts.add(order["account_id"])
        if ticket_id:
            ticket = db.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id.strip().upper(),)).fetchone()
            if not ticket:
                return {"error": "Ticket not found"}
            _allowed_account(actor, ticket["account_id"])
            result["ticket"] = _ticket_for_actor(dict(ticket), actor)
            referenced_accounts.add(ticket["account_id"])
        if account_id:
            normalised_account_id = account_id.strip().upper()
            if not db.execute("SELECT 1 FROM accounts WHERE account_id=?", (normalised_account_id,)).fetchone():
                return {"error": "Account not found"}
            referenced_accounts.add(normalised_account_id)
        if len(referenced_accounts) > 1:
            raise ValueError("The supplied records belong to different accounts.")
        resolved_account = next(iter(referenced_accounts), None)
        if not resolved_account and account_query:
            query_tokens = re.findall(r"[a-zA-Z0-9]{3,}", account_query.lower())
            matches = []
            for token in query_tokens:
                matches.extend(db.execute("SELECT * FROM accounts WHERE lower(account_name) LIKE ?", (f"%{token}%",)).fetchall())
            unique_matches = {match["account_id"]: match for match in matches}
            if len(unique_matches) == 1:
                result["account"] = _account_for_actor(dict(next(iter(unique_matches.values()))), actor)
                resolved_account = result["account"]["account_id"]
        if resolved_account:
            resolved_account = resolved_account.upper()
            _allowed_account(actor, resolved_account)
            account = db.execute("SELECT * FROM accounts WHERE account_id=?", (resolved_account,)).fetchone()
            if account and "account" not in result:
                result["account"] = _account_for_actor(dict(account), actor)
        return result


def search_knowledge(source_db, actor: dict[str, Any], query: str, account_id: str | None = None) -> dict[str, Any]:
    if actor.get("role") not in VALID_ROLES:
        raise PermissionError("Your role is not authorised to search ParcelPilot knowledge.")
    if not isinstance(query, str):
        raise TypeError("Knowledge queries must be text.")
    scoped_account = (account_id or actor.get("account_id") or "").upper() or None
    if scoped_account:
        _allowed_account(actor, scoped_account)
    terms = [term.lower() for term in re.findall(r"[a-zA-Z0-9]{3,}", query)][:12]
    if not terms:
        return {"sources": []}
    with connect(source_db) as db:
        rows = db.execute(
            """SELECT c.id, c.page, c.text, d.filename, d.category, d.status, d.authority, d.account_id
               FROM chunks c JOIN documents d ON d.id=c.document_id
               WHERE d.status='current' AND (d.account_id IS NULL OR d.account_id=?)""",
            (scoped_account,),
        ).fetchall()
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        source = dict(row)
        matches = sum(source["text"].lower().count(term) for term in terms)
        if matches:
            source["excerpt"] = source.pop("text")[:420].replace("\n", " ").strip()
            scored.append((matches * 10 + source["authority"] / 10, source))
    return {"sources": [item for _, item in sorted(scored, reverse=True, key=lambda value: value[0])[:5]]}


def evaluate_order(source_db, actor: dict[str, Any], order_id: str) -> dict[str, Any]:
    record = lookup_operations(source_db, actor, order_id=order_id, _include_internal=True)
    if record.get("error"):
        return record
    order, account = record["order"], record["account"]
    agreement = _agreement_rules(source_db, account["account_id"])
    snapshot = _parse_time(record["snapshot_at"])
    pickup_end = _parse_time(order["pickup_window_end"])
    cancellation: dict[str, Any] = {"eligible": False, "outcome": "No cancellation request is recorded."}
    if order["status"] == "DRAFT":
        cancellation = {"eligible": True, "fee_inr": 0, "outcome": "Draft shipments can be cancelled with no fee."}
    elif order["status"] == "BOOKED" and not order["pickup_actual_at"]:
        if agreement.get("cancellation_fee_waiver_before_pickup"):
            cancellation = {
                "eligible": True,
                "fee_inr": 0,
                "outcome": f"The active agreement ({agreement['document']}) waives the fee before pickup.",
            }
        elif order["cancellation_requested_at"]:
            requested_at = _parse_time(order["cancellation_requested_at"])
            booked_at = _parse_time(order["booked_at"])
            if not requested_at or not booked_at or requested_at < booked_at:
                cancellation = {"eligible": False, "outcome": "Recorded cancellation timing is invalid or incomplete; escalate for review."}
            else:
                elapsed = requested_at - booked_at
                fee = 0 if elapsed <= timedelta(minutes=30) else 250
                cancellation = {"eligible": True, "fee_inr": fee, "elapsed_minutes": int(elapsed.total_seconds() // 60), "outcome": "The current SOP applies because no agreement waiver exists."}
        else:
            cancellation = {"eligible": True, "fee_inr": "pending", "outcome": "Cancellation timing is needed to determine the default fee."}
    elif order["status"] == "PICKED_UP":
        cancellation = {"eligible": False, "outcome": "Do not cancel after pickup; use return-to-origin if requested."}
    elif order["status"] == "DELIVERED":
        cancellation = {"eligible": False, "outcome": "Delivered shipments cannot be cancelled."}
    delay_minutes = int((snapshot - pickup_end).total_seconds() // 60) if pickup_end and snapshot else None
    if delay_minutes is not None and delay_minutes < 0:
        delay_minutes = None
    service_credit: dict[str, Any] = {"eligible": False, "outcome": "Carrier fault, timing, and customer fault must be established before promising a credit."}
    if _is_true(order["carrier_fault"]) and not _is_true(order["customer_fault"]) and delay_minutes is not None:
        if agreement.get("failed_pickup_credit"):
            credit_rule = agreement["failed_pickup_credit"]
            service_credit = {
                "eligible": delay_minutes > credit_rule["threshold_minutes"],
                "amount_inr": credit_rule["amount_inr"] if delay_minutes > credit_rule["threshold_minutes"] else 0,
                "threshold_minutes": credit_rule["threshold_minutes"],
                "delay_minutes": delay_minutes,
                "outcome": f"The active agreement ({agreement['document']}) replaces the default credit threshold and amount.",
            }
        else:
            try:
                amount = min(500, round(float(order["shipment_fee_inr"]) * 0.10))
            except (TypeError, ValueError):
                service_credit = {"eligible": False, "delay_minutes": delay_minutes, "outcome": "The carrier fault is established, but the shipment fee is missing or invalid; escalate before calculating a credit."}
            else:
                service_credit = {"eligible": delay_minutes > 120, "amount_inr": amount if delay_minutes > 120 else 0, "threshold_minutes": 120, "delay_minutes": delay_minutes, "outcome": "The current cancellation and service-credit SOP applies."}
    record["order"] = _order_for_actor(order, actor)
    return {**record, "cancellation": cancellation, "service_credit": service_credit}


def evaluate_ticket(source_db, actor: dict[str, Any], ticket_id: str) -> dict[str, Any]:
    record = lookup_operations(source_db, actor, ticket_id=ticket_id)
    if record.get("error"):
        return record
    ticket, account = record["ticket"], record["account"]
    agreement = _agreement_rules(source_db, account["account_id"])
    description = f"{ticket['subject']} {ticket['description']}".lower()
    if any(marker in description for marker in ("all shipment creation", "api key exposure", "credential exposure")):
        severity, reason = "P1", "A complete shipment-creation outage or suspected credential exposure is P1 under Support Policy v3."
    elif "bulk upload" in description or "shows booked" in description or (
        "upload" in description and "fail" in description
    ):
        severity, reason = "P2", "The issue materially degrades a feature but an operational workaround may exist."
    else:
        severity, reason = "P3", "The available information indicates a how-to, configuration, or limited-impact request."
    targets = {
        "Enterprise": {"P1": "30 minutes, 24x7", "P2": "2 business hours", "P3": "1 business day"},
        "Growth": {"P1": "2 business hours", "P2": "4 business hours", "P3": "2 business days"},
        "Standard": {"P1": "4 business hours", "P2": "1 business day", "P3": "2 business days"},
    }
    plan_targets = targets.get(account["plan"])
    if not plan_targets:
        return {**record, "triage": {"severity": severity, "reason": reason, "response_target": "Requires plan validation", "timing": {"status": "needs_plan_validation", "outcome": "The account plan is not recognised in the current policy mapping; escalate before making an SLA claim."}}}
    agreement_target = agreement.get("support_targets", {}).get(severity)
    target = agreement_target["text"] if agreement_target else plan_targets[severity]
    timing: dict[str, Any] = {"status": "needs_business_hours", "outcome": "The pack does not define business hours, so non-24x7 SLA elapsed time is not calculated."}
    is_24x7 = bool(agreement_target and agreement_target["is_24x7"]) or (
        not agreement_target and severity == "P1" and account["plan"] == "Enterprise"
    )
    if severity == "P1" and is_24x7:
        target_minutes = agreement_target["minutes"] if agreement_target else 30
        if not target_minutes:
            timing = {"status": "invalid_target", "outcome": "The active agreement has a 24x7 P1 target without a calculable duration; escalate before making an SLA claim."}
            return {**record, "triage": {"severity": severity, "reason": reason, "response_target": target, "timing": timing}}
        snapshot = _parse_time(record["snapshot_at"])
        created_at = _parse_time(ticket["created_at"])
        if not snapshot or not created_at or created_at > snapshot:
            timing = {"status": "invalid_timestamp", "outcome": "Ticket timing is missing or inconsistent with the dataset snapshot; escalate before making an SLA claim."}
        else:
            elapsed_minutes = int((snapshot - created_at).total_seconds() // 60)
            timing = {"status": "breached" if elapsed_minutes > target_minutes else "within_target", "elapsed_minutes": elapsed_minutes, "target_minutes": target_minutes, "outcome": "P1 is 24x7 for this account, so elapsed time can be calculated from the dataset snapshot."}
    return {**record, "triage": {"severity": severity, "reason": reason, "response_target": target, "timing": timing}}


def scan_issue_signals(source_db, actor: dict[str, Any]) -> dict[str, Any]:
    if actor["role"] != "operations_lead":
        raise PermissionError("Operations signal scanning requires the operations lead role.")
    with connect(source_db) as db:
        snapshot = db.execute("SELECT value FROM meta WHERE key='snapshot_at'").fetchone()[0]
        tickets = [dict(row) for row in db.execute("SELECT * FROM tickets WHERE lower(status)='open'")]
        all_tickets = [dict(row) for row in db.execute("SELECT * FROM tickets")]
        orders = [dict(row) for row in db.execute("SELECT * FROM orders")]
    signals: list[dict[str, Any]] = []
    for ticket in tickets:
        text = f"{ticket['subject']} {ticket['description']}".lower()
        if any(marker in text for marker in ("api key exposure", "credential exposure", "all shipment creation")):
            triage = evaluate_ticket(source_db, actor, ticket["ticket_id"])["triage"]
            if triage["timing"]["status"] == "breached":
                reason = "Critical security/outage signal; its explicit P1 response target is already breached. Immediate escalation required."
            else:
                reason = "Critical security/outage signal; immediate escalation required."
            signals.append({"severity": "P1", "ticket_id": ticket["ticket_id"], "reason": reason})
        elif "bulk upload" in text or "shows booked" in text:
            signals.append({"severity": "P2", "ticket_id": ticket["ticket_id"], "reason": "Matches a current product known issue; investigate and give approved workaround."})
    for order in orders:
        if _is_true(order["carrier_fault"]) and order["status"] == "BOOKED":
            signals.append({"severity": "P2", "order_id": order["order_id"], "reason": "Carrier-fault pickup has not completed; assess service-credit eligibility."})
    for label, markers in {
        "bulk upload failures": ("bulk upload",),
        "shipment status not updating": ("shows booked",),
    }.items():
        matches = [ticket for ticket in all_tickets if any(marker in f"{ticket['subject']} {ticket['description']}".lower() for marker in markers)]
        if len(matches) > 1:
            ticket_ids = sorted(ticket["ticket_id"] for ticket in matches)
            account_count = len({ticket["account_id"] for ticket in matches})
            signals.append(
                {
                    "severity": "P2",
                    "label": f"Recurring issue: {label}",
                    "ticket_ids": ticket_ids,
                    "reason": f"{len(matches)} related tickets across {account_count} account(s), including closed history for pattern detection. Historical resolutions were not used as authority.",
                }
            )
    severity_rank = {"P1": 0, "P2": 1, "P3": 2}
    signals.sort(key=lambda signal: (severity_rank.get(signal["severity"], 9), signal.get("ticket_id", signal.get("order_id", signal.get("label", "")))))
    return {"snapshot_at": snapshot, "signals": signals}


def _validate_action(source_db, actor: dict[str, Any], action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if actor.get("role") not in VALID_ROLES:
        raise PermissionError("Your role cannot prepare actions.")
    allowed_actions = CUSTOMER_ACTIONS if actor["role"] == "customer" else STAFF_ACTIONS
    if action_type not in allowed_actions:
        raise PermissionError("Your role cannot prepare that action.")
    if not isinstance(payload, dict):
        raise TypeError("Action details must be a compact JSON object.")
    try:
        encoded_payload = json.dumps(payload)
    except (TypeError, ValueError) as error:
        raise ValueError("Action details must be JSON-compatible.") from error
    if len(encoded_payload) > 6000:
        raise ValueError("Action details must be a compact JSON object.")
    if action_type == "update_ticket" and not str(payload.get("ticket_id", "")).strip():
        raise ValueError("A ticket update must reference a ticket.")
    with connect(source_db) as db:
        target_account = _account_for_reference(db, payload)
    _allowed_account(actor, target_account)
    reason = str(payload.get("reason", "")).strip()
    if not reason:
        raise ValueError("A proposed action needs a reason supported by the available evidence.")
    checked_payload = {**payload, "account_id": target_account, "reason": reason[:1200]}
    for field in ("order_id", "ticket_id"):
        if field in checked_payload:
            checked_payload[field] = str(checked_payload[field]).strip().upper()
    return checked_payload


def prepare_action(source_db, runtime_db, actor: dict[str, Any], action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    checked_payload = _validate_action(source_db, actor, action_type, payload)
    action_id = str(uuid.uuid4())
    expiry = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    with connect(runtime_db) as db:
        db.execute("DELETE FROM pending_actions WHERE expires_at < ?", (datetime.now(UTC).isoformat(),))
        db.execute("INSERT INTO pending_actions VALUES (?,?,?,?,?)", (action_id, actor["id"], action_type, json.dumps(checked_payload), expiry))
    return {"pending_action_id": action_id, "action_type": action_type, "payload": checked_payload, "requires_confirmation": True}


def confirm_action(source_db, runtime_db, actor: dict[str, Any], action_id: str) -> dict[str, str]:
    with connect(runtime_db) as db:
        db.execute("BEGIN IMMEDIATE")
        pending = db.execute("SELECT * FROM pending_actions WHERE id=? AND user_id=?", (action_id, actor["id"])).fetchone()
        if not pending:
            raise PermissionError("Pending action not found for this signed-in user.")
        if datetime.fromisoformat(pending["expires_at"]) < datetime.now(UTC):
            db.execute("DELETE FROM pending_actions WHERE id=?", (action_id,))
            raise ValueError("The proposed action expired; prepare it again.")
        payload = json.loads(pending["payload_json"])
        _validate_action(source_db, actor, pending["action_type"], payload)
        confirmed_id, now = str(uuid.uuid4()), datetime.now(UTC).isoformat()
        db.execute("INSERT INTO actions VALUES (?,?,?,?,?)", (confirmed_id, actor["id"], pending["action_type"], pending["payload_json"], now))
        db.execute("INSERT INTO audit_events(user_id,event_type,detail,created_at) VALUES (?,?,?,?)", (actor["id"], "action_confirmed", pending["action_type"], now))
        db.execute("DELETE FROM pending_actions WHERE id=?", (action_id,))
    return {"action_id": confirmed_id, "status": "confirmed"}
