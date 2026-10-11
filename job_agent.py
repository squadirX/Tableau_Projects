#!/usr/bin/env python3

import imaplib
import email
import re
import json
import csv
import os
import ssl
import html
import urllib.request
import argparse
from email.header import decode_header
from datetime import datetime, timedelta


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = os.path.expanduser("~/JobSearchAgent")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
PROFILE_FILE = os.path.join(BASE_DIR, "candidate_profile.json")

JOBS_FILE = os.path.join(DATA_DIR, "jobs.csv")
LOG_FILE = os.path.join(LOG_DIR, "job_agent.log")
RUN_HISTORY_FILE = os.path.join(LOG_DIR, "run_history.csv")

YAHOO_IMAP_SERVER = "imap.mail.yahoo.com"
YAHOO_IMAP_PORT = 993


# ============================================================
# DEFAULT CANDIDATE PROFILE
# ============================================================

DEFAULT_PROFILE = {
    "name": "Saiful Quadir",

    "target_titles": [
        "market research analyst",
        "market research manager",
        "data analyst",
        "business analyst",
        "business development analyst",
        "project coordinator",
        "project manager",
        "project analyst",
        "marketing analyst",
        "marketing analytics",
        "operations analyst",
        "software qa",
        "qa analyst",
        "quality assurance analyst"
    ],

    "preferred_locations": [
        "toronto",
        "ontario",
        "canada",
        "remote"
    ],

    "skills": [
        "market research",
        "data analysis",
        "business development",
        "project management",
        "project coordination",
        "stakeholder management",
        "digital surveys",
        "survey analysis",
        "market analysis",
        "competitive analysis",
        "business analysis",
        "data visualization",
        "reporting",
        "power bi",
        "tableau",
        "excel",
        "automation",
        "process improvement",
        "systems analysis",
        "software testing",
        "quality assurance",
        "marketing analytics",
        "machine learning",
        "python",
        "sql"
    ],

    "experience": [
        "market research",
        "digital surveys",
        "data analysis",
        "project management",
        "business development",
        "systems analysis",
        "team management",
        "process automation",
        "reporting",
        "client management"
    ],

    "education": [
        "mba",
        "marketing",
        "b.com",
        "business"
    ],

    "certifications": [
        "google project management",
        "google cybersecurity",
        "ibm machine learning"
    ],

    "do_not_apply": [
        "unpaid",
        "commission only",
        "commission-only",
        "volunteer",
        "must be a us citizen",
        "u.s. citizenship required",
        "security clearance required",
        "requires relocation"
    ]
}


# ============================================================
# UTILITIES
# ============================================================

def log(message):
    os.makedirs(LOG_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"

    print(line)

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(filename, default):
    if not os.path.exists(filename):
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)

        return default

    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception:
        return default


def clean_text(text):
    if not text:
        return ""

    text = html.unescape(text)

    text = re.sub(
        r"<script.*?</script>",
        " ",
        text,
        flags=re.I | re.S
    )

    text = re.sub(
        r"<style.*?</style>",
        " ",
        text,
        flags=re.I | re.S
    )

    text = re.sub(
        r"<[^>]+>",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def normalize_space(text):
    if not text:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(text)
    ).strip()


def decode_email_subject(subject):
    if not subject:
        return ""

    result = ""

    for part, encoding in decode_header(subject):

        if isinstance(part, bytes):

            try:
                result += part.decode(
                    encoding or "utf-8",
                    errors="ignore"
                )

            except Exception:

                result += part.decode(
                    "utf-8",
                    errors="ignore"
                )

        else:
            result += str(part)

    return result


def normalize_url(url):
    if not url:
        return ""

    url = html.unescape(
        str(url)
    ).strip()

    markdown_match = re.fullmatch(
        r"\[([^\]]+)\]\(([^)]+)\)",
        url
    )

    if markdown_match:

        first = markdown_match.group(1).strip()
        second = markdown_match.group(2).strip()

        if first.lower().startswith("http"):
            url = first

        elif second.lower().startswith("http"):
            url = second

    url = url.rstrip(
        ".,);]}>\"'"
    )

    return url


def unique_preserve_order(items):
    result = []
    seen = set()

    for item in items:

        if item in seen:
            continue

        seen.add(item)
        result.append(item)

    return result


# ============================================================
# EMAIL
# ============================================================

def connect_yahoo(config):

    username = str(
        config.get("yahoo_email") or ""
    ).strip()

    password = str(
        config.get("yahoo_app_password") or ""
    ).strip()

    if not username or not password:
        raise ValueError(
            "Yahoo email or Yahoo App Password is missing "
            "from config.json"
        )

    context = ssl.create_default_context()

    mail = imaplib.IMAP4_SSL(
        YAHOO_IMAP_SERVER,
        YAHOO_IMAP_PORT,
        ssl_context=context
    )

    mail.login(
        username,
        password
    )

    return mail


