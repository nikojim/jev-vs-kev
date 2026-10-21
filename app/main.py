"""FastAPI app: single-ticket triage, batch runs, persisted runs and exports."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import report
from .config import DATA_DIR, HITL_MODES, ROOT, RUNS_DIR, Settings, get_settings, load_taxonomy, normalize_hitl_mode
from .openrouter import make_client
from .definitions_pdf import build_definitions_pdf
from .prompt import build_request
from .triage import MODEL_KEYS, summarize, triage_ticket

MAX_BATCH_ROWS = 500
STATIC_DIR = ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    RUNS_DIR.mkdir(exist_ok=True)
    app.state.client = make_client(get_settings())
    app.state.jobs = {}
    app.state.batch_t0 = {}
    yield
    await app.state.client.aclose()


app = FastAPI(title="JEV vs KEV Triage", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class Ticket(BaseModel):
    id: str | None = None
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    customer_tier: str | None = None
    channel: str | None = None
    expected_team: str | None = None
    expected_critical: bool | None = None
    expected_severity: str | None = None


class TriageRequest(BaseModel):
    ticket: Ticket
    runs: int | None = Field(default=None, ge=1, le=10)
    hitl_mode: str | None = None  # team_or_critical | team | critical (default from .env)


class ReportRequest(BaseModel):
    run: dict  # a run document as returned by /api/triage or /api/runs/{id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _settings_for(hitl_mode: str | None) -> Settings:
    s = get_settings()
    return replace(s, hitl_mode=normalize_hitl_mode(hitl_mode)) if hitl_mode else s


def _run_meta(kind: str, runs_per_ticket: int, s: Settings) -> dict:
    tax = load_taxonomy()
    return {
        "kind": kind,
        "created_at": _now(),
        "models": {m.key: {"slug": m.slug, "label": m.label, "role": m.role, "provider": m.provider} for m in s.models},
        "settings": {
            "hitl_threshold": s.hitl_threshold,
            "hitl_mode": s.hitl_mode,
            "runs_per_ticket": runs_per_ticket,
            "endpoint": s.decisions_url,
        },
        "taxonomy_fingerprint": tax.fingerprint,
        # The ticket-independent part of the request (questions built from the YAML files).
        "questions": build_request(tax, {})["questions"],
    }


def _run_path(run_id: str) -> Path:
    if not run_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "Invalid run id")
    return RUNS_DIR / f"{run_id}.json"


def _load_run(run_id: str) -> dict:
    job = app.state.jobs.get(run_id)
    if job:
        return job
    path = _run_path(run_id)
    if not path.exists():
        raise HTTPException(404, "Run not found")
    return json.loads(path.read_text(encoding="utf-8"))


def _clean_ticket(row: dict, idx: int) -> dict | None:
    row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
    if not row.get("subject") and not row.get("body"):
        return None
    return {
        "id": row.get("id") or f"T-{idx:03d}",
        "subject": row.get("subject") or "(no subject)",
        "body": row.get("body") or row.get("subject", ""),
        "customer_tier": row.get("customer_tier") or None,
        "channel": row.get("channel") or None,
        "expected_team": row.get("expected_team") or None,
        "expected_critical": row.get("expected_critical") or None,
        "expected_severity": row.get("expected_severity") or None,
    }


def parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or not {"subject", "body"} & {f.strip().lower() for f in reader.fieldnames}:
        raise HTTPException(400, "CSV must have at least a 'subject' or 'body' column")
    tickets = [t for i, row in enumerate(reader, 1) if (t := _clean_ticket(row, i))]
    if not tickets:
        raise HTTPException(400, "CSV has no tickets")
    if len(tickets) > MAX_BATCH_ROWS:
        raise HTTPException(400, f"CSV has {len(tickets)} rows; limit is {MAX_BATCH_ROWS}")
    return tickets


# ---------------------------------------------------------------------------
# Routes: config, samples, prompt, single triage
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def api_config():
    s, tax = get_settings(), load_taxonomy()
    return {
        "models": {m.key: {"slug": m.slug, "label": m.label, "role": m.role, "provider": m.provider} for m in s.models},
        "hitl_threshold": s.hitl_threshold,
        "hitl_mode": s.hitl_mode,
        "hitl_modes": HITL_MODES,
        "runs_per_ticket": s.runs_per_ticket,
        "api_key_configured": bool(s.api_key),
        "teams": tax.teams,
        "severity": tax.severity,
        "noul": tax.noul,
        "teams_instructions": tax.teams_cfg.get("instructions"),
        "raw_yaml": tax.raw,
        "taxonomy_fingerprint": tax.fingerprint,
    }


@app.get("/api/definitions.pdf")
async def api_definitions_pdf():
    tax = load_taxonomy()
    pdf = build_definitions_pdf(tax, get_settings())
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="triage-definitions_{tax.fingerprint}.pdf"'},
    )


@app.get("/api/samples")
async def api_samples():
    return json.loads((DATA_DIR / "sample_tickets.json").read_text(encoding="utf-8"))


def _sample_csvs() -> dict[str, Path]:
    return {p.name: p for p in sorted(DATA_DIR.glob("sample_batch*.csv"))}


def _sample_csv(name: str | None) -> Path:
    samples = _sample_csvs()
    path = samples.get(name or "sample_batch.csv")
    if path is None:
        raise HTTPException(404, f"Unknown sample CSV '{name}'. Available: {', '.join(samples)}")
    return path


@app.get("/api/samples/batches")
async def api_sample_batches():
    out = []
    for name, path in _sample_csvs().items():
        rows = max(0, len(path.read_text(encoding="utf-8").strip().splitlines()) - 1)
        out.append({"name": name, "rows": rows})
    return out


@app.get("/api/samples/batch.csv")
async def api_sample_csv(name: str | None = None):
    path = _sample_csv(name)
    return FileResponse(path, media_type="text/csv", filename=path.name)


@app.post("/api/prompt")
async def api_prompt(ticket: Ticket):
    s, tax = get_settings(), load_taxonomy()
    request = build_request(tax, ticket.model_dump())
    return {
        "endpoint": s.decisions_url,
        "models": [m.slug for m in s.models],
        "state": request["state"],
        "questions": request["questions"],
        "request": {"model": "<model slug>", **request},
    }


@app.post("/api/triage")
async def api_triage(req: TriageRequest):
    s, tax = _settings_for(req.hitl_mode), load_taxonomy()
    runs = req.runs or s.runs_per_ticket
    result = await triage_ticket(app.state.client, s, tax, req.ticket.model_dump(), runs)
    return {
        "id": f"single-{uuid.uuid4().hex[:8]}",
        **_run_meta("single", runs, s),
        "status": "done",
        "results": [result],
        "summary": summarize([result], s, tax),
    }


# ---------------------------------------------------------------------------
# Routes: batch
# ---------------------------------------------------------------------------


def _ticket_cost(result: dict) -> dict[str, float]:
    return {
        k: sum(run["metrics"].get("cost_usd") or 0.0 for run in result["runs"][k])
        for k in MODEL_KEYS
    }


def batch_totals(job: dict, duration_s: float) -> dict:
    """Headline numbers for a finished batch: wall-clock time and total spend."""
    models = (job.get("summary") or {}).get("models") or {}
    cost = {k: round(job["live_cost"][k], 8) for k in MODEL_KEYS}
    n = len(job["results"])
    return {
        "duration_s": round(duration_s, 2),
        "tickets": n,
        "calls": sum(models.get(k, {}).get("calls", 0) for k in MODEL_KEYS),
        "tickets_per_min": round(n / duration_s * 60, 1) if duration_s > 0 else None,
        "avg_s_per_ticket": round(duration_s / n, 3) if n else None,
        "concurrency": get_settings().batch_concurrency,
        # Costs are always reported per model, never combined.
        "cost_usd": cost,
        "cost_per_ticket_usd": {k: round(cost[k] / n, 10) if n else None for k in MODEL_KEYS},
        # Sum of each model's request latencies: compute time spent waiting on that model.
        "model_time_s": {k: models.get(k, {}).get("latency_ms", {}).get("total_s") for k in MODEL_KEYS},
    }


async def _run_batch(job: dict, tickets: list[dict], runs: int, s: Settings) -> None:
    tax = load_taxonomy()
    sem = asyncio.Semaphore(s.batch_concurrency)
    results: list[dict | None] = [None] * len(tickets)
    t0 = time.perf_counter()
    app.state.batch_t0[job["id"]] = t0

    async def one(i: int, t: dict) -> None:
        async with sem:
            results[i] = await triage_ticket(app.state.client, s, tax, t, runs)
            for k, c in _ticket_cost(results[i]).items():
                job["live_cost"][k] += c
            job["done"] += 1

    try:
        await asyncio.gather(*(one(i, t) for i, t in enumerate(tickets)))
        job["results"] = [r for r in results if r]
        job["summary"] = summarize(job["results"], s, tax)
        job["status"] = "done"
    except Exception as exc:  # keep partial results visible
        job["results"] = [r for r in results if r]
        job["summary"] = summarize(job["results"], s, tax) if job["results"] else None
        job["status"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"
    job["finished_at"] = _now()
    job["totals"] = batch_totals(job, time.perf_counter() - t0)
    app.state.batch_t0.pop(job["id"], None)
    _run_path(job["id"]).write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    app.state.jobs.pop(job["id"], None)


@app.post("/api/batch")
async def api_batch(
    file: UploadFile | None = File(default=None),
    use_sample: bool = Form(default=False),
    sample: str | None = Form(default=None),
    runs: int | None = Form(default=None),
    hitl_mode: str | None = Form(default=None),
    name: str | None = Form(default=None),
):
    if file is not None and file.filename:
        text = (await file.read()).decode("utf-8", errors="replace")
        source = file.filename
    elif use_sample:
        path = _sample_csv(sample)
        text = path.read_text(encoding="utf-8")
        source = path.name
    else:
        raise HTTPException(400, "Upload a CSV file or set use_sample=true")

    tickets = parse_csv(text)
    settings = _settings_for(hitl_mode)
    runs = max(1, min(10, runs or settings.runs_per_ticket))
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "_" + uuid.uuid4().hex[:6]
    job = {
        "id": run_id,
        **_run_meta("batch", runs, settings),
        "name": name or source,
        "source": source,
        "status": "running",
        "total": len(tickets),
        "done": 0,
        "live_cost": {k: 0.0 for k in MODEL_KEYS},
        "results": [],
        "summary": None,
    }
    app.state.jobs[run_id] = job
    asyncio.create_task(_run_batch(job, tickets, runs, settings))
    return {"id": run_id, "total": len(tickets)}


@app.get("/api/batch/{run_id}/stream")
async def api_batch_stream(run_id: str):
    """Server-sent events with progress; the last event carries the final status."""

    async def events():
        while True:
            job = app.state.jobs.get(run_id)
            if job is None:
                path = _run_path(run_id)
                status = json.loads(path.read_text(encoding="utf-8"))["status"] if path.exists() else "missing"
                yield f"event: end\ndata: {json.dumps({'status': status})}\n\n"
                return
            t0 = app.state.batch_t0.get(run_id)
            progress = {
                "done": job["done"],
                "total": job["total"],
                "elapsed_s": round(time.perf_counter() - t0, 1) if t0 else 0,
                "cost_usd": {k: round(v, 8) for k, v in job["live_cost"].items()},
            }
            # Send on every tick (not only on progress) so the elapsed clock keeps moving.
            yield f"data: {json.dumps(progress)}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# Routes: persisted runs & exports
# ---------------------------------------------------------------------------


@app.get("/api/runs")
async def api_runs():
    items = []
    for path in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        verdict = (doc.get("summary") or {}).get("verdict") or {}
        items.append(
            {
                "id": doc["id"],
                "name": doc.get("name"),
                "created_at": doc.get("created_at"),
                "status": doc.get("status"),
                "tickets": len(doc.get("results", [])),
                "models": doc.get("models"),
                "decision": verdict.get("decision"),
            }
        )
    return items


@app.get("/api/runs/{run_id}")
async def api_run(run_id: str):
    return _load_run(run_id)


@app.delete("/api/runs/{run_id}")
async def api_run_delete(run_id: str):
    path = _run_path(run_id)
    if not path.exists():
        raise HTTPException(404, "Run not found")
    path.unlink()
    return {"deleted": run_id}


CSV_FIELDS_PER_MODEL = [
    ("team", lambda r: (r["parsed"] or {}).get("team")),
    ("team_confidence", lambda r: (r["parsed"] or {}).get("team_confidence")),
    ("p_critical", lambda r: (r["parsed"] or {}).get("noul")),
    ("critical", lambda r: (r["parsed"] or {}).get("critical")),
    ("noul_confidence", lambda r: (r["parsed"] or {}).get("noul_confidence")),
    ("severity_score", lambda r: (r["parsed"] or {}).get("severity_score")),
    ("severity_level", lambda r: (r["parsed"] or {}).get("severity_level")),
    ("severity_confidence", lambda r: (r["parsed"] or {}).get("severity_confidence")),
    ("needs_human", lambda r: r["hitl"]["required"]),
    ("hitl_reasons", lambda r: " | ".join(r["hitl"]["reasons"])),
    ("latency_ms", lambda r: r["metrics"].get("latency_ms")),
    ("ttfb_ms", lambda r: r["metrics"].get("ttfb_ms")),
    ("input_tokens", lambda r: r["metrics"].get("input_tokens")),
    ("output_tokens", lambda r: r["metrics"].get("output_tokens")),
    ("cost_usd", lambda r: r["metrics"].get("cost_usd")),
    ("provider", lambda r: r["metrics"].get("provider")),
    ("model_version", lambda r: r["metrics"].get("model_version")),
    ("error", lambda r: r["call_error"] or "; ".join(r["validation_errors"])),
]


def results_to_csv(results: list[dict]) -> str:
    header = ["id", "subject", "customer_tier", "channel", "expected_team", "expected_critical", "expected_severity"]
    header += [f"{k}_{name}" for k in MODEL_KEYS for name, _ in CSV_FIELDS_PER_MODEL]
    header += ["divergence", "team_match", "noul_match", "severity_delta_kev_minus_jev", "p_critical_delta_kev_minus_jev", "hitl_match"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in results:
        t, c = r["ticket"], r["comparison"]
        row = [t.get("id"), t.get("subject"), t.get("customer_tier"), t.get("channel"),
               t.get("expected_team"), t.get("expected_critical"), t.get("expected_severity")]
        for k in MODEL_KEYS:
            row += [fn(r["primary"][k]) for _, fn in CSV_FIELDS_PER_MODEL]
        row += [c["status"], c["team_match"], c["noul_match"], c["severity_delta"], c["noul_delta"], c["hitl_match"]]
        w.writerow(row)
    return buf.getvalue()


@app.get("/api/runs/{run_id}/export.csv")
async def api_export_csv(run_id: str):
    run = _load_run(run_id)
    return Response(
        results_to_csv(run["results"]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="triage_{run_id}.csv"'},
    )


@app.get("/api/runs/{run_id}/export.json")
async def api_export_json(run_id: str):
    return JSONResponse(
        _load_run(run_id), headers={"Content-Disposition": f'attachment; filename="triage_{run_id}.json"'}
    )


@app.get("/api/runs/{run_id}/report.html")
async def api_report_run(run_id: str, download: bool = False):
    run = _load_run(run_id)
    headers = {"Content-Disposition": f'attachment; filename="jev-vs-kev-report_{run_id}.html"'} if download else {}
    return HTMLResponse(report.render(run, load_taxonomy()), headers=headers)


@app.post("/api/report")
async def api_report_adhoc(req: ReportRequest):
    """Report for a run held by the client (e.g. a single-ticket triage that is not persisted)."""
    run = req.run
    if "results" not in run or "summary" not in run:
        raise HTTPException(400, "Payload must be a run document with 'results' and 'summary'")
    rid = run.get("id", "adhoc")
    return HTMLResponse(
        report.render(run, load_taxonomy()),
        headers={"Content-Disposition": f'attachment; filename="jev-vs-kev-report_{rid}.html"'},
    )
