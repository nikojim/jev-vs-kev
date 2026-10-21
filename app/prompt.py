"""Builds the Decisions API request from the YAML taxonomy. Both models get the exact same request."""

from __future__ import annotations

from .config import Taxonomy

# Question keys are ours; the API returns answers under the same keys.
Q_TEAM, Q_NOUL, Q_SEVERITY = "team", "critical", "severity"


def build_state(ticket: dict) -> dict:
    """The content the models evaluate. Empty fields are omitted."""
    state = {
        "ticket_id": ticket.get("id"),
        "customer_tier": ticket.get("customer_tier"),
        "channel": ticket.get("channel"),
        "subject": (ticket.get("subject") or "").strip(),
        "body": (ticket.get("body") or "").strip(),
    }
    return {k: v for k, v in state.items() if v}


def build_questions(tax: Taxonomy) -> dict:
    team_criteria = {
        t["id"]: f"{t['name']}: {t['description']}"
        + (f" Examples: {'; '.join(t['examples'])}." if t.get("examples") else "")
        for t in tax.teams
    }
    severity = tax.severity
    severity_instructions = severity["instructions"]
    if severity.get("scoring_factors"):
        severity_instructions = {"question": severity["instructions"], "consider": severity["scoring_factors"]}
    return {
        Q_TEAM: {
            "type": "choice",
            "instructions": tax.teams_cfg.get("instructions", "Which support team should own this ticket?"),
            "criteria": team_criteria,
        },
        Q_NOUL: {
            "type": "noul",
            "instructions": tax.noul["instructions"],
            "criteria": {"true": tax.noul["criteria"]["true"], "false": tax.noul["criteria"]["false"]},
        },
        Q_SEVERITY: {
            "type": "score",
            "instructions": severity_instructions,
            "criteria": [f"{lvl['name']}: {lvl['definition']}" for lvl in tax.severity_levels],
        },
    }


def build_request(tax: Taxonomy, ticket: dict, model: str | None = None) -> dict:
    body = {"state": build_state(ticket), "questions": build_questions(tax)}
    return {"model": model, **body} if model else body
