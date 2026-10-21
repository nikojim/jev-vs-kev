"""Settings (.env) and triage taxonomy (YAML) loading."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"

load_dotenv(ROOT / ".env")


def _float(name: str, default: float | None = None) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# Which confidences can send a ticket to human review.
HITL_MODES = {
    "team_or_critical": "Team or critical",
    "team": "Team only",
    "critical": "Critical (NOUL) only",
}


def normalize_hitl_mode(value: str | None) -> str:
    mode = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    mode = {"1": "team_or_critical", "2": "team", "3": "critical", "team_or_noul": "team_or_critical", "noul": "critical"}.get(mode, mode)
    return mode if mode in HITL_MODES else "team_or_critical"


@dataclass(frozen=True)
class ModelSpec:
    key: str  # "jev" | "kev"
    slug: str
    label: str
    role: str  # "baseline" | "challenger"
    price_in: float | None  # USD per 1M input tokens (fallback)
    price_out: float | None  # USD per 1M output tokens (fallback)
    provider: dict | None = None  # OpenRouter provider preferences, sent as-is in the request


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


def provider_prefs(prefix: str) -> dict | None:
    """Build OpenRouter `provider` preferences from {PREFIX}_PROVIDER_* env vars (None = OpenRouter default routing)."""
    prefs: dict = {}
    if order := _list(f"{prefix}_PROVIDER_ORDER"):
        prefs["order"] = order
    if only := _list(f"{prefix}_PROVIDER_ONLY"):
        prefs["only"] = only
    if ignore := _list(f"{prefix}_PROVIDER_IGNORE"):
        prefs["ignore"] = ignore
    if sort := os.getenv(f"{prefix}_PROVIDER_SORT", "").strip().lower():
        if sort in {"price", "throughput", "latency"}:
            prefs["sort"] = sort
    fallbacks = os.getenv(f"{prefix}_ALLOW_FALLBACKS", "").strip().lower()
    if fallbacks in {"true", "false", "1", "0", "yes", "no"}:
        prefs["allow_fallbacks"] = fallbacks in {"true", "1", "yes"}
    return prefs or None


@dataclass(frozen=True)
class Settings:
    api_key: str
    decisions_url: str
    referer: str
    title: str
    models: tuple[ModelSpec, ...]
    hitl_threshold: float
    hitl_mode: str
    batch_concurrency: int
    runs_per_ticket: int
    max_retries: int
    timeout_s: float
    verdict_min_agreement: float
    verdict_max_hitl_delta: float
    verdict_max_accuracy_drop: float
    extra: dict = field(default_factory=dict)

    def model(self, key: str) -> ModelSpec:
        return next(m for m in self.models if m.key == key)


@lru_cache
def get_settings() -> Settings:
    return Settings(
        api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        decisions_url=os.getenv("OPENROUTER_DECISIONS_URL", "").strip() or "https://openrouter.ai/api/alpha/decisions",
        referer=os.getenv("APP_REFERER", "http://localhost:8000"),
        title=os.getenv("APP_TITLE", "JEV vs KEV Triage"),
        models=(
            ModelSpec(
                key="jev",
                slug=os.getenv("JEV_MODEL", "typesafe/jev-1.14"),
                label=os.getenv("JEV_LABEL", "JEV 1.14"),
                role="baseline",
                price_in=_float("JEV_PRICE_IN"),
                price_out=_float("JEV_PRICE_OUT"),
                provider=provider_prefs("JEV"),
            ),
            ModelSpec(
                key="kev",
                slug=os.getenv("KEV_MODEL", "jaredpalmer/kev-4b"),
                label=os.getenv("KEV_LABEL", "KEV 4B"),
                role="challenger",
                price_in=_float("KEV_PRICE_IN"),
                price_out=_float("KEV_PRICE_OUT"),
                provider=provider_prefs("KEV"),
            ),
        ),
        hitl_threshold=_float("HITL_CONFIDENCE_THRESHOLD", 0.80),
        hitl_mode=normalize_hitl_mode(os.getenv("HITL_MODE")),
        batch_concurrency=max(1, _int("BATCH_CONCURRENCY", 4)),
        runs_per_ticket=max(1, _int("RUNS_PER_TICKET", 1)),
        max_retries=max(0, _int("MAX_RETRIES", 3)),
        timeout_s=_float("REQUEST_TIMEOUT_S", 60.0),
        verdict_min_agreement=_float("VERDICT_MIN_AGREEMENT", 0.85),
        verdict_max_hitl_delta=_float("VERDICT_MAX_HITL_DELTA", 0.05),
        verdict_max_accuracy_drop=_float("VERDICT_MAX_ACCURACY_DROP", 0.05),
    )


@dataclass(frozen=True)
class Taxonomy:
    teams_cfg: dict
    severity: dict
    criticality: dict
    raw: dict[str, str]  # file name -> raw YAML text (for the Definitions tab)
    fingerprint: str  # short hash of the three files, stamped on every run

    @property
    def teams(self) -> list[dict]:
        return self.teams_cfg["teams"]

    @property
    def team_ids(self) -> list[str]:
        return [t["id"] for t in self.teams]

    @property
    def noul(self) -> dict:
        return self.criticality["noul"]

    @property
    def noul_threshold(self) -> float:
        return float(self.noul.get("decision_threshold", 0.5))

    @property
    def severity_levels(self) -> list[dict]:
        return self.severity["levels"]

    def severity_level(self, score: float | None) -> dict | None:
        """Nearest level for a probability-weighted score position."""
        if score is None:
            return None
        levels = self.severity_levels
        return levels[max(0, min(len(levels) - 1, round(score)))]

    def severity_index(self, value) -> int | None:
        """Accepts a level id, a level name or a 0-based index (ground-truth parsing)."""
        if value is None or str(value).strip() == "":
            return None
        text = str(value).strip().lower()
        for i, lvl in enumerate(self.severity_levels):
            if text in (lvl["id"].lower(), lvl["name"].lower()):
                return i
        try:
            idx = int(float(text))
        except ValueError:
            return None
        return idx if 0 <= idx < len(self.severity_levels) else None


def load_taxonomy(config_dir: Path = CONFIG_DIR) -> Taxonomy:
    # Re-read on every call so YAML edits apply without restarting the server.
    raw = {
        name: (config_dir / name).read_text(encoding="utf-8")
        for name in ("teams.yaml", "criticality.yaml", "severity.yaml")
    }
    digest = hashlib.sha256("".join(raw.values()).encode()).hexdigest()[:10]
    return Taxonomy(
        teams_cfg=yaml.safe_load(raw["teams.yaml"]),
        criticality=yaml.safe_load(raw["criticality.yaml"]),
        severity=yaml.safe_load(raw["severity.yaml"]),
        raw=raw,
        fingerprint=digest,
    )
