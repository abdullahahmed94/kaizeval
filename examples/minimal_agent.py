"""The smallest agent Kaizeval can evaluate (black box, "simple" HTTP style).

Run:  uvicorn examples.minimal_agent:app --port 8200
Then register it in Kaizeval as a "Generic HTTP chat endpoint" with URL http://127.0.0.1:8200/chat

Replace the body of `reply` with a call to your real agent. Returning `citations` is optional but
lets Kaizeval check that answers point at the right source.
"""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class Message(BaseModel):
    message: str
    session_id: str | None = None
    history: list[dict] = []


def reply(question: str) -> tuple[str, list[str]]:
    if "refund" in question.lower():
        return "Unused appliances can be returned within 30 days of delivery.", ["returns-and-refunds.md"]
    return "I don't know. That is not covered in the documents I have.", []


@app.post("/chat")
def chat(body: Message) -> dict:
    answer, citations = reply(body.message)
    return {"answer": answer, "citations": citations}
