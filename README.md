# AutoApply — System Design

An AI agent that discovers jobs, tailors an application, submits it, and reports back. Web dashboard for reviewing applications and reading run logs.

---

## 1. The refinement that matters most

The naive version is "LLM + Playwright, spray 500 applications." That version dies in week one for three reasons:

1. **Anti-bot walls.** LinkedIn/Indeed explicitly forbid automated submission and will ban the account.
2. **Hallucinated experience.** An LLM writing free-form resume bullets will invent jobs you never had. That's not a bug, it's a fraud risk on a real application.
3. **Unreviewable output.** If nothing is inspectable, you can't tell a successful submit from a form that silently failed.

So the design makes three inversions:

| Naive | Refined |
|---|---|
| Scrape LinkedIn/Indeed | Target **ATS platforms directly** (Greenhouse, Lever, Ashby, Workable) — public job APIs, stable form DOM, no aggressive bot defense |
| LLM freely writes the resume | **Grounded generation** from a structured fact store; a validator rejects any claim not traceable to a stored fact |
| Auto-submit everything | **Approval queue** by default; auto-submit is an opt-in per-source setting once you trust the pipeline |

---

## 2. High-level architecture

```
┌──────────────────────────────────────────────────────┐
│  Frontend (Next.js + React + Tailwind)               │
│  Dashboard · Review Queue · Run Log Viewer · Profile │
└───────────────┬──────────────────────────────────────┘
                │ REST + SSE (live log stream)
┌───────────────▼──────────────────────────────────────┐
│  API Service (FastAPI, Cloud Run)                    │
│  auth · CRUD · approve/reject · log tail · metrics   │
└───────┬───────────────────────────┬──────────────────┘
        │ enqueue                   │ read
┌───────▼─────────────┐   ┌─────────▼────────────────┐
│ Orchestrator        │   │ Postgres (Cloud SQL)     │
│ workflow + state    │   │ + pgvector               │
│ machine, retries,   │   │ jobs·apps·runs·events    │
│ human-in-loop waits │   └──────────────────────────┘
└───────┬─────────────┘   ┌──────────────────────────┐
        │ dispatch        │ GCS: screenshots, PDFs,  │
┌───────▼─────────────┐   │ DOM snapshots            │
│ Worker Pool         │   └──────────────────────────┘
│ ┌─────────────────┐ │
│ │ Ingest Worker   │ │ → pulls ATS job feeds
│ │ Match Worker    │ │ → embeddings + fit score
│ │ Tailor Worker   │ │ → LLM + validator + PDF render
│ │ Submit Worker   │ │ → Playwright, per-ATS adapter
│ └─────────────────┘ │
└─────────────────────┘
```

---

## 3. The pipeline (five stages)

### Stage 1 — Ingest
Pull from **public ATS job-board endpoints** rather than scraping aggregators:
- Greenhouse: `boards-api.greenhouse.io/v1/boards/{token}/jobs`
- Lever: `api.lever.co/v0/postings/{company}`
- Ashby / Workable: equivalent public posting endpoints

You maintain a `companies` table mapping company → ATS + board token. Seed it once from a public list, grow it manually.

**Dedup:** canonical hash of `normalize(company + title + location + ats_job_id)`. Unique index prevents the same posting entering twice from two sources.

### Stage 2 — Match
- Embed job description once (cache by content hash — this is the main cost lever).
- Embed the user profile once per profile version.
- Cosine similarity for recall, then a cheap LLM pass (gpt-4o-mini class) for precision: returns `{score, reasons[], blockers[]}`.
- **Hard filters run before the LLM**, never after: visa sponsorship, years-required, location, clearance. Cheap regex/rule checks kill 60% of candidates for free.

### Stage 3 — Tailor (the part that needs discipline)

The **fact store** is a structured table of verified claims:

```json
{
  "id": "exp_003",
  "type": "experience",
  "org": "Morningstar",
  "role": "Software Engineering Intern",
  "start": "2026-06", "end": null,
  "facts": [
    "Built X service in Python reducing Y latency by Z%",
    "Owned CI pipeline for N repos"
  ],
  "skills": ["python", "aws", "ci/cd"]
}
```

Generation contract:
1. Retrieve top-k relevant facts for this job.
2. LLM may **only rephrase and reorder** retrieved facts — never introduce new orgs, dates, numbers, or technologies.
3. **Validator step:** every generated bullet must cite the `fact_id` it derives from. A second cheap model (or NER + numeric diff) checks that no entity or number appears in the output that isn't in the source fact. Fail → regenerate once → fail again → route to manual review.
4. Render to PDF from an HTML/LaTeX template. Store in GCS, hash-addressed.

This is the same human-augmented-verification shape as WikiVerify — the model proposes, a deterministic layer verifies, a human adjudicates the ambiguous middle.

### Stage 4 — Submit

Playwright worker, one browser context per application, fresh profile.

**Per-ATS adapter pattern.** Each adapter implements:

```python
class ATSAdapter(Protocol):
    def detect(self, url: str) -> bool: ...
    def map_fields(self, page) -> dict[str, Field]: ...
    def fill(self, page, application: Application) -> None: ...
    def submit(self, page) -> SubmitResult: ...
    def verify(self, page) -> bool: ...   # confirmation text / URL change
```

Field mapping is **heuristics first** (label text, `name`, `aria-label`, `for` attributes matched against a known synonym dictionary), **LLM fallback second** for unrecognized fields — and the LLM answer is cached per `(ats, field_signature)` so you pay for it once, not once per application.

Escalation rules — pause the workflow and notify the user, don't guess:
- CAPTCHA or 2FA challenge
- Account creation required (Workday, mostly)
- Free-text question over N characters ("Why do you want to work here?") when confidence is low
- Any field mapped with confidence below threshold

**Verification is mandatory.** A submit isn't `submitted` until the adapter confirms a success signal. Capture a screenshot + DOM snapshot at submit time either way — that artifact is what makes the log viewer worth building.

### Stage 5 — Outcome tracking
Gmail API watch on a dedicated alias (`you+apply@gmail.com`). Classify inbound mail into `confirmation | rejection | interview_request | other`, match back to the application via company + thread heuristics. This closes the funnel and gives the dashboard real numbers instead of just "submitted."

---

## 4. Application state machine

Every application row is a state machine — this is what makes the frontend logs coherent instead of a wall of text.

```
discovered → matched → drafted → pending_review
                                      │
                    ┌─────────────────┼──────────────┐
                 rejected          approved      needs_input
                                      │               │
                                  submitting ◄────────┘
                                      │
                        ┌─────────────┼───────────┐
                    submitted      failed    blocked
                        │
              ┌─────────┼──────────┐
         confirmed  interview   declined
```

Rules: transitions are append-only events, never in-place mutation. `run_events` is the source of truth; the `applications.state` column is a materialized cache of the latest event.

---

## 5. Data model

```sql
users(id, email, created_at)
profiles(id, user_id, version, base_resume_json, updated_at)
facts(id, profile_id, type, org, role, start, end, text, skills[])
companies(id, name, ats_type, board_token, careers_url)
jobs(id, company_id, ats_job_id, title, location, description,
     remote, canonical_hash UNIQUE, posted_at, embedding vector(1536))
matches(id, job_id, profile_id, score, reasons_json, blockers_json)
applications(id, user_id, job_id, state, resume_artifact_id,
             cover_artifact_id, submitted_at,
             UNIQUE(user_id, job_id))          -- idempotency
runs(id, application_id, stage, started_at, ended_at, status, cost_cents)
run_events(id, run_id, ts, level, event_type, message, payload_json)
artifacts(id, kind, gcs_uri, sha256, bytes)
credentials(id, user_id, provider, ciphertext, kms_key_version)
```