def get_email_body(msg):

    text_parts = []
    html_parts = []

    if msg.is_multipart():

        for part in msg.walk():

            content_type = part.get_content_type()

            disposition = str(
                part.get(
                    "Content-Disposition",
                    ""
                )
            )

            if "attachment" in disposition.lower():
                continue

            if content_type not in [
                "text/plain",
                "text/html"
            ]:
                continue

            try:

                payload = part.get_payload(
                    decode=True
                )

                if not payload:
                    continue

                charset = (
                    part.get_content_charset()
                    or "utf-8"
                )

                decoded = payload.decode(
                    charset,
                    errors="ignore"
                )

                if content_type == "text/plain":
                    text_parts.append(decoded)
                else:
                    html_parts.append(decoded)

            except Exception:
                continue

    else:

        try:

            payload = msg.get_payload(
                decode=True
            )

            if payload:

                charset = (
                    msg.get_content_charset()
                    or "utf-8"
                )

                decoded = payload.decode(
                    charset,
                    errors="ignore"
                )

                if msg.get_content_type() == "text/html":
                    html_parts.append(decoded)
                else:
                    text_parts.append(decoded)

        except Exception:
            pass

    combined = []

    if text_parts:
        combined.extend(text_parts)

    if html_parts:
        combined.extend(html_parts)

    return "\n".join(combined)


def is_job_alert(sender, subject, body):

    text = (
        str(sender) +
        " " +
        str(subject) +
        " " +
        str(body)
    ).lower()

    indicators = [
        "linkedin",
        "indeed",
        "job alert",
        "jobs you may be interested",
        "jobs matching",
        "recommended jobs",
        "job recommendations",
        "new jobs"
    ]

    return any(
        x in text
        for x in indicators
    )


# ============================================================
# URL EXTRACTION
# ============================================================

def extract_urls(text):

    if not text:
        return []

    candidates = []

    # HTML href URLs
    href_patterns = [
        r"""href\s*=\s*"([^"]+)""",
        r"""href\s*=\s*'([^']+)'"""
    ]

    for pattern in href_patterns:

        candidates.extend(
            re.findall(
                pattern,
                text,
                flags=re.I
            )
        )

    # Plain URLs
    candidates.extend(
        re.findall(
            r'https?://[^\s<>"\']+',
            text
        )
    )

    # Markdown URLs
    candidates.extend(
        re.findall(
            r'\[[^\]]+\]\((https?://[^)\s]+)\)',
            text
        )
    )

    cleaned = []

    for candidate in candidates:

        candidate = normalize_url(
            candidate
        )

        if not candidate:
            continue

        if candidate not in cleaned:
            cleaned.append(candidate)

    return cleaned


def classify_url(url):

    low = normalize_url(
        url
    ).lower()

    # --------------------------------------------------------
    # IMPORTANT:
    # Only a real numeric LinkedIn Job ID qualifies.
    #
    # Examples accepted:
    #
    # /jobs/view/4457153716/
    # /comm/jobs/view/4457153716/
    #
    # Examples rejected:
    #
    # /comm/jobs/search-results/
    # /comm/jobs/alerts/
    # /comm/feed/
    # /help/linkedin/answer/
    # /premium/products/
    # --------------------------------------------------------

    if re.search(
        r"(?:https?://)?(?:[\w-]+\.)?linkedin\.com/"
        r"(?:comm/)?jobs/view/(\d+)",
        low
    ):
        return "LinkedIn"

    if (
        "indeed.com" in low
        and re.search(
            r"[?&]jk=",
            low
        )
    ):
        return "Indeed"

    return "Other"


def extract_job_urls(
    body,
    requested_source="Both"
):

    requested = normalize_space(
        requested_source
    ).lower()

    results = []

    for url in extract_urls(body):

        source = classify_url(
            url
        )

        if source == "Other":
            continue

        if requested in [
            "linkedin",
            "indeed"
        ]:

            if source.lower() != requested:
                continue

        url = normalize_url(
            url
        )

        item = (
            source,
            url
        )

        if item not in results:
            results.append(
                item
            )

    return results


# ============================================================
# LINKEDIN-SPECIFIC URL EXTRACTION
# ============================================================

def extract_linkedin_job_urls(body):

    """
    Return ONE canonical URL per LinkedIn Job ID.

    LinkedIn emails often contain several URLs for the same job:

        company_logo_0
        job_posting_0
        jobcard_body_0

    All may point to the same Job ID.

    This function keeps only one URL for each numeric Job ID
    and completely ignores LinkedIn navigation URLs.
    """

    if not body:
        return []

    jobs_by_id = {}

    for url in extract_urls(body):

        normalized = normalize_url(
            url
        )

        if classify_url(
            normalized
        ) != "LinkedIn":
            continue

        match = re.search(
            r"linkedin\.com/"
            r"(?:comm/)?jobs/view/"
            r"(\d+)",
            normalized,
            flags=re.I
        )

        if not match:
            continue

        job_id = match.group(1)

        if job_id not in jobs_by_id:
            jobs_by_id[job_id] = normalized

    return list(
        jobs_by_id.values()
    )


