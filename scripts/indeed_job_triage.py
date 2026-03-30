#!/usr/bin/env python3
import argparse
import datetime as dt
import email
import html
import imaplib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
NUMBER_TOKEN = r"(?:\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)"
EXPERIENCE_PATTERNS = [
    re.compile(
        rf"(?P<first>{NUMBER_TOKEN})\s*(?:\+)?\s*(?:-|to|or)\s*(?P<second>{NUMBER_TOKEN})\s*\+?\s*years?",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:at least|minimum of|minimum|requires?)\s+(?P<first>{NUMBER_TOKEN})\+?\s+years?",
        re.IGNORECASE,
    ),
    re.compile(rf"(?P<first>{NUMBER_TOKEN})\+\s+years?", re.IGNORECASE),
    re.compile(rf"(?P<first>{NUMBER_TOKEN})\s+or more\s+years?", re.IGNORECASE),
    re.compile(
        rf"(?P<first>{NUMBER_TOKEN})\s+years?\s+(?:of\s+)?experience",
        re.IGNORECASE,
    ),
]
ENTRY_LEVEL_HINTS = (
    "entry level",
    "entry-level",
    "junior",
    "new grad",
    "recent graduate",
    "graduate",
    "no experience",
    "no prior experience",
    "willing to train",
    "training provided",
    "apprentice",
    "trainee",
)
SENIORITY_EXCLUDES = (
    "senior",
    "sr ",
    "sr.",
    "lead",
    "principal",
    "staff",
    "manager",
    "director",
    "architect",
)
INDEED_LINK_RE = re.compile(r"https?://[^\s\"'>]+indeed\.[^\s\"'>]+", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
RELATIVE_POSTED_RE = re.compile(r"posted\s+(?P<days>\d+)\+?\s+days?\s+ago", re.IGNORECASE)


@dataclass
class JobRecord:
    source: str
    source_id: str
    title: str
    company: str
    location: str
    url: str
    description: str
    metadata: Dict[str, Any]


@dataclass
class ScoredJob:
    job: JobRecord
    score: int
    decision: str
    reasons: List[str]
    experience_hits: List[str]
    experience_years_max: Optional[int]
    entry_level_signal: bool
    ollama_verdict: Optional[Dict[str, Any]]
    is_new: bool


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_dotenv(paths: Iterable[Path]) -> None:
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


def get_keychain_password(service: str, account: str) -> Optional[str]:
    if not service or not account:
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service, "-a", account],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.strip() or None


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def html_to_text(content: str) -> str:
    content = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", content)
    content = re.sub(r"(?i)<br\s*/?>", "\n", content)
    content = re.sub(r"(?i)</p>|</div>|</li>|</tr>|</h\d>", "\n", content)
    content = TAG_RE.sub(" ", content)
    content = html.unescape(content)
    return re.sub(r"[ \t]+", " ", content).replace("\r", "")


def number_value(token: str) -> Optional[int]:
    token = token.lower().strip()
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


def get_context_label(text: str, start: int, end: int) -> str:
    left = max(0, start - 25)
    right = min(len(text), end + 25)
    while left > 0 and not text[left - 1].isspace():
        left -= 1
    while right < len(text) and not text[right - 1].isspace():
        right += 1
    return normalize_whitespace(text[left:right])


def extract_experience(text: str) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    lowered = text.lower()
    hits: List[Dict[str, Any]] = []
    years_max: Optional[int] = None

    for pattern in EXPERIENCE_PATTERNS:
        for match in pattern.finditer(lowered):
            first = number_value(match.group("first"))
            second = number_value(match.groupdict().get("second", ""))
            if first is None:
                continue
            max_year = second if second is not None else first
            snippet = get_context_label(text, match.start(), match.end())
            preferred = bool(re.search(r"\b(preferred|nice to have|bonus)\b", snippet, re.IGNORECASE))
            hit = {
                "snippet": snippet,
                "first": first,
                "second": second,
                "max_year": max_year,
                "preferred": preferred,
            }
            hits.append(hit)
            if not preferred:
                years_max = max_year if years_max is None else max(years_max, max_year)

    grouped: Dict[Tuple[Optional[int], bool], Dict[str, Any]] = {}
    for hit in hits:
        key = (hit["max_year"], hit["preferred"])
        current = grouped.get(key)
        if current is None or len(hit["snippet"]) > len(current["snippet"]):
            grouped[key] = hit
    ordered = sorted(grouped.values(), key=lambda item: len(item["snippet"]), reverse=True)
    filtered: List[Dict[str, Any]] = []
    for hit in ordered:
        snippet = hit["snippet"]
        if any(
            hit["preferred"] == kept["preferred"] and snippet in kept["snippet"]
            for kept in filtered
        ):
            continue
        filtered.append(hit)
    return filtered, years_max


