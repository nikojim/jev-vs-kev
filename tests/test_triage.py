"""Offline tests: request building, answer parsing, HITL rule, divergence, aggregation, API client and report."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app import report
from app.config import get_settings, load_taxonomy, normalize_hitl_mode
from app.openrouter import decide
from app.prompt import build_questions, build_request
from app.triage import compare, ground_truth, hitl_decision, parse_answers, summarize, triage_ticket

TAX = load_taxonomy()
SETTINGS = replace(get_settings(), api_key="test-key", max_retries=1, hitl_threshold=0.8)


def answers(team="billing", tc=0.9, p_crit=0.05, sev=1.2, sc=0.9):
    """A Decisions API response body."""
    return {
        "id": "gen-dec-1",
        "model": "typesafe/jev-1.14-20261014",
        "provider": "TypeSafe",
        "answers": {
            "team": {"type": "choice", "choice": team, "confidence": tc, "probabilities": {team: tc}},
            "critical": {"type": "noul", "noul": p_crit},
            "severity": {"type": "score", "score": sev, "confidence": sc, "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3, "3": 0}},
        },
        "usage": {"input_tokens": 435, "output_tokens": 69, "cost": 0.00001827},
    }


def test_request_is_built_from_yaml():
    q = build_questions(TAX)
    assert q["team"]["type"] == "choice" and set(q["team"]["criteria"]) == set(TAX.team_ids)
    assert q["critical"]["type"] == "noul" and set(q["critical"]["criteria"]) == {"true", "false"}
    assert q["severity"]["type"] == "score" and len(q["severity"]["criteria"]) == len(TAX.severity_levels)
    req = build_request(TAX, {"subject": "S", "body": "B", "customer_tier": "pro"}, "m/x")
    assert req["model"] == "m/x" and req["state"] == {"customer_tier": "pro", "subject": "S", "body": "B"}


def test_parse_answers():
    parsed, errors = parse_answers(answers(p_crit=0.94, sev=2.82), TAX)
    assert errors == []
    assert parsed["team"] == "billing" and parsed["team_confidence"] == 0.9
    assert parsed["critical"] is True and parsed["noul_confidence"] == 0.94
    assert parsed["severity_level"] == "critical"


def test_parse_answers_flags_unknown_team():
    _, errors = parse_answers(answers(team="marketing"), TAX)
    assert any("team" in e for e in errors)


@pytest.mark.parametrize(
    "tc,p_crit,expected",
    [(0.9, 0.05, False), (0.79, 0.05, True), (0.95, 0.43, True), (0.95, 0.85, False), (0.8, 0.2, False)],
)
def test_hitl_threshold(tc, p_crit, expected):
    parsed, errors = parse_answers(answers(tc=tc, p_crit=p_crit), TAX)
    assert hitl_decision(parsed, errors, None, 0.8)["required"] is expected


@pytest.mark.parametrize(
    "mode,tc,p_crit,expected",
    [
        ("team_or_critical", 0.6, 0.05, True),
        ("team_or_critical", 0.95, 0.5, True),
        ("team", 0.6, 0.05, True),
        ("team", 0.95, 0.5, False),  # low NOUL confidence ignored
        ("critical", 0.6, 0.05, False),  # low team confidence ignored
        ("critical", 0.95, 0.5, True),
    ],
)
def test_hitl_modes(mode, tc, p_crit, expected):
    parsed, errors = parse_answers(answers(tc=tc, p_crit=p_crit), TAX)
    assert hitl_decision(parsed, errors, None, 0.8, mode)["required"] is expected


@pytest.mark.parametrize("mode", ["team_or_critical", "team", "critical"])
def test_hitl_modes_always_flag_failures(mode):
    assert hitl_decision(None, [], "boom", 0.8, mode)["required"] is True
    parsed, errors = parse_answers(answers(team="marketing"), TAX)
    assert hitl_decision(parsed, errors, None, 0.8, mode)["required"] is True


def test_normalize_hitl_mode():
    assert [normalize_hitl_mode(v) for v in ("1", "2", "3", "TEAM", "noul", None, "bogus")] == [
        "team_or_critical", "team", "critical", "team", "critical", "team_or_critical", "team_or_critical"]


def test_hitl_on_failure():
    assert hitl_decision(None, [], "boom", 0.8)["required"] is True


def test_ground_truth():
    assert ground_truth({"subject": "s", "body": "b", "expected_team": None, "expected_severity": None}, TAX) is None
    gt = ground_truth({"expected_team": "billing", "expected_critical": "true", "expected_severity": "High"}, TAX)
    assert gt == {"team": "billing", "critical": True, "severity": 2}


def _run(data, required=False):
    return {"parsed": parse_answers(data, TAX)[0] if data else None, "hitl": {"required": required, "reasons": []}}


def test_compare_statuses():
    a = answers()
    assert compare(_run(a), _run(a))["status"] == "aligned"
    assert compare(_run(a), _run(answers(sev=2.5)))["status"] == "partial"
    assert compare(_run(a), _run(answers(p_crit=0.9)))["status"] == "partial"
    assert compare(_run(a), _run(answers(team="security", p_crit=0.9)))["status"] == "diverges"
    assert compare(_run(a), _run(None))["status"] == "error"


# ---------------------------------------------------------------------------
# API client with a fake Decisions endpoint
# ---------------------------------------------------------------------------


def fake_transport(by_model: dict[str, dict], fail_first: set[str] | None = None):
    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert set(body) >= {"model", "state", "questions"}
        model = body["model"]
        calls[model] = calls.get(model, 0) + 1
        if fail_first and model in fail_first and calls[model] == 1:
            return httpx.Response(429, json={"error": {"code": 429, "message": "Rate limit exceeded"}})
        return httpx.Response(200, json=by_model[model])

    return httpx.MockTransport(handler), calls


def test_decide_measures_metrics_and_retries():
    slug = SETTINGS.model("jev").slug
    transport, calls = fake_transport({slug: answers()}, fail_first={slug})

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await decide(client, replace(SETTINGS, max_retries=2), SETTINGS.model("jev"), build_request(TAX, {"subject": "x", "body": "y"}))

    res = asyncio.run(go())
    assert res.error is None
    m = res.metrics
    assert m.attempts == 2 and calls[slug] == 2
    assert m.input_tokens == 435 and m.output_tokens == 69 and m.total_tokens == 504
    assert m.cost_usd == 0.00001827 and m.cost_source == "openrouter"
    assert m.provider == "TypeSafe" and m.model_version.startswith("typesafe/jev-1.14")
    assert m.ttfb_ms is not None and m.latency_ms >= m.ttfb_ms


def test_provider_preferences_are_sent(monkeypatch):
    from app.config import provider_prefs

    monkeypatch.setenv("KEV_PROVIDER_ORDER", "SiliconFlow, Phala")
    monkeypatch.setenv("KEV_PROVIDER_IGNORE", "Phala")
    monkeypatch.setenv("KEV_PROVIDER_SORT", "latency")
    monkeypatch.setenv("KEV_ALLOW_FALLBACKS", "false")
    prefs = provider_prefs("KEV")
    assert prefs == {"order": ["SiliconFlow", "Phala"], "ignore": ["Phala"], "sort": "latency", "allow_fallbacks": False}
    monkeypatch.delenv("KEV_PROVIDER_ORDER")
    monkeypatch.delenv("KEV_PROVIDER_IGNORE")
    monkeypatch.delenv("KEV_PROVIDER_SORT")
    monkeypatch.delenv("KEV_ALLOW_FALLBACKS")
    assert provider_prefs("KEV") is None

    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=answers())

    model = replace(SETTINGS.model("kev"), provider=prefs)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await decide(client, SETTINGS, model, {"state": "x", "questions": {}})

    assert asyncio.run(go()).error is None
    assert seen["provider"] == prefs


def test_decide_reports_non_retryable_error():
    def handler(request):
        return httpx.Response(400, json={"error": {"code": 400, "message": "bad questions"}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await decide(client, SETTINGS, SETTINGS.model("kev"), {"state": "x", "questions": {}})

    res = asyncio.run(go())
    assert res.error == "HTTP 400: bad questions" and res.metrics.attempts == 1


def test_end_to_end_ticket_summary_and_report():
    jev, kev = SETTINGS.model("jev").slug, SETTINGS.model("kev").slug
    transport, _ = fake_transport({jev: answers(), kev: answers(team="product_support", tc=0.6)})

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            t = {"id": "T-1", "subject": "Charged twice", "body": "Two charges",
                 "expected_team": "billing", "expected_critical": "false", "expected_severity": "medium"}
            return await triage_ticket(client, SETTINGS, TAX, t, runs_per_ticket=2)

    result = asyncio.run(go())
    assert result["comparison"]["status"] == "partial"
    assert result["primary"]["kev"]["hitl"]["required"] is True
    assert result["primary"]["jev"]["hitl"]["required"] is False
    assert result["consistency"]["jev"]["team"] == 1.0
    assert result["correctness"]["jev"] == {"team": True, "noul": True, "severity_abs_error": 0.2}

    summary = summarize([result], SETTINGS, TAX)
    assert summary["models"]["jev"]["accuracy"]["team"] == 1.0
    assert summary["pair"]["team_agreement"] == 0.0
    assert summary["verdict"]["decision"] == "keep_baseline"

    run = {
        "id": "test", "created_at": "now", "name": "t",
        "models": {m.key: {"slug": m.slug, "label": m.label, "role": m.role} for m in SETTINGS.models},
        "settings": {"hitl_threshold": 0.8, "runs_per_ticket": 2, "endpoint": SETTINGS.decisions_url},
        "taxonomy_fingerprint": TAX.fingerprint, "questions": build_questions(TAX),
        "results": [result], "summary": summary,
    }
    html = report.render(run, TAX)
    assert "Keep JEV" in html and "<svg" in html and "Calibration" in html and "P(critical)" in html


@pytest.mark.parametrize("fonts", [None, ("missing.ttf", "missing-bold.ttf")])
def test_definitions_pdf(fonts):
    from app.definitions_pdf import build_definitions_pdf

    # None = system Unicode font if available; missing paths = core-font fallback with ASCII replacements.
    pdf = build_definitions_pdf(TAX, SETTINGS, font_paths=fonts)
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000


def test_missing_api_key_is_reported():
    async def go():
        async with httpx.AsyncClient() as client:
            return await decide(client, replace(SETTINGS, api_key=""), SETTINGS.model("kev"), {})

    assert "OPENROUTER_API_KEY" in asyncio.run(go()).error
