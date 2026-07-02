"""
RFP & Grant Opportunity AI Agent
Scrapes, filters, evaluates, stores, and reports on opportunities.
"""

import argparse
import base64
import json
import logging
import os
import re
import sqlite3
import time
import webbrowser
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import anthropic
import requests
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request as BatchRequest
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

def _split_excluded(opportunities: list[dict], config: dict) -> tuple[list[dict], list[dict]]:
    """Split opportunities into (to_score, excluded_records) based on excluded keywords."""
    excluded_kws = [kw.lower() for kw in config.get("keywords", {}).get("excluded", [])]

    to_score: list[dict] = []
    excluded_records: list[dict] = []
    for opp in opportunities:
        text = (opp.get("title", "") + " " + opp.get("description", "")).lower()
        triggered = next((kw for kw in excluded_kws if kw in text), None)
        if triggered:
            excluded_records.append({
                "title": opp.get("title", ""),
                "url": opp.get("source_url", ""),
                "triggered_keyword": triggered,
            })
        else:
            to_score.append(opp)
    return to_score, excluded_records


def keyword_filter(opportunities: list[dict], config: dict) -> list[dict]:
    logger.info("=== STAGE: KEYWORD FILTER ===")

    keywords = config.get("keywords", {})
    required = {
        cat: [kw.lower() for kw in kws]
        for cat, kws in keywords.get("required", {}).items()
    }
    optional = {
        cat: [kw.lower() for kw in kws]
        for cat, kws in keywords.get("optional", {}).items()
    }
    excluded = [kw.lower() for kw in keywords.get("excluded", [])]

    passed: list[dict] = []
    for opp in opportunities:
        text = (opp.get("title", "") + " " + opp.get("description", "")).lower()

        triggered = next((kw for kw in excluded if kw in text), None)
        if triggered:
            logger.info(f"  Dropped (excluded keyword '{triggered}'): {opp.get('title', '')[:50]}")
            continue

        required_matches = {
            cat: {kw: (kw in text) for kw in kws}
            for cat, kws in required.items()
        }
        required_matched = sum(hit for m in required_matches.values() for hit in m.values())
        required_total = sum(len(kws) for kws in required.values())

        optional_matches = {
            cat: [kw for kw in kws if kw in text]
            for cat, kws in optional.items()
        }
        optional_matched = sum(len(kws) for kws in optional_matches.values())
        optional_total = sum(len(kws) for kws in optional.values())

        total = required_total + optional_total
        opp["keyword_matches"] = required_matches
        opp["keyword_score"] = round((required_matched + optional_matched) / total, 3) if total else 0.0
        opp["optional_keyword_matches"] = optional_matches
        opp["optional_keyword_count"] = optional_matched
        opp["optional_keyword_total"] = optional_total
        passed.append(opp)

    passed.sort(key=lambda x: x.get("keyword_score", 0), reverse=True)
    logger.info(
        f"Keyword filter: {len(passed)} passed (excluded {len(opportunities) - len(passed)}), "
        f"sorted by keyword_score descending"
    )
    return passed


# ──────────────────────────────────────────────────────────────
# 4. LLM EVALUATOR
# ──────────────────────────────────────────────────────────────

def _format_keyword_matches_summary(opportunity: dict) -> str:
    lines = []
    for cat, matches in opportunity.get("keyword_matches", {}).items():
        found = [kw for kw, hit in matches.items() if hit]
        lines.append(f"  {cat}: {len(found)}/{len(matches)} matched — {', '.join(found) or 'none'}")
    opt_count = opportunity.get("optional_keyword_count", 0)
    opt_total = opportunity.get("optional_keyword_total", 0)
    if opt_total:
        lines.append(f"  optional keywords: {opt_count}/{opt_total} matched")
    return "\n".join(lines) if lines else "no keyword data"


def _build_evaluation_prompt(template: str, company_profile: dict, opportunity: dict) -> str:
    keyword_matches_summary = _format_keyword_matches_summary(opportunity)
    context = {**company_profile, **opportunity, "keyword_matches_summary": keyword_matches_summary}
    for key, value in context.items():
        if isinstance(value, (list, tuple)):
            value = "; ".join(str(v) for v in value)
        elif isinstance(value, dict):
            continue
        elif not isinstance(value, (str, int, float, bool, type(None))):
            continue
        template = template.replace(f"{{{key}}}", str(value))
    return template


def _parse_llm_response(text: str) -> dict:
    result: dict = {
        "relevance_score": 0,
        "summary": "",
        "red_flags": [],
        "win_likelihood": "low",
        "mission_alignment_score": 0,
        "mission_fit_explanation": "",
        "key_requirements": [],
        "win_tip": "",
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
            result["mission_alignment_score"] = int(parsed.get("mission_alignment_score", 0))
            result["mission_fit_explanation"] = str(parsed.get("mission_fit_explanation", ""))
            result["key_requirements"] = list(parsed.get("key_requirements", []))
            result["win_tip"] = str(parsed.get("win_tip", ""))
            return result
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback: parse line-by-line
    in_requirements = False
    for line in text.splitlines():
        stripped = line.strip()
        if re.search(r"key.?requirements", stripped, re.IGNORECASE):
            in_requirements = True
            continue
        if in_requirements and re.match(r"[-•]\s*", stripped):
            requirement = re.sub(r"^[-•]\s*", "", stripped).strip()
            if requirement:
                result["key_requirements"].append(requirement)
            continue
        in_requirements = False

        if re.search(r"mission.?alignment.?score", stripped, re.IGNORECASE):
            m = re.search(r"(\d+)", stripped)
            if m:
                result["mission_alignment_score"] = min(10, max(1, int(m.group(1))))
        elif re.search(r"relevance.?score", stripped, re.IGNORECASE):
            m = re.search(r"(\d+)", stripped)
            if m:
                result["relevance_score"] = min(10, max(1, int(m.group(1))))
        elif re.match(r"summary\s*[:\-]", stripped, re.IGNORECASE):
            result["summary"] = stripped.split(":", 1)[-1].strip()
        elif re.search(r"mission.?fit.?explanation", stripped, re.IGNORECASE):
            result["mission_fit_explanation"] = stripped.split(":", 1)[-1].strip()
        elif re.search(r"win.?tip", stripped, re.IGNORECASE):
            result["win_tip"] = stripped.split(":", 1)[-1].strip()
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


_DEFAULT_RUBRIC_WEIGHTS = {
    "keyword_score": 0.45,
    "relevance_score": 0.35,
    "mission_alignment_score": 0.2,
}


def _compute_composite_score(opp: dict, weights: dict) -> float:
    """Blend the deterministic keyword_score (Phase 5) with the LLM-derived
    scores into one weighted 0-100 rubric score. keyword_score is not a gate —
    an opportunity with no keyword hits can still rank well on LLM judgment.
    win_likelihood is intentionally excluded (Phase 8 follow-up) — it was too
    speculative to weight alongside the other three signals."""
    keyword_component = opp.get("keyword_score", 0.0) or 0.0
    relevance_component = min(10, max(0, opp.get("relevance_score", 0) or 0)) / 10
    mission_component = min(10, max(0, opp.get("mission_alignment_score", 0) or 0)) / 10

    composite = (
        weights.get("keyword_score", 0) * keyword_component
        + weights.get("relevance_score", 0) * relevance_component
        + weights.get("mission_alignment_score", 0) * mission_component
    )
    return round(composite * 100, 1)


_EVALUATION_TOOL = {
    "name": "evaluate_opportunity",
    "description": "Return a structured evaluation of an RFP or grant opportunity.",
    "input_schema": {
        "type": "object",
        "properties": {
            "relevance_score": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Relevance score from 1 (not relevant) to 10 (perfect fit).",
            },
            "summary": {
                "type": "string",
                "description": "1-2 sentence overview of why this is or isn't a good fit.",
            },
            "red_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concerns such as sole-source requirements, mismatched scope, or unclear budget. Empty array if none.",
            },
            "win_likelihood": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "Estimated probability of winning this opportunity.",
            },
            "mission_alignment_score": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": (
                    "Rating (1-10) of how genuinely this opportunity aligns with the "
                    "company's mission of CBO collaboration, social care infrastructure, "
                    "and health equity — judge real mission fit, not just keyword overlap."
                ),
            },
            "mission_fit_explanation": {
                "type": "string",
                "description": (
                    "1-3 sentence plain-language explanation of how this opportunity does "
                    "or doesn't connect to the company's mission, for display in a "
                    "\"Why this fits\" report section."
                ),
            },
            "key_requirements": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "3-6 short bullet points describing what this RFP/grant is specifically "
                    "looking for — scope of work, deliverables, eligibility requirements, "
                    "or qualifications the funder expects from an applicant."
                ),
            },
            "win_tip": {
                "type": "string",
                "description": (
                    "One concise, specific, actionable tip for how wellConnected could "
                    "strengthen its odds of winning this particular opportunity."
                ),
            },
        },
        "required": [
            "relevance_score",
            "summary",
            "red_flags",
            "win_likelihood",
            "mission_alignment_score",
            "mission_fit_explanation",
            "key_requirements",
            "win_tip",
        ],
    },
}


def _extract_scores_from_message(message) -> dict:
    """Pull the structured evaluation fields out of a completed Messages API
    response, preferring the forced tool_use block and falling back to
    text parsing if the model didn't return one."""
    tool_block = next(
        (block for block in message.content if block.type == "tool_use"),
        None,
    )
    if tool_block:
        inp = tool_block.input
        return {
            "relevance_score": min(10, max(1, int(inp.get("relevance_score", 0)))),
            "summary": str(inp.get("summary", "")),
            "red_flags": list(inp.get("red_flags", [])),
            "win_likelihood": str(inp.get("win_likelihood", "low")).lower(),
            "mission_alignment_score": min(10, max(1, int(inp.get("mission_alignment_score", 0)))),
            "mission_fit_explanation": str(inp.get("mission_fit_explanation", "")),
            "key_requirements": list(inp.get("key_requirements", [])),
            "win_tip": str(inp.get("win_tip", "")),
        }

    response_text = next(
        (block.text for block in message.content if hasattr(block, "text")),
        "",
    )
    return _parse_llm_response(response_text)


