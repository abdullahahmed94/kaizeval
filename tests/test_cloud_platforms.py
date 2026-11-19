"""Cloud platforms: Amazon Bedrock models, Bedrock and AgentCore agents, Salesforce Agentforce agents.

No test here reaches a real cloud service. A stand-in on loopback speaks each wire format, and
the request signature it receives is checked against the reference implementation in botocore.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import Any

import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from conftest import generate_dataset, new_user, run_eval, serve, setup_fake_model, upload_kb
from kaizeval.aws import (
    AwsCredentials,
    EventStreamError,
    canonical_path,
    decode_event_stream,
    encode_event,
    parse_credentials,
    sign,
)
from kaizeval.connectors.base import AdapterError
from kaizeval.connectors.cloud import AgentCoreAdapter, AgentforceAdapter, BedrockAgentAdapter, find_text
from kaizeval.netguard import EgressPolicy
from kaizeval.project import ProjectError, load_config

AKID, SECRET = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
ARN = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/support_agent-AbCdEf1234"
LOCAL = EgressPolicy(offline=True)


def reference_signature(method: str, url: str, body: bytes, headers: dict[str, str], *, region: str, service: str,
                        token: str | None = None) -> str:
    """The signature botocore computes for the same request and timestamp."""
    request = AWSRequest(method=method, url=url, data=body, headers=headers)
    request.context["timestamp"] = headers["x-amz-date"]
    auth = SigV4Auth(Credentials(AKID, SECRET, token), service, region)
    canonical = auth.canonical_request(request)
    return auth.signature(auth.string_to_sign(request, canonical), request)


class Cloud:
    """Loopback stand-in for the cloud endpoints Kaizeval calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.signature_ok: list[bool] = []
        self.reject_temperature = False
        self.agentcore_sse = False
        self.sf_token_calls = 0
        self.sf_expire_once = False
        self.sf_ended: list[str] = []
        app = FastAPI()

        async def record(request: Request, service: str | None) -> dict[str, Any]:
            body = await request.body()
            raw_path = request.scope["raw_path"].decode()
            call = {"path": raw_path, "headers": dict(request.headers), "body": json.loads(body) if body else None}
            self.calls.append(call)
            if service:
                authorization = request.headers.get("authorization", "")
                names = re.search(r"SignedHeaders=([^,]+)", authorization).group(1).split(";")
                signed = {name: request.headers[name] for name in names if name != "host"}
                query = request.scope["query_string"].decode()
                url = f"http://{request.headers['host']}{raw_path}" + (f"?{query}" if query else "")
                region = re.search(r"Credential=[^/]+/\d+/([^/]+)/", authorization).group(1)
                expected = reference_signature(request.method, url, body, signed, region=region, service=service,
                                               token=request.headers.get("x-amz-security-token"))
                self.signature_ok.append(authorization.endswith(f"Signature={expected}"))
            return call

        @app.post("/model/{model_id:path}/converse")
        async def converse(request: Request):
            call = await record(request, "bedrock")
            body = call["body"]
            if self.reject_temperature and "temperature" in body["inferenceConfig"]:
                return JSONResponse(status_code=400, content={"message": "temperature is not supported for this model"})
            match = re.search(r"<input>\n(.*)\n</input>", body["messages"][0]["content"][0]["text"], re.S)
            inputs = json.loads(match.group(1)) if match else {}
            answer = {"ok": True, "echo": inputs["nonce"]} if "nonce" in inputs else {"note": "hello"}
            return {"output": {"message": {"role": "assistant", "content": [{"text": json.dumps(answer)}]}},
                    "stopReason": "end_turn", "usage": {"inputTokens": 77, "outputTokens": 11, "totalTokens": 88}}

        @app.post("/agents/{agent_id}/agentAliases/{alias}/sessions/{session}/text")
        async def invoke_agent(agent_id: str, alias: str, session: str, request: Request):
            call = await record(request, "bedrock")
            text = call["body"]["inputText"]
            if text == "act":
                stream = encode_event({":message-type": "event", ":event-type": "returnControl"}, b"{}")
            elif text == "throttle":
                stream = encode_event({":message-type": "exception", ":exception-type": "throttlingException"}, b'{"message":"slow down"}')
            else:
                turn = sum(1 for c in self.calls if c["path"].endswith(f"/sessions/{session}/text"))
                first = {"bytes": base64.b64encode(f"Answer {turn} to: ".encode()).decode()}
                second = {"bytes": base64.b64encode(text.encode()).decode(), "attribution": {"citations": [{"retrievedReferences": [
                    {"location": {"type": "S3", "s3Location": {"uri": "s3://kb/refund-policy.md"}}},
                    {"location": {"type": "WEB", "webLocation": {"url": "https://example.test/faq"}}}]}]}}
                stream = b"".join(encode_event({":message-type": "event", ":event-type": "chunk", ":content-type": "application/json"},
                                               json.dumps(part).encode()) for part in (first, second))
            return Response(content=stream, media_type="application/vnd.amazon.eventstream")

        @app.post("/runtimes/{arn:path}/invocations")
        async def invoke_runtime(request: Request):
            call = await record(request, "bedrock-agentcore")
            prompt = call["body"].get("prompt") or call["body"].get("question")
            if self.agentcore_sse:
                lines = "".join(f"data: {json.dumps(piece)}\n\n" for piece in ("Streamed ", f"reply to {prompt}"))
                return Response(content=lines, media_type="text/event-stream")
            return {"result": {"role": "assistant", "content": [{"text": f"Reply to {prompt}"}]}, "citations": ["handbook.md"]}

        @app.post("/services/oauth2/token")
        async def token(request: Request):
            form = await request.form()
            self.sf_token_calls += 1
            if form.get("client_secret") != "consumer-secret" or form.get("grant_type") != "client_credentials":
                return JSONResponse(status_code=401, content={"error": "invalid_client"})
            return {"access_token": f"token-{self.sf_token_calls}", "instance_url": "https://example.my.salesforce.com"}

        def allowed(request: Request) -> bool:
            if self.sf_expire_once:
                self.sf_expire_once = False
                return False
            return request.headers.get("authorization", "").startswith("Bearer token-")

        @app.post("/einstein/ai-agent/v1/agents/{agent_id}/sessions")
        async def sf_start(agent_id: str, request: Request):
            call = await record(request, None)
            if not allowed(request):
                return JSONResponse(status_code=401, content={"error": "expired"})
            return {"sessionId": f"session-{len(self.calls)}", "messages": [{"type": "Inform", "message": "Hi, how can I help?"}],
                    "echo": call["body"]}

        @app.post("/einstein/ai-agent/v1/sessions/{session}/messages")
        async def sf_message(session: str, request: Request):
            call = await record(request, None)
            if not allowed(request):
                return JSONResponse(status_code=401, content={"error": "expired"})
            message = call["body"]["message"]
            if message["text"] == "silent":
                return {"messages": [{"type": "Escalate"}]}
            return {"messages": [{"type": "Inform", "id": "1", "message": f"Turn {message['sequenceId']}: {message['text']}",
                                  "citedReferences": [{"url": "https://help.example.test/returns"}]}]}

        @app.delete("/einstein/ai-agent/v1/sessions/{session}")
        async def sf_end(session: str, request: Request):
            self.sf_ended.append(f"{session}:{request.headers.get('x-session-end-reason')}")
            return Response(status_code=204)

        self.url, self.server = serve(app)


