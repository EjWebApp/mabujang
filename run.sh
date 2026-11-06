#!/usr/bin/env bash
# 마부장님 서버 실행: ./run.sh  (기본 포트 8080)
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
command -v claude >/dev/null || { echo "Claude Code CLI 설치: npm i -g @anthropic-ai/claude-code"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
exec uvicorn mabujang.app:app --host 0.0.0.0 --port "${PORT:-8080}"
