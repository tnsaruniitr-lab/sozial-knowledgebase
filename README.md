# sozial-knowledgebase

Human-queryable knowledge base for an ambulanter Pflegedienst (Sozialstation
Mobil). Structured care data — patients, tours, services, billing, budgets —
lives in **Supabase Postgres (Frankfurt / eu-central-1)** so it stays in
Germany, and is queried in plain language.

## Architecture (one data layer, several consumers)

```
            Supabase (Frankfurt)  ── single source of truth
            patients · tours · visits · visit_services
            lk_codes · invoices · budgets · (authorizations)
                 ▲                         ▲
   reads tours   │                         │  reads everything (queries)
   ┌─────────────┴───────┐     ┌───────────┴──────────────┐
   │  tour-planner        │     │  sozial-knowledgebase     │
   │  (map / routing view)│     │  (this repo: Q&A / views) │
   └──────────────────────┘     └──────────────────────────┘
```

- **Structured-first.** Most questions ("which §45 budgets expire 30 June?",
  "tour efficiency", "loss-making patients") are answered with **SQL views** —
  exact and auditable, not RAG guesses.
- **Ask-anything (later).** A natural-language → read-only-SQL layer for ad-hoc
  questions, plus a thin **pgvector RAG** layer only for the unstructured PDFs
  (Verordnungen / care plans).

## Layout

| Path | What |
|---|---|
| `supabase/schema.sql` | tables (apply once) |
| `supabase/views.sql`  | the seed questions, as views |
| `etl/load_structured.py` | parse the source files → load Postgres |
| `etl/data/` | drop the source `.xls` here — **git-ignored (PII)** |

## Compliance

This is **special-category health data (GDPR Art. 9 + §203 StGB)**. Rules:
- DB region = **Frankfurt**; sign the Supabase DPA (or move to a German-owned
  host — Hetzner/IONOS — for production).
- **Never commit patient files or secrets** (see `.gitignore`).
- The app reads via the **publishable key + row-level security**; the loader and
  any text-to-SQL server use the **connection string from an env var**, never in
  code.

## Run (once the DB exists)

```bash
pip install -r etl/requirements.txt
export DATABASE_URL='postgresql://…@…eu-central-1.pooler.supabase.com:5432/postgres'
# put the source .xls files in etl/data/  (pg.xls, bill.xls, 39-june.xls, 45-june.xls, tour1.xls)
psql "$DATABASE_URL" -f supabase/schema.sql      # or apply via Supabase MCP
psql "$DATABASE_URL" -f supabase/views.sql
python etl/load_structured.py                     # patients, prices, budgets, invoices
```

## Live engine — answer with Claude, no API key (POC)

For local POC use the ask box can be powered by a Claude session on this machine
instead of a paid API key — full quality, zero per-query cost. It's a tiny file
queue, not a network service:

```
browser → /ask → bridge/q/<id>.json ──▶  Monitor loop notifies the Claude session
                       ▲                          │ writes SQL, runs it read-only
   server polls bridge/a/<id>.json ◀── bridge/answer.py
```

- `server.py` `/ask`: if no `LLM_API_KEY`, and the heartbeat `bridge/ENGINE` is
  fresh (<12 s), it queues the question and waits for the answer file.
- The Claude session runs a watcher over `bridge/q/`; on each question it composes
  the SQL and calls `bridge/answer.py <id> "<sql>"` (single SELECT, READ ONLY
  txn, statement timeout, auto-LIMIT — same guards as the API path).
- The box shows a **🟢 Live-Engine verbunden** badge while attached; **⚪ Engine
  offline** otherwise. Runtime files under `bridge/q,a` + `ENGINE` are git-ignored.
- **Limits:** only works while that Claude session is open and watching; latency =
  the session's response time; for always-on/unattended use, set `LLM_API_KEY`.

## Status

- [x] Schema + seed-question views
- [x] ETL for the structured sources (patients, LK prices, budgets, invoices)
- [x] Tours/visits loader — FULL Apr 1–May 24 loaded (8,884 visits); parser in etl/parser/
- [x] Tier-1 query app (app/index.html) — 4 canned questions, live from Supabase
- [x] Natural-language ask box + backend (app/server.py) — NL→SQL→read-only
  - **Two engines, same box:** set `LLM_API_KEY` (Anthropic/OpenAI) for unattended
    self-serve, **or** run the live in-session bridge (no key) — see below.
- [ ] pgvector RAG over Verordnungen / care-plan PDFs
- [ ] `authorizations` (extracted from Muster 12 / HKP) → "authorised vs serviced"