def has_entry_level_signal(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in ENTRY_LEVEL_HINTS)


def title_has_excluded_seniority(title: str, exclude_keywords: Iterable[str]) -> bool:
    lowered = f" {title.lower()} "
    return any(keyword.lower() in lowered for keyword in exclude_keywords)


def extract_urls(text: str) -> List[str]:
    urls = []
    for match in INDEED_LINK_RE.finditer(text):
        url = match.group(0).rstrip(").,]")
        urls.append(url)
    deduped = []
    seen = set()
    for url in urls:
        if url in seen:
            continue
        deduped.append(url)
        seen.add(url)
    return deduped


def parse_message_body(message: email.message.EmailMessage) -> str:
    html_body = ""
    text_body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            content_type = part.get_content_type()
            if content_type == "text/plain" and len(decoded) > len(text_body):
                text_body = decoded
            elif content_type == "text/html" and len(decoded) > len(html_body):
                html_body = decoded
    else:
        payload = message.get_payload(decode=True) or b""
        decoded = payload.decode(message.get_content_charset() or "utf-8", errors="replace")
        if message.get_content_type() == "text/html":
            html_body = decoded
        else:
            text_body = decoded

    if html_body and len(html_to_text(html_body)) >= len(text_body):
        return html_to_text(html_body)
    return text_body


def maybe_parse_date(value: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, IndexError):
        return value


def parse_iso_datetime(value: str) -> Optional[dt.datetime]:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    normalized = normalized.replace("Z", "+00:00")
    normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", normalized)
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def parse_relative_posted_age_days(value: str) -> Optional[int]:
    if not value:
        return None
    lowered = value.strip().lower()
    if not lowered:
        return None
    if "today" in lowered or "just posted" in lowered:
        return 0
    if "yesterday" in lowered:
        return 1
    match = RELATIVE_POSTED_RE.search(lowered)
    if match:
        return int(match.group("days"))
    return None


def job_posted_datetime(job: JobRecord) -> Optional[dt.datetime]:
    metadata = job.metadata or {}
    for key in ("posted_at", "posted_at_iso", "open_date", "posted_date", "update_date", "pub_date", "sent_at"):
        value = metadata.get(key)
        if not value:
            continue
        parsed = parse_iso_datetime(str(value))
        if parsed is not None:
            return parsed
    return None


def job_post_age_days(job: JobRecord, now: Optional[dt.datetime] = None) -> Optional[float]:
    metadata = job.metadata or {}
    posted_at = job_posted_datetime(job)
    if posted_at is not None:
        now_utc = now or dt.datetime.now(dt.timezone.utc)
        delta = now_utc - posted_at
        if delta.total_seconds() < 0:
            return 0.0
        return delta.total_seconds() / 86400.0
    for key in ("posted_on", "posted_raw", "posted"):
        value = metadata.get(key)
        if not value:
            continue
        age_days = parse_relative_posted_age_days(str(value))
        if age_days is not None:
            return float(age_days)
    return None


def job_posted_label(job: JobRecord) -> str:
    metadata = job.metadata or {}
    for key in ("posted_on", "posted_raw", "posted"):
        value = metadata.get(key)
        if value:
            return normalize_whitespace(str(value))
    posted_at = job_posted_datetime(job)
    if posted_at is not None:
        return posted_at.date().isoformat()
    return ""


def job_post_sort_value(job: JobRecord) -> float:
    posted_at = job_posted_datetime(job)
    if posted_at is not None:
        return posted_at.timestamp()
    age_days = job_post_age_days(job)
    if age_days is not None:
        now_utc = dt.datetime.now(dt.timezone.utc)
        return (now_utc - dt.timedelta(days=age_days)).timestamp()
    return float("-inf")


