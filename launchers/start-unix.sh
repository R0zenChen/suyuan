#!/usr/bin/env bash
set -euo pipefail

opener="${1:-}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [[ ! -x .venv/bin/python ]]; then
  if command -v python3.12 >/dev/null 2>&1; then
    python3.12 -m venv .venv
  else
    echo '需要先安装 Python 3.12。' >&2
    exit 1
  fi
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

if ! command -v node >/dev/null 2>&1; then
  echo '未找到 Node.js；融文数据源将不可用，其他功能仍可运行。' >&2
fi

if [[ -n "$opener" ]] && command -v "$opener" >/dev/null 2>&1; then
  (
    for _ in {1..60}; do
      if curl --silent --fail --max-time 1 http://127.0.0.1:8000/api/config >/dev/null 2>&1; then
        "$opener" http://127.0.0.1:8000 >/dev/null 2>&1 || true
        exit 0
      fi
      sleep 1
    done
  ) &
fi

exec .venv/bin/python -u app.py
