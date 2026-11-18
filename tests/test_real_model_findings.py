"""Regression tests for problems that only showed up when a real language model was used."""

from __future__ import annotations

import pytest

from conftest import MockModel, new_user, run_eval, setup_sample_agent
from kaizeval.textutil import expected_script, is_verifiable_refusal, similarity, written_in_script

ANSWER = "In all other cases the customer pays a flat return fee of 25 USD per appliance."


@pytest.mark.parametrize("response,quote,expected", [
    ("I don't know. That is not covered in the documents I have.", "I don't know.", True),
    ("Unfortunately we have no information about that topic.", "Unfortunately we have no information about that topic.", True),
    ("لا تتوفر لدي معلومات عن ذلك.", "لا تتوفر لدي معلومات عن ذلك.", True),
    (ANSWER, "", False),                                              # no quote at all
    (ANSWER, "I do not know.", False),                                # the quote is not in the answer
    (ANSWER, ANSWER, False),                                          # a real sentence, but not a refusal
    ("Items returned late are not eligible for a refund.", "Items returned late are not eligible for a refund.", False),
])
def test_a_claimed_refusal_must_be_quoted_and_read_like_one(response, quote, expected):
    assert is_verifiable_refusal(response, quote) is expected


def test_script_checks_for_translated_questions():
    assert expected_script("Egyptian Arabic") == "arabic" and expected_script("French") is None
    assert written_in_script("كم يوم لإرجاع الجهاز؟", "arabic")
    assert written_in_script("كم تكلفة الشحن USD؟", "arabic")       # a Latin unit is fine
    assert not written_in_script("كمftime تستغرق؟", "arabic")           # a word that mixes scripts
    assert not written_in_script("كم الوقت 환불؟", "arabic")              # a word in a third script
    assert not written_in_script("kam yom l-irga3?", "arabic")               # transliteration only
    assert similarity("What is the refund window for unused appliances?", "refund window for unused appliances") > 0.75
    assert similarity("What is the refund window?", "How much does express delivery cost?") < 0.3


class HallucinatingJudge(MockModel):
    """A model that always claims the agent declined, like the real one did for an unrelated answer."""

    @staticmethod
    def answer(body):
        reply = MockModel.answer(body)
        if "scores" in reply and "abstention" in reply["scores"]:
            reply.update({"scores": {"abstention": 1.0}, "verdict": "pass",
                          "rationale": "The assistant abstained.", "refusal_quote": HallucinatingJudge.quote})
        return reply

    quote = ""


@pytest.fixture(scope="module")
def judge():
    model = HallucinatingJudge()
    yield model
    model.server.should_exit = True


@pytest.mark.parametrize("mode,quote", [("single", ""), ("single", "I cannot answer that."), ("dual", "")])
def test_reviewer_cannot_pass_a_non_refusal_as_a_refusal(client, sample_agent, judge, mode, quote):
    """The baseline sample agent answers out-of-scope questions with an unrelated sentence. A reviewer
    that says "it abstained" without a verifiable quote from the answer is overruled."""
    HallucinatingJudge.quote = quote
    s = new_user(client)
    provider = s.post("/ws/model-providers", json={"name": "judge", "kind": "openai_compatible", "base_url": judge.url + "/v1"})
    assert s.post(f"/ws/model-providers/{provider['id']}/verify")["ok"]
    s.put("/ws/model-role-policies/default", json={"provider_id": provider["id"]})
    agent_id = setup_sample_agent(s, sample_agent)
    csv = b"question,expected_behavior\nWhat are the terms of the company mobile app?,abstain\n"
    dataset_id = s.post("/ws/datasets:import", files={"file": ("q.csv", csv, "text/csv")})["dataset"]["id"]
    run = run_eval(s, agent_id, dataset_id, mode=mode)
    assert run["status"] == "completed" and run["summary"]["by_dimension"]["abstention"]["mean"] == 0.0
    assert run["summary"]["pass_rate"] == 0.0
    result = s.get(f"/ws/evaluation-runs/{run['id']}/results")["items"][0]
    assert "don't know" not in result["response"]["text"].lower()  # the agent really did not decline
    assert all(j["scores"]["abstention"] == 0.0 and j["overruled"] for j in result["judgments"] if j["task"] != "arbiter")
    assert result["verdict"] == "fail"
    if mode == "dual":
        assert any("same model" in caveat for caveat in run["summary"]["caveats"])


