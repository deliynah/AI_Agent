"""
RFP & Grant Opportunity AI Agent
Scrapes, filters, evaluates, stores, and reports on opportunities.
"""

import json
import logging
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import anthropic
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from jinja2 import Template

# ──────────────────────────────────────────────────────────────
# LOGGING SETUP : Before the AI Agent is even useable, this function captures:
# -runtime errors 
# scraping failures: agent tries to collect data from the interent and something foeswrong (website is down, wrong URL, etc.)
# API + other errors: calling json api instead of a webpage, etc. 
# ──────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO", log_file: str = "agent.log") -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )


logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# 1. LOAD CONFIG
# ──────────────────────────────────────────────────────────────

REQUIRED_TOP_KEYS = [
    "keywords",
    "budget_range",
    "target_agencies",
    "scraping_sources"
]


def load_config(path: str = "config.json") -> dict:
    logger.info(f"Loading config from {path}")
    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)

    missing = [k for k in REQUIRED_TOP_KEYS if k not in config]
    if missing:
        raise ValueError(f"config.json is missing required fields: {missing}")

    logger.info("Config loaded and validated successfully.")
    return config


# ──────────────────────────────────────────────────────────────
# 2. SCRAPER MODULE
# ──────────────────────────────────────────────────────────────

def _build_opportunity(
    title: str,
    description: str,
    link: str,
    pub_date: str,
    source: dict,
) -> dict:
    return {
        "title": title,
        "description": description,
        "source_url": link,
        "deadline": pub_date,
        "estimated_value": "",
        "agency_or_funder": "",
        "source_name": source.get("name", ""),
    }


def scrape_rss(source: dict) -> list[dict]:
    logger.info(f"Scraping RSS: {source['url']}")
    try:
        response = requests.get(source["url"], timeout=30)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        opportunities: list[dict] = []

        channel = root.find("channel")
        if channel is not None:
            for item in channel.findall("item"):
                opportunities.append(_build_opportunity(
                    title=item.findtext("title", default=""),
                    description=item.findtext("description", default=""),
                    link=item.findtext("link", default=""),
                    pub_date=item.findtext("pubDate", default=""),
                    source=source,
                ))
        else:
            # Atom feed fallback
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", ns):
                link_el = entry.find("atom:link", ns)
                href = link_el.get("href", "") if link_el is not None else ""
                opportunities.append(_build_opportunity(
                    title=entry.findtext("atom:title", default="", namespaces=ns),
                    description=(
                        entry.findtext("atom:summary", default="", namespaces=ns)
                        or entry.findtext("atom:content", default="", namespaces=ns)
                    ),
                    link=href,
                    pub_date=entry.findtext("atom:published", default="", namespaces=ns),
                    source=source,
                ))

        logger.info(f"  RSS '{source['name']}': {len(opportunities)} items")
        return opportunities
    except Exception as exc:
        logger.error(f"RSS scrape failed for '{source.get('name')}': {exc}")
        return []


def scrape_api(source: dict) -> list[dict]:
    logger.info(f"Scraping API: {source['url']}")
    try:
        method = source.get("method", "GET").upper()
        headers = source.get("headers", {})
        params = source.get("params", {})
        body = source.get("body", {})

        if method == "POST":
            response = requests.post(source["url"], headers=headers, json=body, timeout=30)
        else:
            response = requests.get(source["url"], headers=headers, params=params, timeout=30)

        response.raise_for_status()
        data = response.json()

        # Drill into a nested key if specified (e.g. "data.results")
        results_key = source.get("results_key", "")
        if results_key:
            for key in results_key.split("."):
                data = data[key]

        if not isinstance(data, list):
            data = [data]

        field_map = source.get("field_map", {})
        opportunities = []
        for item in data:
            opportunities.append({
                "title": item.get(field_map.get("title", "title"), ""),
                "description": item.get(field_map.get("description", "description"), ""),
                "source_url": item.get(field_map.get("link", "link"), source["url"]),
                "deadline": item.get(field_map.get("deadline", "deadline"), ""),
                "estimated_value": item.get(field_map.get("estimated_value", "estimated_value"), ""),
                "agency_or_funder": item.get(field_map.get("agency_or_funder", "agency_or_funder"), ""),
                "source_name": source.get("name", ""),
            })

        logger.info(f"  API '{source['name']}': {len(opportunities)} items")
        return opportunities
    except Exception as exc:
        logger.error(f"API scrape failed for '{source.get('name')}': {exc}")
        return []