Two things to not skip:
- `UNIQUE(user_id, job_id)` — the single line that prevents the embarrassing double-apply.
- `credentials` encrypted with **KMS envelope encryption**, and a log scrubber that redacts on write, not on read.

---

## 6. Frontend

**Stack:** Next.js (App Router), React, Tailwind, TanStack Query, shadcn/ui.

Four screens:

1. **Dashboard** — funnel counters (discovered → matched → submitted → responses), response rate by company/source, spend this week.
2. **Applications table** — filter by state/company/date; row expands into a timeline of `run_events` with the submit screenshot inline.
3. **Review queue** — the primary daily surface. Side-by-side: job description | generated resume diff vs base (added bullets highlighted, each hoverable to show its source `fact_id`) | cover letter. Buttons: Approve · Edit · Skip.
4. **Run log viewer** — live tail via **SSE** (`GET /api/runs/{id}/stream`), filterable by level and stage. Not WebSockets: logs are one-directional, SSE reconnects for free, and it survives Cloud Run's proxy without extra config.

---

## 7. Infra on GCP

| Concern | Choice | Why |
|---|---|---|
| API | Cloud Run | scale-to-zero, you already know it |
| Workers | Cloud Run Jobs (ingest/match) + a **GKE Autopilot or Compute MIG** pool for Playwright | headless Chrome needs ~1GB RAM and a warm start; Cloud Run's cold start hurts here |
| Queue | Cloud Tasks (simple) → **Temporal** if it grows | long-running, multi-step, human-in-the-loop waits are exactly Temporal's shape |
| DB | Cloud SQL Postgres + pgvector | one datastore for relational + vector beats bolting on a vector DB |
| Storage | GCS, lifecycle rule 90d on screenshots | artifacts get big fast |
| Secrets | Secret Manager + KMS | never env vars for user credentials |
| Observability | Cloud Logging + OpenTelemetry traces, one `trace_id` per application | you want to follow one application across all five stages |
| Egress | Per-user proxy identity, jittered pacing, daily cap | see §8 |
| CI/CD | GitHub Actions → Artifact Registry → Cloud Run | |

**On n8n:** great for the ingest/notification edges, wrong tool for the submit path. Browser automation with retries, artifacts, and state needs real code. Use n8n for "new match → Slack/email me," not for the core workflow.

---

## 8. Constraints to design around, not discover later

- **ToS.** Automated submission violates LinkedIn's and Indeed's terms. Applying directly through a company's own ATS is a much more defensible posture, and it's also where the better-quality postings are. Keep the volume human-plausible (10–30/day, not 500) — high volume is what triggers both bans and recruiter blocklists.
- **Honesty.** The fact-store + validator design isn't just architecture hygiene; a tailored resume that overstates is a real problem for you, not for the bot. Keep the human approval gate until the validator has a track record.
- **Cost.** Embeddings cached by content hash, small model for match/classification, large model only for cover letters, field-mapping answers cached per ATS. Track `cost_cents` per run so the dashboard shows cost-per-application.
- **Rate limits.** Token bucket per ATS domain, exponential backoff with jitter, circuit breaker that trips a source after N consecutive failures.

---

## 9. Build order

**Phase 1 — vertical slice (2–3 weeks).** One user (you), Greenhouse only, manual approval, no frontend beyond a table. Prove: ingest → match → tailor → submit → confirm on ten real applications. This is the phase that tells you whether the whole idea works.

**Phase 2 — the product.** Add Lever + Ashby adapters. Build the review queue and the SSE log viewer. Add state machine, artifacts, screenshots. Add outcome tracking via Gmail.

**Phase 3 — hardening.** Temporal for orchestration. Multi-user with encrypted credentials and per-user isolation. Auto-submit opt-in for high-confidence matches. Analytics: which resume variants correlate with callbacks.

**Phase 4 — leverage.** A/B test resume phrasings against real response rates. That dataset — tailoring strategy vs callback rate — is the genuinely novel thing here and nobody else has it.