def fetch_jobs_from_imap(source_cfg: Dict[str, Any]) -> List[JobRecord]:
    host = os.getenv(source_cfg["host_env"], "")
    user = os.getenv(source_cfg["user_env"], "")
    password = os.getenv(source_cfg["password_env"], "")
    if not password:
        service = os.getenv(source_cfg.get("password_keychain_service_env", ""), "") or source_cfg.get("password_keychain_service", "")
        password = get_keychain_password(service, user)
    if not host or not user or not password:
        raise RuntimeError(
            "Missing IMAP credentials. Set environment variables "
            f"{source_cfg['host_env']}, {source_cfg['user_env']}, and {source_cfg['password_env']}, "
            "or store the password in macOS Keychain."
        )

    folder = source_cfg.get("folder", "INBOX")
    search_mode = source_cfg.get("search", "UNSEEN")
    sender = source_cfg.get("from", "alert@indeed.com")
    limit = int(source_cfg.get("limit", 20))

    client = imaplib.IMAP4_SSL(host)
    client.login(user, password)
    try:
        status, _ = client.select(folder)
        if status != "OK":
            raise RuntimeError(f"Could not select IMAP folder {folder!r}.")

        status, data = client.uid("search", None, search_mode, "FROM", f'"{sender}"')
        if status != "OK":
            raise RuntimeError("IMAP search failed.")
        ids = data[0].split()[-limit:]
        records: List[JobRecord] = []
        for message_id in ids:
            status, message_data = client.uid("fetch", message_id, "(BODY.PEEK[])")
            if status != "OK":
                continue
            raw_bytes = message_data[0][1]
            message = email.message_from_bytes(raw_bytes, policy=policy.default)
            body = parse_message_body(message)
            subject = message.get("Subject", "(No subject)")
            urls = extract_urls(body)
            record = JobRecord(
                source="imap",
                source_id=message_id.decode("utf-8", errors="replace"),
                title=subject,
                company="Unknown",
                location="Unknown",
                url=urls[0] if urls else "",
                description=body,
                metadata={
                    "from": message.get("From", ""),
                    "subject": subject,
                    "sent_at": maybe_parse_date(message.get("Date", "")),
                    "extracted_urls": urls,
                },
            )
            records.append(record)
        return records
    finally:
        client.logout()


def jobs_from_json(path: Path) -> List[JobRecord]:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    items = raw if isinstance(raw, list) else raw.get("jobs", [])
    jobs: List[JobRecord] = []
    for idx, item in enumerate(items, start=1):
        jobs.append(
            JobRecord(
                source="json",
                source_id=str(item.get("id", idx)),
                title=str(item.get("title", "Unknown title")),
                company=str(item.get("company", "Unknown")),
                location=str(item.get("location", "Unknown")),
                url=str(item.get("url", "")),
                description=str(item.get("description", "")),
                metadata={"raw": item},
            )
        )
    return jobs


def jobs_from_folder(path: Path) -> List[JobRecord]:
    jobs: List[JobRecord] = []
    for idx, item in enumerate(sorted(path.rglob("*")), start=1):
        if not item.is_file():
            continue
        suffix = item.suffix.lower()
        if suffix == ".json":
            jobs.extend(jobs_from_json(item))
            continue
        if suffix not in {".txt", ".md", ".html", ".htm", ".eml"}:
            continue

        if suffix == ".eml":
            raw = item.read_bytes()
            message = email.message_from_bytes(raw, policy=policy.default)
            description = parse_message_body(message)
            title = message.get("Subject", item.stem)
            urls = extract_urls(description)
            jobs.append(
                JobRecord(
                    source="folder",
                    source_id=str(item),
                    title=title,
                    company="Unknown",
                    location="Unknown",
                    url=urls[0] if urls else "",
                    description=description,
                    metadata={"path": str(item), "type": "eml", "extracted_urls": urls},
                )
            )
            continue

        content = item.read_text(encoding="utf-8", errors="replace")
        description = html_to_text(content) if suffix in {".html", ".htm"} else content
        urls = extract_urls(description)
        jobs.append(
            JobRecord(
                source="folder",
                source_id=str(item),
                title=item.stem.replace("_", " "),
                company="Unknown",
                location="Unknown",
                url=urls[0] if urls else "",
                description=description,
                metadata={"path": str(item), "type": suffix.lstrip("."), "extracted_urls": urls},
            )
        )
    return jobs


