import csv
import tempfile
import unittest
from pathlib import Path
import jobsearchagent_consolidated as app

class ConsolidatedTests(unittest.TestCase):
    def test_scoring_is_bounded_and_exclusions_win(self):
        r = app.score_job("Data Analyst", "Example Co", "Toronto", "Python and SQL; unpaid volunteer")
        self.assertGreaterEqual(r["score"], 0)
        self.assertLessEqual(r["score"], 100)
        self.assertEqual(r["decision"], "SKIP")
        self.assertTrue(r["negative_matches"])

    def test_missing_fields_are_not_invented(self):
        r = app.score_job("", "", "", "")
        self.assertIn("Title: Unknown", r["evidence_notes"])
        self.assertIn("Company: Unknown", r["evidence_notes"])
        self.assertEqual(r["confidence"], "Low")

    def test_audit_does_not_rewrite_source_and_normalizer_quarantines_bad_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app.DATA_DIR = root
            app.LOG_DIR = root
            app.LEGACY_JOBS = root / "jobs.csv"
            app.NORMALIZED_JOBS = root / "jobs_normalized.csv"
            app.AUDIT_FILE = root / "audit.json"
            fields = app.LEGACY_SCHEMA
            with app.LEGACY_JOBS.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f); w.writerow(fields)
                w.writerow(["2026-01-01","Indeed","Data Analyst","Acme","Toronto","80","APPLY","Python","", "https://example.com/job/1"])
                w.writerow(["bad","row","with","extra","fields","https://example.com"])
            before = app.LEGACY_JOBS.read_bytes()
            audit = app.safe_normalize()
            after = app.LEGACY_JOBS.read_bytes()
            self.assertEqual(before, after)
            self.assertEqual(audit["malformed_rows"], 1)
            with app.NORMALIZED_JOBS.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["posted_date"], "")

if __name__ == "__main__":
    unittest.main()
