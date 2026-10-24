#!/usr/bin/env bash
# Прогон тестов всех сервисов с покрытием: ./scripts/test_all.sh
set -e
cd "$(dirname "$0")/.."
for s in llm-gateway ingest pipeline admin; do
  echo "=== $s ==="
  (cd services/$s && ../../.venv/bin/python -m pytest -q --cov=app --cov-report=term-missing:skip-covered 2>&1 | grep -v -i -E "warn|testclient|^$|Docs:")
done
