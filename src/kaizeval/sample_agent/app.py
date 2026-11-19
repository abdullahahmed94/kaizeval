"""Bundled sample agent: a small, deterministic document Q&A service.

It exists so the full Kaizeval loop can be demonstrated against a *real, separately running* agent
without any model or credentials. It retrieves passages by keyword overlap and answers by quoting
sentences. Its baseline configuration is deliberately weak (one passage, one sentence, no
citations, never declines) so that an evaluation finds genuine, measurable defects.

The candidate protocol under ``/admin`` comes from ``kaizeval.agentkit``, exactly as it would in
your own agent: the baseline has no mutating endpoint, candidates are independent copies, and
``/admin/promote`` only moves a production pointer and requires an approval reference.
"""

from __future__ import annotations

import math
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..agentkit import CandidateStore
from ..textutil import content_tokens, sentences, stem

KB_DIR = Path(__file__).parent / "kb"

BASELINE_CONFIG: dict[str, Any] = {
    "top_k": 1,
    "max_sentences": 1,
    "cite_sources": False,
    "abstain_threshold": 0.0,
    "query_expansion": False,
}

# ``addresses`` tells an optimizer which failure categories a setting is meant to influence.
CONFIG_SCHEMA: dict[str, dict[str, Any]] = {
    "top_k": {"type": "integer", "min": 1, "max": 6, "step": 2, "suggested": 3,
              "description": "Number of passages retrieved for each question.",
              "addresses": ["retrieval_grounding"]},
    "max_sentences": {"type": "integer", "min": 1, "max": 6, "step": 2, "suggested": 3,
                      "description": "Maximum number of sentences quoted in an answer.",
                      "addresses": ["output_formatting"]},
    "cite_sources": {"type": "boolean", "description": "Append the source document and section to each answer.",
                     "addresses": ["citation"]},
    "abstain_threshold": {"type": "number", "min": 0.0, "max": 1.0, "step": 0.25, "suggested": 0.5,
                          "description": "Minimum share of the question's key terms that must be found in the "
                                         "best passage; below it the agent says it does not know.",
                          "addresses": ["instruction_prompt"]},
    "query_expansion": {"type": "boolean", "description": "Match word stems instead of exact words.",
                        "addresses": ["retrieval_grounding"]},
}

ABSTAIN_TEXT = "I don't know. That is not covered in the documents I have."


class KnowledgeBase:
    def __init__(self, kb_dir: Path):
        self.chunks: list[dict[str, Any]] = []
        for path in sorted(kb_dir.glob("*")):
            if path.suffix.lower() not in (".md", ".txt"):
                continue
            section = ""
            lines: list[str] = []
            for line in path.read_text(encoding="utf-8").splitlines() + ["## "]:
                if line.startswith("#"):
                    text = " ".join(lines).strip()
                    if text and section:
                        self.chunks.append({"source": path.name, "section": section, "text": text})
                    section, lines = line.lstrip("# ").strip(), []
                elif line.strip():
                    lines.append(line.strip())
        for chunk in self.chunks:
            words = content_tokens(chunk["section"] + " " + chunk["text"])
            chunk["tokens"] = set(words)
            chunk["stems"] = {stem(w) for w in words}
        n = max(1, len(self.chunks))
        self.idf: dict[str, float] = {}
        self.idf_stem: dict[str, float] = {}
        for field, table in (("tokens", self.idf), ("stems", self.idf_stem)):
            vocabulary = set().union(*(c[field] for c in self.chunks)) if self.chunks else set()
            for word in vocabulary:
                df = sum(1 for c in self.chunks if word in c[field])
                table[word] = math.log(1 + n / df)
        self.max_idf = math.log(1 + n)

    def search(self, query: str, config: dict[str, Any]) -> list[tuple[float, dict[str, Any]]]:
        expand = bool(config.get("query_expansion"))
        words = content_tokens(query)
        q = {stem(w) for w in words} if expand else set(words)
        if not q:
            return []
        field, idf = ("stems", self.idf_stem) if expand else ("tokens", self.idf)
        total = sum(idf.get(w, self.max_idf) for w in q)
        scored = []
        for chunk in self.chunks:
            hit = sum(idf.get(w, self.max_idf) for w in q if w in chunk[field])
            scored.append((hit / total if total else 0.0, chunk))
        scored.sort(key=lambda pair: -pair[0])
        return scored[: int(config.get("top_k", 1))]