def extract_linkedin_job_id(url):

    match = re.search(
        r"linkedin\.com/"
        r"(?:comm/)?jobs/view/"
        r"(\d+)",
        normalize_url(url),
        flags=re.I
    )

    if match:
        return match.group(1)

    return ""


# ============================================================
# LINKEDIN JOB CARD EXTRACTION
# ============================================================

INVALID_LINKEDIN_TEXT = {
    "relative",
    "logo",
    "image",
    "linkedin",
    "home",
    "feed",
    "help",
    "settings",
    "alerts",
    "unsubscribe",
    "messaging",
    "notifications",
    "mynetwork",
    "jobs",
    "search results",
    "job alerts"
}


def _valid_linkedin_field(text):

    text = normalize_space(
        text
    )

    if not text:
        return False

    if text.lower() in INVALID_LINKEDIN_TEXT:
        return False

    if len(text) < 2:
        return False

    if len(text) > 180:
        return False

    return True


def _looks_like_company(text):

    text = normalize_space(
        text
    )

    if not _valid_linkedin_field(text):
        return False

    words = text.split()

    if len(words) > 8:
        return False

    return True


def _split_title_company(segment):

    segment = normalize_space(
        segment
    )

    if not segment:
        return "", ""

    # --------------------------------------------------------
    # Reject obvious LinkedIn template/navigation text.
    # --------------------------------------------------------

    lower = segment.lower()

    if lower in INVALID_LINKEDIN_TEXT:
        return "", ""

    # --------------------------------------------------------
    # First try explicit " · " separator.
    #
    # Example:
    #
    # Senior Product Manager · Superhuman
    # --------------------------------------------------------

    if " · " in segment:

        parts = [
            normalize_space(x)
            for x in segment.split(" · ")
        ]

        if len(parts) >= 2:

            title = parts[0]
            company = parts[1]

            if (
                _valid_linkedin_field(title)
                and
                _valid_linkedin_field(company)
            ):
                return title, company

    # --------------------------------------------------------
    # Plain-text fallback.
    #
    # Example:
    #
    # Member of Technical Staff, North Modelling (Evals) Cohere
    # --------------------------------------------------------

    words = segment.split()

    if len(words) < 2:
        return "", ""

    candidates = []

    for count in range(
        1,
        min(5, len(words) - 1) + 1
    ):

        company_words = words[-count:]
        title_words = words[:-count]

        company = " ".join(
            company_words
        )

        title = " ".join(
            title_words
        )

        if not _valid_linkedin_field(
            title
        ):
            continue

        if not _valid_linkedin_field(
            company
        ):
            continue

        if company.lower() in {
            "senior",
            "manager",
            "director",
            "analyst",
            "engineer",
            "specialist",
            "coordinator",
            "developer",
            "recruiting",
            "relative",
            "logo",
            "image"
        }:
            continue

        if len(company) > 80:
            continue

        candidates.append(
            (
                count,
                title,
                company
            )
        )

    if candidates:

        candidates.sort(
            key=lambda x: x[0]
        )

        _, title, company = candidates[0]

        return (
            normalize_space(title),
            normalize_space(company)
        )

    return "", ""


