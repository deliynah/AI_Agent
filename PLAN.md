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

/ 8: 
Here's the value chart, scored 1–5 from your tiers (5 = lead with it) and laid against how much this specific RFP actually cues each term.
Category-level value
Category
Value
Your tier
RFP cue
Read
Core Infrastructure
★★★★★
T1
High
Value and RFP align — anchor the proposal here
Referral & Intake
★★★★★
T1
Med
The strategic core; RFP under-cues it (foreground it yourself)
Care & Case Management
★★★★★
T1
Med
Provable daily-workflow layer; RFP light on the language
Collaboration & Community
★★★★★
T1
High
Network effect + RFP-cued — strong lead
Data, Metrics & Value
★★★★
T2
High
Differentiator; RFP wants reporting/analytics specifically
Scale, Strategy & Funding
★★★★
T2
High
Governance/sustainability are RFP-central
Certifications & Security
★★
Gate
High
Table stakes — score pass/fail, don't sell it
Geographic Scope
★
Filter
Absent
Routing filter only; nothing to score in an Iowa RFP

Keyword-level value chart
★★★★★ — Lead (Tier 1)
Keyword
RFP cue
Note
Integration hub / engine
High
Most RFP-cued infra term
Data sharing platform
High
Core of the ask
Interoperability / interoperable systems
High
The hub-vs-spreadsheet line
Cross-sector / multi-agency collaboration
High
Makes everything else worth anything
Health & human services integration
High
Substance of cross-sector
Community resource network / infrastructure
Med
Named in your Tier 1
Referral management
Med
Central workflow
Single source of record / master index
Low
High value; MPI is harder to prove
Closed-loop referral (mgmt + closure)
Low
THE category, but RFP barely says it — foreground proactively
Referral status / closure rate
Low
The proof point buyers want
Coordinated entry / shared intake
Low
High value; needs cross-org buy-in
Care coordination / coordinated care
Low
Mature, easy to demonstrate
Case management
Low
Mature, easy to demonstrate

★★★★ — Strong differentiator (Tier 2, plus Tier-1 mechanisms)
Keyword
RFP cue
Note
APIs / open APIs / RESTful
High
Expected plumbing, not a differentiator on its own
Standards (FHIR / HL7 / NIEM)
High
Required mechanism, near table-stakes
Analytics / reporting / dashboards
High
RFP-heavy and operationally provable
Governance / long-term operations
High
RFP-central; provable sustainability
Outcomes measurement / performance metrics
Med
Buyers increasingly demand it
Data quality / metrics
Med
Operational, provable
Scalability / replicability
Med
As vendor, tie to real footprint — don't overclaim
Sustainability / sustainable funding model
Med
De-risks vendor-collapse fear
Real-time data exchange
Med
Valuable; universal proof is harder
Client / participant tracking
Med
Component of case mgmt
Evidence-based practice
Low
Credibility anchor
Whole-person / holistic / integrated care
Low
Payer value, moderately provable
Resource directory
Low
Trivially feasible = weak differentiator
SDOH / social determinants
Low
High rhetoric; must back it
Health equity / equity & access
Absent
Easy to claim; this RFP doesn't cue it
Population health
Absent
Payer-facing value; not signaled here

★★★ — Aspirational (Tier 3) — claim carefully, anchor to a pilot
Keyword
RFP cue
Note
Systems change / capacity building
Med
Framing, not a load-bearing promise
Unified strategy
Med
Legit as framing
Value-based care / ROI / cost avoidance
Absent
Lead only with real numbers
Community-owned data / co-design / collective impact
Absent
Include only if solicitation signals it
Clinical-community linkage / care gaps
Absent
Adjacent; not cued, harder to prove
Grant compliance reporting
Absent
Table-stakes for a nonprofit-run CIE

★★ — Gate / conditional (assumed baseline or municipal-only): security & privacy compliance, HIPAA/42 CFR Part 2/FERPA/CJIS, SOC2/Azure/hosting, City-CBO/public-private, civic-tech/municipal.
★ — Filter (not value): all geographic terms.
The two reads that matter
The intersection of value and RFP cue splits your Tier-1 items into two plays:
Aligned leads (value 5 + RFP High): interoperability, integration hub, data sharing, cross-sector collaboration, H&HS integration. The RFP is asking loudly and you should answer loudly.
Under-cued strategic core (value 5 + RFP Low): closed-loop referrals, closure rate, shared intake, care coordination, case management. These are your category-defining strengths, but this Iowa RFP leans toward infrastructure/governance/interop language rather than referral mechanics. So you have to surface them yourself — they won't be rewarded by keyword-matching alone, only by an evaluator who reads for them.
And the mirror image: health equity, population health, value-based care/ROI, community-owned data are Tier 2–3 value but absent from this RFP. Don't anchor the narrative to them here — hold them for a solicitation that signals equity/municipal/payer priorities.
One reconciliation, since it'll look contradictory against turn 2: Referral & Intake got a low point pool there (RFP-fit weighting) but a 5 here (strategic value). Both are right — they're different axes. The operational rubric wants both columns: RFP-fit tells you what this buyer's evaluators will tick; strategic value tells you what to build your win-theme around.



Fix Runtime: 
1. Message Batches API — the biggest win (bypasses rate limits entirely)

Your RFP run is a scheduled/batch job, not an interactive request. The Batches API is not subject to the standard ITPM/RPM limits that are killing you — you submit all 67 evaluations at once, poll until done, and it's 50% cheaper on top. Most batches finish within an hour, often minutes.

