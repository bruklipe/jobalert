#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import re
import smtplib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import indeed_job_triage as triage


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
SAM_OPPORTUNITIES_URL = "https://api.sam.gov/opportunities/v2/search"
DEFAULT_ALLOWED_NOTICE_TYPES = (
    "Solicitation",
    "Combined Synopsis/Solicitation",
    "Presolicitation",
    "Sources Sought",
    "Special Notice",
)


@dataclass
class OpportunityRecord:
    source: str
    notice_id: str
    title: str
    organization: str
    office: str
    location: str
    state: str
    url: str
    posted_at: str
    response_deadline: str
    notice_type: str
    base_type: str
    set_aside: str
    naics_code: str
    classification_code: str
    contact_name: str
    contact_email: str
    contact_phone: str
    description: str
    resource_links: List[str]
    metadata: Dict[str, Any]


@dataclass
class RankedOpportunity:
    opportunity: OpportunityRecord
    score: int
    keyword_hits: List[str]
    is_new: bool


@dataclass
class CollectionIssue:
    source: str
    code: str
    message: str
    next_access_time: str = ""


class SamRateLimitError(RuntimeError):
    def __init__(self, message: str, next_access_time: str = "") -> None:
        super().__init__(message)
        self.next_access_time = next_access_time


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def fetch_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            key: value
            for key, value in params.items()
            if value is not None and str(value).strip() != ""
        }
    )
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset, errors="ignore"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore").strip()
        parsed_body: Dict[str, Any] = {}
        if body:
            try:
                parsed_body = json.loads(body)
            except json.JSONDecodeError:
                parsed_body = {}
        if exc.code == 429:
            next_access_time = triage.normalize_whitespace(str(parsed_body.get("nextAccessTime", "")))
            message = triage.normalize_whitespace(str(parsed_body.get("description") or parsed_body.get("message") or "Message throttled out"))
            if next_access_time:
                message = f"{message} Next access time: {next_access_time}."
            raise SamRateLimitError(message, next_access_time=next_access_time) from exc
        detail = body[:300] if body else exc.reason
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {url}: {exc.reason}") from exc


def normalize_list(values: Iterable[Any]) -> List[str]:
    cleaned: List[str] = []
    for value in values:
        text = triage.normalize_whitespace(str(value))
        if not text or text in cleaned:
            continue
        cleaned.append(text)
    return cleaned


def normalize_notice_type(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def keyword_matches(text: str, keyword: str) -> bool:
    normalized_keyword = triage.normalize_whitespace(keyword).lower()
    if not normalized_keyword:
        return False
    pattern = r"(?<!\w)" + re.escape(normalized_keyword).replace(r"\ ", r"\s+") + r"(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def parse_datetime(value: str) -> Optional[dt.datetime]:
    if not value:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    normalized = normalized.replace("Z", "+00:00")
    normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", normalized)
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        pass
    else:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            parsed = dt.datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=dt.timezone.utc)
    return None


def format_datetime_label(value: str) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return triage.normalize_whitespace(str(value))
    return parsed.astimezone(dt.timezone.utc).date().isoformat()


def format_next_access_label(value: str) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return triage.normalize_whitespace(str(value))
    return parsed.astimezone().isoformat(timespec="minutes")


def extract_state(place: Dict[str, Any]) -> str:
    state = place.get("state", "")
    if isinstance(state, dict):
        state = state.get("code") or state.get("name") or ""
    return triage.normalize_whitespace(str(state))


def extract_city(place: Dict[str, Any]) -> str:
    city = place.get("city", "")
    if isinstance(city, dict):
        city = city.get("name") or city.get("code") or ""
    return triage.normalize_whitespace(str(city))


