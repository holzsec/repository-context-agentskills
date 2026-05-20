#!/usr/bin/env python3
"""Categorize extracted endpoint findings across skill marketplaces.

The script reads URL/IP findings from an analysis database, maps analyzed apps
back to marketplaces, optionally matches hosts against Exodus tracker metadata,
and writes marketplace/category summary outputs.
"""

import argparse
import csv
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse

import tldextract

from marketplace_latex_utils import MARKETPLACE_ORDER

TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=None, cache_dir="/tmp/tldextract-cache")
HASH_APP_RE = re.compile(r"^(?P<name>.+)_(?P<hash>[0-9a-f]{64})$")
ANALYZED_DEFAULTS = {
    "ClawHub": 16755,
    "skills_sh": 125928,
    "skillsdirectory": 17611,
    "gharchive": 136095,
}


def extract_host_from_finding(finding, finding_type):
    """Normalize a regex finding into a hostname or IP address key."""

    text = (finding or "").strip()
    if not text:
        return ""
    if finding_type == "ip_address":
        return text.lower()
    ep = text
    if ep.lower().startswith("jdbc:"):
        ep = ep[5:]
    try:
        parsed = urlparse(ep)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().strip(".")
    if not host or "{" in host or "}" in host or "[" in host or "]" in host or "$" in host:
        return ""
    return host


def normalize_host_level(host, level):
    if not host:
        return ""
    if level == "subdomain":
        return host

    extracted = TLD_EXTRACTOR(host)
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}".lower()
    return host


def iter_exodus_trackers(payload):
    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict):
                yield entry
        return

    if not isinstance(payload, dict):
        return

    trackers = payload.get("trackers")
    if isinstance(trackers, list):
        for entry in trackers:
            if isinstance(entry, dict):
                yield entry
        return

    if isinstance(trackers, dict):
        for entry in trackers.values():
            if isinstance(entry, dict):
                yield entry
        return

    for entry in payload.values():
        if isinstance(entry, dict):
            yield entry