def extract_linkedin_job_cards(body):

    """
    Extract plausible LinkedIn job cards.

    This parser is intentionally conservative.

    It will NOT create a job card from generic LinkedIn
    template/navigation text such as:

        relative
        logo
        image
        LinkedIn
        Jobs
        Search results
        Alerts
        Unsubscribe
    """

    if not body:
        return []

    text = clean_text(
        body
    )

    if not text:
        return []

    # Remove header
    text = re.sub(
        r"^.*?match your preferences\.\s*",
        "",
        text,
        flags=re.I
    )

    # Remove footer
    text = re.split(
        r"\s+See all jobs\b",
        text,
        maxsplit=1,
        flags=re.I
    )[0]

    text = normalize_space(
        text
    )

    if not text:
        return []

    cards = []

    # --------------------------------------------------------
    # Main LinkedIn pattern.
    #
    # Example:
    #
    # Job Title · Company · Toronto, ON (Remote)
    #
    # or:
    #
    # Job Title Company · Toronto, ON (Remote)
    # --------------------------------------------------------

    pattern = re.compile(
        r"(?P<prefix>.+?)"
        r"\s+[·•]\s+"
        r"(?P<location>"
        r"[^·•]{1,120}?"
        r"\((?:Remote|Hybrid)\)"
        r")",
        flags=re.I
    )

    for match in pattern.finditer(
        text
    ):

        prefix = normalize_space(
            match.group("prefix")
        )

        location = normalize_space(
            match.group("location")
        )

        if not prefix or not location:
            continue

        # ----------------------------------------------------
        # If prefix contains several separators, use the
        # final separator as company boundary.
        # ----------------------------------------------------

        title = ""
        company = ""

        parts = [
            normalize_space(x)
            for x in re.split(
                r"\s+[·•]\s+",
                prefix
            )
            if normalize_space(x)
        ]

        if len(parts) >= 2:

            title = parts[-2]
            company = parts[-1]

        else:

            title, company = _split_title_company(
                prefix
            )

        if not _valid_linkedin_field(title):
            continue

        if not _valid_linkedin_field(company):
            continue

        cards.append(
            {
                "job_id": "",
                "job_url": "",
                "job_title": title,
                "company": company,
                "location": location
            }
        )

    # --------------------------------------------------------
    # Conservative fallback.
    # --------------------------------------------------------

    if not cards:

        fallback_pattern = re.compile(
            r"(?P<prefix>.+?)"
            r"\s+[·•]\s+"
            r"(?P<location>"
            r"(?:Toronto|Vancouver|Montreal|Ottawa|Calgary|"
            r"Mississauga|Brampton|Markham|Vaughan|"
            r"Richmond Hill|Oakville|Burlington|"
            r"New York|Boston|Chicago|"
            r"Remote|Canada)"
            r"[^·•]{0,80}?"
            r"(?:\((?:Remote|Hybrid)\))"
            r")",
            flags=re.I
        )

        for match in fallback_pattern.finditer(
            text
        ):

            prefix = normalize_space(
                match.group("prefix")
            )

            location = normalize_space(
                match.group("location")
            )

            title, company = _split_title_company(
                prefix
            )

            if not _valid_linkedin_field(
                title
            ):
                continue

            if not _valid_linkedin_field(
                company
            ):
                continue

            cards.append(
                {
                    "job_id": "",
                    "job_url": "",
                    "job_title": title,
                    "company": company,
                    "location": location
                }
            )

    # --------------------------------------------------------
    # Remove duplicate cards.
    # --------------------------------------------------------

    unique_cards = []
    seen = set()

    for card in cards:

        key = (
            card["job_title"].lower(),
            card["company"].lower(),
            card["location"].lower()
        )

        if key in seen:
            continue

        seen.add(key)

        unique_cards.append(
            card
        )

    return unique_cards


# ============================================================
# JOB PAGE
# ============================================================

def fetch_page(url):

    headers = {
        "User-Agent":
            "Mozilla/5.0 (Macintosh; Intel Mac OS X) "
            "AppleWebKit/605.1.15 "
            "Version/18.0 Safari/605.1.15"
    }

    request = urllib.request.Request(
        url,
        headers=headers
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=15
        ) as response:

            data = response.read(
                2_000_000
            )

            charset = (
                response.headers.get_content_charset()
                or "utf-8"
            )

            return clean_text(
                data.decode(
                    charset,
                    errors="ignore"
                )
            )

    except Exception as e:

        log(
            f"Could not read job page: "
            f"{url} | {e}"
        )

        return ""


# ============================================================
# JOB INFORMATION
# ============================================================

def guess_job_title(
    subject,
    body,
    page
):

    combined = (
        subject +
        " " +
        body +
        " " +
        page[:10000]
    )

    patterns = [

        r"(?:job title|position|role)"
        r"\s*[:\-]\s*"
        r"([A-Za-z0-9 ,/&\-\(\)']{3,150})",

        r"(?:hiring|looking for)"
        r"\s+(?:a|an)?\s*"
        r"([A-Za-z0-9 ,/&\-\(\)']{3,150})"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            combined,
            re.I
        )

        if match:

            title = normalize_space(
                match.group(1)
            )

            if len(title) < 150:
                return title

    lower = combined.lower()

    for title in DEFAULT_PROFILE[
        "target_titles"
    ]:

        if title.lower() in lower:
            return title.title()

    return ""


def guess_company(
    body,
    page
):

    text = (
        body +
        " " +
        page[:15000]
    )

    patterns = [

        r"(?:company|employer)"
        r"\s*[:\-]\s*"
        r"([A-Za-z0-9&.,' \-]{2,100})",

        r"(?:at|with)"
        r"\s+"
        r"([A-Z][A-Za-z0-9&.,' \-]{2,80})"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text
        )

        if match:

            company = normalize_space(
                match.group(1)
            )

            if len(company) < 100:
                return company

    return ""


def guess_location(
    body,
    page
):

    text = (
        body +
        " " +
        page[:15000]
    )

    locations = [
        "Toronto",
        "Mississauga",
        "Brampton",
        "Vaughan",
        "Markham",
        "Richmond Hill",
        "Oakville",
        "Burlington",
        "Ontario",
        "Canada",
        "Remote"
    ]

    found = []

    lower = text.lower()

    for location in locations:

        if location.lower() in lower:
            found.append(
                location
            )

    return ", ".join(
        dict.fromkeys(found)
    )


# ============================================================
# MATCHING ENGINE
# ============================================================

