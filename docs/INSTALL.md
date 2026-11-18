# Installation, configuration and upgrade

Kaizeval is self-hosted. Choose one of two ways to run it.

| | Single node | Docker Compose |
|---|---|---|
| Processes | one (API, console and worker thread) | API, worker(s), PostgreSQL |
| Database | SQLite file | PostgreSQL |
| Files | local directory | shared volume, or S3-compatible storage |
| Use for | trying it, a single team, a CI machine | a shared installation |

## 1. Single node

Requirements: Python 3.10 or newer.

```bash
pip install kaizeval                            # or the wheel attached to the GitHub release
kaizeval demo                                   # optional: full journey on the bundled sample agent
kaizeval serve                                  # http://127.0.0.1:8000
```

From a source checkout (needs Node.js 20+ once, to build the console):

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .
cd web && npm ci && npm run build && cd ..
kaizeval serve
```

Data (database, uploaded files and a generated secret key) is stored in `~/.kaizeval` unless
`KAIZEVAL_DATA_DIR` is set. The first account you create becomes the owner of the workspace it
creates. To listen on other interfaces use `kaizeval serve --host 0.0.0.0` behind a TLS proxy.

`kaizeval demo` uses a separate directory (`~/.kaizeval/demo`), starts the sample agent on port
8101, runs the whole journey with the **fake** model provider and prints a login. Its results are
synthetic by design.

## 2. Docker Compose

Requirements: Docker Engine with the Compose plugin.

```bash
cp .env.example .env
# set POSTGRES_PASSWORD and KAIZEVAL_SECRET_KEY (and SAMPLE_AGENT_ADMIN_TOKEN for the demo profile)
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
```

Open `http://127.0.0.1:8000`. Add `--profile demo` to also start the sample agent
(reachable from the API as `http://sample-agent:8101`), or `--profile s3` to store files in MinIO
(set `KAIZEVAL_STORAGE_BACKEND=s3` and the S3 keys in `.env`).

The API container applies database migrations on start; workers wait for it. Scale workers with
`docker compose … up -d --scale worker=3`.

> The CI workflow builds this image and starts this stack (API, worker, PostgreSQL and the sample
> agent) on every push, then checks health, the API and the console. The functional test-suite
> runs on SQLite and, in a second job, with every table in PostgreSQL.

### Network-isolated deployment

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.offline.yml --env-file .env up -d
```

All Kaizeval containers join an internal Docker network with no outside route; an nginx
container is the only one published to the host and it only forwards inbound requests. Because
nothing can be pulled inside such an environment:

1. On a connected machine, build and save the images:
   `docker compose -f deploy/docker-compose.yml build` then
   `docker save kaizeval:0.2.0 postgres:16-alpine nginx:alpine -o kaizeval-images.tar`
   (add `ollama/ollama:latest` if you use the `ollama` profile).
2. Transfer the archive and the `deploy/` directory, then `docker load -i kaizeval-images.tar`.
3. Provision model weights for your local model server out of band (for the `ollama` profile,
   place them in the `ollama` volume).
4. Register the model with its internal address, for example `http://ollama:11434/v1`.

For a pip-based air-gapped install, download the wheels on a connected machine
(`pip download kaizeval-0.2.0-py3-none-any.whl -d wheels/`) and install with
`pip install --no-index --find-links wheels/ kaizeval`.

## 3. Configuration

All settings are environment variables (or lines in a `.env` file in the working directory).
See [`.env.example`](../.env.example) for the full list. The important ones:

| Variable | Default | Meaning |
|---|---|---|
| `KAIZEVAL_OFFLINE` | `true` | Hard instance-wide offline boundary |
| `KAIZEVAL_EGRESS_ALLOWLIST` | empty | Internal host names with dots that are allowed in offline mode |
| `KAIZEVAL_DATA_DIR` | `~/.kaizeval` | SQLite file, uploaded files, generated secret key |
| `KAIZEVAL_DATABASE_URL` | SQLite in the data dir | For PostgreSQL: `postgresql+psycopg://user:pass@host:5432/db` |
| `KAIZEVAL_SECRET_KEY` | generated | Signs tokens and encrypts stored credentials |
| `KAIZEVAL_ALLOW_SIGNUP` | `true` | Open self-registration (the very first account is always allowed) |
| `KAIZEVAL_WORKER_MODE` | `inline` | `inline`, or `external` with `kaizeval worker` processes |
| `KAIZEVAL_STORAGE_BACKEND` | `filesystem` | or `s3` |
| `KAIZEVAL_MAX_UPLOAD_MB` | `25` | Upload size limit |

## 4. First steps in the console

1. Create an account and a workspace (offline or connected).
2. **Models**: register your model endpoint, run **Verify**, set it as the default.
3. **Agents**: register your agent and run **Verify connection**.
4. **Knowledge**: upload the documents the agent is supposed to answer from.
5. **Specification**: build one; review and approve it, or skip.
6. **Datasets**: generate tests and/or import approved Q&A.
7. **Evaluations**: run a baseline. Read the findings and recommendations.
8. **Optimization** (only for agents that support candidates): run it and read the A/B and holdout
   results. When the loop stops, the run page asks for your decision: **Continue improving** from
   the candidate, or open the candidate and approve or reject its release.

## 5. Upgrade

1. Back up the database and the object storage (or the whole data directory).
2. Install the new version (`pip install -U …` or rebuild the images).
3. Start it. Migrations are applied automatically (`kaizeval migrate` applies them without
   starting the server).
4. Read `CHANGELOG.md` for anything that needs manual action.

Downgrades are not supported; restore the backup instead.

## 6. Backup and restore

- Single node: stop the server and copy the data directory.
- Compose: `pg_dump` the `kaizeval` database and archive the `data` volume (and the MinIO bucket
  if used). Keep `KAIZEVAL_SECRET_KEY` with the backup; without it stored credentials cannot be
  decrypted.
