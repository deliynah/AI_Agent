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
- **2.3** (Optional hardening) Use Claude tool-use / structured output for deterministic
  JSON parsing.

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

### Phase 4 — Source-specific scraping (follow-up)
- Enabled sources (Buffalo Bids `.aspx`, Buffalo CivicAlerts, NYS DOH RFP index) are HTML
  pages; `scrape_web` needs per-site CSS `selectors` blocks. The generic `article`
  selector won't match these portals. yap yap yap 
- May require live-page inspection to tune selectors.
- Optionally add an LLM-extraction fallback for unstructured pages.

- personal notes: sites have different HTML strucutre, this phase will insepct each live site to figure out which CSS classes/elemnts contain oppurnuity listings 
---

## Drafted Keyword Additions (Phase 1.2, pending approval)

The matcher does a simple `keyword.lower() in text` substring check, and most current
required terms are long exact phrases an RFP will never contain verbatim. For the strict
AND-gate to ever pass, each required category needs short, real-world tokens (all existing
phrases are kept).
-Personal notes: 
  -check for case-sensitivity 
  -phase 1.2 adds a safety check that if too many required category failed to match then it logs that the keywords in the category need more short/realstic token added 


## Additional features 
Feature Request: Keyword Filter Drop-Rate Alert
When the keyword filter runs, track how many opportunities are evaluated versus how many are rejected. If the rejection rate exceeds a set threshold (for example, 90% or more of all scraped opportunities are dropped in a single pipeline run), automatically send a notification to the developer/user.
The notification should include:

The total number of opportunities scraped
The total number rejected by the keyword filter
The rejection rate as a percentage
A breakdown of which required keyword categories were responsible for the most drops (e.g. certification&security failed 45 times, geographic_scope failed 12 times)
A warning message saying something like: "High rejection rate detected — the keyword filter may be too strict. Consider reviewing and expanding the keyword list in config.json."

This notification should be delivered through the existing notifications system already planned in Phase 1.3, using whatever channel is configured (email, Slack, etc.).
The threshold for triggering the alert (e.g. 90%) should be a configurable value in config.json under the notifications section so it can be adjusted without touching the code.

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

1. Phase 1.1 (✅ done) → 1.3 (config sections) → 1.2 (filter rewrite + keyword additions)
2. Phase 3 (so it can call the API) → smoke-test end-to-end
3. Phase 2 (prompt quality) → Phase 4 (real scraping selectors)

## Open Decisions

1. Approve the drafted keyword list as-is, or edit it.
2. Keep strict 3-category AND, or soften `certification&security` to scored-only.


### Phase 5 — Replace Hard Keyword Gate with Scored Priority System ✅ DONE

**What changed:**
- `keyword_filter()` rewritten: removed the AND-gate logic that dropped RFPs failing any required category. Only excluded keywords still cause hard rejection.
- Every non-excluded RFP now passes to the LLM, scored and sorted by `keyword_score`.
- `keyword_matches` dict built per-RFP: per required category, records exactly which keywords were found (True/False checklist).
- `keyword_score` = (required_matched + optional_matched) / total_keywords — 0–1 float.
- RFPs sorted by `keyword_score` descending before entering LLM evaluation.
- `keyword_matches_summary` injected into the LLM prompt so Claude sees which terms matched.
- HTML report now shows a keyword checklist per card (green chips = matched, gray = not matched) and optional keyword summary.
- `--debug` report updated: now shows all 212 RFPs ranked by score with their full keyword checklist, instead of the old rejection list.

**Result:** 212 RFPs now reach the LLM (previously 0). The most keyword-aligned opportunities are evaluated first.
