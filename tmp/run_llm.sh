#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "Usage: $0 <hostname> <query>"
  exit 1
fi

HOST="$1"
shift
QUERY="$*"

# The API requires its key on every route but /node_id. Export EXO_API_KEY with
# the line the node logs at startup; leave it unset only for a node started with
# EXO_API_AUTH_DISABLED=true.
AUTH=()
if [ -n "${EXO_API_KEY:-}" ]; then
  AUTH=(-H "Authorization: Bearer $EXO_API_KEY")
fi

curl -sN -X POST "http://$HOST:52415/v1/chat/completions" \
  ${AUTH[@]+"${AUTH[@]}"} \
  -H "Content-Type: application/json" \
  -d "{
        \"model\": \"mlx-community/Kimi-K2-Thinking\",
        \"stream\": true,
        \"messages\": [{ \"role\": \"user\",   \"content\": \"$QUERY\"}]
      }" |
  grep --line-buffered '^data:' |
  grep --line-buffered -v 'data: \[DONE\]' |
  cut -d' ' -f2- |
  jq -r --unbuffered '.choices[].delta.content // empty' |
  awk '{ORS=""; print; fflush()} END {print "\n"}'
