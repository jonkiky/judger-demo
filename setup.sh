#!/bin/sh
set -eu
cd "$(dirname "$0")"
POC_PYTHON="${POC_PYTHON:-python3}"
"$POC_PYTHON" -c 'import sys,sqlite3; assert sys.version_info >= (3,11), "Python 3.11+ required"; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")'
if [ ! -x .venv/bin/python ]; then
  "$POC_PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
.venv/bin/python scripts/prepare_corpus.py
printf '%s\n' 'Setup complete. Ensure the two local Ollama models are available, then run ./start.sh.'
