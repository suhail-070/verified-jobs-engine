# Verified Jobs Engine — Product & Technical Spec (v0.1)

> Working name. Rename freely.
> Status: Draft. Target market: **India**.
> Confidence tags used throughout: **[Certain]** verified fact, **[Likely]** strong inference, **[Verify]** must be confirmed in Milestone 0 before building on it.

---

## 1. Problem

Indian job seekers waste enormous time on listings that are stale ("ghost jobs"), posted by consultancies to harvest resumes, or outright scams (registration fees, WhatsApp-only "HR"). Existing portals optimise for listing volume, not listing truth.

**Positioning:** *Only direct-from-employer jobs, each with an explainable trust score.*
Trust comes primarily from **where the data comes from** (the employer's own ATS / career domain), and secondarily from detection. Detection alone cannot make a dirty source clean.

## 2. Non-goals (hard constraints)

These are deliberate and are not to be revisited without legal advice:

1. **No scraping of LinkedIn, Naukri, Indeed, Foundit, or Google Jobs.** Their ToS prohibit it; circumventing bot protection (proxy rotation, fingerprint spoofing, CAPTCHA solving) is out of scope entirely.
2. **No harvesting, storing, or scoring of individuals' personal contact data** (emails, phone numbers, profiles of HR staff). DPDP Act 2023 applies.
3. **No resume collection in MVP.** We are a discovery layer, not an ATS. Apply buttons link to the employer's own apply page.
4. **No paid placement / ads in the feed.** It contradicts the trust positioning.

## 3. Users & MVP scope

**Primary user:** Indian tech/professional job seeker (0–12 yrs), tired of fake listings.

**MVP must do:**
- Ingest jobs from employer-owned sources (§4).
- Normalise into one schema incl. location, **work mode**, salary, seniority, skills.
- De-duplicate across sources.
- Compute an explainable trust score with visible reasons.
- Search + filter UI (incl. work-mode filter, §7).
- "Find the hiring team" deep-link (§9).

**MVP will not do:** accounts beyond optional saved searches, alerts (post-MVP), mobile apps, recruiter-side features.

## 4. Data sources (India)

### 4.1 The honest risk
**[Likely]** The public-ATS ecosystem (Greenhouse, Lever, Ashby) skews to startups and global companies. Large Indian employers frequently use Darwinbox, Keka, Zoho Recruit, SuccessFactors, Workday, or bespoke career sites. **India coverage is the #1 product risk.** Milestone 0 exists to measure it before anything else is built.

### 4.2 Source tiers

| Tier | Source type | Examples | Trust prior | Notes |
|---|---|---|---|---|
| T1 | Public ATS job-board APIs | Greenhouse, Lever, Ashby, SmartRecruiters, Workable | Highest | Designed for syndication, structured JSON |
| T2 | India-centric ATS career pages | Darwinbox, Keka, Zoho Recruit | High | **[Verify]** whether stable public JSON endpoints exist and ToS permit indexing |
| T3 | Employer career pages with schema.org `JobPosting` JSON-LD | Any company optimising for Google Jobs | High (if on company domain) | Polite crawler, robots.txt respected |
| T4 | Licensed aggregator APIs | Adzuna, Jooble | Medium | Fills gaps; must pass stricter trust checks; check India coverage + licence terms |

Known public endpoints (**[Verify]** each in M0):
- Greenhouse: `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`
- Lever: `GET https://api.lever.co/v0/postings/{company}?mode=json`
- Ashby: `GET https://api.ashbyhq.com/posting-api/job-board/{org}`
- SmartRecruiters: `GET https://api.smartrecruiters.com/v1/companies/{id}/postings`
- Workable: `GET https://apply.workable.com/api/v1/widget/accounts/{subdomain}`

### 4.3 Company registry (the seed list)
Sources are keyed off a curated `company` table, not discovered by crawling the open web.
- Seed: top Indian startups, GCCs (global capability centres), IT services, listed companies.
- For each company: canonical domain, detected ATS + board token, career URL.
- ATS detection: fetch career page → match known ATS hostnames/embeds.
- Growth: user-submitted "add this company" → verified by a job before activation.

### 4.4 Crawling rules
- Identify honestly: `User-Agent: VerifiedJobsBot/0.1 (+https://<site>/bot)`.
- Respect `robots.txt`, `Crawl-delay`, and conditional requests (`ETag` / `If-Modified-Since`).
- Per-domain rate limit (default 1 req / 5 s), exponential backoff on 429/5xx.
- On any block/CAPTCHA: **stop and mark source `blocked`**. Never escalate.
- Store raw payloads (compressed) for re-processing; retain 90 days.

## 5. Architecture

```
               ┌───────────────┐
 company ────▶ │  Scheduler    │  (per-source cadence, Postgres-backed queue)
 registry      └──────┬────────┘
                      ▼
               ┌───────────────┐      raw_payload (jsonb / zstd)
               │ Fetch workers │ ───▶ Postgres
               └──────┬────────┘
                      ▼
               ┌───────────────┐   deterministic parsers per source
               │  Normaliser   │ + LLM extraction for free text (§8.3)
               └──────┬────────┘
                      ▼
               ┌───────────────┐   pg_trgm + pgvector near-dup
               │  Dedupe       │
               └──────┬────────┘
                      ▼
               ┌───────────────┐   rules + signals → score + reasons
               │  Trust scorer │
               └──────┬────────┘
                      ▼
               ┌───────────────┐      ┌──────────────┐
               │  API (FastAPI)│ ◀──▶ │ Web (Next.js)│
               └───────────────┘      └──────────────┘
```

**Stack (proposed):**
- **PostgreSQL 17** — single source of truth. Extensions: `pg_trgm`, `pgvector`, `unaccent`. Full-text search via `tsvector` first; add a dedicated search engine only if Postgres FTS demonstrably fails.
- **Queue:** Postgres `FOR UPDATE SKIP LOCKED` (no Redis in MVP).
- **Workers:** Python 3.12, `httpx`, `selectolax`/`extruct` for JSON-LD.
- **API:** FastAPI.
- **Web:** Next.js + Tailwind.
- **LLM:** Claude API — a small/fast model (e.g. Haiku-class) for bulk extraction via the Batch API; larger model only for disputed/edge cases.
- **Hosting:** India region (e.g. Mumbai) to keep data residency simple.

## 6. Data model (core)

```sql
CREATE TYPE work_mode AS ENUM ('wfo', 'hybrid', 'wfh', 'wfa', 'unknown');
CREATE TYPE field_source AS ENUM ('structured', 'extracted', 'inferred');
CREATE TYPE source_status AS ENUM ('active', 'paused', 'blocked', 'dead');

CREATE TABLE company (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name            text NOT NULL,
  domain          text NOT NULL UNIQUE,        -- canonical, e.g. 'razorpay.com'
  hq_city         text,
  verified        boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE source (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id      bigint REFERENCES company(id),  -- NULL for T4 aggregators
  kind            text NOT NULL,                  -- 'greenhouse','lever','jsonld',...
  tier            smallint NOT NULL CHECK (tier BETWEEN 1 AND 4),
  locator         text NOT NULL,                  -- board token / URL
  status          source_status NOT NULL DEFAULT 'active',
  last_fetched_at timestamptz,
  next_fetch_at   timestamptz,
  UNIQUE (kind, locator)
);

CREATE TABLE raw_payload (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id   bigint NOT NULL REFERENCES source(id),
  fetched_at  timestamptz NOT NULL DEFAULT now(),
  http_status smallint,
  etag        text,
  body        bytea                                -- zstd-compressed
) PARTITION BY RANGE (fetched_at);                 -- monthly, drop > 90d

CREATE TABLE job (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  canonical_id          bigint REFERENCES job(id),  -- NULL = this row is canonical
  source_id             bigint NOT NULL REFERENCES source(id),
  external_id           text NOT NULL,
  company_id            bigint REFERENCES company(id),
  title                 text NOT NULL,
  description_text      text,
  apply_url             text NOT NULL,
  apply_domain          text GENERATED ALWAYS AS (substring(apply_url from '://([^/:]+)')) STORED,

  -- location & work mode (see §7)
  cities                text[],                      -- normalised Indian cities
  work_mode             work_mode NOT NULL DEFAULT 'unknown',
  work_mode_source      field_source,
  work_mode_confidence  real,                        -- 0..1
  hybrid_office_days    smallint CHECK (hybrid_office_days BETWEEN 1 AND 5),
  remote_region         text,                        -- 'IN', 'IN-KA', 'APAC', 'GLOBAL'

  -- comp & level
  salary_min_inr        integer,                     -- annual CTC
  salary_max_inr        integer,
  experience_min_yrs    smallint,
  experience_max_yrs    smallint,
  seniority             text,
  skills                text[],

  -- lifecycle
  first_seen_at         timestamptz NOT NULL DEFAULT now(),
  last_seen_at          timestamptz NOT NULL DEFAULT now(),
  closed_at             timestamptz,
  posted_at             timestamptz,                 -- as claimed by source

  -- trust
  trust_score           smallint,                    -- 0..100
  trust_reasons         jsonb,                       -- [{code, weight, detail}]

  search_tsv            tsvector,
  embedding             vector(1024),
  UNIQUE (source_id, external_id)
);

CREATE INDEX ON job USING gin (search_tsv);
CREATE INDEX ON job USING gin (title gin_trgm_ops);
CREATE INDEX ON job (work_mode) WHERE closed_at IS NULL AND canonical_id IS NULL;
CREATE INDEX ON job USING gin (cities);
CREATE INDEX ON job USING hnsw (embedding vector_cosine_ops);

-- every observation, for ghost-job detection
CREATE TABLE job_sighting (
  job_id     bigint NOT NULL REFERENCES job(id),
  seen_at    timestamptz NOT NULL,
  PRIMARY KEY (job_id, seen_at)
);
```

## 7. Work mode (WFO / Hybrid / WFH / WFA)

### 7.1 Taxonomy

| Value | Label shown | Definition |
|---|---|---|
| `wfo` | Work from office | On-site 5 days / week (or unstated on-site). |
| `hybrid` | Hybrid | Mix of office + remote. Store `hybrid_office_days` when stated (e.g. "3 days in office" → 3). |
| `wfh` | Work from home | Fully remote, **but location-restricted** (must live in India / a state / near a city for occasional visits). `remote_region` holds the restriction. |
| `wfa` | Work from anywhere | Fully remote with no location restriction (or explicitly "work from anywhere"). |
| `unknown` | Not specified | Source gives no usable signal. |

The WFH vs WFA split matters in India: many "remote" roles require residency in a specific city or state, or periodic office visits. Collapsing them misleads users.

### 7.2 How it is determined (priority order)
1. **Structured:** ATS fields (e.g. workplace type, `jobLocationType: TELECOMMUTE` in JSON-LD). **[Certain]** schema.org has no native "hybrid" value, so hybrid always needs step 2.
2. **Extracted:** LLM reads title + location + description, returns `{work_mode, office_days, remote_region, evidence_quote, confidence}`. The evidence quote is stored and must be a verbatim substring of the source text (rejected otherwise — cheap hallucination guard).
3. **Inferred:** keyword rules ("remote", "WFH", "hybrid", "x days a week") as fallback.
4. Below confidence 0.6 → `unknown`.

### 7.3 Contradictions are a trust signal
If structured says "Remote" but the description says "must relocate to Pune" or "5 days on-site", set the extracted value, and add trust reason `WORK_MODE_MISMATCH` (§8). Misleading "remote" tags are a common bait pattern.

### 7.4 Filter UX
- Multi-select chips: `WFO` `Hybrid` `WFH` `WFA`.
- Hybrid sub-filter: "max office days" slider (1–4).
- WFH sub-filter: region (Anywhere in India / my state / my city).
- Toggle **"Include jobs with unclear work mode"** — on by default, so filtering doesn't silently hide most of the catalogue. Unknown jobs show a grey "Work mode not stated" tag.
- URL-encoded filters (`?mode=hybrid,wfh&maxOfficeDays=2`) so searches are shareable.

## 8. Trust score

### 8.1 Principles
- **Explainable over clever.** Rules-based weighted score in MVP; every point traces to a reason shown on the card.
- **Source dominates.** A T1 job from the employer's own ATS starts high; a T4 aggregator job starts mid and must earn trust.
- **Behaviour > prose.** Ghost jobs are detected by lifecycle data, not by reading text.

### 8.2 Signals (initial weights — to be tuned against the labelled set, §10)

| Code | Signal | Effect |
|---|---|---|
| `SRC_TIER_1..4` | Source tier prior | 85 / 80 / 75 / 55 base |
| `APPLY_DOMAIN_MATCH` | Apply URL on company domain or its known ATS | +5 |
| `APPLY_DOMAIN_MISMATCH` | Apply URL on unrelated domain | −25 |
| `FREE_EMAIL_CONTACT` | Contact is gmail/yahoo/outlook etc. | −30 |
| `MESSAGING_ONLY_CONTACT` | WhatsApp/Telegram-only contact | −35 |
| `FEE_REQUEST` | Registration / training / security deposit fee language | −60 (hard flag) |
| `SALARY_IMPLAUSIBLE` | Salary far outside band for title + experience | −15 |
| `STALE_OPEN` | Open > 60 days continuously | −10; > 120 days −25 |
| `SERIAL_REPOST` | Same title+company closed and reposted ≥ 3× in 180 days | −20 |
| `DUPLICATE_TEXT` | Description near-identical to another company's job (trigram/embedding) | −20 |
| `WORK_MODE_MISMATCH` | Structured vs text contradiction (§7.3) | −10 |
| `COMPANY_UNVERIFIED` | Company not in verified registry | −15 |
| `POSTED_DATE_LIE` | Source claims "posted today" but first_seen is much older | −10 |

Score clamped to 0–100. Display bands: **Verified** ≥ 80, **Caution** 50–79, **Hidden by default** < 50 (viewable via toggle). `FEE_REQUEST` always hides.

### 8.3 Where the LLM is used (and isn't)
- **Used for:** structured extraction (salary, experience, skills, work mode), and classifying scam-language patterns with an evidence quote.
- **Not used for:** final verdicts, duplicate detection (deterministic), staleness (lifecycle data), or anything without stored evidence.
- Cost control: Batch API, prompt caching for the fixed instruction block, re-extract only when description hash changes.

## 9. "Find the hiring team" (no personal-data scraping)

For each job, render a button that opens a **pre-filled LinkedIn people search** in the user's own logged-in session, e.g. company + `("talent acquisition" OR recruiter OR "hiring manager")` + department keyword.
- We generate a URL. We do not fetch, store, or score any person's data.
- Optional post-MVP: a message template helper ("how to write a referral request") — content only, no contact data.

## 10. Evaluation

You cannot claim "trust" without measuring it.
- **Labelled set:** hand-label 500 listings (from T1–T4 + a sample of known-bad listings users report) as `genuine / ghost / consultancy / scam`.
- **Metrics:** precision of the "Hidden" band (we must not hide real jobs — target ≥ 95%), recall on scams (target ≥ 80%).
- **Work-mode accuracy:** label 300 jobs; target ≥ 90% exact match on the 5-way enum, ≥ 97% on remote-vs-not.
- **User feedback loop:** "Report this listing" with reason codes → review queue → feeds weights.

## 11. Legal & compliance checklist
- [ ] Each source's ToS reviewed and recorded in `source` metadata before activation.
- [ ] robots.txt honoured; bot info page published.
- [ ] DPDP Act 2023: privacy notice; only personal data collected is optional user account email; deletion on request.
- [ ] No personal data of third parties (recruiters/HR) stored.
- [ ] Takedown process for employers who want listings removed (48h SLA).
- [ ] Lawyer review before public launch — this spec is not legal advice.

## 12. Milestones

| # | Milestone | Exit criteria |
|---|---|---|
| **M0** | **Coverage spike (1–2 weeks)** | List 300 target Indian employers; detect ATS for each; report % reachable via T1/T2/T3. **Go/no-go: < 40% reachable → rethink sourcing before building further.** |
| M1 | Ingestion | T1 + T3 connectors, scheduler, raw storage, normalised `job` rows for ≥ 150 companies |
| M2 | Normalisation + dedupe | Cities, salary, experience, work mode extracted; dedupe live; work-mode accuracy measured |
| M3 | Trust v1 | All §8.2 signals live; labelled set built; metrics reported |
| M4 | Web UI | Search, filters (incl. work mode), job cards with trust reasons, hiring-team link |
| M5 | Private beta | 50–100 users, report-listing loop, weekly weight tuning |

## 13. UX direction (summary)
- Stark white, high whitespace, monospaced accents for data (developer-console feel). Dark mode supported.
- Job card: title, company, city, **work-mode chip**, salary band, trust badge with expandable reasons, "seen since" age.
- Compare view: 2–4 jobs side by side, rows aligned by field.
- No ads, no pop-ups, no interstitials. Fast: p95 search < 300 ms.
- Inspired by the reference aesthetic — no third-party branding or assets copied.

## 14. Open questions
1. Do Darwinbox / Keka / Zoho Recruit expose public, ToS-permitted job JSON? (M0)
2. Is a company-legitimacy signal from the MCA registry (CIN lookup) obtainable legally and affordably?
3. Non-tech roles in scope for MVP, or tech-only to keep extraction quality high?
4. Monetisation that doesn't compromise trust (user subscription for alerts? employer "verified" badge is a conflict of interest — avoid).
