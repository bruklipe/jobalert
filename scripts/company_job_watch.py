#!/usr/bin/env python3
import argparse
import datetime as dt
import html
import json
import re
import os
import smtplib
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.message import EmailMessage
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import indeed_job_triage as triage


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def fetch_text(url: str, method: str = "GET", data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None) -> str:
    merged_headers = {"User-Agent": USER_AGENT}
    if headers:
        merged_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=merged_headers, method=method)
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", errors="ignore")


def post_json(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    text = fetch_text(url, method="POST", data=body, headers={"Content-Type": "application/json"})
    return json.loads(text)


def fetch_jsonp(url: str) -> Dict[str, Any]:
    text = fetch_text(url)
    start = text.find("(")
    end = text.rfind(")")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("JSONP payload was not found.")
    return json.loads(text[start + 1 : end])


def maybe_parse_rss_date(value: str) -> str:
    return triage.maybe_parse_date(value) if value else ""


def clean_text(text: str) -> str:
    return triage.normalize_whitespace(html.unescape(text))


def html_fragment_to_text(text: str) -> str:
    return triage.normalize_whitespace(triage.html_to_text(html.unescape(html.unescape(text))))


def slugify_title(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")
    return value or "job"


def profile_keywords(profile: Dict[str, Any]) -> List[str]:
    return list(dict.fromkeys(profile.get("job_titles", [])))


def profile_locations(profile: Dict[str, Any]) -> List[str]:
    return list(dict.fromkeys(profile.get("locations", [])))


def text_matches_keywords(text: str, keywords: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords if keyword)


def text_matches_locations(text: str, locations: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(location.lower() in lowered for location in locations if location)


def prefilter_job(title: str, location: str, profile: Dict[str, Any], allow_location_miss: bool = False) -> bool:
    joined = f"{title} {location}".strip()
    title_match = text_matches_keywords(joined, profile_keywords(profile))
    location_match = text_matches_locations(joined, profile_locations(profile))
    return title_match and (location_match or allow_location_miss)


def title_is_excluded(title: str, profile: Dict[str, Any]) -> bool:
    exclude_keywords = tuple(profile.get("exclude_keywords", [])) + triage.SENIORITY_EXCLUDES
    return triage.title_has_excluded_seniority(title, exclude_keywords)


def should_collect(title: str, location: str, profile: Dict[str, Any], allow_location_miss: bool = False) -> bool:
    return prefilter_job(title, location, profile, allow_location_miss=allow_location_miss) and not title_is_excluded(title, profile)


def extract_ford_cards(html_text: str) -> List[Dict[str, str]]:
    pattern = re.compile(
        r'<a class="job-list__job-link" href="(?P<href>[^"]+)"[^>]*>\s*(?P<title>[^<]+)\s*</a>'
        r'[\s\S]{0,500}?<li class="job-list__job-info job-location">(?P<location>[^<]+)</li>',
        re.IGNORECASE,
    )
    cards = []
    for match in pattern.finditer(html_text):
        cards.append(
            {
                "href": match.group("href"),
                "title": clean_text(match.group("title")),
                "location": clean_text(match.group("location")),
            }
        )
    return cards


def extract_ford_description(url: str) -> str:
    html_text = fetch_text(url)
    match = re.search(
        r'<div class="ajd-job-details__ats-description ats-description">([\s\S]{0,20000}?)</div>\s*</section>',
        html_text,
        re.IGNORECASE,
    )
    if match:
        return html_fragment_to_text(match.group(1))
    return triage.html_to_text(html_text)


def collect_ford(source: Dict[str, Any], profile: Dict[str, Any]) -> List[triage.JobRecord]:
    jobs: List[triage.JobRecord] = []
    base_url = source["search_url"].rstrip("/")
    states = source.get("states", [])
    keywords = source.get("search_keywords", profile_keywords(profile))
    for keyword in keywords:
        for state in states:
            query = urllib.parse.urlencode({"k": keyword, "State": state})
            url = f"{base_url}?{query}"
            try:
                html_text = fetch_text(url)
            except Exception:
                continue
            for card in extract_ford_cards(html_text):
                if not should_collect(card["title"], card["location"], profile):
                    continue
                job_url = urllib.parse.urljoin(source["careers_url"], card["href"])
                try:
                    description = extract_ford_description(job_url)
                except Exception:
                    description = ""
                jobs.append(
                    triage.JobRecord(
                        source="ford_radancy",
                        source_id=card["href"],
                        title=card["title"],
                        company=source["name"],
                        location=card["location"],
                        url=job_url,
                        description=description,
                        metadata={"search_url": url, "state": state, "keyword": keyword},
                    )
                )
    return jobs


def extract_gm_description(url: str) -> str:
    html_text = fetch_text(url)
    match = re.search(
        r'<meta name="description" property="og:description" content="([^"]+)"',
        html_text,
        re.IGNORECASE,
    )
    if match:
        return clean_text(match.group(1))
    return triage.html_to_text(html_text)


def collect_gm(source: Dict[str, Any], profile: Dict[str, Any]) -> List[triage.JobRecord]:
    jobs: List[triage.JobRecord] = []
    base_url = source["base_url"].rstrip("/")
    endpoint = f"{base_url}/wday/cxs/{source['tenant']}/{source['site']}/jobs"
    keywords = source.get("search_keywords", profile_keywords(profile))
    limit = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages_per_query", 3))
    for keyword in keywords:
        offset = 0
        for _ in range(max_pages):
            payload = {"limit": limit, "offset": offset, "searchText": keyword}
            try:
                data = post_json(endpoint, payload)
            except Exception:
                break
            postings = data.get("jobPostings", [])
            if not postings:
                break
            for posting in postings:
                title = clean_text(posting.get("title", "Unknown title"))
                location = clean_text(posting.get("locationsText", "Unknown"))
                if not should_collect(title, location, profile):
                    continue
                external_path = posting.get("externalPath", "")
                job_url = f"{base_url}/en-US/{source['site']}{external_path}"
                try:
                    description = extract_gm_description(job_url)
                except Exception:
                    description = ""
                jobs.append(
                    triage.JobRecord(
                        source="gm_workday",
                        source_id=external_path or job_url,
                        title=title,
                        company=source["name"],
                        location=location,
                        url=job_url,
                        description=description,
                        metadata={
                            "keyword": keyword,
                            "posted_on": posting.get("postedOn", ""),
                            "remote_type": posting.get("remoteType", ""),
                            "bullet_fields": posting.get("bulletFields", []),
                        },
                    )
                )
            offset += limit
            if len(postings) < limit:
                break
    return jobs


def extract_toyota_cards(html_text: str) -> List[Dict[str, str]]:
    pattern = re.compile(
        r'"jobId":"(?P<job_id>[^"]+)"'
        r'.{0,1200}?"title":"(?P<title>[^"]+)"'
        r'.{0,1200}?"postedDate":"(?P<posted_date>[^"]+)"'
        r'.{0,1200}?"cityStateCountry":"(?P<city_state_country>[^"]+)"'
        r'.{0,1200}?"location":"(?P<location>[^"]+)"'
        r'.{0,5000}?"descriptionTeaser":"(?P<description_teaser>[^"]*)"',
        re.IGNORECASE | re.DOTALL,
    )
    cards = []
    for match in pattern.finditer(html_text):
        cards.append(
            {
                "job_id": match.group("job_id"),
                "title": clean_text(match.group("title")),
                "posted_date": clean_text(match.group("posted_date")),
                "city_state_country": clean_text(match.group("city_state_country")),
                "location": clean_text(match.group("location")),
                "description_teaser": html_fragment_to_text(match.group("description_teaser")),
            }
        )
    return cards


def extract_toyota_description(url: str, teaser: str) -> str:
    try:
        raw_html = fetch_text(url)
    except Exception:
        return teaser
    if "position to which you applied has closed" in raw_html.lower():
        return teaser
    decoded = html.unescape(html.unescape(raw_html))
    text = triage.html_to_text(decoded)
    text = triage.normalize_whitespace(text)
    if teaser and teaser.lower() not in text.lower():
        text = f"{teaser} {text}"
    return text


def collect_toyota(source: Dict[str, Any], profile: Dict[str, Any]) -> List[triage.JobRecord]:
    jobs: List[triage.JobRecord] = []
    base_url = source["search_url"].rstrip("/")
    max_pages = int(source.get("max_pages", 10))
    page_size = int(source.get("page_size", 10))
    seen_ids = set()
    for page_index in range(max_pages):
        if page_index == 0:
            url = base_url
        else:
            query = urllib.parse.urlencode({"from": page_index * page_size, "s": 1})
            url = f"{base_url}?{query}"
        try:
            html_text = fetch_text(url)
        except Exception:
            break
        cards = extract_toyota_cards(html_text)
        if not cards:
            break
        page_added = 0
        for card in cards:
            if card["job_id"] in seen_ids:
                continue
            seen_ids.add(card["job_id"])
            if not should_collect(card["title"], card["location"], profile):
                continue
            detail_url = f"{source['detail_base_url'].rstrip('/')}/job/{card['job_id']}/{urllib.parse.quote(slugify_title(card['title']))}"
            description = extract_toyota_description(detail_url, card["description_teaser"])
            jobs.append(
                triage.JobRecord(
                    source="toyota_phenom",
                    source_id=card["job_id"],
                    title=card["title"],
                    company=source["name"],
                    location=card["location"],
                    url=detail_url,
                    description=description,
                        metadata={
                            "search_url": url,
                            "posted_date": card["posted_date"],
                            "city_state_country": card["city_state_country"],
                            "description_teaser": card["description_teaser"],
                        },
                )
            )
            page_added += 1
        if page_added == 0 and page_index > 0:
            break
    return jobs


def collect_keyence(source: Dict[str, Any], profile: Dict[str, Any]) -> List[triage.JobRecord]:
    jobs: List[triage.JobRecord] = []
    xml_text = fetch_text(source["rss_url"])
    root = ET.fromstring(xml_text)
    for item in root.findall("./channel/item"):
        title = clean_text(item.findtext("title", "Unknown title"))
        url = clean_text(item.findtext("link", ""))
        description_html = item.findtext("description", "")
        description = html_fragment_to_text(description_html)
        location_match = re.search(r"\(([^)]+)\)", title)
        location = clean_text(location_match.group(1)) if location_match else "Unknown"
        if not should_collect(title, location, profile, allow_location_miss=True):
            continue
        jobs.append(
            triage.JobRecord(
                source="keyence_rss",
                source_id=url or title,
                title=title,
                company=source["name"],
                location=location,
                url=url,
                description=description,
                metadata={"rss_url": source["rss_url"], "pub_date": maybe_parse_rss_date(item.findtext("pubDate", ""))},
            )
        )
    return jobs


def collect_stellantis(source: Dict[str, Any], profile: Dict[str, Any]) -> List[triage.JobRecord]:
    jobs: List[triage.JobRecord] = []
    seen_ids = set()
    endpoint = source["api_url"].rstrip("/") + "/job"
    keywords = source.get("search_keywords", profile_keywords(profile))
    limit = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages_per_query", 3))
    organization = str(source["organization"])
    detail_base_url = source["detail_base_url"].rstrip("/")

    for keyword in keywords:
        offset = 1
        for _ in range(max_pages):
            query = urllib.parse.urlencode(
                {
                    "Organization": organization,
                    "Limit": limit,
                    "offset": offset,
                    "SearchText": keyword,
                    "callback": "CWS.jobs.jobCallback",
                }
            )
            url = f"{endpoint}?{query}"
            try:
                data = fetch_jsonp(url)
            except Exception:
                break
            postings = data.get("queryResult", [])
            if not postings:
                break
            page_added = 0
            for posting in postings:
                posting_id = str(posting.get("id", "")).strip()
                if not posting_id or posting_id in seen_ids:
                    continue
                seen_ids.add(posting_id)
                title = clean_text(posting.get("title", "Unknown title"))
                location_parts = [
                    clean_text(posting.get("primary_city", "")),
                    clean_text(posting.get("primary_state", "")),
                    clean_text(posting.get("primary_country", "")),
                ]
                location = clean_text(", ".join(part for part in location_parts if part))
                if not should_collect(title, location, profile):
                    continue
                description = html_fragment_to_text(posting.get("description", ""))
                job_url = f"{detail_base_url}/job/{posting_id}/{urllib.parse.quote(slugify_title(title))}"
                jobs.append(
                    triage.JobRecord(
                        source="stellantis_cws",
                        source_id=posting_id,
                        title=title,
                        company=source["name"],
                        location=location or "Unknown",
                        url=job_url,
                        description=description,
                        metadata={
                            "keyword": keyword,
                            "clientid": posting.get("clientid", ""),
                            "ref": posting.get("ref", ""),
                            "function": posting.get("function", ""),
                            "industry": posting.get("industry", ""),
                            "entity_status": posting.get("entity_status", ""),
                            "open_date": posting.get("open_date", ""),
                            "update_date": posting.get("update_date", ""),
                            "years_experience": posting.get("years_experience", ""),
                        },
                    )
                )
                page_added += 1
            if page_added == 0:
                break
            offset += limit
    return jobs


def build_email_subject(scored_jobs: List[triage.ScoredJob], prefix: str) -> str:
    today = dt.datetime.now().strftime("%Y-%m-%d")
    new_shortlisted = sum(1 for job in scored_jobs if job.is_new and job.decision == "shortlist")
    new_review = sum(1 for job in scored_jobs if job.is_new and job.decision == "review")
    if new_shortlisted or new_review:
        return f"{prefix} {today}: {new_shortlisted} shortlist, {new_review} review"
    return f"{prefix} {today}: no matches"


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


def maybe_send_digest_email(scored_jobs: List[triage.ScoredJob], config: Dict[str, Any], config_path: Path) -> None:
    email_cfg = config.get("email", {})
    if not email_cfg.get("enabled"):
        return
    recipients = [address.strip() for address in email_cfg.get("to", []) if str(address).strip()]
    if not recipients:
        raise RuntimeError("Email is enabled but no recipient addresses are configured.")
    method = str(os.getenv("JOB_WATCH_EMAIL_METHOD", email_cfg.get("method", "apple_mail"))).strip().lower()
    summary_text = triage.build_summary(scored_jobs).strip()
    body = summary_text + "\n"
    subject = build_email_subject(scored_jobs, str(email_cfg.get("subject_prefix", "Daily company job watch")).strip() or "Daily company job watch")
    sender = str(os.getenv("JOB_WATCH_FROM_ADDRESS", email_cfg.get("from_address", ""))).strip()
    if method == "apple_mail":
        send_email_via_apple_mail(recipients, subject, body, sender=sender)
        log(f"Email sent to {', '.join(recipients)}.")
        return
    if method == "smtp":
        smtp_host = str(os.getenv("JOB_WATCH_SMTP_HOST", email_cfg.get("smtp_host", ""))).strip()
        smtp_port_raw = os.getenv("JOB_WATCH_SMTP_PORT", str(email_cfg.get("smtp_port", 465)))
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
        use_ssl_raw = str(os.getenv("JOB_WATCH_SMTP_USE_SSL", str(email_cfg.get("smtp_use_ssl", True)))).strip().lower()
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


def collect_sources(config: Dict[str, Any]) -> List[triage.JobRecord]:
    profile = config["profile"]
    jobs: List[triage.JobRecord] = []
    collectors = {
        "ford_radancy": collect_ford,
        "gm_workday": collect_gm,
        "magna_workday": collect_gm,
        "stellantis_cws": collect_stellantis,
        "toyota_phenom": collect_toyota,
        "honda_phenom": collect_toyota,
        "keyence_rss": collect_keyence,
    }
    for source in config.get("sources", []):
        if not source.get("enabled", True):
            continue
        collector = collectors.get(source["type"])
        if collector is None:
            continue
        try:
            log(f"Collecting {source.get('name', source['type'])}...")
            before = len(jobs)
            jobs.extend(collector(source, profile))
            added = len(jobs) - before
            log(f"Collected {added} raw matches from {source.get('name', source['type'])}.")
        except Exception as exc:
            print(f"Collector warning for {source.get('name', source['type'])}: {exc}", file=sys.stderr)
    return triage.dedupe_jobs(jobs)


def write_raw_jobs(config: Dict[str, Any], config_path: Path, jobs: List[triage.JobRecord]) -> None:
    output_cfg = config["output"]
    raw_path = (config_path.parent / output_cfg["raw_jobs_path"]).resolve()
    triage.ensure_parent(raw_path)
    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "jobs": [asdict(job) for job in jobs],
    }
    raw_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def apply_runtime_overrides(config: Dict[str, Any]) -> None:
    ollama_override = str(os.getenv("JOB_WATCH_OLLAMA_ENABLED", "")).strip().lower()
    if ollama_override:
        config.setdefault("ollama", {})["enabled"] = ollama_override in {"1", "true", "yes", "on"}


def main() -> int:
    default_config_path = Path(__file__).resolve().parent.parent / "config" / "company_watch.json"
    parser = argparse.ArgumentParser(description="Collect public company jobs and score them for entry-level fit.")
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
    apply_runtime_overrides(config)
    jobs = collect_sources(config)
    write_raw_jobs(config, config_path, jobs)

    state_path = (config_path.parent / config["output"]["state_path"]).resolve()
    state = triage.load_state(state_path)
    seen_keys = set(state.get("seen_keys", []))
    updated_seen_keys = set(seen_keys)

    scored_jobs: List[triage.ScoredJob] = []
    for job in jobs:
        scored = triage.score_job(job, config)
        job_key = triage.stable_job_key(job)
        scored.is_new = job_key not in seen_keys
        verdict = triage.maybe_run_ollama(scored, config)
        scored_jobs.append(triage.merge_ollama(scored, verdict))
        updated_seen_keys.add(job_key)

    scored_jobs.sort(
        key=lambda item: (
            item.decision != "shortlist",
            -triage.job_post_sort_value(item.job),
            -item.score,
            item.job.company.lower(),
            item.job.title.lower(),
        )
    )
    triage.write_outputs(scored_jobs, config, config_path)
    triage.save_state(state_path, updated_seen_keys)
    if not args.no_email:
        maybe_send_digest_email(scored_jobs, config, config_path)
    print(f"Collected jobs: {len(jobs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
