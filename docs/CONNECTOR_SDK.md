# Connector SDK

A connector lets Kaizeval talk to an agent. The contract is `kaizeval.connectors.base.AgentAdapter`.

## Principle: declare, never pretend

An adapter returns the set of operations it really implements. Kaizeval intersects that with what
the customer granted and only then offers features. An operation that is not implemented raises
`UNSUPPORTED_CAPABILITY`. Nothing is simulated, and the console shows the reason next to every
unavailable capability.

| Capability | Meaning | Needed for |
|---|---|---|
| `invoke` | Send a conversation, get an answer | Every evaluation |
| `inspect` | Read configuration and its patchable schema | Diagnostics detail, optimization |
| `traces` | Read execution traces | (reserved; not used by the bundled adapters) |
| `create_candidate` | Create an isolated copy of the configuration | Optimization |
| `apply_patch` | Change allowlisted settings **of a candidate** | Optimization |
| `restore_candidate` | Put a candidate back to a previous configuration | Rollback |
| `promote` | Make an approved candidate the production version | Release |

Customer grants (set when the agent is registered):

| Access level | Grants |
|---|---|
| `black_box` | `invoke` |
| `read_only` | `invoke`, `inspect`, `traces` |
| `candidate_write` | the above plus `create_candidate`, `apply_patch`, `restore_candidate` |
| `allow_promote` (separate flag, needs `candidate_write`) | `promote` |

Granting `candidate_write` or `allow_promote` requires the workspace **owner** role. Granting
write access is not the same as approving a change: each patch (human-guided mode) and every
promotion still needs its own decision.

## The contract

```python
class AgentAdapter(ABC):
    name = "my_adapter"

    def capabilities(self) -> set[str]: ...
    def healthcheck(self) -> dict: ...
    def invoke(self, messages: list[dict], *, version: str = "baseline",
               session_id: str | None = None) -> AgentReply: ...

    # optional: the base class raises UNSUPPORTED_CAPABILITY
    def inspect(self, version: str = "baseline") -> dict: ...        # {"config", "digest", "schema", "description"}
    def get_traces(self, session_id: str) -> dict: ...
    def create_candidate(self) -> str: ...                           # returns the candidate id
    def apply_patch(self, candidate_id: str, patch: dict) -> dict: ...   # {"before", "after", "digest"}
    def restore_candidate(self, candidate_id: str, config: dict) -> dict: ...
    def promote(self, candidate_id: str, approval_id: str) -> dict: ...
    def close(self) -> None: ...
```

`AgentReply` carries `text`, `citations` (list of strings), `latency_ms`, optional `cost`, `usage`,
`session_id`, `version`.

Errors are `AdapterError(code, message)` with one of: `UNSUPPORTED_CAPABILITY`,
`INSUFFICIENT_PERMISSION`, `NO_SAFE_ISOLATION`, `RATE_LIMITED`, `NON_REPLAYABLE_TOOL`, `TIMEOUT`,
`CONNECTION_ERROR`, `BAD_RESPONSE`, `EGRESS_BLOCKED`, `AUTH`, `INVALID_PATCH`.

Rules for adapter authors:

- Build HTTP clients with `netguard.guarded_client(policy, …)` so the workspace network policy
  applies to every request. The policy is passed to the adapter's constructor.
- `invoke` with `version="baseline"` must reach the unmodified agent. Other values are candidate
  ids returned by `create_candidate`.
- `apply_patch` must never be able to change the baseline. If the target system cannot give you
  an isolated copy, do not implement `create_candidate`; Kaizeval will then offer recommendations
  only.
- The `schema` returned by `inspect` is the allowlist. Kaizeval validates every patch against it
  (type and range) before calling `apply_patch`. Supported types: `boolean`, `integer`, `number`,
  `string` (with `max_length`). Optional hints: `min`, `max`, `step`, `suggested`, `description`
  and `addresses` (a list of failure categories the setting influences: `instruction_prompt`,
  `retrieval_grounding`, `citation`, `output_formatting`, …).
- `inspect()["digest"]` must change whenever the configuration changes. Kaizeval re-reads the
  baseline digest after each patch and aborts with `NO_SAFE_ISOLATION` if it moved.
- Evaluation must not cause side effects in real systems. If the agent calls tools that write,
  pay or send, point Kaizeval at a test deployment with mocked or read-only tools.

Register an adapter:

```python
from kaizeval.connectors import register_adapter

register_adapter("my_adapter", MyAdapter, {
    "label": "My platform",
    "description": "What it can and cannot do.",
    "max_access_level": "read_only",      # the highest grant this adapter can honour
})
```

## Bundled adapters

### `http_chat`: generic HTTP chat endpoint (black box)

Capabilities: `invoke` only.

| Config key | Default | Meaning |
|---|---|---|
| `url` | required | Endpoint that receives a POST |
| `style` | `simple` | `simple` or `openai` |
| `message_field`, `session_field` | `message`, `session_id` | Body field names (`simple` style) |
| `model` | `default` | Model name (`openai` style) |
| `response_path` | `answer` / `choices.0.message.content` | Dotted path to the answer text |
| `citations_path` | `citations` | Dotted path to citations (strings or objects with `source`/`title`/`url`, optional `section`) |
| `auth` | `{"type": "bearer"}` | `bearer`, or `{"type": "header", "header_name": "X-API-Key"}` |
| `headers`, `extra_body` | `{}` | Extra non-secret headers / body fields |
| `timeout_s` | `60` | Request timeout |
| `cost_per_call` | none | Your estimate of one call's cost, used for agent-cost reporting |
| `allow_public` | `false` | Allow a public destination (connected workspaces only) |

`simple` request body: `{"message": "<last user message>", "session_id": "…", "history": […]}`.
`openai` request body: `{"model": "…", "messages": […]}`. Most agent frameworks can expose one of
these, so no code is needed to evaluate them.

### `local_config`: local candidate protocol

Capabilities: `invoke`, `inspect`, `create_candidate`, `apply_patch`, `restore_candidate`, `promote`.
Config: `base_url`, optional `timeout_s`, `cost_per_call`; the credential is the admin bearer token.

An agent supports this adapter by exposing the following endpoints. The bundled sample agent
(`kaizeval/sample_agent/app.py`) is a complete reference implementation of about 300 lines.

| Endpoint | Purpose |
|---|---|
| `GET /health` | `{"ok": true, "production": "<version>"}` |
| `POST /chat` | Body `{"message", "session_id", "version"}` → `{"answer", "citations", "version", "config_digest"}` |
| `GET /admin/schema` | `{"schema": {setting: {type, min, max, …}}, "description": "…"}` |
| `GET /admin/config?version=` | `{"version", "config", "digest"}` |
| `POST /admin/candidates` | Create a copy of the baseline → `{"candidate_id", "config"}` |
| `PATCH /admin/candidates/{id}` | Body `{"patch": {...}}` → `{"before", "after", "digest"}`; `422` for an invalid patch |
| `PUT /admin/candidates/{id}/config` | Body `{"config": {...}}` → restore |
| `POST /admin/promote` | Body `{"candidate_id", "approval_id"}` → move the production pointer |

`/admin/*` requires `Authorization: Bearer <admin token>`. There is deliberately **no endpoint
that modifies the baseline**. Promotion only moves a pointer, so rolling back means pointing at
`baseline` again.

## Vendor-specific connectors

No vendor-specific connector ships in this release. Before adding one, confirm from the vendor's
documentation which operations are really available. Many platforms expose conversation APIs but
no safe way to create and patch an isolated version; such a connector must advertise `invoke`
(and possibly `inspect`) only.
