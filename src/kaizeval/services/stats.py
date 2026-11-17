"""Paired comparison of two evaluation runs over the same cases."""

from __future__ import annotations

import math
import random
import statistics
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CaseResult, EvaluationRun, TestCase


def sign_test_p(wins: int, losses: int) -> float | None:
    """Two-sided exact sign test on the non-tied pairs."""
    n = wins + losses
    if n == 0:
        return None
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 4)


def bootstrap_ci(deltas: list[float], *, resamples: int = 2000, seed: int = 7) -> tuple[float, float] | None:
    """95% percentile bootstrap interval for the mean paired difference."""
    if len(deltas) < 2:
        return None
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(statistics.fmean(rng.choices(deltas, k=n)) for _ in range(resamples))
    return round(means[int(0.025 * resamples)], 4), round(means[int(0.975 * resamples) - 1], 4)


def paired_delta(a: list[float], b: list[float]) -> dict[str, Any]:
    """Compare per-case scores of baseline ``a`` and candidate ``b`` (same order, same cases)."""
    deltas = [y - x for x, y in zip(a, b, strict=True)]
    n = len(deltas)
    if n == 0:
        return {"n": 0, "verdict": "no_data"}
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    delta = mean_b - mean_a
    wins, losses = sum(1 for d in deltas if d > 1e-9), sum(1 for d in deltas if d < -1e-9)
    ci = bootstrap_ci(deltas)
    if ci is None:
        verdict = "inconclusive"
    elif ci[0] > 0:
        verdict = "improved"
    elif ci[1] < 0:
        verdict = "regressed"
    else:
        verdict = "inconclusive"
    return {
        "n": n, "baseline_mean": round(mean_a, 4), "candidate_mean": round(mean_b, 4),
        "delta": round(delta, 4), "delta_percentage_points": round(delta * 100, 2),
        # A relative change is only meaningful against a non-zero baseline.
        "relative_change_pct": round(delta / mean_a * 100, 2) if mean_a > 0 else None,
        "ci95": list(ci) if ci else None,
        "ci95_percentage_points": [round(ci[0] * 100, 2), round(ci[1] * 100, 2)] if ci else None,
        "wins": wins, "losses": losses, "ties": n - wins - losses, "sign_test_p": sign_test_p(wins, losses),
        "verdict": verdict,
        "interpretation": {
            "improved": "The candidate scores higher and the 95% interval excludes zero.",
            "regressed": "The candidate scores lower and the 95% interval excludes zero.",
            "inconclusive": "The difference could be noise at this sample size; do not read it as a real change.",
        }[verdict],
    }


def compare_runs(db: Session, baseline: EvaluationRun, candidate: EvaluationRun, *, include_cases: bool = True) -> dict[str, Any]:
    """Paired A/B comparison. With ``include_cases=False`` only aggregates are returned (used for the holdout)."""
    def scored(run: EvaluationRun) -> dict[str, CaseResult]:
        return {r.case_id: r for r in db.scalars(select(CaseResult).where(CaseResult.run_id == run.id))
                if r.overall is not None}

    ra, rb = scored(baseline), scored(candidate)
    shared = sorted(set(ra) & set(rb))
    cases = {c.id: c for c in db.scalars(select(TestCase).where(TestCase.id.in_(shared)))} if shared else {}
    overall = paired_delta([ra[i].overall for i in shared], [rb[i].overall for i in shared])  # type: ignore[misc]

    categories: dict[str, dict[str, Any]] = {}
    for category in sorted({cases[i].category for i in shared}):
        ids = [i for i in shared if cases[i].category == category]
        a_mean = statistics.fmean(ra[i].overall for i in ids)  # type: ignore[misc]
        b_mean = statistics.fmean(rb[i].overall for i in ids)  # type: ignore[misc]
        categories[category] = {"n": len(ids), "baseline_mean": round(a_mean, 4), "candidate_mean": round(b_mean, 4),
                                "delta_percentage_points": round((b_mean - a_mean) * 100, 2)}

    dimensions: dict[str, dict[str, Any]] = {}
    for dim in sorted({d for i in shared for d in (ra[i].final.get("scores") or {})} |
                      {d for i in shared for d in (rb[i].final.get("scores") or {})}):
        pairs = [(ra[i].final["scores"][dim]["score"], rb[i].final["scores"][dim]["score"]) for i in shared
                 if dim in (ra[i].final.get("scores") or {}) and dim in (rb[i].final.get("scores") or {})]
        if pairs:
            a_mean, b_mean = statistics.fmean(p[0] for p in pairs), statistics.fmean(p[1] for p in pairs)
            dimensions[dim] = {"n": len(pairs), "baseline_mean": round(a_mean, 4), "candidate_mean": round(b_mean, 4),
                               "delta_percentage_points": round((b_mean - a_mean) * 100, 2)}

    crit_a = {i for i in shared if ra[i].final.get("critical_failure")}
    crit_b = {i for i in shared if rb[i].final.get("critical_failure")}
    sa, sb = baseline.summary or {}, candidate.summary or {}
    result: dict[str, Any] = {
        "baseline_run_id": baseline.id, "candidate_run_id": candidate.id,
        "baseline_version": baseline.agent_version, "candidate_version": candidate.agent_version,
        "split": baseline.split, "paired_cases": len(shared),
        "unpaired_cases": len(set(ra) ^ set(rb)),
        "overall": overall, "by_category": categories, "by_dimension": dimensions,
        "critical_failures": {"baseline": len(crit_a), "candidate": len(crit_b), "new": len(crit_b - crit_a),
                              "fixed": len(crit_a - crit_b)},
        "pass_rate": {"baseline": sa.get("pass_rate"), "candidate": sb.get("pass_rate")},
        "latency_ms": {"baseline_p50": (sa.get("latency_ms") or {}).get("p50"), "candidate_p50": (sb.get("latency_ms") or {}).get("p50"),
                       "baseline_p95": (sa.get("latency_ms") or {}).get("p95"), "candidate_p95": (sb.get("latency_ms") or {}).get("p95")},
        "agent_cost": {"baseline": (sa.get("agent") or {}).get("estimated_cost"), "candidate": (sb.get("agent") or {}).get("estimated_cost")},
        "synthetic": bool(baseline.synthetic or candidate.synthetic),
        "conditions": {"same_dataset_version": baseline.dataset_version == candidate.dataset_version,
                       "same_mode": baseline.mode == candidate.mode, "dataset_version": baseline.dataset_version},
    }
    if include_cases:
        result["regressed_case_ids"] = [i for i in shared if rb[i].overall < ra[i].overall - 1e-9]  # type: ignore[operator]
        result["improved_case_ids"] = [i for i in shared if rb[i].overall > ra[i].overall + 1e-9]  # type: ignore[operator]
    return result
