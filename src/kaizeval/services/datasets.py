"""Test datasets: generation from evidence, import of approved Q&A, splitting, coverage and access control.

Holdout isolation is enforced here. ``regression_cases`` is the only reader that optimization code
may use. Holdout content is released solely through ``holdout_cases``, which needs a
``HoldoutAccess`` grant that only the holdout validation service and audited human review create.
"""

from __future__ import annotations

import csv
import io
import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..gateway import Gateway, GatewayError
from ..ground_truth import DocumentGroundTruthProvider, current_kb_chunks
from ..models import Dataset, Document, TestCase
from ..textutil import expected_script, normalize, overlap_recall, similarity, written_in_script

ANSWERABLE_CATEGORIES = ["knowledge", "citation", "edge_case"]
MAX_TESTERS = 8
# Each tester writes its own tests independently, from its own share of the knowledge and with its own angle.
TESTER_PERSONAS = [
    "an everyday customer who asks direct, practical questions",
    "an edge-case hunter who writes terse, oddly phrased or slightly misspelled questions",
    "a sceptical auditor who wants the exact source, numbers and conditions",
    "a confused newcomer who mixes up terms and asks vague questions",
    "a power user who asks detailed, multi-part questions",
    "an impatient user who writes very short messages",
    "a careful reader who asks about exceptions and limits",
    "a non-native speaker who uses simple wording",
]
SEVERITIES = ["low", "normal", "high", "critical"]


class DatasetError(Exception):
    pass


@dataclass(frozen=True)
class HoldoutAccess:
    """Proof that a caller is allowed to read sealed holdout items. Never created by optimization code."""

    purpose: str  # "holdout_validation" | "human_review"
    actor: str


# --- access domains -----------------------------------------------------------------------


def regression_cases(db: Session, dataset_id: str) -> list[TestCase]:
    return list(db.scalars(select(TestCase).where(TestCase.dataset_id == dataset_id, TestCase.split == "regression")
                           .order_by(TestCase.created_at, TestCase.id)))


def holdout_cases(db: Session, dataset_id: str, access: HoldoutAccess) -> list[TestCase]:
    if not isinstance(access, HoldoutAccess) or access.purpose not in ("holdout_validation", "human_review"):
        raise PermissionError("holdout items are sealed")
    return list(db.scalars(select(TestCase).where(TestCase.dataset_id == dataset_id, TestCase.split == "holdout")
                           .order_by(TestCase.created_at, TestCase.id)))


def cases_for_run(db: Session, dataset_id: str, split: str, access: HoldoutAccess | None = None) -> list[TestCase]:
    if split == "regression":
        return regression_cases(db, dataset_id)
    if split == "holdout":
        if access is None:
            raise PermissionError("holdout items are sealed")
        return holdout_cases(db, dataset_id, access)
    raise DatasetError(f"unknown split '{split}'")


# --- generation ---------------------------------------------------------------------------


