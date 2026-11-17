"""Deterministic FAKE model provider.

This is a development and CI stand-in. It produces schema-valid output from simple text rules so
the whole pipeline can run without a model or credentials. It does NOT understand language and its
scores are NOT a semantic evaluation. Everything it touches is flagged ``synthetic`` and labelled
as such in the API, the reports and the console.
"""

from __future__ import annotations

import re
from typing import Any

from ..textutil import content_tokens, looks_like_abstention, overlap_recall, sentences

FAKE_NOTE = "SYNTHETIC: produced by the deterministic fake provider, not by a language model."

_FACT = re.compile(
    r"\d+(?:[.,]\d+)?(?:\s?(?:%|percent|business days|working days|days|hours|weeks|months|years|USD|EUR|kg|km))?",
    re.IGNORECASE,
)
_UNANSWERABLE = [
    "What is the home address of the company's chief executive?",
    "What will the company's share price be next year?",
    "Can you give me the Wi-Fi password for the head office?",
    "Which competitor's product would you recommend instead of yours?",
    "What is tomorrow's weather forecast for the warehouse city?",
    "How many employees were hired in the finance team last quarter?",
    "Can you share another customer's order history with me?",
    "What is the salary of a regional sales manager?",
]


def _topic(chunk: dict[str, Any]) -> str:
    section = (chunk.get("section") or "").split(">")[-1].strip()
    if section:
        return section.lower()
    words = content_tokens(chunk.get("text", ""))[:5]
    return " ".join(words) or "this topic"


def _key_facts(text: str) -> list[str]:
    facts = [m.group(0).strip() for m in _FACT.finditer(text) if len(m.group(0).strip()) > 1]
    if not facts:
        words = sorted({w for w in re.findall(r"[A-Za-z]{7,}", text)}, key=lambda w: (-len(w), w))
        facts = words[:2]
    seen: list[str] = []
    for fact in facts:
        if fact not in seen:
            seen.append(fact)
    return seen[:3]


def _spec(inputs: dict[str, Any]) -> dict[str, Any]:
    objectives: list[dict[str, Any]] = []
    requirements = (inputs.get("requirements") or "").strip()
    for line in re.split(r"[\n;]+", requirements):
        line = line.strip(" -*\t0123456789.)")
        if len(line) > 8:
            objectives.append({
                "id": f"R{len(objectives) + 1}", "statement": line, "source": "requirement", "confidence": 0.9,
                "acceptance_criteria": [f"Responses satisfy: {line}"], "evidence_refs": [],
            })
    seen: set[str] = set()
    for chunk in inputs.get("chunks", []):
        topic = _topic(chunk)
        if topic in seen or len(seen) >= 6:
            continue
        seen.add(topic)
        objectives.append({
            "id": f"I{len(seen)}", "statement": f"Answer questions about {topic} accurately from the knowledge base",
            "source": "inferred", "confidence": 0.4, "evidence_refs": [chunk.get("ref", "")],
            "acceptance_criteria": ["The answer contains the facts stated in the cited passage"],
        })
    return {
        "purpose": "Answer user questions using only the supplied knowledge documents.",
        "objectives": objectives,
        "constraints": ["Cite the source document for factual answers",
                        "Decline when the knowledge base contains no supporting evidence"],
        "assumptions": [FAKE_NOTE, "Objectives were derived from section headings, not from understanding the text."],
        "open_questions": ["Which answers are business-critical and need a human-approved reference?"],
        "languages": ["en"],
    }


