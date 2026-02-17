#!/usr/bin/env bash
# insights_diagnostic.sh — Understand what /insights sees on this machine
#
# Compatible: Linux (GNU coreutils), macOS (BSD coreutils)
#
# Usage: ./insights_diagnostic.sh

set -euo pipefail

PROJECTS_DIR="${HOME}/.claude/projects"
FACETS_DIR="${HOME}/.claude/usage-data/facets"
OS_TYPE="$(uname -s)"

if [[ ! -d "${PROJECTS_DIR}" ]]; then
    echo "ERROR: ${PROJECTS_DIR} does not exist."
    echo "Claude Code has not been used on this machine."
    exit 1
fi

# ──────────────────────────────────────────────────────────────
# PORTABLE HELPERS — GNU vs BSD differences
# ──────────────────────────────────────────────────────────────

count_files() {
    # $1=dir, $2=pattern
    find "$1" -maxdepth 1 -name "$2" 2>/dev/null | wc -l | tr -d ' '
}

count_files_recursive() {
    find "$1" -name "$2" 2>/dev/null | wc -l | tr -d ' '
}

count_small_files() {
    # Files under 1KB, non-agent, .jsonl
    # BSD find uses -size differently but 1024c works on both
    find "$1" -maxdepth 1 -name '*.jsonl' ! -name 'agent-*' \
        -size -1024c 2>/dev/null | wc -l | tr -d ' '
}

get_file_dates() {
    # Returns oldest and newest mtime for *.jsonl in a directory
    # Works on both GNU and BSD by using ls + sort
    local dir="$1"
    local files
    files=$(find "${dir}" -maxdepth 1 -name '*.jsonl' 2>/dev/null)

    if [[ -z "${files}" ]]; then
        echo "n/a n/a"
        return
    fi

    if [[ "${OS_TYPE}" == "Darwin" ]]; then
        # BSD stat: -f '%m' for epoch, -f '%Sm' for human-readable
        oldest=$(echo "${files}" | xargs stat -f '%m %N' 2>/dev/null \
            | sort -n | head -1 | cut -d' ' -f1)
        newest=$(echo "${files}" | xargs stat -f '%m %N' 2>/dev/null \
            | sort -n | tail -1 | cut -d' ' -f1)
        # Convert epoch to date
        oldest=$(date -r "${oldest}" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "n/a")
        newest=$(date -r "${newest}" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "n/a")
    else
        # GNU stat: -c '%Y' for epoch
        oldest=$(echo "${files}" | xargs stat -c '%Y %n' 2>/dev/null \
            | sort -n | head -1 | cut -d' ' -f1)
        newest=$(echo "${files}" | xargs stat -c '%Y %n' 2>/dev/null \
            | sort -n | tail -1 | cut -d' ' -f1)
        oldest=$(date -d "@${oldest}" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "n/a")
        newest=$(date -d "@${newest}" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "n/a")
    fi

    echo "${oldest} ${newest}"
}

get_file_mtime() {
    local file="$1"
    if [[ "${OS_TYPE}" == "Darwin" ]]; then
        stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "${file}" 2>/dev/null || echo "n/a"
    else
        stat -c '%y' "${file}" 2>/dev/null | cut -d'.' -f1 || echo "n/a"
    fi
}

get_dir_size() {
    du -sh "$1" 2>/dev/null | cut -f1 | tr -d ' '
}

# ──────────────────────────────────────────────────────────────
# REPORT
# ──────────────────────────────────────────────────────────────

echo "=== Claude Code /insights Diagnostic ==="
echo "Machine:  $(hostname)"
echo "User:     $(whoami)"
echo "Home:     ${HOME}"
echo "OS:       ${OS_TYPE} $(uname -r)"
echo "Arch:     $(uname -m)"
echo "Date:     $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo ""

echo "=== Project Directories ==="

total_all=0
total_agent=0
total_regular=0

for project_dir in "${PROJECTS_DIR}"/*/; do
    [[ -d "${project_dir}" ]] || continue

    hash=$(basename "${project_dir}")

    all=$(count_files "${project_dir}" '*.jsonl')
    agent=$(count_files "${project_dir}" 'agent-*.jsonl')
    regular=$((all - agent))
    small=$(count_small_files "${project_dir}")
    likely=$((regular - small))
    if [[ ${likely} -lt 0 ]]; then likely=0; fi

    total_all=$((total_all + all))
    total_agent=$((total_agent + agent))
    total_regular=$((total_regular + regular))

    dates=$(get_file_dates "${project_dir}")
    date_oldest=$(echo "${dates}" | awk '{print $1" "$2}')
    date_newest=$(echo "${dates}" | awk '{print $3" "$4}')

    printf "\n  Hash: %.16s...\n" "${hash}"
    echo "  Total files:      ${all}"
    echo "  Agent sub-sess:   ${agent} (excluded by /insights)"
    echo "  Regular sessions: ${regular}"
    echo "  Tiny (<1KB):      ${small} (likely excluded)"
    echo "  Likely analyzed:  ~${likely}"
    echo "  Date range:       ${date_oldest} → ${date_newest}"
done

echo ""
echo "=== Totals ==="
echo "  All session files:  ${total_all}"
echo "  Agent sub-sessions: ${total_agent}"
echo "  Regular sessions:   ${total_regular}"
echo ""

echo "=== Facet Cache ==="
if [[ -d "${FACETS_DIR}" ]]; then
    cached=$(count_files_recursive "${FACETS_DIR}" '*.json')
    cache_size=$(get_dir_size "${FACETS_DIR}")
    echo "  Cached facets: ${cached}"
    echo "  Cache size:    ${cache_size}"

    uncached=$((total_regular - cached))
    if [[ ${uncached} -lt 0 ]]; then uncached=0; fi
    echo "  Uncached:      ~${uncached} (will be analyzed on next /insights run)"

    if [[ ${uncached} -gt 50 ]]; then
        runs=$(( (uncached + 49) / 50 ))
        echo "  Runs needed:   ~${runs} (50 sessions/run cap)"
    fi
else
    echo "  No facet cache (first /insights run will start from scratch)"
fi

echo ""
echo "=== Existing Report ==="
report="${HOME}/.claude/usage-data/report.html"
if [[ -f "${report}" ]]; then
    report_date=$(get_file_mtime "${report}")
    report_size=$(du -h "${report}" 2>/dev/null | cut -f1 | tr -d ' ')
    echo "  Last generated: ${report_date}"
    echo "  Size:           ${report_size}"

    subtitle=$(grep -o 'subtitle">[^<]*' "${report}" 2>/dev/null \
        | sed 's/subtitle">//' || echo "n/a")
    echo "  Subtitle:       ${subtitle}"
else
    echo "  No report found."
fi

echo ""
echo "=== Path Hash Reference ==="
echo "  /insights hashes the absolute project path to create directory names."
echo "  Same path on different machines = same hash = sessions merge cleanly."
echo "  Different paths = different hashes = separate project areas in report."
echo ""
echo "  To check what path a hash corresponds to, look at session content:"
echo "    head -1 ${PROJECTS_DIR}/<hash>/<any-session>.jsonl | python3 -m json.tool | grep cwd"
