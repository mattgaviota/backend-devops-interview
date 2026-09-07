# Notes

_Built with Claude Code (claude-sonnet-4-6). Chat transcripts are saved alongside this file._

## What I did and why

### 1. Developer experience — Docker-first setup

**Problem:** Getting the app running required `mise`, `uv`, and a locally configured PostgreSQL 16 instance. Someone without mise couldn't even install Python. The existing `docker-compose.yml` only ran Postgres, not the app itself.

**What I changed:**

- **`Dockerfile`** — `python:3.14-slim` base image matching the `mise.toml`/`pyproject.toml` pin. `uv` binary is copied from the official Astral image rather than installed via pip, which keeps the layer slim and fast. Dependencies are installed with `uv sync --frozen --no-dev` using the committed `uv.lock` for a fully reproducible build. The deps layer is ordered before `COPY . .` so it's cached across code-only changes. The default `CMD` runs uvicorn against `core.asgi:application` (4 workers) — Django Ninja is ASGI-native so this is the correct server. `docker-compose.yml` overrides this with `runserver` for dev convenience (auto-reload on save).
- **`docker-compose.yml`** — Added an `app` service that builds the `Dockerfile`, waits for Postgres to pass its healthcheck (`pg_isready`), runs `migrate`, then starts the dev server. The DB healthcheck (`interval: 5s, retries: 10`) replaces the previous `depends_on` without a condition, which used to race.
- **`.dockerignore`** — Excludes `.venv/`, `__pycache__/`, `.git/`, and other build noise so the image stays small and cache invalidation is accurate.
- **`core/settings.py`** — DB connection values now read from environment variables with the original hardcoded values as defaults. This means `uv run python manage.py ...` still works locally without any env setup, while the Docker service passes `DATABASE_HOST=db` to reach the Postgres container.

**Result:** `docker compose up --build` is the only step needed to get a running API. Seeding is a separate explicit step (`docker compose exec app uv run python manage.py seed`) because it takes minutes and isn't always wanted.

---

## What I deliberately didn't do

- **Switch to gunicorn** — The dev server is intentional here; it gives reload-on-save for free. Gunicorn belongs in the production-readiness phase alongside `SECRET_KEY`/`DEBUG` env promotion.
- **Secret management** — `SECRET_KEY`, `DEBUG`, and `ALLOWED_HOSTS` are still the insecure defaults. Fixing those is part of the production-readiness work, not DX.
- **Multi-stage Docker build** — Added complexity for minimal gain at this scale. Worth revisiting if image size becomes a concern.

---

### 2. Performance — N+1 queries and missing pagination on list endpoints

**Problem:** `list_posts`, `search_posts`, and `posts_by_tag` all called `_serialize_post_list` which accessed `post.author` (FK) and `post.tags.all()` (M2M) per row — 2N+1 queries for N posts. With 90k published posts and no pagination, this was catastrophic: the queryset was fully evaluated into memory, then 180k+ extra queries fired.

**What I changed:**

All three list endpoints now:
- `select_related("author")` — one JOIN replaces N author lookups
- `prefetch_related("tags")` — one batch query replaces N tag lookups
- `limit` / `offset` query params (default `limit=20`) — slicing the queryset hits the DB with `LIMIT`/`OFFSET` so only the requested page is loaded; the serialization loop then runs over a small set

Result: a page of 20 posts now costs **2 queries** (posts+author JOIN, tags batch) regardless of total dataset size.

### 3. Performance — DB indexes and full-text search

**Problem:** The list queries filtered `is_published=True` and sorted by `created_at DESC` with no index to support that pattern. Search used `icontains` on `body` — a leading-wildcard `LIKE '%...%'` that no B-tree index can satisfy, causing a full sequential scan of all 100k rows.

**What I changed (`blog/models.py` + migration `0002_add_post_indexes`):**

- **Partial index** `post_published_created_idx` — covers only rows where `is_published = true`, ordered by `created_at DESC`. Smaller than a full composite index (skips the ~10% unpublished rows) and directly matches every public list query's `WHERE` + `ORDER BY`.
- **GIN index** `post_fts_gin` — a functional index on `to_tsvector('english', title || ' ' || body)`. Postgres can use this for `@@` queries, turning search into a bitmap index scan instead of a seq scan.
- **`search_posts` rewritten** to use `SearchVector` + `SearchQuery` (both with `config="english"`) so the ORM generates a `@@` operator that hits the GIN index. The `icontains` path is gone.