def build_location_label(item: Dict[str, Any]) -> Tuple[str, str]:
    place = item.get("placeOfPerformance") or {}
    office_address = item.get("officeAddress") or {}

    city = extract_city(place) or triage.normalize_whitespace(str(office_address.get("city", "")))
    state = extract_state(place) or triage.normalize_whitespace(str(office_address.get("state", "")))
    zip_code = triage.normalize_whitespace(str(place.get("zip") or office_address.get("zipcode") or ""))

    location_parts = [part for part in (city, state, zip_code) if part]
    if location_parts:
        return ", ".join(location_parts), state

    office = triage.normalize_whitespace(str(item.get("office", "")))
    return office or "Unknown", state


def build_organization_label(item: Dict[str, Any]) -> str:
    candidates = [
        item.get("fullParentPathName", ""),
        item.get("organizationName", ""),
        ".".join(
            [
                triage.normalize_whitespace(str(item.get("department", ""))),
                triage.normalize_whitespace(str(item.get("subTier", ""))),
                triage.normalize_whitespace(str(item.get("office", ""))),
            ]
        ).strip("."),
    ]
    for candidate in candidates:
        normalized = triage.normalize_whitespace(str(candidate))
        if normalized:
            return normalized
    return "Unknown organization"


def extract_contact(item: Dict[str, Any]) -> Tuple[str, str, str]:
    contacts = item.get("pointOfContact") or []
    if isinstance(contacts, dict):
        contacts = [contacts]
    primary = {}
    for contact in contacts:
        if str(contact.get("type", "")).strip().lower() == "primary":
            primary = contact
            break
    if not primary and contacts:
        primary = contacts[0]
    return (
        triage.normalize_whitespace(str(primary.get("fullName", ""))),
        triage.normalize_whitespace(str(primary.get("email", ""))),
        triage.normalize_whitespace(str(primary.get("phone", ""))),
    )


def derive_public_url(item: Dict[str, Any], notice_id: str) -> str:
    ui_link = triage.normalize_whitespace(str(item.get("uiLink", "")))
    if ui_link and ui_link.lower() != "null":
        return ui_link.replace("https://beta.sam.gov/", "https://sam.gov/")
    return f"https://sam.gov/opp/{notice_id}/view"


def age_days(value: str, now: Optional[dt.datetime] = None) -> Optional[float]:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    now_utc = now or dt.datetime.now(dt.timezone.utc)
    delta = now_utc - parsed
    if delta.total_seconds() < 0:
        return 0.0
    return delta.total_seconds() / 86400.0


def stable_opportunity_key(opportunity: OpportunityRecord) -> str:
    if opportunity.notice_id:
        return opportunity.notice_id.lower()
    return opportunity.url.lower()


