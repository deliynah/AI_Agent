# wellConnected — RFP & Grant Opportunity AI Agent: Implementation Plan

## Project Brief

I am building an AI agent for **wellConnected**. This company developed a platform
called **allco**, which provides a space for healthcare equity between CBOs
(Community-Based Organizations) and communities. The platform allows information to be
shared between users and healthcare organizations. For example: a patient wants to
transfer to another doctor but doesn't want to go through the whole process of
re-entering their data — the platform lets that doctor gain access to that data without
the patient having to re-fill their entire info. The platform also lets users get the
support they need by giving them easy access to reach out to different health
organizations and request services (e.g., food pantries).

This agent is meant to give wellConnected a prototype: an AI Agent (using LLM APIs and
automated workflow tools) designed to **scrape, filter, and evaluate external RFP and
grant opportunities** based on company-specific keywords, implemented in `main.py`.

---

## Key Finding

`main.py` is already **~90% built**. All eight pipeline stages exist and are
well-structured:

```
scrape → keyword filter → LLM evaluate → eligibility check → SQLite store → HTML report → notify
```

The real work is **not** writing the agent from scratch — it is that `main.py` was wired
to a config schema that does not match the actual `config.json`. The pipeline
orchestration, SQLite layer, HTML report, date parsing, and the LLM-response parser are
all already complete and need no changes.

### Schema mismatches (the gaps this plan closes)

| `main.py` expects | `config.json` actually has | Result |
|---|---|---|
| `state_portals` (in `REQUIRED_TOP_KEYS`) | `scarping_sources` (typo) | Hard crash in `load_config()` — **FIXED in Phase 1.1** |
| `config["scraping_sources"]` | `scarping_sources` (typo) | 0 sources scraped — **FIXED in Phase 1.1** |
| `config["search_criteria"]["must_have_keywords"]` | `keywords.required/optional/excluded`, nested by category | Keyword filter does nothing |
| `config["evaluation"]` (model, min score, prompt) | *missing* | Defaults only; no prompt |
| `config["company_profile"]` | *missing* | Empty company name in prompt + report |
| `config["output"]["database"/"report"]` | *missing* | Falls back to defaults (works by luck) |
| `config["notifications"]`, `config["logging"]`, `config["scheduler"]` | *missing* | Notifications/scheduler inert |
| `run_pipeline()` is never called | `start_scheduler` is commented out | Running `main.py` does nothing |

Also missing: `requirements.txt`, and API-key loading (`anthropic.Anthropic()` needs
`ANTHROPIC_API_KEY`).

---

## The Plan

### Phase 1 — Reconcile config ↔ code (critical path)

**1.1 Fix the crash. ✅ DONE**
- Renamed `scarping_sources` → `scraping_sources` in `config.json`.
- Updated `REQUIRED_TOP_KEYS` in `main.py` (dropped `state_portals`, added `scraping_sources`).
- Verified: `load_config()` now passes validation; 21 scraping sources detected.

**1.2 Rewrite `keyword_filter()` for strict per-category matching** (main.py lines 255–274).
New logic:
- An opportunity passes only if its `title + description` text contains **≥1 keyword from
  *each* required category**: `certification&security` **AND** `geographic_scope` **AND**
  `core_infrastructure`.
- If it matches **any** `excluded` keyword → reject.
- `optional` keywords don't gate — count matches and store as `keyword_score` to boost the
  relevance score downstream.
- Log every drop with **which required category failed**, so keywords can be tuned from
  real data.

> ⚠️ Strict per-category is high-precision but brittle: a perfectly relevant grant that
> doesn't mention security/HIPAA gets dropped *before* the LLM sees it. Mitigation drafted
> below (keyword additions). Open decision: keep strict 3-category AND, or soften
> `certification&security` to scored-only.

**1.3 Add the missing config sections** so every stage has its inputs:
- `company_profile` — wellConnected/allco mission text (used in LLM prompt + report header).
- `evaluation` — `llm_model`, `min_relevance_score`, `min/max_days_until_deadline`,
  `flag_sole_source`, `evaluation_prompt_template`.
- `output` — `database.path`, `report.output_path`, `max_opportunities_per_report`.
- `notifications`, `logging`, `scheduler` — present even if disabled, so `.get()` chains
  resolve cleanly.

### Phase 2 — LLM evaluation quality
- **2.1** Author `evaluation_prompt_template` injecting `company_profile` + opportunity
  fields (the `{key}` substitution at main.py line 281 already supports this), instructing
  Claude to return the exact JSON `_parse_llm_response` expects (`relevance_score`,
  `summary`, `red_flags`, `win_likelihood`).
- **2.2** Fold `keyword_score` from Phase 1 into the prompt as a signal. Default model:
  `claude-sonnet-4-6` (or `claude-opus-4-8` for higher-quality judgment at higher cost).
- **2.3** ✅ DONE — Claude tool-use for deterministic JSON parsing. Added `_EVALUATION_TOOL`
  schema to `main.py` and switched `llm_evaluate` to `tool_choice={"type":"tool","name":"evaluate_opportunity"}`.
  Claude is now forced to return a typed struct; `_parse_llm_response` remains as a text fallback only.

  -personal notes: 
    -instructions Claude recieves when judging whether grant is good fit fore wellconnected
    -Use Claude's tool-use feature to force a guaranteed JSON structure back 

