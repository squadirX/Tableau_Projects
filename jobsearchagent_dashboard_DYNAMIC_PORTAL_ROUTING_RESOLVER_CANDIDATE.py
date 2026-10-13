#!/usr/bin/env python3
"""JobSearchAgent — simple, local-only dashboard prototype.

Run: python3 jobsearchagent_dashboard.py
Open: http://127.0.0.1:8765
Writes only to ~/JobSearchAgent/data_v2; it never reads or changes the legacy jobs.csv.
"""
from __future__ import annotations

import json
import queue as _queue
import os
import re
import threading
import traceback
import webbrowser
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urljoin
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import ipaddress
import time

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
# Visible feedback for each guided portal action; scoped to the selected saved job.
LAST_GUIDED_ACTION_RESULT = {"job_id": "", "action": "", "ok": None, "message": "", "at": ""}

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


def validate_application_dates(applied_date_value: str, followup_date_value: str, today=None):
    """Reject future application dates and past follow-up dates. Empty fields are allowed."""
    if today is None:
        today = datetime.now().astimezone().date()

    parsed_applied = None
    parsed_followup = None
    for label, value in (("Applied date", applied_date_value), ("Follow-up date", followup_date_value)):
        if not value:
            continue
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return f"{label} must be a valid calendar date."
        if label == "Applied date":
            parsed_applied = parsed
        else:
            parsed_followup = parsed

    if parsed_applied is not None and parsed_applied > today:
        return "Applied date cannot be in the future. Choose today or an earlier date."
    if parsed_followup is not None and parsed_followup < today:
        return "Follow-up date cannot be in the past. Choose today or a future date."
    return None


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


DEFAULT_PROCESS_BAR_CSS = r"""
body { padding-bottom: 98px; }
#jsa-process-bar { position:fixed; z-index:10000; left:0; right:0; bottom:0; display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:10px 16px; min-height:76px; background:#ffffff; border-top:1px solid #cbd6e5; box-shadow:0 -5px 18px #17365b18; }
#jsa-process-bar .jsa-footer-title { font-size:13px; font-weight:800; color:#17243a; margin-right:4px; white-space:nowrap; }
#jsa-process-bar label { margin:0; font-size:12px; color:#33435b; }
#jsa-process-bar select { width:auto; min-width:110px; min-height:38px; padding:6px 9px; font-size:13px; }
#jsa-process-bar button { min-height:38px; padding:8px 13px; font-size:13px; white-space:nowrap; }
#jsa-process-bar button:disabled { opacity:.45; cursor:not-allowed; }
#jsa-footer-status { flex:1 1 240px; min-width:160px; color:#66748b; font-size:12px; }
@media(max-width:800px) { body { padding-bottom:180px; } #jsa-process-bar { gap:7px; padding:9px 10px; } #jsa-process-bar .jsa-footer-title { flex-basis:100%; } #jsa-footer-status { flex-basis:100%; } }
@media print { #jsa-process-bar { display:none !important; } body { padding-bottom:0 !important; } }
"""

