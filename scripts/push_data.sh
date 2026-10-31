#!/bin/sh
# Push a data.json file to a running Cockpit.
# Usage:
#   INGEST_TOKEN=... BASE_URL=https://trevor-cockpit.onrender.com ./scripts/push_data.sh data.json
set -eu
if [ "$#" -ne 1 ]; then
  echo "usage: INGEST_TOKEN=... BASE_URL=https://host ./scripts/push_data.sh data.json" >&2
  exit 2
fi
: "${INGEST_TOKEN:?set INGEST_TOKEN}"
: "${BASE_URL:?set BASE_URL}"
curl --fail-with-body -sS -X POST "${BASE_URL%/}/api/ingest" \
  -H "Authorization: Bearer ${INGEST_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary @"$1"
echo