def build_search_requests(source_cfg: Dict[str, Any], filters_cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    searches = source_cfg.get("searches", [])
    normalized: List[Dict[str, str]] = []
    for search in searches:
        if not isinstance(search, dict):
            continue
        title = triage.normalize_whitespace(str(search.get("title", "")))
        naics_code = triage.normalize_whitespace(str(search.get("naics_code", "")))
        if not title and not naics_code:
            continue
        normalized.append(
            {
                "label": triage.normalize_whitespace(str(search.get("label", title or naics_code))),
                "title": title,
                "naics_code": naics_code,
            }
        )
    if normalized:
        return normalized

    fallback_keywords = normalize_list(filters_cfg.get("include_keywords", []))
    return [{"label": keyword, "title": keyword, "naics_code": ""} for keyword in fallback_keywords]


def dedupe_opportunities(opportunities: List[OpportunityRecord]) -> List[OpportunityRecord]:
    deduped: Dict[str, OpportunityRecord] = {}
    for opportunity in opportunities:
        key = stable_opportunity_key(opportunity)
        if key not in deduped:
            deduped[key] = opportunity
    return list(deduped.values())


def collect_sam_opportunities(source_cfg: Dict[str, Any], filters_cfg: Dict[str, Any]) -> Tuple[List[OpportunityRecord], List[CollectionIssue]]:
    api_key_env = triage.normalize_whitespace(str(source_cfg.get("api_key_env", "PAINTING_WATCH_SAM_API_KEY")))
    api_key = os.getenv(api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(
            f"SAM.gov source is enabled but {api_key_env} is not set. "
            "Create a SAM.gov public API key and add it to your environment or GitHub secret."
        )

    states = normalize_list(source_cfg.get("states", []) or filters_cfg.get("states", []))
    if not states:
        states = [""]
    searches = build_search_requests(source_cfg, filters_cfg)
    max_post_age_days = int(filters_cfg.get("max_post_age_days", 14))
    page_size = int(source_cfg.get("page_size", 100))
    max_pages = int(source_cfg.get("max_pages_per_query", 5))
    posted_from = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_post_age_days)).strftime("%m/%d/%Y")
    posted_to = dt.datetime.now(dt.timezone.utc).strftime("%m/%d/%Y")

    collected: List[OpportunityRecord] = []
    issues: List[CollectionIssue] = []
    for state in states:
        for search in searches:
            offset = 0
            for _ in range(max_pages):
                params = {
                    "api_key": api_key,
                    "postedFrom": posted_from,
                    "postedTo": posted_to,
                    "status": "active",
                    "limit": page_size,
                    "offset": offset,
                    "state": state,
                    "title": search["title"],
                    "ncode": search["naics_code"],
                }
                log(
                    "Collecting SAM.gov opportunities"
                    f" [state={state or 'any'} search={search['label'] or 'default'} offset={offset}]..."
                )
                try:
                    payload = fetch_json(SAM_OPPORTUNITIES_URL, params)
                except SamRateLimitError as exc:
                    next_access_label = format_next_access_label(exc.next_access_time) if exc.next_access_time else ""
                    message = "SAM.gov API quota was exhausted during this run."
                    if next_access_label:
                        message += f" Next access window: {next_access_label}."
                    log(message)
                    issues.append(
                        CollectionIssue(
                            source="sam_gov",
                            code="rate_limited",
                            message=message,
                            next_access_time=exc.next_access_time,
                        )
                    )
                    return dedupe_opportunities(collected), issues
                items = payload.get("opportunitiesData") or []
                if not items:
                    break
                for item in items:
                    notice_id = triage.normalize_whitespace(str(item.get("noticeId", "")))
                    if not notice_id:
                        continue
                    location_label, state_code = build_location_label(item)
                    contact_name, contact_email, contact_phone = extract_contact(item)
                    raw_description = item.get("description", "")
                    description = ""
                    if isinstance(raw_description, str) and raw_description and not raw_description.startswith("http"):
                        description = triage.normalize_whitespace(triage.html_to_text(raw_description))
                    collected.append(
                        OpportunityRecord(
                            source="sam_gov",
                            notice_id=notice_id,
                            title=triage.normalize_whitespace(str(item.get("title", "Unknown title"))),
                            organization=build_organization_label(item),
                            office=triage.normalize_whitespace(str(item.get("office", ""))),
                            location=location_label,
                            state=state_code,
                            url=derive_public_url(item, notice_id),
                            posted_at=triage.normalize_whitespace(str(item.get("postedDate", ""))),
                            response_deadline=triage.normalize_whitespace(str(item.get("responseDeadLine", ""))),
                            notice_type=triage.normalize_whitespace(str(item.get("type", ""))),
                            base_type=triage.normalize_whitespace(str(item.get("baseType", ""))),
                            set_aside=triage.normalize_whitespace(
                                str(
                                    item.get("typeOfSetAsideDescription")
                                    or item.get("setAside")
                                    or item.get("typeOfSetAside")
                                    or ""
                                )
                            ),
                            naics_code=triage.normalize_whitespace(str(item.get("naicsCode", ""))),
                            classification_code=triage.normalize_whitespace(str(item.get("classificationCode", ""))),
                            contact_name=contact_name,
                            contact_email=contact_email,
                            contact_phone=contact_phone,
                            description=description,
                            resource_links=[
                                triage.normalize_whitespace(str(link))
                                for link in item.get("resourceLinks") or []
                                if triage.normalize_whitespace(str(link))
                            ],
                            metadata={
                                "search_label": search["label"],
                                "search_title": search["title"],
                                "search_naics_code": search["naics_code"],
                                "posted_from": posted_from,
                                "posted_to": posted_to,
                                "active": triage.normalize_whitespace(str(item.get("active", ""))),
                                "additional_info_link": triage.normalize_whitespace(str(item.get("additionalInfoLink", ""))),
                                "solicitation_number": triage.normalize_whitespace(str(item.get("solicitationNumber", ""))),
                            },
                        )
                    )
                if len(items) < page_size:
                    break
                offset += page_size
    return dedupe_opportunities(collected), issues


