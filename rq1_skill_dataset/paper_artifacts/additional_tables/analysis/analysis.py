"""
Directory analysis pipeline.

This script analyzes an already-extracted directory, stores file metadata in
SQLite, and runs secret scanning with TruffleHog. For non-text files, it
creates temporary `.ownstrings` sidecar files using `strings` so binary content
is still scanned.
"""

import argparse
import json
import logging
import os
import pathlib
import re
import shlex
import sqlite3
import subprocess

import magic


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename="zip_analysis.log",
    filemode="a",
)
logger = logging.getLogger(__name__)


mime = magic.Magic(mime=True)


TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/x-sh",
    "application/xhtml+xml",
    "application/x-yaml",
}

URL_REGEX = re.compile(r"(?i)\bhttps?://[^\s<>'\"`]+")
IPV4_REGEX = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
)
MARKDOWN_INLINE_CODE_REGEX = re.compile(r"`([^`\n]+)`")
MARKDOWN_FENCED_CODE_REGEX = re.compile(
    r"```(?P<language>[^\n`]*)\n(?P<body>.*?)```",
    re.DOTALL,
)

MARKDOWN_COMMAND_BLOCK_LANGUAGES = {
    "comand",
    "command",
    "commands",
    "bash",
    "sh",
    "shell",
    "zsh",
    "ksh",
    "fish",
    "console",
    "terminal",
    "powershell",
    "pwsh",
    "ps1",
    "cmd",
    "bat",
}

KNOWN_SHELL_BUILTINS = {
    "alias",
    "cd",
    "echo",
    "eval",
    "exec",
    "exit",
    "export",
    "history",
    "pwd",
    "read",
    "set",
    "source",
    "test",
    "trap",
    "true",
    "type",
    "umask",
    "unalias",
    "unset",
}


def select_files_for_references(files):
    """Return original file names to search for as intra-directory references."""

    result = set()
    for file in files:
        file_name = file.get("fileName", "")
        if file_name.endswith(".ownstrings"):
            file_name = file_name[: -len(".ownstrings")]
        if file_name:
            result.add(file_name)
    return list(result)


def get_mime_type(file_path: str) -> str:
    try:
        return mime.from_file(file_path)
    except Exception as exc:
        logger.error("Error getting mime type for %s: %s", file_path, exc, exc_info=True)
        return "unknown/unknown"


def is_text_mime(mime_type: str) -> bool:
    return mime_type.startswith("text/") or mime_type in TEXT_MIME_TYPES


def create_strings_file(file_path: str):
    """Create a temporary `.ownstrings` file for binary scanning when needed."""

    output_path = f"{file_path}.ownstrings"
    if os.path.exists(output_path):
        return output_path, False

    try:
        with open(output_path, "w", encoding="utf-8", errors="ignore") as out:
            subprocess.run(["strings", file_path], stdout=out, check=False)
        return output_path, True
    except Exception as exc:
        logger.error("Error creating strings file for %s: %s", file_path, exc, exc_info=True)
        return output_path, False


def parse_trufflehog_results(results, root_dir: str):
    """Group TruffleHog JSON-line results by normalized source file path."""

    root_dir_norm = os.path.normpath(root_dir)
    results_per_file = {}

    for result in results:
        file_path = (
            result.get("SourceMetadata", {})
            .get("Data", {})
            .get("Filesystem", {})
            .get("file", "")
        )
        file_path = os.path.normpath(file_path)
        if file_path.endswith(".ownstrings"):
            file_path = file_path[: -len(".ownstrings")]

        # TruffleHog may return relative paths.
        if not os.path.isabs(file_path):
            file_path = os.path.normpath(os.path.join(root_dir_norm, file_path))

        tmp = results_per_file.get(file_path, [])
        tmp.append(result)
        results_per_file[file_path] = tmp

    return results_per_file


