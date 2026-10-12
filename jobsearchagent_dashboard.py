#!/usr/bin/env python3
"""JobSearchAgent — simple, local-only dashboard prototype.

Run: python3 jobsearchagent_dashboard.py
Open: http://127.0.0.1:8765
Writes only to ~/JobSearchAgent/data_v2; it never reads or changes the legacy jobs.csv.
"""
from __future__ import annotations

import json
import os
import re
import threading
import webbrowser
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from job_sources import AdzunaSourceError, fetch_adzuna_jobs, merge_live_jobs

HOST = "127.0.0.1"
PORT = 8765
DATA_DIR = Path(os.environ.get("JOBSEARCHAGENT_DATA_DIR", str(Path.home() / "JobSearchAgent" / "data_v2")))
JOBS_FILE = DATA_DIR / "jobs.json"
APPLICATIONS_FILE = DATA_DIR / "applications.json"
PROFILE_FILE = DATA_DIR / "profile.json"
LOCK = threading.RLock()
# Source credentials stay in memory only; they are never written to a file.
SOURCE_CREDENTIALS = {"adzuna_app_id": "", "adzuna_app_key": ""}
LAST_SOURCE_RESULT = {"kind": "", "message": ""}

EXCLUSIONS = {
    "unpaid / volunteer": r"\bunpaid\b|\bvolunteer\b|\bvoluntary\b",
    "commission-only": r"\bcommission[- ]only\b",
    "US citizenship requirement": r"\bU\.?S\.? citizen(ship)? required\b|\bmust be a U\.?S\.? citizen\b",
    "security clearance": r"\bsecurity clearance\b|\bclassified clearance\b",
    "relocation required": r"\brelocation required\b|\bmust relocate\b",
}
TARGET_TERMS = (
    "market research", "data analyst", "data analysis", "business analyst",
    "marketing analytics", "project coordinator", "software qa", "quality assurance",
    "reporting", "mis analyst", "operations analyst", "business development",
    "data quality", "research operations", "cybersecurity analyst",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_store() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for file_path in (JOBS_FILE, APPLICATIONS_FILE):
        if not file_path.exists():
            file_path.write_text("[]\n", encoding="utf-8")


def read_records(file_path: Path) -> list[dict]:
    ensure_store()
    try:
        records = json.loads(file_path.read_text(encoding="utf-8"))
        return records if isinstance(records, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_records(file_path: Path, records: list[dict]) -> None:
    ensure_store()
    temporary = file_path.with_suffix(file_path.suffix + ".tmp")
    temporary.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(file_path)


# Skill concepts are deliberately explicit and conservative. We only claim a match
# when the user has entered that skill or their verified experience provides evidence.
SKILL_ALIASES = {
    "excel": ("excel", "microsoft excel", "ms excel"),
    "sql": ("sql", "sql queries", "structured query language"),
    "data analysis": ("data analysis", "data analytics", "analyze data", "analyzing data", "analyse data", "analysing data"),
    "reporting": ("reporting", "reports", "report development", "business reporting"),
    "power bi": ("power bi", "powerbi"),
    "tableau": ("tableau",),
    "python": ("python",),
    "market research": ("market research", "market intelligence", "consumer research", "customer research"),
    "business analysis": ("business analysis", "business analyst", "requirements gathering", "business requirements"),
    "project coordination": ("project coordination", "project coordinator", "project management", "project delivery"),
    "quality assurance": ("quality assurance", "software qa", "software testing", "testing"),
    "data quality": ("data quality", "data validation", "data cleansing", "data cleaning"),
    "stakeholder management": ("stakeholder management", "stakeholder engagement", "stakeholder communication"),
    "financial analysis": ("financial analysis", "financial reporting", "financial modeling", "financial modelling"),
    "crm": ("crm", "customer relationship management", "salesforce"),
    "google analytics": ("google analytics", "ga4"),
    "jira": ("jira",),
    "agile/scrum": ("agile", "scrum"),
    "cybersecurity": ("cybersecurity", "cyber security", "information security", "security monitoring"),
    "risk analysis": ("risk analysis", "risk assessment"),
    "statistics": ("statistical analysis", "statistics", "statistical methods"),
    "powerpoint": ("powerpoint", "power point"),
    "sharepoint": ("sharepoint",),
}


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    result = []
    for item in values:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in result:
            result.append(cleaned[:200])
    return result


def _normalise_text(value: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", " ", str(value or "").lower()).strip()


def _contains_term(text: str, term: str) -> bool:
    normalized_text = " " + _normalise_text(text) + " "
    normalized_term = " " + _normalise_text(term) + " "
    return bool(normalized_term.strip()) and normalized_term in normalized_text


def _concepts(text: str) -> set[str]:
    found = set()
    for concept, aliases in SKILL_ALIASES.items():
        if any(_contains_term(text, alias) for alias in aliases):
            found.add(concept)
    return found


def load_candidate_profile() -> dict:
    """Load only user-entered verified evidence from the new local profile file."""
    if not PROFILE_FILE.exists():
        return {"verified_skills": [], "verified_experience": []}
    try:
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"verified_skills": [], "verified_experience": []}
    if not isinstance(data, dict):
        return {"verified_skills": [], "verified_experience": []}
    return {
        "verified_skills": _as_list(data.get("verified_skills", [])),
        "verified_experience": _as_list(data.get("verified_experience", [])),
        "updated_at": str(data.get("updated_at", "")),
    }


def save_candidate_profile(profile: dict) -> None:
    """Write the verified profile atomically, separate from jobs and applications."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "verified_skills": _as_list(profile.get("verified_skills", []))[:100],
        "verified_experience": _as_list(profile.get("verified_experience", []))[:100],
        "updated_at": now_iso(),
    }
    temporary = PROFILE_FILE.with_suffix(PROFILE_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(PROFILE_FILE)


def _requirements_for(job: dict) -> tuple[list[str], str]:
    explicit = _as_list(job.get("required_skills", job.get("verified_requirements", [])))
    if explicit:
        return explicit, "entered from posting"
    # Old records have no dedicated requirement field. Detect only known terms
    # literally present in the posting and visibly label them as detected/inferred.
    job_text = " ".join(str(job.get(key, "")) for key in ("title", "description"))
    detected = [concept for concept, aliases in SKILL_ALIASES.items()
                if any(_contains_term(job_text, alias) for alias in aliases)]
    return detected, "detected from posting — confirm" if detected else "not supplied"


def _requirement_match(requirement: str, skill_texts: list[str], experience_texts: list[str]) -> tuple[bool, bool]:
    """Return (supported by skills, supported by verified experience)."""
    req_concepts = _concepts(requirement)
    if req_concepts:
        skill_concepts = set().union(*(_concepts(item) for item in skill_texts)) if skill_texts else set()
        exp_concepts = set().union(*(_concepts(item) for item in experience_texts)) if experience_texts else set()
        skill_match = req_concepts.issubset(skill_concepts)
        exp_match = req_concepts.issubset(exp_concepts)
        # For multi-concept labels (e.g. "Excel reporting"), combined evidence can
        # satisfy the requirement when skills and experience together cover it.
        combined_match = req_concepts.issubset(skill_concepts | exp_concepts)
        if combined_match and not skill_match and not exp_match:
            skill_match = bool(req_concepts & skill_concepts)
            exp_match = bool(req_concepts & exp_concepts)
        return skill_match, exp_match

    req_norm = _normalise_text(requirement)
    if not req_norm:
        return False, False
    def text_matches(values):
        for value in values:
            value_norm = _normalise_text(value)
            if req_norm and (req_norm in (" " + value_norm + " ") or
                             (len(req_norm.split()) >= 2 and set(req_norm.split()).issubset(set(value_norm.split())))):
                return True
        return False
    return text_matches(skill_texts), text_matches(experience_texts)


def score_job_details(job: dict, profile: dict | None = None) -> dict:
    """Explainable requirement-to-evidence scoring. Not a hiring prediction."""
    searchable = " ".join(str(job.get(key, "")) for key in ("title", "company", "location", "description"))
    exclusions = [name for name, pattern in EXCLUSIONS.items() if re.search(pattern, searchable, re.I)]
    candidate = profile if isinstance(profile, dict) else load_candidate_profile()
    profile_skills = _as_list(candidate.get("verified_skills", []))
    profile_experience = _as_list(candidate.get("verified_experience", []))
    # Keep support for the older job form: users may have recorded role-specific
    # candidate skills in this field before the global profile editor existed.
    role_skills = _as_list(job.get("verified_skills", []))
    candidate_skills = list(dict.fromkeys(profile_skills + role_skills))
    evidence_exists = bool(candidate_skills or profile_experience)
    requirements, requirements_source = _requirements_for(job)
    matched, gaps, experience_matches, evidence_sources = [], [], [], {}

    for requirement in requirements:
        skill_match, experience_match = _requirement_match(requirement, candidate_skills, profile_experience)
        if skill_match or experience_match:
            matched.append(requirement)
            sources = []
            if skill_match:
                sources.append("verified skill")
            if experience_match:
                sources.append("verified experience")
            evidence_sources[requirement] = " + ".join(sources)
        else:
            gaps.append(requirement)
        if experience_match:
            experience_matches.append(requirement)

    has_description = bool(str(job.get("description", "")).strip()) and not bool(job.get("description_is_snippet", False))
    title = str(job.get("title", "")).lower()
    title_fit = any(term in title for term in TARGET_TERMS)
    req_coverage = len(matched) / len(requirements) if requirements else 0.0
    experience_coverage = len(experience_matches) / len(requirements) if requirements else 0.0
    # Weights: requirement evidence 55, experience evidence 20, role family 10,
    # complete job description 15. Conservative caps prevent missing data from
    # receiving a misleadingly high score.
    score = round(req_coverage * 55 + experience_coverage * 20 + (10 if title_fit else 0) + (15 if has_description else 0))
    if not evidence_exists:
        score = min(score, 39)
    if not has_description:
        score = min(score, 49)
    if not requirements:
        score = min(score, 59)
    if exclusions:
        status = "SKIP"
    elif score >= 70 and matched:
        status = "QUALIFIED — REVIEW"
    else:
        status = "NEEDS REVIEW"

    return {
        "score": max(0, min(100, score)),
        "status": status,
        "exclusions": exclusions,
        "requirements": requirements,
        "requirements_source": requirements_source,
        "matched": matched,
        "gaps": gaps,
        "evidence_sources": evidence_sources,
        "experience_matches": experience_matches,
        "has_candidate_evidence": evidence_exists,
        "has_description": has_description,
    }


def score_job(job: dict, profile: dict | None = None) -> tuple[int, str, list[str]]:
    """Backward-compatible score API: (score, status, exclusion list)."""
    result = score_job_details(job, profile)
    return result["score"], result["status"], result["exclusions"]


# Backward compatibility for earlier test or helper files that used this name.
evaluate_job = score_job


def render_page(active: str, body: str) -> str:
    nav_items = [
        ("overview", "Overview"), ("shortlist", "Job Shortlist"),
        ("sources", "Live Job Sources"), ("workspace", "Application Workspace"), ("reports", "Performance & Reports"),
    ]
    nav = "".join(
        f'<a class="nav {"active" if key == active else ""}" href="/{key}">{label}</a>'
        for key, label in nav_items
    )
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JobSearchAgent</title><style>
:root{{--bg:#f3f6fb;--ink:#17243a;--muted:#66748b;--line:#dfe6ef;--blue:#2158c5;--card:#fff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
header{{background:#132744;color:white;padding:20px 28px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}}header h1{{margin:0;font-size:22px}}header p{{margin:3px 0 0;color:#cbd7e8}}.pill{{border:1px solid #6e88aa;border-radius:999px;padding:5px 10px;font-size:12px}}
.layout{{display:grid;grid-template-columns:220px 1fr;min-height:calc(100vh - 86px)}}aside{{padding:20px 12px;background:#eaf0f8;border-right:1px solid var(--line)}}.nav{{display:block;padding:11px 13px;margin:4px 0;border-radius:9px;color:#33435b;text-decoration:none;font-weight:600}}.nav.active,.nav:hover{{background:#d7e5ff;color:#1748a8}}
main{{padding:26px;max-width:1400px;width:100%;margin:0 auto}}h2{{margin:0 0 5px;font-size:25px}}.sub{{color:var(--muted);margin:0 0 20px}}.cards{{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:14px;margin:18px 0}}
.card,.panel{{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:18px;box-shadow:0 3px 12px #17365b08}}.metric{{font-size:28px;font-weight:750;margin-top:6px}}.label{{color:var(--muted);font-size:13px}}.panel{{margin:16px 0}}.panel h3{{margin:0 0 14px;font-size:17px}}
button{{background:var(--blue);color:white;border:0;border-radius:8px;padding:10px 14px;font-weight:650;cursor:pointer}}input,select,textarea{{display:block;visibility:visible;opacity:1;font:inherit;width:100%;min-height:46px;padding:10px 11px;border:2px solid #8b9db4;border-radius:8px;background:#fff;color:var(--ink);box-shadow:inset 0 1px 2px #17243a12}}textarea{{min-height:112px;resize:vertical}}label{{display:block;font-size:13px;font-weight:700;margin:11px 0 5px;color:var(--ink)}}input:focus,select:focus,textarea:focus{{outline:3px solid #b9d0ff;border-color:var(--blue)}}.formgrid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.wide{{grid-column:1/-1}}
table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{padding:11px 9px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}.tablewrap{{overflow:auto}}.tag{{display:inline-block;padding:3px 8px;border-radius:99px;background:#e7efff;color:#244da0;font-size:12px;font-weight:700}}.warning{{background:#fff7df;border:1px solid #f0dda1;border-radius:9px;padding:12px;color:#71541a}}.empty{{padding:24px;text-align:center;color:var(--muted)}}
@media(max-width:800px){{.layout{{grid-template-columns:1fr}}aside{{display:flex;overflow:auto;padding:8px}}.nav{{white-space:nowrap}}main{{padding:16px}}.cards{{grid-template-columns:repeat(2,minmax(0,1fr))}}.formgrid{{grid-template-columns:1fr}}.wide{{grid-column:auto}}}}
</style></head><body><header><div><h1>JobSearchAgent <span style="color:#8fb7ff">v2</span></h1><p>Local job intelligence · evidence first · you stay in control</p></div><span class="pill">LOCAL ONLY · NO AUTO-APPLY</span></header>
<div class="layout"><aside>{nav}</aside><main>{body}<p class="label" style="margin-top:28px">Simple is the best. Prototype data is separate from legacy files.</p></main></div></body></html>'''


def metric(label: str, value, note: str = "") -> str:
    return f'<div class="card"><div class="label">{escape(label)}</div><div class="metric">{escape(str(value))}</div><div class="label">{escape(note)}</div></div>'


def jobs():
    with LOCK:
        return read_records(JOBS_FILE)


def applications():
    with LOCK:
        return read_records(APPLICATIONS_FILE)


def overview_page() -> str:
    job_records, app_records = jobs(), applications()
    candidate = load_candidate_profile()
    qualified = sum(1 for item in job_records if (lambda result: result[0] >= 70 and not result[2])(score_job(item)))
    followups = sum(1 for item in app_records if item.get("status") in ("Follow-up due", "Interview"))
    body = '<h2>Overview</h2><p class="sub">Your search at a glance. Match scores use only the job details and verified evidence you enter.</p><div class="cards">'
    body += metric("Jobs captured", len(job_records), "Manually entered and verified")
    body += metric("Qualified for review", qualified, "Score ≥ 70; no detected exclusion")
    body += metric("Applications tracked", len(app_records), "No automatic submissions")
    body += metric("Follow-up / interview", followups, "Items needing attention")
    body += '</div>'
    skills_value = escape(", ".join(candidate.get("verified_skills", [])))
    experience_value = escape("\n".join(candidate.get("verified_experience", [])))
    body += (
        '<div class="panel"><h3>Your verified profile evidence</h3>'
        '<p class="label">Enter only skills and experience supported by your real background. This is stored locally in data_v2/profile.json.</p>'
        '<form method="post" action="/save-profile"><div class="formgrid">'
        f'<div class="wide"><label for="profile_skills">Confirmed skills (comma-separated)</label><input id="profile_skills" name="profile_skills" value="{skills_value}" placeholder="Excel, SQL, market research"></div>'
        f'<div class="wide"><label for="profile_experience">Verified experience evidence (one statement per line)</label><textarea id="profile_experience" name="profile_experience" placeholder="Prepared weekly Excel reports\nConducted customer research">{experience_value}</textarea></div>'
        '<div class="wide"><button type="submit">Save verified profile</button></div></div></form>'
        '</div>'
    )
    body += '<div class="panel"><h3>Next actions</h3><ol><li>Save your confirmed skills and experience in the profile section.</li><li>Add a job and enter its required skills from the posting.</li><li>Review matched evidence, gaps, and any detected exclusions.</li><li>Track only jobs you choose and prepare drafts for your own review.</li></ol></div>'
    body += '<div class="warning"><b>Prototype note:</b> Scores compare entered job requirements with your verified skills and experience. Missing requirements, profile evidence, or job descriptions lower or cap the score. A score is not a hiring prediction.</div>'
    return render_page("overview", body)


def shortlist_page() -> str:
    records = jobs()
    body = '<h2>Job Shortlist</h2><p class="sub">Start a new shortlist with jobs you have checked. For live imports, use <a href="/sources">Live Job Sources</a>. Enter required skills for the clearest match score; detected terms are labelled for review.</p><div class="panel"><h3>Add a job</h3><form method="post" action="/add-job"><div class="formgrid">'
    for key, label, kind in [("title", "Job title", "text"), ("company", "Company", "text"), ("location", "Location", "text"), ("source_url", "Source URL", "url"), ("posting_date", "Posting date (YYYY-MM-DD)", "text"), ("work_type", "Work type", "text")]:
        required = "required" if key in ("title", "company") else ""
        body += f'<div><label for="{key}">{label}</label><input id="{key}" name="{key}" type="{kind}" {required}></div>'
    body += '<div class="wide"><label for="description">Verified job description / requirements</label><textarea id="description" name="description" placeholder="Paste job information you checked. Do not enter private credentials."></textarea></div><div class="wide"><label for="required_skills">Required skills stated in the job posting (comma-separated)</label><input id="required_skills" name="required_skills" placeholder="Excel, SQL, Power BI"></div><div class="wide"><label for="verified_skills">Your confirmed skills relevant to this job (optional)</label><input id="verified_skills" name="verified_skills" placeholder="Excel, market research"></div><div class="wide"><button type="submit">Save job</button></div></div></form></div><div class="panel"><h3>Saved jobs</h3>'
    if not records:
        body += '<div class="empty">No jobs saved yet. Add a verified role above to populate this table.</div>'
    else:
        body += '<div class="tablewrap"><table><thead><tr><th>Role / Company</th><th>Location</th><th>Score</th><th>Decision support</th><th>Evidence / gaps</th><th>Source</th></tr></thead><tbody>'
        for item in sorted(records, key=lambda row: row.get("created_at", ""), reverse=True):
            details = score_job_details(item)
            score, status, exclusions = details["score"], details["status"], details["exclusions"]
            source = f'<a href="{escape(item.get("source_url", ""), quote=True)}" target="_blank" rel="noopener">Open source</a>' if item.get("source_url", "").startswith(("https://", "http://")) else "Not supplied"
            exclusion_note = " · Exclusion: " + ", ".join(exclusions) if exclusions else ""
            evidence_lines = []
            if details["matched"]:
                evidence_lines.append("Matched: " + ", ".join(details["matched"]))
            if details["gaps"]:
                evidence_lines.append("Gaps: " + ", ".join(details["gaps"]))
            if not details["requirements"]:
                evidence_lines.append("Requirements not supplied; score capped")
            elif details["requirements_source"] != "entered from posting":
                evidence_lines.append("Detected terms — confirm")
            if not details["has_candidate_evidence"]:
                evidence_lines.append("Save verified profile first")
            evidence_html = "<br>".join(escape(line) for line in evidence_lines) or "No matching evidence yet"
            body += f'<tr><td><b>{escape(item.get("title", ""))}</b><br>{escape(item.get("company", ""))}<br><span class="label">{escape(item.get("posting_date", "") or "Posting date unknown")}</span></td><td>{escape(item.get("location", "Unknown"))}</td><td><span class="tag">{score}/100</span></td><td>{escape(status + exclusion_note)}</td><td>{evidence_html}</td><td>{source}</td></tr>'
        body += '</tbody></table></div>'
    body += '</div>'
    return render_page("shortlist", body)



def live_sources_page(message: str = "") -> str:
    configured = bool(SOURCE_CREDENTIALS.get("adzuna_app_id") and SOURCE_CREDENTIALS.get("adzuna_app_key"))
    queries = "\n".join([
        "data analyst", "market research", "business analyst", "marketing analytics",
        "reporting analyst", "project coordinator",
    ])
    body = '<h2>Live Job Sources</h2><p class="sub">Fetch current published job ads through an official API. Imports are saved to this dashboard\'s current data folder; nothing is auto-applied.</p>'
    if message:
        body += f'<div class="warning">{escape(message)}</div>'
    if LAST_SOURCE_RESULT.get("message") and not message:
        style = "warning" if LAST_SOURCE_RESULT.get("kind") == "error" else "panel"
        body += f'<div class="{style}">{escape(LAST_SOURCE_RESULT["message"])}</div>'
    body += '<div class="panel"><h3>Source: Adzuna Jobs API</h3><p>Adzuna provides a searchable jobs API. A free/approved API account and API ID/key are required. Credentials entered here are held in memory for this dashboard process only and are not saved to disk.</p>'
    status = "Configured for this running session" if configured else "Not configured for this running session"
    body += f'<p><span class="tag">{escape(status)}</span></p>'
    body += '<p><a href="https://developer.adzuna.com/overview" target="_blank" rel="noopener">Adzuna API documentation and registration</a></p>'
    body += '<form method="post" action="/save-adzuna-credentials"><div class="formgrid">'
    body += '<div><label for="adzuna_app_id">Adzuna App ID</label><input id="adzuna_app_id" name="adzuna_app_id" type="text" autocomplete="off" spellcheck="false" placeholder="Paste your App ID"></div>'
    body += '<div><label for="adzuna_app_key">Adzuna App Key</label><input id="adzuna_app_key" name="adzuna_app_key" type="password" autocomplete="new-password" placeholder="Paste your App Key"></div>'
    body += '<div class="wide"><button type="submit">Use credentials for this session</button></div></div></form>'
    body += '<p class="label">Stopping/restarting the dashboard clears these in-memory credentials; enter them again when needed. Do not send API keys in chat or put them into a project file.</p></div>'
    body += '<div class="panel"><h3>Fetch live jobs</h3><p>Enter one search phrase per line. The first live adapter is Adzuna Canada. A maximum of 8 queries and 20 results per query is allowed per fetch to keep requests bounded.</p>'
    body += '<form method="post" action="/fetch-live-jobs"><div class="formgrid">'
    body += f'<div class="wide"><label for="queries">Job search phrases (one per line; maximum 8)</label><textarea id="queries" name="queries" required>{escape(queries)}</textarea></div>'
    body += '<div><label for="location">Location</label><input id="location" name="location" value="Toronto" placeholder="Toronto, Remote Canada, or Canada-wide"></div>'
    body += '<div><label for="per_query">Results per search phrase</label><select id="per_query" name="per_query"><option value="5">5</option><option value="10" selected>10</option><option value="20">20</option></select></div>'
    body += '<div class="wide"><button type="submit">Fetch live jobs now</button></div></div></form></div>'
    body += '<div class="warning"><b>Source boundaries:</b> this uses the provider API, not automated scraping of LinkedIn or Indeed. LinkedIn prohibits scraping in its User Agreement; LinkedIn integrations require permitted product/API access. Job Bank XML access is request-based and intended for eligible job-board operators. Additional authorized adapters can be added later.</div>'
    return render_page("sources", body)


def workspace_page() -> str:
    job_records, app_records = jobs(), applications()
    body = '<h2>Application Workspace</h2><p class="sub">Track choices and follow-ups. Nothing is submitted automatically.</p><div class="panel"><h3>Track a job</h3><form method="post" action="/add-application"><div class="formgrid"><div><label>Job</label><select name="job_id" required><option value="">Choose a saved job…</option>'
    for item in job_records:
        body += f'<option value="{escape(item.get("id", ""), quote=True)}">{escape(item.get("title", ""))} — {escape(item.get("company", ""))}</option>'
    body += '</select></div><div><label>Status</label><select name="status"><option>Considering</option><option>Preparing</option><option>Applied</option><option>Follow-up due</option><option>Interview</option><option>Rejected</option><option>Offer</option><option>Withdrawn</option></select></div><div><label>Applied date</label><input name="applied_date" placeholder="YYYY-MM-DD"></div><div><label>Follow-up date</label><input name="followup_date" placeholder="YYYY-MM-DD"></div><div class="wide"><label>Notes / next action</label><textarea name="notes" placeholder="Next action or interview notes…"></textarea></div><div class="wide"><button type="submit">Add to tracker</button></div></div></form></div><div class="panel"><h3>Application tracker</h3>'
    if not app_records:
        body += '<div class="empty">No applications tracked yet. Add a job to the shortlist first.</div>'
    else:
        by_id = {item.get("id"): item for item in job_records}
        body += '<div class="tablewrap"><table><thead><tr><th>Role</th><th>Status</th><th>Applied</th><th>Follow-up</th><th>Notes</th></tr></thead><tbody>'
        for record in sorted(app_records, key=lambda row: row.get("updated_at", ""), reverse=True):
            job = by_id.get(record.get("job_id"), {})
            body += f'<tr><td><b>{escape(job.get("title", "Unknown job"))}</b><br>{escape(job.get("company", ""))}</td><td><span class="tag">{escape(record.get("status", "Considering"))}</span></td><td>{escape(record.get("applied_date", "") or "—")}</td><td>{escape(record.get("followup_date", "") or "—")}</td><td>{escape(record.get("notes", "") or "—")}</td></tr>'
        body += '</tbody></table></div>'
    body += '</div><div class="warning">Use confirmed resume facts in application drafts. Unknown facts must stay as placeholders.</div>'
    return render_page("workspace", body)


def reports_page() -> str:
    job_records, app_records = jobs(), applications()
    bands = {"90–100 Excellent": 0, "80–89 Strong": 0, "70–79 Qualified": 0, "Below 70": 0, "Excluded": 0}
    for item in job_records:
        score, _, exclusions = score_job(item)
        if exclusions: bands["Excluded"] += 1
        elif score >= 90: bands["90–100 Excellent"] += 1
        elif score >= 80: bands["80–89 Strong"] += 1
        elif score >= 70: bands["70–79 Qualified"] += 1
        else: bands["Below 70"] += 1
    body = '<h2>Performance &amp; Reports</h2><p class="sub">Reports use only the new local records.</p><div class="panel"><h3>Job score distribution</h3><table><thead><tr><th>Band</th><th>Jobs</th><th>Share</th></tr></thead><tbody>'
    for label, count in bands.items():
        share = f"{count / len(job_records) * 100:.0f}%" if job_records else "—"
        body += f'<tr><td>{escape(label)}</td><td>{count}</td><td>{share}</td></tr>'
    body += '</tbody></table></div><div class="panel"><h3>Application funnel</h3>'
    statuses = {}
    for record in app_records:
        status = record.get("status", "Considering")
        statuses[status] = statuses.get(status, 0) + 1
    if statuses:
        body += '<table><thead><tr><th>Status</th><th>Count</th></tr></thead><tbody>'
        for status, count in sorted(statuses.items()):
            body += f'<tr><td>{escape(status)}</td><td>{count}</td></tr>'
        body += '</tbody></table>'
    else:
        body += '<div class="empty">Application metrics appear after you track applications.</div>'
    body += '</div><div class="panel"><h3>Reporting notes</h3><ul><li>Response and interview rates will be shown only when outcome data is available.</li><li>Source comparisons need confirmed source information.</li><li>Skill-gap analysis can be added later using real job requirements and verified profile evidence.</li></ul></div>'
    return render_page("reports", body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def respond(self, status: int, html: str):
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        routes = {"/": overview_page, "/overview": overview_page, "/shortlist": shortlist_page, "/sources": live_sources_page, "/workspace": workspace_page, "/reports": reports_page}
        fn = routes.get(urlparse(self.path).path)
        if fn is None:
            self.respond(404, render_page("", '<h2>Page not found</h2><a href="/">Return to Overview</a>'))
            return
        self.respond(200, fn())

    def do_POST(self):
        path = urlparse(self.path).path
        length = min(int(self.headers.get("Content-Length", "0") or 0), 1_000_000)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = {key: value[0].strip() for key, value in parse_qs(raw, keep_blank_values=True).items()}
        if path == "/save-adzuna-credentials":
            app_id = form.get("adzuna_app_id", "").strip()[:200]
            app_key = form.get("adzuna_app_key", "").strip()[:300]
            if not app_id or not app_key:
                LAST_SOURCE_RESULT.update({"kind": "error", "message": "Both Adzuna App ID and App Key are required. Credentials were not changed."})
                self.send_response(303); self.send_header("Location", "/sources"); self.end_headers()
                return
            with LOCK:
                SOURCE_CREDENTIALS["adzuna_app_id"] = app_id
                SOURCE_CREDENTIALS["adzuna_app_key"] = app_key
                LAST_SOURCE_RESULT.update({"kind": "success", "message": "Adzuna credentials accepted in memory for this session only. They were not written to disk."})
            self.send_response(303); self.send_header("Location", "/sources"); self.end_headers()
            return
        if path == "/fetch-live-jobs":
            query_lines = [line.strip() for line in form.get("queries", "").splitlines() if line.strip()]
            location = form.get("location", "").strip()[:120]
            try:
                per_query = int(form.get("per_query", "10"))
            except ValueError:
                per_query = 10
            per_query = max(1, min(20, per_query))
            if not query_lines:
                LAST_SOURCE_RESULT.update({"kind": "error", "message": "Enter at least one search phrase."})
                self.respond(400, live_sources_page("Enter at least one search phrase.")); return
            if len(query_lines) > 8:
                self.respond(400, live_sources_page("Use no more than 8 search phrases per fetch.")); return
            with LOCK:
                app_id = SOURCE_CREDENTIALS.get("adzuna_app_id", "")
                app_key = SOURCE_CREDENTIALS.get("adzuna_app_key", "")
            try:
                fetched = fetch_adzuna_jobs(
                    query_lines, location, per_query,
                    app_id=app_id, app_key=app_key,
                )
                with LOCK:
                    existing = read_records(JOBS_FILE)
                    merged, report = merge_live_jobs(existing, fetched)
                    save_records(JOBS_FILE, merged)
                    LAST_SOURCE_RESULT.update({
                        "kind": "success",
                        "message": (f"Live fetch complete from Adzuna: {report['added']} new job(s), "
                                    f"{report['refreshed']} refreshed, {report['duplicates']} duplicate result(s) ignored. "
                                    f"{report['total_fetched']} result(s) received across {len(query_lines)} query/queries. "
                                    "Review each source listing before applying."),
                    })
                self.send_response(303); self.send_header("Location", "/sources"); self.end_headers()
                return
            except AdzunaSourceError as exc:
                # Exception text is deliberately sanitized by the source adapter; never echo credentials.
                LAST_SOURCE_RESULT.update({"kind": "error", "message": str(exc)})
                self.respond(400, live_sources_page(str(exc))); return
            except Exception:
                LAST_SOURCE_RESULT.update({"kind": "error", "message": "Live fetch failed unexpectedly. Check the source credentials and network connection; API credentials were not saved to disk."})
                self.respond(500, live_sources_page(LAST_SOURCE_RESULT["message"])); return
        if path == "/save-profile":
            skills = [part.strip()[:100] for part in form.get("profile_skills", "").split(",") if part.strip()][:100]
            experience = [line.strip()[:500] for line in form.get("profile_experience", "").splitlines() if line.strip()][:100]
            with LOCK:
                save_candidate_profile({"verified_skills": skills, "verified_experience": experience})
            self.send_response(303); self.send_header("Location", "/overview"); self.end_headers()
            return
        if path == "/add-job":
            title, company = form.get("title", ""), form.get("company", "")
            if not title or not company:
                self.respond(400, render_page("shortlist", '<h2>Title and company are required.</h2><a href="/shortlist">Go back</a>'))
                return
            source_url = form.get("source_url", "")
            if source_url and not source_url.startswith(("https://", "http://")):
                source_url = ""
            record = {
                "id": f"job-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                "title": title[:200], "company": company[:200], "location": form.get("location", "")[:200],
                "source_url": source_url[:1000], "posting_date": form.get("posting_date", "")[:30],
                "work_type": form.get("work_type", "")[:100], "description": form.get("description", "")[:20000],
                "required_skills": [part.strip()[:80] for part in form.get("required_skills", "").split(",") if part.strip()][:50],
                "verified_skills": [part.strip()[:80] for part in form.get("verified_skills", "").split(",") if part.strip()][:50],
                "created_at": now_iso(), "source_method": "manual_verified_entry",
            }
            with LOCK:
                existing = read_records(JOBS_FILE)
                if any(row.get("title", "").casefold() == title.casefold() and row.get("company", "").casefold() == company.casefold() for row in existing):
                    self.respond(409, render_page("shortlist", '<h2>This role and company already exist in this new shortlist.</h2><a href="/shortlist">Return to shortlist</a>'))
                    return
                existing.append(record)
                save_records(JOBS_FILE, existing)
            self.send_response(303); self.send_header("Location", "/shortlist"); self.end_headers()
            return
        if path == "/add-application":
            job_id = form.get("job_id", "")
            if not any(row.get("id") == job_id for row in jobs()):
                self.respond(400, render_page("workspace", '<h2>Choose a saved job first.</h2><a href="/workspace">Go back</a>'))
                return
            record = {"id": f"app-{datetime.now().strftime('%Y%m%d%H%M%S%f')}", "job_id": job_id,
                      "status": form.get("status", "Considering")[:60], "applied_date": form.get("applied_date", "")[:30],
                      "followup_date": form.get("followup_date", "")[:30], "notes": form.get("notes", "")[:3000], "updated_at": now_iso()}
            with LOCK:
                existing = read_records(APPLICATIONS_FILE)
                existing.append(record)
                save_records(APPLICATIONS_FILE, existing)
            self.send_response(303); self.send_header("Location", "/workspace"); self.end_headers()
            return
        self.respond(404, render_page("", '<h2>Unknown action</h2><a href="/">Return to Overview</a>'))


def main():
    ensure_store()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("JobSearchAgent dashboard is running.")
    print("Live source adapter: Adzuna Canada API (credentials remain in memory only).")
    print(f"Open: http://{HOST}:{PORT}")
    print(f"Separate data folder: {DATA_DIR}")
    print("Press Ctrl+C to stop. The legacy jobs.csv is not read or modified.")
    try:
        webbrowser.open(f"http://{HOST}:{PORT}")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
