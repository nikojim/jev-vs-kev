# JEV vs KEV — Support Ticket Triage Benchmark

This project benchmarks two **decision models** served through the [OpenRouter Decisions API](https://openrouter.ai/docs/guides/community/jev) side by side. The task is a real one: **triaging customer support tickets**.

| Role | Default model | Purpose |
|---|---|---|
| **Baseline** | `typesafe/jev-1.14` (JEV) | Reference behaviour |
| **Challenger** | `jaredpalmer/kev-4b` (KEV) | Candidate replacement. Is it as good, faster and cheaper? |

Decision models do not generate text. They receive a **state** (the ticket) and a set of **typed questions**, and return typed answers with probabilities attached. Both models receive the exact same request. For each ticket they answer three questions:

| Question | Decision type | Answer |
|---|---|---|
| Which **team** should own it? | `choice` (options from `teams.yaml`) | Chosen team, plus `confidence` and the probability of every team |
| Is it **critical** (the **NOUL**)? | `noul` (definition in `criticality.yaml`) | `P(critical)`. Critical when P ≥ 0.5; confidence = max(P, 1 − P) |
| How **severe** is it? | `score` (ordered levels in `severity.yaml`) | Probability-weighted position on the scale (0 = Low … 3 = Critical), plus `confidence` |

A ticket goes to **human review** when the **team confidence** or the **NOUL confidence** is below **80%**. The threshold is configurable.

The app compares the two models on **quality** and **performance**, shows **where and how often KEV deviates from JEV**, and gives a verdict: *Can KEV replace JEV?*

- **Quality:** agreement, accuracy, calibration, consistency and the human-review rate.
- **Performance:** TTFB, latency, tokens and cost.

---

## Contents

1. [Architecture](#architecture)
2. [Quick start](#quick-start)
3. [Configuration](#configuration)
4. [Request and answers](#request-and-answers)
5. [Human-in-the-loop rule](#human-in-the-loop-rule)
6. [Metrics explained](#metrics-explained)
7. [Using the UI](#using-the-ui)
8. [Batch CSV format](#batch-csv-format)
9. [Reports & exports](#reports--exports)
10. [API reference](#api-reference)
11. [Testing](#testing)
12. [Limitations & troubleshooting](#limitations--troubleshooting)

---

## Architecture

```
Browser (static/index.html + app.js, Chart.js)
   │  REST + Server-Sent Events
   ▼
FastAPI (app/main.py)
   ├─ app/prompt.py      builds state + typed questions from config/*.yaml
   ├─ app/openrouter.py  POST /api/alpha/decisions → TTFB, latency, tokens, cost, retries
   ├─ app/triage.py      answer parsing → HITL rule → JEV/KEV comparison → aggregates & verdict
   └─ app/report.py      self-contained HTML report (inline SVG charts, Jinja2 template)
   │
   ▼
OpenRouter Decisions API  (both models called in parallel for each ticket)
```

```
.
├── .env.example              # all settings (copy to .env)
├── config/
│   ├── teams.yaml            # team options      → `choice` question
│   ├── criticality.yaml      # NOUL definition   → `noul` question
│   └── severity.yaml         # severity levels   → `score` question
├── data/
│   ├── sample_tickets.json   # 12 tickets for the Single-ticket picker
│   └── sample_batch.csv      # 25 labelled tickets for batch runs
├── app/
│   ├── config.py  prompt.py  openrouter.py  triage.py  report.py  main.py
│   └── templates/report.html
├── static/                   # index.html, app.js, styles.css
├── runs/                     # saved batch runs (JSON, created at runtime, git-ignored)
├── tests/test_triage.py      # offline tests (no network)
└── main.py                   # `uv run main.py` launcher
```

## Quick start

Prerequisites: **Python 3.13**, **[uv](https://docs.astral.sh/uv/)**, and an **OpenRouter API key** with credits.

```bash
uv sync
cp .env.example .env          # then set OPENROUTER_API_KEY
uv run main.py                # or: uv run uvicorn app.main:app --reload
```

Open **http://localhost:8000**.

Without an API key the app still starts. It shows a warning banner, and you can still browse samples, inspect requests and read the definitions.

## Configuration

### Environment variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | — | **Required** to call the models |
| `OPENROUTER_DECISIONS_URL` | `https://openrouter.ai/api/alpha/decisions` | Decisions API endpoint |
| `APP_REFERER`, `APP_TITLE` | localhost / app name | OpenRouter attribution headers |
| `JEV_MODEL` / `JEV_LABEL` | `typesafe/jev-1.14` / `JEV 1.14` | Baseline model slug and display name |
| `KEV_MODEL` / `KEV_LABEL` | `jaredpalmer/kev-4b` / `KEV 4B` | Challenger model slug and display name |
| `JEV_PROVIDER_*`, `KEV_PROVIDER_*` | empty | Provider routing per model: `_ORDER`, `_ONLY`, `_IGNORE` (comma lists), `_SORT` (`price`/`throughput`/`latency`), `_ALLOW_FALLBACKS`. Empty = OpenRouter default routing. See [Provider routing](#provider-routing) |
| `JEV_PRICE_IN/OUT`, `KEV_PRICE_IN/OUT` | empty | USD per 1M tokens. Used **only** if the response has no `usage.cost` |
| `HITL_CONFIDENCE_THRESHOLD` | `0.80` | Confidence below this sends the ticket to human review |
| `HITL_MODE` | `team_or_critical` | Which confidences are checked: `team_or_critical`, `team` or `critical`. This is the default; it can be changed per run in the UI |
| `BATCH_CONCURRENCY` | `4` | Tickets processed in parallel during a batch |
| `RUNS_PER_TICKET` | `1` | Default number of repeated runs per model, used to measure consistency |
| `MAX_RETRIES` | `3` | Retries on 408/429/5xx/524/529 and timeouts, with exponential backoff and jitter |
| `REQUEST_TIMEOUT_S` | `60` | Per-request timeout |
| `VERDICT_MIN_AGREEMENT` | `0.85` | Minimum team + NOUL agreement with JEV |
| `VERDICT_MAX_HITL_DELTA` | `0.05` | Maximum allowed increase in KEV's human-review rate over JEV's |
| `VERDICT_MAX_ACCURACY_DROP` | `0.05` | Maximum allowed accuracy drop vs JEV (only when ground truth exists) |

### Taxonomy YAML files

The three YAML files are **re-read on every request**, so edits apply without restarting the server. Their content becomes the typed questions sent to the models, and the **Definitions** tab shows it. Every run is stamped with a short **fingerprint** (a hash of the three files), so you can tell which definitions a run used.

**`config/teams.yaml`** → `choice`. Each team becomes an option, and its criterion text is `name: description. Examples: …`.
```yaml
instructions: Which support team should own this ticket?
teams:
  - id: billing
    name: Billing & Payments
    description: Invoices, charges, refunds, subscription plans, payment methods...
    examples: [Charged twice for the same month, Need a VAT invoice]
```

**`config/criticality.yaml`** → `noul`. A yes/no question; the model returns P(true).
```yaml
noul:
  instructions: Is this ticket critical — does it require an immediate, on-call response?
  criteria:
    "true": Production is down ... Needs a response within 15 minutes and on-call paging.
    "false": Partial loss of functionality with a workaround ...
  outcomes:                       # display only
    "true":  { name: Critical, response_sla: 15 minutes, action: Page on-call and open an incident }
    "false": { name: Not critical, response_sla: Normal queue, action: Route to the assigned team queue }
  decision_threshold: 0.5
```

**`config/severity.yaml`** → `score`. The levels are ordered from least to most severe.
```yaml
instructions: How severe is the business impact of this ticket?
scoring_factors: [Number of users affected, Availability of a workaround, ...]
levels:
  - { id: low, name: Low, definition: Cosmetic issue or question... }
  - { id: medium, ... }
  - { id: high, ... }
  - { id: critical, ... }
```

You can add, remove or rename teams and severity levels. Severity colours follow the level order: grey → yellow → orange → red.

### Provider routing

OpenRouter can serve the same model through several providers. KEV, for example, is served by SiliconFlow and Phala. The providers can behave very differently:

| Run (100 tickets) | KEV via SiliconFlow | KEV via Phala | Batch time |
|---|---|---|---|
| Normal | p50 657 ms | p50 651 ms | 22 s |
| Phala overloaded (returning HTTP 429) | p50 661 ms | p50 2.7 s, max 58 s | 163 s |

Pin or exclude providers in `.env` to keep the comparison stable:

```bash
KEV_PROVIDER_IGNORE=Phala          # never route KEV to Phala
# or: KEV_PROVIDER_ORDER=SiliconFlow,Phala   (prefer SiliconFlow, fall back to Phala)
# or: KEV_PROVIDER_SORT=latency               (let OpenRouter pick the fastest)
```

The preferences are sent as the request's `provider` field. They are stored with each run (`models.<key>.provider`). The provider that actually served each call is shown in the model cards, the dashboard and the CSV export.

## Request and answers

Each model call is a `POST` to `/api/alpha/decisions`. Use **View request** in the UI to see the exact request.

```json
{
  "model": "typesafe/jev-1.14",
  "state": { "customer_tier": "enterprise", "subject": "Dashboard returns 502", "body": "All 340 users get 502 since 09:10" },
  "questions": {
    "team":     { "type": "choice", "instructions": "Which support team should own this ticket?", "criteria": { "billing": "Billing & Payments: ...", "...": "..." } },
    "critical": { "type": "noul",   "instructions": "Is this ticket critical — ...", "criteria": { "true": "...", "false": "..." } },
    "severity": { "type": "score",  "instructions": { "question": "How severe ...", "consider": ["..."] }, "criteria": ["Low: ...", "Medium: ...", "High: ...", "Critical: ..."] }
  }
}
```

Response (real `typesafe/jev-1.14` output):

```json
{
  "model": "typesafe/jev-1.14-20261014",
  "provider": "TypeSafe",
  "answers": {
    "team":     { "type": "choice", "choice": "platform_reliability", "confidence": 1, "probabilities": { "billing": 0, "platform_reliability": 1 } },
    "critical": { "type": "noul", "noul": 0.94 },
    "severity": { "type": "score", "score": 2.82, "confidence": 0.83, "probabilities": { "0": 0, "1": 0, "2": 0.17, "3": 0.83 } }
  },
  "usage": { "input_tokens": 435, "output_tokens": 69, "cost": 0.00001827 }
}
```

The app turns these answers into one triage record for each model:

- **team** and **team_confidence**, from the `choice` answer.
- **P(critical)**, taken from `noul`. From it the app derives:
  - **critical** = P ≥ `decision_threshold`
  - **noul_confidence** = max(P, 1 − P)
- **severity_score**, taken from `score`, plus the nearest **severity level** and the **severity_confidence**.

An unknown team id or a missing answer is reported as a validation error and sends the ticket to human review.

## Human-in-the-loop rule

Which confidences are checked is configurable. There are three modes:

| # | Mode | Ticket goes to human review when… |
|---|---|---|
| 1 | `team_or_critical` (default) | team confidence < threshold **or** NOUL confidence < threshold |
| 2 | `team` | team confidence < threshold |
| 3 | `critical` | NOUL confidence < threshold |

`noul_confidence` = max(P(critical), 1 − P(critical)). In every mode, a failed call or an invalid answer also sends the ticket to human review.

Set the default with `HITL_MODE` in `.env`, or pick a mode per run in the **Single ticket** and **Batch** forms ("Human review when confidence is low on"). The mode is saved with each run. It is shown next to the results, in the dashboard verdict and in the report. Confidence bars that the mode does not check are drawn without a threshold mark and labelled "not checked by rule".

With the 0.80 threshold, any ticket where 0.2 < P(critical) < 0.8 has NOUL confidence below 80%; in modes 1 and 3 it goes to a human. The rule is evaluated **per model**, and the UI lists the exact reasons, for example `NOUL confidence 59% < 80% (P(critical) = 41%)`. Severity confidence is shown but does not trigger review.

## Metrics explained

| Metric | How it is measured |
|---|---|
| **TTFB** | Time from sending the request to receiving the response headers. The Decisions API returns a single JSON document and does **not stream tokens**, so time-to-first-token does not exist for these models. TTFB is the closest equivalent |
| **Latency** | Total time until the full response body has been read |
| **Tokens** | `usage.input_tokens` and `usage.output_tokens` |
| **Cost** | `usage.cost` from OpenRouter. If it is missing, `.env` pricing is used. The tooltip shows which source applied |
| **Cost / 1k tickets** | Average cost per call × 1000 |
| **Provider / snapshot** | The provider that served the call and the exact model snapshot, e.g. `typesafe/jev-1.14-20261014` |
| **Agreement** | Share of tickets where KEV's team, or KEV's critical / not-critical decision, matches JEV's |
| **Divergence status** | See below |
| **Mean \|Δ\|** | Average absolute difference between KEV and JEV in severity position and in P(critical) |
| **Accuracy** | Only when the CSV has `expected_*` columns. Covers team, NOUL, team + NOUL, and severity MAE (measured in levels) |
| **Calibration** | Confidence is grouped into bins, and each bin's average confidence is compared with its actual accuracy. Points on the diagonal mean the model is well calibrated; points below it mean it is over-confident |
| **Consistency** | With `runs > 1`: how often the repeated runs agree on team and NOUL, plus the standard deviation of severity. The **modal** answer is used as the model's result |

Divergence status:

- **Aligned:** same team, same critical / not-critical decision, and severity within ±0.5 levels.
- **Partial:** the team or the NOUL decision differs (or severity drifts by more than ±0.5).
- **Diverges:** both the team and the NOUL decision differ.
- **Error:** a model failed.

### Verdict: "Can KEV replace JEV?"

KEV passes only if **all** of these hold:

1. Team + NOUL agreement with JEV ≥ `VERDICT_MIN_AGREEMENT`.
2. KEV's human-review rate − JEV's human-review rate ≤ `VERDICT_MAX_HITL_DELTA`.
3. If ground truth exists: JEV accuracy − KEV accuracy ≤ `VERDICT_MAX_ACCURACY_DROP`.
4. KEV has no more failed or invalid calls than JEV.

Cost, latency and TTFB differences appear as notes next to the verdict.

## Using the UI

### Single ticket

Write a ticket or pick one of the 12 samples, choose the number of runs, and click **Run triage**. Each model card shows:

- **Team**, with its top probabilities and a confidence bar. The bar marks the review threshold and turns red below it.
- **NOUL**: a Critical / Not critical pill with its SLA, the P(critical) bar, and the NOUL confidence bar.
- **Severity**: the levels shaded by probability, plus the score and the confidence.
- A **human-review** or **auto-routed** badge, with the reasons.
- Metrics, the provider, and the raw API response.

A banner says whether KEV agrees with, partially deviates from, or diverges from JEV. **View request** shows the questions, the state, and the full request JSON, with a copy button.

### Batch

Upload a CSV or click **Run sample CSV**. Progress streams live. The results table can be filtered by *diverges*, *needs human* and *errors*. Click a row to open that ticket in the single view.

### Decision dashboard

Pick any saved run. The dashboard shows:

- The verdict card and the KPI tiles.
- How often KEV deviates from JEV.
- Speed / tokens / cost small multiples. Each has its own scale, and ★ marks the better value.
- The quality chart.
- The share of tickets flagged critical, and the team and severity distributions.
- **P(critical) and severity scatter plots, JEV vs KEV.** The P(critical) plot shades the human-review band.
- Team and NOUL confusion matrices (JEV × KEV).
- Calibration charts.

### Definitions

Shows the three questions as the models see them, plus the raw YAML.

**Export definitions (PDF)** in the Definitions tab (or `GET /api/definitions.pdf`) downloads a PDF with:

- The human-review rule: the threshold and the three modes, with the default marked.
- Teams, with descriptions and examples.
- NOUL outcomes, with SLA, action and criteria.
- Severity levels and scoring factors.
- An appendix with the exact questions sent to the models.

The file name includes the taxonomy fingerprint, so each PDF matches the YAML version it was generated from.

### Colours and navigation

Colours are fixed per model everywhere: **JEV = blue**, **KEV = orange**. Agreement is green, partial deviation is yellow, and divergence is red. Critical is red and Not critical is grey; both always carry a text label.

Light and dark themes are supported; use the ◐ toggle in the header. Tabs can be deep-linked, for example `/#dashboard`.

## Batch CSV format

| Column | Required | Notes |
|---|---|---|
| `subject` | yes* | *At least one of `subject` / `body` is needed |
| `body` | yes* | |
| `id` | no | Auto-generated (`T-001`…) if missing |
| `customer_tier` | no | e.g. `free`, `pro`, `enterprise` |
| `channel` | no | e.g. `email`, `chat`, `phone`, `web_form` |
| `expected_team` | no | Ground truth: enables accuracy and calibration |
| `expected_critical` | no | Ground truth: `true` / `false` (also accepts yes/no, 1/0) |
| `expected_severity` | no | Ground truth: a level id or name (`low`, `High`), or a 0-based index |

There is a limit of 500 rows per batch. Two labelled samples are included and can be picked from the **Sample CSV** selector in the Batch tab:

- [`data/sample_batch.csv`](data/sample_batch.csv): 25 tickets, for a quick smoke test.
- [`data/sample_batch_100.csv`](data/sample_batch_100.csv): 100 hand-written tickets for a more substantial comparison. It has 16–18 tickets per team and 18 critical tickets. Severity is split into 44 low, 28 medium, 12 high and 16 critical. It covers all tiers and channels and includes deliberately ambiguous tickets.

Any `data/sample_batch*.csv` file you add appears in the selector automatically.

## Reports & exports

Every batch run is saved to `runs/<timestamp>_<id>.json` and can be reopened from the dashboard.

While a batch runs, the progress bar shows tickets done, elapsed time, the estimated time left and the money spent so far by each model. When it finishes, **Batch run — time & cost** tiles appear in the Batch tab, the dashboard and the report. They show:

- **Total batch time:** wall-clock, from start to finish.
- **Throughput:** tickets per minute and seconds per ticket.
- **Total cost per model:** for JEV and KEV separately, plus cost per ticket and per 1k tickets. Costs are never combined across models.
- **Sum of model latencies:** for each model, its request latencies added up, plus its average latency per call. Both models run in parallel for every ticket, and `BATCH_CONCURRENCY` tickets run at a time. So this sum is larger than the total batch time. It measures how much compute time each model used, independent of concurrency, which makes it the fair metric for comparing JEV against KEV.

The same numbers are stored under `totals` in the run JSON.

| Export | Where | Content |
|---|---|---|
| **HTML report** | Batch / Dashboard → *Open report* or *Download report*. Single ticket → *Export report* | See the list below |
| **PDF** | Open the HTML report → *Print / Save as PDF* | Print-optimised layout |
| **CSV** | Batch / Dashboard → *CSV* | One row per ticket with both models' team, P(critical), NOUL decision, severity, confidences, HITL decision and reasons, metrics, provider, snapshot and divergence |
| **JSON** | Batch / Dashboard / Single → *JSON* | The full raw run, including every repeated call and the raw API responses |

The HTML report is a self-contained file with no external dependencies. It contains:

- Run metadata and the verdict.
- An executive summary.
- Performance & cost and quality charts.
- The NOUL distribution.
- Confusion matrices.
- P(critical) and severity scatter plots.
- Calibration charts.
- Divergent tickets shown side by side.
- The human-review list.
- An appendix with all tickets, the definitions, and the exact questions sent.

## API reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/config` | Models, threshold, teams, NOUL definition, severity levels, raw YAML |
| `GET` | `/api/definitions.pdf` | Definitions (teams, NOUL, severity, human-review rule) as PDF |
| `GET` | `/api/samples` | Sample tickets |
| `GET` | `/api/samples/batches` | Available sample CSVs with row counts |
| `GET` | `/api/samples/batch.csv?name=…` | Download a sample CSV (default `sample_batch.csv`) |
| `POST` | `/api/prompt` | Body: ticket. Returns the endpoint, `state`, `questions` and the full request |
| `POST` | `/api/triage` | Body: `{"ticket": {...}, "runs": 1}`. Runs both models and returns a run document |
| `POST` | `/api/batch` | Multipart: `file` **or** `use_sample=true` (+ optional `sample=<file name>`), plus optional `runs` and `name`. Returns `{id, total}` |
| `GET` | `/api/batch/{id}/stream` | SSE progress `{done, total}`. The final `end` event carries the status |
| `GET` | `/api/runs` | Saved runs (most recent first) |
| `GET` / `DELETE` | `/api/runs/{id}` | Fetch or delete a run |
| `GET` | `/api/runs/{id}/export.csv` · `export.json` | Exports |
| `GET` | `/api/runs/{id}/report.html[?download=true]` | HTML report |
| `POST` | `/api/report` | Body: `{"run": <run document>}`. Report for an unsaved (single-ticket) run |

Example:

```bash
curl -X POST localhost:8000/api/triage -H "Content-Type: application/json" \
  -d '{"ticket":{"subject":"Dashboard returns 502","body":"All 340 users get 502 since 09:10","customer_tier":"enterprise"},"runs":1}'
```

Interactive docs are available at `/docs`.

## Testing

```bash
uv run pytest
```

The tests run fully offline. They use a fake Decisions endpoint (`httpx.MockTransport`) built on real response shapes, and cover:

- Request building from YAML
- Answer parsing
- The HITL threshold, including the NOUL confidence band
- Ground-truth parsing
- Divergence statuses
- Retries and non-retryable errors
- TTFB, token and cost capture
- Aggregation and the verdict
- Report rendering

## Limitations & troubleshooting

- **"… is a decisions model and cannot be used with the chat/completions endpoint"**: an older `.env` still points at chat completions. The app now always uses `OPENROUTER_DECISIONS_URL`, and the old `OPENROUTER_BASE_URL`, `TEMPERATURE`, `MAX_TOKENS` and `USE_JSON_MODE` variables are ignored.
- **`HTTP 402`**: the OpenRouter account has no credits left.
- **`HTTP 404`**: check the model slugs in `.env`.
- **`HTTP 429` / `524` / `529`**: the call is retried automatically. If it keeps happening, lower `BATCH_CONCURRENCY`.
- **No TTFT**: decision models do not stream tokens, so TTFB is reported instead. It includes network and queueing time at OpenRouter and its providers, so compare models within the same run rather than across days.
- **Different providers**: the two models may be served by different providers (e.g. TypeSafe vs SiliconFlow). Latency differences partly reflect the provider; the dashboard shows which provider served each model.
- **A batch suddenly takes much longer**: check which provider served the slow calls (CSV `*_provider` column, or the providers note in the dashboard). If one provider is overloaded, exclude it with `*_PROVIDER_IGNORE`. See [Provider routing](#provider-routing).
- **Probabilities are model-reported.** Check the calibration charts before trusting the 80% rule.
- Runs in progress are held in memory. If the server restarts mid-batch, that batch is lost; completed runs are on disk.
