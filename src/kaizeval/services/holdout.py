"""Independent holdout validation.

This module is the only place that grants access to sealed holdout items for automated use. It runs
the baseline and the final candidate on the holdout split and returns *aggregates only*. No case
text, reference answer or per-case score leaves this module toward the optimizer.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .. import audit
from ..jobs import JobContext
from ..models import Dataset, EvaluationRun, Experiment
from .datasets import HoldoutAccess, split_counts
from .evaluation import create_run, execute_run
from .stats import compare_runs


def validate(db: Session, *, workspace_id: str, agent_id: str, dataset_id: str, candidate_version: str,
             mode: str, config: dict[str, Any], optimization_run_id: str, user_id: str | None,
             ctx: JobContext | None = None) -> dict[str, Any]:
    dataset = db.get(Dataset, dataset_id)
    assert dataset is not None
    if not split_counts(db, dataset_id).get("holdout"):
        return {"status": "skipped", "reason": "the dataset has no holdout cases"}
    access = HoldoutAccess(purpose="holdout_validation", actor=f"optimization:{optimization_run_id}")
    runs: list[EvaluationRun] = []
    for version in ("baseline", candidate_version):
        run = create_run(db, workspace_id=workspace_id, agent_id=agent_id, dataset_id=dataset_id, split="holdout",
                         mode=mode, kind="holdout", agent_version=version, config=config, user_id=user_id,
                         optimization_run_id=optimization_run_id)
        db.commit()
        run = execute_run(db, run.id, ctx=ctx, access=access)
        if run.status != "completed":
            return {"status": "failed", "reason": run.error or "holdout run did not complete"}
        runs.append(run)
    comparison = compare_runs(db, runs[0], runs[1], include_cases=False)
    experiment = Experiment(workspace_id=workspace_id, optimization_run_id=optimization_run_id, kind="holdout",
                            baseline_run_id=runs[0].id, candidate_run_id=runs[1].id, comparison=comparison,
                            decision="final")
    db.add(experiment)
    # The holdout has now informed a decision. It must be rotated before it can gate further tuning.
    dataset.holdout_exposed = True
    audit.record(db, workspace_id=workspace_id, user_id=user_id, action="holdout.validated",
                 subject_type="optimization_run", subject_id=optimization_run_id,
                 detail={"generation": dataset.holdout_generation, "paired_cases": comparison["paired_cases"]})
    db.commit()
    return {
        "status": "completed", "experiment_id": experiment.id, "generation": dataset.holdout_generation,
        "paired_cases": comparison["paired_cases"], "overall": comparison["overall"],
        "by_category": comparison["by_category"], "critical_failures": comparison["critical_failures"],
        "latency_ms": comparison["latency_ms"], "synthetic": comparison["synthetic"],
        "note": "Aggregate result of a sealed set that the optimizer never saw. If you tune further based on "
                "this result, rotate the holdout first.",
    }
