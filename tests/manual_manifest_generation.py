"""Manual manifest generation tool — NOT an automated test (no pytest test functions live here).

Generates `catalogues_to_fetch/{CATALOGUE}.xlsx` from the SPARQL endpoint of `ENV` and
nothing else: no ingestion, no scoring, no Reference Result fetch. Any existing manifest for
the catalogue is overwritten.

`generate_manifest`'s default folder (`catalogues_to_fetch/`) is relative to the current
working directory, not to this file's location — so this script `chdir`s to the repo root
first, matching every other `manual_*.py` script's "run from anywhere" convention.

`ENV = "PPE"` needs a real `.env` (`PPE_USERNAME`/`PPE_PASSWORD`); `"PROD"` needs only
network access.

Usage: run `uv run python tests/manual_manifest_generation.py` from anywhere. Change `ENV`
and `CATALOGUE` below to point at a different environment or catalogue.
"""

import os
import sys
from pathlib import Path

from mqa_advisor.environment import build_session
from mqa_advisor.ingest import load_manifest
from mqa_advisor.manifest import ManifestGenerationError, generate_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]

# "PPE" logs in with PPE_USERNAME/PPE_PASSWORD; "PROD" uses a public, unauthenticated session.
ENV = "PROD"

# 🚨 USER: change this to the id of the catalogue whose manifest you want to generate — the id
# after `88u/catalogue/` in its URI, e.g. "europol" or "data-gov-lt".
CATALOGUE = "data-gov-lt"


if __name__ == "__main__":
    os.chdir(REPO_ROOT)

    try:
        path = generate_manifest(CATALOGUE, env=ENV, session=build_session(ENV))
    except ManifestGenerationError as e:
        print(e, file=sys.stderr)
        raise SystemExit(1) from e

    rows = load_manifest(CATALOGUE, path.parent)
    print(f"Done. Wrote {len(rows)} resource(s) to {path}")