def matches_allowed_notice_type(opportunity: OpportunityRecord, filters_cfg: Dict[str, Any]) -> bool:
    allowed = normalize_list(filters_cfg.get("allowed_notice_types", DEFAULT_ALLOWED_NOTICE_TYPES))
    if not allowed:
        return True
    normalized_allowed = {normalize_notice_type(item) for item in allowed}
    return (
        normalize_notice_type(opportunity.notice_type) in normalized_allowed
        or normalize_notice_type(opportunity.base_type) in normalized_allowed
    )


def score_opportunity(opportunity: OpportunityRecord, filters_cfg: Dict[str, Any]) -> Optional[Tuple[int, List[str]]]:
    if not matches_allowed_notice_type(opportunity, filters_cfg):
        return None

    if age_days(opportunity.posted_at) is not None:
        max_post_age_days = float(filters_cfg.get("max_post_age_days", 14))
        if age_days(opportunity.posted_at) > max_post_age_days:
            return None

    preferred_states = {state.upper() for state in normalize_list(filters_cfg.get("preferred_states", []))}
    preferred_naics = {code for code in normalize_list(filters_cfg.get("preferred_naics_codes", []))}
    exclude_keywords = [keyword.lower() for keyword in normalize_list(filters_cfg.get("exclude_keywords", []))]
    include_keywords = normalize_list(filters_cfg.get("include_keywords", []))

    combined_text = "\n".join(
        [
            opportunity.title,
            opportunity.organization,
            opportunity.office,
            opportunity.location,
            opportunity.notice_type,
            opportunity.base_type,
            opportunity.set_aside,
            opportunity.description,
        ]
    ).lower()

    if exclude_keywords and any(keyword_matches(combined_text, keyword) for keyword in exclude_keywords):
        return None

    keyword_hits = [keyword for keyword in include_keywords if keyword_matches(combined_text, keyword)]
    has_naics_match = opportunity.naics_code in preferred_naics if preferred_naics else False
    if include_keywords or preferred_naics:
        if not keyword_hits and not has_naics_match:
            return None

    score = 0
    if has_naics_match:
        score += 35
    if preferred_states and opportunity.state.upper() in preferred_states:
        score += 20
    if keyword_hits:
        score += min(35, 12 + (len(keyword_hits) - 1) * 6)
    if opportunity.response_deadline:
        score += 5
    if opportunity.set_aside and "small" in opportunity.set_aside.lower():
        score += 5
    return score, keyword_hits


def rank_opportunities(opportunities: List[OpportunityRecord], config: Dict[str, Any], seen_keys: Iterable[str]) -> List[RankedOpportunity]:
    filters_cfg = config.get("filters", {})
    seen = set(seen_keys)
    ranked: List[RankedOpportunity] = []
    for opportunity in opportunities:
        scored = score_opportunity(opportunity, filters_cfg)
        if scored is None:
            continue
        score, keyword_hits = scored
        ranked.append(
            RankedOpportunity(
                opportunity=opportunity,
                score=score,
                keyword_hits=keyword_hits,
                is_new=stable_opportunity_key(opportunity) not in seen,
            )
        )
    ranked.sort(
        key=lambda item: (
            not item.is_new,
            -(parse_datetime(item.opportunity.posted_at).timestamp() if parse_datetime(item.opportunity.posted_at) else float("-inf")),
            -item.score,
            item.opportunity.organization.lower(),
            item.opportunity.title.lower(),
        )
    )
    return ranked


