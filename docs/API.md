# REST API

Everything the web console does goes through this API, so anything you can click you can also
script. Every instance serves its OpenAPI 3 document at `/api/openapi.json`; import it into your
API client of choice. (No hosted "try it" page is bundled, because those pages load scripts from
a public CDN and Kaizeval makes no external requests.) This page explains the conventions and
lists every route.

A complete, runnable walkthrough that uses only the Python standard library is in
[`examples/api_quickstart.py`](../examples/api_quickstart.py): sign up, register an agent, import
questions, evaluate, and read results, findings, recommendations and the report.

## Conventions

- **Base path:** `/api/v1`. Almost every resource lives under `/api/v1/workspaces/{workspace_id}/…`.
- **Authentication:** `Authorization: Bearer <token>`. Obtain a token from `POST /auth/login`
  (or `POST /auth/signup`). Tokens expire after `KAIZEVAL_TOKEN_TTL_MINUTES` (default 12 hours).
- **Authorization:** each workspace member has one role: `owner` > `editor` > `reviewer` > `viewer`.
  A higher role includes the lower ones. A user who is not a member of a workspace receives `404`
  for everything inside it, so workspace ids are never confirmed to outsiders. An object id from
  another workspace also returns `404`.
- **Errors:** every error has the same shape and a stable `code`:

  ```json
  {"error": {"code": "EGRESS_BLOCKED", "message": "offline mode: host is not local or allowlisted…"}}
  ```

  Validation errors use `code: "VALIDATION_ERROR"` and add `issues: [{"field", "message"}]`.
  Every response carries an `X-Request-ID` header; quote it when reporting a problem.
- **Long-running operations** return a job (HTTP `202`):

  ```json
  {"id": "…", "kind": "evaluation.run", "status": "queued", "progress": {}, "result": null, "error": null}
  ```

  `status` is one of `queued | running | review_required | completed | failed | cancelled`.
  Poll `GET /workspaces/{ws}/jobs/{id}`. `review_required` means the job is paused until a person
  decides (human-guided optimization).
- **Idempotency:** send an `Idempotency-Key` header with `POST /evaluation-runs`,
  `/optimization-runs`, `/datasets:generate`, `/specifications:build` and `/holdout:rotate`.
  Repeating the request with the same key returns the original job instead of starting a new one.
- **Pagination:** list endpoints accept `limit` (max 200) and `offset` and return
  `{"items": […], "total": n, "limit": n, "offset": n}`.
- **Credentials** are write-only. API keys and agent credentials are encrypted at rest and never
  returned; objects expose `has_credential: true|false` instead.
- **Synthetic flag:** objects produced with the fake model provider carry `synthetic: true`.
  Clients must show that such results are not real evaluations.

## A minimal session

```bash
BASE=http://127.0.0.1:8000/api/v1
TOKEN=$(curl -s $BASE/auth/login -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"…"}' | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
WS=<workspace id from GET /auth/me>

# register and verify a local model (Ollama exposes an OpenAI-compatible API under /v1)
curl -s $BASE/workspaces/$WS/model-providers -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"local","kind":"openai_compatible","base_url":"http://localhost:11434/v1","default_model":"<model name>"}'
curl -s -X POST $BASE/workspaces/$WS/model-providers/<id>/verify -H "authorization: Bearer $TOKEN"
curl -s -X PUT $BASE/workspaces/$WS/model-role-policies/default -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"provider_id":"<id>"}'

# start a regression evaluation and poll its job
curl -s $BASE/workspaces/$WS/evaluation-runs -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"agent_id":"<agent>","dataset_id":"<dataset>","mode":"single"}'
```

## Routes

The table below is generated from the application (`python scripts/gen_api_reference.py`).

<!-- routes:start -->

### Authentication and workspaces

| Method | Path (under `/api/v1`) | Minimum role | Purpose |
|---|---|---|---|
| `GET` | `/system` | public | System |
| `POST` | `/auth/signup` | public | Signup |
| `POST` | `/auth/login` | public | Login |
| `GET` | `/auth/me` | signed in | Me |
| `POST` | `/workspaces` | signed in | Create workspace |
| `GET` | `/workspaces/{ws}` | viewer | Get workspace |
| `PATCH` | `/workspaces/{ws}` | owner | Update workspace |
| `GET` | `/workspaces/{ws}/members` | viewer | List members |
| `PUT` | `/workspaces/{ws}/members` | owner | Upsert member |
| `DELETE` | `/workspaces/{ws}/members/{user_id}` | owner | Remove member |

### Model providers and usage

