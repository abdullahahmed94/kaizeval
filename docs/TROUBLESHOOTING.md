# Troubleshooting

Every API error has a `code`. Find it below. Include the `X-Request-ID` response header when you
report a problem.

## Models

| Code or symptom | Cause | What to do |
|---|---|---|
| `MODEL_NOT_CONFIGURED` | No provider is assigned to the role and there is no default | Models → set a default model, or an override for that role. Deterministic evaluation works without a model |
| `PREFLIGHT_REQUIRED` | The provider was never verified, or its last verification failed | Models → Verify |
| `EGRESS_BLOCKED` when adding or calling a provider | The destination is not allowed in this mode | Offline: use a local address or add the host to `KAIZEVAL_EGRESS_ALLOWLIST`. Connected: tick "allow public endpoint" on the provider. An instance started with `KAIZEVAL_OFFLINE=true` can never reach public hosts |
| `INSTANCE_OFFLINE` | You tried to create a connected workspace on an offline instance | Restart with `KAIZEVAL_OFFLINE=false` if that is really intended |
| Preflight: `connectivity` fails with `CONNECTION` | The model server is not reachable from the Kaizeval process | Check the URL and port. Inside Docker, `localhost` is the container itself: use the service name or the host's address |
| Preflight or the first call fails with `TIMEOUT` | A local model server loads the model on its first request, which can take minutes | Run Verify again once the model is loaded, keep it loaded (`OLLAMA_KEEP_ALIVE` for Ollama), or raise `params.preflight_timeout_s` and the role timeout |
| Preflight: `structured_output` fails | The model did not return valid JSON for the schema | Use a model that follows JSON instructions, or a server that supports JSON mode |
| `INVALID_OUTPUT` during a run | The model kept returning invalid JSON after retries | Same as above; raise `params.max_retries` if failures are occasional |
| `BUDGET_EXCEEDED` | A call, token, cost or agent-call limit was reached | The run keeps its partial results. Raise the limit and start again |
| `AUTH` | The endpoint rejected the API key | Re-create the provider with a valid key (keys cannot be read back) |

## Agents

| Code or symptom | Cause | What to do |
|---|---|---|
| `CONNECTION_ERROR`, `TIMEOUT` on verify | The agent endpoint is unreachable or slow | Check the URL; raise `timeout_s` in the agent configuration |
| `BAD_RESPONSE: no answer text at the configured response path` | The response JSON has a different shape | Set `response_path` (for example `data.reply`) and `citations_path` |
| `UNSUPPORTED_CAPABILITY` | The adapter cannot do this for the agent | Expected for black-box agents: use recommendations. See [CONNECTOR_SDK.md](CONNECTOR_SDK.md) to make an agent patchable |
| `INSUFFICIENT_PERMISSION` | The adapter can, but the access level granted does not allow it | An owner can raise the agent's access level, then verify again |
| "the agent has not been verified" | Runs need a successful verification; any edit clears it | Agents → Verify connection |
| `citation_accuracy` is 0 for every case | The agent returns no citations, or they are at another path | Set `citations_path`, or start runs with `config.require_citations = false` |

## Datasets and evaluation

| Symptom | Cause | What to do |
|---|---|---|
| "no parsed knowledge documents" | Nothing was uploaded, or parsing failed | Knowledge → check the document status and error |
| A PDF has "no extractable text" | It is a scan | Run OCR first; Kaizeval does not include OCR |
| Many cases are `unverified` | The drafted reference could not be found verbatim in the passage | Review those cases; approve a corrected reference or leave them unscored |
| Cases show `n/a` | No verified reference and no model judge for the main dimension | Use single or dual mode, or approve reference answers |
| `HOLDOUT_SEALED` | Holdout items are hidden by design | Use the coverage summary; a reviewer can open them deliberately (audited, forces a rotation) |
| `HOLDOUT_SPENT` | The holdout already informed a decision or was opened | Datasets → Rotate holdout |
| `NOT_COMPARABLE` | The two runs used different datasets, splits or evaluation modes | Compare runs made with the same mode |
| The same version scores differently in two runs | The agent is a model with a non-zero temperature | Set `evaluation.repeats` (project file) or `config.repeats` (API) to 2 or more; ignore differences within the reported variation |
| A/B verdict is `inconclusive` | The difference is within noise for this number of cases | Add cases, or accept that the change has no demonstrated effect |
| Everything is labelled SYNTHETIC | The fake provider is routed to that role | Configure and verify a real model |

## Optimization

| Code or symptom | Cause | What to do |
|---|---|---|
| `POLICY_BLOCKED` | Autonomous mode is disabled for the workspace | An owner enables it in workspace settings, or use human-guided mode |
| Run stays `review_required` | Human-guided mode waits for a decision on the proposed change | Approve or reject the pending change |
| Every change is rolled back | No change passed the gates | Read the gate reasons in each round; relax `min_improvement` or `max_category_regression` only if that is justified |
| `APPROVAL_REQUIRED` on promote | No unused approval exists for this candidate | An owner approves the release first |
| `NO_SAFE_ISOLATION` | The baseline changed while a candidate was patched | The connector or agent is not isolating candidates. Stop and fix the agent's admin API |

## Running the server

| Symptom | What to do |
|---|---|
| The console shows "has not been built" | From a source checkout run `cd web && npm ci && npm run build`. Released wheels include the console |
| Port already in use | `kaizeval serve --port 8010` (and `--agent-port` for `kaizeval demo`) |
| Jobs stay `queued` | With `KAIZEVAL_WORKER_MODE=external` at least one `kaizeval worker` must be running |
| A job is `failed` with "worker stopped while the job was running" | The process was restarted mid-job. Start the run again |
| "secret cannot be decrypted with the current KAIZEVAL_SECRET_KEY" | The key changed. Restore the old key, or re-enter the credentials |
| SQLite "database is locked" | Several processes share one SQLite file. Use one process, or PostgreSQL |
| Signed out unexpectedly | Tokens expire after `KAIZEVAL_TOKEN_TTL_MINUTES` (default 12 hours) |

## Getting more detail

- `KAIZEVAL_LOG_LEVEL=DEBUG` increases log detail. Bodies and credentials are never logged.
- The audit page lists who did what and when.
- `GET /api/v1/workspaces/{ws}/jobs/{id}` shows a job's status, progress and error.
