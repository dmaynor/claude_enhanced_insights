# Claude Enhanced Insights

An enhanced reimplementation of Claude Code's `/insights` command that removes session caps, raises token limits, and produces detailed HTML reports analyzing your Claude Code usage patterns.

## Why

The built-in `/insights` command has conservative limits: 200 sessions max, 500-token summaries, 50 facets sent to report generation. This toolkit processes **all** sessions with higher fidelity, and supports syncing sessions from multiple machines into a single report.

### Limits Comparison

| Parameter | Built-in `/insights` | Enhanced |
|-----------|---------------------|----------|
| Session cap | 200 | 9,999 |
| Summary tokens | 500 | 2,048 |
| Facet extraction tokens | 4,096 | 8,192 |
| Report section tokens | 8,192 | 16,384 |
| Facets sent to report | 50 | 200 |
| User message truncation | 500 chars | 2,000 chars |
| Assistant message truncation | 300 chars | 1,000 chars |

## Prerequisites

- Python 3.8+
- An active Claude Code OAuth session (`~/.claude/.credentials.json` must exist — just run `claude` once and log in)

```bash
pip install anthropic httpx
```

## Usage

### Generate a Report

```bash
# Full run — analyzes all sessions
python3 enhanced_insights.py

# Dry run — shows session counts and cost estimate, no API calls
python3 enhanced_insights.py --dry-run

# Filter to specific projects
python3 enhanced_insights.py --project "*my-project*"

# Only sessions after a date
python3 enhanced_insights.py --after 2026-01-15

# Use a different model (default: claude-opus-4-6)
python3 enhanced_insights.py --model claude-sonnet-4-20250514
```

Output is saved to your home directory:
- `~/claude-insights-YYYYMMDD-HHMMSS.html` — the visual report
- `~/claude-insights-YYYYMMDD-HHMMSS.json` — raw aggregated data

### Multi-Machine Analysis

If you use Claude Code on multiple machines, sync sessions before generating the report:

```bash
# 1. Edit REMOTES array in insights_sync.sh with your hosts
# 2. Sync (requires SSH key auth)
bash insights_sync.sh

# Preview without transferring
bash insights_sync.sh --dry-run

# 3. Generate report across all machines
python3 enhanced_insights.py
```

Sessions are matched by project path hash — the same project path on different machines merges automatically.

### Diagnose Available Data

```bash
bash insights_diagnostic.sh
```

Shows per-project session counts, facet cache status, date ranges, and estimates how many API calls a full run will require. Read-only, no side effects. Works on both Linux and macOS.

## How It Works

```
Remote Machines (optional)
        |
        | insights_sync.sh (SSH + rsync)
        v
~/.claude/projects/*.jsonl (session transcripts)
        |
        | enhanced_insights.py
        |
        +---> Extract metrics (tools, languages, tokens, git activity)
        +---> Extract facets via Claude API (goals, outcomes, satisfaction, friction)
        |       \--> Cache in ~/.claude/usage-data/facets/
        +---> Aggregate across all sessions
        +---> Generate report sections (8 parallel Claude API calls)
        +---> Render HTML report
        v
~/claude-insights-YYYYMMDD-HHMMSS.html
~/claude-insights-YYYYMMDD-HHMMSS.json
```

### Report Sections

- **At a Glance** — what's working, what's hindering you, quick wins, ambitious workflows
- **What You Work On** — project areas with session counts
- **How You Use Claude Code** — interaction style narrative
- **Impressive Things You Did** — effective workflows identified from sessions
- **Where Things Go Wrong** — friction categories with specific examples
- **Charts** — goals, tools, languages, session types, outcomes, satisfaction, helpfulness, response times, time-of-day activity, tool errors
- **Suggestions** — CLAUDE.md additions, features to try, usage pattern improvements
- **On the Horizon** — ambitious autonomous workflows to try as models improve

### Caching

Facet extraction results are cached per-session in `~/.claude/usage-data/facets/`. Subsequent runs skip already-analyzed sessions. To force reanalysis, delete the cache:

```bash
rm -rf ~/.claude/usage-data/facets/*
```

## Security

- Output files are written with `0600` permissions (owner-only read/write)
- OAuth tokens are read from Claude Code's existing credentials — no separate API key needed
- The first 200 characters of user prompts are included in reports and cache files
- Remote hosts in `insights_sync.sh` should be edited for your environment before use