def scrape_web(source: dict) -> list[dict]:
    logger.info(f"Scraping web: {source['url']}")
    try:
        headers = source.get("headers", {"User-Agent": "Mozilla/5.0 (compatible; RFPAgent/1.0)"})
        response = requests.get(source["url"], headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        selectors = source.get("selectors", {})
        listing_sel = selectors.get("listing", "article")
        base_url = source["url"]

        def _text(el, sel: str) -> str:
            node = el.select_one(sel) if sel else None
            return node.get_text(strip=True) if node else ""

        def _href(el, sel: str) -> str:
            node = el.select_one(sel) if sel else None
            if node is None:
                return base_url
            href = node.get("href", "")
            return urljoin(base_url, href) if href else base_url

        opportunities = []
        for listing in soup.select(listing_sel):
            title = _text(listing, selectors.get("title", ""))
            description = _text(listing, selectors.get("description", ""))
            link = _href(listing, selectors.get("link", ""))
            if title or description:
                opportunities.append({
                    "title": title,
                    "description": description,
                    "source_url": link,
                    "deadline": _text(listing, selectors.get("deadline", "")),
                    "estimated_value": _text(listing, selectors.get("estimated_value", "")),
                    "agency_or_funder": _text(listing, selectors.get("agency_or_funder", "")),
                    "source_name": source.get("name", ""),
                })

        logger.info(f"  Web '{source['name']}': {len(opportunities)} items")
        return opportunities
    except Exception as exc:
        logger.error(f"Web scrape failed for '{source.get('name')}': {exc}")
        return []


def scrape_all(config: dict) -> list[dict]:
    logger.info("=== STAGE: SCRAPING ===")
    all_opportunities: list[dict] = []

    for source in config.get("scraping_sources", []):
        if not source.get("enabled", True):
            logger.info(f"Skipping disabled source: {source.get('name')}")
            continue

        source_type = source.get("type", "").lower()
        if source_type == "rss":
            results = scrape_rss(source)
        elif source_type == "api":
            results = scrape_api(source)
        elif source_type == "web":
            results = scrape_web(source)
        else:
            logger.warning(f"Unknown source type '{source_type}' for '{source.get('name')}'")
            results = []

        all_opportunities.extend(results)

    logger.info(f"Scraping complete: {len(all_opportunities)} raw opportunities")
    return all_opportunities


# ──────────────────────────────────────────────────────────────
# 3. KEYWORD FILTER
# ──────────────────────────────────────────────────────────────

def keyword_filter(opportunities: list[dict], config: dict) -> list[dict]:
    logger.info("=== STAGE: KEYWORD FILTER ===")

    keywords = config.get("keywords", {})
    required_categories = {
        category: [kw.lower() for kw in kws]
        for category, kws in keywords.get("required", {}).items()
    }
    optional_keywords = [
        kw.lower()
        for kws in keywords.get("optional", {}).values()
        for kw in kws
    ]
    excluded = [kw.lower() for kw in keywords.get("excluded", [])]

    passed: list[dict] = []
    for opp in opportunities:
        text = (opp.get("title", "") + " " + opp.get("description", "")).lower()

        if any(kw in text for kw in excluded):
            logger.info(f"  Dropped (excluded keyword match): {opp.get('title', '')[:50]}")
            continue

        failed_category = next(
            (
                category
                for category, kws in required_categories.items()
                if not any(kw in text for kw in kws)
            ),
            None,
        )
        if failed_category:
            logger.info(
                f"  Dropped (no match in required category '{failed_category}'): "
                f"{opp.get('title', '')[:50]}"
            )
            continue

        optional_matches = sum(1 for kw in optional_keywords if kw in text)
        opp["keyword_score"] = (
            round(optional_matches / len(optional_keywords), 2) if optional_keywords else 0.0
        )
        passed.append(opp)

    logger.info(f"Keyword filter: {len(passed)} passed, {len(opportunities) - len(passed)} filtered out")
    return passed


# ──────────────────────────────────────────────────────────────
# 4. LLM EVALUATOR
# ──────────────────────────────────────────────────────────────

def _build_evaluation_prompt(template: str, company_profile: dict, opportunity: dict) -> str:
    context = {**company_profile, **opportunity}
    for key, value in context.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


def _parse_llm_response(text: str) -> dict:
    result: dict = {
        "relevance_score": 0,
        "summary": "",
        "red_flags": [],
        "win_likelihood": "low",
    }

    # Prefer an explicit JSON code block
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
            result["relevance_score"] = int(parsed.get("relevance_score", 0))
            result["summary"] = str(parsed.get("summary", ""))
            result["red_flags"] = list(parsed.get("red_flags", []))
            result["win_likelihood"] = str(parsed.get("win_likelihood", "low")).lower()
            return result
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback: parse line-by-line
    for line in text.splitlines():
        stripped = line.strip()
        if re.search(r"relevance.?score", stripped, re.IGNORECASE):
            m = re.search(r"(\d+)", stripped)
            if m:
                result["relevance_score"] = min(10, max(1, int(m.group(1))))
        elif re.match(r"summary\s*[:\-]", stripped, re.IGNORECASE):
            result["summary"] = stripped.split(":", 1)[-1].strip()
        elif re.search(r"win.?likelihood", stripped, re.IGNORECASE):
            for level in ("high", "medium", "low"):
                if level in stripped.lower():
                    result["win_likelihood"] = level
                    break
        elif re.search(r"red.?flag", stripped, re.IGNORECASE):
            flag = stripped.split(":", 1)[-1].strip()
            if flag:
                result["red_flags"].append(flag)

    if not result["summary"]:
        result["summary"] = text[:300]

    return result


def llm_evaluate(opportunities: list[dict], config: dict) -> list[dict]:
    logger.info("=== STAGE: LLM EVALUATION ===")

    eval_cfg = config.get("evaluation", {})
    model = eval_cfg.get("llm_model", "claude-sonnet-4-6")
    min_score = eval_cfg.get("min_relevance_score", 5)
    prompt_template = eval_cfg.get("evaluation_prompt_template", "")
    company_profile = config.get("company_profile", {})

    client = anthropic.Anthropic()
    evaluated: list[dict] = []

    for i, opp in enumerate(opportunities, 1):
        logger.info(f"Evaluating {i}/{len(opportunities)}: {opp.get('title', '')[:70]}")
        try:
            prompt = _build_evaluation_prompt(prompt_template, company_profile, opp)
            if not prompt.strip():
                prompt = (
                    f"Evaluate this opportunity for {company_profile.get('name', 'our company')}.\n\n"
                    f"Title: {opp.get('title', '')}\n"
                    f"Description: {opp.get('description', '')[:1500]}\n\n"
                    "Respond with a JSON block containing:\n"
                    "  relevance_score (integer 1-10)\n"
                    "  summary (1-2 sentence overview)\n"
                    "  red_flags (list of strings, empty list if none)\n"
                    "  win_likelihood (\"low\", \"medium\", or \"high\")"
                )

            message = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = message.content[0].text
            scores = _parse_llm_response(response_text)

            if scores["relevance_score"] < min_score:
                logger.info(
                    f"  Dropped — score {scores['relevance_score']} < {min_score}: "
                    f"{opp.get('title', '')[:50]}"
                )
                continue

            opp.update(scores)
            evaluated.append(opp)

        except Exception as exc:
            logger.error(f"LLM evaluation failed for '{opp.get('title', '')}': {exc}")

    logger.info(f"LLM evaluation: {len(evaluated)}/{len(opportunities)} passed min score {min_score}")
    return evaluated


# ──────────────────────────────────────────────────────────────
# 5. ELIGIBILITY CHECK
# ──────────────────────────────────────────────────────────────

_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m/%d/%Y %I:%M %p",
    "%d/%m/%Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S GMT",
]