| Method | Path (under `/api/v1`) | Minimum role | Purpose |
|---|---|---|---|
| `POST` | `/workspaces/{ws}/model-providers` | owner | Create provider |
| `GET` | `/workspaces/{ws}/model-providers` | viewer | List providers |
| `POST` | `/workspaces/{ws}/model-providers/{provider_id}/verify` | editor | Preflight: network policy, connectivity and structured-output capability. Required before AI-assisted steps |
| `DELETE` | `/workspaces/{ws}/model-providers/{provider_id}` | owner | Delete provider |
| `GET` | `/workspaces/{ws}/model-role-policies` | viewer | List policies |
| `PUT` | `/workspaces/{ws}/model-role-policies/{role}` | editor | Set policy |
| `DELETE` | `/workspaces/{ws}/model-role-policies/{role}` | editor | Delete policy |
| `GET` | `/workspaces/{ws}/model-usage` | viewer | Usage |
| `GET` | `/workspaces/{ws}/runs/{run_id}/model-usage` | viewer | Platform-model usage for one run. Optimization runs aggregate all of their evaluations |

### Agents

| Method | Path (under `/api/v1`) | Minimum role | Purpose |
|---|---|---|---|
| `GET` | `/workspaces/{ws}/agent-adapters` | viewer | Adapters |
| `POST` | `/workspaces/{ws}/agents` | editor (owner for some options) | Create agent |
| `GET` | `/workspaces/{ws}/agents` | viewer | List agents |
| `GET` | `/workspaces/{ws}/agents/{agent_id}` | viewer | Get agent |
| `PATCH` | `/workspaces/{ws}/agents/{agent_id}` | editor (owner for some options) | Update agent |
| `POST` | `/workspaces/{ws}/agents/{agent_id}/verify` | editor | Probe the connection and record which capabilities are both implemented and granted |
| `GET` | `/workspaces/{ws}/agents/{agent_id}/capabilities` | viewer | Capabilities |
| `GET` | `/workspaces/{ws}/agents/{agent_id}/configuration` | viewer | Agent configuration |
| `DELETE` | `/workspaces/{ws}/agents/{agent_id}` | owner | Delete agent |

### Knowledge and specifications

| Method | Path (under `/api/v1`) | Minimum role | Purpose |
|---|---|---|---|
| `POST` | `/workspaces/{ws}/documents` | editor | Upload document |
| `GET` | `/workspaces/{ws}/documents` | viewer | List documents |
| `GET` | `/workspaces/{ws}/documents/{document_id}/chunks` | viewer | Document chunks |
| `GET` | `/workspaces/{ws}/evidence/{chunk_id}` | viewer | Evidence |
| `POST` | `/workspaces/{ws}/specifications:build` | editor | Build spec |
| `GET` | `/workspaces/{ws}/specifications` | viewer | List specs |
| `GET` | `/workspaces/{ws}/specifications/{spec_id}` | viewer | Get spec |
| `PATCH` | `/workspaces/{ws}/specifications/{spec_id}` | reviewer | Edit spec |
| `POST` | `/workspaces/{ws}/specifications/{spec_id}/review` | reviewer | Review spec |

### Datasets

| Method | Path (under `/api/v1`) | Minimum role | Purpose |
|---|---|---|---|
| `POST` | `/workspaces/{ws}/datasets:generate` | editor | Generate evidence-linked cases and split them into regression and sealed holdout, before any optimization |
| `POST` | `/workspaces/{ws}/datasets:import` | editor | Import business-owned Q&A or real conversations (.csv, .jsonl, .json) |
| `GET` | `/workspaces/{ws}/datasets` | viewer | List datasets |
| `GET` | `/workspaces/{ws}/datasets/{dataset_id}` | viewer | Get dataset |
| `GET` | `/workspaces/{ws}/datasets/{dataset_id}/coverage` | viewer | Dataset coverage |
| `GET` | `/workspaces/{ws}/datasets/{dataset_id}/cases` | viewer (reviewer for some options) | List cases |
| `PATCH` | `/workspaces/{ws}/datasets/{dataset_id}/cases/{case_id}` | reviewer | Human review of a reference: approve it, correct it, or flag it. Approved references outrank drafts |
| `POST` | `/workspaces/{ws}/datasets/{dataset_id}/holdout:rotate` | editor | Rotate |
| `POST` | `/workspaces/{ws}/datasets/{dataset_id}/stale:retire` | editor | Retire regression cases whose evidence came from a document version that has been replaced |

### Evaluation and recommendations

