# Agent instructions: petrosa-data-manager

Data integrity, intelligence, and distribution hub for the Petrosa trading ecosystem.

Ecosystem rules (data pillars, commit and PR process, wording, memory) are in the umbrella [AGENTS.md](https://github.com/PetroSa2/petrosa/blob/main/AGENTS.md); this file covers this repository only. Only statements that can be checked against the repository are listed as facts.

## Commands (from the Makefile)

| Command | Purpose |
|---|---|
| `make setup` | Complete environment setup with dependencies and pre-commit |
| `make lint` | Run linting checks with ruff (replaces flake8) |
| `make format` | Format code with ruff (replaces black + isort) |
| `make type-check` | Run type checking with mypy |
| `make test` | Run all tests with coverage (fail if below 40%) |
| `make unit` | Run unit tests only |
| `make integration` | Run integration tests only |
| `make e2e` | Run end-to-end tests only |
| `make security` | Run comprehensive security scans (gitleaks, detect-secrets, bandit, trivy) |
| `make pipeline` | Run complete CI/CD pipeline locally |
| `make pre-commit` | Run pre-commit hooks on all files |

Run the local pipeline or at least lint and tests before opening a pull request.

## Facts

- Python: `requires-python = ">=3.11"`; `.python-version` is `3.11.9`.
- Production MySQL is 5.7; migrations must use MySQL 5.7-compatible SQL. On a large table every ADD or DROP COLUMN rebuilds it, so a migration changes one table with a single in-place, non-blocking ALTER (ALGORITHM=INPLACE, LOCK=NONE) that holds all the changes.
- Lint and format: ruff (config in `ruff.toml`).
- Type checking: mypy (config in `mypy.ini`).
- Tests: pytest, in `tests/`; the coverage floor is 40%.
- Container image: built from `Dockerfile`.
- Instrumentation uses the internal `petrosa-otel` package.
- CI workflows: `.github/workflows/ci-checks.yml`, `.github/workflows/deploy.yml`, `.github/workflows/manual-deploy.yml`.

## Layout

Python packages at the top level: `client/`, `data_manager/`. Also `tests/`, `docs/` and `scripts/` where they exist.

## Rules (policy)

- This service is the only one that talks to the databases (MongoDB for real time, MySQL for history). Other services reach data through its API.
- Commits use Conventional Commits; branches are `{type}/{issue-number}-{slug}`; a PR body contains `Closes #N`; never merge with `--admin`.
- Text that leaves the repository (PR titles and bodies, commit messages, code comments) uses generic roles such as Agentic Developer and never names the upstream workflow engine or its personas.
- Do not commit logs, drafts, scratch files or generated working notes. GitHub and the memory server are the record.

## Legacy rules

`docs/agent-rules.md` holds the repository's previous editor rules, moved unchanged and not yet re-verified. Prefer this file and the code.