def _parse_deadline(deadline_str: str) -> datetime | None:
    if not deadline_str:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(deadline_str.strip(), fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def eligibility_check(opportunities: list[dict], config: dict) -> list[dict]:
    logger.info("=== STAGE: ELIGIBILITY CHECK ===")

    eval_cfg = config.get("evaluation", {})
    min_days = eval_cfg.get("min_days_until_deadline", 7)
    max_days = eval_cfg.get("max_days_until_deadline", 365)
    flag_sole = eval_cfg.get("flag_sole_source", True)

    now = datetime.now()
    passed: list[dict] = []

    for opp in opportunities:
        deadline = _parse_deadline(opp.get("deadline", ""))

        if deadline:
            days_until = (deadline - now).days
            if days_until < min_days:
                logger.info(f"  Dropped (deadline in {days_until}d < {min_days}d): {opp.get('title', '')[:50]}")
                continue
            if days_until > max_days:
                logger.info(f"  Dropped (deadline in {days_until}d > {max_days}d): {opp.get('title', '')[:50]}")
                continue

        if flag_sole:
            text = (opp.get("title", "") + " " + opp.get("description", "")).lower()
            if "sole source" in text or "single source" in text:
                opp.setdefault("red_flags", [])
                opp["red_flags"] = list(opp["red_flags"]) + ["Sole/single source mentioned"]
                opp["sole_source_flag"] = True

        opp.setdefault("sole_source_flag", False)
        passed.append(opp)

    logger.info(f"Eligibility check: {len(passed)}/{len(opportunities)} passed")
    return passed


# ──────────────────────────────────────────────────────────────
# 6. DATABASE STORAGE
# ──────────────────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS opportunities (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT,
    description       TEXT,
    source_url        TEXT UNIQUE,
    deadline          TEXT,
    estimated_value   TEXT,
    agency_or_funder  TEXT,
    source_name       TEXT,
    relevance_score   INTEGER DEFAULT 0,
    summary           TEXT,
    red_flags         TEXT,
    win_likelihood    TEXT DEFAULT 'low',
    sole_source_flag  INTEGER DEFAULT 0,
    scraped_at        TEXT,
    status            TEXT DEFAULT 'new'
)
"""

_INSERT_OPPORTUNITY = """
INSERT OR IGNORE INTO opportunities
    (title, description, source_url, deadline, estimated_value,
     agency_or_funder, source_name, relevance_score, summary,
     red_flags, win_likelihood, sole_source_flag, scraped_at, status)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _init_database(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_TABLE)
    conn.commit()
    return conn