def analyze_job(
    title,
    company,
    location,
    description,
    profile
):

    text = (
        title +
        " " +
        company +
        " " +
        location +
        " " +
        description
    ).lower()

    score = 0

    matched_skills = []

    # TITLE
    for target in profile[
        "target_titles"
    ]:

        if target.lower() in title.lower():

            score += 30
            break

    # SKILLS
    skills_found = 0

    for skill in profile[
        "skills"
    ]:

        if skill.lower() in text:

            matched_skills.append(
                skill
            )

            skills_found += 1

    score += min(
        40,
        skills_found * 4
    )

    # EXPERIENCE
    experience_found = 0

    for item in profile[
        "experience"
    ]:

        if item.lower() in text:

            experience_found += 1

    score += min(
        20,
        experience_found * 4
    )

    # LOCATION
    for preferred in profile[
        "preferred_locations"
    ]:

        if preferred.lower() in text:

            score += 10
            break

    # NEGATIVE RULES
    negative_matches = []

    for rule in profile[
        "do_not_apply"
    ]:

        if rule.lower() in text:

            negative_matches.append(
                rule
            )

    # DECISION
    if negative_matches:
        decision = "SKIP"

    elif score >= 70:
        decision = "APPLY"

    elif score >= 45:
        decision = "REVIEW"

    else:
        decision = "SKIP"

    return {
        "score": min(
            score,
            100
        ),

        "decision": decision,

        "matched_skills":
            ", ".join(
                matched_skills[:15]
            ),

        "negative_matches":
            ", ".join(
                negative_matches
            )
    }


# ============================================================
# DATABASE / CSV
# ============================================================