def add_secrets_to_results(results, secrets_per_file):
    for result in results:
        normalized_path = os.path.normpath(result["filePath"])
        if normalized_path in secrets_per_file:
            result["secrets_trufflehog"] = secrets_per_file[normalized_path]
    return results


def normalize_url(url: str) -> str:
    return url.rstrip(".,;:!?)]}\"'")


def extract_regex_findings(file_path: str):
    """Find URL and IPv4 literals in a text file or extracted-strings sidecar."""

    urls = set()
    ips = set()
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as in_file:
            for line in in_file:
                for url in URL_REGEX.findall(line):
                    urls.add(normalize_url(url))
                for ip in IPV4_REGEX.findall(line):
                    ips.add(ip)
    except Exception as exc:
        logger.debug("Could not scan %s for regex findings: %s", file_path, exc)

    findings = []
    for url in sorted(urls):
        findings.append({"finding": url, "finding_type": "url"})
    for ip in sorted(ips):
        findings.append({"finding": ip, "finding_type": "ip_address"})
    return findings


def collect_regex_findings(results):
    findings_per_file = {}
    for result in results:
        file_path = result["filePath"]
        source_path = file_path
        ownstrings_path = f"{file_path}.ownstrings"
        if os.path.exists(ownstrings_path):
            source_path = ownstrings_path
        findings = extract_regex_findings(source_path)
        if findings:
            findings_per_file[os.path.normpath(file_path)] = findings
    return findings_per_file


def add_regex_findings_to_results(results, findings_per_file):
    for result in results:
        normalized_path = os.path.normpath(result["filePath"])
        if normalized_path in findings_per_file:
            result["regex_findings"] = findings_per_file[normalized_path]
    return results


def normalize_command_candidate(candidate: str) -> str:
    normalized = candidate.strip()
    if not normalized:
        return ""

    normalized = re.sub(r"^\s*[-*+]\s+", "", normalized)
    normalized = re.sub(r"^\s*\d+\.\s+", "", normalized)
    normalized = re.sub(r"^\s*(?:\$|#)\s+", "", normalized)
    return normalized.strip()


def is_likely_shell_command(candidate: str) -> bool:
    candidate = normalize_command_candidate(candidate)
    if not candidate:
        return False

    if len(candidate) > 500:
        return False

    if candidate.startswith(("#", "//", "<!--")):
        return False

    if candidate.lower().startswith(("http://", "https://")):
        return False

    first_segment = re.split(r"&&|\|\||[|;]", candidate, maxsplit=1)[0].strip()
    try:
        tokens = shlex.split(first_segment, posix=True)
    except ValueError:
        tokens = first_segment.split()

    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens.pop(0)

    if not tokens:
        return False

    command_name = tokens[0]
    if command_name in KNOWN_SHELL_BUILTINS:
        return True
    if command_name.startswith(("./", "../", "/", "~")):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", command_name))


def extract_markdown_commands(file_path: str):
    """Extract shell-looking commands from markdown inline and fenced code."""

    commands = set()
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as in_file:
            content = in_file.read()

        for match in MARKDOWN_INLINE_CODE_REGEX.findall(content):
            candidate = normalize_command_candidate(match)
            if is_likely_shell_command(candidate):
                commands.add(candidate)

        for match in MARKDOWN_FENCED_CODE_REGEX.finditer(content):
            language = match.group("language").strip().lower()
            block = match.group("body")
            if language not in MARKDOWN_COMMAND_BLOCK_LANGUAGES:
                continue
            for line in block.splitlines():
                candidate = normalize_command_candidate(line)
                if is_likely_shell_command(candidate):
                    commands.add(candidate)
    except Exception as exc:
        logger.debug("Could not scan %s for markdown commands: %s", file_path, exc)

    return sorted(commands)


