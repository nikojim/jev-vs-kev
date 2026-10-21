"""Answer parsing, human-in-the-loop rule, JEV vs KEV comparison and batch aggregation."""

from __future__ import annotations

import asyncio
import statistics
from collections import Counter

import httpx

from .config import Settings, Taxonomy
from .openrouter import decide
from .prompt import Q_NOUL, Q_SEVERITY, Q_TEAM, build_request

MODEL_KEYS = ("jev", "kev")
CALIBRATION_BINS = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0001)]
SEVERITY_ALIGNED_DELTA = 0.5  # |Δ severity position| considered "same severity"

# ---------------------------------------------------------------------------
# Parsing & validation of Decisions API answers
# ---------------------------------------------------------------------------


def _prob(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, v))


def parse_answers(data: dict, tax: Taxonomy) -> tuple[dict, list[str]]:
    """Map the typed answers to a flat triage record. Returns (parsed, validation errors)."""
    answers = data.get("answers") or {}
    errors: list[str] = []

    team_a = answers.get(Q_TEAM) or {}
    team = team_a.get("choice")
    if team not in tax.team_ids:
        errors.append(f"Unknown or missing team '{team}'")
        team = None
    team_conf = _prob(team_a.get("confidence"))
    if team_conf is None and team and isinstance(team_a.get("probabilities"), dict):
        team_conf = _prob(team_a["probabilities"].get(team))
    if team_conf is None:
        errors.append("Missing team confidence")

    noul_a = answers.get(Q_NOUL) or {}
    p_critical = _prob(noul_a.get("noul"))
    if p_critical is None:
        errors.append("Missing NOUL answer")

    sev_a = answers.get(Q_SEVERITY) or {}
    n_levels = len(tax.severity_levels)
    severity = None
    try:
        severity = float(sev_a.get("score"))
        if not -0.01 <= severity <= n_levels - 1 + 0.01:
            errors.append(f"Severity score {severity} outside 0–{n_levels - 1}")
        severity = round(max(0.0, min(n_levels - 1.0, severity)), 3)
    except (TypeError, ValueError):
        errors.append("Missing severity score")
    level = tax.severity_level(severity)

    return (
        {
            "team": team,
            "team_confidence": team_conf,
            "team_probabilities": team_a.get("probabilities") or {},
            "noul": p_critical,  # P(critical)
            "critical": None if p_critical is None else p_critical >= tax.noul_threshold,
            "noul_confidence": None if p_critical is None else round(max(p_critical, 1 - p_critical), 4),
            "severity_score": severity,
            "severity_level": level["id"] if level else None,
            "severity_confidence": _prob(sev_a.get("confidence")),
            "severity_probabilities": sev_a.get("probabilities") or {},
        },
        errors,
    )


def hitl_decision(
    parsed: dict | None, errors: list[str], call_error: str | None, threshold: float, mode: str = "team_or_critical"
) -> dict:
    """needs human = (checked confidences below threshold) OR unusable answer.

    mode selects which confidences are checked:
      team_or_critical -> team confidence OR NOUL (criticality) confidence
      team             -> team confidence only
      critical         -> NOUL (criticality) confidence only
    Failed calls and invalid answers always need a human, whatever the mode.
    """
    check_team = mode in ("team_or_critical", "team")
    check_noul = mode in ("team_or_critical", "critical")
    reasons: list[str] = []
    if call_error:
        reasons.append("Model call failed")
    elif parsed is None:
        reasons.append("No answer")
    else:
        if errors:
            reasons.append("Invalid answer: " + "; ".join(errors))
        tc, nc = parsed.get("team_confidence"), parsed.get("noul_confidence")
        if check_team and tc is not None and tc < threshold:
            reasons.append(f"Team confidence {tc:.0%} < {threshold:.0%}")
        if check_noul and nc is not None and nc < threshold:
            reasons.append(f"NOUL confidence {nc:.0%} < {threshold:.0%} (P(critical) = {parsed['noul']:.0%})")
    return {"required": bool(reasons), "reasons": reasons}