def existing_urls():

    """Return URLs only from structurally valid legacy rows.

    The legacy file is never repaired or rewritten here. Rows with a
    different width are ignored for deduplication and reported by the
    companion audit tool.
    """
    urls = set()

    if not os.path.exists(JOBS_FILE):
        return urls

    fields = [
        "date", "source", "title", "company", "location",
        "score", "decision", "matched_skills", "negative_matches", "url"
    ]

    try:
        with open(JOBS_FILE, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            if header != fields:
                log("WARNING: jobs.csv header differs from expected 10-column schema; deduplication skipped for safety.")
                return urls

            for row in reader:
                if len(row) != len(fields):
                    continue
                url = row[9].strip()
                if url.startswith(("http://", "https://")):
                    urls.add(url)

    except Exception as e:
        log(f"WARNING: Could not safely read existing job URLs: {e}")

    return urls


def save_job(job):

    """Append one schema-checked legacy record; never rewrite existing rows."""
    os.makedirs(DATA_DIR, exist_ok=True)

    fields = [
        "date", "source", "title", "company", "location",
        "score", "decision", "matched_skills", "negative_matches", "url"
    ]

    if set(job.keys()) != set(fields):
        missing = sorted(set(fields) - set(job.keys()))
        extra = sorted(set(job.keys()) - set(fields))
        raise ValueError(f"Refusing to write a non-schema job row. Missing={missing}; extra={extra}")

    file_exists = os.path.exists(JOBS_FILE)
    if file_exists:
        # Fail closed if existing file header is unexpected. Do not append
        # to a damaged/foreign schema and do not attempt automatic repair.
        with open(JOBS_FILE, "r", encoding="utf-8-sig", newline="") as check:
            header = next(csv.reader(check), [])
        if header != fields:
            raise RuntimeError(
                "Refusing to append: jobs.csv header does not match the expected "
                "10-column schema. Run the read-only audit first; source was not changed."
            )

    with open(JOBS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="raise")
        if not file_exists:
            writer.writeheader()
        writer.writerow(job)


# ============================================================
# PERMANENT RUN HISTORY
# ============================================================

def initialize_run_history():

    os.makedirs(
        LOG_DIR,
        exist_ok=True
    )

    fields = [
        "run_id",
        "start_time",
        "end_time",
        "duration_seconds",
        "days_to_scan",
        "source",
        "emails_scanned",
        "alerts_found",
        "jobs_extracted",
        "new_jobs",
        "duplicates",
        "errors",
        "apply",
        "review",
        "skip",
        "status"
    ]

    if not os.path.exists(
        RUN_HISTORY_FILE
    ):

        with open(
            RUN_HISTORY_FILE,
            "w",
            newline="",
            encoding="utf-8"
        ) as f:

            writer = csv.DictWriter(
                f,
                fieldnames=fields
            )

            writer.writeheader()

    return fields


def append_run_history(
    run_data
):

    fields = initialize_run_history()

    with open(
        RUN_HISTORY_FILE,
        "a",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fields,
            extrasaction="ignore"
        )

        writer.writerow(
            run_data
        )


# ============================================================
# PROCESS LINKEDIN ALERT
# ============================================================

def process_linkedin_alert(
    subject,
    body,
    profile,
    known_urls
):

    # --------------------------------------------------------
    # Get ONE URL per LinkedIn Job ID.
    # --------------------------------------------------------

    linkedin_urls = extract_linkedin_job_urls(
        body
    )

    log(
        f"LinkedIn unique job URLs found: "
        f"{len(linkedin_urls)}"
    )

    # --------------------------------------------------------
    # Extract cards separately.
    # --------------------------------------------------------

    cards = extract_linkedin_job_cards(
        body
    )

    log(
        f"LinkedIn candidate cards found: "
        f"{len(cards)}"
    )

    jobs_processed = 0
    duplicates = 0

    if not linkedin_urls:

        log(
            "No valid LinkedIn /jobs/view/<ID> URLs found."
        )

        return {
            "processed": 0,
            "duplicates": 0
        }

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # DO NOT pair cards and URLs by array position.
    #
    # LinkedIn uses multiple URLs for the same job and also
    # contains navigation URLs. Positional pairing caused the
    # original relative/logo problem.
    # --------------------------------------------------------

    # --------------------------------------------------------
    # Build a reusable card pool.
    # --------------------------------------------------------

    unused_cards = list(
        cards
    )

    for url in linkedin_urls:

        url = normalize_url(
            url
        )

        job_id = extract_linkedin_job_id(
            url
        )

        if not job_id:
            continue

        if url in known_urls:

            duplicates += 1

            log(
                f"Duplicate LinkedIn job skipped: "
                f"Job ID {job_id}"
            )

            continue

        # ----------------------------------------------------
        # Start with no assumptions.
        # ----------------------------------------------------

        title = ""
        company = ""
        location = ""

        # ----------------------------------------------------
        # Find the next plausible card.
        #
        # We deliberately do NOT assume card index equals
        # URL index.
        # ----------------------------------------------------

        if unused_cards:

            card = unused_cards.pop(0)

            candidate_title = normalize_space(
                card.get(
                    "job_title",
                    ""
                )
            )

            candidate_company = normalize_space(
                card.get(
                    "company",
                    ""
                )
            )

            candidate_location = normalize_space(
                card.get(
                    "location",
                    ""
                )
            )

            if (
                _valid_linkedin_field(
                    candidate_title
                )
                and
                _valid_linkedin_field(
                    candidate_company
                )
            ):

                title = candidate_title
                company = candidate_company
                location = candidate_location

        # ----------------------------------------------------
        # Fetch actual LinkedIn page.
        # ----------------------------------------------------

        page = fetch_page(
            url
        )

        # ----------------------------------------------------
        # Page becomes fallback only.
        # ----------------------------------------------------

        if not title:

            title = guess_job_title(
                subject,
                "",
                page
            )

        if not company:

            company = guess_company(
                "",
                page
            )

        if not location:

            location = guess_location(
                "",
                page
            )

        # ----------------------------------------------------
        # Final validation.
        #
        # Never save obvious template/navigation text.
        # ----------------------------------------------------

        if title.lower() in INVALID_LINKEDIN_TEXT:
            title = ""

        if company.lower() in INVALID_LINKEDIN_TEXT:
            company = ""

        # ----------------------------------------------------
        # If everything is unavailable, keep the job URL but
        # do not manufacture a title/company.
        # ----------------------------------------------------

        description = (
            body +
            " " +
            page
        )

        analysis = analyze_job(
            title,
            company,
            location,
            description,
            profile
        )

        job = {
            "date":
                datetime.now().strftime(
                    "%Y-%m-%d %H:%M"
                ),

            "source":
                "LinkedIn",

            "title":
                title,

            "company":
                company,

            "location":
                location,

            "score":
                analysis["score"],

            "decision":
                analysis["decision"],

            "matched_skills":
                analysis["matched_skills"],

            "negative_matches":
                analysis["negative_matches"],

            "url":
                url
        }

        save_job(
            job
        )

        known_urls.add(
            url
        )

        jobs_processed += 1

        print()
        print("-" * 60)
        print(
            "Source:    LinkedIn"
        )
        print(
            f"Job ID:    {job_id}"
        )
        print(
            f"Title:     {title or 'Unknown'}"
        )
        print(
            f"Company:   {company or 'Unknown'}"
        )
        print(
            f"Location:  {location or 'Unknown'}"
        )
        print(
            f"Match:     {analysis['score']}/100"
        )
        print(
            f"Decision:  {analysis['decision']}"
        )
        print(
            f"URL:       {url}"
        )

    return {
        "processed": jobs_processed,
        "duplicates": duplicates
    }


# ============================================================
# PROCESS INDEED ALERT
# ============================================================

def process_indeed_alert(
    subject,
    body,
    profile,
    known_urls
):

    urls = extract_job_urls(
        body,
        "Indeed"
    )

    jobs_processed = 0
    duplicates = 0

    for source, url in urls:

        if url in known_urls:

            duplicates += 1
            continue

        log(
            f"New Indeed job URL: {url}"
        )

        page = fetch_page(
            url
        )

        title = guess_job_title(
            subject,
            body,
            page
        )

        company = guess_company(
            body,
            page
        )

        location = guess_location(
            body,
            page
        )

        description = (
            body +
            " " +
            page
        )

        analysis = analyze_job(
            title,
            company,
            location,
            description,
            profile
        )

        job = {
            "date":
                datetime.now().strftime(
                    "%Y-%m-%d %H:%M"
                ),

            "source":
                source,

            "title":
                title,

            "company":
                company,

            "location":
                location,

            "score":
                analysis["score"],

            "decision":
                analysis["decision"],

            "matched_skills":
                analysis["matched_skills"],

            "negative_matches":
                analysis["negative_matches"],

            "url":
                url
        }

        save_job(
            job
        )

        known_urls.add(
            url
        )

        jobs_processed += 1

        print()
        print("-" * 60)
        print(
            f"Source:    {source}"
        )
        print(
            f"Title:     {title or 'Unknown'}"
        )
        print(
            f"Company:   {company or 'Unknown'}"
        )
        print(
            f"Location:  {location or 'Unknown'}"
        )
        print(
            f"Match:     {analysis['score']}/100"
        )
        print(
            f"Decision:  {analysis['decision']}"
        )
        print(
            f"URL:       {url}"
        )

    return {
        "processed": jobs_processed,
        "duplicates": duplicates
    }


# ============================================================
# COMMAND LINE
# ============================================================

def parse_arguments():

    parser = argparse.ArgumentParser(
        description="Personal Job Search Agent"
    )

    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of days to scan"
    )

    parser.add_argument(
        "--source",
        choices=[
            "LinkedIn",
            "Indeed",
            "Both"
        ],
        default="Both",
        help="Email source to scan"
    )

    return parser.parse_args()


