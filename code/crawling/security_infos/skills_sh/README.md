# Skills.sh Security Crawlers

This directory contains two ways to collect Skills.sh security metadata.

## Scripts

- `scrape_skills_security.py` - Scrapes individual Skills.sh pages, records
  scanner pass/fail rows, and extracts Socket finding cards from audit pages.
- `get_from_api.py` - Reads the paginated `/api/audits/{page}` endpoint and
  stores scanner JSON payloads for skills that already exist in the local
  `skills` table.

The API-based crawler is the preferred option when the endpoint contains all
fields needed for an analysis. The HTML scraper remains useful for fields that
are only rendered on public pages.
