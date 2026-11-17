# LLM Gateway

Every model-assisted step in Kaizeval goes through one module, `kaizeval.gateway`. The gateway is
separate from the agent under test: models configured here are the *platform's* models (they
generate tests, judge answers, explain failures and propose changes).

Deterministic checks need no model. Specification inference, test generation, model judging,
model-written root-cause hypotheses and optimization proposals do. When a required model is
missing, the step fails with `MODEL_NOT_CONFIGURED` and says so; nothing switches to another
provider on its own.

## Roles

| Role | Used for | Task(s) |
|---|---|---|
| `specification_inference` | Draft a specification from requirements / documents | `spec.infer` |
| `test_generator` | Questions, unanswerable questions, language variants | `generate.questions`, `generate.unanswerable`, `generate.variants` |
| `reference_draft` | Draft expected answers from a passage | `reference.draft` |
| `evaluator_primary` | First (or only) judge | `judge.primary` |
| `evaluator_secondary` | Independent second judge (dual mode) | `judge.secondary` |
| `arbiter` | Resolves judge disagreement or flags human review | `arbiter` |
| `root_cause` | Failure hypotheses | `root_cause` |
| `optimizer` | Proposes one candidate configuration change | `optimize` |

Assign a `default` policy and, optionally, an override per role
(`PUT /model-role-policies/{role}`): provider, model name, temperature, max tokens, timeout.
Several roles may share a model. Using a different model family for the secondary judge reduces,
but does not remove, correlated errors; agreement between two judges is not ground truth.

## Providers

### OpenAI-compatible endpoint (`kind: "openai_compatible"`)

Any server that implements `POST {base_url}/chat/completions`. This covers local servers such as
Ollama (`http://localhost:11434/v1`) and vLLM (`http://localhost:8000/v1`), and remote providers
with a compatible API.

| Field | Meaning |
|---|---|
| `base_url` | URL up to and including the version segment, for example `http://localhost:11434/v1` |
| `api_key` | Optional. Stored encrypted; never returned |
| `default_model` | Model name sent when a role policy does not set one |
| `allow_egress` | Owner opt-in to send workspace content to a **public** destination. Ignored in offline mode |
| `params.json_mode` | Send `response_format: {"type": "json_object"}` (default `true`; retried without it if the server answers 400) |
| `params.max_retries` | Retries for transient failures and invalid output (default `2`) |
| `params.max_concurrency` | Simultaneous requests to this provider (default `4`) |
| `params.context_window` | Declared context size, reported by preflight |
| `params.price_per_1k_input`, `params.price_per_1k_output` | Optional prices used to **estimate** cost |

### Fake provider (`kind: "fake"`)

A deterministic, in-process stand-in used by the demo and by CI. It produces schema-valid output
from text rules (templates, token overlap). **It does not understand language.** Everything it
produces is flagged `synthetic` in the API, the console and reports, and must never be presented
as a real evaluation or as evidence of improvement.

## Preflight

`POST /model-providers/{id}/verify` runs, in order:

1. **network_policy**: the destination is allowed by the workspace mode and the provider settings;
2. **connectivity**: a real chat request is answered;
3. **structured_output**: the reply is valid JSON for a schema and echoes a random value;
4. **context_window**: reports the declared size, or that none was declared.

The result is stored on the provider. `Gateway.generate` refuses to run a task whose provider has
not passed preflight (`PREFLIGHT_REQUIRED`), so no AI-assisted action starts on an unverified
model.

## What happens on every call

1. Resolve the role to a policy and provider (role override, else `default`).
2. Refuse if the provider has not passed preflight.
3. Check the destination with `netguard` **before any content is sent**.
4. Check the budget for the run or optimization scope.
5. Render the versioned prompt. Untrusted material is passed only inside a delimited JSON block.
6. Call the provider; retry transient failures (`TIMEOUT`, `CONNECTION`, `RATE_LIMITED`,
   `PROVIDER_ERROR`) with exponential backoff. `AUTH`, `BAD_REQUEST` and `EGRESS_BLOCKED` are not
   retried.
7. Parse and validate the reply against the task's Pydantic schema; on invalid output, ask again
   with the validation error, up to the retry limit, then fail with `INVALID_OUTPUT`.
8. Record a `model_invocations` row for every attempt: role, task, provider, model, prompt
   version, status, tokens, latency, estimated cost.

## Usage and cost

`GET /model-usage` (workspace), `GET /runs/{id}/model-usage` (one evaluation, or a whole
optimization run) report calls, tokens and latency by role.

- Token counts come from the provider's `usage` field. When a server does not report them they
  are estimated from text length and marked `tokens_estimated`.
- `estimated_cost` is computed only when prices are configured on the provider, and is always
  labelled an estimate. For a local model with no price it is `null`: Kaizeval reports tokens and
  latency and does not invent a bill.
- The **target agent's** invocation cost is tracked separately (per-call cost declared on the
  agent connection) and never mixed with platform-model usage.

Budgets (`limits` on a run or an optimization policy): `max_model_calls`, `max_model_tokens`,
`max_model_cost`, `max_agent_calls`. When one is reached the run stops with `BUDGET_EXCEEDED`
and keeps the partial results.

## Offline and connected operation

| Instance setting | Workspace mode | Reachable model endpoints |
|---|---|---|
| `KAIZEVAL_OFFLINE=true` (default) | always offline | loopback, private addresses, single-label host names, hosts in `KAIZEVAL_EGRESS_ALLOWLIST` |
| `KAIZEVAL_OFFLINE=false` | `offline` | same as above |
| `KAIZEVAL_OFFLINE=false` | `connected` | the above, plus public hosts for providers with `allow_egress: true` |

In offline mode a dotted host name that is not allowlisted is refused **without a DNS lookup**.
Link-local and metadata addresses (for example `169.254.169.254`) are refused in every mode.
Redirects are never followed and proxy environment variables are ignored.

## Using a local model

1. Run a local OpenAI-compatible server and load a model that follows JSON instructions
   reliably. Small models often fail the structured-output preflight check or produce weak
   judgments; preflight tells you about the first, your own review of sample judgments tells you
   about the second.
2. Register it: kind `openai_compatible`, base URL such as `http://localhost:11434/v1`, the
   model name as `default_model`.
3. Verify it, then set it as the `default` policy.
4. Optionally route `evaluator_secondary` to a different model.

## Adding a provider kind

Add a branch in `gateway/core.py:_complete` that returns a `Completion`, add the kind to
`PROVIDER_KINDS`, map its failures to `GatewayError` codes, route every request through
`guarded_client`, and add tests that run against a local stub server.
