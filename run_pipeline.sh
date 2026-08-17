#!/usr/bin/env bash
# Convenience wrapper: runs the full data-gathering pipeline in order.
# Usage: ./run_pipeline.sh
set -euo pipefail
cd "$(dirname "$0")/data_pipeline"

echo "==> 1/5 discover"; python3 discover.py
echo "==> 2/5 crawl";    python3 crawl.py
echo "==> 3/5 extract";  python3 extract.py
echo "==> 4/5 pdfs";     python3 pdf_ingest.py || echo "   (skipped: pip install pypdf)"
echo "==> 5/5 audit";    python3 audit.py

echo
echo "Done. Read the coverage report:"
echo "  ${BM_DATA_DIR:-data}/reports/site_report.md"
echo "If crawl.py warned about client-rendered pages, run:"
echo "  python3 render.py --all-unrendered   # then re-run extract.py and audit.py"
