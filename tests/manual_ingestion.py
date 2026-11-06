"""Manual ingestion tool — NOT an automated test (no pytest test functions live here).

Fetches a catalogue's manifest from PPE/PROD and writes the JSON-LD for every dataset and
dataservice plus a report to disk.

`PPEIngestor.run_catalogue`'s defaults (`catalogues_to_fetch/`, `fetched_catalogues_data/`)
are relative to the current working directory, not to this file's location — so this
script `chdir`s to the repo root before calling it, matching every other `manual_*.py`
script's "run from anywhere" convention.

Requires a real `.env` (`PPE_USERNAME`/`PPE_PASSWORD`) and network access to PPE.

Usage: run `uv run python tests/manual_ingestion.py` from anywhere. Change `CATALOGUE`
below to point at a different catalogue.
"""

import os
from collections import Counter
from pathlib import Path

import openpyxl

from mqa_advisor.auth import Config, PPEAuth, PublicSession
from mqa_advisor.ingest import PPEIngestor

REPO_ROOT = Path(__file__).resolve().parents[1]

# "PPE" logs in with PPE_USERNAME/PPE_PASSWORD; "PROD" uses a public, unauthenticated
# session. The session must match the environment (see `ensure_session_matches_env`).
ENV = "PROD"

# 🚨 USER: change this to whichever catalogue you want to fetch — must match an existing
# catalogues_to_fetch/{CATALOGUE}.xlsx manifest. Output goes to
# fetched_catalogues_data/PPE/{CATALOGUE}/.
CATALOGUE = "data-gov-lt"


if __name__ == "__main__":
    os.chdir(REPO_ROOT)

    if ENV == "PPE":
        session = PPEAuth(Config.from_env())
        session.login()
    else:
        session = PublicSession()

    ingestor = PPEIngestor(session, env=ENV)
    report_path = ingestor.run_catalogue(CATALOGUE)
    ingestor.shutdown()

    sheet = openpyxl.load_workbook(report_path).active
    results = Counter(row[4] for row in sheet.iter_rows(min_row=2, values_only=True))
    print(
        f"Fetched {results['Success']} of {sum(results.values())} resources "
        f"({results['Failed']} failed, {results['Skipped']} skipped)."
    )
    print(f"Done. Report written to {report_path}")