def llm_evaluate(opportunities: list[dict], config: dict) -> list[dict]:
    """Evaluate opportunities via the Message Batches API (Phase 9 / #1 runtime
    fix). Batches are not subject to the standard per-minute rate limits that
    made the concurrent ThreadPoolExecutor approach 429 — all RFPs are
    submitted as one batch, processed server-side, and polled to completion."""
    logger.info("=== STAGE: LLM EVALUATION (Message Batches) ===")

    total = len(opportunities)
    if total == 0:
        logger.info("No opportunities to evaluate — skipping batch submission.")
        return []

    eval_cfg = config.get("evaluation", {})
    model = eval_cfg.get("llm_model", "claude-sonnet-4-6")
    min_score = eval_cfg.get("min_relevance_score", 5)
    prompt_template = eval_cfg.get("evaluation_prompt_template", "")
    company_profile = config.get("company_profile", {})
    rubric_weights = eval_cfg.get("rubric_weights", _DEFAULT_RUBRIC_WEIGHTS)
    poll_interval = eval_cfg.get("batch_poll_interval_seconds", 15)
    batch_timeout = eval_cfg.get("batch_timeout_seconds", 3600)

    client = anthropic.Anthropic()

    batch_requests = []
    for i, opp in enumerate(opportunities):
        prompt = _build_evaluation_prompt(prompt_template, company_profile, opp)
        if not prompt.strip():
            prompt = (
                f"Evaluate this opportunity for {company_profile.get('name', 'our company')}.\n\n"
                f"Title: {opp.get('title', '')}\n"
                f"Description: {opp.get('description', '')[:1500]}"
            )
        batch_requests.append(BatchRequest(
            custom_id=f"rfp-{i}",
            params=MessageCreateParamsNonStreaming(
                model=model,
                max_tokens=1024,
                tools=[_EVALUATION_TOOL],
                tool_choice={"type": "tool", "name": "evaluate_opportunity"},
                messages=[{"role": "user", "content": prompt}],
            ),
        ))

    logger.info(f"Submitting batch of {total} RFPs to the Message Batches API")
    batch = client.messages.batches.create(requests=batch_requests)
    logger.info(f"  Batch {batch.id} created — polling every {poll_interval}s (timeout {batch_timeout}s)")

    start = time.monotonic()
    while batch.processing_status != "ended":
        if time.monotonic() - start > batch_timeout:
            logger.error(
                f"  Batch {batch.id} still '{batch.processing_status}' after {batch_timeout}s — "
                f"collecting whatever results are ready and moving on"
            )
            break
        time.sleep(poll_interval)
        batch = client.messages.batches.retrieve(batch.id)
        counts = batch.request_counts
        logger.info(
            f"  Batch {batch.id}: {batch.processing_status} "
            f"(succeeded={counts.succeeded}, errored={counts.errored}, processing={counts.processing})"
        )

    results_by_id = {result.custom_id: result for result in client.messages.batches.results(batch.id)}

    evaluated: list[dict] = []
    for i, opp in enumerate(opportunities):
        result = results_by_id.get(f"rfp-{i}")
        if result is None:
            logger.error(f"  No batch result for '{opp.get('title', '')[:60]}' — dropping")
            continue
        if result.result.type != "succeeded":
            logger.error(f"  Batch eval '{result.result.type}' for '{opp.get('title', '')[:60]}' — dropping")
            continue

        scores = _extract_scores_from_message(result.result.message)
        opp.update(scores)
        opp["composite_score"] = _compute_composite_score(opp, rubric_weights)
        opp["meets_relevance_threshold"] = opp["relevance_score"] >= min_score
        evaluated.append(opp)

    passing = sum(1 for o in evaluated if o.get("meets_relevance_threshold"))
    logger.info(
        f"LLM evaluation: {len(evaluated)}/{total} scored "
        f"({passing} meet the {min_score}+ relevance threshold, all {len(evaluated)} kept for ranking) "
        f"via batch {batch.id}"
    )
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
    status            TEXT DEFAULT 'new',
    mission_alignment_score INTEGER DEFAULT 0,
    mission_fit_explanation TEXT,
    key_requirements  TEXT,
    win_tip           TEXT,
    keyword_score     REAL DEFAULT 0,
    composite_score   REAL DEFAULT 0
)
"""

_INSERT_OPPORTUNITY = """
INSERT OR IGNORE INTO opportunities
    (title, description, source_url, deadline, estimated_value,
     agency_or_funder, source_name, relevance_score, summary,
     red_flags, win_likelihood, sole_source_flag, scraped_at, status,
     mission_alignment_score, mission_fit_explanation, key_requirements, win_tip,
     keyword_score, composite_score)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _init_database(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_TABLE)

    # Migrate older databases created before mission-alignment/win-tip columns existed.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(opportunities)")}
    for col, ddl in (
        ("mission_alignment_score", "ALTER TABLE opportunities ADD COLUMN mission_alignment_score INTEGER DEFAULT 0"),
        ("mission_fit_explanation", "ALTER TABLE opportunities ADD COLUMN mission_fit_explanation TEXT"),
        ("key_requirements", "ALTER TABLE opportunities ADD COLUMN key_requirements TEXT"),
        ("win_tip", "ALTER TABLE opportunities ADD COLUMN win_tip TEXT"),
        ("keyword_score", "ALTER TABLE opportunities ADD COLUMN keyword_score REAL DEFAULT 0"),
        ("composite_score", "ALTER TABLE opportunities ADD COLUMN composite_score REAL DEFAULT 0"),
    ):
        if col not in existing_cols:
            conn.execute(ddl)

    conn.commit()
    return conn


_CACHED_EVAL_COLUMNS = [
    "source_url", "relevance_score", "summary", "red_flags", "win_likelihood",
    "mission_alignment_score", "mission_fit_explanation", "key_requirements", "win_tip",
]