@pytest.fixture(scope="module")
def cloud() -> Cloud:
    stand_in = Cloud()
    yield stand_in
    stand_in.server.should_exit = True


# --- signing and framing ------------------------------------------------------------------


@pytest.mark.parametrize("url, service, token", [
    ("https://bedrock-runtime.us-east-1.amazonaws.com" + canonical_path("/model/us.vendor.model-v1:0/converse"), "bedrock", ""),
    ("https://bedrock-agent-runtime.us-east-1.amazonaws.com/agents/AGENT12345/agentAliases/TSTALIASID/sessions/abc/text", "bedrock", "session-token"),
    ("https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123456789012%3Aruntime%2Fa-1"
     "/invocations?qualifier=DEFAULT&accountId=123456789012", "bedrock-agentcore", ""),
])
def test_signature_matches_the_reference_implementation(url, service, token):
    body = json.dumps({"inputText": "مرحبا"}).encode()
    headers = sign("POST", url, body, AwsCredentials(AKID, SECRET, token), region="us-east-1", service=service,
                   headers={"Content-Type": "application/json"}, now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert headers["x-amz-date"] == "20260102T030405Z" and "host" not in headers
    signed = {k: v for k, v in headers.items() if k != "authorization"}
    expected = reference_signature("POST", url, body, signed, region="us-east-1", service=service, token=token or None)
    assert headers["authorization"].endswith(f"Signature={expected}")
    assert f"Credential={AKID}/20260102/us-east-1/{service}/aws4_request" in headers["authorization"]
    assert ("x-amz-security-token" in headers) is bool(token)


def test_credential_forms():
    assert parse_credentials(json.dumps({"access_key_id": AKID, "secret_access_key": SECRET, "session_token": "t"}), env={}) == \
        AwsCredentials(AKID, SECRET, "t")
    assert parse_credentials(json.dumps({"AccessKeyId": AKID, "SecretAccessKey": SECRET}), env={}) == AwsCredentials(AKID, SECRET)
    assert parse_credentials(f"{AKID}:{SECRET}", env={}) == AwsCredentials(AKID, SECRET)
    assert parse_credentials(f"{AKID}:{SECRET}:tok", env={}).session_token == "tok"
    assert parse_credentials(SECRET, access_key_id=AKID, env={}) == AwsCredentials(AKID, SECRET)
    api_key = parse_credentials("bedrock-api-key-abc123", env={})
    assert api_key.bearer_token == "bedrock-api-key-abc123" and api_key.usable
    assert sign("POST", "https://x.test/a", b"", api_key, region="us-east-1", service="bedrock")["authorization"] == \
        "Bearer bedrock-api-key-abc123"
    assert parse_credentials(None, env={"AWS_ACCESS_KEY_ID": AKID, "AWS_SECRET_ACCESS_KEY": SECRET}) == AwsCredentials(AKID, SECRET)
    assert parse_credentials(None, env={"AWS_BEARER_TOKEN_BEDROCK": "k"}).bearer_token == "k"
    assert not parse_credentials(None, env={}).usable


def test_event_stream_round_trip_and_corruption():
    data = encode_event({":event-type": "chunk"}, b'{"bytes":"aGk="}') + encode_event({":event-type": "trace"}, b"{}")
    assert decode_event_stream(data) == [({":event-type": "chunk"}, b'{"bytes":"aGk="}'), ({":event-type": "trace"}, b"{}")]
    with pytest.raises(EventStreamError):
        decode_event_stream(data[:-3])
    with pytest.raises(EventStreamError):
        decode_event_stream(data[:20] + b"X" + data[21:])


def test_answer_text_is_found_in_common_reply_shapes():
    assert find_text({"result": {"content": [{"text": "a"}, {"text": "b"}]}}, None) == "ab"
    assert find_text({"output": "x"}, None) == "x" and find_text("plain", None) == "plain"
    assert find_text({"data": {"reply": "y"}}, "data.reply") == "y"
    assert find_text({"data": {"reply": "y"}}, None) is None


# --- Amazon Bedrock as the model ----------------------------------------------------------


def test_bedrock_model_provider_end_to_end(client, cloud):
    s = new_user(client)
    assert s.post("/ws/model-providers", expect=422, json={"name": "b", "kind": "aws_bedrock", "default_model": "m"})["error"]["code"] == "REGION_REQUIRED"
    assert s.post("/ws/model-providers", expect=422, json={"name": "b", "kind": "aws_bedrock", "params": {"region": "us-east-1"},
                                                          "base_url": cloud.url})["error"]["code"] == "MODEL_REQUIRED"
    # the regional endpoint is a public address: an offline workspace refuses it before anything is sent
    assert s.post("/ws/model-providers", expect=409, json={"name": "b", "kind": "aws_bedrock", "default_model": "m",
                                                          "params": {"region": "us-east-1"}})["error"]["code"] == "EGRESS_BLOCKED"
    cloud.reject_temperature = True
    provider = s.post("/ws/model-providers", json={
        "name": "bedrock", "kind": "aws_bedrock", "base_url": cloud.url, "default_model": "us.vendor.model-v1:0",
        "api_key": f"{AKID}:{SECRET}", "params": {"region": "eu-central-1"}})
    assert "api_key" not in provider and SECRET not in json.dumps(provider)
    before = len(cloud.calls)
    report = s.post(f"/ws/model-providers/{provider['id']}/verify")
    cloud.reject_temperature = False
    assert report["ok"] is True, report
    calls = cloud.calls[before:]
    assert [("temperature" in c["body"]["inferenceConfig"]) for c in calls] == [True, False]
    assert calls[0]["path"] == "/model/us.vendor.model-v1%3A0/converse"
    assert calls[0]["body"]["system"][0]["text"] and calls[0]["body"]["messages"][0]["role"] == "user"
    assert "/eu-central-1/bedrock/aws4_request" in calls[0]["headers"]["authorization"]
    assert cloud.signature_ok[-2:] == [True, True]
    usage = s.get("/ws/model-usage")
    assert usage["prompt_tokens"] == 77 and usage["completion_tokens"] == 11


# --- agents on AWS ------------------------------------------------------------------------


def test_bedrock_agent_adapter(cloud):
    config = {"agent_id": "AGENT12345", "agent_alias_id": "TSTALIASID", "region": "us-west-2", "endpoint_url": cloud.url}
    adapter = BedrockAgentAdapter(config, f"{AKID}:{SECRET}", LOCAL)
    reply = adapter.invoke([{"role": "user", "content": "first"}, {"role": "assistant", "content": "ignored"},
                            {"role": "user", "content": "What is the refund window?"}])
    assert reply.text == "Answer 2 to: What is the refund window?"
    assert reply.citations == ["s3://kb/refund-policy.md", "https://example.test/faq"]
    assert reply.session_id.startswith("kaizeval-") and cloud.signature_ok[-2:] == [True, True]
    assert cloud.calls[-1]["path"] == f"/agents/AGENT12345/agentAliases/TSTALIASID/sessions/{reply.session_id}/text"
    with pytest.raises(AdapterError) as err:
        adapter.invoke([{"role": "user", "content": "act"}])
    assert err.value.code == "NON_REPLAYABLE_TOOL"
    with pytest.raises(AdapterError) as err:
        adapter.invoke([{"role": "user", "content": "throttle"}])
    assert err.value.code == "RATE_LIMITED"
    with pytest.raises(AdapterError) as err:
        adapter.invoke([{"role": "user", "content": "x"}], version="candidate")
    assert err.value.code == "UNSUPPORTED_CAPABILITY"
    with pytest.raises(AdapterError) as err:
        BedrockAgentAdapter(config, None, LOCAL).invoke([{"role": "user", "content": "x"}])
    assert err.value.code == "AUTH" or parse_credentials(None).usable
    adapter.close()
    assert BedrockAgentAdapter.config_error({"agent_id": "A"}) and BedrockAgentAdapter.config_error({**config, "endpoint_url": ""}) is None
    assert BedrockAgentAdapter.endpoints({"region": "us-west-2"}) == ["https://bedrock-agent-runtime.us-west-2.amazonaws.com"]


def test_agentcore_adapter(cloud):
    config = {"agent_runtime_arn": ARN, "endpoint_url": cloud.url}
    adapter = AgentCoreAdapter(config, json.dumps({"access_key_id": AKID, "secret_access_key": SECRET, "session_token": "tmp"}), LOCAL)
    assert adapter.region == "eu-west-1"
    reply = adapter.invoke([{"role": "user", "content": "Where is my order?"}])
    assert reply.text == "Reply to Where is my order?" and reply.citations == ["handbook.md"]
    call = cloud.calls[-1]
    assert call["path"] == "/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aeu-west-1%3A123456789012%3Aruntime%2Fsupport_agent-AbCdEf1234/invocations"
    assert len(call["headers"]["x-amzn-bedrock-agentcore-runtime-session-id"]) >= 33
    assert call["headers"]["x-amz-security-token"] == "tmp" and cloud.signature_ok[-1] is True
    cloud.agentcore_sse = True
    try:
        streamed = AgentCoreAdapter({**config, "message_field": "question"}, f"{AKID}:{SECRET}", LOCAL).invoke(
            [{"role": "user", "content": "Q"}])
    finally:
        cloud.agentcore_sse = False
    assert streamed.text == "Streamed reply to Q" and cloud.calls[-1]["body"] == {"question": "Q"}
    with pytest.raises(AdapterError) as err:
        AgentCoreAdapter({**config, "response_path": "nothing.here"}, f"{AKID}:{SECRET}", LOCAL).invoke([{"role": "user", "content": "Q"}])
    assert err.value.code == "BAD_RESPONSE" and "response_path" in err.value.message
    assert AgentCoreAdapter.endpoints({"agent_runtime_arn": ARN}) == ["https://bedrock-agentcore.eu-west-1.amazonaws.com"]
    assert AgentCoreAdapter.config_error({}) and AgentCoreAdapter.config_error({"agent_runtime_arn": "not-an-arn"})


# --- Salesforce Agentforce ----------------------------------------------------------------


def _agentforce(cloud: Cloud, **extra: Any) -> dict[str, Any]:
    return {"my_domain": "example.my.salesforce.com", "agent_id": "0XxAgent", "client_id": "consumer-key",
            "api_base": cloud.url, "token_url": cloud.url + "/services/oauth2/token", **extra}


def test_agentforce_adapter(cloud):
    adapter = AgentforceAdapter(_agentforce(cloud), "consumer-secret", LOCAL)
    tokens = cloud.sf_token_calls
    reply = adapter.invoke([{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "x"},
                            {"role": "user", "content": "Can I return shoes?"}])
    assert reply.text == "Turn 2: Can I return shoes?" and reply.citations == ["https://help.example.test/returns"]
    start = next(c for c in cloud.calls if c["path"].endswith("/agents/0XxAgent/sessions") and c["body"])
    assert start["body"]["instanceConfig"] == {"endpoint": "https://example.my.salesforce.com"} and start["body"]["bypassUser"] is True
    assert cloud.sf_ended[-1] == f"{reply.session_id}:UserRequest"
    # the token is reused, and refreshed once when the platform says it expired
    cloud.sf_expire_once = True
    adapter.invoke([{"role": "user", "content": "again"}])
    assert cloud.sf_token_calls == tokens + 2
    ended = len(cloud.sf_ended)
    with pytest.raises(AdapterError) as err:
        adapter.invoke([{"role": "user", "content": "silent"}])
    assert err.value.code == "BAD_RESPONSE" and "Escalate" in err.value.message
    assert len(cloud.sf_ended) == ended + 1  # the session is closed even when the turn fails
    adapter.close()
    with pytest.raises(AdapterError) as err:
        AgentforceAdapter(_agentforce(cloud), "wrong", LOCAL).invoke([{"role": "user", "content": "x"}])
    assert err.value.code == "AUTH"
    assert AgentforceAdapter.endpoints({"my_domain": "acme.my.salesforce.com"}) == [
        "https://acme.my.salesforce.com/services/oauth2/token", "https://api.salesforce.com/einstein/ai-agent/v1"]
    assert "client_id" in AgentforceAdapter.config_error({"my_domain": "a", "agent_id": "b"})


# --- through the API ----------------------------------------------------------------------


def test_cloud_agents_register_verify_and_evaluate(client, cloud):
    s = new_user(client)
    listed = s.get("/ws/agent-adapters")["adapters"]
    assert {"aws_bedrock_agent", "aws_agentcore", "salesforce_agentforce"} <= set(listed)
    assert all(listed[name]["max_access_level"] == "black_box" for name in ("aws_bedrock_agent", "aws_agentcore", "salesforce_agentforce"))
    # incomplete settings, a hosted address from an offline workspace, and more access than the platform offers
    assert s.post("/ws/agents", expect=422, json={"name": "a", "adapter": "salesforce_agentforce",
                                                 "config": {"my_domain": "x.my.salesforce.com"}})["error"]["code"] == "AGENT_CONFIG_INCOMPLETE"
    assert s.post("/ws/agents", expect=409, json={"name": "a", "adapter": "aws_bedrock_agent", "config": {
        "agent_id": "A", "agent_alias_id": "B", "region": "us-east-1"}})["error"]["code"] == "EGRESS_BLOCKED"
    assert s.post("/ws/agents", expect=409, json={"name": "a", "adapter": "aws_agentcore", "access_level": "candidate_write",
                                                 "config": {"agent_runtime_arn": ARN, "endpoint_url": cloud.url}})["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    bodies = {
        "bedrock": {"adapter": "aws_bedrock_agent", "credential": f"{AKID}:{SECRET}", "config": {
            "agent_id": "AGENT12345", "agent_alias_id": "TSTALIASID", "region": "us-east-1", "endpoint_url": cloud.url}},
        "agentcore": {"adapter": "aws_agentcore", "credential": f"{AKID}:{SECRET}", "config": {"agent_runtime_arn": ARN, "endpoint_url": cloud.url}},
        "agentforce": {"adapter": "salesforce_agentforce", "credential": "consumer-secret", "config": _agentforce(cloud)},
    }
    ids = {}
    for name, body in bodies.items():
        agent = s.post("/ws/agents", json={"name": name, **body})
        assert SECRET not in json.dumps(agent) and "consumer-secret" not in json.dumps(agent)
        verified = s.post(f"/ws/agents/{agent['id']}/verify")
        assert verified["ok"] is True and verified["capabilities"] == ["invoke"], verified
        ids[name] = agent["id"]
    setup_fake_model(s)
    upload_kb(s)
    dataset = generate_dataset(s, ids["agentforce"], n_cases=6)
    for name in ("agentforce", "bedrock"):
        run = run_eval(s, ids[name], dataset)
        assert run["status"] == "completed" and run["summary"]["cases"] >= 1, run
    assert s.post("/ws/optimization-runs", expect=409, json={"agent_id": ids["bedrock"], "dataset_id": dataset})["error"]["code"]


# --- project file -------------------------------------------------------------------------


def test_project_file_accepts_a_cloud_agent(tmp_path):
    path = tmp_path / "kaizeval.yaml"

    def write(agent: str, network: str = "connected") -> None:
        path.write_text(f"project: p\nnetwork: {network}\nagent:\n{agent}\nrequirements: answer\n", encoding="utf-8")

    write("  platform: salesforce_agentforce\n  my_domain: acme.my.salesforce.com\n  agent_id: 0Xx\n  client_id: key\n"
          "  credential_env: SF_SECRET\n  allow_public: true")
    assert load_config(path)["agent"]["platform"] == "salesforce_agentforce"
    for agent, network, expected in [
        ("  platform: azure", "connected", "must be one of"),
        ("  platform: aws_bedrock_agent\n  agent_id: A", "connected", "agent_alias_id, region"),
        ("  platform: salesforce_agentforce\n  my_domain: a.my.salesforce.com\n  agent_id: 0Xx", "connected", "client_id (or client_id_env)"),
        ("  platform: aws_agentcore\n  agent_runtime_arn: arn\n  url: http://x", "connected", "unknown setting"),
        ("  platform: aws_agentcore\n  agent_runtime_arn: arn", "offline", "network: connected"),
        ("  style: simple", "offline", "agent.platform"),
    ]:
        write(agent, network)
        with pytest.raises(ProjectError) as err:
            load_config(path)
        assert expected in str(err.value), err.value


def test_knowledge_folder_is_read_and_an_empty_match_is_an_error(tmp_path, client, sample_agent):
    import shutil

    from conftest import KB_DIR
    from kaizeval.project import run_project

    files = sorted(KB_DIR.glob("*.md"))
    (tmp_path / "kb" / "deep").mkdir(parents=True)
    shutil.copy(files[0], tmp_path / "kb" / files[0].name)
    shutil.copy(files[1], tmp_path / "kb" / "deep" / files[1].name)
    (tmp_path / "kb" / "ignored.bin").write_bytes(b"\x00")
    path = tmp_path / "kaizeval.yaml"

    def write(knowledge: str) -> None:
        path.write_text(f"project: folder\nmodel:\n  kind: fake\nagent:\n  url: {sample_agent['url']}/chat\n"
                        f"requirements: answer from the documents\nknowledge: {knowledge}\n"
                        "tests:\n  cases: 4\n  testers: 1\nevaluation:\n  reviewers: 0\n", encoding="utf-8")

    lines: list[str] = []
    write("kb")
    run_project(path, log=lines.append)
    assert any("2 knowledge file(s)" in line for line in lines), lines
    write("missing-folder")
    with pytest.raises(ProjectError) as err:
        run_project(path, log=lines.append)
    assert "matches no document" in str(err.value)
