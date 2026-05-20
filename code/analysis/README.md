# Directory Analysis

This directory contains a rewritten analysis flow for already-extracted content.

## Behavior

- Input is an already-extracted directory path (`--inputPath`).
- The directory is analyzed in place (it is not deleted afterward).
- File metadata is stored in SQLite (`apps`, `files`, `secrets` tables).
- Secret detection uses TruffleHog over the input directory.
- URL and IP address findings are stored in `regex_findings`.
- Markdown shell-command snippets are stored in `markdown_commands`.
- File-name references between files are recorded in the `files.reference_files`
  column.
- For non-text files, a temporary `strings` sidecar (`<file>.ownstrings`) is
  created so binary contents are also included in secret scanning.
- Temporary `.ownstrings` files created during analysis are removed afterward.

## Usage

```bash
python3 analysis.py \
  --inputPath /path/to/extracted/archive \
  --database-file /path/to/analysis.sqlite
```

## Requirements

- `trufflehog` in `PATH`
- `strings` in `PATH`
- Python packages:
  - `python-magic`
