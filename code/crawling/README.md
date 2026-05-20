# Crawling

This directory contains crawlers that populate local SQLite databases with
marketplace listings, repository metadata, and security scanner results.

## Subdirectories

- `skills/` - Marketplace listing crawlers, currently focused on Skills.sh.
- `security_infos/` - Crawlers for security scan metadata exposed by Skills.sh
  and OpenClaw/ClawHub.

The crawlers call live external services. Prefer explicit `--limit`,
`--max-pages`, `--sleep`, or `--workers` settings when testing changes.