def render_contact(opportunity: OpportunityRecord) -> str:
    parts = [part for part in (opportunity.contact_name, opportunity.contact_email, opportunity.contact_phone) if part]
    return " | ".join(parts) if parts else "Not listed"


def render_resource_links(resource_links: List[str], limit: int = 4) -> str:
    if not resource_links:
        return "None listed"
    visible = resource_links[:limit]
    extra = len(resource_links) - len(visible)
    suffix = f" (+{extra} more)" if extra > 0 else ""
    return "; ".join(visible) + suffix


def build_email_subject(ranked: List[RankedOpportunity], prefix: str, issues: Optional[List[CollectionIssue]] = None) -> str:
    today = dt.datetime.now().strftime("%Y-%m-%d")
    new_count = sum(1 for item in ranked if item.is_new)
    total_count = len(ranked)
    issues = issues or []
    if any(issue.code == "rate_limited" for issue in issues):
        return f"{prefix} {today}: source quota reached"
    if new_count:
        return f"{prefix} {today}: {new_count} new lead{'s' if new_count != 1 else ''}"
    if total_count:
        return f"{prefix} {today}: no new leads, {total_count} recent match{'es' if total_count != 1 else ''}"
    return f"{prefix} {today}: no matches"


def build_summary(ranked: List[RankedOpportunity], config: Dict[str, Any], issues: Optional[List[CollectionIssue]] = None) -> str:
    filters_cfg = config.get("filters", {})
    issues = issues or []
    states = normalize_list(filters_cfg.get("states", [])) or ["any state"]
    max_post_age_days = int(filters_cfg.get("max_post_age_days", 14))
    new_items = [item for item in ranked if item.is_new]
    existing_items = [item for item in ranked if not item.is_new]

    lines = [
        "Daily painting bid watch",
        f"Generated: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}",
        "Source: SAM.gov Contract Opportunities",
        f"Scope: {', '.join(states)} | posted in the last {max_post_age_days} day(s)",
        f"Recent matches reviewed: {len(ranked)}",
        f"New matches since last run: {len(new_items)}",
        "",
    ]

    if issues:
        lines.append("Run notes:")
        for issue in issues:
            lines.append(f"- {issue.message}")
        lines.append("")

    if new_items:
        lines.append("New opportunities:")
        for index, item in enumerate(new_items[:12], start=1):
            opportunity = item.opportunity
            lines.extend(
                [
                    f"{index}. {opportunity.title}",
                    f"   Agency: {opportunity.organization}",
                    f"   Posted: {format_datetime_label(opportunity.posted_at)}",
                    f"   Deadline: {format_datetime_label(opportunity.response_deadline) or 'Not listed'}",
                    f"   Location: {opportunity.location}",
                    f"   Type: {opportunity.notice_type or opportunity.base_type or 'Unknown'}",
                    f"   Set-aside: {opportunity.set_aside or 'None listed'}",
                    f"   NAICS: {opportunity.naics_code or 'Not listed'}",
                    f"   Contact: {render_contact(opportunity)}",
                    f"   Link: {opportunity.url}",
                    "",
                ]
            )
    else:
        if any(issue.code == "rate_limited" for issue in issues):
            lines.append("No new painting-related bid opportunities were reviewed because the SAM.gov API quota was exhausted for this key.")
        else:
            lines.append("No new painting-related bid opportunities matched today.")
        lines.append("")

    if existing_items:
        lines.append("Still-open recent matches already seen:")
        for index, item in enumerate(existing_items[:8], start=1):
            opportunity = item.opportunity
            lines.append(
                f"{index}. {opportunity.title} | {format_datetime_label(opportunity.posted_at)} | "
                f"{opportunity.location} | {opportunity.url}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_report(ranked: List[RankedOpportunity], config: Dict[str, Any], issues: Optional[List[CollectionIssue]] = None) -> str:
    filters_cfg = config.get("filters", {})
    issues = issues or []
    lines = [
        "# Painting Bid Watch",
        "",
        f"- Generated: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}",
        "- Source: SAM.gov Contract Opportunities",
        f"- States: {', '.join(normalize_list(filters_cfg.get('states', [])) or ['any state'])}",
        f"- Max post age: {int(filters_cfg.get('max_post_age_days', 14))} day(s)",
        f"- Matching opportunities: {len(ranked)}",
        f"- New opportunities: {sum(1 for item in ranked if item.is_new)}",
        "",
    ]
    if issues:
        lines.append("## Run Notes")
        lines.append("")
        for issue in issues:
            lines.append(f"- {issue.message}")
        lines.append("")
    if not ranked:
        if any(issue.code == "rate_limited" for issue in issues):
            lines.append("No painting-related opportunities were reviewed because the SAM.gov API quota for this key was temporarily exhausted.")
        else:
            lines.append("No painting-related opportunities matched the current filters.")
        lines.append("")
        return "\n".join(lines)

    for item in ranked:
        opportunity = item.opportunity
        status_tag = "NEW" if item.is_new else "Seen before"
        lines.extend(
            [
                f"## {opportunity.title}",
                "",
                f"- Status: {status_tag}",
                f"- Agency: {opportunity.organization}",
                f"- Office: {opportunity.office or 'Not listed'}",
                f"- Posted: {format_datetime_label(opportunity.posted_at)}",
                f"- Response deadline: {format_datetime_label(opportunity.response_deadline) or 'Not listed'}",
                f"- Place of performance: {opportunity.location}",
                f"- Notice type: {opportunity.notice_type or opportunity.base_type or 'Unknown'}",
                f"- Set-aside: {opportunity.set_aside or 'None listed'}",
                f"- NAICS: {opportunity.naics_code or 'Not listed'}",
                f"- Classification code: {opportunity.classification_code or 'Not listed'}",
                f"- Relevance score: {item.score}",
                f"- Keyword hits: {', '.join(item.keyword_hits) if item.keyword_hits else 'None'}",
                f"- Contact: {render_contact(opportunity)}",
                f"- Resource links: {render_resource_links(opportunity.resource_links)}",
                f"- Opportunity link: {opportunity.url}",
            ]
        )
        additional_info = triage.normalize_whitespace(str(opportunity.metadata.get("additional_info_link", "")))
        if additional_info:
            lines.append(f"- Additional info: {additional_info}")
        solicitation_number = triage.normalize_whitespace(str(opportunity.metadata.get("solicitation_number", "")))
        if solicitation_number:
            lines.append(f"- Solicitation number: {solicitation_number}")
        if opportunity.description:
            lines.extend(
                [
                    "",
                    "### Description",
                    "",
                    opportunity.description,
                ]
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_outputs(
    config: Dict[str, Any],
    config_path: Path,
    opportunities: List[OpportunityRecord],
    ranked: List[RankedOpportunity],
    issues: Optional[List[CollectionIssue]] = None,
) -> None:
    output_cfg = config["output"]
    issues = issues or []
    summary = build_summary(ranked, config, issues=issues)
    report = build_report(ranked, config, issues=issues)

    raw_path = (config_path.parent / output_cfg["raw_path"]).resolve()
    summary_path = (config_path.parent / output_cfg["summary_path"]).resolve()
    report_path = (config_path.parent / output_cfg["report_path"]).resolve()
    json_path = (config_path.parent / output_cfg["json_path"]).resolve()

    triage.ensure_parent(raw_path)
    triage.ensure_parent(summary_path)
    triage.ensure_parent(report_path)
    triage.ensure_parent(json_path)

    raw_payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "opportunities": [asdict(opportunity) for opportunity in opportunities],
        "issues": [asdict(issue) for issue in issues],
    }
    result_payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "matches": len(ranked),
            "new_matches": sum(1 for item in ranked if item.is_new),
        },
        "issues": [asdict(issue) for issue in issues],
        "matches": [
            {
                "score": item.score,
                "keyword_hits": item.keyword_hits,
                "is_new": item.is_new,
                "opportunity": asdict(item.opportunity),
            }
            for item in ranked
        ],
    }

    raw_path.write_text(json.dumps(raw_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    summary_path.write_text(summary, encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    json_path.write_text(json.dumps(result_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def send_email_via_apple_mail(to_addresses: List[str], subject: str, body: str, sender: str = "") -> None:
    script_lines = [
        "on run argv",
        'set theSubject to item 1 of argv',
        'set theBody to item 2 of argv',
        'set theSender to item 3 of argv',
        'tell application "Mail"',
        'set newMessage to make new outgoing message with properties {visible:false, subject:theSubject, content:theBody}',
        'if theSender is not "" then',
        'set sender of newMessage to theSender',
        'end if',
        'repeat with i from 4 to count of argv',
        'tell newMessage',
        'make new to recipient at end of to recipients with properties {address:item i of argv}',
        'end tell',
        'end repeat',
        'send newMessage',
        'end tell',
        'end run',
    ]
    subprocess.run(
        ["osascript", *sum([["-e", line] for line in script_lines], []), subject, body, sender, *to_addresses],
        check=True,
        capture_output=True,
        text=True,
    )


def send_email_via_smtp(
    to_addresses: List[str],
    subject: str,
    body: str,
    sender: str,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    use_ssl: bool = True,
) -> None:
    if not sender:
        raise RuntimeError("SMTP email requires a from_address.")
    if not smtp_host or not smtp_username or not smtp_password:
        raise RuntimeError("SMTP email requires host, username, and password.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(to_addresses)
    message.set_content(body)

    if use_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=60) as server:
            server.login(smtp_username, smtp_password)
            server.send_message(message)
        return

    with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.send_message(message)


def maybe_send_digest_email(ranked: List[RankedOpportunity], config: Dict[str, Any], issues: Optional[List[CollectionIssue]] = None) -> None:
    email_cfg = config.get("email", {})
    if not email_cfg.get("enabled", True):
        return
    issues = issues or []

    recipients = [address.strip() for address in email_cfg.get("to", []) if str(address).strip()]
    if not recipients:
        raise RuntimeError("Email is enabled but no recipient addresses are configured.")

    method = str(
        os.getenv(
            str(email_cfg.get("method_env", "PAINTING_WATCH_EMAIL_METHOD")).strip(),
            email_cfg.get("method", "apple_mail"),
        )
    ).strip().lower()
    sender = str(
        os.getenv(
            str(email_cfg.get("from_address_env", "JOB_WATCH_FROM_ADDRESS")).strip(),
            email_cfg.get("from_address", ""),
        )
    ).strip()
    subject_prefix = str(email_cfg.get("subject_prefix", "Daily painting bid watch")).strip() or "Daily painting bid watch"
    subject = build_email_subject(ranked, subject_prefix, issues=issues)
    body = build_summary(ranked, config, issues=issues)

    if method == "apple_mail":
        send_email_via_apple_mail(recipients, subject, body, sender=sender)
        log(f"Email sent to {', '.join(recipients)}.")
        return

    if method == "smtp":
        smtp_host = str(
            os.getenv(
                str(email_cfg.get("smtp_host_env", "JOB_WATCH_SMTP_HOST")).strip(),
                email_cfg.get("smtp_host", ""),
            )
        ).strip()
        smtp_port_raw = str(
            os.getenv(
                str(email_cfg.get("smtp_port_env", "JOB_WATCH_SMTP_PORT")).strip(),
                email_cfg.get("smtp_port", 465),
            )
        ).strip()
        smtp_username = str(
            os.getenv(
                str(email_cfg.get("smtp_username_env", "JOB_WATCH_SMTP_USERNAME")).strip(),
                email_cfg.get("smtp_username", ""),
            )
        ).strip()
        smtp_password = str(
            os.getenv(
                str(email_cfg.get("smtp_password_env", "JOB_WATCH_SMTP_PASSWORD")).strip(),
                email_cfg.get("smtp_password", ""),
            )
        ).strip()
        use_ssl_raw = str(
            os.getenv(
                str(email_cfg.get("smtp_use_ssl_env", "JOB_WATCH_SMTP_USE_SSL")).strip(),
                email_cfg.get("smtp_use_ssl", True),
            )
        ).strip().lower()
        try:
            smtp_port = int(smtp_port_raw)
        except (TypeError, ValueError):
            smtp_port = 465
        use_ssl = use_ssl_raw not in {"0", "false", "no"}
        send_email_via_smtp(
            recipients,
            subject,
            body,
            sender=sender,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            use_ssl=use_ssl,
        )
        log(f"SMTP email sent to {', '.join(recipients)}.")
        return

    raise RuntimeError(f"Unsupported email method: {method}")


def collect_opportunities(config: Dict[str, Any]) -> Tuple[List[OpportunityRecord], List[CollectionIssue]]:
    opportunities: List[OpportunityRecord] = []
    issues: List[CollectionIssue] = []
    for source_cfg in config.get("sources", []):
        if not source_cfg.get("enabled", True):
            continue
        source_type = str(source_cfg.get("type", "")).strip().lower()
        if source_type != "sam_gov":
            continue
        before = len(opportunities)
        try:
            source_opportunities, source_issues = collect_sam_opportunities(source_cfg, config.get("filters", {}))
            opportunities.extend(source_opportunities)
            issues.extend(source_issues)
        except RuntimeError as exc:
            message = f"{source_cfg.get('name', source_type)} collection error: {exc}"
            log(message)
            issues.append(CollectionIssue(source=source_type, code="collection_error", message=message))
        added = len(opportunities) - before
        log(f"Collected {added} raw opportunity matches from {source_cfg.get('name', source_type)}.")
    return opportunities, issues


def main() -> int:
    default_config_path = Path(__file__).resolve().parent.parent / "config" / "painting_bid_watch.json"
    parser = argparse.ArgumentParser(description="Collect recent painting-related bid opportunities and email a digest.")
    parser.add_argument(
        "--config",
        default=str(default_config_path),
        help="Path to the JSON config file.",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Skip sending the digest email even if email is enabled in config.",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    triage.load_dotenv(
        [
            config_path.parent / ".env",
            config_path.parent / ".env.local",
            config_path.parent.parent / ".env",
            config_path.parent.parent / ".env.local",
        ]
    )
    config = triage.load_config(config_path)

    opportunities, issues = collect_opportunities(config)
    state_path = (config_path.parent / config["output"]["state_path"]).resolve()
    state = triage.load_state(state_path)
    seen_keys = set(state.get("seen_keys", []))

    ranked = rank_opportunities(opportunities, config, seen_keys)
    write_outputs(config, config_path, opportunities, ranked, issues=issues)

    updated_seen = set(seen_keys)
    for opportunity in opportunities:
        updated_seen.add(stable_opportunity_key(opportunity))
    triage.save_state(state_path, updated_seen)

    if not args.no_email:
        maybe_send_digest_email(ranked, config, issues=issues)

    print(f"Collected opportunities: {len(opportunities)}")
    print(f"Matched opportunities: {len(ranked)}")
    if issues:
        print(f"Issues: {len(issues)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
