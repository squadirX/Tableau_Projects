#!/usr/bin/env python3
"""JobSearchAgent consolidated companion: safe audit, shortlist, tracking and local dashboard.

This module is deliberately additive. It never rewrites jobs.csv or config.json.
Malformed legacy CSV rows are reported and excluded from normalized output rather
than guessed into columns. Dashboard binds to loopback only.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import webbrowser
from collections import Counter
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(os.path.expanduser("~/JobSearchAgent"))
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
PROFILE_FILE = BASE_DIR / "candidate_profile.json"
LEGACY_JOBS = DATA_DIR / "jobs.csv"
NORMALIZED_JOBS = DATA_DIR / "jobs_normalized.csv"
APPLICATIONS_FILE = DATA_DIR / "applications.json"
AUDIT_FILE = DATA_DIR / "csv_integrity_audit.json"
SCHEMA = ["discovered_at", "posted_date", "source", "title", "company", "location",
          "score", "decision", "matched_skills", "negative_matches", "url",
          "evidence_notes", "application_status", "follow_up_date", "notes"]
LEGACY_SCHEMA = ["date", "source", "title", "company", "location", "score",
                 "decision", "matched_skills", "negative_matches", "url"]
STATUSES = ["Not applied", "Prepared", "Applied", "Interview", "Offer", "Rejected", "Withdrawn"]
EXCLUSIONS = ["unpaid", "commission-only", "commission only", "volunteer",
              "must be a us citizen", "u.s. citizenship required",
              "security clearance required", "requires relocation"]

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

def read_json(path, default):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default

def profile():
    p = read_json(PROFILE_FILE, {})
    return {k: p.get(k, []) for k in ("target_titles", "skills", "experience",
            "preferred_locations", "do_not_apply")} | {"verified_facts": p.get("verified_facts", [])}

def audit_csv():
    """Read-only audit. Never modifies the legacy source."""
    result = {"checked_at": now_text(), "source": str(LEGACY_JOBS), "exists": LEGACY_JOBS.exists(),
              "header": [], "rows_total": 0, "valid_10_column_rows": 0,
              "malformed_rows": 0, "row_widths": {}, "malformed_examples": [],
              "source_unchanged": True, "normalized_output": str(NORMALIZED_JOBS)}
    if not LEGACY_JOBS.exists():
        AUDIT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    widths = Counter()
    with LEGACY_JOBS.open("r", encoding="utf-8-sig", newline="", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        result["header"] = header
        for line_no, row in enumerate(reader, start=2):
            if not row or not any(cell.strip() for cell in row):
                continue
            result["rows_total"] += 1
            widths[str(len(row))] += 1
            if len(row) == len(header) == len(LEGACY_SCHEMA):
                result["valid_10_column_rows"] += 1
            else:
                result["malformed_rows"] += 1
                if len(result["malformed_examples"]) < 20:
                    result["malformed_examples"].append({
                        "line": line_no, "columns_found": len(row),
                        "preview": [cell[:100] for cell in row[:5]],
                        "action": "quarantined from normalized view; original row preserved"
                    })
    result["row_widths"] = dict(widths)
    AUDIT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result

def safe_normalize():
    """Create a separate normalized view using only structurally valid rows."""
    ensure_dirs()
    audit = audit_csv()
    rows = []
    if LEGACY_JOBS.exists():
        with LEGACY_JOBS.open("r", encoding="utf-8-sig", newline="", errors="replace") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            for raw in reader:
                if len(raw) != len(header) or len(header) != len(LEGACY_SCHEMA):
                    continue
                d = dict(zip(header, raw))
                url = d.get("url", "").strip()
                title = d.get("title", "").strip()
                # Reject shifted rows where essential fields do not resemble a job.
                if not (url.startswith(("http://", "https://")) and title):
                    continue
                rows.append({
                    "discovered_at": d.get("date", "").strip(), "posted_date": "",
                    "source": d.get("source", "").strip(), "title": title,
                    "company": d.get("company", "").strip(), "location": d.get("location", "").strip(),
                    "score": d.get("score", "").strip(), "decision": d.get("decision", "").strip(),
                    "matched_skills": d.get("matched_skills", "").strip(),
                    "negative_matches": d.get("negative_matches", "").strip(), "url": url,
                    "evidence_notes": "Imported from structurally valid legacy row; posting date not verified.",
                    "application_status": "Not applied", "follow_up_date": "", "notes": ""
                })
    # Stable dedupe by normalized URL; keep first occurrence and never infer missing values.
    unique, seen = [], set()
    for row in rows:
        key = row["url"].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    with NORMALIZED_JOBS.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCHEMA)
        writer.writeheader()
        writer.writerows(unique)
    audit["normalized_rows_written"] = len(unique)
    audit["normalization_policy"] = "Separate output; source jobs.csv unchanged; malformed rows excluded, not repaired."
    AUDIT_FILE.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    return audit

def read_jobs():
    if not NORMALIZED_JOBS.exists():
        safe_normalize()
    with NORMALIZED_JOBS.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def write_jobs(rows):
    with NORMALIZED_JOBS.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

def score_job(title, company, location, description, p=None):
    """Transparent fit estimate. Keyword presence is evidence, not proof of qualification."""
    p = p or profile()
    title, location = title or "", location or ""
    text = " ".join([title, company or "", location, description or ""]).lower()
    matches, negatives, evidence = [], [], []
    title_targets = [str(x).lower() for x in p.get("target_titles", [])]
    skills = [str(x).lower() for x in p.get("skills", [])]
    exp = [str(x).lower() for x in p.get("experience", [])]
    prefs = [str(x).lower() for x in p.get("preferred_locations", [])]
    target_match = next((x for x in title_targets if x and x in title.lower()), None)
    if target_match:
        evidence.append("Target-title phrase found: " + target_match)
    matches = [s for s in skills if s and s in text]
    exp_matches = [x for x in exp if x and x in text]
    negative_rules = list(dict.fromkeys([*EXCLUSIONS, *p.get("do_not_apply", [])]))
    negatives = [str(x) for x in negative_rules if str(x).strip() and str(x).lower() in text]
    score = (30 if target_match else 0) + min(40, 4 * len(matches)) + min(20, 4 * len(exp_matches))
    location_match = next((x for x in prefs if x and x in location.lower()), None)
    score += 10 if location_match else 0
    if matches: evidence.append("Keyword matches in listing: " + ", ".join(matches[:12]))
    if exp_matches: evidence.append("Experience phrases in listing: " + ", ".join(exp_matches[:8]))
    if location_match: evidence.append("Preferred location phrase: " + location_match)
    if not title.strip(): evidence.append("Title: Unknown")
    if not company or not company.strip(): evidence.append("Company: Unknown")
    if not location.strip(): evidence.append("Location: Unknown")
    if not (description or "").strip(): evidence.append("Description: Unknown; score has low confidence")
    decision = "SKIP" if negatives else ("APPLY" if score >= 70 else "REVIEW" if score >= 45 else "SKIP")
    return {"score": min(100, score), "decision": decision,
            "matched_skills": ", ".join(matches[:15]), "negative_matches": ", ".join(negatives),
            "evidence_notes": "; ".join(evidence),
            "confidence": "Low" if not description else "Keyword-based; manual review required"}

def load_applications():
    data = read_json(APPLICATIONS_FILE, [])
    return data if isinstance(data, list) else []

def save_applications(items):
    tmp = APPLICATIONS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(APPLICATIONS_FILE)

def esc(v):
    return html.escape(str(v or ""))

def page(title, body):
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>JobSearchAgent · """ + esc(title) + """</title>
<style>
body{font:15px system-ui,sans-serif;max-width:1200px;margin:24px auto;padding:0 16px;color:#20252b;background:#f6f7f9}
nav a{margin-right:16px}section,.card{background:white;border:1px solid #d9dee5;border-radius:10px;padding:16px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.num{font-size:26px;font-weight:700}
table{border-collapse:collapse;width:100%;background:white}th,td{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}th{background:#eef1f4}
input,select,textarea{padding:8px;max-width:100%;box-sizing:border-box}label{display:block;margin:8px 0}button{padding:9px 14px;cursor:pointer}
small,.muted{color:#5b6470}.warn{color:#8a4200}.scroll{overflow:auto}a{color:#1456a0}
</style></head><body><h1>JobSearchAgent</h1><p class="muted">Simple is the best · Local-only dashboard · No automatic applications</p>
<nav><a href="/">Overview</a><a href="/jobs">Job Shortlist</a><a href="/applications">Application Workspace</a><a href="/audit">Data Integrity</a><a href="/reports">Reports</a></nav>""" + body + "</body></html>"