# ---------------------------------------------------------------------------
# Running models
# ---------------------------------------------------------------------------


async def run_model(client: httpx.AsyncClient, settings: Settings, tax: Taxonomy, key: str, payload: dict) -> dict:
    res = await decide(client, settings, settings.model(key), payload)
    parsed, errors = (None, [])
    if res.error is None:
        parsed, errors = parse_answers(res.data, tax)
    return {
        "model": key,
        "ok": res.error is None and not errors,
        "call_error": res.error,
        "validation_errors": errors,
        "parsed": parsed,
        "hitl": hitl_decision(parsed, errors, res.error, settings.hitl_threshold, settings.hitl_mode),
        "metrics": res.metrics.to_dict(),
        "raw": res.data,
    }


def _primary(runs: list[dict]) -> dict:
    """Representative run: first run matching the most common (team, critical) pair."""
    usable = [r for r in runs if r["parsed"]]
    if not usable:
        return runs[0]
    modal, _ = Counter((r["parsed"]["team"], r["parsed"]["critical"]) for r in usable).most_common(1)[0]
    return next(r for r in usable if (r["parsed"]["team"], r["parsed"]["critical"]) == modal)


def consistency(runs: list[dict]) -> dict | None:
    usable = [r["parsed"] for r in runs if r["parsed"]]
    if len(runs) < 2 or not usable:
        return None
    team_mode = Counter(p["team"] for p in usable).most_common(1)[0][1]
    noul_mode = Counter(p["critical"] for p in usable).most_common(1)[0][1]
    sev = [p["severity_score"] for p in usable if p["severity_score"] is not None]
    return {
        "runs": len(runs),
        "team": round(team_mode / len(runs), 3),
        "noul": round(noul_mode / len(runs), 3),
        "severity_stdev": round(statistics.pstdev(sev), 3) if len(sev) > 1 else 0.0,
    }


def compare(jev: dict, kev: dict) -> dict:
    """How KEV (challenger) deviates from JEV (baseline)."""
    pj, pk = jev.get("parsed"), kev.get("parsed")
    empty = {"team_match": None, "noul_match": None, "severity_delta": None, "noul_delta": None, "hitl_match": None}
    if not pj or not pk or pj["team"] is None or pk["team"] is None:
        return {"status": "error", **empty}
    team_match = pj["team"] == pk["team"]
    noul_match = pj["critical"] == pk["critical"]
    sev_delta = None
    if pj["severity_score"] is not None and pk["severity_score"] is not None:
        sev_delta = round(pk["severity_score"] - pj["severity_score"], 3)
    noul_delta = None
    if pj["noul"] is not None and pk["noul"] is not None:
        noul_delta = round(pk["noul"] - pj["noul"], 4)
    if team_match and noul_match and (sev_delta is None or abs(sev_delta) <= SEVERITY_ALIGNED_DELTA):
        status = "aligned"
    elif team_match or noul_match:
        status = "partial"
    else:
        status = "diverges"
    return {
        "status": status,
        "team_match": team_match,
        "noul_match": noul_match,
        "severity_delta": sev_delta,
        "noul_delta": noul_delta,
        "hitl_match": jev["hitl"]["required"] == kev["hitl"]["required"],
    }


def _bool(value) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "yes", "1", "y", "critical"}:
        return True
    if text in {"false", "no", "0", "n", "not critical"}:
        return False
    return None


def ground_truth(ticket: dict, tax: Taxonomy) -> dict | None:
    gt = {
        "team": (ticket.get("expected_team") or "").strip() or None,
        "critical": _bool(ticket.get("expected_critical")),
        "severity": tax.severity_index(ticket.get("expected_severity")),
    }
    return gt if any(v is not None for v in gt.values()) else None


