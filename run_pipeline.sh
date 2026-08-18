#!/usr/bin/env bash
# Runs the full data-gathering pipeline in order and stops on the first failure.
#
#   ./run_pipeline.sh              collect from the live site
#   ./run_pipeline.sh --from-mirror <dir>   ingest an existing wget mirror instead
#
# Set BM_USER_AGENT to include a contact address before running against the
# live site.
set -euo pipefail
cd "$(dirname "$0")/data_pipeline"

DATA="${BM_DATA_DIR:-data}"

if [[ "${1:-}" == "--from-mirror" ]]; then
  echo "==> 1/6 import local mirror"; python3 import_local.py --dir "$2" --strip-host-dir
else
  echo "==> 1/6 discover"; python3 discover.py
  echo "==> 2/6 crawl";    python3 crawl.py
fi

echo "==> 3/6 extract";  python3 extract.py
echo "==> 4/6 pdfs";     python3 pdf_ingest.py || echo "   (skipped: pip install pypdf)"
echo "==> 5/6 audit";    python3 audit.py
echo "==> 6/6 export";   python3 export.py

echo
echo "==> verification"
python3 verify_corpus.py || true

cat <<EOF

Collected. Read these, in this order:
  $DATA/reports/verification.md   does the corpus meet the trust rules?
  $DATA/reports/site_report.md    what does the site contain, what is missing?
  $DATA/export/CORPUS_CARD.md     provenance summary of what was collected

Deliverables:
  $DATA/corpus/documents.jsonl    full records (sections, tables, provenance)
  $DATA/export/*.csv              human-inspectable tables
  $DATA/export/corpus.sqlite      FTS5 keyword index, ready for the agent

If verification flagged client-rendered pages:
  python3 render.py --all-unrendered && python3 extract.py && python3 verify_corpus.py
EOF