def collect_markdown_commands(results):
    commands_per_file = {}
    for result in results:
        suffix = result.get("suffix", "").lower()
        if suffix not in {".md", ".markdown"}:
            continue
        file_path = result["filePath"]
        commands = extract_markdown_commands(file_path)
        if commands:
            commands_per_file[os.path.normpath(file_path)] = commands
    return commands_per_file


def add_markdown_commands_to_results(results, commands_per_file):
    for result in results:
        normalized_path = os.path.normpath(result["filePath"])
        if normalized_path in commands_per_file:
            result["markdown_commands"] = commands_per_file[normalized_path]
    return results


def search_for_secrets_trufflehog(file_path: str):
    secrets = []
    try:
        result = subprocess.run(
            [
                "trufflehog",
                "-j",
                "--no-update",
                "--no-debug",
                "filesystem",
                file_path,
                "--no-verification",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        out = result.stdout.strip()
        if not out:
            return secrets

        for line in out.splitlines():
            if not line.strip():
                continue
            parsed = json.loads(line)
            if "running source" in parsed.get("msg", "") or "finished" in parsed.get("msg", ""):
                continue
            secrets.append(parsed)
    except Exception as exc:
        logger.error("Error running trufflehog on %s: %s", file_path, exc, exc_info=True)
    return secrets


def analyze_references(results):
    references_to_search = select_files_for_references(results)
    if not references_to_search:
        return results

    found_references = {}
    for result in results:
        file_path = result["filePath"]
        source_path = file_path
        # Use extracted strings for binaries so references inside binaries are searchable.
        if not is_text_mime(result["mimeType"]) and os.path.exists(f"{file_path}.ownstrings"):
            source_path = f"{file_path}.ownstrings"

        try:
            with open(source_path, "r", encoding="utf-8", errors="ignore") as in_file:
                content = in_file.read()
        except Exception as exc:
            logger.debug("Skipping reference scan for %s: %s", source_path, exc)
            continue

        for reference_name in references_to_search:
            if reference_name == result["fileName"]:
                continue
            if reference_name in content:
                current = found_references.get(reference_name, set())
                current.add(file_path)
                found_references[reference_name] = current

    for result in results:
        refs = sorted(found_references.get(result["fileName"], set()))
        if refs:
            result["reference_files"] = json.dumps(refs)

    return results


def analyze_directory(output_dir: str):
    """Run all file, endpoint, command, reference, and secret scans for a tree."""

    results = []
    created_strings_files = []

    for root, _, files in os.walk(output_dir):
        for file_name in files:
            file_path = os.path.join(root, file_name)
            mime_type = get_mime_type(file_path)
            suffix = pathlib.Path(file_path).suffix.lower()
            try:
                file_size = os.path.getsize(file_path)
            except Exception:
                file_size = -1

            if not is_text_mime(mime_type):
                strings_path, is_created = create_strings_file(file_path)
                if is_created:
                    created_strings_files.append(strings_path)

            results.append(
                {
                    "fileName": file_name,
                    "filePath": file_path,
                    "fileSize": file_size,
                    "mimeType": mime_type,
                    "suffix": suffix,
                    "secrets_trufflehog": [],
                    "secret_key_value_pairs": [],
                    "secrets_gitleaks": [],
                    "regex_findings": [],
                    "markdown_commands": [],
                }
            )

    regex_findings = collect_regex_findings(results)
    results = add_regex_findings_to_results(results, regex_findings)
    markdown_commands = collect_markdown_commands(results)
    results = add_markdown_commands_to_results(results, markdown_commands)

    secrets = search_for_secrets_trufflehog(output_dir)
    parsed = parse_trufflehog_results(secrets, output_dir)
    results = add_secrets_to_results(results, parsed)
    results = analyze_references(results)
    return results, created_strings_files


def connect_db(file_name: str):
    try:
        return sqlite3.connect(file_name)
    except Exception as exc:
        logger.error("Error connecting to database %s: %s", file_name, exc, exc_info=True)
        raise


def setup_tables(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS apps("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "app_name VARCHAR, "
        "platform VARCHAR)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS files("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "app_id INT, "
        "file_size BIGINT, "
        "file_name VARCHAR, "
        "file_path VARCHAR, "
        "mime_type TEXT, "
        "suffix VARCHAR, "
        "reference_files TEXT, "
        "FOREIGN KEY (app_id) REFERENCES apps(id))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS secrets("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "file_id INT, "
        "secret TEXT, "
        "detection_rule TEXT, "
        "FOREIGN KEY (file_id) REFERENCES files(id))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS regex_findings("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "file_id INT, "
        "finding TEXT, "
        "finding_type TEXT, "
        "FOREIGN KEY (file_id) REFERENCES files(id))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS markdown_commands("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "file_id INT, "
        "command TEXT, "
        "FOREIGN KEY (file_id) REFERENCES files(id))"
    )


def archive_analyzed(app_name: str, platform: str, con) -> int:
    cur = con.execute(
        "SELECT id FROM apps WHERE app_name = ? AND platform = ?",
        [app_name, platform],
    )
    return len(cur.fetchall())


def setup_database(db_file: str, app_name: str, platform: str):
    con = connect_db(db_file)
    setup_tables(con)
    already = archive_analyzed(app_name, platform, con)
    con.close()
    return already > 0


def insert_results(db_file: str, archive_name: str, platform: str, files):
    """Persist one analyzed directory and all child findings into SQLite."""

    con = connect_db(db_file)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO apps (app_name, platform) VALUES (?, ?)",
        [archive_name, platform],
    )
    app_id = cur.lastrowid

    for file_info in files:
        cur.execute(
            "INSERT INTO files(app_id, file_size, file_name, file_path, mime_type, suffix, reference_files) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                app_id,
                file_info["fileSize"],
                file_info["fileName"],
                file_info["filePath"],
                file_info["mimeType"],
                file_info["suffix"],
                file_info.get("reference_files"),
            ],
        )

        file_id = cur.lastrowid
        for secret in file_info["secrets_trufflehog"]:
            cur.execute(
                "INSERT INTO secrets(file_id, secret, detection_rule) VALUES (?, ?, ?)",
                [file_id, json.dumps(secret), "trufflehog"],
            )
        for finding in file_info.get("regex_findings", []):
            cur.execute(
                "INSERT INTO regex_findings(file_id, finding, finding_type) VALUES (?, ?, ?)",
                [file_id, finding["finding"], finding["finding_type"]],
            )
        for command in file_info.get("markdown_commands", []):
            cur.execute(
                "INSERT INTO markdown_commands(file_id, command) VALUES (?, ?)",
                [file_id, command],
            )

    con.commit()
    con.close()


def remove_strings_files(paths):
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as exc:
            logger.warning("Could not remove strings file %s: %s", path, exc)


def parse_flags():
    parser = argparse.ArgumentParser(description="Analyze a directory for metadata and secrets")
    parser.add_argument(
        "--inputPath",
        type=str,
        required=True,
        help="Path to an already-extracted directory",
    )
    parser.add_argument(
        "--database-file",
        type=str,
        default="analysis_sqllite.db",
        help="Path to SQLite database file",
    )
    return parser.parse_args()


def main():
    args = parse_flags()
    input_path = os.path.abspath(args.inputPath)
    db_file = args.database_file

    if not os.path.isdir(input_path):
        logger.error("Input path is not a directory: %s", input_path)
        raise SystemExit(1)

    archive_name = os.path.basename(os.path.normpath(input_path))
    platform = "directory"

    if setup_database(db_file, archive_name, platform):
        logger.info("Directory %s already analyzed. Skipping.", archive_name)
        return

    files, strings_files = analyze_directory(input_path)
    insert_results(db_file, archive_name, platform, files)
    logger.info("Analysis complete for %s", input_path)
    remove_strings_files(strings_files)


if __name__ == "__main__":
    main()