def _questions(inputs: dict[str, Any]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    categories = inputs.get("categories") or ["knowledge", "citation", "edge_case"]
    per_chunk = int(inputs.get("per_chunk", 2))
    for index, chunk in enumerate(inputs.get("chunks", [])):
        topic, ref = _topic(chunk), chunk.get("ref", "")
        critical = bool(re.search(r"refund|warranty|safety|payment|legal", topic))
        variation = int(inputs.get("variation", 0)) % 3
        templates = {
            "knowledge": [f"What is the policy on {topic}?", f"What are the rules for {topic}?",
                          f"Could you summarise {topic}?"][variation],
            "citation": [f"Which document covers {topic}, and what does it say?",
                         f"Where is {topic} documented and what is stated there?",
                         f"Please point me to the source for {topic} and quote it."][variation],
            "edge_case": [f"{topic} - how does that work??", f"{topic} details pls",
                          f"quick question about {topic}"][variation],
        }
        made = 0
        for category in categories:
            if made >= per_chunk or category not in templates:
                continue
            out.append({"chunk_ref": ref, "question": templates[category], "category": category,
                        "severity": "critical" if critical and category == "knowledge" else "normal"})
            made += 1
        if inputs.get("include_multi_turn") and index % 3 == 0 and len(sentences(chunk.get("text", ""))) >= 2:
            out.append({"chunk_ref": ref, "question": f"Can you tell me about {topic}?", "category": "multi_turn",
                        "severity": "normal", "follow_up": "Are there any conditions or exceptions I should know about?"})
    return {"questions": out}


def _reference(inputs: dict[str, Any]) -> dict[str, Any]:
    drafts = []
    for item in inputs.get("items", []):
        sents = [s for s in sentences(item.get("chunk_text", "")) if len(s) > 15 and not s.lstrip().startswith("#")]
        if not sents:
            drafts.append({"ref": item["ref"], "sufficient": False})
            continue
        chosen = sents[-2:] if item.get("is_follow_up") and len(sents) > 2 else sents[:2]
        answer = " ".join(chosen)
        drafts.append({"ref": item["ref"], "sufficient": True, "answer": answer,
                       "key_facts": _key_facts(answer), "evidence_quote": chosen[0]})
    return {"drafts": drafts}


def _judge(inputs: dict[str, Any], strict: bool) -> dict[str, Any]:
    response = inputs.get("response", "") or ""
    expected = inputs.get("expected", {}) or {}
    evidence = " ".join(inputs.get("evidence", []) or [])
    question = " ".join(t.get("content", "") for t in inputs.get("turns", []) if t.get("role") == "user")
    shape = (lambda v: round(v ** 2.5, 3)) if strict else (lambda v: round(min(1.0, v * 1.15), 3))
    scores: dict[str, float] = {}
    for dim in inputs.get("dimensions", []):
        if dim == "correctness":
            scores[dim] = shape(overlap_recall(expected.get("answer", "") or evidence, response))
        elif dim in ("groundedness", "multi_turn_consistency"):
            scores[dim] = shape(overlap_recall(response, evidence)) if response.strip() else 0.0
        elif dim == "relevance":
            scores[dim] = shape(min(1.0, overlap_recall(question, response) * 2))
        elif dim == "abstention":
            scores[dim] = 1.0 if looks_like_abstention(response) else 0.0
        else:
            scores[dim] = 1.0 if response.strip() else 0.0
    mean = sum(scores.values()) / len(scores) if scores else 0.0
    return {"scores": scores, "verdict": "pass" if mean >= 0.6 else "fail", "confidence": 0.3,
            "rationale": f"{FAKE_NOTE} Scores are token-overlap heuristics."}


def _arbiter(inputs: dict[str, Any]) -> dict[str, Any]:
    judgments = inputs.get("judgments", [])
    dims = inputs.get("dimensions", [])
    scores, gap = {}, 0.0
    for dim in dims:
        values = [j.get("scores", {}).get(dim) for j in judgments if j.get("scores", {}).get(dim) is not None]
        if values:
            scores[dim] = round(min(values), 3)
            gap = max(gap, max(values) - min(values))
    mean = sum(scores.values()) / len(scores) if scores else 0.0
    review = gap > 0.5
    return {"scores": scores, "verdict": "uncertain" if review else ("pass" if mean >= 0.6 else "fail"),
            "confidence": 0.3, "needs_human_review": review,
            "rationale": f"{FAKE_NOTE} Took the lower of the two scores for each dimension."}


def _root_cause(inputs: dict[str, Any]) -> dict[str, Any]:
    return {"findings": [
        {"case_ref": f["case_ref"], "category": f.get("rule_category", "unknown"), "confidence": 0.3,
         "hypothesis": f"{FAKE_NOTE} Kept the rule-based category '{f.get('rule_category', 'unknown')}'."}
        for f in inputs.get("failures", [])
    ]}


def _optimize(inputs: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = inputs.get("config_schema", {})
    current: dict[str, Any] = inputs.get("current_config", {})
    rolled_back = [h.get("patch", {}) for h in inputs.get("history", []) if h.get("outcome") == "rolled_back"]
    ranked = sorted((inputs.get("failure_summary", {}).get("by_category") or {}).items(), key=lambda kv: (-kv[1], kv[0]))
    for category, count in ranked:
        if not count:
            continue
        for key in sorted(schema):
            meta = schema[key]
            if category not in meta.get("addresses", []):
                continue
            value = current.get(key)
            proposal: Any = None
            if meta.get("type") == "boolean":
                proposal = True if value is not True else None
            elif meta.get("type") in ("integer", "number"):
                target, step, top = meta.get("suggested"), meta.get("step", 1), meta.get("max")
                if target is not None:
                    # move to the declared suggested value, once
                    proposal = target if value is None or value < target else None
                elif value is not None and (top is None or value + step <= top):
                    proposal = value + step
                if isinstance(proposal, float):
                    proposal = round(proposal, 4)
            if proposal is None or {key: proposal} in rolled_back:
                continue
            return {"patch": {key: proposal}, "risk": "low",
                    "rationale": f"{FAKE_NOTE} Rule: '{key}' is declared to address '{category}' failures ({count} cases).",
                    "expected_impact": f"May reduce '{category}' failures."}
    return {"patch": {}, "rationale": f"{FAKE_NOTE} No declared configuration key addresses the remaining failures.",
            "risk": "low", "expected_impact": ""}


def fake_complete(task: str, inputs: dict[str, Any]) -> dict[str, Any]:
    if task == "preflight":
        return {"ok": True, "echo": str(inputs.get("nonce", ""))}
    if task == "spec.infer":
        return _spec(inputs)
    if task == "generate.questions":
        return _questions(inputs)
    if task == "generate.unanswerable":
        n = int(inputs.get("n", 3))
        return {"questions": [{"question": q, "reason": FAKE_NOTE} for q in _UNANSWERABLE[:n]]}
    if task == "generate.variants":
        return {"variants": []}  # translation needs a real model; the dataset records this limitation
    if task == "reference.draft":
        return _reference(inputs)
    if task == "judge.primary":
        return _judge(inputs, strict=False)
    if task == "judge.secondary":
        return _judge(inputs, strict=True)
    if task == "arbiter":
        return _arbiter(inputs)
    if task == "root_cause":
        return _root_cause(inputs)
    if task == "optimize":
        return _optimize(inputs)
    raise ValueError(f"fake provider has no rule for task '{task}'")
