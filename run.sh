#!/usr/bin/env bash
# Start the demo.  ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating venv..."
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -r requirements.txt
fi

if [ ! -f .env ]; then
  echo "No .env found — copying .env.example. Add your GOOGLE_API_KEY to it."
  cp .env.example .env
fi

exec .venv/bin/python server.py