def test_a_source_named_in_the_answer_text_counts_as_a_citation(client):
    """A model-backed agent told to "name the source" writes it in its answer; that must be measurable."""
    from fastapi import FastAPI

    from conftest import serve, setup_fake_model, upload_kb

    answers = {"named": "Unused appliances can be returned within 30 days (source: returns-and-refunds.md).",
               "titled": "According to the Returns and Refunds document, you have 30 days.",
               "wrong": "You have 30 days, see shipping-and-delivery.md.",
               "none": "You have 30 days."}
    app = FastAPI()

    @app.post("/chat")
    def chat(body: dict):
        return {"answer": answers.get(body["message"].split()[0].lower(), "Hello.")}

    url, server = serve(app)
    try:
        s = new_user(client)
        setup_fake_model(s)
        upload_kb(s)
        agent = s.post("/ws/agents", json={"name": "talker", "adapter": "http_chat", "config": {"url": url + "/chat"}})
        s.post(f"/ws/agents/{agent['id']}/verify")
        dataset_id = s.post("/ws/datasets:generate", json={"name": "t", "n_cases": 12, "unanswerable_share": 0.0, "holdout_fraction": 0.0})["dataset"]["id"]
        cases = s.get(f"/ws/datasets/{dataset_id}/cases", params={"limit": 50})["items"]
        refund = next(c for c in cases if c["evidence_refs"][0]["filename"] == "returns-and-refunds.md")
        # reuse one evidence-linked case, asked four times with a different first word
        from kaizeval.db import session_scope
        from kaizeval.models import TestCase

        with session_scope() as db:
            keep = db.get(TestCase, refund["id"])
            for other in db.query(TestCase).filter(TestCase.dataset_id == dataset_id, TestCase.id != refund["id"]):
                db.delete(other)
            keep.split = "regression"
            keep.turns = [{"role": "user", "content": "named question"}]
            for word in ("titled", "wrong", "none"):
                db.add(TestCase(workspace_id=keep.workspace_id, dataset_id=dataset_id, split="regression", category="citation",
                                turns=[{"role": "user", "content": f"{word} question"}], expected=keep.expected,
                                answer_status=keep.answer_status, evidence_refs=keep.evidence_refs, provenance={}))
        run = run_eval(s, agent["id"], dataset_id, mode="deterministic")
        results = s.get(f"/ws/evaluation-runs/{run['id']}/results")["items"]
        scores = {r["case"]["turns"][0]["content"].split()[0]: r["final"]["scores"]["citation_accuracy"]["score"] for r in results}
        assert scores == {"named": 1.0, "titled": 1.0, "wrong": 0.5, "none": 0.0}
    finally:
        server.should_exit = True


def test_a_real_refusal_in_other_words_is_still_accepted(client, judge):
    """An agent that declines politely without a stock phrase passes when the reviewer quotes that sentence."""
    from fastapi import FastAPI

    from conftest import serve

    refusal = "Unfortunately we have no details about that in our help centre."
    app = FastAPI()

    @app.post("/chat")
    def chat(body: dict):
        return {"answer": refusal, "citations": []}

    url, server = serve(app)
    HallucinatingJudge.quote = refusal
    try:
        s = new_user(client)
        provider = s.post("/ws/model-providers", json={"name": "judge", "kind": "openai_compatible", "base_url": judge.url + "/v1"})
        s.post(f"/ws/model-providers/{provider['id']}/verify")
        s.put("/ws/model-role-policies/default", json={"provider_id": provider["id"]})
        agent = s.post("/ws/agents", json={"name": "polite", "adapter": "http_chat", "config": {"url": url + "/chat"}})
        s.post(f"/ws/agents/{agent['id']}/verify")
        csv = b"question,expected_behavior\nWhat are the terms of the company mobile app?,abstain\n"
        dataset_id = s.post("/ws/datasets:import", files={"file": ("q.csv", csv, "text/csv")})["dataset"]["id"]
        run = run_eval(s, agent["id"], dataset_id, mode="single")
        assert run["summary"]["by_dimension"]["abstention"]["mean"] == 1.0 and run["summary"]["pass_rate"] == 1.0
    finally:
        server.should_exit = True