def load_exodus_trackers(exodus_url, cache_path, timeout):
    """Load Exodus tracker metadata from the network, falling back to cache."""

    payload = None

    try:
        with request.urlopen(exodus_url, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        payload = json.loads(raw)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(raw, encoding="utf-8")
        return payload, "live_api"
    except (error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        pass

    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            return payload, "cache"
        except (json.JSONDecodeError, OSError):
            return None, "unavailable"

    return None, "unavailable"


def _clean_network_signature_value(value):
    cleaned = value.strip().lower()
    if not cleaned:
        return ""
    cleaned = cleaned.replace(".*", "")
    cleaned = cleaned.replace("\\", "")
    cleaned = cleaned.replace("^", "").replace("$", "")
    cleaned = cleaned.lstrip(".")
    cleaned = cleaned.strip()

    # Keep domain-like tokens only.
    if not cleaned or "." not in cleaned:
        return ""
    if any(c in cleaned for c in "[](){}+?"):
        return ""
    return cleaned


def build_exodus_suffix_to_tracker(payload):
    """Build suffix and tracker-name indexes from Exodus tracker metadata."""

    suffix_to_tracker_ids = defaultdict(set)
    tracker_names = {}

    for tracker in iter_exodus_trackers(payload):
        tracker_id = str(tracker.get("id") or "").strip()
        if not tracker_id:
            continue
        tracker_names[tracker_id] = str(tracker.get("name") or tracker_id).strip()

        network_signature = str(tracker.get("network_signature") or "")
        for raw_piece in network_signature.split("|"):
            domain = _clean_network_signature_value(raw_piece)
            if not domain:
                continue
            suffix_to_tracker_ids[domain].add(tracker_id)

    return suffix_to_tracker_ids, tracker_names


def host_suffixes(host):
    parts = host.split(".")
    for i in range(len(parts)):
        yield ".".join(parts[i:])


def host_matches_domain_list(host, domains):
    if not host or not domains:
        return False
    for domain in domains:
        d = (domain or "").strip().lower().strip(".")
        if not d:
            continue
        if host == d or host.endswith(f".{d}"):
            return True
    return False


def normalize_marketplace_name(name):
    value = (name or "").strip().lower().replace("-", "_")
    if value in {"clawhub"}:
        return "ClawHub"
    if value in {"skills_sh", "skills.sh", "skill.sh", "skill.sh_additional", "skillssh"}:
        return "skills_sh"
    if value in {"skillsdirectory", "skills_directory"}:
        return "skillsdirectory"
    if value in {"gharchive", "github", "gh_archive"}:
        return "gharchive"
    return None


def normalize_skill_name(name):
    return (name or "").strip().lower()


def extract_name_and_hash(app_name):
    app_name = (app_name or "").strip()
    m = HASH_APP_RE.match(app_name)
    if not m:
        return app_name, None
    return m.group("name").strip(), m.group("hash")


def chunked(values, size):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i : i + size]


def table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def load_hash_to_marketplaces(marketplace_db_path, hashes):
    hash_to_marketplaces = defaultdict(set)
    if not marketplace_db_path or not hashes:
        return hash_to_marketplaces

    db_uri = f"file:{Path(marketplace_db_path)}?mode=ro&immutable=1"
    conn = sqlite3.connect(db_uri, uri=True)
    try:
        has_flat_tables = table_exists(conn, "analyzed_skills") or table_exists(conn, "skills_additional_flat")
        for chunk in chunked(sorted(hashes), 500):
            placeholders = ",".join(["?"] * len(chunk))
            if has_flat_tables:
                if table_exists(conn, "analyzed_skills"):
                    rows = conn.execute(
                        f"""
                        SELECT DISTINCT skill_hash, marketplace
                        FROM analyzed_skills
                        WHERE skill_hash IN ({placeholders})
                        """,
                        chunk,
                    ).fetchall()
                    for skill_hash, marketplace in rows:
                        mk = normalize_marketplace_name(marketplace)
                        if mk:
                            hash_to_marketplaces[skill_hash].add(mk)
                if table_exists(conn, "skills_additional_flat"):
                    rows = conn.execute(
                        f"""
                        SELECT DISTINCT skill_hash, marketplace
                        FROM skills_additional_flat
                        WHERE skill_hash IN ({placeholders})
                        """,
                        chunk,
                    ).fetchall()
                    for skill_hash, marketplace in rows:
                        mk = normalize_marketplace_name(marketplace)
                        if mk:
                            hash_to_marketplaces[skill_hash].add(mk)
                continue

            rows = conn.execute(
                f"""
                SELECT DISTINCT s.skill_hash, m.name
                FROM skills s
                JOIN skill_marketplace_links sml
                  ON sml.repository = s.repository AND sml.skill_path = s.skill_path
                JOIN marketplaces m ON m.id = sml.marketplace_id
                WHERE s.skill_hash IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            for skill_hash, marketplace in rows:
                mk = normalize_marketplace_name(marketplace)
                if mk:
                    hash_to_marketplaces[skill_hash].add(mk)

            rows = conn.execute(
                f"""
                SELECT DISTINCT sa.skill_hash, m.name
                FROM skills_additional sa
                JOIN skills_additional_marketplace_links saml
                  ON saml.repository = sa.repository AND saml.skill_path = sa.skill_path
                JOIN marketplaces m ON m.id = saml.marketplace_id
                WHERE sa.skill_hash IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            for skill_hash, marketplace in rows:
                mk = normalize_marketplace_name(marketplace)
                if mk:
                    hash_to_marketplaces[skill_hash].add(mk)
    finally:
        conn.close()
    return hash_to_marketplaces


def load_clawhub_names(marketplace_db_path):
    clawhub_names = set()
    db_uri = f"file:{Path(marketplace_db_path)}?mode=ro&immutable=1"
    conn = sqlite3.connect(db_uri, uri=True)
    try:
        if table_exists(conn, "analyzed_skills"):
            rows = conn.execute(
                """
                SELECT DISTINCT skill_name, skill_path
                FROM analyzed_skills
                WHERE lower(marketplace) = 'clawhub'
                """
            ).fetchall()
            for skill_name, skill_path in rows:
                sn = normalize_skill_name(skill_name)
                if sn:
                    clawhub_names.add(sn)
                sp = str(skill_path or "").strip().rstrip("/")
                if sp:
                    clawhub_names.add(normalize_skill_name(Path(sp).name))
        if table_exists(conn, "skills_additional_flat"):
            rows = conn.execute(
                """
                SELECT DISTINCT skill_name, skill_path
                FROM skills_additional_flat
                WHERE lower(marketplace) = 'clawhub'
                """
            ).fetchall()
            for skill_name, skill_path in rows:
                sn = normalize_skill_name(skill_name)
                if sn:
                    clawhub_names.add(sn)
                sp = str(skill_path or "").strip().rstrip("/")
                if sp:
                    clawhub_names.add(normalize_skill_name(Path(sp).name))
    finally:
        conn.close()
    return clawhub_names


def load_app_marketplaces(db_conn, marketplace_db_path, clawhub_name, unknown_hash_name):
    """Map analyzed app IDs to marketplaces using app names and hash metadata."""

    apps = db_conn.execute("SELECT id, app_name, COALESCE(platform, '') FROM apps").fetchall()
    hashes = {extract_name_and_hash(app_name)[1] for _, app_name, _ in apps}
    hashes.discard(None)
    hash_to_marketplaces = load_hash_to_marketplaces(marketplace_db_path, hashes)
    clawhub_names = load_clawhub_names(marketplace_db_path)

    app_to_marketplaces = {}
    unknown_hashed_apps = []
    for app_id, app_name, _platform in apps:
        skill_name, app_hash = extract_name_and_hash(app_name)
        skill_name_norm = normalize_skill_name(skill_name)
        marketplaces = set()
        if app_hash:
            marketplaces.update(m for m in hash_to_marketplaces.get(app_hash, set()) if m != "ClawHub")
        if skill_name_norm in clawhub_names:
            marketplaces.add(clawhub_name)
        if not marketplaces:
            marketplaces = {unknown_hash_name}
            if app_hash:
                unknown_hashed_apps.append((app_id, app_name, app_hash))
        app_to_marketplaces[app_id] = marketplaces
    return app_to_marketplaces, unknown_hashed_apps


def format_pct(numerator, denominator):
    if not denominator:
        return ""
    return f"{(numerator / denominator) * 100.0:.2f}"


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Detect skills that contact any tracker from the Exodus tracker list and report counts per marketplace."
        )
    )
    parser.add_argument("--db", default="../results/final_paper.db", help="Path to SQLite DB")
    parser.add_argument(
        "--exodus-url",
        default="https://reports.exodus-privacy.eu.org/api/trackers",
        help="Exodus trackers API endpoint",
    )
    parser.add_argument(
        "--exodus-cache",
        default="analysis_outputs/exodus_trackers_cache.json",
        help="Cache file for Exodus API payload",
    )
    parser.add_argument("--exodus-timeout", type=int, default=15, help="Timeout in seconds for Exodus API fetch")
    parser.add_argument("--no-exodus", action="store_true", help="Disable live API/cache load and produce empty matches")
    parser.add_argument(
        "--marketplace-db",
        default="../dataset/full_skill_marketplace.db",
        help="Path to full_skill_marketplace.db for hash->marketplace mapping",
    )
    parser.add_argument("--clawhub-name", default="ClawHub", help="Marketplace label for non-hash app_name rows")
    parser.add_argument("--unknown-hash-name", default="UnknownHashed", help="Marketplace label for unmapped hash app_name rows")
    parser.add_argument(
        "--unknown-hash-print-limit",
        type=int,
        default=20,
        help="How many unknown hashed entries to print in stdout summary (default: 20, -1 for all, 0 for none)",
    )
    parser.add_argument(
        "--out",
        default="analysis_outputs/exodus_tracker_contact_by_marketplace.csv",
        help="Output CSV with per-marketplace skill contact counts",
    )
    parser.add_argument(
        "--out-skills",
        default="analysis_outputs/exodus_tracker_contact_by_skill.csv",
        help="Output CSV with per-skill tracker contact status",
    )
    parser.add_argument(
        "--out-skill-endpoints",
        default="analysis_outputs/exodus_tracker_endpoints_by_skill.json",
        help="Output JSON mapping skill -> matched Exodus tracking endpoints",
    )
    parser.add_argument(
        "--level",
        choices=["domain", "subdomain"],
        default="domain",
        help="URL host aggregation level before matching against tracker domains",
    )
    parser.add_argument(
        "--exclude-tracking-domains",
        default="",
        help="Comma-separated domains to ignore for tracker mapping (example: google.com,facebook.com)",
    )
    parser.add_argument(
        "--without-google-facebook",
        action="store_true",
        help="Ignore Google/Facebook domains for tracker mapping (google.com, facebook.com)",
    )
    parser.add_argument("--total-skills-clawhub", type=int, default=ANALYZED_DEFAULTS["ClawHub"])
    parser.add_argument("--total-skills-skills-sh", type=int, default=ANALYZED_DEFAULTS["skills_sh"])
    parser.add_argument("--total-skills-skillsdirectory", type=int, default=ANALYZED_DEFAULTS["skillsdirectory"])
    parser.add_argument("--total-skills-gharchive", type=int, default=ANALYZED_DEFAULTS["gharchive"])
    args = parser.parse_args()
    excluded_tracking_domains = set()
    if args.exclude_tracking_domains.strip():
        excluded_tracking_domains.update(
            x.strip().lower().strip(".")
            for x in args.exclude_tracking_domains.split(",")
            if x.strip()
        )
    if args.without_google_facebook:
        excluded_tracking_domains.update({"google.com", "facebook.com"})

    exodus_source = "disabled"
    suffix_to_tracker_ids = defaultdict(set)
    tracker_names = {}
    if not args.no_exodus:
        exodus_payload, exodus_source = load_exodus_trackers(
            exodus_url=args.exodus_url,
            cache_path=Path(args.exodus_cache),
            timeout=args.exodus_timeout,
        )
        if exodus_payload is not None:
            suffix_to_tracker_ids, tracker_names = build_exodus_suffix_to_tracker(exodus_payload)

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        """
        SELECT rf.finding, lower(rf.finding_type), f.app_id, a.app_name
        FROM regex_findings rf
        LEFT JOIN files f ON f.id = rf.file_id
        LEFT JOIN apps a ON a.id = f.app_id
        WHERE lower(rf.finding_type) IN ('url', 'ip_address')
        """
    ).fetchall()

    app_to_marketplaces, unknown_hashed_apps = load_app_marketplaces(
        conn,
        args.marketplace_db,
        args.clawhub_name,
        args.unknown_hash_name,
    )

    all_app_rows = conn.execute("SELECT id, app_name FROM apps").fetchall()
    conn.close()

    app_info = {app_id: app_name for app_id, app_name in all_app_rows}

    app_tracker_ids = defaultdict(set)
    app_tracker_hosts = defaultdict(set)
    apps_with_findings = set()
    skipped_no_host = 0
    skipped_no_app = 0

    for finding, finding_type, app_id, _app_name in rows:
        host = extract_host_from_finding(finding, finding_type)
        if finding_type == "url":
            host = normalize_host_level(host, args.level)
        if not host:
            skipped_no_host += 1
            continue
        if app_id is None:
            skipped_no_app += 1
            continue
        if host_matches_domain_list(host, excluded_tracking_domains):
            continue

        apps_with_findings.add(app_id)
        for suffix in host_suffixes(host):
            tracker_ids = suffix_to_tracker_ids.get(suffix)
            if not tracker_ids:
                continue
            app_tracker_ids[app_id].update(tracker_ids)
            app_tracker_hosts[app_id].add(host)

    marketplace_total_apps = defaultdict(set)
    marketplace_contact_apps = defaultdict(set)

    for app_id in app_info:
        for marketplace in app_to_marketplaces.get(app_id, {args.unknown_hash_name}):
            marketplace_total_apps[marketplace].add(app_id)
            if app_tracker_ids.get(app_id):
                marketplace_contact_apps[marketplace].add(app_id)

    preferred_order = [m for m, _ in MARKETPLACE_ORDER]
    observed_marketplaces = set(marketplace_total_apps.keys()) | set(marketplace_contact_apps.keys())
    ordered_marketplaces = preferred_order + sorted(observed_marketplaces - set(preferred_order))
    marketplace_skill_totals = {
        "ClawHub": args.total_skills_clawhub,
        "skills_sh": args.total_skills_skills_sh,
        "skillsdirectory": args.total_skills_skillsdirectory,
        "gharchive": args.total_skills_gharchive,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "marketplace",
                "skills_total",
                "skills_contacting_exodus_tracker",
                "pct_contacting_exodus_tracker",
                "skills_with_no_exodus_tracker_contact",
            ]
        )
        for marketplace in ordered_marketplaces:
            observed_total = len(marketplace_total_apps.get(marketplace, set()))
            total = marketplace_skill_totals.get(marketplace, observed_total)
            contacting = len(marketplace_contact_apps.get(marketplace, set()))
            writer.writerow(
                [
                    marketplace,
                    total,
                    contacting,
                    format_pct(contacting, total),
                    total - contacting,
                ]
            )

    out_skills_path = Path(args.out_skills)
    out_skills_path.parent.mkdir(parents=True, exist_ok=True)
    with out_skills_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "app_id",
                "app_name",
                "marketplaces",
                "contacts_exodus_tracker",
                "tracker_count",
                "trackers",
                "matched_host_count",
            ]
        )
        for app_id in sorted(app_info):
            trackers = sorted(app_tracker_ids.get(app_id, set()), key=lambda tid: tracker_names.get(tid, tid).lower())
            tracker_labels = [tracker_names.get(tid, tid) for tid in trackers]
            marketplaces = sorted(app_to_marketplaces.get(app_id, {args.unknown_hash_name}))
            writer.writerow(
                [
                    app_id,
                    app_info.get(app_id, "") or "",
                    "|".join(marketplaces),
                    "yes" if trackers else "no",
                    len(trackers),
                    "|".join(tracker_labels),
                    len(app_tracker_hosts.get(app_id, set())),
                ]
            )

    out_skill_endpoints_path = Path(args.out_skill_endpoints)
    out_skill_endpoints_path.parent.mkdir(parents=True, exist_ok=True)
    skill_to_tracking_endpoints = {}
    for app_id in sorted(app_info):
        app_name = (app_info.get(app_id, "") or "").strip()
        if app_name:
            key = app_name
        else:
            key = f"app_id:{app_id}"
        endpoints = sorted(app_tracker_hosts.get(app_id, set()))
        if endpoints:
            skill_to_tracking_endpoints[key] = endpoints
    out_skill_endpoints_path.write_text(
        json.dumps(skill_to_tracking_endpoints, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    total_apps = len(app_info)
    contacted_apps = len(app_tracker_ids)

    print(f"Exodus source: {exodus_source}")
    print(f"Exodus tracker domains loaded: {len(suffix_to_tracker_ids)}")
    print(f"Total skills (apps): {total_apps}")
    print(f"Skills with any Exodus tracker contact: {contacted_apps}")
    print(f"Skills without Exodus tracker contact: {total_apps - contacted_apps}")
    print(f"Skills with at least one parsed endpoint finding: {len(apps_with_findings)}")
    print(f"Skipped findings without parseable host: {skipped_no_host}")
    print(f"Skipped findings without app_id: {skipped_no_app}")
    print(f"Unknown hashed apps: {len(unknown_hashed_apps)}")
    if unknown_hashed_apps:
        entries = sorted(unknown_hashed_apps, key=lambda x: (x[2], x[0]))
        if args.unknown_hash_print_limit == 0:
            entries = []
        elif args.unknown_hash_print_limit > 0:
            entries = entries[: args.unknown_hash_print_limit]
        print("Unknown hashed entries (app_id, app_name, skill_hash):")
        for app_id, app_name, app_hash in entries:
            print(f"  {app_id}\t{app_name}\t{app_hash}")
        if args.unknown_hash_print_limit >= 0 and len(unknown_hashed_apps) > len(entries):
            print(f"  ... and {len(unknown_hashed_apps) - len(entries)} more")

    print(f"Wrote marketplace report: {out_path}")
    print(f"Wrote per-skill report: {out_skills_path}")
    print(f"Wrote skill->tracking endpoints JSON: {out_skill_endpoints_path}")


if __name__ == "__main__":
    main()
