# JobSearchAgent consolidated upgrade (review branch)

This is an additive upgrade proposal. The original main branch is not changed.
Branch: `jobsearchagent/consolidated-upgrade-v1`

## Files
- `JobSearchAgent/jobsearchagent_consolidated.py`: local-only dashboard, read-only CSV audit, separate normalized CSV, conservative scoring, application tracking, verified manual job entry.
- `JobSearchAgent/test_jobsearchagent_consolidated.py`: basic regression tests.
- `job_agent.py`: narrowly guarded CSV append and deduplication behavior. It refuses to append if the legacy header is wrong and skips malformed-width rows for deduplication; it does not repair existing data.

## Run on macOS
```bash
cd "$HOME/JobSearchAgent"
# After copying the three companion files into this folder:
python3 -m py_compile job_agent.py jobsearchagent_consolidated.py application_draft_builder.py
python3 -m unittest test_jobsearchagent_consolidated
python3 jobsearchagent_consolidated.py --audit
python3 jobsearchagent_consolidated.py --normalize
python3 jobsearchagent_consolidated.py
```

Copy the companion script and test file from this repository's `JobSearchAgent/` folder into your local `~/JobSearchAgent/` folder before running. Keep the original `jobs.csv` and the verified backup. The normalized view is `data/jobs_normalized.csv`; malformed rows are excluded from that view, not guessed or discarded from the source.

Dashboard binds only to 127.0.0.1 by default (port 8765). Stop with Ctrl+C. This companion does not connect to mail or submit applications.

## Honest implementation boundaries
- Current mail ingestion remains Yahoo Mail alerts for LinkedIn/Indeed. Glassdoor integration is not claimed; use an authorized export or manually verified listing.
- Match score is a keyword-based triage estimate and requires manual review. It is not a claim that a skill is proven by a listing.
- Resume facts are not invented. Tailored resume/cover-letter generation needs the current resume's verified text; the diagnostic archive intentionally did not include the resume.
- No applications are auto-submitted.