class ObviousTester(MockModel):
    """A tester that, like a real model at low temperature, always asks the most obvious question about
    a passage, unless it is told what was already asked."""

    counter = 0

    @staticmethod
    def answer(body):
        import json
        import re

        from kaizeval.textutil import sentences

        reply = MockModel.answer(body)
        if reply:
            return reply
        inputs = json.loads(re.search(r"<input>\n(.*)\n</input>", body["messages"][1]["content"], re.S).group(1))
        if "chunks" in inputs and "per_chunk" in inputs:
            questions = []
            for chunk in inputs["chunks"]:
                topic = (chunk.get("section") or "this topic").split(">")[-1].strip()
                obvious = f"What does the {topic} section state?"
                done = len(chunk.get("already_asked") or [])
                text = obvious if not done else f"Under which condition, number {done}, does {topic} apply to my order?"
                questions.append({"chunk_ref": chunk["ref"], "question": text, "category": "knowledge", "severity": "normal"})
            return {"questions": questions}
        if "items" in inputs:
            drafts = []
            for item in inputs["items"]:
                usable = [s for s in sentences(item["chunk_text"]) if len(s) > 15 and not s.lstrip().startswith("#")]
                if item["ref"].startswith("u") or not usable:
                    drafts.append({"ref": item["ref"], "sufficient": False})
                else:
                    fact = max(re.findall(r"[A-Za-z0-9]+", usable[0]), key=len)
                    drafts.append({"ref": item["ref"], "sufficient": True, "answer": usable[0], "key_facts": [fact],
                                   "evidence_quote": usable[0]})
            return {"drafts": drafts}
        if "topics" in inputs:
            ObviousTester.counter += 1
            return {"questions": [{"question": f"What is the home address of board member number {ObviousTester.counter}?",
                                   "reason": "personal data"}]}
        return {}


def test_a_fresh_holdout_asks_what_was_not_asked_before(client):
    """Found with a real model: rotating the holdout failed because the tester repeated the questions the
    dataset already had. The tester is now told what each passage was already asked."""
    from conftest import upload_kb

    model = ObviousTester()
    try:
        s = new_user(client)
        provider = s.post("/ws/model-providers", json={"name": "tester", "kind": "openai_compatible", "base_url": model.url + "/v1"})
        assert s.post(f"/ws/model-providers/{provider['id']}/verify")["ok"]
        s.put("/ws/model-role-policies/default", json={"provider_id": provider["id"]})
        upload_kb(s)
        made = s.post("/ws/datasets:generate", json={"name": "t", "n_cases": 40, "unanswerable_share": 0.1, "holdout_fraction": 0.2})
        assert made["job"]["status"] == "completed", made["job"]["error"]
        dataset_id = made["dataset"]["id"]
        before = s.get(f"/ws/datasets/{dataset_id}")
        assert before["counts"]["holdout"] >= 2
        old = {c["turns"][0]["content"] for c in s.get(f"/ws/datasets/{dataset_id}/cases", params={"limit": 200})["items"]}
        assert sum(1 for q in old if q.startswith("What does the")) >= 5  # the obvious question, once per passage
        sent_before = len(model.requests)

        job = s.post(f"/ws/datasets/{dataset_id}/holdout:rotate", json={})
        assert job["status"] == "completed", job
        assert job["result"]["new_holdout_cases"] >= 2
        prompts = "\n".join(r["messages"][1]["content"] for r in model.requests[sent_before:])
        assert '"already_asked"' in prompts and "What does the" in prompts
        after = s.get(f"/ws/datasets/{dataset_id}")
        assert after["holdout_generation"] == 2 and after["counts"]["holdout"] == job["result"]["new_holdout_cases"]
        # the visible set gained the retired holdout cases and nothing else; none of them is a repeat
        visible = [c["turns"][0]["content"] for c in s.get(f"/ws/datasets/{dataset_id}/cases", params={"limit": 200})["items"]]
        assert len(visible) == len(set(visible)) == before["counts"]["regression"] + before["counts"]["holdout"]
    finally:
        model.server.should_exit = True