def _load_cached_evaluations(db_path: str, source_urls: list[str]) -> dict[str, dict]:
    """Look up previously-evaluated RFPs by source_url. A row only exists here
    if a prior run actually completed LLM evaluation and saved it (Phase 6/7),
    so presence in the table is a reliable 'already scored' signal."""
    if not source_urls:
        return {}

    conn = _init_database(db_path)
    cached: dict[str, dict] = {}
    try:
        urls = list(dict.fromkeys(source_urls))  # de-dupe, preserve order
        cols = ", ".join(_CACHED_EVAL_COLUMNS)
        # SQLite caps bound parameters (default 999) — chunk the IN clause.
        for i in range(0, len(urls), 500):
            chunk = urls[i:i + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT {cols} FROM opportunities WHERE source_url IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                record = dict(zip(_CACHED_EVAL_COLUMNS, row))
                cached[record["source_url"]] = record
    finally:
        conn.close()
    return cached


def partition_by_cache(opportunities: list[dict], config: dict) -> tuple[list[dict], list[dict]]:
    """Runtime fix #3 (incremental evaluation): split keyword-filtered RFPs into
    (to_evaluate, cached) so a repeat run only spends an LLM call on RFPs that
    are new since the last run.

    scrape_all() always re-parses each source URL's full listing in full on
    every run (unchanged), so a newly-posted RFP always shows up here with its
    own source_url. An opportunity is only treated as 'cached' if THAT exact
    source_url already has a stored evaluation — a brand new posting on an
    already-known listing page has a source_url that has never been seen
    before, so it always falls through to to_evaluate and gets scored by the
    LLM. Nothing is skipped based on the listing page URL, only on the
    individual RFP's own URL.
    """
    db_path = config.get("output", {}).get("database", {}).get("path", "opportunities.db")
    rubric_weights = config.get("evaluation", {}).get("rubric_weights", _DEFAULT_RUBRIC_WEIGHTS)
    min_score = config.get("evaluation", {}).get("min_relevance_score", 5)

    source_urls = [opp.get("source_url", "") for opp in opportunities if opp.get("source_url")]
    cached_rows = _load_cached_evaluations(db_path, source_urls)

    to_evaluate: list[dict] = []
    cached: list[dict] = []
    for opp in opportunities:
        row = cached_rows.get(opp.get("source_url", ""))
        if row is None:
            to_evaluate.append(opp)
            continue

        opp["relevance_score"] = row["relevance_score"] or 0
        opp["summary"] = row["summary"] or ""
        opp["win_likelihood"] = row["win_likelihood"] or "low"
        opp["mission_alignment_score"] = row["mission_alignment_score"] or 0
        opp["mission_fit_explanation"] = row["mission_fit_explanation"] or ""
        opp["win_tip"] = row["win_tip"] or ""
        try:
            opp["red_flags"] = json.loads(row["red_flags"]) if row["red_flags"] else []
        except (json.JSONDecodeError, TypeError):
            opp["red_flags"] = []
        try:
            opp["key_requirements"] = json.loads(row["key_requirements"]) if row["key_requirements"] else []
        except (json.JSONDecodeError, TypeError):
            opp["key_requirements"] = []

        # Recompute off the freshly-scraped keyword_score so ranking stays
        # consistent with current rubric weights even though the LLM
        # sub-scores (relevance/mission fit) are reused from the prior run.
        opp["composite_score"] = _compute_composite_score(opp, rubric_weights)
        opp["meets_relevance_threshold"] = opp["relevance_score"] >= min_score
        cached.append(opp)

    logger.info(
        f"Evaluation cache: {len(cached)} already scored (reused, no LLM call), "
        f"{len(to_evaluate)} new — sending only the new ones to the LLM"
    )
    return to_evaluate, cached


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
                opp.get("mission_alignment_score", 0),
                opp.get("mission_fit_explanation", ""),
                json.dumps(opp.get("key_requirements", [])),
                opp.get("win_tip", ""),
                opp.get("keyword_score", 0.0),
                opp.get("composite_score", 0.0),
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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --navy:#14328f; --navy-deep:#0c2166; --teal:#14b8a6; --teal-deep:#0d9488;
      --teal-bg:#e6f7f4; --amber:#f5a623; --amber-bg:#fdf3df; --red:#e2543d; --red-bg:#fbe9e6;
      --orange:#f6a936; --ink:#16203c; --ink-soft:#5b6478; --paper:#f6f7fb; --line:#e4e7f0;
      --cream:#faf8f3; --blue-bg:#eaf1ff;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--paper); font-family: 'Inter', -apple-system, Segoe UI, Helvetica, Arial, sans-serif; color: var(--ink); }

    .hero { background: linear-gradient(135deg, var(--navy-deep), var(--navy) 60%, #1d4fb0); padding: 30px 40px 34px; color: #fff; }
    .hero-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 26px; flex-wrap: wrap; gap: 12px; }
    .brand img { height: 26px; display: block; }
    .cta { display: flex; gap: 10px; flex-wrap: wrap; }
    .btn { font-family: 'Inter', sans-serif; font-size: 12.5px; font-weight: 700; padding: 10px 18px; border-radius: 999px; cursor: pointer; border: 1px solid transparent; text-decoration: none; display: inline-block; }
    .btn-ghost { background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.35); color: #fff; }
    .btn-ghost:hover { background: rgba(255,255,255,0.2); color: #fff; }
    .btn-primary { background: linear-gradient(135deg, var(--orange), #f7bb5c); color: #3a2405; box-shadow: 0 6px 16px rgba(246,169,54,0.35); }
    .eyebrow { font-size: 11px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: var(--teal); margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
    .eyebrow::before { content: ""; width: 16px; height: 1.5px; background: var(--teal); }
    .hero h1 { font-family: 'Fraunces', Georgia, serif; font-weight: 600; font-size: 34px; margin: 0 0 16px; letter-spacing: -0.3px; }
    .chips { display: flex; gap: 9px; flex-wrap: wrap; }
    .chip { background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.18); padding: 7px 13px; border-radius: 9px; font-size: 12px; }
    .chip b { font-weight: 700; }
    .chip.warn { background: rgba(245,166,35,0.16); border-color: rgba(245,166,35,0.4); color: #ffe6b8; }

    .wrap { max-width: 1040px; margin: 0 auto; padding: 30px 20px 50px; }

    .dossier { background: #fff; border-radius: 16px; box-shadow: 0 10px 30px -14px rgba(20,40,110,0.18); border: 1px solid var(--line); overflow: hidden; margin-bottom: 26px; }
    .dossier-body { display: grid; grid-template-columns: 1fr 290px; }
    .dossier-main { padding: 34px 38px; border-right: 1px solid var(--line); }
    .dossier-rail { background: var(--cream); padding: 30px 26px; position: relative; }
    .dossier.is-complete { opacity: 0.6; }
    .dossier.is-complete .dossier-rail { background: #eef7f5; }
    .dossier.is-in-progress .dossier-rail { background: #fff8ec; }
    .rail-icons { position: absolute; top: 16px; right: 16px; display: flex; gap: 6px; z-index: 3; }
    .icon-btn { background: #fff; border: 1px solid var(--line); color: var(--ink-soft); width: 32px; height: 32px; border-radius: 50%; font-size: 0.95em; cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 0; flex-shrink: 0; }
    .icon-btn:hover { border-color: var(--amber); color: #8a5a10; }
    .icon-btn.pin-icon.pinned { background: var(--amber); border-color: var(--amber); color: #3a2405; }
    .icon-btn.status-icon.in-progress { background: var(--amber); border-color: var(--amber); color: #3a2405; }
    .icon-btn.status-icon.completed { background: var(--teal); border-color: var(--teal-deep); color: #fff; }

    .status-dd { position: relative; }
    .status-menu { display: none; position: absolute; top: 38px; right: 0; background: #fff; border: 1px solid var(--line); border-radius: 10px; box-shadow: 0 8px 20px -6px rgba(20,40,110,0.25); padding: 6px; z-index: 10; min-width: 140px; }
    .status-menu.open { display: block; }
    .status-menu button { display: block; width: 100%; text-align: left; background: none; border: none; padding: 8px 10px; font-family: 'Inter', sans-serif; font-size: 12.5px; font-weight: 600; color: var(--ink); border-radius: 6px; cursor: pointer; }
    .status-menu button:hover { background: var(--paper); }
    .status-menu button.active { color: var(--teal-deep); }
    .status-pill { font-size: 11px; font-weight: 700; padding: 4px 11px; border-radius: 20px; text-transform: uppercase; letter-spacing: 0.3px; white-space: nowrap; }
    .status-pill.in_progress { background: var(--amber-bg); color: #8a5a10; }
    .status-pill.completed { background: var(--teal-bg); color: var(--teal-deep); }
    .status-pill.not_started { background: #f0f0f0; color: #888; }

    .log-form { display: flex; gap: 8px; margin-bottom: 18px; flex-wrap: wrap; }
    .log-input { flex: 1; min-width: 220px; padding: 10px 14px; border: 1px solid var(--line); border-radius: 999px; font-family: 'Inter', sans-serif; font-size: 13px; color: var(--ink); }
    .log-input:focus { outline: none; border-color: var(--teal); }
    .log-form .btn { padding: 10px 20px; font-size: 12.5px; }

    .timeline { margin-top: 6px; }
    .tl-row { display: grid; grid-template-columns: 64px 20px 1fr; gap: 14px; position: relative; padding-bottom: 20px; }
    .tl-row:last-child { padding-bottom: 0; }
    .tl-date { text-align: right; font-size: 11.5px; color: var(--ink-soft); line-height: 1.3; padding-top: 3px; }
    .tl-date .tl-d1 { display: block; font-weight: 600; }
    .tl-date .tl-d2 { display: block; }
    .tl-line { position: relative; display: flex; justify-content: center; }
    .tl-line::before { content: ""; position: absolute; top: 4px; bottom: -20px; width: 2px; background: var(--line); }
    .tl-row:last-child .tl-line::before { display: none; }
    .tl-dot { width: 11px; height: 11px; border-radius: 50%; background: var(--ink-soft); margin-top: 3px; z-index: 2; position: relative; }
    .tl-dot.manual { background: var(--teal); }
    .tl-dot.auto { background: var(--navy); }
    .tl-card { background: #fff; border: 1px solid var(--line); border-radius: 10px; padding: 12px 16px; }
    .tl-badge { display: inline-block; font-size: 10.5px; font-weight: 700; padding: 3px 9px; border-radius: 20px; margin-bottom: 6px; }
    .tl-badge.manual { background: var(--teal-bg); color: var(--teal-deep); }
    .tl-badge.auto { background: var(--blue-bg); color: var(--navy); }
    .tl-note { margin: 0; font-size: 13.5px; color: #39415a; line-height: 1.5; }

    .d-title { font-family: 'Fraunces', Georgia, serif; font-size: 24px; font-weight: 600; line-height: 1.28; margin: 0 0 20px; color: var(--navy-deep); }
    .facts { display: grid; grid-template-columns: 1fr 1fr; gap: 16px 24px; padding: 18px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); margin-bottom: 24px; }
    .fact .k { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--ink-soft); font-weight: 700; margin-bottom: 3px; }
    .fact .v { font-size: 14.5px; font-weight: 600; }
    .sec-h { font-size: 11.5px; font-weight: 800; letter-spacing: 1px; text-transform: uppercase; color: var(--ink-soft); margin: 22px 0 12px; }
    .sec-h:first-of-type { margin-top: 0; }
    .d-summary { font-size: 14.5px; line-height: 1.65; color: #39415a; margin: 0; }
    .looking { list-style: none; padding: 0; margin: 0; }
    .looking li { position: relative; padding: 0 0 0 26px; margin-bottom: 11px; font-size: 14px; line-height: 1.5; color: #39415a; }
    .looking li::before { content: "✓"; position: absolute; left: 0; top: 0; color: var(--teal-deep); font-weight: 800; }
    .callout { border-radius: 12px; padding: 16px 18px; margin: 0 0 14px; font-size: 13.8px; line-height: 1.6; }
    .callout .ct { font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 6px; display: flex; align-items: center; gap: 7px; }
    .c-tip { background: var(--amber-bg); border-left: 3px solid var(--amber); color: #6b4a12; }
    .c-tip .ct { color: #b4790f; }
    .c-fit { background: var(--blue-bg); border-left: 3px solid var(--navy); color: #2b3556; }
    .c-fit .ct { color: var(--navy); }
    .c-flag { background: var(--red-bg); border-left: 3px solid var(--red); color: #7a2c1e; }
    .c-flag .ct { color: var(--red); }
    .c-flag ul { margin: 0; padding-left: 18px; }
    .c-flag li { margin-bottom: 5px; }

    .rail-score { text-align: center; margin-bottom: 20px; }
    .ring { position: relative; width: 120px; height: 120px; margin: 0 auto 10px; }
    .ring svg { transform: rotate(-90deg); width: 100%; height: 100%; }
    .ring-bg { fill: none; stroke: #dfe3ee; stroke-width: 9; }
    .ring-fill { fill: none; stroke: var(--teal); stroke-width: 9; stroke-linecap: round; stroke-dasharray: 339.292; }
    .ring-t { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
    .ring-t .n { font-family: 'Fraunces', Georgia, serif; font-size: 30px; font-weight: 600; color: var(--navy-deep); }
    .ring-t .l { font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px; color: var(--ink-soft); }
    .rail-h { font-size: 11px; font-weight: 800; letter-spacing: 0.8px; text-transform: uppercase; color: var(--ink-soft); margin: 0 0 12px; }
    .mrow { margin-bottom: 10px; }
    .mrow .top { display: flex; justify-content: space-between; font-size: 12px; font-weight: 700; margin-bottom: 5px; color: #39415a; }
    .mrow .top .s { color: var(--teal-deep); }
    .track { height: 6px; background: #dfe3ee; border-radius: 99px; overflow: hidden; }
    .fill { height: 100%; background: linear-gradient(90deg, var(--teal), var(--teal-deep)); border-radius: 99px; }
    .mrow-detail { margin-top: 5px; }
    .mrow-detail summary { cursor: pointer; font-size: 11px; color: var(--ink-soft); font-weight: 600; list-style: none; }
    .mrow-detail summary::-webkit-details-marker { display: none; }
    .mrow-detail summary::before { content: "▸ show matched keywords"; }
    .mrow-detail[open] summary::before { content: "▾ hide matched keywords"; }
    .mrow-kw { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
    .kw-pill { background: var(--teal-bg); color: var(--teal-deep); font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px; }
    .kw-pill.miss { background: #f0f0f0; color: #aaa; }

    .status-summary-dd { margin: 16px 0; border: 1px solid var(--line); border-radius: 10px; padding: 13px 16px; background: var(--paper); }
    .status-summary-dd summary { cursor: pointer; font-size: 12px; font-weight: 700; color: var(--navy); list-style: none; }
    .status-summary-dd summary::-webkit-details-marker { display: none; }
    .status-summary-dd summary::before { content: "▸ Show summary & what they're looking for"; }
    .status-summary-dd[open] summary::before { content: "▾ Hide summary & what they're looking for"; }
    .status-summary-dd[open] summary { margin-bottom: 12px; }
    .status-summary-dd .d-summary { margin-bottom: 10px; }

    .chip.new-badge { background: rgba(20,184,166,0.18); border-color: rgba(20,184,166,0.45); color: #d7fbf4; }

    .rail-meta { border-top: 1px solid var(--line); margin-top: 16px; padding-top: 16px; font-size: 12px; line-height: 1.7; color: var(--ink-soft); }
    .rail-meta b { color: var(--ink); }
    .tagset { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
    .tag { background: var(--teal-bg); color: var(--teal-deep); font-size: 11px; font-weight: 600; padding: 4px 9px; border-radius: 6px; }
    .sole-tag { background: var(--amber-bg); color: #8a5a10; font-weight: 700; padding: 2px 7px; border-radius: 6px; }

    .rail-actions { margin-top: 22px; display: flex; flex-direction: column; gap: 8px; }
    .rail-actions .btn { width: 100%; text-align: center; padding: 12px; font-size: 13.5px; }
    .btn-pin { width: 100%; display: flex; align-items: center; justify-content: center; gap: 6px; padding: 11px; font-size: 13px; background: #fff; border: 1px solid var(--line); color: var(--ink-soft); border-radius: 999px; cursor: pointer; font-family: 'Inter', sans-serif; font-weight: 700; }
    .btn-pin:hover { border-color: var(--amber); color: #8a5a10; }
    .btn-pin.pinned { background: var(--amber); border-color: var(--amber); color: #3a2405; }
    .btn-pin.completed { background: var(--teal); border-color: var(--teal-deep); color: #fff; }

    .no-results { text-align: center; padding: 60px 20px; color: var(--ink-soft); font-size: 1.05em; background: #fff; border-radius: 16px; border: 1px solid var(--line); }

    .tab-nav { display: flex; gap: 8px; margin-bottom: 22px; }
    .tab-btn { font-family: 'Inter', sans-serif; font-size: 13px; font-weight: 700; padding: 10px 20px; border-radius: 999px; cursor: pointer; border: 1px solid var(--line); background: #fff; color: var(--ink-soft); }
    .tab-btn:hover { border-color: var(--navy); color: var(--navy); }
    .tab-btn.active { background: var(--navy); border-color: var(--navy); color: #fff; }

    .footer-bar { background: var(--navy-deep); padding: 18px 32px; display: flex; align-items: center; justify-content: center; gap: 10px; }
    .footer-bar img { height: 18px; }
    .footer-bar span { color: rgba(255,255,255,0.6); font-size: 0.76em; letter-spacing: 0.02em; }

    @media (max-width: 720px) {
      .dossier-body { grid-template-columns: 1fr; }
      .dossier-main { border-right: none; border-bottom: 1px solid var(--line); }
      .facts { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="hero">
    <div class="hero-top">
      <div class="brand">{% if logo_data_uri %}<img src="{{ logo_data_uri }}" alt="wellConnected">{% else %}<strong style="color:#fff;font-family:'Fraunces',serif;font-size:1.2em;">wellConnected</strong>{% endif %}</div>
      <div class="cta">
        <button class="btn btn-ghost" onclick="showPinnedView()">📌 Pinned (<span id="pin-count">0</span>)</button>
        <button class="btn btn-ghost" onclick="showStatusView()">📋 Status (<span id="status-count">0</span>)</button>
      </div>
    </div>
    <div class="eyebrow">Grant Intelligence Report</div>
    <h1>RFP &amp; Grant Opportunities</h1>
    <div class="chips">
      <div class="chip">Generated: <b>{{ report_date }}</b></div>
      <div class="chip">Company: <b>{{ company_name }}</b></div>
      <div class="chip">Matched: <b>{{ current_opportunities | length }}</b></div>
      <div class="chip new-badge">New Match: <b>{{ new_match_opportunities | length }}</b></div>
      <div class="chip">All scored: <b>{{ all_opportunities | length }}</b></div>
    </div>
  </div>

  <div id="main-view" class="wrap">
    <div class="tab-nav">
      <button id="tab-btn-current" class="tab-btn active" onclick="showTab('current')">Matched ({{ current_opportunities | length }})</button>
      <button id="tab-btn-new-match" class="tab-btn" onclick="showTab('new-match')">New Match ({{ new_match_opportunities | length }})</button>
      <button id="tab-btn-all" class="tab-btn" onclick="showTab('all')">All Scored RFPs ({{ all_opportunities | length }})</button>
    </div>

    {% macro render_card(opp) %}
      <div class="dossier">
        <div class="dossier-body">
          <div class="dossier-main">
            <h2 class="d-title">{{ opp.title or "Untitled Opportunity" }}</h2>
            <div class="facts">
              {% if opp.agency_or_funder %}<div class="fact"><div class="k">Agency / Funder</div><div class="v">{{ opp.agency_or_funder }}</div></div>{% endif %}
              {% if opp.deadline %}<div class="fact"><div class="k">Deadline</div><div class="v">{{ opp.deadline }}</div></div>{% endif %}
              {% if opp.estimated_value %}<div class="fact"><div class="k">Est. Value</div><div class="v">{{ opp.estimated_value }}</div></div>{% endif %}
              <div class="fact"><div class="k">Source</div><div class="v">{{ opp.source_name }}</div></div>
            </div>

            {% if opp.summary %}
            <div class="sec-h">Summary</div>
            <p class="d-summary">{{ opp.summary }}</p>
            {% endif %}

            {% if opp.key_requirements %}
            <div class="sec-h">What this RFP is looking for</div>
            <ul class="looking">
              {% for req in opp.key_requirements %}<li>{{ req }}</li>{% endfor %}
            </ul>
            {% endif %}

            {% if opp.win_tip or opp.mission_fit_explanation or opp.red_flags %}<div class="sec-h">&nbsp;</div>{% endif %}
            {% if opp.win_tip %}
            <div class="callout c-tip"><div class="ct">💡 Tip to win this RFP</div>{{ opp.win_tip }}</div>
            {% endif %}
            {% if opp.mission_fit_explanation %}
            <div class="callout c-fit"><div class="ct">◆ Why this fits {{ company_name }}</div>{{ opp.mission_fit_explanation }}</div>
            {% endif %}
            {% if opp.red_flags %}
            <div class="callout c-flag"><div class="ct">⚠ Red flags</div><ul>{% for flag in opp.red_flags %}<li>{{ flag }}</li>{% endfor %}</ul></div>
            {% endif %}
          </div>

          <aside class="dossier-rail">
            <div class="rail-icons">
              <button class="icon-btn pin-icon" data-opp="{{ opp.pin_payload }}" onclick="togglePin(this)" title="Pin this RFP" aria-label="Pin this RFP">📌</button>
              <div class="status-dd">
                <button class="icon-btn status-icon" data-opp="{{ opp.pin_payload }}" onclick="toggleStatusMenu(this)" title="Set status" aria-label="Set status">○</button>
                <div class="status-menu">
                  <button onclick="setStatusFromMenu(this,'not_started')">Not Started</button>
                  <button onclick="setStatusFromMenu(this,'in_progress')">In Progress</button>
                  <button onclick="setStatusFromMenu(this,'completed')">Completed</button>
                </div>
              </div>
            </div>
            <div class="rail-score">
              <div class="ring">
                <svg viewBox="0 0 120 120"><circle class="ring-bg" cx="60" cy="60" r="54"/><circle class="ring-fill" cx="60" cy="60" r="54" style="stroke-dashoffset:{{ opp.score_ring_offset }}"/></svg>
                <div class="ring-t"><div class="n">{{ opp.composite_score | round | int }}</div><div class="l">Composite / 100</div></div>
              </div>
            </div>

            <div class="rail-h">Rubric breakdown</div>
            {% for row in opp.rubric_breakdown %}
            <div class="mrow">
              <div class="top"><span>{{ row.label }}</span><span class="s">{{ row.pct }}%</span></div>
              <div class="track"><div class="fill" style="width:{{ row.pct }}%"></div></div>
            </div>
            {% endfor %}

            {% if opp.keyword_hits %}
            <div class="rail-h">Match breakdown</div>
            {% for cat, info in opp.keyword_hits.items() %}
            <div class="mrow">
              <div class="top"><span>{{ info.label }}</span><span class="s">{{ info.matched | length }}/{{ info.total }}</span></div>
              <div class="track"><div class="fill" style="width:{{ info.pct }}%"></div></div>
              <details class="mrow-detail">
                <summary></summary>
                <div class="mrow-kw">
                  {% if info.matched %}
                    {% for kw in info.matched %}<span class="kw-pill">{{ kw }}</span>{% endfor %}
                  {% else %}
                    <span class="kw-pill miss">no matches</span>
                  {% endif %}
                </div>
              </details>
            </div>
            {% endfor %}
            {% endif %}

            {% if opp.sole_source_flag or opp.optional_matched_flat %}
            <div class="rail-meta">
              {% if opp.sole_source_flag %}<span class="sole-tag">Sole source</span>{% endif %}
              {% if opp.optional_matched_flat %}
              <br><b>Optional matches</b>
              <div class="tagset">{% for kw in opp.optional_matched_flat %}<span class="tag">{{ kw }}</span>{% endfor %}</div>
              {% endif %}
            </div>
            {% endif %}

            <div class="rail-actions">
              <a class="btn btn-primary" href="{{ opp.source_url }}" target="_blank" rel="noopener">View opportunity →</a>
            </div>
          </aside>
        </div>
      </div>
    {% endmacro %}

    <div id="tab-current" class="tab-panel">
      {% if current_opportunities %}
        {% for opp in current_opportunities %}{{ render_card(opp) }}{% endfor %}
      {% else %}
        <div class="no-results">No opportunities meet the {{ min_relevance_score }}/10 relevance threshold this run — check the "All Scored RFPs" tab.</div>
      {% endif %}
    </div>

    <div id="tab-new-match" class="tab-panel" style="display:none;">
      <div id="new-match-empty" class="no-results"{% if new_match_opportunities %} style="display:none;"{% endif %}>No new matches right now — the next run will surface anything freshly posted and relevant that you haven't reviewed yet.</div>
      {% for opp in new_match_opportunities %}{{ render_card(opp) }}{% endfor %}
    </div>

    <div id="tab-all" class="tab-panel" style="display:none;">
      {% if all_opportunities %}
        {% for opp in all_opportunities %}{{ render_card(opp) }}{% endfor %}
      {% else %}
        <div class="no-results">No opportunities were scraped this run.</div>
      {% endif %}
    </div>
  </div>

  <div id="pinned-view" class="wrap" style="display:none;">
    <button class="btn btn-primary" style="margin-bottom:22px;" onclick="showMainView()">← Back to report</button>
    <div id="pinned-empty" class="no-results">No pinned RFPs yet. Click the 📌 icon on any opportunity to save it here.</div>
    <div id="pinned-list"></div>
  </div>

  <div id="status-view" class="wrap" style="display:none;">
    <button class="btn btn-primary" style="margin-bottom:22px;" onclick="showMainView()">← Back to report</button>
    <div id="status-empty" class="no-results">No tracked RFPs yet. Set an RFP's status to "In Progress" or "Completed" to start tracking it here.</div>
    <div id="status-list"></div>
  </div>

  <div class="footer-bar">
    {% if footer_logo_data_uri %}<img src="{{ footer_logo_data_uri }}" alt="wellConnected">{% endif %}
    <span>Powered by wellConnected</span>
  </div>

  <script>
    const PIN_KEY = 'wellconnected_pinned_rfps';
    const STATUS_KEY = 'wellconnected_rfp_status';
    const STATUS_LABELS = { not_started: 'Not Started', in_progress: 'In Progress', completed: 'Completed' };

    // In-memory fallback so pin/status still work for the current page session
    // even if the browser blocks localStorage for local file:// pages (some do).
    let _pinnedCache = null;
    function getPinned() {
      if (_pinnedCache) return _pinnedCache;
      try { _pinnedCache = JSON.parse(localStorage.getItem(PIN_KEY) || '[]'); } catch (e) { _pinnedCache = []; }
      return _pinnedCache;
    }
    function savePinned(list) {
      _pinnedCache = list;
      try { localStorage.setItem(PIN_KEY, JSON.stringify(list)); } catch (e) { /* session-only persistence */ }
    }
    function isPinned(url) {
      return getPinned().some(function (o) { return o.source_url === url; });
    }

    let _statusCache = null;
    function getStatusMap() {
      if (_statusCache) return _statusCache;
      try { _statusCache = JSON.parse(localStorage.getItem(STATUS_KEY) || '{}'); } catch (e) { _statusCache = {}; }
      return _statusCache;
    }
    function saveStatusMap(map) {
      _statusCache = map;
      try { localStorage.setItem(STATUS_KEY, JSON.stringify(map)); } catch (e) { /* session-only persistence */ }
    }
    function getStatusRecord(url) {
      return getStatusMap()[url] || null;
    }

    // Setting status back to "Not Started" clears tracking for that RFP entirely
    // (including its logged history) — treated as a deliberate reset.
    function setStatus(url, opp, status) {
      const map = getStatusMap();
      const existing = map[url];
      const prevStatus = existing ? existing.status : 'not_started';
      if (status === 'not_started') {
        delete map[url];
        saveStatusMap(map);
        return;
      }
      const record = existing || { opp: opp, log: [] };
      record.opp = opp;
      record.status = status;
      if (status !== prevStatus) {
        record.log.push({ note: 'Status changed to ' + STATUS_LABELS[status], at: new Date().toISOString(), auto: true });
      }
      map[url] = record;
      saveStatusMap(map);
    }

    function addStageLog(url, note) {
      const map = getStatusMap();
      const record = map[url];
      if (!record) return;
      record.log.push({ note: note, at: new Date().toISOString(), auto: false });
      map[url] = record;
      saveStatusMap(map);
      renderStatusTab();
    }

    function setPinBtnState(btn, pinned) {
      btn.classList.toggle('pinned', pinned);
      btn.title = pinned ? 'Unpin this RFP' : 'Pin this RFP';
      const label = btn.querySelector('.pin-label');
      if (label) label.textContent = pinned ? 'Pinned' : 'Pin this RFP';
    }

    function statusGlyph(status) {
      if (status === 'in_progress') return '⏳';
      if (status === 'completed') return '✓';
      return '○';
    }

    function updateStatusIcon(btn, status) {
      btn.classList.remove('in-progress', 'completed');
      if (status === 'in_progress') btn.classList.add('in-progress');
      else if (status === 'completed') btn.classList.add('completed');
      btn.textContent = statusGlyph(status);
      btn.title = STATUS_LABELS[status] || STATUS_LABELS.not_started;
    }

    // Cards get tagged with their original position on load so status changes
    // that move them can restore them to where they were instead of leaving
    // them stranded out of order.
    function reinsertInOrder(card) {
      const container = card.parentNode;
      if (!container) return;
      const order = parseInt(card.dataset.order || '0', 10);
      const siblings = Array.from(container.querySelectorAll(':scope > .dossier'));
      let target = null;
      for (const sib of siblings) {
        if (sib === card) continue;
        if (parseInt(sib.dataset.order || '0', 10) > order) { target = sib; break; }
      }
      if (target) container.insertBefore(card, target);
      else container.appendChild(card);
    }

    function togglePin(btn) {
      const opp = JSON.parse(btn.getAttribute('data-opp'));
      const list = getPinned();
      const idx = list.findIndex(function (o) { return o.source_url === opp.source_url; });
      if (idx >= 0) {
        list.splice(idx, 1);
        setPinBtnState(btn, false);
      } else {
        opp.pinned_at = new Date().toISOString();
        list.push(opp);
        setPinBtnState(btn, true);
      }
      savePinned(list);
      renderPinned();
    }

    function unpin(sourceUrl) {
      savePinned(getPinned().filter(function (o) { return o.source_url !== sourceUrl; }));
      renderPinned();
      document.querySelectorAll('.pin-icon[data-opp]').forEach(function (btn) {
        const opp = JSON.parse(btn.getAttribute('data-opp'));
        if (opp.source_url === sourceUrl) setPinBtnState(btn, false);
      });
    }

    function unpinFromCard(btn) {
      if (!confirm('Are you sure you want to unpin this RFP?')) return;
      unpin(btn.getAttribute('data-url'));
    }

    function closeAllStatusMenus() {
      document.querySelectorAll('.status-menu.open').forEach(function (m) { m.classList.remove('open'); });
    }
    document.addEventListener('click', function (e) {
      if (!e.target.closest('.status-dd')) closeAllStatusMenus();
    });

    function toggleStatusMenu(btn) {
      const menu = btn.nextElementSibling;
      const isOpen = menu.classList.contains('open');
      closeAllStatusMenus();
      if (!isOpen) menu.classList.add('open');
    }

    function setStatusFromMenu(menuBtn, status) {
      const dd = menuBtn.closest('.status-dd');
      const icon = dd.querySelector('.status-icon');
      const opp = JSON.parse(icon.getAttribute('data-opp'));
      applyStatus(opp, status);
      closeAllStatusMenus();
    }

    // A New Match RFP counts as "checked out" the moment any status is set on
    // it — unlike Matched/All (where only "completed" dims/reorders the card),
    // New Match removes it outright so the section stays limited to genuinely
    // untouched, freshly-posted matches.
    function refreshNewMatchCount() {
      const panel = document.getElementById('tab-new-match');
      const btn = document.getElementById('tab-btn-new-match');
      if (!panel || !btn) return;
      const visible = Array.from(panel.querySelectorAll(':scope > .dossier')).filter(function (c) {
        return c.style.display !== 'none';
      });
      btn.textContent = 'New Match (' + visible.length + ')';
      const emptyMsg = document.getElementById('new-match-empty');
      if (emptyMsg) emptyMsg.style.display = visible.length ? 'none' : 'block';
    }

    // Applies a status change and syncs every status-icon on the page that refers
    // to this RFP (there can be one in the main list, Pinned tab, New Match tab,
    // and the Status tab).
    function applyStatus(opp, status) {
      setStatus(opp.source_url, opp, status);
      document.querySelectorAll('.status-icon[data-opp]').forEach(function (btn) {
        let btnOpp;
        try { btnOpp = JSON.parse(btn.getAttribute('data-opp')); } catch (e) { return; }
        if (btnOpp.source_url !== opp.source_url) return;
        updateStatusIcon(btn, status);
        const card = btn.closest('.dossier');
        if (card && card.closest('#main-view')) {
          card.classList.toggle('is-complete', status === 'completed');
          card.classList.toggle('is-in-progress', status === 'in_progress');
          if (status === 'completed' && card.parentNode) card.parentNode.appendChild(card);
          else reinsertInOrder(card);
        }
        if (card && card.closest('#tab-new-match')) {
          card.style.display = status === 'not_started' ? '' : 'none';
        }
      });
      renderStatusTab();
      refreshNewMatchCount();
    }

    function submitLog(btn) {
      const wrap = btn.closest('.dossier[data-url]');
      if (!wrap) return;
      const url = wrap.getAttribute('data-url');
      const input = wrap.querySelector('.log-input');
      const note = input.value.trim();
      if (!note) return;
      addStageLog(url, note);
    }

    function escapeHtml(str) {
      return String(str == null ? '' : str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function miniDossierHtml(opp, actionsHtml, statusHtml) {
      let html = '<div class="dossier"><div class="dossier-main" style="border-right:none;">';
      html += '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:8px;">';
      html += '<h2 class="d-title" style="margin:0;">' + escapeHtml(opp.title || 'Untitled Opportunity') + '</h2>';
      html += '<div style="display:flex;gap:8px;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end;align-items:center;">';
      html += '<span class="chip" style="background:var(--teal-bg);color:var(--teal-deep);border:none;">Score: ' + (opp.relevance_score || 0) + '/10</span>';
      if (opp.mission_alignment_score) {
        html += '<span class="chip" style="background:var(--blue-bg);color:var(--navy);border:none;">Fit: ' + opp.mission_alignment_score + '/10</span>';
      }
      if (statusHtml) html += statusHtml;
      html += '</div></div>';
      html += '<div class="facts">';
      if (opp.agency_or_funder) html += '<div class="fact"><div class="k">Agency / Funder</div><div class="v">' + escapeHtml(opp.agency_or_funder) + '</div></div>';
      if (opp.deadline) html += '<div class="fact"><div class="k">Deadline</div><div class="v">' + escapeHtml(opp.deadline) + '</div></div>';
      if (opp.estimated_value) html += '<div class="fact"><div class="k">Est. Value</div><div class="v">' + escapeHtml(opp.estimated_value) + '</div></div>';
      html += '</div>';
      if (opp.summary) html += '<div class="sec-h">Summary</div><p class="d-summary">' + escapeHtml(opp.summary) + '</p>';
      if (opp.key_requirements && opp.key_requirements.length) {
        html += '<div class="sec-h">What this RFP is looking for</div><ul class="looking">';
        opp.key_requirements.forEach(function (req) { html += '<li>' + escapeHtml(req) + '</li>'; });
        html += '</ul>';
      }
      if (opp.win_tip) html += '<div class="callout c-tip"><div class="ct">💡 Tip to win this RFP</div>' + escapeHtml(opp.win_tip) + '</div>';
      if (opp.mission_fit_explanation) html += '<div class="callout c-fit"><div class="ct">◆ Why this fits {{ company_name }}</div>' + escapeHtml(opp.mission_fit_explanation) + '</div>';
      html += '<div style="display:flex;gap:10px;margin-top:20px;flex-wrap:wrap;">' + actionsHtml + '</div>';
      html += '</div></div>';
      return html;
    }

    function pinnedCardHtml(opp) {
      const rec = getStatusRecord(opp.source_url);
      const currentStatus = rec ? rec.status : 'not_started';
      const actions = '<button class="btn-pin pinned" data-url="' + escapeHtml(opp.source_url) + '" onclick="unpinFromCard(this)" title="Unpin this RFP"><span>📌</span><span class="pin-label">Pinned</span></button>' +
        '<a class="btn btn-primary" href="' + escapeHtml(opp.source_url) + '" target="_blank" rel="noopener">View opportunity →</a>';
      // Same status icon + dropdown as the home page cards (statusDropdownHtml),
      // so status can be set directly from the Pinned tab without leaving it.
      return miniDossierHtml(opp, actions, statusDropdownHtml(opp, currentStatus));
    }

    function renderPinned() {
      const list = getPinned();
      const container = document.getElementById('pinned-list');
      const emptyMsg = document.getElementById('pinned-empty');
      container.innerHTML = list.slice().reverse().map(pinnedCardHtml).join('');
      emptyMsg.style.display = list.length ? 'none' : 'block';
      document.getElementById('pin-count').textContent = list.length;
    }

    function statusDropdownHtml(opp, currentStatus) {
      const payload = escapeHtml(JSON.stringify(opp));
      const cls = currentStatus === 'in_progress' ? ' in-progress' : (currentStatus === 'completed' ? ' completed' : '');
      let html = '<div class="status-dd">';
      html += '<button class="icon-btn status-icon' + cls + '" data-opp="' + payload + '" onclick="toggleStatusMenu(this)" title="' + STATUS_LABELS[currentStatus || 'not_started'] + '">' + statusGlyph(currentStatus) + '</button>';
      html += '<div class="status-menu">';
      ['not_started', 'in_progress', 'completed'].forEach(function (s) {
        html += '<button class="' + (s === (currentStatus || 'not_started') ? 'active' : '') + '" onclick="setStatusFromMenu(this,\\'' + s + '\\')">' + STATUS_LABELS[s] + '</button>';
      });
      html += '</div></div>';
      return html;
    }

    function formatTlDate(iso) {
      const d = new Date(iso);
      if (isNaN(d)) return { d1: '', d2: '' };
      return {
        d1: d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
        d2: String(d.getFullYear()),
      };
    }

    function timelineHtml(log) {
      if (!log || !log.length) return '<p style="font-size:12.5px;color:var(--ink-soft);">No updates logged yet.</p>';
      return log.map(function (entry) {
        const dt = formatTlDate(entry.at);
        const cls = entry.auto ? 'auto' : 'manual';
        const badgeLabel = entry.auto ? '● Status Update' : '📝 Progress Note';
        return '<div class="tl-row">' +
          '<div class="tl-date"><span class="tl-d1">' + dt.d1 + '</span><span class="tl-d2">' + dt.d2 + '</span></div>' +
          '<div class="tl-line"><span class="tl-dot ' + cls + '"></span></div>' +
          '<div class="tl-card"><span class="tl-badge ' + cls + '">' + badgeLabel + '</span><p class="tl-note">' + escapeHtml(entry.note) + '</p></div>' +
          '</div>';
      }).join('');
    }

    function statusEntryHtml(url, record) {
      const opp = record.opp;
      let html = '<div class="dossier" data-url="' + escapeHtml(url) + '" style="margin-bottom:26px;">';
      html += '<div class="dossier-main" style="border-right:none;">';
      html += '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:8px;flex-wrap:wrap;">';
      html += '<h2 class="d-title" style="margin:0;">' + escapeHtml(opp.title || 'Untitled Opportunity') + '</h2>';
      html += '<div style="display:flex;gap:8px;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end;align-items:center;">';
      html += '<span class="status-pill ' + record.status + '">' + STATUS_LABELS[record.status] + '</span>';
      html += statusDropdownHtml(opp, record.status);
      html += '</div></div>';
      html += '<div class="facts">';
      if (opp.agency_or_funder) html += '<div class="fact"><div class="k">Agency / Funder</div><div class="v">' + escapeHtml(opp.agency_or_funder) + '</div></div>';
      if (opp.deadline) html += '<div class="fact"><div class="k">Deadline</div><div class="v">' + escapeHtml(opp.deadline) + '</div></div>';
      if (opp.estimated_value) html += '<div class="fact"><div class="k">Est. Value</div><div class="v">' + escapeHtml(opp.estimated_value) + '</div></div>';
      html += '</div>';
      if (opp.summary || (opp.key_requirements && opp.key_requirements.length)) {
        html += '<details class="status-summary-dd"><summary></summary>';
        if (opp.summary) html += '<p class="d-summary">' + escapeHtml(opp.summary) + '</p>';
        if (opp.key_requirements && opp.key_requirements.length) {
          html += '<div class="sec-h" style="margin-top:10px;">What this RFP is looking for</div><ul class="looking">';
          opp.key_requirements.forEach(function (req) { html += '<li>' + escapeHtml(req) + '</li>'; });
          html += '</ul>';
        }
        html += '</details>';
      }
      if (record.status === 'in_progress') {
        html += '<div class="sec-h">Log an update</div>';
        html += '<div class="log-form">';
        html += '<input type="text" class="log-input" placeholder="What stage/process are you on?" onkeydown="if(event.key===\\'Enter\\'){event.preventDefault();this.nextElementSibling.click();}">';
        html += '<button class="btn btn-primary" onclick="submitLog(this)">Log Update</button>';
        html += '</div>';
      }
      html += '<div class="sec-h">Timeline</div>';
      html += '<div class="timeline">' + timelineHtml(record.log) + '</div>';
      html += '<div style="margin-top:18px;"><a class="btn btn-primary" href="' + escapeHtml(opp.source_url) + '" target="_blank" rel="noopener">View opportunity →</a></div>';
      html += '</div></div>';
      return html;
    }

    function renderStatusTab() {
      const map = getStatusMap();
      const urls = Object.keys(map);
      const container = document.getElementById('status-list');
      const emptyMsg = document.getElementById('status-empty');
      urls.sort(function (a, b) {
        const la = map[a].log[map[a].log.length - 1];
        const lb = map[b].log[map[b].log.length - 1];
        return new Date(lb ? lb.at : 0) - new Date(la ? la.at : 0);
      });
      container.innerHTML = urls.map(function (u) { return statusEntryHtml(u, map[u]); }).join('');
      emptyMsg.style.display = urls.length ? 'none' : 'block';
      document.getElementById('status-count').textContent = urls.length;
    }

    function showPinnedView() {
      document.getElementById('main-view').style.display = 'none';
      document.getElementById('status-view').style.display = 'none';
      document.getElementById('pinned-view').style.display = 'block';
    }
    function showStatusView() {
      document.getElementById('main-view').style.display = 'none';
      document.getElementById('pinned-view').style.display = 'none';
      document.getElementById('status-view').style.display = 'block';
    }
    function showMainView() {
      document.getElementById('pinned-view').style.display = 'none';
      document.getElementById('status-view').style.display = 'none';
      document.getElementById('main-view').style.display = 'block';
    }

    function showTab(name) {
      document.getElementById('tab-current').style.display = name === 'current' ? 'block' : 'none';
      document.getElementById('tab-new-match').style.display = name === 'new-match' ? 'block' : 'none';
      document.getElementById('tab-all').style.display = name === 'all' ? 'block' : 'none';
      document.getElementById('tab-btn-current').classList.toggle('active', name === 'current');
      document.getElementById('tab-btn-new-match').classList.toggle('active', name === 'new-match');
      document.getElementById('tab-btn-all').classList.toggle('active', name === 'all');
    }

    document.addEventListener('DOMContentLoaded', function () {
      document.querySelectorAll('#main-view .dossier').forEach(function (card, i) {
        card.dataset.order = i;
      });
      document.querySelectorAll('.pin-icon[data-opp]').forEach(function (btn) {
        const opp = JSON.parse(btn.getAttribute('data-opp'));
        if (isPinned(opp.source_url)) setPinBtnState(btn, true);
      });
      document.querySelectorAll('.status-icon[data-opp]').forEach(function (btn) {
        const opp = JSON.parse(btn.getAttribute('data-opp'));
        const rec = getStatusRecord(opp.source_url);
        const status = rec ? rec.status : 'not_started';
        updateStatusIcon(btn, status);
        const card = btn.closest('.dossier');
        if (status !== 'not_started' && card && card.parentNode) {
          card.classList.toggle('is-complete', status === 'completed');
          card.classList.toggle('is-in-progress', status === 'in_progress');
          if (status === 'completed') card.parentNode.appendChild(card);
        }
        // In case localStorage already tracked this RFP (e.g. from before the
        // report was last regenerated), New Match should honor that immediately
        // rather than waiting for the next status change to hide it.
        if (card && card.closest('#tab-new-match') && status !== 'not_started') {
          card.style.display = 'none';
        }
      });
      renderPinned();
      renderStatusTab();
      refreshNewMatchCount();
    });
  </script>
</body>
</html>
"""


def generate_report(opportunities: list[dict], config: dict) -> str:
    logger.info("=== STAGE: REPORT GENERATION ===")

    report_cfg = config.get("output", {}).get("report", {})
    output_dir = report_cfg.get("output_path", "reports/")
    max_opps = report_cfg.get("max_opportunities_per_report", 50)
    company_name = config.get("company_profile", {}).get("name", "")

    min_relevance_score = config.get("evaluation", {}).get("min_relevance_score", 5)

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Rank by the composite rubric score (Phase 8: keyword_score + relevance_score +
    # mission_alignment_score, weighted per config["evaluation"]["rubric_weights"]).
    # Raw relevance_score is the tiebreaker for opportunities with an identical composite.
    # Precompute over every ranked opportunity once — "Current" and "All" below are
    # just two views (a filtered subset and the full set) over these same objects.
    ranked_opps = sorted(
        opportunities,
        key=lambda x: (x.get("composite_score", 0), x.get("relevance_score", 0)),
        reverse=True,
    )

    for opp in ranked_opps:
        if isinstance(opp.get("red_flags"), str):
            try:
                opp["red_flags"] = json.loads(opp["red_flags"])
            except (json.JSONDecodeError, TypeError):
                opp["red_flags"] = []
        if isinstance(opp.get("key_requirements"), str):
            try:
                opp["key_requirements"] = json.loads(opp["key_requirements"])
            except (json.JSONDecodeError, TypeError):
                opp["key_requirements"] = []

        # Precompute display-only shapes so the template only shows matched keywords.
        opp["keyword_hits"] = {}
        for cat, matches in (opp.get("keyword_matches") or {}).items():
            matched = [kw for kw, hit in matches.items() if hit]
            total = len(matches)
            opp["keyword_hits"][cat] = {
                "matched": matched,
                "total": total,
                "pct": round(100 * len(matched) / total) if total else 0,
                "label": cat.replace("&", " & ").replace("_", " ").title(),
            }
        opp["optional_matched_flat"] = [
            kw for kws in (opp.get("optional_keyword_matches") or {}).values() for kw in kws
        ]

        # Ring-chart stroke-dashoffset for a circle of radius 54 (circumference ≈ 339.292).
        composite = min(100, max(0, opp.get("composite_score", 0) or 0))
        opp["score_ring_offset"] = round(339.292 * (1 - composite / 100), 1)

        # Phase 8 rubric breakdown — the same weighted components that make up
        # composite_score, shown as individual progress rows in the report rail.
        # win_likelihood is deliberately not part of the rubric or this breakdown.
        opp["rubric_breakdown"] = [
            {"label": "Keyword Match", "pct": round((opp.get("keyword_score", 0) or 0) * 100)},
            {"label": "Relevance", "pct": round((opp.get("relevance_score", 0) or 0) * 10)},
            {"label": "Mission Fit", "pct": round((opp.get("mission_alignment_score", 0) or 0) * 10)},
        ]

        # Payload embedded in the pin button so a pinned RFP can be re-rendered client-side
        # (via localStorage) without needing the full pipeline dataset around.
        pin_data = {
            "title": opp.get("title", ""),
            "source_url": opp.get("source_url", ""),
            "agency_or_funder": opp.get("agency_or_funder", ""),
            "deadline": opp.get("deadline", ""),
            "estimated_value": opp.get("estimated_value", ""),
            "source_name": opp.get("source_name", ""),
            "relevance_score": opp.get("relevance_score", 0),
            "mission_alignment_score": opp.get("mission_alignment_score", 0),
            "keyword_score": opp.get("keyword_score", 0),
            "composite_score": opp.get("composite_score", 0),
            "summary": opp.get("summary", ""),
            "mission_fit_explanation": opp.get("mission_fit_explanation", ""),
            "win_tip": opp.get("win_tip", ""),
            "key_requirements": opp.get("key_requirements", []),
        }
        opp["pin_payload"] = _escape_for_html_attr(json.dumps(pin_data))

    # "Matched" = today's stricter view (meets the configured relevance threshold).
    # "All" = every scraped RFP that made it through keyword/eligibility filtering,
    # ranked by the same rubric — nothing is hidden just for scoring low on relevance.
    # "New Match" = the intersection of matched + is_new (never scored before this
    # run — see partition_by_cache()/run_pipeline()). Whether the user has already
    # "checked it out" is tracked client-side (localStorage status), so the JS
    # further hides any of these once a status other than not_started is set.
    current_opps = [o for o in ranked_opps if o.get("meets_relevance_threshold")][:max_opps]
    new_match_opps = [o for o in current_opps if o.get("is_new")]
    all_opps = ranked_opps[:max_opps]

    report_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    filepath = os.path.join(output_dir, "report.html")

    html = Template(_HTML_TEMPLATE).render(
        current_opportunities=current_opps,
        new_match_opportunities=new_match_opps,
        all_opportunities=all_opps,
        min_relevance_score=min_relevance_score,
        report_date=report_date,
        company_name=company_name,
        logo_data_uri=_encode_asset_data_uri("assets/wellconnected-footer.png"),
        footer_logo_data_uri=_encode_asset_data_uri("assets/wellconnected-footer.png"),
    )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(
        f"Report saved: {filepath} "
        f"({len(current_opps)} in Matched, {len(new_match_opps)} in New Match, {len(all_opps)} in All)"
    )
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
# REJECTION DEBUGGER
# ──────────────────────────────────────────────────────────────

_DEBUG_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RFP Keyword Scorer — Debug View</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,500;0,600;1,500&family=Poppins:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --primary: #1648AF;   /* brand navy */
      --hero-blue: #1747B0; /* login-page hero */
      --teal: #1CBBAD;      /* secondary — logo bowl */
      --gold: #FFC252;      /* accent — CTA gold */
      --sky: #1FB8FF;       /* bright accent — checkmarks, borders */
      --sky-light: #47C5FF;
      --error: #FF5722;     /* reserved for rejected / error states */
      --slate: #2C3E50;
      --offwhite: #F9F9F9;
    }
    * { box-sizing: border-box; }
    body { font-family: 'Poppins', -apple-system, Segoe UI, Helvetica, Arial, sans-serif; background: var(--offwhite); color: var(--slate); margin: 0; padding: 0 0 60px; }

    .navbar { background: var(--primary); padding: 14px 32px; display: flex; align-items: center; justify-content: space-between; }
    .navbar-logo { height: 32px; display: block; }
    .navbar-badge { color: var(--sky-light); border: 1px solid rgba(71,197,255,0.55); border-radius: 20px; padding: 5px 16px; font-size: 0.72em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }

    .hero { background: var(--hero-blue); color: #fff; padding: 36px 32px 30px; }
    .hero h1 { font-family: 'Lora', Georgia, serif; font-weight: 500; font-size: 1.9em; margin: 0 0 8px; }
    .hero .subtitle { color: rgba(255,255,255,0.82); font-size: 0.92em; margin: 0; }
    .hero .subtitle strong { color: #fff; }

    .content { max-width: 1500px; margin: 0 auto; padding: 32px 24px 0; }

    h2 { font-family: 'Lora', Georgia, serif; font-weight: 600; color: var(--primary); font-size: 1.15em; margin: 34px 0 14px; }

    .summary-row { display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 12px; }
    .stat { background: #fff; border: 1px solid #e3e6ea; border-radius: 0 28px 0 28px; padding: 18px 26px; text-align: center; min-width: 160px; border-top: 3px solid var(--teal); }
    .stat .num { font-family: 'Lora', Georgia, serif; font-size: 2.1em; font-weight: 600; color: var(--primary); line-height: 1.1; }
    .stat .lbl { font-size: 0.78em; color: #667; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.03em; }

    .excl-table { width: 100%; border-collapse: collapse; font-size: 0.85em; margin-bottom: 36px; background: #fff; }
    .excl-table th { background: var(--error); color: #fff; padding: 8px 12px; text-align: left; font-weight: 600; }
    .excl-table td { padding: 7px 12px; border-bottom: 1px solid #eee; }
    .excl-table tr:nth-child(even) { background: #fff6f3; }

    .table-wrap { overflow-x: auto; border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.84em; background: #fff; }
    thead th { background: var(--primary); color: #fff; padding: 10px 14px; text-align: left; white-space: nowrap; font-weight: 600; }
    tbody tr:nth-child(even) { background: #fafbfd; }
    tbody tr:hover { background: #eef6ff; }
    td { padding: 9px 14px; vertical-align: top; border-bottom: 1px solid #eee; }
    .score-cell { font-weight: 700; font-size: 1.05em; white-space: nowrap; }
    .score-hi { color: #128f83; }
    .score-mid { color: #a67312; }
    .score-lo { color: #aaa; }
    .title-cell { font-weight: 600; max-width: 220px; word-break: break-word; }
    .url-cell { max-width: 140px; word-break: break-all; font-size: 0.8em; }
    .kw-section { }
    .kw-cat-row { display: flex; align-items: flex-start; gap: 8px; margin: 3px 0; flex-wrap: wrap; }
    .kw-cat-label { font-size: 0.72em; font-weight: bold; color: #777; min-width: 160px; flex-shrink: 0; padding-top: 2px; text-transform: uppercase; letter-spacing: 0.03em; }
    .kw-chips { display: flex; flex-wrap: wrap; gap: 3px; }
    .kw-hit { background: #dff6f4; color: #128f83; padding: 1px 6px; border-radius: 4px; font-size: 0.74em; font-weight: 600; }
    .kw-hit::before { content: "✓ "; color: var(--sky); }
    .kw-miss { background: #f0f0f0; color: #bbb; padding: 1px 6px; border-radius: 4px; font-size: 0.74em; }
    .opt-line { font-size: 0.78em; color: #999; margin-top: 4px; }
    a { color: var(--primary); text-decoration: none; }
    a:hover { color: var(--teal); text-decoration: underline; }

    .footer-bar { background: var(--primary); margin-top: 48px; padding: 18px 32px; display: flex; align-items: center; justify-content: center; gap: 10px; }
    .footer-bar img { height: 20px; }
    .footer-bar span { color: rgba(255,255,255,0.75); font-size: 0.78em; letter-spacing: 0.02em; }
  </style>
</head>
<body>
  <div class="navbar">
    {% if logo_data_uri %}<img class="navbar-logo" src="{{ logo_data_uri }}" alt="allco">{% else %}<strong style="color:#fff;font-family:'Lora',serif;font-size:1.3em;">allco</strong>{% endif %}
    <span class="navbar-badge">Internal Tool &middot; Debug View</span>
  </div>

  <div class="hero">
    <h1>RFP Keyword Scorer</h1>
    <p class="subtitle">Generated: <strong>{{ report_date }}</strong> &nbsp;&middot;&nbsp; All non-excluded RFPs ranked by keyword score. Use this to tune keywords in config.json.</p>
  </div>

  <div class="content">
    <div class="summary-row">
      <div class="stat"><div class="num">{{ total_scraped }}</div><div class="lbl">Total Scraped</div></div>
      <div class="stat"><div class="num">{{ excluded_count }}</div><div class="lbl">Excluded (hard reject)</div></div>
      <div class="stat"><div class="num">{{ scored_count }}</div><div class="lbl">Scored &amp; Ranked</div></div>
    </div>

    {% if excluded %}
    <h2>Excluded by Keyword ({{ excluded | length }})</h2>
    <table class="excl-table">
      <thead><tr><th>#</th><th>Title</th><th>Triggered Keyword</th><th>URL</th></tr></thead>
      <tbody>
        {% for r in excluded %}
        <tr>
          <td>{{ loop.index }}</td>
          <td>{{ r.title or "(no title)" }}</td>
          <td><strong>{{ r.triggered_keyword }}</strong></td>
          <td><a href="{{ r.url }}" target="_blank" rel="noopener">{{ r.url[:60] }}{% if r.url | length > 60 %}&hellip;{% endif %}</a></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% endif %}

    <h2>Scored &amp; Ranked ({{ scored | length }}) — highest keyword overlap first</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Rank</th>
            <th>Score</th>
            <th>Title</th>
            <th>URL</th>
            <th>Keyword Matches by Category</th>
          </tr>
        </thead>
        <tbody>
          {% for opp in scored %}
          {% set s = opp.keyword_score | default(0) %}
          <tr>
            <td>{{ loop.index }}</td>
            <td class="score-cell {{ 'score-hi' if s >= 0.1 else ('score-mid' if s >= 0.03 else 'score-lo') }}">{{ "%.3f"|format(s) }}</td>
            <td class="title-cell">{{ opp.title or "(no title)" }}</td>
            <td class="url-cell">{% if opp.source_url %}<a href="{{ opp.source_url }}" target="_blank" rel="noopener">{{ opp.source_url[:55] }}{% if opp.source_url | length > 55 %}&hellip;{% endif %}</a>{% else %}&mdash;{% endif %}</td>
            <td>
              <div class="kw-section">
                {% for cat, matches in opp.keyword_matches.items() %}
                <div class="kw-cat-row">
                  <span class="kw-cat-label">{{ cat }}</span>
                  <div class="kw-chips">
                    {% for kw, hit in matches.items() %}<span class="{{ 'kw-hit' if hit else 'kw-miss' }}">{{ kw }}</span>{% endfor %}
                  </div>
                </div>
                {% endfor %}
                <p class="opt-line">Optional: {{ opp.optional_keyword_count | default(0) }}/{{ opp.optional_keyword_total | default(0) }} matched</p>
              </div>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <div class="footer-bar">
    {% if footer_logo_data_uri %}<img src="{{ footer_logo_data_uri }}" alt="wellConnected">{% endif %}
    <span>Powered by wellConnected</span>
  </div>
</body>
</html>"""


def _escape_for_html_attr(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _encode_asset_data_uri(path: str) -> str:
    asset_path = Path(path)
    if not asset_path.is_file():
        logger.warning(f"Brand asset not found, skipping: {path}")
        return ""
    encoded = base64.b64encode(asset_path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def generate_debug_report(total_scraped: int, scored: list[dict], excluded: list[dict], output_dir: str = "reports/") -> str:
    report_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    filepath = os.path.join(output_dir, "stats.html")

    html = Template(_DEBUG_TEMPLATE).render(
        report_date=report_date,
        total_scraped=total_scraped,
        excluded_count=len(excluded),
        scored_count=len(scored),
        excluded=excluded,
        scored=scored,
        logo_data_uri=_encode_asset_data_uri("assets/logo-reversed.png"),
        footer_logo_data_uri=_encode_asset_data_uri("assets/wellconnected-footer.png"),
    )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"Debug report saved: {filepath} ({len(excluded)} excluded, {len(scored)} scored)")
    return filepath


def run_debug(config: dict) -> None:
    logger.info("=== KEYWORD SCORER DEBUG ===")
    raw = scrape_all(config)

    to_score, excluded_records = _split_excluded(raw, config)
    scored = keyword_filter(to_score, config)

    output_dir = config.get("output", {}).get("report", {}).get("output_path", "reports/")
    filepath = generate_debug_report(len(raw), scored, excluded_records, output_dir)
    webbrowser.open(Path(filepath).resolve().as_uri())


# ──────────────────────────────────────────────────────────────
# PIPELINE
# ──────────────────────────────────────────────────────────────

def run_pipeline(config: dict) -> None:
    logger.info("=" * 50)
    logger.info("PIPELINE START")
    logger.info("=" * 50)
    start = datetime.now()

    raw = scrape_all(config)
    to_score, excluded_records = _split_excluded(raw, config)
    filtered = keyword_filter(to_score, config)

    output_dir = config.get("output", {}).get("report", {}).get("output_path", "reports/")
    # Still written to disk as a diagnostic artifact (and used by `--debug`),
    # just no longer linked from the report UI ("See the stats" tab removed).
    generate_debug_report(len(raw), filtered, excluded_records, output_dir)

    # Runtime fix #3: only send RFPs the LLM hasn't already scored. scrape_all()
    # above always re-parses every listing page in full, so a newly-posted RFP
    # (new source_url) always lands in to_evaluate — see partition_by_cache().
    to_evaluate, cached = partition_by_cache(filtered, config)
    newly_evaluated = llm_evaluate(to_evaluate, config)
    # is_new marks RFPs that were never scored before *this* run — the report's
    # "New Match" section combines this with meets_relevance_threshold (and,
    # client-side, with "not yet reviewed") to surface freshly-posted good fits.
    for opp in newly_evaluated:
        opp["is_new"] = True
    for opp in cached:
        opp["is_new"] = False
    evaluated = cached + newly_evaluated
    eligible = eligibility_check(evaluated, config)
    save_to_database(eligible, config)
    report_path = generate_report(eligible, config)
    send_notifications(eligible, report_path, config)

    elapsed = (datetime.now() - start).seconds
    logger.info("=" * 50)
    logger.info(f"PIPELINE COMPLETE — {len(eligible)} opportunities — {elapsed}s elapsed")
    logger.info("=" * 50)

    webbrowser.open(Path(report_path).resolve().as_uri())


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
    setup_logging()
    load_dotenv()

    parser = argparse.ArgumentParser(description="RFP & Grant Opportunity AI Agent")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Run keyword-filter rejection debugger and output rejection_debug.html (no LLM calls)",
    )
    args = parser.parse_args()

    if not args.debug and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill in your key."
        )

    config = load_config("config.json")
    log_cfg = config.get("logging", {})
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_file=log_cfg.get("file", "agent.log"),
    )

    if args.debug:
        run_debug(config)
    elif config.get("scheduler", {}).get("enabled", False):
        start_scheduler(config)
    else:
        run_pipeline(config)
