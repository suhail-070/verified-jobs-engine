# Verified Jobs Engine

Direct-from-employer job aggregation for India, with an explainable trust score and work-mode filters (WFO / Hybrid / WFH / WFA).

> **Status: design phase — no code yet.** The full design lives in [docs/SPEC.md](docs/SPEC.md).

## How to use this repo

1. **Read the spec** — start with §2 (non-goals), §4 (data sources), §7 (work mode), §8 (trust score).
2. **Answer the open questions** in [§14](docs/SPEC.md#14-open-questions) before building.
3. **Run Milestone 0 first** — the India coverage spike. Do not start M1 until M0 passes its go/no-go (≥ 40% of target employers reachable via legitimate sources).

## Planned stack

| Layer | Choice |
|---|---|
| Database | PostgreSQL 17 + `pg_trgm`, `pgvector`, `unaccent` |
| Queue | Postgres `SKIP LOCKED` (no Redis) |
| Workers | Python 3.12 (`httpx`, `selectolax`, `extruct`) |
| API | FastAPI |
| Web | Next.js + Tailwind |
| LLM | Claude API (Batch API for bulk extraction) |

## Planned local setup (once code lands)

```bash
git clone https://github.com/suhail-070/verified-jobs-engine
cd verified-jobs-engine
docker compose up -d db        # Postgres 17 with extensions
make migrate                   # apply schema from docs/SPEC.md §6
make worker                    # run fetch/normalise/score workers
make web                       # start API + web UI
```

These commands are placeholders and will not work until M1 is implemented.

## Ground rules

- No scraping of LinkedIn, Naukri, Indeed, Foundit or Google Jobs.
- No collection of third-party personal data (recruiter/HR contacts).
- Respect `robots.txt`; stop on any block — never evade.

See [docs/SPEC.md §2 and §11](docs/SPEC.md#2-non-goals-hard-constraints) for the full list.