| Method | Path (under `/api/v1`) | Minimum role | Purpose |
|---|---|---|---|
| `POST` | `/workspaces/{ws}/evaluation-runs` | editor (owner for some options) | Start run |
| `GET` | `/workspaces/{ws}/evaluation-runs` | viewer | List runs |
| `GET` | `/workspaces/{ws}/evaluation-runs/{run_id}` | viewer | Get run |
| `GET` | `/workspaces/{ws}/evaluation-runs/{run_id}/results` | viewer (reviewer for some options) | Per-case results with evidence. Holdout case content is restricted to reviewers and audited |
| `GET` | `/workspaces/{ws}/evaluation-runs/{run_id}/findings` | viewer | Run findings |
| `GET` | `/workspaces/{ws}/compare` | viewer | Paired comparison of two completed runs over the same split |
| `POST` | `/workspaces/{ws}/recommendations` | editor | Build recommendations |
| `GET` | `/workspaces/{ws}/recommendations` | viewer | List recommendations |
| `PATCH` | `/workspaces/{ws}/recommendations/{recommendation_id}` | reviewer | Set recommendation state |
| `POST` | `/workspaces/{ws}/runs/{run_id}/cancel` | editor | Cancel a queued or running evaluation or optimization run |

### Optimization, candidates and release

| Method | Path (under `/api/v1`) | Minimum role | Purpose |
|---|---|---|---|
| `POST` | `/workspaces/{ws}/optimization-runs` | editor (owner for some options) | Start a bounded optimization of an isolated candidate. Refused when the connector cannot patch safely |
| `GET` | `/workspaces/{ws}/optimization-runs` | viewer | List optimizations |
| `GET` | `/workspaces/{ws}/optimization-runs/{run_id}` | viewer | Get optimization |
| `POST` | `/workspaces/{ws}/optimization-runs/{run_id}/stop` | editor | Stop optimization |
| `POST` | `/workspaces/{ws}/candidate-patches/{patch_id}/decision` | reviewer | Approve or reject one proposed change to the candidate (human-guided mode). Never touches production |
| `GET` | `/workspaces/{ws}/candidates` | viewer | List candidates |
| `GET` | `/workspaces/{ws}/candidates/{candidate_id}` | viewer | Get candidate |
| `POST` | `/workspaces/{ws}/candidates/{candidate_id}/approve` | owner | Record the explicit release decision for a validated candidate. Approval alone deploys nothing |
| `POST` | `/workspaces/{ws}/candidates/{candidate_id}/promote` | owner | Promote an approved candidate through the connector. Needs the promote capability and a prior approval |
| `POST` | `/workspaces/{ws}/candidates/{candidate_id}/rollback` | owner | Rollback candidate |
| `GET` | `/workspaces/{ws}/experiments/{experiment_id}` | viewer | Get experiment |

### Reports, audit and jobs

| Method | Path (under `/api/v1`) | Minimum role | Purpose |
|---|---|---|---|
| `GET` | `/workspaces/{ws}/reports/{object_id}` | viewer | Report for an evaluation run or an optimization run, as JSON or Markdown |
| `GET` | `/workspaces/{ws}/audit` | reviewer | Audit log |
| `GET` | `/workspaces/{ws}/jobs/{job_id}` | viewer | Get job |

<!-- routes:end -->

## Selected payloads

### Evaluation run configuration (`config` in `POST /evaluation-runs`)

| Key | Default | Meaning |
|---|---|---|
| `pass_threshold` | `0.7` | Overall score a case needs to pass |
| `disagreement_threshold` | `0.3` | Dual mode: largest per-dimension gap that still counts as agreement |
| `require_citations` | `true` | Score `citation_accuracy` for cases that have evidence |
| `repeats` | `1` | Invoke the agent this many times per case and average (for non-deterministic agents) |
| `weights` | see `services/evaluation.py` | Weight of each dimension in the overall score |
| `abstain_patterns` | `[]` | Extra regular expressions that count as a refusal |
| `limits` | `{}` | `max_agent_calls`, `max_model_calls`, `max_model_tokens`, `max_model_cost` |

### Optimization policy (`policy` in `POST /optimization-runs`)

| Key | Default | Meaning |
|---|---|---|
| `max_rounds` | `5` | Hard cap on proposed changes |
| `target_score` | `null` | Stop when the best overall score reaches this value |
| `min_improvement` | `0.005` | Gain over the best version needed to keep a change |
| `stagnation_rounds` | `2` | Stop after this many rounds in a row without a kept change |
| `max_category_regression` | `0.05` | Largest tolerated drop of any category against the baseline |
| `allow_new_critical_failures` | `false` | A new critical failure rolls the change back |
| `category_minimums` | `{}` | Required minimum mean per category |
| `max_latency_increase_pct` | `null` | Roll back when median latency grows more than this |
| `eval_mode` | `single` | `deterministic`, `single` or `dual` |
| `run_holdout` | `true` | Validate the final candidate on the sealed holdout |
| `limits` | `{}` | Same keys as above; applied to the whole optimization run |

### Dataset import file

CSV, JSON Lines or a JSON array. Recognised columns: `question` (required), `expected_answer`,
`key_facts` (list, or `;`-separated text), `expected_behavior` (`answer` or `abstain`), `category`,
`severity` (`low|normal|high|critical`), `language`, `follow_up`, `split` (`regression|holdout`).