This is the clean fix for the 429 problem: no worker tuning, no backoff tuning, no rate-limit ceiling. You'd replace the ThreadPoolExecutor in llm_evaluate() with:
- client.messages.batches.create(requests=[...]) — one Request per RFP, keyed by custom_id
- poll batches.retrieve(id).processing_status until "ended"
- collect via batches.results(id), keyed by custom_id (results come back unordered)

Tradeoff: it's asynchronous (not instant), so it only fits if the RFP run doesn't need to block a user. For a nightly scraper, it's ideal.

2. Prompt caching — cuts latency + token pressure per call

Every one of your 67 calls re-sends the entire company profile, priority themes, mission detail, and the tool schema — all identical. Right now evaluation_prompt_template (config.json:200) bakes the static company context and the volatile RFP into one user prompt, so nothing caches.

Restructure so the stable prefix is cached and only the RFP varies:
- Move the company profile / mission / priority themes into a system block with cache_control: {"type": "ephemeral"}
- Keep only the per-RFP fields (title, description, keyword matches) in the user message, after the cached prefix

Cache reads cost ~0.1× and cut the prefill latency on the repeated prefix. Render order is tools → system → messages, so a cached system block also covers your _EVALUATION_TOOL schema. Note the minimum cacheable prefix on Sonnet is ~1024 tokens — your company profile clears that easily.

3. Incremental evaluation — skip RFPs you've already scored

You already persist composite_score in SQLite. If your sources repeat day-to-day, most of the 67 are RFPs you scored yesterday. Skip any whose source_url is already in the DB → the 67 drops to a handful of genuinely new ones. This is probably your single largest practical runtime cut for repeated runs, and it stacks with everything else.

4. Two-tier Haiku triage (your own Phase 9.2)

claude-haiku-4-5 has a separate rate-limit pool from Sonnet, runs faster, and is ~⅕ the cost. Score all 67 with Haiku, then run the full rich Sonnet evaluation only on the top-K. Fewer Sonnet calls = less pressure on the pool that's actually saturating.

5. Shrink each call

- The template uses the full {description} — cap it (e.g. [:2000]). Long RFP bodies are the biggest variable input.
- Trim template prose. Every token saved raises how many calls fit under ITPM.

6. Rate-aware concurrency (if you stay on the sync API)

If you don't move to Batches, don't fire blind. Set max_workers to match your ITPM headroom (with caching + trimmed prompts, 2–3 is likely safe) or add a token-bucket limiter. The ultimate ceiling-raiser is a higher usage tier on your Anthropic account — worth checking what tier you're on, since that's the actual constant limiting max_workers.

---
My recommendation: combine #3 (incremental) + #1 (Batches) — incremental cuts the count, Batches removes the rate-limit ceiling for whatever's left. Add #2 (caching) and you've cut cost too. That gets you off the 429 treadmill without any worker-count guessing.

### Phase 10 — Runtime Fix: Incremental Evaluation + Message Batches API ✅ DONE

**What changed:**
- **#3 Incremental evaluation:** new `partition_by_cache()` (main.py, in the DB section) splits the keyword-filtered RFP list into `to_evaluate` (never scored before) vs. `cached` (already has a stored evaluation) by looking up each RFP's own `source_url` in SQLite via `_load_cached_evaluations()`. `scrape_all()` is untouched — it always re-parses every source listing page in full on every run, so a brand-new RFP posted on an already-known listing page has a `source_url` that has never been seen before and always falls into `to_evaluate`. Only an RFP whose *exact* URL already has a completed evaluation is skipped. For cached RFPs, the LLM-derived fields (relevance/mission scores, summary, red flags, key requirements, win tip) are pulled from the DB row, but `composite_score` is **recomputed** against the freshly-scraped `keyword_score` so ranking stays correct even if rubric weights changed since the last run.
- **#1 Message Batches API:** `llm_evaluate()` no longer uses a `ThreadPoolExecutor` worker pool. It now builds one `BatchRequest` per RFP (`client.messages.batches.create(...)`), polls `batches.retrieve(id).processing_status` until `"ended"` (interval/timeout configurable), then reads results via `batches.results(id)` keyed by `custom_id` (order is not guaranteed). Batches are not subject to the standard per-minute rate limits, which is what was causing the 429s in Phase 9.1's concurrent-worker approach.
- `run_pipeline()` updated: `filtered → partition_by_cache() → llm_evaluate(to_evaluate) → evaluated = cached + newly_evaluated → eligibility_check(evaluated)`. Cached RFPs flow through eligibility/report exactly like freshly-evaluated ones, so a previously-scored RFP whose deadline has since passed is correctly dropped, and the report always shows the full current set (old + new), not just this run's new evaluations.
- `config.json`: `evaluation.max_workers` / `evaluation.max_retries` (Phase 9.1, now unused) replaced with `evaluation.batch_poll_interval_seconds` (15) and `evaluation.batch_timeout_seconds` (3600).
- Removed `_evaluate_one()` and the `ThreadPoolExecutor`/`as_completed` imports; parsing logic extracted into a shared `_extract_scores_from_message()` helper reused by the batch result loop.
- Verified with a scratch-DB smoke test: seeded one "already evaluated" RFP, ran a simulated fresh scrape containing that same URL plus a brand-new URL — confirmed the old URL was skipped from LLM evaluation (scores reused, composite recomputed off the fresh keyword_score) and the new URL correctly routed to `to_evaluate`.

**Result:** Repeat runs only pay for LLM calls on genuinely new RFPs, and whatever calls remain go through Batches instead of a rate-limited concurrent pool — eliminating the 429s that stalled Phase 9.1 without needing to tune `max_workers` at all.


