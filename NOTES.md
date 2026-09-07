# Notes

_Built with Claude Code (claude-sonnet-4-6). Full chat transcript available on request._

---

## What I did and why

### 1. Developer experience — Docker-first setup

**Problem:** Getting the app running required `mise`, `uv`, and a locally installed PostgreSQL 16. Someone without `mise` couldn't even install Python. The existing `docker-compose.yml` only ran Postgres — the app itself wasn't containerized.

**Changes:**

- **`Dockerfile`** — `python:3.14-slim` base (matches `mise.toml`/`pyproject.toml` pin). `uv` binary copied from the official Astral image, keeping the layer slim. Dependencies installed via `uv sync --frozen --no-dev` against the committed `uv.lock` — fully reproducible. The deps `COPY` is ordered before `COPY . .` so it's cached across code-only changes. Default `CMD` runs gunicorn with `uvicorn.workers.UvicornWorker` — Django Ninja is ASGI-native, so this is the right server stack for production.
- **`docker-compose.yml`** — Added `app` service with a `pg_isready` healthcheck on `db` (replacing a bare `depends_on` that used to race), runs `migrate` then `runserver` on startup. Overrides the Dockerfile `CMD` with `runserver` for live-reload in dev. Mounts `.:/app` as a volume so code changes are picked up without rebuilding.
- **`.dockerignore`** — Excludes `.venv/`, `__pycache__/`, `.git/` so the image is lean and cache invalidation is accurate.
- **`core/settings.py`** — All DB connection values (`DATABASE_HOST`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_PORT`) read from env with the original values as defaults. Local dev without Docker still works with zero env setup.

**Result:** `docker compose up --build` is the only prerequisite. Seeding is a separate explicit step (`docker compose exec app uv run python manage.py seed`) because it takes minutes and isn't always wanted.

---

### 2. Performance — N+1 queries and pagination

**Problem:** `list_posts`, `search_posts`, and `posts_by_tag` all evaluated the full queryset into memory (no pagination), then fired two extra queries per post — one for `post.author` (FK) and one for `post.tags.all()` (M2M). With 90k published posts that's 180k+ extra queries per request.

**Changes:**

All three list endpoints now use:
- `select_related("author")` — one JOIN replaces N author lookups
- `prefetch_related("tags")` — one batch query replaces N tag lookups
- `limit` / `offset` query params (default `limit=20`) — slices the queryset to `LIMIT`/`OFFSET` before evaluation

Result: a page of 20 posts costs **2 queries** regardless of dataset size.

`get_post` had the same N+1 on comment authors — fixed with `select_related("author")` on the comments queryset.

---

### 3. Performance — DB indexes and full-text search

**Problem:** List queries filtered `is_published=True` and sorted by `created_at DESC` with no index to support that pattern. Search used `icontains` on `body` — a leading-wildcard `LIKE '%...%'` that no B-tree index can satisfy, causing a full sequential scan of 100k rows on every search request.

**Changes (`blog/models.py` + migrations `0002`, `0003`):**

- **Partial index** `post_published_created_idx` on `created_at DESC WHERE is_published = true` — smaller than a full composite index (skips the ~10% unpublished rows) and directly matches the `WHERE` + `ORDER BY` of every public list query.
- **GIN index** `post_fts_gin` — functional index on `to_tsvector('english', title || ' ' || body)`. Postgres resolves `@@` queries via a bitmap index scan instead of a seq scan.
- **`search_posts` rewritten** to use `SearchVector` + `SearchQuery` with `config="english"` — generates the `@@` operator that hits the GIN index.
- **B-tree index on `User.email`** (`0003`) — `find_user_by_email` does an exact-match lookup; without an index it scans the whole users table.

**Trade-off:** The GIN index is maintained on every `INSERT`/`UPDATE` to `Post`. Acceptable for a write-light content service; worth monitoring on heavy write workloads.

---

### 4. Performance — view count race condition

**Problem:** `get_post` did a Python-level read-modify-write: read `view_count`, add 1, save. Two concurrent requests both read `100`, both write `101` — one increment lost per collision.

**Fix:** `Post.objects.filter(id=post_id).update(view_count=F("view_count") + 1)` pushes the increment into a single `UPDATE ... SET view_count = view_count + 1` SQL statement — atomic at the database level. `refresh_from_db(fields=["view_count"])` reads back the committed value for the response.

---

### 5. Production readiness — environment-driven settings

**Problem:** `SECRET_KEY`, `DEBUG=True`, and `ALLOWED_HOSTS=["*"]` were hardcoded. Shipping the insecure key or running debug mode in production leaks stack traces and disables Django's security middleware.

**Changes (`core/settings.py`):**

- `SECRET_KEY` — reads from env; falls back to the insecure placeholder so local dev still works without any env setup.
- `DEBUG` — reads `DEBUG` env var parsed as boolean (`"true"` → `True`). Defaults to `False` — safe by default.
- `ALLOWED_HOSTS` — reads a comma-separated string from env. Defaults to `"*"`.

`docker-compose.yml` sets `DEBUG=true`, the insecure dev key, and `ALLOWED_HOSTS=*` explicitly — a new team member can read exactly what a production deployment needs to override.

---

### 6. Production readiness — health endpoint

**Change (`core/urls.py`):** Added `GET /health` as a plain Django view outside the Ninja API. It calls `connection.ensure_connection()` and returns `{"status": "ok"}` (200) or `{"status": "error", "database": "unavailable"}` (503). Being outside Ninja means it responds even if something in the API layer is broken — the right scope for K8s liveness and readiness probes.

---

### 7. Production readiness — Kubernetes manifests and CI/CD pipeline

**Target stack:** GKE + Cloud SQL (Postgres) + Artifact Registry + nginx-ingress.

**`k8s/` manifests:**

- `configmap.yaml` — non-sensitive config; `DATABASE_HOST=127.0.0.1` because the Cloud SQL Auth Proxy runs as a sidecar and exposes Postgres on the pod's loopback interface.
- `secret.yaml.example` — template only; real values are never committed. Instructions to create the Secret via `kubectl create secret` or a secrets operator.
- `serviceaccount.yaml` — K8s ServiceAccount annotated for Workload Identity, bound to a GCP Service Account with `roles/cloudsql.client`. No key files stored anywhere.
- `deployment.yaml` — 2 replicas; Cloud SQL Auth Proxy sidecar; liveness and readiness probes on `/health`; resource requests/limits defined; migrations run in an init container before the app starts.
- `service.yaml` — ClusterIP, port 80 → 8000.
- `ingress.yaml` — nginx Ingress with host-based routing.

**`.github/workflows/ci.yaml`** — three sequential jobs, all gated on the previous passing:

1. **`test`** — spins up a Postgres service container, installs deps via `uv`, runs `pytest`.
2. **`build-and-push`** — runs only on merges to `main` (skipped on PRs); authenticates to GCP via Workload Identity Federation (no SA key files in GitHub); builds and pushes to Artifact Registry tagged with `github.sha` and `latest`; layer cache via GHA cache.
3. **`deploy`** — fetches GKE credentials via `get-gke-credentials`; `kubectl set image` updates the `migrate` init container and `app` container to the exact SHA just pushed; `kubectl rollout status --timeout=5m` blocks until the rollout is healthy.

---

## What I deliberately didn't do

- **Authentication / authorization** — explicitly out of scope per the assignment. Worth noting the direction in production: JWT tokens validated in middleware, with the Ninja router enforcing auth per-endpoint.
- **Multi-stage Docker build** — would reduce the final image size by excluding build tools, but adds complexity with minimal gain at this scale. Worth revisiting if image size or supply-chain scanning becomes a concern.
- **Pagination on `get_post` comments** — a post with 500k comments is pathological, but the comment list is already `select_related` and ordered. Cursor-based pagination would be the right fix if comment volumes become a problem.
- **`create_post` tag N+1** — `Tag.objects.get(slug=slug)` in a for-loop is N queries. Easy fix (`filter(slug__in=...)`) but low priority since post creation is a low-frequency write path.
- **Helm chart** — the plain manifests are simpler to read and review. Helm makes sense once you need to deploy the same service to multiple environments with different values.
- **TLS in the manifests** — left `cert-manager` out to keep the manifests focused. The Ingress annotation for cert-manager is a one-liner once the operator is installed in the cluster.
- **Test coverage improvements** — explicitly out of scope per the assignment.

---

## What I'd do next with another day

1. **HorizontalPodAutoscaler** — scale the deployment on CPU (`targetAverageUtilization: 70`) so the service handles traffic spikes without over-provisioning.
2. **TLS** — add `cert-manager` + a `ClusterIssuer` to provision Let's Encrypt certs for the Ingress automatically.
3. **Structured logging** — configure Django's `LOGGING` setting to emit JSON to stdout so GKE's log ingestion can index fields (level, request_id, duration) rather than treating each line as an opaque string.
4. **Search ranking** — `SearchQuery` returns results in arbitrary order; adding `SearchRank` and ordering by it would make search results significantly more useful.
5. **Cursor-based pagination** — `OFFSET` pagination degrades as offset grows (Postgres still scans skipped rows). For the list endpoints a keyset cursor (`WHERE created_at < :cursor ORDER BY created_at DESC LIMIT 20`) would be both faster and more stable under concurrent writes.
6. **`create_post` tag query** — replace the N-query loop with `Tag.objects.filter(slug__in=payload.tag_slugs)`.
