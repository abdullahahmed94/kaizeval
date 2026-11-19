"""Connectors for agents hosted on cloud platforms.

* ``aws_bedrock_agent``: an Amazon Bedrock agent, called through ``InvokeAgent``.
* ``aws_agentcore``: an agent hosted on Amazon Bedrock AgentCore Runtime (``InvokeAgentRuntime``).
* ``salesforce_agentforce``: a Salesforce Agentforce agent, called through the Agent API.

All three are black-box: Kaizeval sends the test conversation and reads the answer. They cannot
read or change the configuration of the agent, so they report the ``invoke`` capability only.
Every request goes through the guarded HTTP client, so the network policy of the workspace
applies, and a hosted platform is reachable only from a connected workspace with ``allow_public``.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from ..aws import REGION, EventStreamError, b64, canonical_path, decode_event_stream, parse_credentials, sign
from ..netguard import EgressBlocked, EgressPolicy, guarded_client
from .base import INVOKE, AdapterError, AgentAdapter, AgentReply
from .http_chat import as_citations, dig

ANSWER_KEYS = ("answer", "result", "response", "output", "message", "text", "content", "completion")


def user_turns(messages: list[dict[str, str]]) -> list[str]:
    """The user side of a test conversation. A hosted agent keeps its own history inside a session,
    so the earlier user turns are replayed in a fresh session and the last answer is the result."""
    turns = [m["content"] for m in messages if m.get("role") == "user"]
    if not turns:
        raise AdapterError("BAD_RESPONSE", "the test conversation has no user message")
    return turns


def find_text(data: Any, path: str | None) -> str | None:
    """Answer text of an agent-defined JSON reply: the configured path, else the usual field names."""
    if isinstance(data, str):
        return data
    if path:
        value = dig(data, path)
        return value if isinstance(value, str) else None
    if isinstance(data, dict):
        for key in ANSWER_KEYS:
            value = data.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, (dict, list)):
                nested = find_text(value, None)
                if nested is not None:
                    return nested
    if isinstance(data, list):
        parts = [find_text(item, None) for item in data]
        if parts and all(isinstance(part, str) for part in parts):
            return "".join(parts)  # type: ignore[arg-type]
    return None


class CloudAdapter(AgentAdapter):
    """Shared HTTP handling: one guarded client, and platform errors mapped to adapter error codes."""

    platform = "the platform"

    def __init__(self, config: dict[str, Any], secret: str | None, policy: EgressPolicy):
        self.config = config
        self.secret = secret
        self.policy = policy
        self.timeout = float(config.get("timeout_s", 120))
        self._client: httpx.Client | None = None

    @classmethod
    def config_error(cls, config: dict[str, Any]) -> str | None:
        """A message when required settings are missing, else None."""
        return None

    @classmethod
    def endpoints(cls, config: dict[str, Any]) -> list[str]:
        """Every URL this agent configuration will call (checked against the network policy)."""
        return []

    def capabilities(self) -> set[str]:
        return {INVOKE}

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = guarded_client(self.policy, timeout=self.timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http().request(method, url, **kwargs)
        except EgressBlocked as exc:
            raise AdapterError("EGRESS_BLOCKED", exc.reason) from exc
        except httpx.ConnectTimeout as exc:
            raise AdapterError("CONNECTION_ERROR", f"cannot connect to {httpx.URL(url).host}: check the network, "
                               "proxy or firewall of the machine that runs Kaizeval") from exc
        except httpx.TimeoutException as exc:
            raise AdapterError("TIMEOUT", f"{self.platform} ({httpx.URL(url).host}) did not answer within {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise AdapterError("CONNECTION_ERROR", f"cannot reach {httpx.URL(url).host} ({type(exc).__name__})") from exc
        return response

    def _raise_for(self, response: httpx.Response, what: str) -> None:
        status = response.status_code
        if status < 400:
            return
        detail = response.text[:200].replace("\n", " ")
        if status in (401, 403):
            raise AdapterError("AUTH", f"{self.platform} rejected the credentials or permissions for {what} (HTTP {status}): {detail}")
        if status == 429:
            raise AdapterError("RATE_LIMITED", f"{self.platform} rate limit reached")
        if status == 404:
            raise AdapterError("BAD_RESPONSE", f"{self.platform} did not find the resource for {what} (HTTP 404): {detail}")
        raise AdapterError("BAD_RESPONSE", f"{self.platform} returned HTTP {status} for {what}: {detail}")

    def healthcheck(self) -> dict[str, Any]:
        reply = self.invoke([{"role": "user", "content": self.config.get("healthcheck_message", "Hello")}])
        return {"ok": True, "latency_ms": reply.latency_ms, "returns_citations": bool(reply.citations)}


# --- Amazon Web Services ------------------------------------------------------------------


class _AwsAdapter(CloudAdapter):
    service = "bedrock"
    host_prefix = ""

    def __init__(self, config: dict[str, Any], secret: str | None, policy: EgressPolicy):
        super().__init__(config, secret, policy)
        self.region = self._region(config)
        self.credentials = parse_credentials(secret, access_key_id=str(config.get("access_key_id") or ""))

    @classmethod
    def _region(cls, config: dict[str, Any]) -> str:
        return str(config.get("region") or "")

    @classmethod
    def _base(cls, config: dict[str, Any]) -> str:
        explicit = str(config.get("endpoint_url") or "").rstrip("/")
        return explicit or f"https://{cls.host_prefix}.{cls._region(config)}.amazonaws.com"

    @classmethod
    def endpoints(cls, config: dict[str, Any]) -> list[str]:
        return [cls._base(config)]

    def _signed(self, url: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
        if not self.credentials.usable:
            raise AdapterError("AUTH", "no AWS credentials: store an access key pair as the credential of this agent, "
                               "or set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY for the Kaizeval process")
        signed = sign("POST", url, body, self.credentials, region=self.region, service=self.service, headers=headers)
        return self._send("POST", url, content=body, headers=signed)


def _reference_label(reference: dict[str, Any]) -> str | None:
    location = reference.get("location") or {}
    for value in location.values():
        if isinstance(value, dict):
            label = value.get("uri") or value.get("url") or value.get("id")
            if label:
                return str(label)
    return None


class BedrockAgentAdapter(_AwsAdapter):
    name = "aws_bedrock_agent"
    platform = "Amazon Bedrock"
    host_prefix = "bedrock-agent-runtime"

    @classmethod
    def config_error(cls, config: dict[str, Any]) -> str | None:
        missing = [key for key in ("agent_id", "agent_alias_id") if not config.get(key)]
        if missing:
            return f"an Amazon Bedrock agent needs {' and '.join(missing)}"
        if not config.get("endpoint_url") and not REGION.match(cls._region(config)):
            return "an Amazon Bedrock agent needs 'region', for example 'us-east-1'"
        return None

    def _turn(self, text: str, session_id: str) -> tuple[str, list[str]]:
        path = canonical_path(f"/agents/{self.config['agent_id']}/agentAliases/{self.config['agent_alias_id']}"
                              f"/sessions/{session_id}/text")
        body: dict[str, Any] = {"inputText": text, **(self.config.get("extra_body") or {})}
        response = self._signed(self._base(self.config) + path, json.dumps(body).encode(),
                                {"content-type": "application/json", "accept": "application/vnd.amazon.eventstream"})
        self._raise_for(response, "InvokeAgent")
        try:
            events = decode_event_stream(response.content)
        except EventStreamError as exc:
            raise AdapterError("BAD_RESPONSE", f"Amazon Bedrock returned an unreadable event stream ({exc})") from exc
        parts: list[str] = []
        citations: list[str] = []
        for headers, payload in events:
            if headers.get(":message-type") == "exception":
                kind = str(headers.get(":exception-type") or "error")
                code = "RATE_LIMITED" if "throttling" in kind.lower() else "AUTH" if "accessDenied" in kind else "BAD_RESPONSE"
                raise AdapterError(code, f"Amazon Bedrock agent failed: {kind}: {payload.decode(errors='replace')[:200]}")
            event = headers.get(":event-type")
            if event == "returnControl":
                raise AdapterError("NON_REPLAYABLE_TOOL", "the agent returned control to the caller for an action; "
                                   "Kaizeval cannot carry out customer actions on behalf of the agent")
            if event != "chunk":
                continue
            try:
                chunk = json.loads(payload)
                parts.append(b64(chunk.get("bytes") or "").decode("utf-8", errors="replace"))
            except (ValueError, AttributeError) as exc:
                raise AdapterError("BAD_RESPONSE", "Amazon Bedrock returned an unreadable answer chunk") from exc
            for citation in ((chunk.get("attribution") or {}).get("citations") or []):
                for reference in citation.get("retrievedReferences") or []:
                    label = _reference_label(reference)
                    if label and label not in citations:
                        citations.append(label)
        if not parts:
            raise AdapterError("BAD_RESPONSE", "the Amazon Bedrock agent returned no answer text")
        return "".join(parts), citations

    def invoke(self, messages: list[dict[str, str]], *, version: str = "baseline",
               session_id: str | None = None) -> AgentReply:
        if version != "baseline":
            raise AdapterError("UNSUPPORTED_CAPABILITY", "an Amazon Bedrock agent is tested as it is deployed; "
                               "to compare versions, register each agent alias as its own agent")
        session = f"kaizeval-{uuid.uuid4().hex}"
        text, citations = "", list[str]()
        for turn in user_turns(messages):
            started = time.perf_counter()  # the reported latency is that of the final turn
            text, citations = self._turn(turn, session)
        return AgentReply(text=text, citations=citations, latency_ms=int((time.perf_counter() - started) * 1000),
                          session_id=session, cost=self.config.get("cost_per_call"), version="baseline")


class AgentCoreAdapter(_AwsAdapter):
    name = "aws_agentcore"
    platform = "Amazon Bedrock AgentCore"
    service = "bedrock-agentcore"
    host_prefix = "bedrock-agentcore"

    @classmethod
    def _region(cls, config: dict[str, Any]) -> str:
        if config.get("region"):
            return str(config["region"])
        parts = str(config.get("agent_runtime_arn") or "").split(":")
        return parts[3] if len(parts) > 3 else ""

    @classmethod
    def config_error(cls, config: dict[str, Any]) -> str | None:
        if not config.get("agent_runtime_arn"):
            return "an AgentCore agent needs 'agent_runtime_arn'"
        if not config.get("endpoint_url") and not REGION.match(cls._region(config)):
            return "an AgentCore agent needs 'region' (it could not be read from the runtime ARN)"
        return None

    def _read(self, response: httpx.Response) -> tuple[str, list[str]]:
        path = self.config.get("response_path")
        content_type = response.headers.get("content-type", "")
        data: Any
        if "text/event-stream" in content_type:
            items: list[Any] = []
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        items.append(json.loads(raw))
                    except ValueError:
                        items.append(raw)
            pieces = [find_text(item, path) for item in items]
            text = "".join(piece for piece in pieces if isinstance(piece, str)) or None
            data = next((item for item in reversed(items) if isinstance(item, dict)), {})
        else:
            try:
                data = response.json()
            except ValueError:
                data = response.text
            text = find_text(data, path)
        if not isinstance(text, str) or not text.strip():
            raise AdapterError("BAD_RESPONSE", "no answer text in the AgentCore reply; set 'response_path' to the field "
                               "that holds the answer of your agent")
        return text, as_citations(dig(data, self.config.get("citations_path", "citations")))

    def _turn(self, text: str, session: str) -> tuple[str, list[str]]:
        url = f"{self._base(self.config)}/runtimes/{quote(str(self.config['agent_runtime_arn']), safe='-_.~')}/invocations"
        url += f"?qualifier={quote(str(self.config.get('qualifier') or 'DEFAULT'), safe='-_.~')}"
        body = {self.config.get("message_field", "prompt"): text, **(self.config.get("extra_body") or {})}
        response = self._signed(url, json.dumps(body).encode(), {
            "content-type": "application/json", "accept": "application/json, text/event-stream",
            "x-amzn-bedrock-agentcore-runtime-session-id": session})
        self._raise_for(response, "InvokeAgentRuntime")
        return self._read(response)

    def invoke(self, messages: list[dict[str, str]], *, version: str = "baseline",
               session_id: str | None = None) -> AgentReply:
        if version != "baseline":
            raise AdapterError("UNSUPPORTED_CAPABILITY", "an AgentCore agent is tested as it is deployed; "
                               "to compare versions, register each endpoint (qualifier) as its own agent")
        session = f"kaizeval-{uuid.uuid4().hex}"  # the service asks for at least 33 characters
        text, citations = "", list[str]()
        for turn in user_turns(messages):
            started = time.perf_counter()  # the reported latency is that of the final turn
            text, citations = self._turn(turn, session)
        return AgentReply(text=text, citations=citations, latency_ms=int((time.perf_counter() - started) * 1000),
                          session_id=session, cost=self.config.get("cost_per_call"), version="baseline")


# --- Salesforce ---------------------------------------------------------------------------


def _origin(value: str) -> str:
    value = value.strip().rstrip("/")
    return value if "://" in value else f"https://{value}"


class AgentforceAdapter(CloudAdapter):
    name = "salesforce_agentforce"
    platform = "Salesforce"

    def __init__(self, config: dict[str, Any], secret: str | None, policy: EgressPolicy):
        super().__init__(config, secret, policy)
        self._token: str | None = None

    @classmethod
    def config_error(cls, config: dict[str, Any]) -> str | None:
        missing = [key for key in ("my_domain", "agent_id", "client_id") if not config.get(key)]
        return f"an Agentforce agent needs {', '.join(missing)}" if missing else None

    @classmethod
    def _api(cls, config: dict[str, Any]) -> str:
        return _origin(str(config.get("api_base") or "https://api.salesforce.com")) + "/einstein/ai-agent/v1"

    @classmethod
    def _token_url(cls, config: dict[str, Any]) -> str:
        return str(config.get("token_url") or f"{_origin(str(config['my_domain']))}/services/oauth2/token")

    @classmethod
    def endpoints(cls, config: dict[str, Any]) -> list[str]:
        return [cls._token_url(config), cls._api(config)]

    def _login(self) -> str:
        if not self.secret:
            raise AdapterError("AUTH", "store the consumer secret of the connected app as the credential of this agent")
        response = self._send("POST", self._token_url(self.config), data={
            "grant_type": "client_credentials", "client_id": self.config["client_id"], "client_secret": self.secret})
        self._raise_for(response, "the access token")
        try:
            token = response.json()["access_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterError("AUTH", "Salesforce did not return an access token") from exc
        self._token = str(token)
        return self._token

    def _call(self, method: str, path: str, what: str, *, body: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None) -> httpx.Response:
        for attempt in (0, 1):
            token = self._token or self._login()
            response = self._send(method, self._api(self.config) + path, json=body,
                                  headers={"Authorization": f"Bearer {token}", **(headers or {})})
            if response.status_code == 401 and attempt == 0:
                self._token = None  # the token expired; sign in once more
                continue
            break
        self._raise_for(response, what)
        return response

    def _start(self) -> str:
        response = self._call("POST", f"/agents/{quote(str(self.config['agent_id']), safe='')}/sessions", "starting a session", body={
            "externalSessionKey": str(uuid.uuid4()),
            "instanceConfig": {"endpoint": _origin(str(self.config["my_domain"]))},
            "streamingCapabilities": {"chunkTypes": ["Text"]},
            "bypassUser": bool(self.config.get("bypass_user", True)),
            **(self.config.get("session_body") or {})})
        try:
            return str(response.json()["sessionId"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterError("BAD_RESPONSE", "Salesforce did not return a session id") from exc

    def _end(self, session: str) -> None:
        try:
            self._call("DELETE", f"/sessions/{quote(session, safe='')}", "ending the session",
                       headers={"x-session-end-reason": "UserRequest"})
        except AdapterError:
            pass  # the answer is already in hand; an unclosed session expires on its own

    def _turn(self, session: str, sequence: int, text: str) -> tuple[str, list[str]]:
        response = self._call("POST", f"/sessions/{quote(session, safe='')}/messages", "sending a message", body={
            "message": {"sequenceId": sequence, "type": "Text", "text": text},
            "variables": self.config.get("variables") or []})
        try:
            messages = response.json().get("messages") or []
        except (ValueError, AttributeError) as exc:
            raise AdapterError("BAD_RESPONSE", "Salesforce did not return JSON") from exc
        parts = [m["message"] for m in messages if isinstance(m, dict) and isinstance(m.get("message"), str) and m["message"]]
        if not parts:
            kinds = ", ".join(sorted({str(m.get("type")) for m in messages if isinstance(m, dict)})) or "none"
            raise AdapterError("BAD_RESPONSE", f"the Agentforce agent returned no answer text (message types: {kinds})")
        citations: list[str] = []
        for message in messages:
            for label in as_citations(message.get("citedReferences") if isinstance(message, dict) else None):
                if label not in citations:
                    citations.append(label)
        return "\n".join(parts), citations

    def invoke(self, messages: list[dict[str, str]], *, version: str = "baseline",
               session_id: str | None = None) -> AgentReply:
        if version != "baseline":
            raise AdapterError("UNSUPPORTED_CAPABILITY", "an Agentforce agent is tested as it is activated in the org; "
                               "to compare versions, register each agent as its own agent")
        turns = user_turns(messages)
        session = self._start()
        try:
            text, citations = "", list[str]()
            for number, turn in enumerate(turns, start=1):
                started = time.perf_counter()  # the reported latency is that of the final turn
                text, citations = self._turn(session, number, turn)
            latency = int((time.perf_counter() - started) * 1000)
        finally:
            self._end(session)
        return AgentReply(text=text, citations=citations, latency_ms=latency,
                          session_id=session, cost=self.config.get("cost_per_call"), version="baseline")