def _split(cases: list[dict[str, Any]], fraction: float, seed: int) -> None:
    """Stratified, seeded assignment made before any optimization. Variants follow their source case."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        if not case.get("variant_of"):
            groups[case["category"]].append(case)
    rng = random.Random(seed)
    for category in sorted(groups):
        members = groups[category]
        rng.shuffle(members)
        n_holdout = round(len(members) * fraction)
        if fraction > 0 and len(members) >= 3:
            n_holdout = max(1, n_holdout)
        n_holdout = min(n_holdout, max(0, len(members) - 1))
        for index, case in enumerate(members):
            case["split"] = "holdout" if index < n_holdout else "regression"
    by_key = {c["key"]: c for c in cases}
    for case in cases:
        if case.get("variant_of"):
            case["split"] = by_key[case["variant_of"]]["split"]


def _draft_cases(db: Session, gateway: Gateway, *, workspace_id: str, n_cases: int, categories: list[str],
                 include_multi_turn: bool, unanswerable_share: float, languages: list[str],
                 document_ids: list[str] | None, seed: int, variation: int, existing_questions: set[str],
                 notes: list[str], testers: int = 1, keep_unverified: bool = False,
                 asked: dict[str, list[str]] | None = None) -> tuple[list[dict[str, Any]], bool]:
    """Draft test cases from the knowledge base.

    ``asked`` maps a passage id to the questions the dataset already holds about it. When given
    (a fresh holdout for a dataset that exists), passages with the fewest questions are preferred
    and the tester is told what was already asked, so the new questions are really new."""
    provider = DocumentGroundTruthProvider(db, workspace_id)
    chunks = [(c, d) for c, d in current_kb_chunks(db, workspace_id, document_ids) if len(c.text) >= 60]
    if not chunks:
        raise DatasetError("no parsed knowledge documents: upload documents before generating tests")
    flagged = [c for c, _ in chunks if c.flags]
    if flagged:
        notes.append(f"{len(flagged)} passage(s) contain text that looks like instructions; they are used as evidence only.")
    answerable_categories = [c for c in categories if c in ANSWERABLE_CATEGORIES] or ["knowledge"]
    n_unanswerable = max(1, round(n_cases * unanswerable_share)) if unanswerable_share > 0 else 0
    n_answerable = max(1, n_cases - n_unanswerable)
    per_chunk = max(1, min(3, math.ceil(n_answerable / len(chunks))))
    rng = random.Random(seed)
    wanted_chunks = min(len(chunks), math.ceil(n_answerable / per_chunk))
    if asked is not None:
        order = {c.id: rng.random() for c, _ in chunks}
        selected = sorted(chunks, key=lambda pair: (len(asked.get(pair[0].id, [])), order[pair[0].id]))[:wanted_chunks]
    elif wanted_chunks < len(chunks):
        step = len(chunks) / wanted_chunks
        offset = rng.random() * step
        selected = [chunks[int(offset + i * step) % len(chunks)] for i in range(wanted_chunks)]
    else:
        selected = chunks
    by_ref = {f"chunk:{c.id}": (c, d) for c, d in selected}
    synthetic = False

    testers = max(1, min(MAX_TESTERS, int(testers), len(selected)))
    questions: list[dict[str, Any]] = []
    dropped = 0
    for tester in range(testers):
        # Round-robin split: every tester works on its own passages, in its own model context.
        share = selected[tester::testers]
        persona = TESTER_PERSONAS[tester % len(TESTER_PERSONAS)]
        for start in range(0, len(share), 4):
            batch = share[start:start + 4]
            out, meta = gateway.generate("generate.questions", {
                "chunks": [{"ref": f"chunk:{c.id}", "section": c.section, "document": d.filename, "text": c.text,
                            **({"already_asked": asked[c.id][:8]} if asked and asked.get(c.id) else {})} for c, d in batch],
                "per_chunk": per_chunk, "categories": answerable_categories, "include_multi_turn": include_multi_turn,
                "variation": variation + tester, "persona": persona,
            })
            synthetic = synthetic or meta.synthetic
            for q in out.questions:
                key = normalize(q.question)
                if q.chunk_ref not in by_ref or not q.question.strip() or key in existing_questions:
                    continue
                # A model may return more than it was asked for, or the same question reworded.
                # Keep at most per_chunk questions per passage (plus one multi-turn) and drop near-duplicates.
                same_chunk = [x for x in questions if x["chunk_ref"] == q.chunk_ref]
                is_multi_turn = q.category == "multi_turn" and bool(q.follow_up)
                plain = [x for x in same_chunk if not x.get("follow_up")]
                if (not is_multi_turn and len(plain) >= per_chunk) or (is_multi_turn and any(x.get("follow_up") for x in same_chunk)):
                    dropped += 1
                    continue
                earlier = [x["question"] for x in same_chunk] + list((asked or {}).get(by_ref[q.chunk_ref][0].id, []))
                if not meta.synthetic and any(similarity(q.question, text) >= 0.75 for text in earlier):
                    dropped += 1
                    continue
                existing_questions.add(key)
                questions.append({**q.model_dump(), "tester": tester + 1, "persona": persona})

    if dropped:
        notes.append(f"{dropped} generated question(s) were discarded as surplus or near-duplicates.")
    items = []
    for index, q in enumerate(questions):
        text = by_ref[q["chunk_ref"]][0].text
        items.append({"ref": f"q{index}", "question": q["question"], "chunk_text": text, "is_follow_up": False})
        if q.get("follow_up"):
            items.append({"ref": f"q{index}:f", "question": q["follow_up"], "chunk_text": text, "is_follow_up": True})
    drafts: dict[str, Any] = {}
    for start in range(0, len(items), 6):
        out, meta = gateway.generate("reference.draft", {"items": items[start:start + 6]})
        synthetic = synthetic or meta.synthetic
        drafts.update({d.ref: d for d in out.drafts})

    cases: list[dict[str, Any]] = []
    unverified_dropped = 0
    for index, q in enumerate(questions):
        chunk_row, _document = by_ref[q["chunk_ref"]]
        draft = drafts.get(f"q{index}")
        if draft is None:
            verification = provider.verify_reference(chunk_row.id, answer="", key_facts=[], quote="", sufficient=False)
            answer, quote = "", ""
        else:
            verification = provider.verify_reference(chunk_row.id, answer=draft.answer, key_facts=draft.key_facts,
                                                     quote=draft.evidence_quote, sufficient=draft.sufficient)
            answer, quote = draft.answer, draft.evidence_quote
        expected: dict[str, Any] = {"behavior": "answer", "answer": answer, "key_facts": verification.key_facts}
        turns = [{"role": "user", "content": q["question"]}]
        category = q["category"]
        if q.get("follow_up"):
            follow = drafts.get(f"q{index}:f")
            if follow is not None and follow.sufficient:
                fv = provider.verify_reference(chunk_row.id, answer=follow.answer, key_facts=follow.key_facts,
                                               quote=follow.evidence_quote, sufficient=True)
                expected["follow_up_answer"] = follow.answer
                expected["follow_up_key_facts"] = fv.key_facts
                turns.append({"role": "user", "content": q["follow_up"]})
                category = "multi_turn"
            else:
                category = "knowledge"
        elif category == "multi_turn":
            category = "knowledge"
        if verification.status == "unverified" and not keep_unverified:
            # A generated question whose reference cannot be tied to verbatim evidence is not a
            # trustworthy test (it is often a vague or meta question). It is left out.
            unverified_dropped += 1
            continue
        cases.append({
            "key": f"q{index}", "category": category, "severity": q["severity"], "language": "en", "turns": turns,
            "expected": expected, "answer_status": verification.status,
            "evidence_refs": [ref for ref in [provider.evidence_ref(chunk_row.id, quote)] if ref],
            "provenance": {"origin": "generated", "verification": verification.detail,
                           "generator": "fake" if synthetic else "model",
                           "tester": q.get("tester", 1), "persona": q.get("persona", "")},
        })

    if unverified_dropped:
        notes.append(f"{unverified_dropped} generated question(s) were left out because no reference answer could be "
                     "linked to verbatim evidence.")
    if n_unanswerable:
        topics = sorted({(c.section or d.filename) for c, d in chunks})[:40]
        out, meta = gateway.generate("generate.unanswerable", {"topics": topics, "n": n_unanswerable})
        synthetic = synthetic or meta.synthetic
        candidates = []
        for item in out.questions[:n_unanswerable]:
            key = normalize(item.question)
            if not item.question.strip() or key in existing_questions:
                continue
            existing_questions.add(key)
            closest_chunk, closest = max(((c, overlap_recall(item.question, c.text)) for c, _ in chunks), key=lambda pair: pair[1])
            candidates.append((item, closest_chunk, closest))
        # A question is only a valid "unanswerable" test if the knowledge base really cannot answer
        # it. Ask the reference role to try, using the closest passage; if it succeeds with verbatim
        # evidence, the question is answerable after all and is discarded.
        answerable: set[int] = set()
        if candidates and not meta.synthetic:
            checks, _ = gateway.generate("reference.draft", {"items": [
                {"ref": f"u{i}", "question": item.question, "chunk_text": chunk.text, "is_follow_up": False}
                for i, (item, chunk, _score) in enumerate(candidates)]})
            for draft in checks.drafts:
                if not draft.ref.startswith("u") or not draft.ref[1:].isdigit() or int(draft.ref[1:]) >= len(candidates):
                    continue
                index = int(draft.ref[1:])
                verdict = provider.verify_reference(candidates[index][1].id, answer=draft.answer, key_facts=draft.key_facts,
                                                    quote=draft.evidence_quote, sufficient=draft.sufficient)
                if verdict.status == "draft":
                    answerable.add(index)
        if answerable:
            notes.append(f"{len(answerable)} proposed out-of-scope question(s) were discarded because the "
                         "knowledge base can answer them.")
        for index, (item, _chunk, closest) in enumerate(candidates):
            if index in answerable:
                continue
            status = "needs_human_review" if closest >= 0.6 else "draft"
            cases.append({
                "key": f"u{index}", "category": "unanswerable", "severity": "high", "language": "en",
                "turns": [{"role": "user", "content": item.question}],
                "expected": {"behavior": "abstain", "answer": "", "key_facts": []}, "answer_status": status,
                "evidence_refs": [],
                "provenance": {"origin": "generated", "reason": item.reason, "generator": "fake" if synthetic else "model",
                               "verification": "a passage partly matches this question; confirm it is really out of scope"
                               if status == "needs_human_review" else
                               "no passage answers this question (checked against the closest passage; not proof of absence)"},
            })

    if languages:
        sources = [c for c in cases if c["category"] == "knowledge"][:max(1, n_cases // 4)]
        if sources:
            out, meta = gateway.generate("generate.variants", {
                "cases": [{"case_ref": c["key"], "question": c["turns"][0]["content"]} for c in sources],
                "languages": languages})
            synthetic = synthetic or meta.synthetic
            by_key = {c["key"]: c for c in sources}
            made = 0
            wrong_script = 0
            for variant in out.variants:
                source = by_key.get(variant.case_ref)
                if source is None or not variant.question.strip():
                    continue
                if normalize(variant.question) == normalize(source["turns"][0]["content"]):
                    continue  # not translated at all
                script = expected_script(variant.language)
                if script and not written_in_script(variant.question, script):
                    wrong_script += 1  # for example "Arabic" written in Latin letters: not a usable test
                    continue
                made += 1
                cases.append({**source, "key": f"{source['key']}:{variant.language}:{made}", "category": "language_variant",
                              "language": variant.language, "variant_of": source["key"],
                              "turns": [{"role": "user", "content": variant.question}],
                              "provenance": {**source["provenance"], "variant_of_question": source["turns"][0]["content"]}})
            if wrong_script:
                notes.append(f"{wrong_script} language variant(s) were discarded because they were not written in the "
                             "script of the requested language. The model may not handle that language well.")
            if made == 0:
                notes.append("Language variants were requested but none were produced. "
                             + ("The fake provider cannot translate; configure a real model." if meta.synthetic
                                else "The model returned no usable variants."))
    return cases, synthetic


def generate_dataset(db: Session, dataset: Dataset, params: dict[str, Any]) -> Dataset:
    """Generate cases and split them. Called from a job; raises DatasetError/GatewayError on failure."""
    gateway = Gateway(db, dataset.workspace_id, run_id=dataset.id, limits=params.get("limits"))
    notes: list[str] = list(dataset.notes or [])
    cases, synthetic = _draft_cases(
        db, gateway, workspace_id=dataset.workspace_id, n_cases=int(params.get("n_cases", 20)),
        categories=params.get("categories") or ANSWERABLE_CATEGORIES,
        include_multi_turn=bool(params.get("include_multi_turn", True)),
        unanswerable_share=float(params.get("unanswerable_share", 0.2)), languages=params.get("languages") or [],
        document_ids=params.get("document_ids"), seed=dataset.seed, variation=0, existing_questions=set(), notes=notes,
        testers=int(params.get("testers") or 1), keep_unverified=bool(params.get("keep_unverified")))
    if len(cases) < 2:
        raise DatasetError("the generator produced fewer than two usable cases")
    _split(cases, dataset.holdout_fraction, dataset.seed)
    _persist(db, dataset, cases)
    dataset.status, dataset.synthetic, dataset.notes = "ready", synthetic, notes
    if synthetic:
        dataset.notes = [*notes, "Generated by the fake provider: questions and references are template-based, "
                                 "for exercising the pipeline only."]
    db.flush()
    return dataset


def _persist(db: Session, dataset: Dataset, cases: list[dict[str, Any]], *, force_split: str | None = None) -> None:
    for case in cases:
        db.add(TestCase(
            workspace_id=dataset.workspace_id, dataset_id=dataset.id, split=force_split or case["split"],
            holdout_generation=dataset.holdout_generation, category=case["category"], severity=case["severity"],
            language=case["language"], turns=case["turns"], expected=case["expected"],
            answer_status=case["answer_status"], evidence_refs=case["evidence_refs"], provenance=case["provenance"],
            weight=2.0 if case["severity"] == "critical" else 1.0,
        ))


def rotate_holdout(db: Session, dataset: Dataset, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
    """Retire the current holdout into the regression set and generate a fresh sealed holdout."""
    current = list(db.scalars(select(TestCase).where(TestCase.dataset_id == dataset.id, TestCase.split == "holdout")))
    existing = {normalize(c.turns[0]["content"]) for c in db.scalars(select(TestCase).where(TestCase.dataset_id == dataset.id))
                if c.turns}
    gateway = Gateway(db, dataset.workspace_id, run_id=dataset.id, limits=params.get("limits"))
    notes = list(dataset.notes or [])
    target = int(params.get("n_cases") or max(4, len(current)))
    # What each passage was already asked, so the fresh holdout does not repeat the regression set.
    asked: dict[str, list[str]] = {}
    for row in db.scalars(select(TestCase).where(TestCase.dataset_id == dataset.id)):
        for ref in row.evidence_refs or []:
            if ref.get("chunk_id") and row.turns:
                asked.setdefault(ref["chunk_id"], []).append(row.turns[0]["content"])
    fresh: list[dict[str, Any]] = []
    synthetic = False
    # A model may repeat what exists or write questions that cannot be verified. Try again on other
    # passages before giving up.
    for attempt in range(3):
        more, used_fake = _draft_cases(
            db, gateway, workspace_id=dataset.workspace_id, n_cases=max(2, target - len(fresh)),
            categories=ANSWERABLE_CATEGORIES, include_multi_turn=False,
            unanswerable_share=0.0 if any(c["category"] == "unanswerable" for c in fresh) else 0.2,
            languages=[], document_ids=None, seed=dataset.seed + dataset.holdout_generation + attempt * 101,
            variation=dataset.holdout_generation + attempt, existing_questions=existing, notes=notes, asked=asked)
        synthetic = synthetic or used_fake
        for item in more:
            item["key"] = f"r{attempt}:{item['key']}"
            for ref in item.get("evidence_refs") or []:
                asked.setdefault(ref["chunk_id"], []).append(item["turns"][0]["content"])
        fresh.extend(more)
        if len(fresh) >= target:
            break
    fresh = fresh[:max(target, 2)]
    if len(fresh) < 2:
        raise DatasetError("the generator produced no new questions for a fresh holdout after three attempts; "
                           "add knowledge documents, use another model, or import additional approved cases")
    for case in current:
        case.split = "regression"
        case.provenance = {**(case.provenance or {}), "retired_from_holdout_generation": case.holdout_generation}
    dataset.holdout_generation += 1
    dataset.holdout_exposed = False
    dataset.version += 1
    dataset.synthetic = dataset.synthetic or synthetic
    dataset.notes = notes
    _persist(db, dataset, fresh, force_split="holdout")
    db.flush()
    audit.record(db, workspace_id=dataset.workspace_id, user_id=user_id, action="dataset.holdout_rotated",
                 subject_type="dataset", subject_id=dataset.id,
                 detail={"retired": len(current), "new": len(fresh), "generation": dataset.holdout_generation})
    return {"retired_to_regression": len(current), "new_holdout_cases": len(fresh), "generation": dataset.holdout_generation}


# --- import -------------------------------------------------------------------------------


def parse_import(filename: str, data: bytes) -> list[dict[str, Any]]:
    text = data.decode("utf-8-sig")
    if filename.lower().endswith((".jsonl", ".ndjson")):
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif filename.lower().endswith(".json"):
        loaded = json.loads(text)
        rows = loaded if isinstance(loaded, list) else loaded.get("cases", [])
    elif filename.lower().endswith(".csv"):
        rows = list(csv.DictReader(io.StringIO(text)))
    else:
        raise DatasetError("import supports .csv, .jsonl and .json files")
    if not rows:
        raise DatasetError("the file contains no rows")
    return rows


def import_cases(db: Session, dataset: Dataset, rows: list[dict[str, Any]], *, approved: bool,
                 holdout_fraction: float, user_id: str | None, source_name: str) -> dict[str, Any]:
    """Import business-owned Q&A or real conversations. Approved rows outrank generated drafts as ground truth."""
    cases, skipped = [], 0
    for index, row in enumerate(rows):
        row = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
        question = str(row.get("question") or row.get("input") or row.get("user") or "").strip()
        if not question:
            skipped += 1
            continue
        facts = row.get("key_facts") or []
        if isinstance(facts, str):
            facts = [f.strip() for f in facts.split(";") if f.strip()]
        behavior = str(row.get("expected_behavior") or row.get("behavior") or "answer").strip().lower()
        behavior = "abstain" if behavior in ("abstain", "decline", "refuse", "unanswerable") else "answer"
        answer = str(row.get("expected_answer") or row.get("answer") or "").strip()
        severity = str(row.get("severity") or "normal").strip().lower()
        turns = [{"role": "user", "content": question}]
        follow_up = str(row.get("follow_up") or "").strip()
        if follow_up:
            turns.append({"role": "user", "content": follow_up})
        has_reference = behavior == "abstain" or bool(answer or facts)
        cases.append({
            "key": f"i{index}", "category": str(row.get("category") or ("unanswerable" if behavior == "abstain" else "imported")),
            "severity": severity if severity in SEVERITIES else "normal", "language": str(row.get("language") or "en"),
            "turns": turns, "expected": {"behavior": behavior, "answer": answer, "key_facts": list(facts)},
            "answer_status": ("approved" if approved else "draft") if has_reference else "unverified",
            "evidence_refs": [], "split": str(row.get("split") or "").strip().lower(),
            "provenance": {"origin": "imported", "file": source_name, "approved_by": user_id if approved else None},
        })
    if not cases:
        raise DatasetError("no row had a 'question' column")
    unassigned = [c for c in cases if c["split"] not in ("regression", "holdout")]
    _split(unassigned, holdout_fraction, dataset.seed + dataset.version)
    _persist(db, dataset, cases)
    dataset.version += 1
    dataset.status = "ready"
    db.flush()
    audit.record(db, workspace_id=dataset.workspace_id, user_id=user_id, action="dataset.imported",
                 subject_type="dataset", subject_id=dataset.id,
                 detail={"rows": len(cases), "skipped": skipped, "approved": approved, "file": source_name})
    return {"imported": len(cases), "skipped": skipped,
            "regression": sum(1 for c in cases if c["split"] == "regression"),
            "holdout": sum(1 for c in cases if c["split"] == "holdout")}


# --- coverage -----------------------------------------------------------------------------


def coverage(db: Session, dataset: Dataset) -> dict[str, Any]:
    """Coverage summary. For the holdout only counts are exposed, never content."""
    cases = list(db.scalars(select(TestCase).where(TestCase.dataset_id == dataset.id, TestCase.split != "retired")))
    regression = [c for c in cases if c.split == "regression"]
    holdout = [c for c in cases if c.split == "holdout"]
    documents = {d.id: d.filename for d in db.scalars(
        select(Document).where(Document.workspace_id == dataset.workspace_id, Document.kind == "kb", Document.status == "parsed"))}
    per_document: Counter[str] = Counter()
    for case in regression:
        for ref in case.evidence_refs or []:
            if ref.get("document_id") in documents:
                per_document[documents[ref["document_id"]]] += 1
    uncovered = sorted(name for name in documents.values() if per_document[name] == 0)
    limits = [
        "Scores on generated tests measure behaviour on these cases only; they are not a measure of real-world accuracy.",
    ]
    if uncovered:
        limits.append(f"{len(uncovered)} knowledge document(s) have no regression case.")
    if not any(c.answer_status == "approved" for c in cases):
        limits.append("No reference answer has been approved by a person; references are evidence-linked drafts.")
    if len(regression) < 30:
        limits.append("Fewer than 30 regression cases: differences between versions will often be statistically inconclusive.")
    return {
        "dataset_id": dataset.id, "version": dataset.version, "synthetic": dataset.synthetic,
        "total": len(cases),
        "regression": {
            "count": len(regression),
            "by_category": dict(Counter(c.category for c in regression)),
            "by_severity": dict(Counter(c.severity for c in regression)),
            "by_language": dict(Counter(c.language for c in regression)),
            "by_answer_status": dict(Counter(c.answer_status for c in regression)),
            "by_document": dict(per_document),
            "by_tester": dict(Counter(str((c.provenance or {}).get("tester", "imported")) for c in regression)),
            "multi_turn": sum(1 for c in regression if len(c.turns or []) > 1),
            "stale": sum(1 for c in regression if c.stale),
        },
        "holdout": {
            "count": len(holdout), "sealed": True, "generation": dataset.holdout_generation,
            "exposed": dataset.holdout_exposed, "by_category": dict(Counter(c.category for c in holdout)),
        },
        "uncovered_documents": uncovered, "limits": limits, "notes": dataset.notes or [],
    }


def split_counts(db: Session, dataset_id: str) -> dict[str, int]:
    rows = db.execute(select(TestCase.split, func.count()).where(TestCase.dataset_id == dataset_id).group_by(TestCase.split)).all()
    return {split: count for split, count in rows}


__all__ = ["DatasetError", "GatewayError", "HoldoutAccess", "cases_for_run", "coverage", "generate_dataset",
           "holdout_cases", "import_cases", "parse_import", "regression_cases", "rotate_holdout", "split_counts"]