def save_to_database(opportunities: list[dict], config: dict) -> int:
    logger.info("=== STAGE: DATABASE STORAGE ===")

    db_path = config.get("output", {}).get("database", {}).get("path", "opportunities.db")
    conn = _init_database(db_path)
    scraped_at = datetime.now().isoformat()
    saved = 0

    for opp in opportunities:
        try:
            conn.execute(_INSERT_OPPORTUNITY, (
                opp.get("title", ""),
                opp.get("description", ""),
                opp.get("source_url", ""),
                opp.get("deadline", ""),
                opp.get("estimated_value", ""),
                opp.get("agency_or_funder", ""),
                opp.get("source_name", ""),
                opp.get("relevance_score", 0),
                opp.get("summary", ""),
                json.dumps(opp.get("red_flags", [])),
                opp.get("win_likelihood", "low"),
                int(bool(opp.get("sole_source_flag", False))),
                scraped_at,
                "new",
            ))
            if conn.execute("SELECT changes()").fetchone()[0]:
                saved += 1
        except sqlite3.Error as exc:
            logger.error(f"DB insert failed for '{opp.get('title', '')}': {exc}")

    conn.commit()
    conn.close()
    logger.info(f"Database: {saved} new records saved → {db_path}")
    return saved


# ──────────────────────────────────────────────────────────────
# 7. REPORT GENERATION
# ──────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RFP &amp; Grant Opportunities — {{ report_date }}</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: Arial, sans-serif; max-width: 1100px; margin: 0 auto; padding: 24px; background: #f4f6f8; color: #222; }
    h1 { color: #1a2638; border-bottom: 3px solid #2e86de; padding-bottom: 10px; margin-bottom: 4px; }
    .meta { color: #666; margin-bottom: 28px; font-size: 0.92em; }
    .card { background: #fff; border-radius: 8px; padding: 20px 24px; margin-bottom: 18px; box-shadow: 0 1px 4px rgba(0,0,0,0.10); }
    .card-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
    .title { font-size: 1.15em; font-weight: bold; color: #1a2638; margin: 0; flex: 1; }
    .score-high   { background: #27ae60; color: #fff; padding: 4px 13px; border-radius: 20px; font-weight: bold; white-space: nowrap; }
    .score-medium { background: #f39c12; color: #fff; padding: 4px 13px; border-radius: 20px; font-weight: bold; white-space: nowrap; }
    .score-low    { background: #e74c3c; color: #fff; padding: 4px 13px; border-radius: 20px; font-weight: bold; white-space: nowrap; }
    .meta-row { display: flex; flex-wrap: wrap; gap: 14px; margin: 10px 0 6px; font-size: 0.88em; color: #555; }
    .meta-row strong { color: #333; }
    .likelihood-high   { display:inline-block; padding:2px 9px; border-radius:12px; font-size:0.85em; font-weight:bold; background:#d5f5e3; color:#27ae60; }
    .likelihood-medium { display:inline-block; padding:2px 9px; border-radius:12px; font-size:0.85em; font-weight:bold; background:#fef9e7; color:#d68910; }
    .likelihood-low    { display:inline-block; padding:2px 9px; border-radius:12px; font-size:0.85em; font-weight:bold; background:#fadbd8; color:#e74c3c; }
    .sole-source { background:#fff3cd; color:#856404; padding:2px 9px; border-radius:12px; font-size:0.82em; font-weight:bold; }
    .summary { color:#444; margin:10px 0 6px; line-height:1.55; font-size:0.95em; }
    .red-flags { background:#fff5f5; border-left:4px solid #e74c3c; padding:8px 12px; border-radius:0 4px 4px 0; margin-top:10px; }
    .red-flags strong { color:#c0392b; }
    .red-flags ul { margin:4px 0 0; padding-left:18px; }
    .red-flags li { font-size:0.9em; color:#555; }
    .view-link { display:inline-block; margin-top:10px; color:#2e86de; font-size:0.9em; text-decoration:none; }
    .view-link:hover { text-decoration:underline; }
    .no-results { text-align:center; padding:60px 20px; color:#999; font-size:1.1em; }
  </style>
</head>
<body>
  <h1>RFP &amp; Grant Opportunities Report</h1>
  <p class="meta">
    Generated: <strong>{{ report_date }}</strong> &nbsp;|&nbsp;
    Company: <strong>{{ company_name }}</strong> &nbsp;|&nbsp;
    Opportunities shown: <strong>{{ opportunities | length }}</strong>
  </p>

  {% if opportunities %}
    {% for opp in opportunities %}
    <div class="card">
      <div class="card-header">
        <p class="title">{{ opp.title or "Untitled Opportunity" }}</p>
        <span class="score-{% if opp.relevance_score >= 8 %}high{% elif opp.relevance_score >= 5 %}medium{% else %}low{% endif %}">
          Score: {{ opp.relevance_score }}/10
        </span>
      </div>

      <div class="meta-row">
        {% if opp.agency_or_funder %}
        <span><strong>Agency/Funder:</strong> {{ opp.agency_or_funder }}</span>
        {% endif %}
        {% if opp.deadline %}
        <span><strong>Deadline:</strong> {{ opp.deadline }}</span>
        {% endif %}
        {% if opp.estimated_value %}
        <span><strong>Est. Value:</strong> {{ opp.estimated_value }}</span>
        {% endif %}
        <span><strong>Source:</strong> {{ opp.source_name }}</span>
        <span>
          <strong>Win Likelihood:</strong>
          <span class="likelihood-{{ opp.win_likelihood }}">{{ opp.win_likelihood | upper }}</span>
        </span>
        {% if opp.sole_source_flag %}
        <span class="sole-source">⚠ Sole Source</span>
        {% endif %}
      </div>

      {% if opp.summary %}
      <p class="summary">{{ opp.summary }}</p>
      {% endif %}

      {% if opp.red_flags %}
      <div class="red-flags">
        <strong>Red Flags:</strong>
        <ul>
          {% for flag in opp.red_flags %}<li>{{ flag }}</li>{% endfor %}
        </ul>
      </div>
      {% endif %}

      <a class="view-link" href="{{ opp.source_url }}" target="_blank" rel="noopener">View Opportunity →</a>
    </div>
    {% endfor %}
  {% else %}
  <div class="no-results">No opportunities met the criteria for this report period.</div>
  {% endif %}
</body>
</html>
"""


def generate_report(opportunities: list[dict], config: dict) -> str:
    logger.info("=== STAGE: REPORT GENERATION ===")

    report_cfg = config.get("output", {}).get("report", {})
    output_dir = report_cfg.get("output_path", "reports/")
    max_opps = report_cfg.get("max_opportunities_per_report", 50)
    company_name = config.get("company_profile", {}).get("name", "")

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    sorted_opps = sorted(
        opportunities, key=lambda x: x.get("relevance_score", 0), reverse=True
    )[:max_opps]

    # Deserialize red_flags that were round-tripped through the DB as JSON strings
    for opp in sorted_opps:
        if isinstance(opp.get("red_flags"), str):
            try:
                opp["red_flags"] = json.loads(opp["red_flags"])
            except (json.JSONDecodeError, TypeError):
                opp["red_flags"] = []

    report_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    datestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"report_{datestamp}.html")

    html = Template(_HTML_TEMPLATE).render(
        opportunities=sorted_opps,
        report_date=report_date,
        company_name=company_name,
    )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"Report saved: {filepath} ({len(sorted_opps)} opportunities)")
    return filepath


# ──────────────────────────────────────────────────────────────
# 8. NOTIFICATIONS  (template — channels not yet implemented)
# ──────────────────────────────────────────────────────────────

def send_notifications(opportunities: list[dict], report_path: str, config: dict) -> None:
    """
    Dispatch notifications about new opportunities.
    Implement the channel helpers below when ready.
    Config section expected: config["notifications"]
    """
    logger.info("=== STAGE: NOTIFICATIONS ===")

    notifications_cfg = config.get("notifications", {})
    if not notifications_cfg.get("enabled", False):
        logger.info("Notifications disabled — skipping.")
        return

    # ── EMAIL ─────────────────────────────────────────────────
    # email_cfg = notifications_cfg.get("email", {})
    # if email_cfg.get("enabled"):
    #     _send_email(opportunities, report_path, email_cfg)

    # ── SLACK ─────────────────────────────────────────────────
    # slack_cfg = notifications_cfg.get("slack", {})
    # if slack_cfg.get("enabled"):
    #     _send_slack(opportunities, report_path, slack_cfg)

    # ── GENERIC WEBHOOK ───────────────────────────────────────
    # webhook_cfg = notifications_cfg.get("webhook", {})
    # if webhook_cfg.get("enabled"):
    #     _send_webhook(opportunities, report_path, webhook_cfg)

    logger.info("Notification template is ready — implement channels above.")


# ──────────────────────────────────────────────────────────────
# PIPELINE
# ──────────────────────────────────────────────────────────────

def run_pipeline(config: dict) -> None:
    logger.info("=" * 50)
    logger.info("PIPELINE START")
    logger.info("=" * 50)
    start = datetime.now()

    raw = scrape_all(config)
    filtered = keyword_filter(raw, config)
    evaluated = llm_evaluate(filtered, config)
    eligible = eligibility_check(evaluated, config)
    save_to_database(eligible, config)
    report_path = generate_report(eligible, config)
    send_notifications(eligible, report_path, config)

    elapsed = (datetime.now() - start).seconds
    logger.info("=" * 50)
    logger.info(f"PIPELINE COMPLETE — {len(eligible)} opportunities — {elapsed}s elapsed")
    logger.info("=" * 50)


# ──────────────────────────────────────────────────────────────
# 9. SCHEDULER
# ──────────────────────────────────────────────────────────────

def start_scheduler(config: dict) -> None:
    scheduler_cfg = config.get("scheduler", {})
    daily_run_time = scheduler_cfg.get("daily_run_time", "08:00")
    run_on_startup = scheduler_cfg.get("run_on_startup", True)

    try:
        hour, minute = map(int, daily_run_time.split(":"))
    except (ValueError, AttributeError):
        logger.warning(f"Invalid daily_run_time '{daily_run_time}' — defaulting to 08:00")
        hour, minute = 8, 0

    if run_on_startup:
        logger.info("run_on_startup=true — running pipeline immediately before scheduling.")
        run_pipeline(config)

    scheduler = BlockingScheduler()
    scheduler.add_job(run_pipeline, "cron", args=[config], hour=hour, minute=minute)
    logger.info(f"Scheduler active — pipeline will run daily at {hour:02d}:{minute:02d}.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped by user.")


# ──────────────────────────────────────────────────────────────
# ENTRYPOINT
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Bootstrap logging before config is available
    setup_logging()

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill in your key."
        )

    config = load_config("config.json")

    # Re-initialise logging with values from config
    log_cfg = config.get("logging", {})
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_file=log_cfg.get("file", "agent.log"),
    )

    if config.get("scheduler", {}).get("enabled", False):
        start_scheduler(config)
    else:
        run_pipeline(config)