**Trade-off noted:** The GIN index is updated on every `INSERT`/`UPDATE` to `Post` — acceptable for a write-light content service, but worth monitoring on heavy write workloads.

### 4. Production readiness — environment-driven settings

**Problem:** `SECRET_KEY`, `DEBUG`, and `ALLOWED_HOSTS` were hardcoded. Shipping the insecure key or running with `DEBUG=True` in production leaks stack traces and disables security middleware.

**What I changed (`core/settings.py`):**

- `SECRET_KEY` — reads from env, falls back to the insecure placeholder so local dev without Docker still works out of the box.
- `DEBUG` — reads `DEBUG` env var, parsed as a boolean (`"true"` → `True`). Defaults to `False` — safe by default.
- `ALLOWED_HOSTS` — reads a comma-separated `ALLOWED_HOSTS` env var (e.g. `"api.example.com,www.example.com"`). Defaults to `"*"`.

`docker-compose.yml` explicitly sets `DEBUG=true`, the dev secret key, and `ALLOWED_HOSTS=*` so the dev environment is self-documenting — a new team member can see exactly what a production deployment needs to override.

### 5. Production readiness — health endpoint, Kubernetes manifests, CI pipeline

**Health endpoint (`core/urls.py`):**

Added `GET /health` as a plain Django view outside the Ninja API. It pings the DB with `connection.ensure_connection()` and returns `{"status": "ok"}` (200) or `{"status": "error", "database": "unavailable"}` (503). Being outside Ninja means it responds even if something in the API layer is broken — it's the right scope for liveness and readiness probes.

**Kubernetes manifests (`k8s/`):**

Assumes GKE + Cloud SQL (Postgres) + nginx-ingress controller.

- `configmap.yaml` — non-sensitive config: DB name, user, host (`127.0.0.1` because the Cloud SQL proxy runs as a sidecar), `ALLOWED_HOSTS`.
- `secret.yaml.example` — template only; real values are never committed. Instructions to create the Secret via `kubectl` or a secrets operator.
- `serviceaccount.yaml` — K8s ServiceAccount annotated for Workload Identity, bound to a GCP Service Account with `roles/cloudsql.client`. No key files anywhere.
- `deployment.yaml` — 2 replicas, Cloud SQL Auth Proxy as a sidecar (authenticates via Workload Identity), liveness and readiness probes on `/health`, resource requests/limits set. Migrations run in an init container before the app starts.
- `service.yaml` — ClusterIP, port 80 → 8000.
- `ingress.yaml` — nginx Ingress, host-based routing.

**GitHub Actions (`.github/workflows/ci.yaml`):**

- Runs on every push and PR against `main`.
- `test` job spins up a Postgres service container and runs `pytest`.
- `build-and-push` job runs only on merges to `main` (skipped on PRs).
- Authentication uses Workload Identity Federation — no service account key file stored in GitHub secrets.
- Image pushed to Artifact Registry tagged with `github.sha` (for traceability and rollback) and `latest`.
- Layer cache shared via GitHub Actions cache (`cache-from/cache-to: type=gha`) to keep build times short.

**What to set in the repo before the pipeline works:**
- Repo variable `GCP_PROJECT_ID`
- Repo secrets `WIF_PROVIDER` and `WIF_SERVICE_ACCOUNT`
- Replace all `PROJECT_ID`, `REGION`, `INSTANCE_NAME`, `REPO` placeholders in the manifests
- Replace `api.example.com` with your actual domain in `configmap.yaml` and `ingress.yaml`

## What I'd do next

- **`create_post` tag loop** — `Tag.objects.get(slug=slug)` in a for-loop is N queries; `Tag.objects.filter(slug__in=payload.tag_slugs)` would be 1.
- **HPA** — add a HorizontalPodAutoscaler targeting ~70% CPU to handle traffic spikes without over-provisioning.
- **TLS** — add `cert-manager` + a `ClusterIssuer` to provision Let's Encrypt certs automatically for the Ingress.

---

## Session log

| Date | Decision |
|------|----------|
| 2026-09-06 | Agreed to focus on DX first, then performance, then production readiness |
| 2026-09-06 | Wrote Dockerfile, updated docker-compose, env-ified DB settings, updated README |
