from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import OpenAI, OpenAIError

from .services import (
    evaluate_order,
    evaluate_ticket,
    lookup_operations,
    prepare_action,
    scan_issue_signals,
    search_knowledge,
)

TOOLS = [
    {"type": "function", "function": {"name": "search_knowledge", "description": "Find current authorised policies, agreements, SOPs and product guidance with citations. Never use the deprecated policy or historical ticket resolutions as authority.", "parameters": {"type": "object", "additionalProperties": False, "properties": {"query": {"type": "string"}, "account_id": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "lookup_operations", "description": "Retrieve authorised order, ticket and account facts from the supplied workbook. Use account_query for a customer name. The server enforces access scope.", "parameters": {"type": "object", "additionalProperties": False, "properties": {"order_id": {"type": "string"}, "ticket_id": {"type": "string"}, "account_id": {"type": "string"}, "account_query": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "evaluate_order", "description": "Calculate cancellation and service-credit eligibility from the authorised order, agreement, current SOP, and fixed dataset snapshot. Use for order cancellation or service-credit questions.", "parameters": {"type": "object", "additionalProperties": False, "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}},
    {"type": "function", "function": {"name": "evaluate_ticket", "description": "Classify ticket priority and assess the applicable response target using current agreements and Support Policy v3. Do not invent business hours when the source pack does not define them.", "parameters": {"type": "object", "additionalProperties": False, "properties": {"ticket_id": {"type": "string"}}, "required": ["ticket_id"]}}},
    {"type": "function", "function": {"name": "scan_issue_signals", "description": "Operations-lead-only scan for urgent, repeated, and unusual issues across accounts.", "parameters": {"type": "object", "additionalProperties": False, "properties": {}}}},
    {"type": "function", "function": {"name": "prepare_action", "description": "Prepare but never execute an escalation, ticket update, or follow-up. Include an authorised account, order, or ticket and an evidence-based reason. The user must explicitly confirm afterwards.", "parameters": {"type": "object", "additionalProperties": False, "properties": {"action_type": {"type": "string", "enum": ["create_escalation", "create_follow_up", "update_ticket"]}, "payload": {"type": "object"}}, "required": ["action_type", "payload"]}}},
]

SYSTEM = """You are ParcelPilot's evidence-first support assistant.
Use only tool results as facts. For order cancellations or service credits, call evaluate_order.
Authority is: signed active account agreement, current topic-specific SOP, Support Policy v3, current product guidance, then current structured facts. Never use deprecated Policy v2 or historical ticket resolutions as policy authority.
Before a factual final response, collect the supporting record and at least one current source; name the source file/page or record ID. Do not reveal unauthorised data. Do not execute actions: prepare a draft and request explicit confirmation. Escalate when sources conflict, necessary facts are missing, or the request is security-sensitive."""
logger = logging.getLogger(__name__)


class Agent:
    def __init__(self, settings, source_db, runtime_db):
        self.settings, self.source_db, self.runtime_db = settings, source_db, runtime_db

    def _execute(
        self,
        name: str,
        arguments: dict[str, Any],
        actor: dict[str, Any],
        *,
        action_requested: bool = False,
    ) -> dict[str, Any]:
        try:
            if name == "search_knowledge":
                return search_knowledge(self.source_db, actor, **arguments)
            if name == "lookup_operations":
                return lookup_operations(self.source_db, actor, **arguments)
            if name == "evaluate_order":
                return evaluate_order(self.source_db, actor, **arguments)
            if name == "evaluate_ticket":
                return evaluate_ticket(self.source_db, actor, **arguments)
            if name == "scan_issue_signals":
                return scan_issue_signals(self.source_db, actor)
            if name == "prepare_action":
                if not action_requested:
                    return {"error": "Do not prepare an action unless the user explicitly asked to create, escalate, follow up, or update a record."}
                return prepare_action(self.source_db, self.runtime_db, actor, **arguments)
            return {"error": "Unknown tool requested."}
        except (PermissionError, TypeError, ValueError) as error:
            return {"error": str(error)}

    def reply(self, message: str, actor: dict[str, Any]) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        small_talk = self._small_talk_reply(message)
        if small_talk:
            return {"answer": small_talk, "events": events, "mode": "deterministic"}
        if self.settings.llm_mode == "provider" and not self.settings.llm_api_key:
            return {"answer": "The configured AI provider has no API key. Set LLM_API_KEY or use offline mode.", "events": events, "mode": "configuration_error"}
        if self.settings.llm_mode != "offline" and self.settings.llm_api_key:
            self._precollect_evidence(message, actor, events)
            use_precollected_evidence = any(
                event.get("tool") == "search_knowledge" and event.get("result", {}).get("sources")
                for event in events
            )
            try:
                response = self._provider_reply(
                    message,
                    actor,
                    events,
                    use_precollected_evidence=use_precollected_evidence,
                )
                self._append_prepared_action(message, actor, events, response)
                return response
            except (OpenAIError, OSError, RuntimeError) as error:
                logger.warning("AI provider failed (%s); using deterministic fallback.", type(error).__name__)
                events.append({"tool": "provider", "error": "Provider unavailable; used deterministic fallback.", "mode": "offline_fallback"})
                fallback = self._fallback(message, actor, events)
                fallback["answer"] = (
                    "The live AI provider is currently unavailable, so this is a limited deterministic response. "
                    + fallback["answer"]
                )
                fallback["mode"] = "offline_fallback"
                return fallback
        return self._fallback(message, actor, events)

    def _provider_reply(
        self,
        message: str,
        actor: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        use_precollected_evidence: bool = False,
    ) -> dict[str, Any]:
        # Free compatible-provider models can legitimately take longer than a typical
        # consumer-chat response. One bounded request is safer than retries:
        # retries multiply latency and may duplicate provider-side work.
        client = OpenAI(
            api_key=self.settings.llm_api_key,
            base_url=self.settings.llm_base_url,
            default_headers=self.settings.llm_headers,
            timeout=60.0,
            max_retries=0,
        )
        messages: list[Any] = [{"role": "system", "content": SYSTEM}]
        if use_precollected_evidence:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "The server has already collected the authorised evidence below. "
                        "Use only this evidence, explain the answer clearly, and cite the source filenames/pages. "
                        "Do not call further tools.\n\n"
                        + self._provider_evidence_context(events)
                    ),
                }
            )
        messages.append({"role": "user", "content": message})
        corrective_round_requested = False
        for _ in range(6):
            response = client.chat.completions.create(
                model=self.settings.llm_model,
                messages=messages,
                tools=TOOLS,
                tool_choice="none" if use_precollected_evidence else "auto",
                temperature=0,
                max_tokens=700,
            )
            if not response.choices:
                raise RuntimeError("Provider returned no completion choices.")
            choice = response.choices[0].message
            if not choice.tool_calls:
                answer = choice.content or "I could not produce a supported answer."
                supporting_sources = [
                    source
                    for event in events
                    if event.get("tool") == "search_knowledge"
                    for source in event.get("result", {}).get("sources", [])
                ]
                has_sources = bool(supporting_sources)
                has_required_record = (
                    not re.search(r"\bORD-\d+\b", message.upper())
                    or any(event.get("tool") == "evaluate_order" and not event.get("result", {}).get("error") for event in events)
                ) and (
                    not re.search(r"\bTKT-\d+\b", message.upper())
                    or any(event.get("tool") == "evaluate_ticket" and not event.get("result", {}).get("error") for event in events)
                )
                if not has_sources or not has_required_record:
                    if not corrective_round_requested:
                        corrective_round_requested = True
                        messages.extend(
                            [
                                {"role": "assistant", "content": answer},
                                {
                                    "role": "system",
                                    "content": (
                                        "Do not finalise yet. Your previous draft lacks the required evidence. "
                                        "Call evaluate_order or evaluate_ticket for any record ID in the user request, "
                                        "then call search_knowledge for current authorised source evidence. Only then answer with citations."
                                    ),
                                },
                            ]
                        )
                        continue
                    answer = "I could not obtain current, citable policy evidence for this answer. I recommend escalating it for human review."
                elif not any(source["filename"] in answer for source in supporting_sources):
                    answer += "\n\nEvidence: " + ", ".join(
                        f"{source['filename']} p.{source['page']}" for source in supporting_sources[:3]
                    ) + "."
                return {"answer": answer, "events": events, "mode": "provider"}
            messages.append(choice)
            for call in choice.tool_calls:
                try:
                    arguments = json.loads(call.function.arguments)
                except json.JSONDecodeError:
                    arguments = {}
                result = self._execute(
                    call.function.name,
                    arguments,
                    actor,
                    action_requested=self._requests_action(message),
                )
                events.append({"tool": call.function.name, "result": result})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
        return {"answer": "I gathered the available evidence but need a support specialist to complete this safely.", "events": events, "mode": "provider"}

    def _precollect_evidence(self, message: str, actor: dict[str, Any], events: list[dict[str, Any]]) -> None:
        """Route factual requests through server-side tools before one provider explanation call."""
        order_match = re.search(r"ORD-\d+", message.upper())
        ticket_match = re.search(r"TKT-\d+", message.upper())
        account_id = actor.get("account_id")
        if order_match:
            evaluation = evaluate_order(self.source_db, actor, order_match.group())
            events.append({"tool": "evaluate_order", "result": evaluation})
            account_id = evaluation.get("account", {}).get("account_id", account_id)
        elif ticket_match:
            evaluation = evaluate_ticket(self.source_db, actor, ticket_match.group())
            events.append({"tool": "evaluate_ticket", "result": evaluation})
            account_id = evaluation.get("account", {}).get("account_id", account_id)
        elif actor["role"] != "customer":
            account_lookup = lookup_operations(self.source_db, actor, account_query=message)
            if account_lookup.get("account"):
                events.append({"tool": "lookup_operations", "result": account_lookup})
                account_id = account_lookup["account"]["account_id"]
        sources = search_knowledge(self.source_db, actor, message, account_id)
        events.append({"tool": "search_knowledge", "result": sources})

    @staticmethod
    def _provider_evidence_context(events: list[dict[str, Any]]) -> str:
        compact: list[dict[str, Any]] = []
        for event in events:
            result = event.get("result", {})
            if event.get("tool") == "search_knowledge":
                compact.append(
                    {
                        "tool": "search_knowledge",
                        "sources": [
                            {
                                "filename": source["filename"],
                                "page": source["page"],
                                "excerpt": source.get("excerpt", "")[:500],
                            }
                            for source in result.get("sources", [])[:3]
                        ],
                    }
                )
            elif event.get("tool") in {"evaluate_order", "evaluate_ticket", "lookup_operations"}:
                compact.append({"tool": event["tool"], "result": result})
        return json.dumps(compact, ensure_ascii=False)

    @staticmethod
    def _small_talk_reply(message: str) -> str | None:
        normalised = re.sub(r"\s+", " ", message.strip().lower()).strip("!.? ")
        if normalised in {"hi", "hello", "hey", "good morning", "good afternoon", "good evening"}:
            return "Hello — I can help with shipment orders, cancellations, service credits, support tickets, and current ParcelPilot policies. What would you like to check?"
        if normalised in {"help", "what can you do", "what do you do"}:
            return "I can look up authorised shipment and ticket facts, retrieve current policies and customer agreements, assess cancellations or service credits, and prepare an escalation or follow-up for your confirmation."
        return None

    def _append_prepared_action(
        self,
        message: str,
        actor: dict[str, Any],
        events: list[dict[str, Any]],
        response: dict[str, Any],
    ) -> None:
        if not self._requests_action(message):
            return
        order_match = re.search(r"ORD-\d+", message.upper())
        ticket_match = re.search(r"TKT-\d+", message.upper())
        reference = (
            {"order_id": order_match.group()}
            if order_match
            else ({"ticket_id": ticket_match.group()} if ticket_match else None)
        )
        if not reference:
            return
        action = self._draft_action_if_requested(message, actor, reference)
        if action:
            events.append({"tool": "prepare_action", "result": action})
            response["answer"] += "\n\nI prepared an action draft. Review and explicitly confirm it in the Evidence & tools panel."


    def _fallback(self, message: str, actor: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        text = message.lower()
        order_match = re.search(r"ORD-\d+", message.upper())
        ticket_match = re.search(r"TKT-\d+", message.upper())
        if actor["role"] == "operations_lead" and ("proactive" in text or "across" in text or "issues" in text):
            scan = scan_issue_signals(self.source_db, actor)
            events.append({"tool": "scan_issue_signals", "result": scan})
            summary = "\n".join(f"- {item['severity']} {item.get('ticket_id') or item.get('order_id')}: {item['reason']}" for item in scan["signals"])
            return {"answer": f"Prioritised operations signals at the dataset snapshot:\n{summary}", "events": events, "mode": "offline"}
        if order_match:
            evaluation = evaluate_order(self.source_db, actor, order_match.group())
            events.append({"tool": "evaluate_order", "result": evaluation})
            if evaluation.get("error"):
                return {"answer": evaluation["error"], "events": events, "mode": "offline"}
            sources = search_knowledge(self.source_db, actor, "cancellation service credit", evaluation["account"]["account_id"])
            events.append({"tool": "search_knowledge", "result": sources})
            answer = self._order_answer(evaluation, text, sources)
            action = self._draft_action_if_requested(message, actor, {"order_id": order_match.group()})
            if action:
                events.append({"tool": "prepare_action", "result": action})
                answer += "\n\nI prepared an action draft. Review and explicitly confirm it in the Evidence & tools panel."
            return {"answer": answer, "events": events, "mode": "offline"}
        if ticket_match:
            data = evaluate_ticket(self.source_db, actor, ticket_match.group())
            events.append({"tool": "evaluate_ticket", "result": data})
            sources = search_knowledge(self.source_db, actor, message, data.get("account", {}).get("account_id"))
            events.append({"tool": "search_knowledge", "result": sources})
            answer = self._ticket_answer(data, sources)
            action = self._draft_action_if_requested(message, actor, {"ticket_id": ticket_match.group()})
            if action:
                events.append({"tool": "prepare_action", "result": action})
                answer += "\n\nI prepared an action draft. Review and explicitly confirm it in the Evidence & tools panel."
            return {"answer": answer, "events": events, "mode": "offline"}
        sources = search_knowledge(self.source_db, actor, message)
        if actor["role"] != "customer":
            account_lookup = lookup_operations(self.source_db, actor, account_query=message)
            if account_lookup.get("account"):
                events.append({"tool": "lookup_operations", "result": account_lookup})
                sources = search_knowledge(self.source_db, actor, message, account_lookup["account"]["account_id"])
        events.append({"tool": "search_knowledge", "result": sources})
        if not sources["sources"]:
            return {"answer": "I could not find enough current, authorised evidence to answer safely. I recommend escalating this to support.", "events": events, "mode": "offline"}
        citations = self._citations(sources)
        return {"answer": f"I found current authorised guidance, but this offline mode needs an order or ticket ID for a record-specific decision. Relevant sources: {citations}", "events": events, "mode": "offline"}

    @staticmethod
    def _citations(sources: dict[str, Any]) -> str:
        return ", ".join(f"{source['filename']} p.{source['page']}" for source in sources["sources"][:3])

    @staticmethod
    def _requests_action(message: str) -> bool:
        text = message.lower()
        if re.search(r"^\s*(?:should|can|could|would|may)\s+(?:i|we)\b", text):
            return False
        return bool(
            re.search(
                r"\b(?:please\s+)?(?:escalate|create|open|raise|start|schedule|update)\b|"
                r"\b(?:can|could)\s+you\s+(?:escalate|create|open|raise|start|schedule|update)\b|"
                r"\b(?:create|start|schedule)\s+(?:a\s+)?follow\s*-?\s*up\b|"
                r"\b(?:please\s+)?follow\s*-?\s*up\b",
                text,
            )
        )

    def _draft_action_if_requested(self, message: str, actor: dict[str, Any], reference: dict[str, str]) -> dict[str, Any] | None:
        if not self._requests_action(message):
            return None
        action_type = "create_escalation" if "escalat" in message.lower() else "create_follow_up"
        return prepare_action(self.source_db, self.runtime_db, actor, action_type, {**reference, "reason": "User explicitly requested this action after the evidence review."})

    def _order_answer(self, evaluation: dict[str, Any], text: str, sources: dict[str, Any]) -> str:
        cancellation_terms = ("cancel", "cancellation", "fee")
        credit_terms = ("credit", "late", "delay", "compensation", "refund")
        decision = (
            evaluation["service_credit"]
            if not any(term in text for term in cancellation_terms) and any(term in text for term in credit_terms)
            else evaluation["cancellation"]
        )
        kind = "service-credit" if decision is evaluation["service_credit"] else "cancellation"
        outcome = decision["outcome"]
        if "fee_inr" in decision:
            outcome += f" Fee: {decision['fee_inr']} INR."
        if "amount_inr" in decision:
            outcome += f" Credit amount: {decision['amount_inr']} INR."
        return f"{kind.title()} decision for {evaluation['order']['order_id']}: {outcome}\n\nEvidence: order {evaluation['order']['order_id']}; {self._citations(sources)}."

    def _ticket_answer(self, data: dict[str, Any], sources: dict[str, Any]) -> str:
        if data.get("error"):
            return data["error"]
        ticket = data["ticket"]
        triage = data["triage"]
        timing = triage["timing"]
        return f"Ticket {ticket['ticket_id']} is {triage['severity']}: {triage['reason']} Response target: {triage['response_target']}. Timing: {timing['outcome']} Historical resolution, if present, is context only and was not used as authority. Current guidance: {self._citations(sources) or 'no matching source; escalate for review.'}"