### Phase 3 — Operational glue
- **3.1** `requirements.txt`: `anthropic, requests, beautifulsoup4, apscheduler, jinja2, python-dotenv`.
- **3.2** `ANTHROPIC_API_KEY` via `.env` + `python-dotenv` (add `.env` to `.gitignore`).
- **3.3** Wire the entrypoint (main.py line 760): call `run_pipeline(config)` for a
  one-shot run, or finish/uncomment `start_scheduler` for daily runs.

  -personal notes: 
    -3.1: creates the file listing all the python libraries the prject depends on, easier to install (just need pip install -r ...)
    -3.2: creates a file to storing ANTHROPIC_API_KEY (secret password-like string that Anthropic gives to aunthetiticate code when it makes calls to Claude's API) <-- main.py cals antrhopic.Antrhopic() that uses Claude for evaluating grant oppurtunities. Antrhopic's server need to verify that requests are legitimate + authorized account 
      - stores in a .env + .gitignore means it stays on local machine only, so if you were to push main.py code into GitHub nobody would be able to see these authentications 
    -3.3: main.py currently does not run correctly so it calude will fix that, run_pipeline never is called 

### Phase 4 — Source-specific scraping ✅ DONE

Live inspection results (fetched all enabled sources with requests + BeautifulSoup):

| Source | Status | Selectors |
|---|---|---|
| City of Buffalo Bids `.aspx` | ✅ working — 10 bids | `.listItemsRow` / `.bidTitle a` / `.bidStatus > div:nth-of-type(2) > span:nth-of-type(2)` |
| Buffalo CivicAlerts | ✅ working — 1 article | `main#main-wrapper` / `.article-header-title` / `.article-content` |
| NYS DOH RFP index | ✅ working — 23 RFPs | `table.alt_row tbody tr` / `td:nth-of-type(1) a` / `td:nth-of-type(2)` |
| **NYS OMH RFPs** (newly enabled) | ✅ 140 RFPs | `table tbody tr` / `td:nth-of-type(1) a` / `td:nth-of-type(2)` |
| **OMH Upcoming Procurements** (newly enabled) | ✅ 38 listings | `.col-md-9 li` / `a` / `a` |
| Erie County Purchasing | ❌ 404 — URL dead, left disabled | — |

**Total pipeline input: 212 raw opportunities from 5 live sources.**

Also fixed in `main.py` `_DATE_FORMATS`:
- Added `%m/%d/%y` → handles OMH format `"8/13/26"` (2-digit year)
- Added `%m/%d/%Y %I:%M %p` → handles Buffalo Bids format `"6/30/2026 4:00 PM"`

Note on the keyword filter: RFP listing pages only expose titles + deadlines (no body text), so all three required AND-gates must fire on the title alone. Opportunities that pass will be ones that explicitly name HIPAA/data sharing/geographic scope in their title — this is high-precision by design.

- personal notes: sites have different HTML structure, this phase inspected each live site to figure out which CSS classes/elements contain opportunity listings
---

## Drafted Keyword Additions (Phase 1.2, pending approval)

The matcher does a simple `keyword.lower() in text` substring check, and most current
required terms are long exact phrases an RFP will never contain verbatim. For the strict
AND-gate to ever pass, each required category needs short, real-world tokens (all existing
phrases are kept).

### `certification&security` (hardest gate — currently 0 single tokens)
`HIPAA`, `data security`, `data privacy`, `confidentiality`, `PHI`, `data protection`

### `geographic_scope` (phrases won't match real formatting like "Buffalo, NY")
`Buffalo`, `New York`, `Erie County`, `Niagara`, `Chautauqua`, `Cattaraugus`,
`Chicago`, `Illinois`, `WNY`

### `core_infrastructure` (only "Interoperability" is a usable token today)
`data sharing`, `data exchange`, `information sharing`, `system integration`,
`single source of truth`

---

## Execution Order

1. Phase 1.1 (✅ done) → 1.3 (✅ done) → 1.2 (✅ done)
2. Phase 3 (✅ done) → Phase 2 (✅ done) → Phase 4 (✅ done)

All phases complete. Next steps: end-to-end test with a real `ANTHROPIC_API_KEY`.

## Open Decisions

1. ✅ Keyword list approved and merged into config.json.
2. ✅ Keeping strict 3-category AND (including `certification&security`).

## Phase 5 — End-to-end test & ongoing source expansion (next steps)

- Set `ANTHROPIC_API_KEY` in `.env` and run `python main.py` for a real pipeline run.
- Inspect `agent.log` and the generated `reports/*.html` to verify LLM scores + HTML output.
- If 0 opportunities survive the keyword filter consistently, consider scraping individual
  RFP detail pages (follow links from listing pages) to get full body text — this gives the
  security/data-sharing keywords a chance to appear.
- Potential sources still to add selectors for (all currently disabled):
  - **NYS Contract Reporter** (nyscr.ny.gov) — requires account/navigation, skip for now
  - **Community Foundation for Greater Buffalo** (cfgb.org) — inspect for grant deadlines page
  - **Erie County Purchasing** — URL is 404; find updated URL
  - **NYS Grants Gateway** — login-walled, skip
