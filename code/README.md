# Source Code

This directory contains the Python scripts that implement the collection,
analysis, and evaluation.

## Subdirectories

- `analysis/` - Scans already-extracted skill directories for file metadata,
  endpoints, markdown commands, references, and secrets.
- `crawling/` - Fetches marketplace listings and security metadata from
  external services.
- `evaluation/` - Builds derived CSV, LaTeX, and PDF outputs from the collected
  SQLite databases.

Each script is designed to run as a command-line tool. Use `--help` on an
individual script to inspect its arguments and default paths.
