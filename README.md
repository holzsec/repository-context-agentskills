# agent_skill

This repository contains scripts used to collect, analyze, and evaluate agent
skill marketplace data. The workflow is organized around three stages:

1. Crawl marketplace and security metadata into SQLite databases.
2. Analyze extracted skill directories for files, endpoints, commands, and
   secrets.
3. Generate evaluation tables, CSV summaries, and figures for reporting.

## Directory Layout

- `code/` - Python source code for crawling, analysis, and evaluation.
- `code/analysis/` - Directory scanner that records file metadata and security
  findings in SQLite.
- `code/crawling/` - Crawlers for marketplace listings and security scan
  metadata.
- `code/evaluation/` - Reporting scripts for endpoint, language, repository,
  overlap, marketplace, and secret analyses.
- `tables/` - Generated PDF tables and report artifacts.

## Runtime Notes

Most scripts are standalone command-line tools. They expect local SQLite
databases and intermediate files produced by earlier collection steps. Some
crawlers call live third-party APIs, so run them with conservative worker and
sleep settings when refreshing data.

Common dependencies include:

- Python 3.10+
- `requests`
- `python-magic`
- `tldextract`
- `matplotlib`
- `numpy`
- Optional lookup/scanning tools such as `trufflehog`, `strings`, `maxminddb`,
  and `dnspython`
