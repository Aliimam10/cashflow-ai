# Local containers and continuous integration

Commit 38 packages the existing FastAPI and Streamlit boundaries into one
reproducible application image and runs them as two local Compose services. It
does not deploy CashFlow AI or make the unauthenticated application safe to
expose on a network.

## Local topology

```text
browser
  |-- http://127.0.0.1:8501 --> Streamlit process --+
  |                                                  | shared loopback namespace
  +-- http://127.0.0.1:8000 --> FastAPI process <----+
                                      |
                                      +-- cashflow_data volume (SQLite)
                                      +-- cashflow_models volume (private artefacts)
                                      +-- local Tesseract executable
```

The two processes share one container network namespace so the frontend can use
the existing loopback-only API client without weakening its host validation.
Both published ports are explicitly bound to the host's `127.0.0.1`; they are
not reachable through a normal external interface. There is no PostgreSQL,
Redis, worker, message broker, or cloud service.

The image runs as an unprivileged `cashflow` user with Linux capabilities
dropped, privilege escalation disabled, and a read-only root filesystem. Only a
bounded temporary filesystem and the two named volumes are writable. Tesseract
is installed inside the image, so scanned-statement OCR remains local to the
Compose environment.

## Build and start

Prerequisites are Docker Desktop (or Docker Engine) and Docker Compose v2.
Validate and build the files before starting the application:

```bash
make docker-config
make docker-build
make docker-up
```

The API service applies the checked-in Alembic migrations before it starts. It
then becomes healthy only when `/ready` confirms both the SQLite connection and
required schema. Streamlit waits for that health check before starting.

Open `http://127.0.0.1:8501` for the interface or
`http://127.0.0.1:8000/docs` for the local API documentation. A machine-readable
check is:

```bash
curl --fail http://127.0.0.1:8000/ready
curl --fail http://127.0.0.1:8000/api/v1/ocr/status
```

Expected results are a readiness response with `"status":"ready"` and an OCR
response reporting that local Tesseract is available. Use only fictional inputs
for screenshots, demonstrations, and repository validation.

Stop both services with:

```bash
make docker-down
```

This keeps the named SQLite and model-artefact volumes for the next start. Do not
add `--volumes` unless you deliberately intend to erase that local data. Never
copy a real database, upload, statement, or model artefact into the repository or
Docker build context.

## Privacy boundary

The Docker build uses explicit source copies and `.dockerignore` excludes dotenv
files, uploads, databases, raw and processed data, generated demonstrations,
logs, and model artefacts before the context reaches the Docker daemon. Compose
does not mount a host data directory or pass `.env` contents into a service.
Runtime data stays in Docker-managed local volumes and remains outside Git.

This is still a single-user local application. It has no authentication, TLS,
remote-secret management, multi-user isolation, backup policy, or production
database. Do not publish either port, forward it through a tunnel, or deploy this
Compose file to a remote host.

## GitHub Actions

`.github/workflows/ci.yml` runs for pull requests, updates to `main`, and manual
dispatches. Its read-only token checks formatting and linting with Ruff, strict
typing with mypy, the complete pytest suite with 100% statement and branch
coverage, package importability, and a full SQLite migration cycle. A dependent
job validates Compose, builds the image, imports the packaged application, and
checks the bundled Tesseract executable.

The workflow uses synthetic test data only. It has no write permission, secrets,
deployment step, release step, image publication, database upload, or retained
financial artefact.

## Manual verification

Without starting a service, run:

```bash
make test-containers
make docker-config
```

The first command checks the delivery files using ordinary pytest and should
report four passing tests. The second should exit successfully and print no
error. On a machine with Docker running, continue with `make docker-build` and
`make docker-up`, check both local URLs above, then run `make docker-down`.

Safe parameters to vary are the two host-side port numbers in `compose.yaml`.
Keep their `127.0.0.1` bindings, the container ports (`8000` and `8501`), and the
application environment values unchanged. The Docker image and GitHub runner
versions are deliberately pinned and should be updated through a reviewed
infrastructure commit.