DEFAULT_PROCESS_BAR_HTML = r"""
<div id="jsa-process-bar" role="region" aria-label="Screenshot and save tools">
  <span class="jsa-footer-title">Page tools</span>
  <label for="jsa-export-format">Format</label>
  <select id="jsa-export-format" aria-label="Screenshot and save format">
    <option value="png">PNG</option>
    <option value="jpg">JPG</option>
    <option value="pdf">PDF</option>
    <option value="webp">WebP</option>
    <option value="svg">SVG</option>
  </select>
  <button type="button" id="jsa-screenshot-button">Screenshot</button>
  <button type="button" id="jsa-save-button" disabled>Save</button>
  <button type="button" id="jsa-location-button">Location Prompt</button>
  <span id="jsa-footer-status" role="status" aria-live="polite">Choose a format and click Screenshot.</span>
</div>
<script>
(function () {
  "use strict";
  const formatControl = document.getElementById("jsa-export-format");
  const screenshotButton = document.getElementById("jsa-screenshot-button");
  const saveButton = document.getElementById("jsa-save-button");
  const locationButton = document.getElementById("jsa-location-button");
  const statusNode = document.getElementById("jsa-footer-status");
  if (!formatControl || !screenshotButton || !saveButton || !locationButton || !statusNode) return;

  let lastBlob = null;
  let lastFileName = "";
  let lastExtension = "";
  let selectedDirectory = null;
  let working = false;

  const FORMATS = {
    png: { mime: "image/png", extension: "png", description: "PNG image" },
    jpg: { mime: "image/jpeg", extension: "jpg", description: "JPEG image" },
    pdf: { mime: "application/pdf", extension: "pdf", description: "PDF document" },
    webp: { mime: "image/webp", extension: "webp", description: "WebP image" },
    svg: { mime: "image/svg+xml", extension: "svg", description: "SVG image" }
  };

  function setStatus(message, isError) {
    statusNode.textContent = message;
    statusNode.style.color = isError ? "#9b2c25" : "#66748b";
  }
  function pageBaseName() {
    const path = window.location.pathname.split("/").filter(Boolean).join("-") || "overview";
    const stamp = new Date().toISOString().replace(/[:.]/g, "-").replace("T", "_").replace("Z", "");
    return "JobSearchAgent-" + path.replace(/[^a-zA-Z0-9_-]/g, "-") + "-" + stamp;
  }
  function xmlEscape(value) {
    return String(value).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function setFormState(source, target) {
    if (source instanceof HTMLInputElement) {
      target.setAttribute("value", source.value);
      if (source.checked) target.setAttribute("checked", "checked"); else target.removeAttribute("checked");
      if (source.indeterminate) target.setAttribute("data-jsa-indeterminate", "true");
    } else if (source instanceof HTMLTextAreaElement) {
      target.textContent = source.value;
    } else if (source instanceof HTMLSelectElement) {
      const targetOptions = target.querySelectorAll("option");
      source.querySelectorAll("option").forEach((option, index) => {
        const copied = targetOptions[index];
        if (!copied) return;
        if (option.selected) copied.setAttribute("selected", "selected"); else copied.removeAttribute("selected");
      });
    } else if (source instanceof HTMLDetailsElement && source.open) {
      target.setAttribute("open", "open");
    }
  }

  function getPageDimensions() {
    const width = Math.ceil(Math.max(document.documentElement.scrollWidth, document.documentElement.clientWidth, document.body.scrollWidth, 1));
    const spacer = Number.parseFloat(window.getComputedStyle(document.body).paddingBottom) || 0;
    const height = Math.ceil(Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, document.body.offsetHeight, 1) - spacer);
    if (width > 16000 || height > 16000 || width * height > 100000000) {
      throw new Error("This page is too large to capture in one image. Try browser Print or capture a smaller page section.");
    }
    return { width: width, height: height };
  }
  function makePageSvg(dimensions) {
    const sourceBody = document.body;
    const clone = sourceBody.cloneNode(true);
    const sourceNodes = [sourceBody, ...sourceBody.querySelectorAll("*")];
    const cloneNodes = [clone, ...clone.querySelectorAll("*")];
    const count = Math.min(sourceNodes.length, cloneNodes.length);
    for (let index = 0; index < count; index += 1) {
      const sourceNode = sourceNodes[index];
      const cloneNode = cloneNodes[index];
      if (!(sourceNode instanceof Element) || !(cloneNode instanceof Element)) continue;
      const computed = window.getComputedStyle(sourceNode);
      const declarations = [];
      for (let propertyIndex = 0; propertyIndex < computed.length; propertyIndex += 1) {
        const property = computed.item(propertyIndex);
        const value = computed.getPropertyValue(property);
        if (value) declarations.push(property + ":" + value + (computed.getPropertyPriority(property) ? " !important" : ""));
      }
      cloneNode.setAttribute("style", declarations.join(";"));
      setFormState(sourceNode, cloneNode);
    }
    clone.querySelector("#jsa-process-bar")?.remove();
    clone.querySelectorAll("script, noscript").forEach(node => node.remove());
    clone.style.setProperty("width", dimensions.width + "px", "important");
    clone.style.setProperty("min-height", dimensions.height + "px", "important");
    clone.style.setProperty("height", "auto", "important");
    clone.style.setProperty("overflow", "visible", "important");
    clone.style.setProperty("position", "static", "important");
    clone.style.setProperty("padding-bottom", "0", "important");
    clone.querySelectorAll(".tablewrap").forEach(node => {
      node.style.setProperty("overflow", "visible", "important");
      node.style.setProperty("max-height", "none", "important");
      node.style.setProperty("height", "auto", "important");
    });
    const bodyMarkup = new XMLSerializer().serializeToString(clone);
    const svgMarkup = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xhtml="http://www.w3.org/1999/xhtml" width="' + dimensions.width + '" height="' + dimensions.height + '" viewBox="0 0 ' + dimensions.width + ' ' + dimensions.height + '"><foreignObject x="0" y="0" width="' + dimensions.width + '" height="' + dimensions.height + '">' + bodyMarkup + '</foreignObject></svg>';
    return new Blob([svgMarkup], { type: "image/svg+xml;charset=utf-8" });
  }
  function visibleElement(element) {
    if (!(element instanceof Element) || element.closest("#jsa-process-bar")) return false;
    for (let current = element; current && current instanceof Element; current = current.parentElement) {
      const style = window.getComputedStyle(current);
      if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return false;
      if (current.tagName === "SCRIPT" || current.tagName === "STYLE" || current.tagName === "NOSCRIPT") return false;
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }
  function cumulativeOpacity(element) {
    let opacity = 1;
    for (let current = element; current && current instanceof Element; current = current.parentElement) {
      const value = Number.parseFloat(window.getComputedStyle(current).opacity);
      if (Number.isFinite(value)) opacity *= value;
    }
    return opacity;
  }
  function rectOnPage(element) {
    const rect = element.getBoundingClientRect();
    const position = window.getComputedStyle(element).position;
    if (position === "fixed") return { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
    return { left: rect.left + window.scrollX, top: rect.top + window.scrollY, width: rect.width, height: rect.height };
  }
  function fillElementBackground(context, element) {
    if (!visibleElement(element)) return;
    const style = window.getComputedStyle(element);
    const box = rectOnPage(element);
    const opacity = cumulativeOpacity(element);
    if (opacity <= 0) return;
    context.save();
    context.globalAlpha = opacity;
    const background = style.backgroundColor;
    if (background && background !== "transparent" && !/^rgba\([^)]*,\s*0\s*\)$/.test(background)) {
      const radius = Math.min(24, Number.parseFloat(style.borderTopLeftRadius) || 0,
        Number.parseFloat(style.borderTopRightRadius) || 0,
        Number.parseFloat(style.borderBottomRightRadius) || 0,
        Number.parseFloat(style.borderBottomLeftRadius) || 0);
      context.fillStyle = background;
      context.beginPath();
      if (typeof context.roundRect === "function" && radius > 0) context.roundRect(box.left, box.top, box.width, box.height, radius);
      else context.rect(box.left, box.top, box.width, box.height);
      context.fill();
    }
    const sides = [
      ["borderTopWidth", "borderTopColor", box.left, box.top, box.width, Math.max(0.5, Number.parseFloat(style.borderTopWidth) || 0)],
      ["borderRightWidth", "borderRightColor", box.left + box.width - (Number.parseFloat(style.borderRightWidth) || 0), box.top, Math.max(0.5, Number.parseFloat(style.borderRightWidth) || 0), box.height],
      ["borderBottomWidth", "borderBottomColor", box.left, box.top + box.height - (Number.parseFloat(style.borderBottomWidth) || 0), box.width, Math.max(0.5, Number.parseFloat(style.borderBottomWidth) || 0)],
      ["borderLeftWidth", "borderLeftColor", box.left, box.top, Math.max(0.5, Number.parseFloat(style.borderLeftWidth) || 0), box.height]
    ];
    for (const side of sides) {
      const widthValue = Number.parseFloat(style[side[0]]) || 0;
      const color = style[side[1]];
      if (widthValue > 0 && color && color !== "transparent") {
        context.fillStyle = color;
        context.fillRect(side[2], side[3], side[4], side[5]);
      }
    }
    context.restore();
  }
  function canvasFont(style) {
    const fontSize = Number.parseFloat(style.fontSize) || 15;
    const fontStyle = style.fontStyle && style.fontStyle !== "normal" ? style.fontStyle + " " : "";
    const fontWeight = style.fontWeight && style.fontWeight !== "normal" ? style.fontWeight + " " : "";
    return { font: fontStyle + fontWeight + fontSize + "px " + (style.fontFamily || "sans-serif"), size: fontSize };
  }
  function transformedText(text, transform) {
    if (transform === "uppercase") return text.toLocaleUpperCase();
    if (transform === "lowercase") return text.toLocaleLowerCase();
    if (transform === "capitalize") return text.replace(/\b\p{L}/gu, part => part.toLocaleUpperCase());
    return text;
  }
  function drawDomText(context, textNode) {
    const element = textNode && textNode.parentElement;
    if (!element || !visibleElement(element)) return;
    const style = window.getComputedStyle(element);
    const opacity = cumulativeOpacity(element);
    if (opacity <= 0) return;
    const font = canvasFont(style);
    const range = document.createRange();
    const text = textNode.nodeValue || "";
    const lines = new Map();
    for (let offset = 0; offset < text.length;) {
      const codePoint = text.codePointAt(offset);
      const character = String.fromCodePoint(codePoint);
      const nextOffset = offset + character.length;
      try {
        range.setStart(textNode, offset);
        range.setEnd(textNode, nextOffset);
        const rect = range.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
          const top = Math.round(rect.top + window.scrollY);
          if (!lines.has(top)) lines.set(top, { text: "", x: rect.left + window.scrollX, top: top, height: rect.height });
          const line = lines.get(top);
          if (!line.text) line.x = rect.left + window.scrollX;
          line.text += character;
          line.height = Math.max(line.height, rect.height);
        }
      } catch (_) { /* A browser-created text node may not have a measurable range. */ }
      offset = nextOffset;
    }
    context.save();
    context.globalAlpha = opacity;
    context.fillStyle = style.color || "#17243a";
    context.font = font.font;
    context.textBaseline = "alphabetic";
    context.textAlign = "left";
    for (const line of lines.values()) {
      const lineText = transformedText(line.text, style.textTransform);
      const lineHeight = Math.max(line.height, font.size);
      const baseline = line.top + Math.max(font.size, (lineHeight - font.size) / 2 + font.size * 0.82);
      context.fillText(lineText, line.x, baseline);
      if (style.textDecorationLine.includes("underline") || style.textDecorationLine.includes("line-through")) {
        const measured = context.measureText(lineText).width;
        context.strokeStyle = style.textDecorationColor && style.textDecorationColor !== "auto" ? style.textDecorationColor : context.fillStyle;
        context.lineWidth = Math.max(1, font.size / 15);
        context.beginPath();
        if (style.textDecorationLine.includes("underline")) {
          context.moveTo(line.x, baseline + Math.max(1, font.size * 0.12));
          context.lineTo(line.x + measured, baseline + Math.max(1, font.size * 0.12));
        }
        if (style.textDecorationLine.includes("line-through")) {
          context.moveTo(line.x, baseline - font.size * 0.32);
          context.lineTo(line.x + measured, baseline - font.size * 0.32);
        }
        context.stroke();
      }
    }
    context.restore();
  }
  function drawFormControls(context, dimensions) {
    const controls = document.querySelectorAll("input, textarea, select");
    for (const element of controls) {
      if (!visibleElement(element)) continue;
      const style = window.getComputedStyle(element);
      const box = rectOnPage(element);
      const type = (element.getAttribute("type") || "text").toLowerCase();
      const opacity = cumulativeOpacity(element);
      context.save();
      context.globalAlpha = opacity;
      if (element instanceof HTMLInputElement && (type === "checkbox" || type === "radio")) {
        const size = Math.min(box.width, box.height, 20);
        const x = box.left + (box.width - size) / 2;
        const y = box.top + (box.height - size) / 2;
        if (type === "radio") {
          context.beginPath(); context.arc(x + size / 2, y + size / 2, size / 2.4, 0, Math.PI * 2);
          context.fillStyle = "#ffffff"; context.fill(); context.strokeStyle = "#596d86"; context.stroke();
          if (element.checked) { context.beginPath(); context.arc(x + size / 2, y + size / 2, size / 4.2, 0, Math.PI * 2); context.fillStyle = "#2158c5"; context.fill(); }
        } else if (element.checked) {
          context.strokeStyle = "#2158c5"; context.lineWidth = 2.3; context.beginPath();
          context.moveTo(x + size * 0.18, y + size * 0.52); context.lineTo(x + size * 0.42, y + size * 0.76); context.lineTo(x + size * 0.84, y + size * 0.23); context.stroke();
        }
        context.restore();
        continue;
      }
      if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement) {
        let value = "";
        if (element instanceof HTMLSelectElement) value = element.selectedIndex >= 0 ? element.options[element.selectedIndex].textContent || "" : "";
        else value = element.value || element.placeholder || "";
        if (element instanceof HTMLTextAreaElement) {
          const font = canvasFont(style); context.font = font.font; context.fillStyle = element.value ? (style.color || "#17243a") : (window.getComputedStyle(element, "::placeholder").color || "#66748b");
          const left = box.left + (Number.parseFloat(style.paddingLeft) || 8);
          const width = Math.max(20, box.width - (Number.parseFloat(style.paddingLeft) || 8) - (Number.parseFloat(style.paddingRight) || 8));
          const words = String(value).split("\n"); let y = box.top + (Number.parseFloat(style.paddingTop) || 8) + font.size;
          for (const paragraph of words) {
            let line = "";
            for (const word of paragraph.split(/\s+/)) {
              const test = line ? line + " " + word : word;
              if (line && context.measureText(test).width > width) { context.fillText(line, left, y); y += font.size * 1.35; line = word; }
              else line = test;
            }
            if (line) context.fillText(line, left, y);
            y += font.size * 1.35;
          }
        } else {
          const font = canvasFont(style); context.font = font.font;
          context.fillStyle = value && element.value ? (style.color || "#17243a") : (element.placeholder ? (window.getComputedStyle(element, "::placeholder").color || "#66748b") : (style.color || "#17243a"));
          const padLeft = Number.parseFloat(style.paddingLeft) || 10;
          const padRight = Number.parseFloat(style.paddingRight) || 10;
          const baseline = box.top + (box.height - font.size) / 2 + font.size * 0.82;
          context.fillText(String(value).replace(/\s+/g, " "), box.left + padLeft, baseline, Math.max(1, box.width - padLeft - padRight));
        }
      }
      context.restore();
    }
  }
  function renderDomToCanvas(dimensions) {
    const canvas = document.createElement("canvas");
    canvas.width = dimensions.width;
    canvas.height = dimensions.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("The browser could not create an image canvas.");
    const bodyStyle = window.getComputedStyle(document.body);
    const rootStyle = window.getComputedStyle(document.documentElement);
    context.fillStyle = bodyStyle.backgroundColor && bodyStyle.backgroundColor !== "rgba(0, 0, 0, 0)" ? bodyStyle.backgroundColor : (rootStyle.backgroundColor || "#ffffff");
    context.fillRect(0, 0, canvas.width, canvas.height);
    const allElements = [document.documentElement, document.body, ...document.body.querySelectorAll("*")];
    for (const element of allElements) fillElementBackground(context, element);
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    let node;
    while ((node = walker.nextNode())) {
      const parent = node.parentElement;
      if (!parent || parent.closest("#jsa-process-bar") || parent.closest("script, style, noscript, option, select, textarea, input")) continue;
      if (!(node.nodeValue || "").trim()) continue;
      textNodes.push(node);
    }
    for (const textNode of textNodes) drawDomText(context, textNode);
    drawFormControls(context, dimensions);
    return canvas;
  }
  function canvasToBlob(canvas, mime, quality) {
    return new Promise((resolve, reject) => {
      canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error("The browser could not create the selected image format.")), mime, quality);
    });
  }
  async function canvasToPdf(canvas) {
    const jpegBlob = await canvasToBlob(canvas, "image/jpeg", 0.92);
    const jpegBytes = new Uint8Array(await jpegBlob.arrayBuffer());
    const encoder = new TextEncoder();
    const chunks = [];
    let byteOffset = 0;
    const offsets = [0, 0, 0, 0, 0, 0];
    function addBytes(bytes) { chunks.push(bytes); byteOffset += bytes.byteLength; }
    function addText(text) { addBytes(encoder.encode(text)); }
    const pageWidth = Math.max(1, canvas.width * 0.75);
    const pageHeight = Math.max(1, canvas.height * 0.75);
    addText("%PDF-1.4\n%\xE2\xE3\xCF\xD3\n");
    offsets[1] = byteOffset; addText("1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n");
    offsets[2] = byteOffset; addText("2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n");
    offsets[3] = byteOffset;
    addText("3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " + pageWidth.toFixed(2) + " " + pageHeight.toFixed(2) + "] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>\nendobj\n");
    offsets[4] = byteOffset;
    addText("4 0 obj\n<< /Type /XObject /Subtype /Image /Width " + canvas.width + " /Height " + canvas.height + " /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length " + jpegBytes.byteLength + " >>\nstream\n");
    addBytes(jpegBytes);
    addText("\nendstream\nendobj\n");
    const content = "q\n" + pageWidth.toFixed(2) + " 0 0 " + pageHeight.toFixed(2) + " 0 0 cm\n/Im0 Do\nQ\n";
    offsets[5] = byteOffset;
    addText("5 0 obj\n<< /Length " + encoder.encode(content).byteLength + " >>\nstream\n" + content + "endstream\nendobj\n");
    const xrefOffset = byteOffset;
    addText("xref\n0 6\n0000000000 65535 f \n");
    for (let index = 1; index <= 5; index += 1) addText(String(offsets[index]).padStart(10, "0") + " 00000 n \n");
    addText("trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + xrefOffset + "\n%%EOF\n");
    return new Blob(chunks, { type: "application/pdf" });
  }
  async function captureSelectedFormat(format) {
    const dimensions = getPageDimensions();
    if (format === "svg") return makePageSvg(dimensions);
    const canvas = renderDomToCanvas(dimensions);
    if (format === "pdf") return await canvasToPdf(canvas);
    const choice = FORMATS[format];
    return await canvasToBlob(canvas, choice.mime, format === "jpg" || format === "webp" ? 0.92 : undefined);
  }
  function ensureFilename(format) {
    const choice = FORMATS[format];
    return pageBaseName() + "." + choice.extension;
  }

  formatControl.addEventListener("change", () => {
    lastBlob = null;
    lastFileName = "";
    lastExtension = "";
    saveButton.disabled = true;
    setStatus("Format changed. Click Screenshot to capture this page in " + formatControl.value.toUpperCase() + ".", false);
  });
  screenshotButton.addEventListener("click", async () => {
    if (working) return;
    working = true;
    screenshotButton.disabled = true;
    saveButton.disabled = true;
    try {
      const format = formatControl.value;
      setStatus("Capturing the full page as " + format.toUpperCase() + "…", false);
      lastBlob = await captureSelectedFormat(format);
      lastExtension = FORMATS[format].extension;
      lastFileName = ensureFilename(format);
      saveButton.disabled = false;
      setStatus("Screenshot ready: " + lastFileName + ". Click Save to choose a location.", false);
    } catch (error) {
      lastBlob = null;
      saveButton.disabled = true;
      const detail = error && error.message ? error.message : "unknown capture error";
      setStatus("Screenshot could not be created: " + detail + " You can use your browser's Print command as a fallback.", true);
    } finally {
      working = false;
      screenshotButton.disabled = false;
    }
  });
  locationButton.addEventListener("click", async () => {
    if (typeof window.showDirectoryPicker !== "function") {
      setStatus("This browser does not support choosing a folder here. Click Save; the browser may offer a Save As location prompt.", true);
      return;
    }
    try {
      selectedDirectory = await window.showDirectoryPicker({ id: "jobsearchagent-save-location", mode: "readwrite", startIn: "downloads" });
      setStatus("Save location selected: " + selectedDirectory.name + ". Capture a screenshot, then click Save.", false);
    } catch (error) {
      if (error && error.name === "AbortError") setStatus("Folder selection canceled. No file was saved.", false);
      else setStatus("Could not select a folder. Click Save to choose a file location instead.", true);
    }
  });
  saveButton.addEventListener("click", async () => {
    if (!lastBlob || !lastFileName || working) {
      setStatus("Click Screenshot first. No file has been saved yet.", true);
      return;
    }
    working = true;
    saveButton.disabled = true;
    try {
      if (selectedDirectory) {
        const fileHandle = await selectedDirectory.getFileHandle(lastFileName, { create: true });
        const writable = await fileHandle.createWritable();
        await writable.write(lastBlob);
        await writable.close();
        setStatus("Saved " + lastFileName + " to the folder you selected.", false);
      } else if (typeof window.showSaveFilePicker === "function") {
        const format = lastExtension;
        const mime = lastBlob.type.split(";")[0] || "application/octet-stream";
        try {
          const fileHandle = await window.showSaveFilePicker({
            suggestedName: lastFileName,
            types: [{ description: FORMATS[format].description, accept: { [mime]: ["." + format] } }]
          });
          const writable = await fileHandle.createWritable();
          await writable.write(lastBlob);
          await writable.close();
          setStatus("Saved " + lastFileName + " to your chosen location.", false);
        } catch (error) {
          if (error && error.name === "AbortError") setStatus("Save canceled. No file was saved.", false);
          else throw error;
        }
      } else {
        const url = URL.createObjectURL(lastBlob);
        const link = document.createElement("a");
        link.href = url;
        link.download = lastFileName;
        link.style.display = "none";
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
        setStatus("Download requested. Choose a save location in your browser if prompted.", false);
      }
    } catch (error) {
      setStatus("Save failed: " + (error && error.message ? error.message : "unknown save error"), true);
    } finally {
      working = false;
      saveButton.disabled = !lastBlob;
    }
  });
})();
</script>
"""

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
{DEFAULT_PROCESS_BAR_CSS}
header{{background:#132744;color:white;padding:20px 28px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}}header h1{{margin:0;font-size:22px}}header p{{margin:3px 0 0;color:#cbd7e8}}.pill{{border:1px solid #6e88aa;border-radius:999px;padding:5px 10px;font-size:12px}}
.layout{{display:grid;grid-template-columns:220px 1fr;min-height:calc(100vh - 86px)}}aside{{padding:20px 12px;background:#eaf0f8;border-right:1px solid var(--line)}}.nav{{display:block;padding:11px 13px;margin:4px 0;border-radius:9px;color:#33435b;text-decoration:none;font-weight:600}}.nav.active,.nav:hover{{background:#d7e5ff;color:#1748a8}}
main{{padding:26px;max-width:1400px;width:100%;margin:0 auto}}h2{{margin:0 0 5px;font-size:25px}}.sub{{color:var(--muted);margin:0 0 20px}}.cards{{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:14px;margin:18px 0}}
.card,.panel{{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:18px;box-shadow:0 3px 12px #17365b08}}.metric{{font-size:28px;font-weight:750;margin-top:6px}}.label{{color:var(--muted);font-size:13px}}.panel{{margin:16px 0}}.panel h3{{margin:0 0 14px;font-size:17px}}
button{{background:var(--blue);color:white;border:0;border-radius:8px;padding:10px 14px;font-weight:650;cursor:pointer}}input,select,textarea{{display:block;visibility:visible;opacity:1;font:inherit;width:100%;min-height:46px;padding:10px 11px;border:2px solid #8b9db4;border-radius:8px;background:#fff;color:var(--ink);box-shadow:inset 0 1px 2px #17243a12}}textarea{{min-height:112px;resize:vertical}}label{{display:block;font-size:13px;font-weight:700;margin:11px 0 5px;color:var(--ink)}}input:focus,select:focus,textarea:focus{{outline:3px solid #b9d0ff;border-color:var(--blue)}}.formgrid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.wide{{grid-column:1/-1}}
table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{padding:11px 9px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}.tablewrap{{overflow:auto}}.tag{{display:inline-block;padding:3px 8px;border-radius:99px;background:#e7efff;color:#244da0;font-size:12px;font-weight:700}}.warning{{background:#fff7df;border:1px solid #f0dda1;border-radius:9px;padding:12px;color:#71541a}}.action-feedback{{border-radius:9px;padding:12px 14px;margin:12px 0;border:1px solid}}.action-feedback.success{{background:#eaf7ed;border-color:#a8d5b1;color:#205c2d}}.action-feedback.error{{background:#fff0ef;border-color:#e5b2ae;color:#8a2922}}.action-feedback.info{{background:#edf4ff;border-color:#b8ccef;color:#244a7b}}.empty{{padding:24px;text-align:center;color:var(--muted)}}
@media(max-width:800px){{.layout{{grid-template-columns:1fr}}aside{{display:flex;overflow:auto;padding:8px}}.nav{{white-space:nowrap}}main{{padding:16px}}.cards{{grid-template-columns:repeat(2,minmax(0,1fr))}}.formgrid{{grid-template-columns:1fr}}.wide{{grid-column:auto}}}}
</style></head><body><header><div><h1>JobSearchAgent <span style="color:#8fb7ff">v2</span></h1><p>Local job intelligence · evidence first · you stay in control</p></div><span class="pill">LOCAL ONLY · CONFIRMATION REQUIRED</span></header>
<div class="layout"><aside>{nav}</aside><main>{body}<p class="label" style="margin-top:28px">Simple is the best. Prototype data is separate from legacy files.</p></main></div>{DEFAULT_PROCESS_BAR_HTML}</body></html>'''


def metric(label: str, value, note: str = "") -> str:
    return f'<div class="card"><div class="label">{escape(label)}</div><div class="metric">{escape(str(value))}</div><div class="label">{escape(note)}</div></div>'


def jobs():
    with LOCK:
        return read_records(JOBS_FILE)


def all_applications():
    """Return every tracked application, including archived test records."""
    with LOCK:
        return read_records(APPLICATIONS_FILE)


def applications():
    """Return active tracked applications; archived entries remain in storage."""
    return [row for row in all_applications() if not row.get("archived", False)]


def overview_page() -> str:
    job_records, app_records = jobs(), applications()
    candidate = load_candidate_profile()
    qualified = sum(1 for item in job_records if (lambda result: result[0] >= 70 and not result[2])(score_job(item)))
    followups = sum(1 for item in app_records if item.get("status") in ("Follow-up due", "Interview"))
    body = '<h2>Overview</h2><p class="sub">Your search at a glance. Match scores use only the job details and verified evidence you enter.</p><div class="cards">'
    body += metric("Jobs captured", len(job_records), "Manually entered and verified")
    body += metric("Qualified for review", qualified, "Score ≥ 70; no detected exclusion")
    body += metric("Applications tracked", len(app_records), "Guided agent; final approval required")
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


def shortlist_page(edit_id: str = "") -> str:
    records = jobs()
    editing = next((row for row in records if row.get("id") == edit_id), None)
    def field_value(key: str) -> str:
        return escape(str((editing or {}).get(key, "")), quote=True)
    required_text = ", ".join(_as_list((editing or {}).get("required_skills", [])))
    verified_text = ", ".join(_as_list((editing or {}).get("verified_skills", [])))
    form_action = "/update-job" if editing else "/add-job"
    heading = "Edit saved job" if editing else "Add a job"
    submit = "Update job" if editing else "Save job"
    body = '<h2>Job Shortlist</h2><p class="sub">Start a new shortlist with jobs you have checked. For live imports, use <a href="/sources">Live Job Sources</a>. Enter required skills for the clearest match score; detected terms are labelled for review.</p>' + f'<div class="panel"><h3>{heading}</h3><form method="post" action="{form_action}"><input type="hidden" name="job_id" value="{escape(str((editing or {}).get("id", "")), quote=True)}"><div class="formgrid">'
    for key, label, kind in [("title", "Job title", "text"), ("company", "Company", "text"), ("location", "Location", "text"), ("source_url", "Source URL", "url"), ("posting_date", "Posting date (YYYY-MM-DD)", "text"), ("work_type", "Work type", "text")]:
        required = "required" if key in ("title", "company") else ""
        body += f'<div><label for="{key}">{label}</label><input id="{key}" name="{key}" type="{kind}" value="{field_value(key)}" {required}></div>'
    body += f'<div class="wide"><label for="description">Verified job description / requirements</label><textarea id="description" name="description" placeholder="Paste job information you checked. Do not enter private credentials.">{escape(str((editing or {}).get("description", "")))}</textarea></div><div class="wide"><label for="required_skills">Required skills stated in the job posting (comma-separated)</label><input id="required_skills" name="required_skills" value="{escape(required_text, quote=True)}" placeholder="Excel, SQL, Power BI"></div><div class="wide"><label for="verified_skills">Your confirmed skills relevant to this job (optional)</label><input id="verified_skills" name="verified_skills" value="{escape(verified_text, quote=True)}" placeholder="Excel, market research"></div><div class="wide"><button type="submit">{submit}</button>' + (' <a href="/shortlist">Cancel edit</a>' if editing else '') + '</div></div></form></div><div class="panel" id="application-editor"><h3>Saved jobs</h3>'
    if not records:
        body += '<div class="empty">No jobs saved yet. Add a verified role above to populate this table.</div>'
    else:
        body += '<p class="label">Saved jobs are sorted by match score: highest to lowest.</p>'
        body += '<div class="tablewrap"><table><thead><tr><th>Role / Company</th><th>Location</th><th>Score</th><th>Decision support</th><th>Evidence / gaps</th><th>Source</th></tr></thead><tbody>'
        for item in sorted(records, key=lambda row: score_job(row)[0], reverse=True):
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
            body += f'<tr><td><b>{escape(item.get("title", ""))}</b><br>{escape(item.get("company", ""))}<br><span class="label">{escape(item.get("posting_date", "") or "Posting date unknown")}</span><br><a href="/shortlist?edit_id={escape(str(item.get("id", "")), quote=True)}">Edit job</a></td><td>{escape(item.get("location", "Unknown"))}</td><td><span class="tag">{score}/100</span></td><td>{escape(status + exclusion_note)}</td><td>{evidence_html}</td><td>{source}</td></tr>'
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


def workspace_page(edit_id: str = "") -> str:
    job_records, app_records = jobs(), all_applications()
    editing = next((row for row in app_records if row.get("id") == edit_id), None) if edit_id else None
    if edit_id and editing is None:
        body = '<h2>Application record not found</h2><p class="sub">It may have been removed or changed.</p><a href="/workspace">Return to Application Workspace</a>'
        return render_page("workspace", body)
    statuses = ("Considering", "Preparing", "Applied", "Follow-up due", "Interview", "Rejected", "Offer", "Withdrawn")
    selected_status = (editing or {}).get("status", "Considering")
    body = '<h2>Application Workspace</h2><p class="sub">Track application outcomes and use the guided agent to prepare documents, navigate employer portals, and submit only after your final approval.</p>'
    body += '<div class="panel"><h3>Guided Application Agent</h3><p class="label">Real-life pilot: choose a saved job. The agent will generate tailored documents, open the employer recruitment portal, fill only clearly matched basic fields, and wait for your final authorization before Submit.</p>'
    if job_records:
        body += '<p class="label">Sorted by match score: highest to lowest.</p>'
        body += '<div class="tablewrap"><table><thead><tr><th>Saved role</th><th>Match score</th><th>Agent workflow</th></tr></thead><tbody>'
        for job_item in sorted(job_records, key=lambda row: score_job(row)[0], reverse=True):
            jid = str(job_item.get("id", ""))
            flow_item = _workflow_for_job(jid)
            rec_item = _application_for_job(jid) or {}
            status_item = flow_item.get("step", "not started")
            score_item, _, _ = score_job(job_item)
            if _guided_application_is_applied(jid):
                workflow_cell = (
                    '<span class="tag">Applied — disabled</span>'
                    '<p class="label">This job is already marked Applied. Guided application is disabled to help prevent a duplicate application. If this status is incorrect, edit the record in Application tracker.</p>'
                )
            else:
                workflow_cell = (escape(str(status_item).replace('_', ' ')) +
                    '<br><a href="/guided-application?job_id=' + escape(jid, quote=True) + '">Open Guided Agent</a>')
            body += '<tr><td><b>' + escape(job_item.get("title", "")) + '</b><br>' + escape(job_item.get("company", "")) + '</td><td>' + str(score_item) + '/100</td><td>' + workflow_cell + '</td></tr>'
        body += '</tbody></table></div>'
    else:
        body += '<div class="empty">Save a job in Job Shortlist before starting an application.</div>'
    body += '</div><div class="panel" id="application-editor"><h3>' + ("Edit tracked application" if editing else "Track a job") + '</h3>'
    if editing:
        edit_job = next((item for item in job_records if item.get("id") == editing.get("job_id")), {})
        body += '<p class="sub">Editing saved record: <b>' + escape(edit_job.get("title", "Unknown job")) + ' — ' + escape(edit_job.get("company", "")) + '</b>. Check the fields below before saving.</p>'
    body += '<form method="post" action="' + ("/update-application" if editing else "/add-application") + '"><div class="formgrid">'
    if editing:
        body += f'<input type="hidden" name="application_id" value="{escape(editing.get("id", ""), quote=True)}">'
    body += '<div><label>Job</label><select name="job_id" required><option value="">Choose a saved job…</option>'
    for item in job_records:
        selected = ' selected' if item.get("id") == (editing or {}).get("job_id") else ''
        body += f'<option value="{escape(item.get("id", ""), quote=True)}"{selected}>{escape(item.get("title", ""))} — {escape(item.get("company", ""))}</option>'
    body += '</select></div><div><label>Status</label><select name="status">'
    for status in statuses:
        selected = ' selected' if status == selected_status else ''
        body += f'<option{selected}>{escape(status)}</option>'
    today_local = datetime.now().astimezone().date().isoformat()
    body += f'</select></div><div><label for="applied_date">Applied date</label><input id="applied_date" type="date" name="applied_date" max="{today_local}" value="{escape((editing or {}).get("applied_date", ""), quote=True)}"><small>Choose today or an earlier date. Future dates are disabled.</small></div>'
    body += f'<div><label for="followup_date">Follow-up date</label><input id="followup_date" type="date" name="followup_date" min="{today_local}" value="{escape((editing or {}).get("followup_date", ""), quote=True)}"><small>Choose today or a future date. Past dates are disabled.</small></div>'
    body += f'<div class="wide"><label>Notes / next action</label><textarea name="notes" placeholder="Next action or interview notes…">{escape((editing or {}).get("notes", ""))}</textarea></div>'
    manual_confirm_checked = ' checked' if ((editing or {}).get("guided_workflow", {}) or {}).get("user_confirmed_external_submission") is True else ''
    body += '<div class="wide"><label><input type="checkbox" name="external_submission_confirmation" value="YES"' + manual_confirm_checked + ' style="display:inline;width:auto;min-height:auto"> I confirm I already submitted this application on the employer’s official website. JobSearchAgent has not independently verified the employer confirmation receipt.</label></div>'
    body += '<div class="wide"><button type="submit">' + ("Save changes" if editing else "Add to tracker") + '</button>'
    if editing:
        body += ' <a style="margin-left:12px" href="/workspace">Cancel edit</a>'
    body += '</div></div></form></div><div class="panel" id="application-tracker"><h3>Application tracker</h3>'
    active_records = [record for record in app_records if not record.get("archived", False)]
    archived_records = [record for record in app_records if record.get("archived", False)]
    by_id = {item.get("id"): item for item in job_records}
    if not active_records:
        body += '<div class="empty">No active applications tracked yet. Archived test records remain available below.</div>'
    else:
        body += '<div class="tablewrap"><table><thead><tr><th>Role</th><th>Status</th><th>Applied</th><th>Follow-up</th><th>Notes</th><th>Action</th></tr></thead><tbody>'
        for record in sorted(active_records, key=lambda row: row.get("updated_at", ""), reverse=True):
            job = by_id.get(record.get("job_id"), {})
            record_id = escape(record.get("id", ""), quote=True)
            notes_text = str(record.get("notes", "") or "")
            action_html = f'<a href="/workspace?edit_id={record_id}#application-editor">Edit</a>'
            # Only records explicitly labelled TEST ONLY may be archived from this screen.
            if "test only" in notes_text.casefold():
                action_html += (' <form method="post" action="/archive-application" style="display:inline;margin-left:8px">'
                                f'<input type="hidden" name="application_id" value="{record_id}">'
                                '<button type="submit" style="background:#5d6b80;padding:6px 9px">Archive test record</button></form>')
            body += f'<tr><td><b>{escape(job.get("title", "Unknown job"))}</b><br>{escape(job.get("company", ""))}</td><td><span class="tag">{escape(record.get("status", "Considering"))}</span></td><td>{escape(record.get("applied_date", "") or "—")}</td><td>{escape(record.get("followup_date", "") or "—")}</td><td>{escape(notes_text or "—")}</td><td>{action_html}</td></tr>'
        body += '</tbody></table></div>'
    body += '</div>'
    if archived_records:
        body += '<div class="panel" id="archived-test-records"><h3>Archived test records</h3><p class="sub">These records are hidden from active tracking but retained in local storage. Restore them if needed.</p><div class="tablewrap"><table><thead><tr><th>Role</th><th>Status</th><th>Applied</th><th>Notes</th><th>Action</th></tr></thead><tbody>'
        for record in sorted(archived_records, key=lambda row: row.get("archived_at", ""), reverse=True):
            job = by_id.get(record.get("job_id"), {})
            record_id = escape(record.get("id", ""), quote=True)
            body += f'<tr><td><b>{escape(job.get("title", "Unknown job"))}</b><br>{escape(job.get("company", ""))}</td><td>{escape(record.get("status", "Considering"))}</td><td>{escape(record.get("applied_date", "") or "—")}</td><td>{escape(record.get("notes", "") or "—")}</td><td><form method="post" action="/restore-application"><input type="hidden" name="application_id" value="{record_id}"><button type="submit" style="background:#5d6b80">Restore</button></form></td></tr>'
        body += '</tbody></table></div></div>'
    body += '<div class="warning">Use confirmed resume facts in application drafts. Unknown facts must stay as placeholders.</div>'
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


# ---------------- Guided application agent (additive v2 pilot) ----------------
# This first pilot targets the Simon-Kucher Market Research Manager application.
# Browser interaction is opt-in, runs locally, and requires explicit user approval
# before the final portal Submit action.

APPLICATION_DOCS_DIR = DATA_DIR / "application_documents"
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"
SIMON_KUCHER_PORTAL_URL = "https://simon-kucher.csod.com/ux/ats/careersite/6/home/?c=simon-kucher&lang=en-US"
EMPLOYER_PORTALS_FILE = DATA_DIR / "verified_employer_portals.json"
APPLICANT_DETAILS = {
    "first_name": "Saiful",
    "last_name": "Quadir",
    "full_name": "Saiful Quadir",
    "email": "saiful.quadir@yahoo.com",
    "phone": "437.473.8544",
    "linkedin": "https://www.linkedin.com/in/saiful-quadir",
    "city": "Toronto",
    "province": "Ontario",
    "country": "Canada",
}
_GUIDED_BROWSER_QUEUE = _queue.Queue()
_GUIDED_BROWSER_THREAD = None
_GUIDED_BROWSER_LOCK = threading.RLock()
_GUIDED_BROWSER_STATE = {
    "status": "not_started", "job_id": "", "url": "", "title": "",
    "message": "Start the guided application to open the employer portal.",
    "captured_at": "", "body_excerpt": "", "controls": [], "review_items": [],
    "required_missing": [], "submit_candidates": [], "role_verified": False,
    "review_ready": False, "uploaded_docs": [], "review_fingerprint": "", "submit_in_progress": False,
}


def _employer_key(company: str) -> str:
    """Stable normalized key so portal mappings follow the selected employer, not a job row."""
    return re.sub(r"[^a-z0-9]+", " ", str(company or "").casefold()).strip()


def _read_verified_employer_portals() -> dict:
    """Read only explicit, locally verified employer portal mappings."""
    try:
        payload = json.loads(EMPLOYER_PORTALS_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("employers"), dict):
            return payload["employers"]
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_verified_employer_portal(company: str, label: str, url: str) -> None:
    """Persist a portal after the user explicitly confirms it is the employer's official portal."""
    key = _employer_key(company)
    parsed = urlparse(url)
    if not key or parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("A company name and valid HTTPS portal URL are required.")
    portals = _read_verified_employer_portals()
    portals[key] = {"label": label.strip()[:120] or company.strip()[:120],
                    "url": url, "hosts": [parsed.hostname.lower().rstrip(".")],
                    "verified_by_user": True, "verified_at": now_iso()}
    EMPLOYER_PORTALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = EMPLOYER_PORTALS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"employers": portals}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(EMPLOYER_PORTALS_FILE)


class _PortalLinkParser(HTMLParser):
    """Collect anchor URLs and their visible labels from a saved job listing page."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(" ".join(self._text).split())))
            self._href = None
            self._text = []


_PORTAL_DISCOVERY_CACHE = {}
_PORTAL_DISCOVERY_CACHE_LOCK = threading.RLock()
_PORTAL_DISCOVERY_CACHE_SECONDS = 600


def _safe_public_https_url(url: str) -> bool:
    """Reject non-HTTPS, credential-bearing, local, and private-network URLs."""
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        if parsed.port not in (None, 443):
            return False
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                return False
        except ValueError:
            pass
        return True
    except (TypeError, ValueError):
        return False


def _employer_url_tokens(company: str) -> list[str]:
    """Create conservative identifiers to match an employer slug in an official/ATS URL."""
    words = re.findall(r"[a-z0-9]+", str(company or "").casefold())
    ignored = {"inc", "incorporated", "ltd", "limited", "llc", "corp", "corporation",
               "company", "co", "the", "group", "jobs", "careers", "canada", "usa"}
    words = [w for w in words if w not in ignored and len(w) >= 4]
    compact = "".join(words)
    # Prefer a full multi-word slug, then distinctive words. Never use tiny tokens like "MD".
    tokens = []
    if len(compact) >= 5:
        tokens.append(compact)
    tokens.extend(sorted(set(words), key=len, reverse=True))
    return list(dict.fromkeys(tokens))


def _fetch_listing_html(source_url: str) -> tuple[str, str] | None:
    """Fetch only public HTTPS Adzuna listing pages; never submit forms or follow user data."""
    if not _safe_public_https_url(source_url):
        return None
    host = (urlparse(source_url).hostname or "").lower().rstrip(".")
    if not (host == "adzuna.ca" or host.endswith(".adzuna.ca") or host == "adzuna.com" or host.endswith(".adzuna.com")):
        return None
    request = Request(source_url, headers={"User-Agent": "JobSearchAgent/1.0 (local job-listing portal discovery)",
                                           "Accept": "text/html,application/xhtml+xml"})
    try:
        with urlopen(request, timeout=5) as response:
            final_url = response.geturl()
            if not _safe_public_https_url(final_url):
                return None
            final_host = (urlparse(final_url).hostname or "").lower().rstrip(".")
            if not (final_host == "adzuna.ca" or final_host.endswith(".adzuna.ca") or final_host == "adzuna.com" or final_host.endswith(".adzuna.com")):
                # The listing itself redirected to an external page. Let the caller check employer identity.
                return "", final_url
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and "html" not in content_type:
                return None
            raw = response.read(2_000_000)
            return raw.decode("utf-8", "replace"), final_url
    except (OSError, URLError, HTTPError, TimeoutError, ValueError):
        return None


def _discover_employer_portal_from_listing(job: dict | None) -> dict | None:
    """Discover an employer/ATS link from the saved Adzuna listing, with strict URL evidence.

    This is a candidate resolver, not a claim that a role is already verified. The existing
    managed-browser workflow must still verify the exact job title and keep final submission gated.
    Results (including misses) are cached briefly to avoid repeated page fetches on every render.
    """
    if not isinstance(job, dict):
        return None
    company = str(job.get("company", "")).strip()
    source_url = str(job.get("source_url", "")).strip()
    tokens = _employer_url_tokens(company)
    if not company or not tokens or not source_url:
        return None
    cache_key = (str(job.get("id", "")), _employer_key(company), source_url)
    now = time.monotonic()
    with _PORTAL_DISCOVERY_CACHE_LOCK:
        cached = _PORTAL_DISCOVERY_CACHE.get(cache_key)
        if cached and now - cached[0] < _PORTAL_DISCOVERY_CACHE_SECONDS:
            return dict(cached[1]) if cached[1] else None

    fetched = _fetch_listing_html(source_url)
    result = None
    if fetched:
        html, final_url = fetched
        candidates = []
        if final_url and final_url != source_url:
            candidates.append((final_url, "redirected listing destination", 0))
        if html:
            parser = _PortalLinkParser()
            try:
                parser.feed(html)
            except Exception:
                parser.links = []
            for href, label in parser.links:
                if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    continue
                candidates.append((urljoin(final_url or source_url, href), label, 1))

        ranked = []
        source_host = (urlparse(source_url).hostname or "").lower().rstrip(".")
        for candidate_url, label, link_rank in candidates:
            if not _safe_public_https_url(candidate_url):
                continue
            parsed = urlparse(candidate_url)
            host = (parsed.hostname or "").lower().rstrip(".")
            if host == source_host or host == "adzuna.ca" or host.endswith(".adzuna.ca") or host == "adzuna.com" or host.endswith(".adzuna.com"):
                continue
            identity = (host + " " + parsed.path).casefold()
            matched = [token for token in tokens if token in re.sub(r"[^a-z0-9]+", "", identity)]
            if not matched:
                continue
            label_lower = str(label or "").casefold()
            apply_signal = any(word in label_lower for word in ("apply", "career", "employer", "application", "job", "recruit"))
            path_signal = any(word in parsed.path.casefold() for word in ("career", "job", "apply", "application", "recruit"))
            if link_rank == 1 and not (apply_signal or path_signal):
                continue
            # Require either a complete employer slug or a distinctive token of at least 5 chars.
            if not any(len(token) >= 5 for token in matched):
                continue
            score = (8 if any(token in re.sub(r"[^a-z0-9]+", "", host) for token in matched) else 0)
            score += 3 if apply_signal else 0
            score += 2 if path_signal else 0
            score += 1 if link_rank == 1 else 0
            ranked.append((score, candidate_url, host))
        if ranked:
            ranked.sort(key=lambda item: (-item[0], len(item[1])))
            _, portal_url, portal_host = ranked[0]
            result = {"label": f"{company} portal (discovered from saved listing)",
                      "url": portal_url, "hosts": (portal_host,),
                      "source": "listing_discovery", "requires_role_verification": True}

    with _PORTAL_DISCOVERY_CACHE_LOCK:
        _PORTAL_DISCOVERY_CACHE[cache_key] = (now, dict(result) if result else None)
        # Bound memory growth for long-running local dashboards.
        if len(_PORTAL_DISCOVERY_CACHE) > 500:
            oldest = sorted(_PORTAL_DISCOVERY_CACHE.items(), key=lambda item: item[1][0])[:100]
            for old_key, _ in oldest:
                _PORTAL_DISCOVERY_CACHE.pop(old_key, None)
    return result


def _portal_config_for_job(job: dict | None) -> dict | None:
    """Resolve a portal dynamically for the selected job's employer.

    Precedence: an explicitly verified local mapping, known built-in mapping, then a
    listing-derived HTTPS application link whose hostname/path contains a distinctive
    employer identifier. The discovered URL is only a candidate; the existing browser flow
    must still verify the exact role before proceeding. We never invent URLs from company names.
    """
    if not isinstance(job, dict):
        return None
    company_text = str(job.get("company", "")).strip()
    key = _employer_key(company_text)
    if not key:
        return None
    saved = _read_verified_employer_portals().get(key)
    if isinstance(saved, dict) and saved.get("verified_by_user") is True:
        url = str(saved.get("url", "")).strip()
        parsed = urlparse(url)
        hosts = saved.get("hosts", [])
        if parsed.scheme == "https" and parsed.hostname and isinstance(hosts, list) and hosts:
            return {"label": str(saved.get("label") or company_text), "url": url,
                    "hosts": tuple(str(h).lower().rstrip(".") for h in hosts), "source": "verified_employer_mapping"}
    company = key
    if "simon kucher" in company:
        return {"label": "Simon-Kucher", "url": SIMON_KUCHER_PORTAL_URL,
                "hosts": ("simon-kucher.csod.com", "simon-kucher.com"), "source": "built_in"}
    if "insight global" in company:
        return {"label": "Insight Global", "url": "https://jobs.insightglobal.com/",
                "hosts": ("jobs.insightglobal.com", "careers.insightglobal.com", "insightglobal.com"), "source": "built_in"}
    # Dynamic fallback for all employers represented by an Adzuna saved listing.
    return _discover_employer_portal_from_listing(job)


def _is_simon_kucher_job(job: dict | None) -> bool:
    """Compatibility helper for the existing Simon-Kucher pilot."""
    cfg = _portal_config_for_job(job)
    return bool(cfg and cfg.get("label") == "Simon-Kucher")


def _guided_portal_mismatch_message(job: dict | None) -> str:
    company = str((job or {}).get("company", "Unknown employer")).strip() or "Unknown employer"
    title = str((job or {}).get("title", "Untitled role")).strip() or "Untitled role"
    return (f"No verified official recruitment portal is configured for {title} at {company}. "
            "The agent has not opened a portal, generated application documents, or changed form fields. "
            "Verify the official portal from the saved job listing, then save it for this employer. "
            "That verified mapping will be reused dynamically for other saved jobs with the same employer.")


def _new_guided_workflow() -> dict:
    return {"step": "not_started", "documents_approved": False,
            "employer_job_verified": False, "submission_verified": False,
            "events": [], "document_paths": {}, "updated_at": now_iso()}


def _get_job(job_id: str) -> dict | None:
    return next((item for item in jobs() if item.get("id") == job_id), None)


def _application_for_job(job_id: str) -> dict | None:
    return next((item for item in applications() if item.get("job_id") == job_id), None)


def _guided_application_is_applied(job_id: str) -> bool:
    """Prevent re-opening an application after submission has been recorded."""
    for record in applications():
        if record.get("job_id") != job_id:
            continue
        flow = record.get("guided_workflow", {})
        if not isinstance(flow, dict):
            flow = {}
        if (str(record.get("status", "")).strip().casefold() == "applied"
                or flow.get("submission_verified") is True
                or flow.get("user_confirmed_external_submission") is True):
            return True
    return False


def _ensure_application_record(job_id: str) -> dict | None:
    """Reuse the existing application for this job; never create a duplicate here."""
    with LOCK:
        job_rows = read_records(JOBS_FILE)
        if not any(item.get("id") == job_id for item in job_rows):
            return None
        rows = read_records(APPLICATIONS_FILE)
        rec = next((item for item in rows if item.get("job_id") == job_id), None)
        if rec is None:
            rec = {
                "id": f"app-guided-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                "job_id": job_id, "status": "Preparing", "applied_date": "",
                "followup_date": "", "notes": "Guided application started; submission is not yet verified.",
                "created_at": now_iso(), "updated_at": now_iso(),
                "guided_workflow": _new_guided_workflow(),
            }
            rows.append(rec)
        else:
            rec.setdefault("guided_workflow", _new_guided_workflow())
            # Never infer that an existing record was submitted.
            rec["updated_at"] = now_iso()
        save_records(APPLICATIONS_FILE, rows)
        return rec


def _record_guided_event(job_id: str, step: str, message: str, extra: dict | None = None) -> None:
    """Persist workflow events only; don't save portal field values or passwords."""
    with LOCK:
        rows = read_records(APPLICATIONS_FILE)
        rec = next((item for item in rows if item.get("job_id") == job_id), None)
        if rec is None:
            return
        flow = rec.setdefault("guided_workflow", _new_guided_workflow())
        flow["step"] = step
        flow["updated_at"] = now_iso()
        events = flow.setdefault("events", [])
        events.append({"at": now_iso(), "step": step, "message": str(message)[:320]})
        flow["events"] = events[-80:]
        if extra:
            flow.update(extra)
        rec["updated_at"] = now_iso()
        save_records(APPLICATIONS_FILE, rows)


def _workflow_for_job(job_id: str) -> dict:
    rec = _application_for_job(job_id) or {}
    flow = rec.get("guided_workflow", {})
    return flow if isinstance(flow, dict) else {}


def _make_guided_documents(job: dict | None = None) -> dict:
    """Generate job-specific DOCX files inside data_v2 without touching jobs.json."""
    APPLICATION_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from jobsearchagent_simon_kucher_documents import generate_documents
    except Exception as exc:
        raise RuntimeError(
            "Document generator is missing. Put jobsearchagent_simon_kucher_documents.py beside "
            "jobsearchagent_dashboard.py, then restart the dashboard."
        ) from exc
    try:
        result = generate_documents(APPLICATION_DOCS_DIR, job=job)
    except ImportError as exc:
        raise RuntimeError(
            "Document generation needs python-docx. Install it with: python3 -m pip install python-docx"
        ) from exc
    clean = {}
    for key, path in result.items():
        p = Path(path).resolve()
        if p.parent != APPLICATION_DOCS_DIR.resolve() or not p.is_file():
            raise RuntimeError("Document generator returned an unexpected or missing output path.")
        clean[key] = str(p)
    return clean


def _is_allowed_employer_portal(url: str, job_id: str | None = None) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        return False
    job = _get_job(job_id) if job_id else None
    cfg = _portal_config_for_job(job)
    if not cfg:
        # Keep compatibility for role detection during the existing Simon-Kucher pilot.
        return host == "simon-kucher.csod.com" or host == "simon-kucher.com" or host.endswith(".simon-kucher.com")
    for allowed in cfg["hosts"]:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def _read_portal_controls(page) -> list[dict]:
    js = r"""els => els.slice(0, 160).map((el, i) => {
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute('type') || (tag === 'select' ? 'select' : tag)).toLowerCase();
      const id = el.id || '';
      let label = '';
      const buttonLike = tag === 'button' || type === 'submit' || type === 'button';
      if (buttonLike) {
        label = (tag === 'button' ? (el.innerText || '') : (el.value || '')) ||
                el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('name') || id || '';
      } else {
        if (id) {
          const found = [...document.querySelectorAll('label')].find(l => l.htmlFor === id);
          if (found) label = found.innerText || '';
        }
        if (!label && el.closest('label')) label = el.closest('label').innerText || '';
        label = label || el.getAttribute('aria-label') || el.getAttribute('title') ||
                el.getAttribute('placeholder') || el.getAttribute('name') || id || '';
      }
      let value = '';
      if (type === 'password') value = '';
      else if (type === 'checkbox' || type === 'radio') value = el.checked ? 'Checked' : 'Not checked';
      else if (type === 'file') value = [...(el.files || [])].map(f => f.name).join(', ');
      else if (tag === 'select') value = [...el.selectedOptions].map(o => o.textContent.trim()).join(', ');
      else if (tag === 'button') value = el.innerText || el.value || '';
      else value = el.value || '';
      const style = getComputedStyle(el);
      const visible = !!(el.getClientRects().length && style.visibility !== 'hidden' && style.display !== 'none');
      const formEls = [...document.querySelectorAll('input,textarea,select')];
      const fieldIndex = formEls.indexOf(el);
      const fileEls = [...document.querySelectorAll('input[type=file]')];
      const fileIndex = fileEls.indexOf(el);
      return {index:i, fieldIndex, fileIndex, tag, type, id, name:el.getAttribute('name') || '', label:String(label).trim().slice(0,180),
              value:String(value).slice(0,500), required:!!el.required || el.getAttribute('aria-required')==='true',
              visible, disabled:!!el.disabled, accept:el.getAttribute('accept') || ''};
    })"""
    try:
        return page.locator("input,textarea,select,button,input[type=submit],input[type=button]").evaluate_all(js)
    except Exception:
        return []


def _role_matches_page(job: dict, url: str, body: str) -> bool:
    if not _is_allowed_employer_portal(url, str(job.get("id", ""))):
        return False
    title = re.sub(r"\s+", " ", str(job.get("title", "")).split("|")[0]).strip().casefold()
    text = re.sub(r"\s+", " ", (body or "")).casefold()
    if not title or title not in text:
        return False
    # Require a saved location signal as well when one is present; this reduces
    # false matches for generic titles such as Data Analyst on a large job board.
    location = str(job.get("location", "")).strip()
    city = re.split(r"[,|]", location)[0].strip().casefold() if location else ""
    if city and city not in text:
        return False
    generic_titles = {"data analyst", "business analyst", "financial analyst", "project coordinator", "marketing analyst"}
    path = (urlparse(url).path or "/").casefold().rstrip("/")
    if title in generic_titles and (not path or path == "/" or "find_a_job" in path or "search" in path):
        return False
    return True


def _snapshot_browser_page(page, job_id: str, status: str = "observed", message: str = "Portal page captured.") -> dict:
    job = _get_job(job_id) or {}
    try:
        title = page.title()[:300]
    except Exception:
        title = ""
    try:
        url = page.url[:1200]
    except Exception:
        url = ""
    try:
        body = page.locator("body").inner_text(timeout=5000)
    except Exception:
        body = ""
    controls = _read_portal_controls(page)
    previous_uploads = list(_GUIDED_BROWSER_STATE.get("uploaded_docs", []))
    portal_uploads = [str(item.get("value", "")).strip() for item in controls
                      if item.get("tag") == "input" and item.get("type") == "file" and str(item.get("value", "")).strip()]
    detected_uploads = list(dict.fromkeys(previous_uploads + portal_uploads))
    role_verified = _role_matches_page(job, url, body) or bool(_workflow_for_job(job_id).get("employer_job_verified"))
    safe_body = re.sub(r"\s+", " ", body).strip()[:5000]
    candidates = []
    for item in controls:
        if item.get("tag") in ("button", "input"):
            label = (item.get("label") or item.get("value") or "").strip()
            if item.get("visible") and re.fullmatch(r"submit(?: application)?", label, re.I):
                candidates.append(label)
    state = {
        "status": status, "job_id": job_id, "url": url, "title": title,
        "message": message, "captured_at": now_iso(), "body_excerpt": safe_body,
        "controls": controls, "role_verified": role_verified,
        "submit_candidates": candidates, "review_items": [], "required_missing": [],
        "review_ready": False, "uploaded_docs": detected_uploads,
        "review_fingerprint": "", "submit_in_progress": False,
    }
    with _GUIDED_BROWSER_LOCK:
        _GUIDED_BROWSER_STATE.clear()
        _GUIDED_BROWSER_STATE.update(state)
    if role_verified and not _workflow_for_job(job_id).get("employer_job_verified"):
        _record_guided_event(job_id, "employer_job_verified", "The exact saved role was visible on the matching employer recruitment portal.", {
            "employer_job_verified": True, "portal_url": url, "portal_title": title
        })
    return state


def _candidate_value(control: dict) -> str:
    label = " ".join(str(control.get(k, "")) for k in ("label", "name", "id", "type", "accept")).lower()
    kind = str(control.get("type", "")).lower()
    if not control.get("visible") or control.get("disabled") or kind in {
        "password", "hidden", "file", "checkbox", "radio", "submit", "button", "reset"
    }:
        return ""
    # Keep this intentionally narrow: never guess legal, immigration, demographic,
    # salary, disability, veteran, DOB, or street-address answers.
    if "email" in label or kind == "email":
        return APPLICANT_DETAILS["email"]
    if any(x in label for x in ("phone", "mobile", "telephone")) or kind == "tel":
        return APPLICANT_DETAILS["phone"]
    if "linkedin" in label:
        return APPLICANT_DETAILS["linkedin"]
    if any(x in label for x in ("first name", "given name", "firstname", "forename")):
        return APPLICANT_DETAILS["first_name"]
    if any(x in label for x in ("last name", "surname", "family name", "lastname")):
        return APPLICANT_DETAILS["last_name"]
    if any(x in label for x in ("full name", "applicant name", "candidate name")):
        return APPLICANT_DETAILS["full_name"]
    if any(x in label for x in ("city", "town")):
        return APPLICANT_DETAILS["city"]
    if any(x in label for x in ("province", "state", "region")):
        return APPLICANT_DETAILS["province"]
    if label.strip() == "country" or "country of residence" in label:
        return APPLICANT_DETAILS["country"]
    return ""


def _browser_worker() -> None:
    """Playwright runs entirely on this thread; the HTTP handler queues actions."""
    playwright = None
    context = None
    page = None
    playwright_error = ""
    while True:
        cmd = _GUIDED_BROWSER_QUEUE.get()
        action = cmd.get("action", "")
        job_id = cmd.get("job_id", "")
        payload = cmd.get("payload", {})
        result = {"ok": False, "message": "Action could not be completed."}
        try:
            if action == "start":
                try:
                    from playwright.sync_api import sync_playwright
                    if playwright is None:
                        playwright = sync_playwright().start()
                        playwright_error = ""
                    if context is None:
                        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                        context = playwright.chromium.launch_persistent_context(
                            user_data_dir=str(BROWSER_PROFILE_DIR), headless=False,
                            args=["--start-maximized"], no_viewport=True,
                        )
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(12000)
                    portal_cfg = _portal_config_for_job(_get_job(job_id))
                    if not portal_cfg or not _is_allowed_employer_portal(portal_cfg.get("url", ""), job_id):
                        raise RuntimeError("No verified official portal mapping is available for this saved employer.")
                    page.goto(portal_cfg["url"], wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(1800)
                    # Try a simple role search. If the portal's structure differs, leave it
                    # open for a user-assisted selection and let Capture inspect that page.
                    try:
                        search_items = page.locator('input[placeholder*="search" i],input[aria-label*="search" i],input[name*="search" i],input[id*="search" i],input[name*="keyword" i]')
                        search = None
                        for si in range(min(search_items.count(), 12)):
                            maybe = search_items.nth(si)
                            try:
                                if maybe.is_visible() and maybe.is_enabled():
                                    search = maybe
                                    break
                            except Exception:
                                continue
                        if search is not None:
                            target_job = _get_job(job_id) or {}
                            search_title = str(target_job.get("title", "")).strip()
                            search_location = str(target_job.get("location", "")).split(",")[0].strip()
                            search.fill(" ".join(x for x in (search_title, search_location) if x))
                            search.press("Enter")
                            page.wait_for_timeout(1800)
                    except Exception:
                        pass
                    # For Simon-Kucher, the known role title is specific enough to try a
                    # direct link. For generic titles (e.g. Data Analyst), leave the
                    # results page open so the user can choose the exact matching listing.
                    try:
                        target_job = _get_job(job_id) or {}
                        if _is_simon_kucher_job(target_job):
                            target_title = str(target_job.get("title", "")).strip()
                            target_link = page.get_by_role("link", name=re.compile(re.escape(target_title.split("|")[0].strip()), re.I)).first
                            if target_link.count() and target_link.is_visible():
                                target_link.click(timeout=4000)
                                page.wait_for_timeout(1400)
                    except Exception:
                        pass
                    state = _snapshot_browser_page(
                        page, job_id, "portal_open",
                        f"Official {portal_cfg['label']} job portal opened. The agent searched for the saved role title. If it is not shown, locate the exact role manually and click Capture current portal page.",
                    )
                    result = {"ok": True, "message": state["message"], "state": state}
                except Exception as exc:
                    message = str(exc).replace(str(BROWSER_PROFILE_DIR), "<local browser profile>")[:500]
                    if "playwright" in str(exc).lower() and ("module" in str(exc).lower() or "install" in str(exc).lower()):
                        message = "Playwright is not installed. Run: python3 -m pip install playwright && python3 -m playwright install chromium"
                    elif "Executable doesn't exist" in str(exc) or "browserType.launch" in str(exc):
                        message = "Playwright is installed but its Chromium browser is missing. Run: python3 -m playwright install chromium"
                    result = {"ok": False, "message": message}
                    with _GUIDED_BROWSER_LOCK:
                        _GUIDED_BROWSER_STATE.update({"status": "blocked", "job_id": job_id, "message": message, "captured_at": now_iso()})
            elif action in ("capture", "fill_basics", "attach_documents", "continue", "prepare_review", "submit"):
                if page is None or page.is_closed():
                    result = {"ok": False, "message": "The managed employer portal browser is not open. Click Start / resume guided application."}
                elif not _is_allowed_employer_portal(page.url, job_id):
                    result = {"ok": False, "message": "The current page is outside the official portal mapped to this saved employer. No fields were changed and no submission action was taken."}
                elif action == "capture":
                    state = _snapshot_browser_page(page, job_id, "observed", "Current employer portal page captured.")
                    result = {"ok": True, "message": state["message"], "state": state}
                elif action == "fill_basics":
                    flow = _workflow_for_job(job_id)
                    if not flow.get("documents_approved"):
                        result = {"ok": False, "message": "Review and approve the tailored documents first."}
                    else:
                        state = _snapshot_browser_page(page, job_id, "observed", "Checking current form before filling basic profile fields.")
                        if not state.get("role_verified"):
                            result = {"ok": False, "message": "The exact job is not yet verified on the employer portal. Open the correct role and Capture current portal page first."}
                        else:
                            controls = _read_portal_controls(page)
                            fields = page.locator("input,textarea,select")
                            filled = []
                            for meta in controls:
                                if meta.get("tag") not in ("input", "textarea", "select"):
                                    continue
                                value = _candidate_value(meta)
                                field_index = meta.get("fieldIndex", -1)
                                if not value or field_index < 0 or field_index >= fields.count() or meta.get("value"):
                                    continue
                                locator = fields.nth(field_index)
                                try:
                                    if meta.get("tag") == "select":
                                        try: locator.select_option(label=value, timeout=1200)
                                        except Exception: locator.select_option(value=value, timeout=1200)
                                    else:
                                        locator.fill(value, timeout=1200)
                                    filled.append(meta.get("label") or meta.get("name") or meta.get("id") or f"field {i+1}")
                                except Exception:
                                    continue
                            state = _snapshot_browser_page(page, job_id, "form_filled", f"Filled {len(filled)} clearly matched, blank basic profile field(s). Review them in the portal.")
                            state["message"] = (f"Filled {len(filled)} clearly matched, blank basic profile field(s): " + (", ".join(filled[:15]) if filled else "none matched safely") + ". No legal, demographic, work-authorization, salary, or street-address answers were guessed.")
                            with _GUIDED_BROWSER_LOCK:
                                _GUIDED_BROWSER_STATE.update(state)
                            _record_guided_event(job_id, "basic_fields_filled", state["message"])
                            result = {"ok": True, "message": state["message"], "state": state}
                elif action == "attach_documents":
                    flow = _workflow_for_job(job_id)
                    if not flow.get("documents_approved"):
                        result = {"ok": False, "message": "Review and approve the tailored documents first."}
                    elif not flow.get("employer_job_verified"):
                        result = {"ok": False, "message": "The correct employer job has not yet been verified on the portal."}
                    else:
                        docs = flow.get("document_paths", {})
                        resume = Path(docs.get("resume", "")) if docs.get("resume") else None
                        cover = Path(docs.get("cover_letter", "")) if docs.get("cover_letter") else None
                        if not resume or not resume.is_file() or not cover or not cover.is_file():
                            result = {"ok": False, "message": "The approved resume/cover-letter files are missing. Regenerate the document set in the workspace."}
                        else:
                            controls = _read_portal_controls(page)
                            file_controls = [m for m in controls if m.get("tag") == "input" and m.get("type") == "file" and not m.get("disabled")]
                            done = []
                            used = set()
                            for meta in file_controls:
                                label = " ".join(str(meta.get(k, "")) for k in ("label", "name", "id", "accept")).lower()
                                target = None
                                if re.search(r"resume|curriculum|\bcv\b", label): target = ("resume", resume)
                                elif re.search(r"cover.?letter|motivation.?letter", label): target = ("cover_letter", cover)
                                elif len(file_controls) == 1 and not label.strip(): target = ("resume", resume)
                                if not target or target[0] in used:
                                    continue
                                file_index = meta.get("fileIndex", -1)
                                if file_index < 0:
                                    continue
                                locator = page.locator("input[type=file]").nth(file_index)
                                try:
                                    locator.set_input_files(str(target[1].resolve()), timeout=5000)
                                    done.append(target[1].name); used.add(target[0])
                                except Exception:
                                    continue
                            old = list(_GUIDED_BROWSER_STATE.get("uploaded_docs", []))
                            merged = list(dict.fromkeys(old + done))
                            state = _snapshot_browser_page(page, job_id, "documents_attached" if done else "observed", 
                                "Attached the approved document(s) in clearly labeled portal upload fields." if done else "No unambiguous resume/cover-letter upload field was found. Use the employer portal's upload control manually, then Capture current portal page.")
                            state["uploaded_docs"] = merged
                            with _GUIDED_BROWSER_LOCK: _GUIDED_BROWSER_STATE.update(state)
                            _record_guided_event(job_id, "documents_attached" if done else "upload_needs_user", state["message"], {"uploaded_document_names": merged})
                            result = {"ok": bool(done), "message": state["message"], "state": state}
                elif action == "continue":
                    state = _snapshot_browser_page(page, job_id, "observed", "Looking for an unambiguous non-submit Continue/Next control.")
                    controls = _read_portal_controls(page)
                    candidates = [m for m in controls if m.get("visible") and not m.get("disabled") and m.get("tag") in ("button", "input") and re.fullmatch(r"next|continue|save and continue|review|proceed|next step", (m.get("label") or m.get("value") or "").strip(), re.I)]
                    if len(candidates) != 1:
                        result = {"ok": False, "message": f"Found {len(candidates)} unambiguous Next/Continue controls. Please use the portal's visible button yourself, then Capture current portal page."}
                    else:
                        meta = candidates[0]
                        locs = page.locator("button,input[type=submit],input[type=button]")
                        # Match by visible text/value; do not click any Submit/Apply control here.
                        target = None
                        for i in range(locs.count()):
                            loc = locs.nth(i)
                            try:
                                label = (loc.inner_text(timeout=500) or loc.get_attribute("value") or loc.get_attribute("aria-label") or "").strip()
                                if label.casefold() == (meta.get("label") or "").casefold() and loc.is_visible() and loc.is_enabled():
                                    target = loc; break
                            except Exception: continue
                        if target is None:
                            result = {"ok": False, "message": "Could not safely identify that Continue control. Use the visible portal control manually."}
                        else:
                            target.click(timeout=5000); page.wait_for_timeout(1200)
                            state = _snapshot_browser_page(page, job_id, "advanced", "Moved to the next non-submit application step.")
                            _record_guided_event(job_id, "portal_advanced", "Moved to the next non-submit portal step.", {"portal_url": page.url})
                            result = {"ok": True, "message": state["message"], "state": state}
                elif action == "prepare_review":
                    state = _snapshot_browser_page(page, job_id, "review_captured", "Captured the current employer form for final review.")
                    if not state.get("role_verified"):
                        result = {"ok": False, "message": "The current page does not yet show the exact saved role. No final review prepared."}
                    else:
                        controls = _read_portal_controls(page)
                        items, missing = [], []
                        for meta in controls:
                            if meta.get("tag") not in ("input", "textarea", "select"):
                                continue
                            if meta.get("type") in ("hidden", "password", "button", "submit", "reset"):
                                continue
                            label = meta.get("label") or meta.get("name") or meta.get("id") or "Unlabeled field"
                            value = meta.get("value", "")
                            is_file_input = meta.get("type") == "file"
                            if not meta.get("visible") and not (is_file_input and str(value).strip()):
                                if is_file_input and meta.get("required") and not str(value).strip():
                                    missing.append(label)
                                continue
                            items.append({"label": label, "value": value, "type": meta.get("type", ""), "required": bool(meta.get("required"))})
                            if meta.get("required") and not str(value).strip():
                                missing.append(label)
                        # Fingerprint only in memory, so a changed form invalidates this review.
                        fingerprint = __import__("hashlib").sha256(json.dumps(items, sort_keys=True).encode("utf-8")).hexdigest()
                        state["review_items"] = items
                        state["required_missing"] = missing
                        state["review_fingerprint"] = fingerprint
                        application_field_present = any(re.search(r"name|email|phone|resume|cover.?letter|experience|education|application|qualification|linkedin|work history", str(item.get("label", "")), re.I) for item in items)
                        state["review_ready"] = bool(items and application_field_present and not missing and len(state.get("submit_candidates", [])) == 1)
                        state["message"] = ("Final review prepared. Check every answer and attachment below." if state["review_ready"] else
                            ("Review captured, but final submission is blocked: " + ("required fields are blank: " + ", ".join(missing[:12]) if missing else "there is not exactly one recognizable Submit button on this page")))
                        with _GUIDED_BROWSER_LOCK: _GUIDED_BROWSER_STATE.update(state)
                        _record_guided_event(job_id, "final_review_prepared", state["message"])
                        result = {"ok": True, "message": state["message"], "state": state}
                elif action == "submit":
                    with _GUIDED_BROWSER_LOCK:
                        current = dict(_GUIDED_BROWSER_STATE)
                    if current.get("job_id") != job_id or not current.get("review_ready") or not current.get("review_fingerprint"):
                        result = {"ok": False, "message": "Final submission is blocked because a complete final review has not been prepared."}
                    else:
                        controls = _read_portal_controls(page)
                        items = []
                        for meta in controls:
                            if meta.get("tag") not in ("input", "textarea", "select") or meta.get("type") in ("hidden", "password", "button", "submit", "reset"):
                                continue
                            is_file_input = meta.get("type") == "file"
                            if not meta.get("visible") and not (is_file_input and str(meta.get("value", "")).strip()):
                                continue
                            items.append({"label": meta.get("label") or meta.get("name") or meta.get("id") or "Unlabeled field", "value": meta.get("value", ""), "type": meta.get("type", ""), "required": bool(meta.get("required"))})
                        fingerprint = __import__("hashlib").sha256(json.dumps(items, sort_keys=True).encode("utf-8")).hexdigest()
                        if fingerprint != current.get("review_fingerprint"):
                            result = {"ok": False, "message": "The portal form changed after final review. No Submit button was clicked. Prepare and review the form again."}
                        else:
                            button_candidates = []
                            for m in controls:
                                label = (m.get("label") or m.get("value") or "").strip()
                                if m.get("visible") and not m.get("disabled") and m.get("tag") in ("button", "input") and re.fullmatch(r"submit(?: application)?", label, re.I):
                                    button_candidates.append(m)
                            if len(button_candidates) != 1:
                                result = {"ok": False, "message": f"Final submission blocked: found {len(button_candidates)} exact Submit button candidates."}
                            else:
                                candidates = page.locator("button,input[type=submit],input[type=button]")
                                target = None
                                candidate_label = (button_candidates[0].get("label") or button_candidates[0].get("value") or "").strip().casefold()
                                for i in range(candidates.count()):
                                    loc = candidates.nth(i)
                                    try:
                                        label = (loc.inner_text(timeout=500) or loc.get_attribute("value") or loc.get_attribute("aria-label") or "").strip().casefold()
                                        if label == candidate_label and loc.is_visible() and loc.is_enabled():
                                            target = loc; break
                                    except Exception: continue
                                if target is None:
                                    result = {"ok": False, "message": "Could not identify the final Submit button reliably. No click was made."}
                                else:
                                    before_url = page.url
                                    target.click(timeout=8000)
                                    try: page.wait_for_load_state("domcontentloaded", timeout=20000)
                                    except Exception: pass
                                    page.wait_for_timeout(2200)
                                    try: body_after = page.locator("body").inner_text(timeout=5000)
                                    except Exception: body_after = ""
                                    patterns = [r"thank you for applying", r"application (?:has been )?(?:successfully )?submitted", r"we have received your application", r"application received", r"submission successful", r"your application (?:was )?submitted", r"confirmation number"]
                                    found = next((re.search(pat, body_after, re.I) for pat in patterns if re.search(pat, body_after, re.I)), None)
                                    evidence = ""
                                    if found:
                                        evidence = re.sub(r"\s+", " ", body_after[max(0, found.start()-120):found.end()+260]).strip()[:500]
                                    if found:
                                        local_date = datetime.now().astimezone().date().isoformat()
                                        _record_guided_event(job_id, "submission_verified", "Employer portal displayed a submission confirmation.", {
                                            "submission_verified": True, "confirmation_evidence": {"at": now_iso(), "url": page.url, "text": evidence},
                                            "submission_attempted_at": now_iso(), "step": "submission_verified"})
                                        with LOCK:
                                            records = read_records(APPLICATIONS_FILE)
                                            apprec = next((x for x in records if x.get("job_id") == job_id), None)
                                            if apprec is not None:
                                                apprec["status"] = "Applied"
                                                apprec["applied_date"] = local_date
                                                apprec["notes"] = "Employer submission confirmation verified by JobSearchAgent."
                                                apprec["updated_at"] = now_iso()
                                                save_records(APPLICATIONS_FILE, records)
                                        state = _snapshot_browser_page(page, job_id, "submission_verified", "Employer confirmation text detected; application status updated to Applied.")
                                        result = {"ok": True, "message": "Employer submission confirmation detected. Application recorded as Applied.", "evidence": evidence, "state": state}
                                    else:
                                        evidence = re.sub(r"\s+", " ", body_after).strip()[:400]
                                        _record_guided_event(job_id, "submission_attempted_unverified", "Submit action was taken after explicit approval, but no reliable confirmation was detected.", {
                                            "submission_verified": False, "submission_attempted_at": now_iso(),
                                            "unverified_result": {"at": now_iso(), "before_url": before_url, "url": page.url, "text_excerpt": evidence},
                                            "step": "submission_attempted_unverified"})
                                        state = _snapshot_browser_page(page, job_id, "submission_attempted_unverified", "Submission was attempted, but the employer confirmation could not be verified. The tracker will not mark this as Applied.")
                                        result = {"ok": False, "message": "Submit was clicked with your approval, but no reliable confirmation was detected. Status was not marked Applied; verify the portal or confirmation email.", "state": state}
            else:
                result = {"ok": False, "message": f"Unknown browser action: {action}"}
        except Exception as exc:
            message = (type(exc).__name__ + ": " + str(exc)).replace(str(BROWSER_PROFILE_DIR), "<local browser profile>")[:500]
            result = {"ok": False, "message": message}
            with _GUIDED_BROWSER_LOCK:
                _GUIDED_BROWSER_STATE.update({"status": "error", "job_id": job_id, "message": message, "captured_at": now_iso()})
        finally:
            if action == "submit":
                with _GUIDED_BROWSER_LOCK:
                    if _GUIDED_BROWSER_STATE.get("job_id") == job_id:
                        _GUIDED_BROWSER_STATE["submit_in_progress"] = False
            cmd["result"] = result
            cmd["done"].set()


def _guided_browser_command(action: str, job_id: str, payload: dict | None = None, timeout: int = 35) -> dict:
    global _GUIDED_BROWSER_THREAD
    job = _get_job(job_id)
    portal = _portal_config_for_job(job)
    if not portal or not _is_allowed_employer_portal(portal.get("url", ""), job_id):
        return {"ok": False, "message": "No verified official portal mapping is available for this saved employer."}
    with _GUIDED_BROWSER_LOCK:
        if _GUIDED_BROWSER_THREAD is None or not _GUIDED_BROWSER_THREAD.is_alive():
            _GUIDED_BROWSER_THREAD = threading.Thread(target=_browser_worker, name="JobSearchAgentPortal", daemon=True)
            _GUIDED_BROWSER_THREAD.start()
    cmd = {"action": action, "job_id": job_id, "payload": payload or {}, "done": threading.Event()}
    _GUIDED_BROWSER_QUEUE.put(cmd)
    if not cmd["done"].wait(timeout):
        return {"ok": False, "message": "The browser action is still running. Refresh this guided page to see its current state."}
    return cmd.get("result", {"ok": False, "message": "No browser result returned."})


def _guided_download_response(handler, job_id: str, kind: str) -> None:
    rec = _application_for_job(job_id) or {}
    paths = (rec.get("guided_workflow") or {}).get("document_paths", {})
    key = "resume" if kind == "resume" else "cover_letter" if kind == "cover-letter" else ""
    path_text = paths.get(key, "") if key else ""
    if not path_text:
        handler.respond(404, render_page("workspace", '<h2>Document not generated yet.</h2><a href="/guided-application?job_id=' + escape(job_id, quote=True) + '">Return to guided application</a>'))
        return
    path = Path(path_text).resolve()
    if path.parent != APPLICATION_DOCS_DIR.resolve() or not path.is_file():
        handler.respond(404, render_page("workspace", '<h2>Document is not available in the application folder.</h2><a href="/guided-application?job_id=' + escape(job_id, quote=True) + '">Return to guided application</a>'))
        return
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    handler.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def guided_application_page(job_id: str) -> str:
    job = _get_job(job_id)
    if not job:
        return render_page("workspace", '<h2>Saved job not found</h2><p class="sub">No records were changed.</p><a href="/workspace">Return to Application Workspace</a>')
    if _guided_application_is_applied(job_id):
        applied_record = _application_for_job(job_id) or {}
        title = escape(str(job.get("title", "")))
        company = escape(str(job.get("company", "")))
        applied_date = escape(str(applied_record.get("applied_date", "") or "Not recorded"))
        body = '<h2>Application already marked Applied</h2>'
        body += '<div class="warning"><b>Further application actions are disabled.</b> This job is already recorded as Applied, so JobSearchAgent is blocking the guided workflow to help prevent a duplicate application.</div>'
        body += '<div class="panel"><h3>Recorded job</h3><p><b>' + title + '</b><br>' + company + '</p><p><span class="tag">Applied</span> &nbsp; Applied date: ' + applied_date + '</p>'
        body += '<p>To proceed again only if this status is wrong, go to Application Workspace and edit this existing record. Do not add another record.</p>'
        body += '<p><a href="/workspace#application-tracker">Return to Application Workspace</a></p></div>'
        return render_page("workspace", body)
    apprec = _application_for_job(job_id) or {}
    flow = apprec.get("guided_workflow", {}) if isinstance(apprec.get("guided_workflow", {}), dict) else {}
    with _GUIDED_BROWSER_LOCK:
        browser = dict(_GUIDED_BROWSER_STATE) if _GUIDED_BROWSER_STATE.get("job_id") == job_id else {}
    docs = flow.get("document_paths", {})
    title = escape(str(job.get("title", "")))
    company = escape(str(job.get("company", "")))
    location = escape(str(job.get("location", "")))
    job_url = str(job.get("source_url", ""))
    job_link = f'<a href="{escape(job_url, quote=True)}" target="_blank" rel="noopener">Open saved job-source listing</a>' if job_url.startswith(("https://", "http://")) else "No source URL saved."
    portal_cfg = _portal_config_for_job(job)
    portal_matches = bool(portal_cfg)
    body = '<h2>Guided Application Agent</h2><p class="sub">Real application pilot · employer match required · confirmation required before final submission.</p>'
    body += f'<div class="panel"><h3>Target role</h3><p><b>{title}</b><br>{company} · {location}</p><p class="label">Saved job ID: {escape(job_id)}</p><p>{job_link}</p>'
    if portal_matches:
        if portal_cfg.get("source") == "listing_discovery":
            body += f'<p><a href="{escape(portal_cfg["url"], quote=True)}" target="_blank" rel="noopener">Open discovered {escape(company)} application portal candidate</a></p>'
            body += '<p class="warning">This link was discovered from the saved job listing using employer-name evidence. The exact role must still be verified on the destination before application steps continue.</p>'
        else:
            body += f'<p><a href="{escape(portal_cfg["url"], quote=True)}" target="_blank" rel="noopener">Official {escape(portal_cfg["label"])} careers portal</a></p>'
    else:
        body += '<div class="action-feedback error" role="alert"><b>Portal setup required for this selected employer</b><p>' + escape(_guided_portal_mismatch_message(job)) + '</p></div>'
        body += '</div><div class="panel"><h3>Configure this employer dynamically</h3>'
        body += '<p>Open the saved job listing, find the employer’s official careers/application portal, and paste that HTTPS URL below. Do not paste a job-board URL unless it is genuinely the employer’s official application portal. This mapping is saved by employer name and reused for its other saved jobs.</p>'
        body += '<form method="post" action="/guided/configure-portal">'
        body += '<input type="hidden" name="job_id" value="' + escape(job_id, quote=True) + '">'
        body += '<label>Employer <input type="text" name="employer_label" value="' + escape(str(job.get("company", "")), quote=True) + '" readonly></label>'
        body += '<label>Official recruitment portal URL <input type="url" name="portal_url" placeholder="https://careers.example.com/" required></label>'
        body += '<label><input type="checkbox" name="confirm_official_portal" value="YES" required style="display:inline;width:auto;min-height:auto"> I verified this HTTPS URL is the official recruitment portal for this employer.</label>'
        body += '<button type="submit">Save verified portal for this employer</button></form>'
        body += '<p><a href="/workspace">Return to Application Workspace</a> · <a href="' + escape(job_url, quote=True) + '" target="_blank" rel="noopener">Open saved job listing</a></p></div>'
    body += '<div class="panel"><h3>Step 1 — Prepare documents and open the employer portal</h3><p>The agent reuses this job’s single application record. Documents and portal navigation are allowed only after the saved employer matches the configured portal. Final submission always requires explicit approval.</p>'
    if portal_matches:
        body += '<form method="post" action="/guided/start"><input type="hidden" name="job_id" value="' + escape(job_id, quote=True) + '"><button type="submit">Start / resume guided application</button></form>'
    else:
        body += '<p class="warning">Start is disabled for this record. Do not start until an employer portal is resolved and the exact saved role can be verified.</p>'
    body += '</div>'
    body += '<div class="panel"><h3>Step 2 — Review and approve the documents</h3>'
    if not portal_matches:
        body += '<p class="warning">Document generation, download, and approval are blocked until the correct employer portal is verified. Existing document links are hidden for this mismatched job.</p>'
    elif docs.get("resume") and Path(docs.get("resume", "")).is_file():
        body += '<p>Resume: <a href="/guided-download?job_id=' + escape(job_id, quote=True) + '&kind=resume">Download tailored resume (.docx)</a></p>'
        body += '<p>Cover letter: <a href="/guided-download?job_id=' + escape(job_id, quote=True) + '&kind=cover-letter">Download tailored cover letter (.docx)</a></p>'
        body += '<p class="label">Both documents were generated inside the local app. Review them before approving.</p><p class="label">Local document folder: ' + escape(str(APPLICATION_DOCS_DIR)) + '</p>'
        if flow.get("documents_approved"):
            body += '<p><span class="tag">Documents approved</span></p>'
        else:
            body += '<form method="post" action="/guided/approve-documents"><input type="hidden" name="job_id" value="' + escape(job_id, quote=True) + '"><button type="submit">I reviewed and approve both documents</button></form>'
    else:
        if portal_matches:
            body += '<p>Documents have not been generated in the local app yet. Click Start / resume guided application above.</p>'
        else:
            body += '<p>Documents have not been generated. Verify and save this employer’s official portal above first; Start remains blocked until then.</p>'
    body += '</div>'
    body += '<div class="panel"><h3>Step 3 — Employer portal navigation</h3>'
    action_feedback = LAST_GUIDED_ACTION_RESULT
    if action_feedback.get("job_id") == job_id and action_feedback.get("message"):
        feedback_kind = "success" if action_feedback.get("ok") else "error" if action_feedback.get("ok") is False else "info"
        feedback_title = "Action completed" if action_feedback.get("ok") else "Action needs attention" if action_feedback.get("ok") is False else "Action status"
        feedback_time = action_feedback.get("at", "")
        body += '<div class="action-feedback ' + feedback_kind + '" role="status" aria-live="polite"><b>' + escape(action_feedback.get("action", "Portal action")) + ' — ' + escape(feedback_title) + '</b><p>' + escape(action_feedback.get("message", "")) + '</p>'
        if feedback_time:
            body += '<p class="label">Updated: ' + escape(feedback_time) + '</p>'
        body += '</div>'
    if browser:
        body += f'<p><b>Browser status:</b> {escape(browser.get("status", "unknown"))}</p><p>{escape(browser.get("message", ""))}</p>'
        if browser.get("title") or browser.get("url"):
            body += f'<p class="label">Current page: {escape(browser.get("title", ""))}<br>{escape(browser.get("url", ""))}</p>'
        if browser.get("role_verified") or flow.get("employer_job_verified"):
            body += '<p><span class="tag">Target role verified on employer portal</span></p>'
        elif browser.get("body_excerpt"):
            body += '<p class="warning">The exact target role has not been verified on the current employer page yet. In the managed browser, search for and open the exact saved role, then click Capture current portal page here.</p>'
        body += '<details><summary>Current page excerpt (local only)</summary><p>' + escape(browser.get("body_excerpt", "")) + '</p></details>'
        controls = browser.get("controls", [])
        if controls:
            rows = []
            for item in controls[:60]:
                if item.get("tag") in ("input", "textarea", "select") and item.get("visible") and item.get("type") not in ("password", "hidden"):
                    rows.append('<tr><td>' + escape(item.get("label") or item.get("name") or item.get("id") or "Unlabeled field") + '</td><td>' + escape(item.get("type", "")) + '</td><td>' + escape(item.get("value", "")) + '</td><td>' + ("Required" if item.get("required") else "") + '</td></tr>')
            if rows:
                body += '<details><summary>Visible portal form fields</summary><div class="tablewrap"><table><thead><tr><th>Field</th><th>Type</th><th>Current value</th><th>Rule</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div></details>'
    else:
        if portal_matches:
            body += '<p>The managed browser has not been started yet. Review the saved job, then click Start / resume guided application to open the official portal for this employer.</p>'
        else:
            body += '<p>The managed browser has not been started because no verified official portal mapping is available for this employer.</p>'
    if portal_matches:
        body += '<div class="formgrid" style="margin-top:14px">'
        for action, label in [("capture", "Capture current portal page"), ("fill_basics", "Fill clearly matched basic fields"), ("attach_documents", "Attach approved resume / letter"), ("continue", "Continue to next non-submit step"), ("prepare_review", "Prepare final review")]:
            body += '<form method="post" action="/guided/' + action + '"><input type="hidden" name="job_id" value="' + escape(job_id, quote=True) + '"><button type="submit">' + label + '</button></form>'
        body += '</div>'
    else:
        body += '<p class="warning">Portal actions are unavailable until an official portal mapping exists and the exact saved role is verified.</p>'
    body += '</div>'
    if browser and browser.get("status") == "review_captured":
        body += '<div class="panel"><h3>Step 4 — Final application review</h3><p>' + escape(browser.get("message", "")) + '</p>'
        items = browser.get("review_items", [])
        if items:
            body += '<div class="tablewrap"><table><thead><tr><th>Application field</th><th>Current answer</th><th>Required</th></tr></thead><tbody>'
            for item in items:
                body += '<tr><td>' + escape(item.get("label", "")) + '</td><td>' + escape(item.get("value", "") or "—") + '</td><td>' + ("Yes" if item.get("required") else "No") + '</td></tr>'
            body += '</tbody></table></div>'
        body += '<p><b>Documents attached by the agent:</b> ' + escape(', '.join(browser.get("uploaded_docs", [])) if browser.get("uploaded_docs") else "None confirmed by the agent; check the employer portal attachments manually.") + '</p>'
        if browser.get("submit_in_progress"):
            body += '<p class="warning">Final submission action is in progress. Do not click Submit again. Refresh this page to check the verified outcome.</p>'
        elif browser.get("required_missing"):
            body += '<p class="warning"><b>Blocked:</b> Required fields still blank: ' + escape(', '.join(browser.get("required_missing", [])[:20])) + '. Complete them in the employer portal and prepare the review again.</p>'
        elif not browser.get("review_ready"):
            body += '<p class="warning">Submission is blocked because the app could not confirm one unambiguous Submit button. No final submission action is available yet.</p>'
        else:
            body += '<p class="warning">Final gate: review the employer, role, each answer, and the attachments in the table above. The next button authorizes the agent to click the portal’s unique Submit button.</p>'
            if not browser.get("uploaded_docs"):
                body += '<label><input type="checkbox" name="attachments_reviewed" value="YES" form="final-submit-form" required style="display:inline;width:auto;min-height:auto"> I visually verified in the employer portal that the required resume and any required cover letter are attached.</label><br>'
            body += '<form id="final-submit-form" method="post" action="/guided/confirm-submit"><input type="hidden" name="job_id" value="' + escape(job_id, quote=True) + '"><label><input type="checkbox" name="final_authorization" value="YES" required style="display:inline;width:auto;min-height:auto"> I reviewed the answers and attachments above and explicitly authorize JobSearchAgent to submit this application.</label><br><button type="submit">Confirm and submit application</button></form>'
        body += '</div>'
    if flow.get("submission_verified"):
        evidence = flow.get("confirmation_evidence", {})
        body += '<div class="panel"><h3>Step 5 — Submission verified</h3><p><span class="tag">Applied</span> Employer confirmation evidence was detected. Confirmation time: ' + escape(evidence.get("at", "")) + '</p><p class="label">Confirmation source: ' + escape(evidence.get("url", "")) + '</p><p>' + escape(evidence.get("text", "")) + '</p></div>'
    elif flow.get("user_confirmed_external_submission"):
        body += '<div class="panel"><h3>Step 5 — Submission recorded</h3><p><span class="tag">Applied · user-confirmed</span></p><p>You confirmed that you submitted on the employer website. JobSearchAgent recorded your confirmation but did not independently verify an employer receipt.</p><p class="label">User confirmation time: ' + escape(flow.get("user_confirmed_external_submission_at", "")) + '</p></div>'
    elif flow.get("step") == "submission_attempted_unverified":
        body += '<div class="warning"><b>Submission attempted, not verified.</b> JobSearchAgent has not marked this application Applied. Check the employer portal or confirmation email, then preserve the evidence.</div>'
    body += '<p><a href="/workspace">← Return to Application Workspace</a></p>'
    return render_page("workspace", body)


def _serve_guided_download(handler, job_id: str, kind: str) -> None:
    _guided_download_response(handler, job_id, kind)


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
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/guided-application":
            job_id = query.get("job_id", [""])[0]
            try:
                page = guided_application_page(job_id)
                self.respond(200, page)
            except Exception as exc:
                # Keep a bad saved record or portal configuration from dropping
                # the HTTP connection; log details in Terminal for diagnosis.
                print(f"ERROR /guided-application job_id={job_id!r}: {exc!r}", flush=True)
                traceback.print_exc()
                safe_id = escape(job_id)
                body = (
                    '<h2>Guided Application could not load</h2>'
                    '<p>The dashboard server is still running. No application was submitted and no records were changed.</p>'
                    '<p>Details were written to the Terminal running JobSearchAgent.</p>'
                    '<p><a href="/workspace">Return to Application Workspace</a></p>'
                    '<p class="label">Job ID: ' + safe_id + '</p>'
                )
                try:
                    self.respond(500, render_page("workspace", body))
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return
        if parsed.path == "/guided-download":
            _serve_guided_download(self, query.get("job_id", [""])[0], query.get("kind", [""])[0])
            return
        if parsed.path == "/workspace":
            edit_id = parse_qs(parsed.query).get("edit_id", [""])[0]
            self.respond(200, workspace_page(edit_id))
            return
        if parsed.path == "/shortlist":
            edit_id = parse_qs(parsed.query).get("edit_id", [""])[0]
            self.respond(200, shortlist_page(edit_id))
            return
        routes = {"/": overview_page, "/overview": overview_page, "/sources": live_sources_page, "/reports": reports_page}
        fn = routes.get(parsed.path)
        if fn is None:
            self.respond(404, render_page("", '<h2>Page not found</h2><a href="/">Return to Overview</a>'))
            return
        self.respond(200, fn())

    def do_POST(self):
        path = urlparse(self.path).path
        length = min(int(self.headers.get("Content-Length", "0") or 0), 1_000_000)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = {key: value[0].strip() for key, value in parse_qs(raw, keep_blank_values=True).items()}
        if path == "/guided/configure-portal":
            job_id = form.get("job_id", "")
            job = _get_job(job_id)
            if not job:
                self.respond(404, render_page("workspace", '<h2>Saved job not found.</h2><a href="/workspace">Return to workspace</a>')); return
            if _guided_application_is_applied(job_id):
                self.respond(409, render_page("workspace", '<h2>Application already marked Applied.</h2><p>Portal configuration is disabled for this job to prevent accidental duplicate application activity.</p><a href="/workspace#application-tracker">Return to workspace</a>')); return
            portal_url = form.get("portal_url", "").strip()
            parsed_portal = urlparse(portal_url)
            company = str(job.get("company", "")).strip()
            host = (parsed_portal.hostname or "").lower().rstrip(".")
            if form.get("confirm_official_portal") != "YES":
                error = "Confirm that you verified the official employer portal before saving."
            elif parsed_portal.scheme != "https" or not host or parsed_portal.username or parsed_portal.password:
                error = "Portal URL must be a valid HTTPS URL without embedded credentials."
            elif ("simon-kucher" in host and "simon kucher" not in _employer_key(company)) or ("insightglobal" in host and "insight global" not in _employer_key(company)):
                error = "That URL appears to belong to a different known employer. Choose the portal for the selected job's employer."
            else:
                error = ""
            if error:
                LAST_GUIDED_ACTION_RESULT.update({"job_id": job_id, "action": "Configure employer portal", "ok": False, "message": error, "at": now_iso()})
                self.respond(400, guided_application_page(job_id)); return
            try:
                _save_verified_employer_portal(company, company, portal_url)
                _record_guided_event(job_id, "employer_portal_configured", "User verified and saved the official portal for this employer.", {"portal_url": portal_url, "portal_verified_by_user": True})
                LAST_GUIDED_ACTION_RESULT.update({"job_id": job_id, "action": "Configure employer portal", "ok": True, "message": "Verified portal saved for " + company + ". Other saved jobs for this employer will resolve to the same mapping.", "at": now_iso()})
            except Exception as exc:
                self.respond(400, render_page("workspace", '<h2>Could not save portal mapping.</h2><p>' + escape(str(exc)) + '</p><a href="/workspace">Return to workspace</a>')); return
            self.send_response(303); self.send_header("Location", "/guided-application?job_id=" + job_id); self.end_headers(); return
        if path == "/guided/start":
            job_id = form.get("job_id", "")
            if _guided_application_is_applied(job_id):
                self.respond(409, render_page("workspace", '<h2>Application already marked Applied.</h2><p>Guided application is disabled to help prevent a duplicate application. Edit the existing tracker record only if this status is incorrect.</p><a href="/workspace#application-tracker">Return to Application Workspace</a>')); return
            job = _get_job(job_id)
            if not job:
                self.respond(404, render_page("workspace", '<h2>Saved job not found.</h2><a href="/workspace">Return to workspace</a>')); return
            if not _portal_config_for_job(job):
                LAST_GUIDED_ACTION_RESULT.update({"job_id": job_id, "action": "Start / resume guided application", "ok": False, "message": _guided_portal_mismatch_message(job), "at": now_iso()})
                self.respond(409, guided_application_page(job_id)); return
            apprec = _ensure_application_record(job_id)
            if apprec is None:
                self.respond(404, render_page("workspace", '<h2>Could not create or find the application record. No job data was changed.</h2><a href="/workspace">Return to workspace</a>')); return
            try:
                document_paths = _make_guided_documents(job)
                _record_guided_event(job_id, "documents_generated", "Tailored resume and cover letter generated in data_v2/application_documents.", {"document_paths": document_paths, "documents_approved": False})
            except Exception as exc:
                _record_guided_event(job_id, "document_generation_blocked", str(exc)[:300])
            result = _guided_browser_command("start", job_id, timeout=65)
            if result.get("ok"):
                portal_cfg_label = (_portal_config_for_job(job) or {}).get("label", "employer")
                _record_guided_event(job_id, "portal_opened", f"Opened official {portal_cfg_label} careers portal in managed browser.", {"portal_url": (_portal_config_for_job(job) or {}).get("url", "")})
            else:
                _record_guided_event(job_id, "portal_open_blocked", result.get("message", "Browser launch failed."))
                with _GUIDED_BROWSER_LOCK:
                    if _GUIDED_BROWSER_STATE.get("job_id") != job_id:
                        _GUIDED_BROWSER_STATE.update({"status": "blocked", "job_id": job_id, "message": result.get("message", "Browser launch failed."), "captured_at": now_iso()})
            self.send_response(303); self.send_header("Location", "/guided-application?job_id=" + job_id); self.end_headers(); return
        if path == "/guided/approve-documents":
            job_id = form.get("job_id", "")
            if _guided_application_is_applied(job_id):
                self.respond(409, render_page("workspace", '<h2>Application already marked Applied.</h2><p>Document approval is disabled for this job to help prevent duplicate applications.</p><a href="/workspace#application-tracker">Return to Application Workspace</a>')); return
            apprec = _ensure_application_record(job_id)
            flow = (apprec or {}).get("guided_workflow", {})
            docs = flow.get("document_paths", {})
            if not docs.get("resume") or not Path(docs.get("resume", "")).is_file() or not docs.get("cover_letter") or not Path(docs.get("cover_letter", "")).is_file():
                self.respond(400, guided_application_page(job_id)); return
            _record_guided_event(job_id, "documents_approved", "User approved the tailored resume and cover letter for this application.", {"documents_approved": True})
            self.send_response(303); self.send_header("Location", "/guided-application?job_id=" + job_id); self.end_headers(); return
        if path.startswith("/guided/"):
            action_map = {
                "/guided/capture": "capture", "/guided/fill_basics": "fill_basics",
                "/guided/attach_documents": "attach_documents", "/guided/continue": "continue",
                "/guided/prepare_review": "prepare_review",
            }
            job_id = form.get("job_id", "")
            action = action_map.get(path)
            if _guided_application_is_applied(job_id):
                self.respond(409, render_page("workspace", '<h2>Application already marked Applied.</h2><p>Portal actions and final submission are disabled for this job. This safeguard helps prevent a duplicate application.</p><a href="/workspace#application-tracker">Return to Application Workspace</a>')); return
            if path == "/guided/confirm-submit":
                origin = self.headers.get("Origin", "")
                if origin:
                    origin_parts = urlparse(origin)
                    if origin_parts.scheme != "http" or origin_parts.hostname not in ("127.0.0.1", "localhost") or origin_parts.port != PORT:
                        self.respond(403, render_page("workspace", '<h2>Final submission blocked.</h2><p>Authorization must come from the local JobSearchAgent dashboard.</p><a href="/workspace">Return to workspace</a>'))
                        return
                authorization = form.get("final_authorization", "")
                with _GUIDED_BROWSER_LOCK:
                    state = dict(_GUIDED_BROWSER_STATE)
                flow = _workflow_for_job(job_id)
                if not _portal_config_for_job(_get_job(job_id)):
                    _record_guided_event(job_id, "final_submit_blocked", _guided_portal_mismatch_message(_get_job(job_id)))
                    self.send_response(303); self.send_header("Location", "/guided-application?job_id=" + job_id); self.end_headers(); return
                attachments_ok = bool(state.get("uploaded_docs")) or form.get("attachments_reviewed") == "YES"
                if authorization != "YES" or not attachments_ok or state.get("job_id") != job_id or state.get("status") != "review_captured" or state.get("submit_in_progress") or not state.get("review_ready") or not flow.get("documents_approved") or not flow.get("employer_job_verified"):
                    _record_guided_event(job_id, "final_submit_blocked", "Final submit was blocked because authorization, attachment verification, document approval, role verification, or final-review checks were incomplete.")
                else:
                    with _GUIDED_BROWSER_LOCK:
                        if _GUIDED_BROWSER_STATE.get("job_id") == job_id:
                            _GUIDED_BROWSER_STATE["submit_in_progress"] = True
                    result = _guided_browser_command("submit", job_id, timeout=45)
                    if not result.get("ok"):
                        with _GUIDED_BROWSER_LOCK:
                            current_browser_status = _GUIDED_BROWSER_STATE.get("status", "")
                            _GUIDED_BROWSER_STATE["submit_in_progress"] = False
                            _GUIDED_BROWSER_STATE["message"] = result.get("message", "Submit did not return verified success.")
                        if current_browser_status != "submission_attempted_unverified":
                            _record_guided_event(job_id, "final_submit_blocked_or_unverified", result.get("message", "Submit did not return verified success."))
                self.send_response(303); self.send_header("Location", "/guided-application?job_id=" + job_id); self.end_headers(); return
            if not action or not _get_job(job_id):
                self.respond(400, render_page("workspace", '<h2>Invalid guided application action or saved job.</h2><a href="/workspace">Return to workspace</a>')); return
            if not _portal_config_for_job(_get_job(job_id)):
                result = {"ok": False, "message": _guided_portal_mismatch_message(_get_job(job_id))}
            else:
                result = _guided_browser_command(action, job_id, timeout=45)
            action_labels = {
                "capture": "Capture current portal page",
                "fill_basics": "Fill clearly matched basic fields",
                "attach_documents": "Attach approved resume / letter",
                "continue": "Continue to next non-submit step",
                "prepare_review": "Prepare final review",
            }
            LAST_GUIDED_ACTION_RESULT.update({
                "job_id": job_id,
                "action": action_labels.get(action, action),
                "ok": bool(result.get("ok")),
                "message": result.get("message", "No confirmation message was returned."),
                "at": now_iso(),
            })
            if not result.get("ok"):
                _record_guided_event(job_id, "portal_action_blocked", result.get("message", "Portal action was not completed."))
                with _GUIDED_BROWSER_LOCK:
                    if _GUIDED_BROWSER_STATE.get("job_id") == job_id:
                        _GUIDED_BROWSER_STATE["message"] = result.get("message", "Portal action was not completed.")
            self.send_response(303); self.send_header("Location", "/guided-application?job_id=" + job_id); self.end_headers(); return
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
        if path in ("/add-job", "/update-job"):
            title, company = form.get("title", "").strip(), form.get("company", "").strip()
            if not title or not company:
                self.respond(400, render_page("shortlist", '<h2>Title and company are required.</h2><a href="/shortlist">Go back</a>'))
                return
            source_url = form.get("source_url", "").strip()
            if source_url and not source_url.startswith(("https://", "http://")):
                source_url = ""
            values = {
                "title": title[:200], "company": company[:200], "location": form.get("location", "")[:200],
                "source_url": source_url[:1000], "posting_date": form.get("posting_date", "")[:30],
                "work_type": form.get("work_type", "")[:100], "description": form.get("description", "")[:20000],
                "required_skills": [part.strip()[:80] for part in form.get("required_skills", "").split(",") if part.strip()][:50],
                "verified_skills": [part.strip()[:80] for part in form.get("verified_skills", "").split(",") if part.strip()][:50],
            }
            with LOCK:
                existing = read_records(JOBS_FILE)
                if path == "/update-job":
                    job_id = form.get("job_id", "")
                    target = next((row for row in existing if row.get("id") == job_id), None)
                    if target is None:
                        self.respond(404, render_page("shortlist", '<h2>Saved job not found; no changes were made.</h2><a href="/shortlist">Return to shortlist</a>'))
                        return
                    if any(row.get("id") != job_id and row.get("title", "").casefold() == title.casefold() and row.get("company", "").casefold() == company.casefold() for row in existing):
                        self.respond(409, render_page("shortlist", '<h2>Another saved job already has this role and company. No changes were made.</h2><a href="/shortlist">Return to shortlist</a>'))
                        return
                    target.update(values)
                    target["updated_at"] = now_iso()
                else:
                    if any(row.get("title", "").casefold() == title.casefold() and row.get("company", "").casefold() == company.casefold() for row in existing):
                        self.respond(409, render_page("shortlist", '<h2>This role and company already exist in this new shortlist.</h2><a href="/shortlist">Return to shortlist</a>'))
                        return
                    existing.append({"id": f"job-{datetime.now().strftime('%Y%m%d%H%M%S%f')}", **values, "created_at": now_iso(), "source_method": "manual_verified_entry"})
                save_records(JOBS_FILE, existing)
            self.send_response(303); self.send_header("Location", "/shortlist"); self.end_headers()
            return
        if path == "/add-job_legacy_disabled":
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
        if path in ("/archive-application", "/restore-application"):
            application_id = form.get("application_id", "")
            with LOCK:
                records = read_records(APPLICATIONS_FILE)
                target = next((row for row in records if row.get("id") == application_id), None)
                if target is None:
                    self.respond(404, render_page("workspace", '<h2>Application record not found.</h2><a href="/workspace">Return to workspace</a>'))
                    return
                if path == "/archive-application":
                    # Safety guard: archiving is allowed only for entries explicitly marked TEST ONLY.
                    if "test only" not in str(target.get("notes", "")).casefold() or target.get("status") == "Applied":
                        self.respond(403, render_page("workspace", '<h2>Archive blocked.</h2><p>Only non-Applied records with an explicit TEST ONLY note can be archived. No records were changed.</p><a href="/workspace#application-tracker">Return to Application Workspace</a>'))
                        return
                    target["archived"] = True
                    target["archived_at"] = now_iso()
                    target["archive_reason"] = "test_record_review"
                else:
                    if not target.get("archived") or target.get("archive_reason") != "test_record_review":
                        self.respond(403, render_page("workspace", '<h2>Restore not allowed.</h2><p>This record is not an archived test record. No records were changed.</p><a href="/workspace">Return to Application Workspace</a>'))
                        return
                    target.pop("archived", None)
                    target.pop("archived_at", None)
                    target.pop("archive_reason", None)
                target["updated_at"] = now_iso()
                save_records(APPLICATIONS_FILE, records)
            self.send_response(303); self.send_header("Location", "/workspace#application-tracker" if path == "/archive-application" else "/workspace#archived-test-records"); self.end_headers()
            return
        if path in ("/add-application", "/update-application"):
            job_id = form.get("job_id", "")
            valid_jobs = {row.get("id") for row in jobs()}
            if job_id not in valid_jobs:
                self.respond(400, render_page("workspace", '<h2>Choose a saved job first.</h2><a href="/workspace">Go back</a>'))
                return
            statuses = {"Considering", "Preparing", "Applied", "Follow-up due", "Interview", "Rejected", "Offer", "Withdrawn"}
            status = form.get("status", "Considering")
            if status not in statuses:
                status = "Considering"
            applied_date_value = form.get("applied_date", "").strip()[:30]
            followup_date_value = form.get("followup_date", "").strip()[:30]
            date_error = validate_application_dates(applied_date_value, followup_date_value)
            if date_error:
                application_id_for_link = form.get("application_id", "") if path == "/update-application" else ""
                return_url = "/workspace?edit_id=" + escape(application_id_for_link, quote=True) + "#application-editor" if application_id_for_link else "/workspace#application-editor"
                self.respond(400, render_page("workspace", '<h2>Check the dates.</h2><p>' + escape(date_error) + '</p><a href="' + return_url + '">Return to the application form</a>'))
                return
            external_submission_confirmation = form.get("external_submission_confirmation") == "YES"
            if status == "Applied":
                existing_rows = read_records(APPLICATIONS_FILE)
                app_id_for_edit = form.get("application_id", "") if path == "/update-application" else ""
                app_for_job = next((x for x in existing_rows if x.get("id") == app_id_for_edit and x.get("job_id") == job_id and ((x.get("guided_workflow") or {}).get("submission_verified") is True or (x.get("guided_workflow") or {}).get("user_confirmed_external_submission") is True)), None) if app_id_for_edit else None
                if app_for_job is None and not external_submission_confirmation:
                    self.respond(409, render_page("workspace", '<h2>Submission confirmation needed.</h2><p>To record an application you submitted yourself, tick the explicit confirmation box in this form. JobSearchAgent will label it user-confirmed, not independently verified.</p><a href="/workspace?edit_id=' + escape(form.get("application_id", ""), quote=True) + '#application-editor">Return to this application</a>'))
                    return
            values = {"job_id": job_id, "status": status,
                      "applied_date": applied_date_value,
                      "followup_date": followup_date_value,
                      "notes": form.get("notes", "")[:3000], "updated_at": now_iso()}
            with LOCK:
                existing = read_records(APPLICATIONS_FILE)
                if path == "/update-application":
                    application_id = form.get("application_id", "")
                    target = next((row for row in existing if row.get("id") == application_id), None)
                    if target is None:
                        self.respond(404, render_page("workspace", '<h2>Application record not found.</h2><a href="/workspace">Return to workspace</a>'))
                        return
                    target.update(values)
                    if status == "Applied" and external_submission_confirmation:
                        flow = target.setdefault("guided_workflow", _new_guided_workflow())
                        flow["user_confirmed_external_submission"] = True
                        flow["user_confirmed_external_submission_at"] = now_iso()
                        flow["step"] = "user_confirmed_external_submission"
                        flow["updated_at"] = now_iso()
                        events = flow.setdefault("events", [])
                        events.append({"at": now_iso(), "step": "user_confirmed_external_submission", "message": "User confirmed they submitted on the employer website; receipt was not independently verified by JobSearchAgent."})
                        flow["events"] = events[-80:]
                        provenance_note = "Submission tracking: user-confirmed on employer website; receipt not independently verified by JobSearchAgent."
                        note_text = str(target.get("notes", "") or "").strip()
                        if provenance_note not in note_text:
                            target["notes"] = (note_text + "\n" if note_text else "") + provenance_note
                else:
                    new_record = {"id": f"app-{datetime.now().strftime('%Y%m%d%H%M%S%f')}", **values}
                    if status == "Applied" and external_submission_confirmation:
                        flow = _new_guided_workflow()
                        flow["user_confirmed_external_submission"] = True
                        flow["user_confirmed_external_submission_at"] = now_iso()
                        flow["step"] = "user_confirmed_external_submission"
                        flow["events"].append({"at": now_iso(), "step": "user_confirmed_external_submission", "message": "User confirmed they submitted on the employer website; receipt was not independently verified by JobSearchAgent."})
                        new_record["guided_workflow"] = flow
                        provenance_note = "Submission tracking: user-confirmed on employer website; receipt not independently verified by JobSearchAgent."
                        note_text = str(new_record.get("notes", "") or "").strip()
                        if provenance_note not in note_text:
                            new_record["notes"] = (note_text + "\n" if note_text else "") + provenance_note
                    existing.append(new_record)
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
    print("Guided application: verified mappings + listing-derived portal discovery; exact role verification and explicit final-submit approval required.")
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
