# Examples

| File | What it shows |
|---|---|
| `approved-qa.csv` | Import format for business-approved questions and answers (Datasets → Import) |
| `conversations.jsonl` | Import format for real conversations turned into test cases |
| `quality-gate.yml` | A GitHub Actions workflow that fails a pull request when the agent regresses |
| `api_quickstart.py` | Using Kaizeval purely over HTTP with the standard library: sign up, register an agent, import questions, evaluate, read results and the report |
| `minimal_agent.py` | The smallest agent Kaizeval can evaluate as a black box (about 20 lines) |

The questions in the import files match the bundled sample knowledge base
(`src/kaizeval/sample_agent/kb`), which describes a fictional company.

A complete, patchable reference agent (candidate protocol) lives in
`src/kaizeval/sample_agent/app.py`. Run it with `kaizeval sample-agent --admin-token <token>`.
