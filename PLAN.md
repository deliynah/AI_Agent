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

### Phase 6 — Company Profile-Based RFP Relevance Matching ✅ DONE

**What changed:**
- `config.json` → `company_profile` expanded with `product`, `mission` (tagline), `mission_detail` (prior descriptive paragraph), `core_problem`, `primary_users`, `key_functionality`, `geographic_focus`, `values`, and `priority_themes` — sourced from `WellConnected_Company_Overview.html`.
- `evaluation_prompt_template` rewritten to inject the full profile (lists are joined into readable strings by `_build_evaluation_prompt`) and explicitly ask Claude to judge genuine mission fit, not just keyword overlap, weighing the `priority_themes` list heavily.
- `_EVALUATION_TOOL` tool-use schema gains two new required fields: `mission_alignment_score` (1–10) and `mission_fit_explanation` (1–3 sentence plain-language reasoning). `_parse_llm_response`'s text fallback also parses these.
- `opportunities.db` schema gains `mission_alignment_score` / `mission_fit_explanation` columns, with an `ALTER TABLE` migration in `_init_database` so existing databases upgrade in place without data loss.
- HTML report: each card now shows a "Mission Fit: X/10" badge next to the relevance score, plus a "Why this fits wellConnected" callout box rendering `mission_fit_explanation`.
- Report sort order now ranks by `(relevance_score, mission_alignment_score)` so, among equally relevant RFPs, the more mission-aligned one surfaces first.

**Result:** The agent now reasons about *why* an RFP fits wellConnected's mission — CBO collaboration, SDOH, health equity, 211/social-service integration, nonprofit compliance — instead of relying solely on keyword overlap, and that reasoning is visible in the report.

### Phase 7 — Single Persistent Report + Integrated Keyword Scorer ✅ DONE

**What changed:**
- `generate_report()` now writes to a fixed `reports/report.html` every run instead of a new `report_<timestamp>.html` file each time — one file that gets overwritten, not an ever-growing folder of one-off reports.
- `run_pipeline()` now calls `webbrowser.open()` on `report.html` after generation, so running `python main.py` opens the latest results automatically instead of requiring you to go find the file.
- The keyword-scorer debug view (`generate_debug_report`, previously only reachable via `python main.py --debug`) is now generated on **every** normal pipeline run too, saved as a fixed `reports/stats.html` alongside the main report.
- Extracted the excluded-keyword-splitting logic (previously duplicated between `run_debug` and implicitly inside `keyword_filter`) into a shared `_split_excluded()` helper, used by both `run_debug` and `run_pipeline`.
- Main report's navbar gained a "See the stats" link (`stats_filename` passed into the Jinja template) that opens `stats.html` in a new tab — so the keyword-match breakdown is one click away from the results report instead of a separate CLI invocation.

**Result:** `python main.py` now produces exactly two live artifacts — `reports/report.html` and `reports/stats.html` — and opens the report in the browser automatically when the run finishes. Older timestamped report files from prior runs still exist on disk but are no longer produced going forward.

---

Original feature request (for reference):



Add a new implementation to the AI Agent that scores and recommends RFPs based on wellConnected's specific company values, goals, and mission. Use the following company profile information extracted directly from the codebase:
Company Profile to embed in config.json under company_profile:

Product: allco — the first centralized social care platform built to streamline workflow processes for CBOs (Community-Based Organizations)
Mission: Collaborative community care, all in one place
Core problem being solved: Fragmentation — separate organizations working off disconnected, isolated systems instead of shared connected data
Primary users: Organization Admins at CBOs, nonprofits, and hospitals — not individual consumers
Key functionality: Cross-organization referrals, case sharing, community member identity reconciliation across agencies (MPI), social determinants of health (SDOH) services
Geographic focus: New York (governing law), Western New York, Buffalo
Values inferred from the codebase:

Collaboration over silos
Meeting people where the existing system already is (e.g. integrating with 211 rather than replacing it)
Formal accountability to nonprofit/CBO sector compliance structures (501(c)(3), tax status tracking)
Consolidation as the core pitch



What to build:

Add the company profile above into config.json under a company_profile section so it can be referenced across the pipeline
In the LLM evaluation prompt, inject the full company profile so Claude can reason about whether each RFP is genuinely aligned with wellConnected's mission — not just keyword matching but actual mission fit
Add a mission_alignment_score field to the LLM evaluation output alongside the existing relevance_score, specifically rating how well the RFP aligns with wellConnected's goals of CBO collaboration, social care infrastructure, and health equity
In the final HTML report, display a "Why this fits wellConnected" section for each recommended RFP explaining in plain language how it connects to the company's mission
Prioritize RFPs that mention any of the following themes, as they are core to wellConnected's identity:

CBO or nonprofit technology infrastructure
Social determinants of health (SDOH)
Care coordination or referral management
Health equity or community health
Data sharing between healthcare and social service organizations
211 integration or social service navigation
Nonprofit compliance or 501(c)(3) organizations



The goal is to move beyond generic keyword matching and have the agent understand why a grant is or isn't a good fit for this specific company.

Phase 7: 

Update the HTML report page with the following changes:
Remove:

The keyword match breakdown that currently appears in the report body — this information is already available in the "See score breakdown" dropdown so it is redundant

Add the following three sections to each RFP card:

RFP Summary & Key Points

A short 2-3 sentence plain language summary of what the RFP is about
A highlights section that calls out:

How much funding is being offered
The deadline
What they are specifically looking for in an applicant
Any eligibility requirements




Stats Dropdown

A collapsible <details> section labeled "Stats"
When opened it shows the scores specific to that RFP:

Relevance score
Mission alignment score
Keyword score
Win likelihood


Display these visually with a simple progress bar or percentage so it is easy to read at a glance


How wellConnected Can Win This RFP

A dedicated section generated by Claude giving specific actionable advice on how wellConnected should position itself to win this particular grant
This should reference wellConnected's actual strengths — CBO collaboration, centralized social care platform, SDOH focus, cross-organization referral system — and connect them directly to what the RFP is asking for
Should also flag any gaps or weaknesses wellConnected should address in their application