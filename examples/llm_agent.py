"""A real, model-backed agent that Kaizeval can test AND improve automatically.

It is a small retrieval agent: it finds the most relevant passages in a folder of Markdown files
and asks a language model to answer from them. Its tunable settings (system prompt, number of
passages, temperature, whether sources are returned) live in a `CandidateStore`, which gives
Kaizeval a safe way to try changes on an isolated copy.

Run it against any OpenAI-compatible endpoint (a local Ollama server by default):

    export LLM_MODEL=your-model-name
    export AGENT_ADMIN_TOKEN=choose-a-secret
    uvicorn examples.llm_agent:app --port 8200

Then test and improve it:

    kaizeval run -c examples/llm_agent.kaizeval.yaml

Environment variables: LLM_BASE_URL (default http://localhost:11434/v1), LLM_MODEL, LLM_API_KEY,
KB_DIR (default: the bundled sample documents), AGENT_ADMIN_TOKEN, AGENT_STATE_FILE.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from kaizeval.agentkit import CandidateStore
from kaizeval.sample_agent.app import KB_DIR, KnowledgeBase

# The baseline is deliberately plain: it does not ask for sources or for a refusal when the
# documents are silent. An evaluation should find that, and the optimizer should fix it.
BASELINE = {
    "system_prompt": "You are a helpful assistant for Larkspur Home Appliances. Answer the customer's question.",
    "top_k": 1,
    "temperature": 0.7,
    "return_sources": False,
}
HINTS = {
    "system_prompt": {"max_length": 2000, "addresses": ["instruction_prompt", "output_formatting", "citation"],
                      "description": "Instructions given to the model before the documents and the question."},
    "top_k": {"min": 1, "max": 6, "suggested": 3, "addresses": ["retrieval_grounding"],
              "description": "Number of passages passed to the model."},
    "temperature": {"min": 0.0, "max": 1.0, "suggested": 0.0, "addresses": ["output_formatting"],
                    "description": "Sampling temperature; lower is more literal."},
    "return_sources": {"addresses": ["citation"], "description": "Return the documents that were used as citations."},
}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    version: str | None = None


def create_app() -> FastAPI:
    base_url = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "")
    api_key = os.environ.get("LLM_API_KEY")
    kb = KnowledgeBase(Path(os.environ.get("KB_DIR") or KB_DIR))
    store = CandidateStore(BASELINE, HINTS, description="Retrieval agent that answers from Markdown documents with a language model.",
                           state_file=os.environ.get("AGENT_STATE_FILE"))
    sessions: dict[str, list[dict[str, str]]] = {}
    client = httpx.Client(timeout=120, trust_env=False,
                          headers={"Authorization": f"Bearer {api_key}"} if api_key else {})

    app = FastAPI(title="Example model-backed agent")
    app.include_router(store.router(os.environ.get("AGENT_ADMIN_TOKEN")))

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "production": store.production, "model": model, "passages": len(kb.chunks)}

    @app.post("/chat")
    def chat(body: ChatRequest) -> dict[str, Any]:
        try:
            version, settings = store.settings(body.version)
        except KeyError:
            raise HTTPException(404, f"unknown version '{body.version}'") from None
        session_id = body.session_id or uuid.uuid4().hex
        history = sessions.setdefault(session_id, [])
        # retrieve with the previous user turn too, so short follow-ups still find their passage
        query = " ".join([m["content"] for m in history if m["role"] == "user"][-1:] + [body.message])
        passages = [chunk for _, chunk in kb.search(query, {"top_k": settings["top_k"]})]
        documents = "\n\n".join(f"[{i}] {p['source']} - {p['section']}\n{p['text']}" for i, p in enumerate(passages, start=1))
        messages = [{"role": "system", "content": settings["system_prompt"]}, *history[-4:],
                    {"role": "user", "content": f"Documents:\n{documents or '(none)'}\n\nQuestion: {body.message}"}]
        try:
            response = client.post(f"{base_url}/chat/completions", json={
                "model": model, "messages": messages, "temperature": settings["temperature"], "max_tokens": 400})
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"].strip()
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise HTTPException(502, f"model call failed: {type(exc).__name__}") from exc
        history += [{"role": "user", "content": body.message}, {"role": "assistant", "content": answer}]
        if len(sessions) > 2000:
            sessions.pop(next(iter(sessions)))
        citations = [{"source": p["source"], "section": p["section"]} for p in passages] if settings["return_sources"] else []
        return {"answer": answer, "citations": citations, "version": version, "config_digest": store.digest(version)}

    return app


app = create_app()
