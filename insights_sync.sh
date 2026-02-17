#!/usr/bin/env bash
set -euo pipefail

REMOTES=(
    "user@host1.example.lan"
    "user@host2.example.lan"
)

PROJECTS_DIR="${HOME}/.claude/projects"
FACETS_DIR="${HOME}/.claude/usage-data/facets"

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN="--dry-run"
    echo "[DRY RUN]"
fi

mkdir -p "${PROJECTS_DIR}" "${FACETS_DIR}"

before=$(find "${PROJECTS_DIR}" -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ')
echo "Local sessions before sync: ${before}"

for remote in "${REMOTES[@]}"; do
    echo ""
    echo "--- ${remote} ---"

    if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "${remote}" true 2>/dev/null; then
        echo "  SKIP: cannot reach"
        continue
    fi

    count=$(ssh -o BatchMode=yes "${remote}" \
        "find ~/.claude/projects/ -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' '" \
        2>/dev/null || echo "0")
    echo "  Remote sessions: ${count}"

    rsync \
        --archive \
        --compress \
        --verbose \
        --ignore-existing \
        --include='*/' \
        --include='*.jsonl' \
        --exclude='*' \
        ${DRY_RUN} \
        "${remote}:~/.claude/projects/" \
        "${PROJECTS_DIR}/" || echo "  ERROR: rsync failed"
done

after=$(find "${PROJECTS_DIR}" -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ')
new=$((after - before))

echo ""
echo "=== Summary ==="
echo "Before: ${before}"
echo "After:  ${after}"
echo "New:    ${new}"

if [[ ${new} -gt 50 ]]; then
    echo ""
    echo "NOTE: /insights processes max 50 new sessions per run."
    echo "Run it $(( (new + 49) / 50 )) times, or clear cache:"
    echo "  rm -rf ${FACETS_DIR}/*"
fi

echo ""
echo "Run: claude /insights"