def correctness(run: dict, gt: dict | None) -> dict | None:
    if not gt:
        return None
    p = run.get("parsed") or {}
    return {
        "team": None if gt["team"] is None else p.get("team") == gt["team"],
        "noul": None if gt["critical"] is None else p.get("critical") == gt["critical"],
        "severity_abs_error": None
        if gt["severity"] is None or p.get("severity_score") is None
        else round(abs(p["severity_score"] - gt["severity"]), 3),
    }


async def triage_ticket(client: httpx.AsyncClient, settings: Settings, tax: Taxonomy, ticket: dict, runs_per_ticket: int = 1) -> dict:
    payload = build_request(tax, ticket)
    jobs = [run_model(client, settings, tax, key, payload) for key in MODEL_KEYS for _ in range(runs_per_ticket)]
    results = await asyncio.gather(*jobs)
    runs = {key: [r for r in results if r["model"] == key] for key in MODEL_KEYS}
    primary = {key: _primary(runs[key]) for key in MODEL_KEYS}
    gt = ground_truth(ticket, tax)
    return {
        "ticket": ticket,
        "primary": primary,
        "runs": runs,
        "consistency": {key: consistency(runs[key]) for key in MODEL_KEYS},
        "comparison": compare(primary["jev"], primary["kev"]),
        "ground_truth": gt,
        "correctness": {key: correctness(primary[key], gt) for key in MODEL_KEYS},
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def _rate(flags: list[bool]) -> float | None:
    return round(sum(flags) / len(flags), 4) if flags else None


def _calibration(pairs: list[tuple[float, bool]]) -> list[dict]:
    out = []
    for lo, hi in CALIBRATION_BINS:
        inbin = [(c, ok) for c, ok in pairs if lo <= c < hi]
        out.append(
            {
                "bin": f"{lo:.1f}–{min(hi, 1.0):.1f}",
                "count": len(inbin),
                "avg_confidence": _mean([c for c, _ in inbin]),
                "accuracy": _rate([ok for _, ok in inbin]),
            }
        )
    return out


def model_summary(results: list[dict], key: str, tax: Taxonomy) -> dict:
    primaries = [r["primary"][key] for r in results]
    all_runs = [run for r in results for run in r["runs"][key]]
    m = [run["metrics"] for run in all_runs if not run["call_error"]]

    lat = [x["latency_ms"] for x in m if x["latency_ms"] is not None]
    ttfb = [x["ttfb_ms"] for x in m if x["ttfb_ms"] is not None]
    costs = [x["cost_usd"] for x in m if x["cost_usd"] is not None]
    tin = [x["input_tokens"] for x in m if x["input_tokens"] is not None]
    tout = [x["output_tokens"] for x in m if x["output_tokens"] is not None]

    parsed = [p["parsed"] for p in primaries if p["parsed"]]
    corr = [r["correctness"][key] for r in results if r["correctness"][key]]
    team_ok = [c["team"] for c in corr if c["team"] is not None]
    noul_ok = [c["noul"] for c in corr if c["noul"] is not None]
    both_ok = [c["team"] and c["noul"] for c in corr if c["team"] is not None and c["noul"] is not None]
    sev_err = [c["severity_abs_error"] for c in corr if c["severity_abs_error"] is not None]

    cal_team, cal_noul = [], []
    for r in results:
        p, c = r["primary"][key]["parsed"], r["correctness"][key]
        if p and c:
            if c["team"] is not None and p["team_confidence"] is not None:
                cal_team.append((p["team_confidence"], c["team"]))
            if c["noul"] is not None and p["noul_confidence"] is not None:
                cal_noul.append((p["noul_confidence"], c["noul"]))

    cons = [r["consistency"][key] for r in results if r["consistency"][key]]
    providers = Counter(x["provider"] for x in m if x.get("provider"))
    versions = Counter(x["model_version"] for x in m if x.get("model_version"))

    return {
        "tickets": len(results),
        "calls": len(all_runs),
        "failed_calls": sum(1 for run in all_runs if run["call_error"]),
        "invalid_outputs": sum(1 for run in all_runs if not run["call_error"] and not run["ok"]),
        "retried_calls": sum(1 for run in all_runs if (run["metrics"].get("attempts") or 0) > 1),
        "providers": dict(providers),
        "model_versions": dict(versions),
        "hitl_rate": _rate([p["hitl"]["required"] for p in primaries]),
        "hitl_count": sum(1 for p in primaries if p["hitl"]["required"]),
        "latency_ms": {"avg": _mean(lat), "p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "total_s": round(sum(lat) / 1000, 2)},
        "ttfb_ms": {"avg": _mean(ttfb), "p50": _pct(ttfb, 0.5), "p95": _pct(ttfb, 0.95)},
        "tokens": {"input_avg": _mean(tin), "output_avg": _mean(tout), "total": sum(tin) + sum(tout)},
        "cost": {
            "total_usd": round(sum(costs), 8) if costs else None,
            "per_call_usd": round(statistics.fmean(costs), 10) if costs else None,
            "per_1k_tickets_usd": round(statistics.fmean(costs) * 1000, 6) if costs else None,
            "priced_calls": len(costs),
        },
        "avg_team_confidence": _mean([p["team_confidence"] for p in parsed if p["team_confidence"] is not None]),
        "avg_noul_confidence": _mean([p["noul_confidence"] for p in parsed if p["noul_confidence"] is not None]),
        "avg_severity_confidence": _mean([p["severity_confidence"] for p in parsed if p["severity_confidence"] is not None]),
        "critical_rate": _rate([p["critical"] for p in parsed if p["critical"] is not None]),
        "accuracy": {
            "team": _rate(team_ok),
            "noul": _rate(noul_ok),
            "both": _rate(both_ok),
            "severity_mae": _mean(sev_err),
            "labelled": len(corr),
        },
        "calibration": {"team": _calibration(cal_team), "noul": _calibration(cal_noul)},
        "consistency": {
            "team": _mean([c["team"] for c in cons]),
            "noul": _mean([c["noul"] for c in cons]),
            "severity_stdev": _mean([c["severity_stdev"] for c in cons]),
        }
        if cons
        else None,
        "distribution": {
            "team": dict(Counter(p["team"] for p in parsed if p["team"])),
            "critical": {
                "true": sum(1 for p in parsed if p["critical"] is True),
                "false": sum(1 for p in parsed if p["critical"] is False),
            },
            "severity": dict(Counter(p["severity_level"] for p in parsed if p["severity_level"])),
        },
    }


def pair_summary(results: list[dict]) -> dict:
    comps = [r["comparison"] for r in results if r["comparison"]["status"] != "error"]
    team_cm: Counter = Counter()
    noul_cm: Counter = Counter()
    sev_points, noul_points = [], []
    for r in results:
        pj, pk = r["primary"]["jev"]["parsed"], r["primary"]["kev"]["parsed"]
        if pj and pk:
            tid = r["ticket"].get("id")
            if pj["team"] and pk["team"]:
                team_cm[(pj["team"], pk["team"])] += 1
            if pj["critical"] is not None and pk["critical"] is not None:
                noul_cm[(str(pj["critical"]).lower(), str(pk["critical"]).lower())] += 1
            if pj["severity_score"] is not None and pk["severity_score"] is not None:
                sev_points.append({"id": tid, "jev": pj["severity_score"], "kev": pk["severity_score"]})
            if pj["noul"] is not None and pk["noul"] is not None:
                noul_points.append({"id": tid, "jev": pj["noul"], "kev": pk["noul"]})
    sev_d = [abs(c["severity_delta"]) for c in comps if c["severity_delta"] is not None]
    noul_d = [abs(c["noul_delta"]) for c in comps if c["noul_delta"] is not None]
    return {
        "compared": len(comps),
        "team_agreement": _rate([c["team_match"] for c in comps]),
        "noul_agreement": _rate([c["noul_match"] for c in comps]),
        "full_agreement": _rate([c["team_match"] and c["noul_match"] for c in comps]),
        "hitl_agreement": _rate([c["hitl_match"] for c in comps]),
        "severity_mean_abs_delta": _mean(sev_d),
        "noul_mean_abs_delta": _mean(noul_d),
        "status_counts": dict(Counter(r["comparison"]["status"] for r in results)),
        "team_confusion": [{"jev": a, "kev": b, "count": n} for (a, b), n in team_cm.items()],
        "noul_confusion": [{"jev": a, "kev": b, "count": n} for (a, b), n in noul_cm.items()],
        "severity_scatter": sev_points,
        "noul_scatter": noul_points,
    }


def verdict(models: dict, pair: dict, settings: Settings) -> dict:
    jev, kev = models["jev"], models["kev"]
    if not pair["compared"]:
        return {"decision": "insufficient_data", "headline": "Not enough successful comparisons", "checks": [], "notes": []}

    checks = []
    agree = pair["full_agreement"] or 0.0
    checks.append(
        {
            "name": "Agreement with JEV (team + NOUL)",
            "value": agree,
            "target": f"≥ {settings.verdict_min_agreement:.0%}",
            "passed": agree >= settings.verdict_min_agreement,
        }
    )
    if jev["hitl_rate"] is not None and kev["hitl_rate"] is not None:
        delta = kev["hitl_rate"] - jev["hitl_rate"]
        checks.append(
            {
                "name": "Human-review rate increase vs JEV",
                "value": delta,
                "target": f"≤ +{settings.verdict_max_hitl_delta:.0%}",
                "passed": delta <= settings.verdict_max_hitl_delta,
            }
        )
    ja, ka = jev["accuracy"]["both"], kev["accuracy"]["both"]
    if ja is not None and ka is not None:
        drop = ja - ka
        checks.append(
            {
                "name": "Accuracy drop vs JEV (ground truth)",
                "value": drop,
                "target": f"≤ {settings.verdict_max_accuracy_drop:.0%}",
                "passed": drop <= settings.verdict_max_accuracy_drop,
            }
        )
    kev_bad, jev_bad = kev["failed_calls"] + kev["invalid_outputs"], jev["failed_calls"] + jev["invalid_outputs"]
    checks.append(
        {
            "name": "KEV failed or invalid calls",
            "value": kev_bad,
            "target": f"≤ {jev_bad} (JEV)",
            "passed": kev_bad <= jev_bad,
        }
    )

    notes = []
    jc, kc = jev["cost"]["per_1k_tickets_usd"], kev["cost"]["per_1k_tickets_usd"]
    if jc and kc is not None:
        notes.append(f"KEV cost per 1k tickets is {abs(1 - kc / jc):.0%} {'lower' if kc <= jc else 'higher'} than JEV (${kc:.4f} vs ${jc:.4f}).")
    jl, kl = jev["latency_ms"]["avg"], kev["latency_ms"]["avg"]
    if jl and kl:
        faster = kl <= jl
        notes.append(f"KEV average latency is {(jl / kl if faster else kl / jl):.2f}× {'faster' if faster else 'slower'} than JEV ({kl:.0f} ms vs {jl:.0f} ms).")
    jt, kt = jev["ttfb_ms"]["avg"], kev["ttfb_ms"]["avg"]
    if jt and kt:
        notes.append(f"Average TTFB: KEV {kt:.0f} ms vs JEV {jt:.0f} ms.")

    passed = all(c["passed"] for c in checks)
    return {
        "decision": "replace" if passed else "keep_baseline",
        "headline": "KEV can replace JEV for triage" if passed else "Keep JEV — KEV does not meet all criteria yet",
        "checks": checks,
        "notes": notes,
    }


def summarize(results: list[dict], settings: Settings, tax: Taxonomy) -> dict:
    models = {key: model_summary(results, key, tax) for key in MODEL_KEYS}
    pair = pair_summary(results)
    return {"models": models, "pair": pair, "verdict": verdict(models, pair, settings)}