def load_jobs(config: Dict[str, Any], config_path: Path) -> List[JobRecord]:
    source_cfg = config["source"]
    source_type = source_cfg.get("type", "folder")
    if source_type == "imap":
        return fetch_jobs_from_imap(source_cfg)
    if source_type == "json":
        source_path = (config_path.parent / source_cfg["path"]).resolve()
        return jobs_from_json(source_path)
    if source_type == "folder":
        source_path = (config_path.parent / source_cfg["path"]).resolve()
        return jobs_from_folder(source_path)
    raise ValueError(f"Unsupported source type: {source_type}")


def dedupe_jobs(jobs: List[JobRecord]) -> List[JobRecord]:
    deduped: List[JobRecord] = []
    seen = set()
    for job in jobs:
        key = job.url or f"{job.title}|{job.company}|{job.location}|{job.source_id}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(job)
    return deduped


def stable_job_key(job: JobRecord) -> str:
    if job.url:
        return normalize_whitespace(job.url.lower())
    parts = [job.title.lower(), job.company.lower(), job.location.lower(), normalize_whitespace(job.description[:240].lower())]
    return "|".join(parts)


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"seen_keys": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"seen_keys": []}


def save_state(path: Path, seen_keys: Iterable[str]) -> None:
    ensure_parent(path)
    payload = {
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "seen_keys": sorted(set(seen_keys)),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def contains_any(text: str, keywords: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords if keyword)


def score_job(job: JobRecord, config: Dict[str, Any]) -> ScoredJob:
    profile = config["profile"]
    allowed_years_max = int(profile.get("allowed_experience_years_max", 1))
    max_post_age_days = profile.get("max_post_age_days")
    required_keywords = profile.get("required_keywords", [])
    preferred_keywords = profile.get("preferred_keywords", [])
    text_exclude_keywords = tuple(profile.get("exclude_keywords", []))
    title_exclude_keywords = text_exclude_keywords + SENIORITY_EXCLUDES
    desired_titles = profile.get("job_titles", [])
    desired_locations = profile.get("locations", [])

    full_text = "\n".join([job.title, job.company, job.location, job.description]).strip()
    lowered = full_text.lower()
    reasons: List[str] = []
    score = 0

    experience_hits, experience_years_max = extract_experience(full_text)
    entry_signal = has_entry_level_signal(full_text)

    if title_has_excluded_seniority(job.title, title_exclude_keywords):
        reasons.append("Rejected for senior title keyword.")
        return ScoredJob(job, 0, "reject", reasons, [hit["snippet"] for hit in experience_hits], experience_years_max, entry_signal, None, False)

    if required_keywords and not contains_any(lowered, required_keywords):
        reasons.append("Missing required keywords.")
        return ScoredJob(job, 0, "reject", reasons, [hit["snippet"] for hit in experience_hits], experience_years_max, entry_signal, None, False)

    if text_exclude_keywords and contains_any(lowered, text_exclude_keywords):
        reasons.append("Contains excluded keywords.")
        return ScoredJob(job, 0, "reject", reasons, [hit["snippet"] for hit in experience_hits], experience_years_max, entry_signal, None, False)

    if max_post_age_days is not None:
        try:
            max_post_age_days_value = float(max_post_age_days)
        except (TypeError, ValueError):
            max_post_age_days_value = None
        if max_post_age_days_value is not None:
            age_days = job_post_age_days(job)
            if age_days is not None and age_days > max_post_age_days_value:
                posted_label = job_posted_label(job)
                if posted_label:
                    reasons.append(
                        f"Rejected because job posting is older than {int(max_post_age_days_value)} day(s) ({posted_label})."
                    )
                else:
                    reasons.append(f"Rejected because job posting is older than {int(max_post_age_days_value)} day(s).")
                return ScoredJob(job, 0, "reject", reasons, [hit["snippet"] for hit in experience_hits], experience_years_max, entry_signal, None, False)

    if desired_titles and contains_any(job.title.lower(), desired_titles):
        score += 25
        reasons.append("Title matches target roles.")

    if desired_locations and contains_any(full_text.lower(), desired_locations):
        score += 20
        reasons.append("Location matches preferences.")

    if preferred_keywords:
        preferred_hits = [keyword for keyword in preferred_keywords if keyword.lower() in lowered]
        if preferred_hits:
            score += min(20, 5 * len(preferred_hits))
            reasons.append("Preferred keywords matched: " + ", ".join(preferred_hits[:4]))

    if entry_signal:
        score += 30
        reasons.append("Entry-level wording detected.")

    if experience_years_max is None and experience_hits:
        reasons.append("Only preferred or non-blocking experience requirements found.")
        score += 10
    elif experience_years_max is None:
        reasons.append("No explicit experience requirement found.")
        score += 10
    elif experience_years_max <= allowed_years_max:
        reasons.append(f"Explicit experience requirement is <= {allowed_years_max} year(s).")
        score += 30
    else:
        reasons.append(f"Rejected because experience requirement appears to exceed {allowed_years_max} year(s).")
        return ScoredJob(job, score, "reject", reasons, [hit["snippet"] for hit in experience_hits], experience_years_max, entry_signal, None, False)

    if score >= 70:
        decision = "shortlist"
    elif score >= 40:
        decision = "review"
    else:
        decision = "reject"
        reasons.append("Score too low for shortlist.")

    return ScoredJob(
        job,
        max(0, min(100, score)),
        decision,
        reasons,
        [hit["snippet"] for hit in experience_hits],
        experience_years_max,
        entry_signal,
        None,
        False,
    )


def maybe_run_ollama(scored_job: ScoredJob, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ollama_cfg = config.get("ollama", {})
    if not ollama_cfg.get("enabled"):
        return None
    if ollama_cfg.get("skip_rejects", False) and scored_job.decision == "reject":
        return None
    minimum_rule_score = ollama_cfg.get("minimum_rule_score")
    if minimum_rule_score is not None:
        try:
            if scored_job.score < int(minimum_rule_score):
                return None
        except (TypeError, ValueError):
            pass

    profile = config["profile"]
    prompt = (
        "You screen jobs for an entry-level candidate.\n"
        "Return ONLY valid JSON with keys fit, score, reason.\n"
        "fit must be one of yes, maybe, no.\n"
        "score must be an integer from 0 to 100.\n"
        "reason must be one short sentence.\n"
        f"Hard rule: if the job requires more than {profile.get('allowed_experience_years_max', 1)} year(s) of experience, fit=no.\n"
        f"Target roles: {', '.join(profile.get('job_titles', [])) or 'not specified'}.\n"
        f"Target locations: {', '.join(profile.get('locations', [])) or 'not specified'}.\n"
        f"Job title: {scored_job.job.title}\n"
        f"Company: {scored_job.job.company}\n"
        f"Location: {scored_job.job.location}\n"
        f"Description:\n{scored_job.job.description[:4000]}\n"
    )

    request_body = json.dumps(
        {
            "model": ollama_cfg.get("model", "llama3.1:8b"),
            "prompt": prompt,
            "stream": False,
            "format": {
                "type": "object",
                "properties": {
                    "fit": {"type": "string"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["fit", "score", "reason"],
            },
            "options": {"temperature": 0},
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        ollama_cfg.get("base_url", "http://127.0.0.1:11434").rstrip("/") + "/api/generate",
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=int(ollama_cfg.get("timeout_seconds", 60))) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {"fit": "unknown", "score": 0, "reason": "Ollama call failed."}

    response_text = payload.get("response", "").strip()
    if response_text.startswith("```"):
        response_text = re.sub(r"^```(?:json)?", "", response_text).strip()
        response_text = re.sub(r"```$", "", response_text).strip()

    try:
        verdict = json.loads(response_text or "{}")
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if not match:
            return {"fit": "unknown", "score": 0, "reason": "Ollama returned non-JSON output."}
        try:
            verdict = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {"fit": "unknown", "score": 0, "reason": "Ollama returned non-JSON output."}

    fit = str(verdict.get("fit", "unknown")).lower()
    if fit not in {"yes", "maybe", "no"}:
        return {"fit": "unknown", "score": 0, "reason": "Ollama returned an unexpected fit value."}
    try:
        score = int(verdict.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    reason = str(verdict.get("reason", "No reason returned.")).strip() or "No reason returned."
    return {"fit": fit, "score": max(0, min(100, score)), "reason": reason}


def merge_ollama(scored_job: ScoredJob, verdict: Optional[Dict[str, Any]]) -> ScoredJob:
    if not verdict:
        return scored_job
    score = scored_job.score
    fit = str(verdict.get("fit", "")).lower()
    if fit == "yes":
        score += 15
    elif fit == "maybe":
        score += 5
    elif fit == "no":
        score -= 20

    decision = scored_job.decision
    if fit == "no":
        decision = "reject"
    elif score >= 75 and decision != "reject":
        decision = "shortlist"
    elif score >= 45 and decision == "reject":
        decision = "review"

    reasons = list(scored_job.reasons)
    reasons.append("Ollama verdict: " + str(verdict.get("reason", "No reason returned.")))
    return ScoredJob(
        job=scored_job.job,
        score=max(0, min(100, score)),
        decision=decision,
        reasons=reasons,
        experience_hits=scored_job.experience_hits,
        experience_years_max=scored_job.experience_years_max,
        entry_level_signal=scored_job.entry_level_signal,
        ollama_verdict=verdict,
        is_new=scored_job.is_new,
    )


def build_report(scored_jobs: List[ScoredJob], config: Dict[str, Any]) -> str:
    now = dt.datetime.now().isoformat(timespec="seconds")
    new_jobs = [job for job in scored_jobs if job.is_new]
    shortlisted = [job for job in scored_jobs if job.decision == "shortlist"]
    review = [job for job in scored_jobs if job.decision == "review"]
    rejected = [job for job in scored_jobs if job.decision == "reject"]
    new_shortlisted = [job for job in shortlisted if job.is_new]
    new_review = [job for job in review if job.is_new]
    new_rejected = [job for job in rejected if job.is_new]

    lines = [
        "# Entry-Level Job Triage Report",
        "",
        f"Generated: {now}",
        f"Target roles: {', '.join(config['profile'].get('job_titles', [])) or 'Not set'}",
        f"Target locations: {', '.join(config['profile'].get('locations', [])) or 'Not set'}",
        f"Allowed experience max: {config['profile'].get('allowed_experience_years_max', 1)} year(s)",
        f"Max posting age: {config['profile'].get('max_post_age_days', 'Not set')} day(s)",
        "",
        f"Processed this run: {len(scored_jobs)}",
        f"New jobs this run: {len(new_jobs)}",
        f"Shortlisted: {len(shortlisted)}",
        f"Review: {len(review)}",
        f"Rejected: {len(rejected)}",
        f"New shortlist: {len(new_shortlisted)}",
        f"New review: {len(new_review)}",
        f"New rejected: {len(new_rejected)}",
        "",
    ]

    def append_section(title: str, jobs: List[ScoredJob]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not jobs:
            lines.append("None.")
            lines.append("")
            return
        for idx, scored in enumerate(jobs, start=1):
            job = scored.job
            lines.append(f"{idx}. {job.title} - {job.company} - {job.location}")
            lines.append(f"   New this run: {'yes' if scored.is_new else 'no'}")
            lines.append(f"   Score: {scored.score}")
            posted_label = job_posted_label(job)
            if posted_label:
                lines.append(f"   Posted: {posted_label}")
            if job.url:
                lines.append(f"   URL: {job.url}")
            lines.append(f"   Reasons: {'; '.join(scored.reasons)}")
            if scored.experience_hits:
                lines.append("   Experience hits:")
                for hit in scored.experience_hits[:3]:
                    lines.append(f"   - {hit}")
            if scored.ollama_verdict:
                lines.append(f"   Ollama: {json.dumps(scored.ollama_verdict, ensure_ascii=True)}")
            lines.append("")

    append_section("New Shortlist", new_shortlisted)
    append_section("New Needs Manual Review", new_review)
    append_section("New Rejected", new_rejected[:10])
    append_section("All Shortlist", shortlisted)
    append_section("All Needs Manual Review", review)

    lines.append("## Manual Apply Reminder")
    lines.append("")
    lines.append("This workflow is designed to rank jobs for manual review. It does not submit applications.")
    lines.append("")
    return "\n".join(lines)


def build_summary(scored_jobs: List[ScoredJob]) -> str:
    now = dt.datetime.now().isoformat(timespec="seconds")
    new_matches = [job for job in scored_jobs if job.is_new and job.decision in {"shortlist", "review"}]
    lines = [
        "# Daily Job Digest",
        "",
        f"Generated: {now}",
        "",
    ]
    if not new_matches:
        lines.append("Today's search found no new shortlist or review jobs.")
        lines.append("")
        return "\n".join(lines)

    shortlisted = [job for job in new_matches if job.decision == "shortlist"]
    review = [job for job in new_matches if job.decision == "review"]
    lines.append(f"New shortlist: {len(shortlisted)}")
    lines.append(f"New review: {len(review)}")
    lines.append("")

    for scored in new_matches:
        job = scored.job
        lines.append(f"- {job.title} - {job.company} - {job.location} [{scored.decision}]")
        posted_label = job_posted_label(job)
        if posted_label:
            lines.append(f"  Posted: {posted_label}")
        if job.url:
            lines.append(f"  URL: {job.url}")
        lines.append(f"  Reasons: {'; '.join(scored.reasons)}")
        lines.append("")
    return "\n".join(lines)


def write_outputs(scored_jobs: List[ScoredJob], config: Dict[str, Any], config_path: Path) -> None:
    output_cfg = config["output"]
    report_path = (config_path.parent / output_cfg["report_path"]).resolve()
    json_path = (config_path.parent / output_cfg["json_path"]).resolve()
    summary_path = (config_path.parent / output_cfg["summary_path"]).resolve()
    ensure_parent(report_path)
    ensure_parent(json_path)
    ensure_parent(summary_path)

    report = build_report(scored_jobs, config)
    report_path.write_text(report, encoding="utf-8")
    summary_path.write_text(build_summary(scored_jobs), encoding="utf-8")

    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "jobs": [
            {
                **asdict(scored.job),
                "score": scored.score,
                "decision": scored.decision,
                "reasons": scored.reasons,
                "experience_hits": scored.experience_hits,
                "experience_years_max": scored.experience_years_max,
                "entry_level_signal": scored.entry_level_signal,
                "ollama_verdict": scored.ollama_verdict,
                "is_new": scored.is_new,
                "job_key": stable_job_key(scored.job),
            }
            for scored in scored_jobs
        ],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")

    print(f"Report: {report_path}")
    print(f"Summary: {summary_path}")
    print(f"JSON: {json_path}")


def main() -> int:
    default_config_path = Path(__file__).resolve().parent.parent / "config" / "job_triage.example.json"
    parser = argparse.ArgumentParser(description="Filter entry-level jobs without auto-applying.")
    parser.add_argument(
        "--config",
        default=str(default_config_path),
        help="Path to the JSON config file.",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    load_dotenv(
        [
            config_path.parent / ".env",
            config_path.parent / ".env.local",
            config_path.parent.parent / ".env",
            config_path.parent.parent / ".env.local",
        ]
    )
    config = load_config(config_path)
    state_path = (config_path.parent / config["output"]["state_path"]).resolve()
    state = load_state(state_path)
    seen_keys = set(state.get("seen_keys", []))

    try:
        jobs = dedupe_jobs(load_jobs(config, config_path))
    except Exception as exc:
        print(f"Source error: {exc}", file=sys.stderr)
        return 1

    scored_jobs: List[ScoredJob] = []
    updated_seen_keys = set(seen_keys)
    for job in jobs:
        scored = score_job(job, config)
        job_key = stable_job_key(job)
        scored.is_new = job_key not in seen_keys
        verdict = maybe_run_ollama(scored, config)
        scored_jobs.append(merge_ollama(scored, verdict))
        updated_seen_keys.add(job_key)

    scored_jobs.sort(key=lambda item: (item.decision != "shortlist", -job_post_sort_value(item.job), -item.score, item.job.title.lower()))
    write_outputs(scored_jobs, config, config_path)
    save_state(state_path, updated_seen_keys)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
