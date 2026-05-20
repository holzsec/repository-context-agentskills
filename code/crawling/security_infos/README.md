# Security Metadata Crawlers

This directory contains crawlers for security scanner and audit metadata.

## Subdirectories

- `openclaw/` - Crawls OpenClaw/ClawHub security results through the Convex API.
- `skills_sh/` - Loads Skills.sh security audit data from HTML pages or the
  audits API.

The resulting SQLite tables are used by evaluation scripts that summarize
vulnerable repositories, verified secrets, and marketplace-level security
findings.
