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

## What I'd do next

- **`get_post` comments N+1** — fetching a post with many comments still fires one query per comment author. `select_related` on the comments queryset would fix it.
- **`create_post` tag loop** — `Tag.objects.get(slug=slug)` in a for-loop is N queries; `Tag.objects.filter(slug__in=payload.tag_slugs)` would be 1.
- **Production readiness** — Promote `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS` to env vars; add a `/health` endpoint.

---

## Session log

| Date | Decision |
|------|----------|
| 2026-09-06 | Agreed to focus on DX first, then performance, then production readiness |
| 2026-09-06 | Wrote Dockerfile, updated docker-compose, env-ified DB settings, updated README |