class AgentState:
    """Conversation memory plus the candidate store (baseline, candidates, production pointer)."""

    def __init__(self, store: CandidateStore):
        self.store = store
        self.sessions: dict[str, list[dict[str, Any]]] = {}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    version: str | None = None


def answer(kb: KnowledgeBase, state: AgentState, message: str, config: dict[str, Any], session_id: str) -> dict[str, Any]:
    history = state.sessions.setdefault(session_id, [])
    results = kb.search(message, config)
    best = results[0][0] if results else 0.0
    previous = history[-1] if history else None
    if previous and previous.get("chunks") and best < 0.34:
        # A short follow-up ("any exceptions?") is answered from the passage used in the previous turn.
        chunks, best = previous["chunks"], 1.0
    else:
        chunks = [c for _, c in results]
    if not chunks or best < float(config.get("abstain_threshold", 0.0)):
        history.append({"message": message, "chunks": []})
        return {"answer": ABSTAIN_TEXT, "citations": []}
    q = set(content_tokens(message))
    candidates = []
    for ci, chunk in enumerate(chunks):
        for si, sentence in enumerate(sentences(chunk["text"])):
            score = len(q & set(content_tokens(sentence)))
            candidates.append((-score, ci, si, sentence, chunk))
    picked = sorted(sorted(candidates)[: int(config.get("max_sentences", 1))], key=lambda item: (item[1], item[2]))
    text = " ".join(item[3] for item in picked)
    used = []
    for item in picked:
        if item[4] not in used:
            used.append(item[4])
    citations = []
    if config.get("cite_sources"):
        citations = [{"source": c["source"], "section": c["section"]} for c in used]
        text += " (Source: " + "; ".join(f"{c['source']} - {c['section']}" for c in used) + ")"
    history.append({"message": message, "chunks": used})
    if len(state.sessions) > 5000:
        state.sessions.pop(next(iter(state.sessions)))
    return {"answer": text, "citations": citations}


def create_sample_agent(*, admin_token: str | None = None, state_file: Path | None = None,
                        kb_dir: Path | None = None, delay_seconds: float = 0.0) -> FastAPI:
    app = FastAPI(title="Kaizeval sample agent", version="0.3.0",
                  description="Deterministic demo agent. Not a language model.")
    kb = KnowledgeBase(kb_dir or KB_DIR)
    store = CandidateStore(
        BASELINE_CONFIG, {key: {k: v for k, v in meta.items() if k != "type"} for key, meta in CONFIG_SCHEMA.items()},
        description="Deterministic extractive Q&A agent over Markdown documents (keyword retrieval).",
        state_file=state_file)
    state = AgentState(store)
    app.state.agent_state = state
    app.include_router(store.router(admin_token))

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "production": store.production, "passages": len(kb.chunks)}

    @app.post("/chat")
    def chat(body: ChatRequest) -> dict[str, Any]:
        if delay_seconds:
            time.sleep(delay_seconds)
        try:
            version, config = store.settings(body.version)
        except KeyError:
            raise HTTPException(404, f"unknown version '{body.version}'") from None
        result = answer(kb, state, body.message, config, body.session_id or uuid.uuid4().hex)
        return {**result, "version": version, "config_digest": store.digest(version)}

    @app.post("/v1/chat/completions")
    def openai_compatible(body: dict[str, Any]) -> dict[str, Any]:
        """OpenAI-style endpoint for the production version, so the generic 'openai' preset can be tried."""
        messages = body.get("messages") or []
        user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        version, config = store.settings(None)
        result = answer(kb, state, str(user), config, uuid.uuid4().hex)
        return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "model": f"sample-agent:{version}",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": result["answer"]}}],
                "citations": result["citations"]}

    return app