def summary():
    jobs = read_jobs()
    apps = load_applications()
    return jobs, apps

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Avoid logging query strings, job URLs, or user-supplied form values.
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt.split("%")[0][:80]))

    def send_html(self, body, status=200):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, path):
        self.send_response(303); self.send_header("Location", path); self.end_headers()

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/audit":
            a = audit_csv()
            body = "<h2>Read-only CSV integrity audit</h2><p>Original jobs.csv is never modified.</p><pre>" + esc(json.dumps(a, indent=2)) + "</pre>"
            return self.send_html(page("Data Integrity", body))
        if route == "/jobs":
            jobs = read_jobs()
            rows = "".join("<tr><td>"+esc(j["score"])+"</td><td>"+esc(j["decision"])+"</td><td><a href='"+esc(j["url"])+"' target='_blank' rel='noopener'>"+esc(j["title"] or "Unknown title")+"</a><br><small>"+esc(j["source"])+"</small></td><td>"+esc(j["company"] or "Unknown")+"</td><td>"+esc(j["location"] or "Unknown")+"</td><td>"+esc(j["posted_date"] or "Unknown")+"</td><td>"+esc(j["application_status"])+"</td></tr>" for j in jobs)
            body = "<h2>Job Shortlist</h2><p>Keyword score is a triage aid, not proof of fit. Unknown fields stay Unknown.</p><div class='scroll'><table><tr><th>Score</th><th>Decision</th><th>Role</th><th>Company</th><th>Location</th><th>Posting date</th><th>Status</th></tr>"+rows+"</table></div><p><a href='/add'>Add a verified job manually</a></p>"
            return self.send_html(page("Job Shortlist", body))
        if route == "/applications":
            apps = load_applications()
            rows = "".join("<tr><td>"+esc(a.get("title"))+"</td><td>"+esc(a.get("company"))+"</td><td>"+esc(a.get("status"))+"</td><td>"+esc(a.get("applied_date") or "—")+"</td><td>"+esc(a.get("follow_up_date") or "—")+"</td><td>"+esc(a.get("notes"))+"</td></tr>" for a in apps)
            body = "<h2>Application Workspace</h2><p>Records are local; nothing is submitted automatically.</p><div class='scroll'><table><tr><th>Role</th><th>Company</th><th>Status</th><th>Applied</th><th>Follow-up</th><th>Notes</th></tr>"+rows+"</table></div><h3>Update / add tracking record</h3><form method='post' action='/track'><label>Job URL <input name='url' required></label><label>Title <input name='title' required></label><label>Company <input name='company'></label><label>Status <select name='status'>"+ "".join("<option>"+esc(s)+"</option>" for s in STATUSES)+"</select></label><label>Applied date <input name='applied_date' placeholder='YYYY-MM-DD'></label><label>Follow-up date <input name='follow_up_date' placeholder='YYYY-MM-DD'></label><label>Notes <textarea name='notes'></textarea></label><button>Save tracking record</button></form>"
            return self.send_html(page("Application Workspace", body))
        if route == "/add":
            body = "<h2>Add a verified job</h2><p>Use job details you have verified from the listing. Posting date is separate from discovery date.</p><form method='post' action='/add'><label>Source <select name='source'><option>Manual</option><option>LinkedIn email</option><option>Indeed email</option><option>Glassdoor export</option><option>Other</option></select></label><label>Title <input name='title' required></label><label>Company <input name='company'></label><label>Location <input name='location'></label><label>Posting date <input name='posted_date' placeholder='YYYY-MM-DD'></label><label>Job URL <input name='url' type='url' required></label><label>Description / requirements <textarea name='description' rows='6'></textarea></label><button>Score and save job</button></form>"
            return self.send_html(page("Add Job", body))
        if route == "/reports":
            jobs, apps = summary()
            decisions = Counter(j.get("decision", "") for j in jobs)
            statuses = Counter(a.get("status", "") for a in apps)
            body = "<h2>Performance & Reports</h2><div class='grid'>"+ "".join("<div class='card'><div>"+esc(k)+"</div><div class='num'>"+str(v)+"</div></div>" for k,v in [("Normalized jobs",len(jobs)),("Applications tracked",len(apps)),("APPLY",decisions["APPLY"]),("REVIEW",decisions["REVIEW"]),("Interviews",statuses["Interview"]),("Offers",statuses["Offer"])])+"</div><p>Response rates and time-to-apply are shown only when sufficient dates are recorded; no synthetic metrics are calculated.</p>"
            return self.send_html(page("Reports", body))
        jobs, apps = summary()
        decisions = Counter(j.get("decision", "") for j in jobs)
        due = sum(1 for a in apps if a.get("follow_up_date") and a["follow_up_date"] <= date.today().isoformat() and a.get("status") not in ("Rejected","Withdrawn","Offer"))
        audit = audit_csv()
        body = "<h2>Overview</h2><div class='grid'>"+ "".join("<div class='card'><div>"+esc(k)+"</div><div class='num'>"+str(v)+"</div></div>" for k,v in [("Normalized jobs",len(jobs)),("APPLY",decisions["APPLY"]),("REVIEW",decisions["REVIEW"]),("Tracked applications",len(apps)),("Follow-ups due",due),("Malformed CSV rows",audit["malformed_rows"])])+"</div><section><h3>Safe operating notes</h3><ul><li>Original jobs.csv remains unchanged.</li><li>Malformed rows are excluded from the normalized view, not guessed or overwritten.</li><li>Job-source email collection remains in the existing agent. Glassdoor jobs can be added from a user-authorized export or verified manually; no login bypass or scraping is performed.</li><li>Application preparation is review-first; no auto-submit.</li></ul></section><p><a href='/jobs'>Open shortlist</a> · <a href='/add'>Add job</a> · <a href='/audit'>Inspect CSV audit</a></p>"
        return self.send_html(page("Overview", body))

    def do_POST(self):
        route = urlparse(self.path).path
        length = min(int(self.headers.get("Content-Length", "0")), 1000000)
        form = {k: v[0].strip() for k,v in parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True).items()}
        if route == "/add":
            url = form.get("url", "")
            if not url.startswith(("https://", "http://")):
                return self.send_html(page("Invalid URL","<p>URL must begin with http:// or https://.</p>"),400)
            rows = read_jobs()
            if any(r.get("url","").rstrip("/").lower()==url.rstrip("/").lower() for r in rows):
                return self.redirect("/jobs")
            result = score_job(form.get("title",""), form.get("company",""), form.get("location",""), form.get("description",""))
            rows.append({"discovered_at":now_text(),"posted_date":form.get("posted_date",""),"source":form.get("source","Manual"),"title":form.get("title",""),"company":form.get("company",""),"location":form.get("location",""),"score":str(result["score"]),"decision":result["decision"],"matched_skills":result["matched_skills"],"negative_matches":result["negative_matches"],"url":url,"evidence_notes":result["evidence_notes"]+"; "+result["confidence"],"application_status":"Not applied","follow_up_date":"","notes":""})
            write_jobs(rows); return self.redirect("/jobs")
        if route == "/track":
            url = form.get("url","")
            if not url.startswith(("http://","https://")):
                return self.send_html(page("Invalid URL","<p>URL must begin with http:// or https://.</p>"),400)
            apps = load_applications()
            record = next((a for a in apps if a.get("url")==url), None)
            if record is None:
                record = {"url":url,"created_at":now_text()}; apps.append(record)
            for key in ("title","company","status","applied_date","follow_up_date","notes"):
                record[key] = form.get(key,"")
            record["updated_at"] = now_text()
            save_applications(apps); return self.redirect("/applications")
        self.send_html(page("Not found","<p>Unknown action.</p>"),404)

def main():
    parser = argparse.ArgumentParser(description="JobSearchAgent consolidated local companion")
    parser.add_argument("--audit", action="store_true", help="Read-only audit of legacy jobs.csv")
    parser.add_argument("--normalize", action="store_true", help="Create separate normalized CSV; never edits jobs.csv")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback only by default")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    ensure_dirs()
    if args.audit:
        print(json.dumps(audit_csv(), indent=2)); return
    if args.normalize:
        print(json.dumps(safe_normalize(), indent=2)); return
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("Security: dashboard may only bind to loopback.")
    if not NORMALIZED_JOBS.exists():
        safe_normalize()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print("JobSearchAgent dashboard:", url)
    print("Original jobs.csv is read-only to this companion; stop with Ctrl+C.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