# ============================================================
# MAIN
# ============================================================

def main():

    args = parse_arguments()

    print()
    print("=" * 60)
    print(
        "       PERSONAL JOB SEARCH AGENT"
    )
    print("=" * 60)
    print()

    os.makedirs(
        DATA_DIR,
        exist_ok=True
    )

    os.makedirs(
        LOG_DIR,
        exist_ok=True
    )

    config = load_json(
        CONFIG_FILE,
        {
            "yahoo_email": "",
            "yahoo_app_password": "",
            "days_to_scan": 7
        }
    )

    profile = load_json(
        PROFILE_FILE,
        DEFAULT_PROFILE
    )

    days_to_scan = (
        args.days
        if args.days is not None
        else int(
            config.get(
                "days_to_scan",
                7
            )
        )
    )

    if days_to_scan < 1:
        days_to_scan = 7

    source = args.source

    run_id = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    start_time = datetime.now()

    emails_scanned = 0
    alerts_found = 0
    jobs_extracted = 0
    new_jobs = 0
    duplicates = 0
    errors = 0
    apply_count = 0
    review_count = 0
    skip_count = 0
    status = "SUCCESS"

    if not config.get(
        "yahoo_email"
    ):

        print()
        print("ERROR:")
        print(
            "Please enter your Yahoo email"
        )
        print(
            "and Yahoo App Password in config.json"
        )
        print()

        return

    log(
        f"Starting job search agent | "
        f"Run ID: {run_id} | "
        f"Days: {days_to_scan} | "
        f"Source: {source}"
    )

    initialize_run_history()

    # ========================================================
    # CONNECT TO YAHOO
    # ========================================================

    try:

        mail = connect_yahoo(
            config
        )

        log(
            "Connected to Yahoo Mail"
        )

    except Exception as e:

        status = "ERROR"
        errors += 1

        log(
            f"Yahoo connection failed: {e}"
        )

        start_end = datetime.now()

        append_run_history(
            {
                "run_id":
                    run_id,

                "start_time":
                    start_time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),

                "end_time":
                    start_end.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),

                "duration_seconds":
                    round(
                        (
                            start_end -
                            start_time
                        ).total_seconds(),
                        2
                    ),

                "days_to_scan":
                    days_to_scan,

                "source":
                    source,

                "emails_scanned":
                    0,

                "alerts_found":
                    0,

                "jobs_extracted":
                    0,

                "new_jobs":
                    0,

                "duplicates":
                    0,

                "errors":
                    errors,

                "apply":
                    0,

                "review":
                    0,

                "skip":
                    0,

                "status":
                    status
            }
        )

        print()
        print(
            "Could not connect to Yahoo Mail."
        )
        print()

        return

    # ========================================================
    # SCAN EMAIL
    # ========================================================

    try:

        status_select, _ = mail.select(
            "INBOX"
        )

        if status_select != "OK":

            raise RuntimeError(
                "Could not select Yahoo INBOX"
            )

        since_date = (
            datetime.now() -
            timedelta(
                days=days_to_scan
            )
        ).strftime(
            "%d-%b-%Y"
        )

        status_search, data = mail.search(
            None,
            "SINCE",
            since_date
        )

        if status_search != "OK":

            raise RuntimeError(
                "Could not search Yahoo INBOX"
            )

        message_ids = data[0].split()

        log(
            f"Messages found in {days_to_scan}-day "
            f"window: {len(message_ids)}"
        )

        known_urls = existing_urls()

        # ----------------------------------------------------
        # Newest first.
        # ----------------------------------------------------

        for message_id in reversed(
            message_ids
        ):

            emails_scanned += 1

            try:

                status_fetch, msg_data = mail.fetch(
                    message_id,
                    "(RFC822)"
                )

                if status_fetch != "OK":
                    continue

                raw_email = msg_data[0][1]

                msg = email.message_from_bytes(
                    raw_email
                )

                subject = decode_email_subject(
                    msg.get(
                        "Subject",
                        ""
                    )
                )

                sender = msg.get(
                    "From",
                    ""
                )

                body = get_email_body(
                    msg
                )

                if not is_job_alert(
                    sender,
                    subject,
                    body
                ):
                    continue

                lower_sender_subject = (
                    sender +
                    " " +
                    subject
                ).lower()

                lower_body = body.lower()

                is_linkedin = (
                    "linkedin" in lower_sender_subject
                    or "linkedin.com" in lower_body
                )

                is_indeed = (
                    "indeed" in lower_sender_subject
                    or "indeed.com" in lower_body
                )

                if source == "LinkedIn" and not is_linkedin:
                    continue

                if source == "Indeed" and not is_indeed:
                    continue

                alerts_found += 1

                log(
                    f"Job alert found: {subject}"
                )

                if is_linkedin:

                    result = process_linkedin_alert(
                        subject,
                        body,
                        profile,
                        known_urls
                    )

                elif is_indeed:

                    result = process_indeed_alert(
                        subject,
                        body,
                        profile,
                        known_urls
                    )

                else:
                    continue

                processed = result[
                    "processed"
                ]

                duplicates += result[
                    "duplicates"
                ]

                jobs_extracted += processed
                new_jobs += processed

            except Exception as e:

                errors += 1

                log(
                    f"Error processing email: {e}"
                )

    except Exception as e:

        status = "ERROR"
        errors += 1

        log(
            f"Scan failed: {e}"
        )

    finally:

        try:
            mail.logout()

        except Exception:
            pass

    # ========================================================
    # FINAL COUNTS
    # ========================================================

    if os.path.exists(
        JOBS_FILE
    ):

        try:

            with open(
                JOBS_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                reader = csv.DictReader(
                    f
                )

                for row in reader:

                    decision = (
                        row.get(
                            "decision",
                            ""
                        )
                        .strip()
                        .upper()
                    )

                    if decision == "APPLY":
                        apply_count += 1

                    elif decision == "REVIEW":
                        review_count += 1

                    elif decision == "SKIP":
                        skip_count += 1

        except Exception:
            pass

    end_time = datetime.now()

    duration = round(
        (
            end_time -
            start_time
        ).total_seconds(),
        2
    )

    # ========================================================
    # PERMANENT RUN HISTORY
    # ========================================================

    append_run_history(
        {
            "run_id":
                run_id,

            "start_time":
                start_time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),

            "end_time":
                end_time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),

            "duration_seconds":
                duration,

            "days_to_scan":
                days_to_scan,

            "source":
                source,

            "emails_scanned":
                emails_scanned,

            "alerts_found":
                alerts_found,

            "jobs_extracted":
                jobs_extracted,

            "new_jobs":
                new_jobs,

            "duplicates":
                duplicates,

            "errors":
                errors,

            "apply":
                apply_count,

            "review":
                review_count,

            "skip":
                skip_count,

            "status":
                status
        }
    )

    # ========================================================
    # FINAL DISPLAY
    # ========================================================

    print()
    print("=" * 60)
    print(
        "JOB SEARCH AGENT RUN COMPLETE"
    )
    print("=" * 60)

    print(
        f"Run ID:          {run_id}"
    )

    print(
        f"Status:          {status}"
    )

    print(
        f"Search window:   {days_to_scan} days"
    )

    print(
        f"Source:          {source}"
    )

    print(
        f"Emails scanned:  {emails_scanned}"
    )

    print(
        f"Alerts found:    {alerts_found}"
    )

    print(
        f"Jobs extracted:  {jobs_extracted}"
    )

    print(
        f"New jobs:        {new_jobs}"
    )

    print(
        f"Duplicates:      {duplicates}"
    )

    print(
        f"Errors:          {errors}"
    )

    print(
        f"Duration:        {duration} seconds"
    )

    print()

    print(
        "Job database:"
    )

    print(
        JOBS_FILE
    )

    print()

    print(
        "Run history:"
    )

    print(
        RUN_HISTORY_FILE
    )

    print("=" * 60)
    print()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()