#!/usr/bin/env python3
"""Summarize verified secret findings by marketplace.

The script reads verified TruffleHog results from the final analysis database,
deduplicates secrets per app, maps app hashes back to marketplaces, and writes
summary/detail CSV files.
"""

import argparse
import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

HASH_APP_RE = re.compile(r"^[^_]+_(?P<hash>[0-9a-f]{64})$")


def extract_hash_from_app_name(app_name):
    m = HASH_APP_RE.match(app_name or "")
    return m.group("hash") if m else None


def fetch_verified_secret_rows(final_db_path):
    uri = f"file:{final_db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA temp_store=MEMORY")
    try:
        return conn.execute(
            """
            SELECT
                s.id AS secret_id,
                s.file_id,
                s.secret,
                s.detection_rule,
                a.id AS app_id,
                a.app_name
            FROM secrets s
            JOIN files f ON f.id = s.file_id
            JOIN apps a ON a.id = f.app_id
            WHERE s.verified = 1
            """
        ).fetchall()
    finally:
        conn.close()


def canonical_secret_value(secret_payload):
    """Extract the best stable secret value from a TruffleHog JSON payload."""

    text = (secret_payload or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text

    if isinstance(parsed, dict):
        for key in ("RawV2", "Raw", "Redacted"):
            value = parsed.get(key)
            if value is not None:
                value_text = str(value).strip()
                if value_text:
                    return value_text
    return text


def detector_name_from_payload(secret_payload):
    text = (secret_payload or "").strip()
    if not text:
        return "unknown"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return "unknown"
    if isinstance(parsed, dict):
        value = parsed.get("DetectorName")
        if value is not None:
            value_text = str(value).strip()
            if value_text:
                return value_text
    return "unknown"


def deduplicate_verified_rows(rows):
    """Keep one row per `(app, canonical secret)` pair."""

    deduped = {}
    for secret_id, file_id, secret_payload, detection_rule, app_id, app_name in rows:
        secret_value = canonical_secret_value(secret_payload)
        detector_name = detector_name_from_payload(secret_payload)
        key = (app_id, secret_value)
        existing = deduped.get(key)
        if existing is None or secret_id < existing[0]:
            deduped[key] = (
                secret_id,
                file_id,
                secret_value,
                detector_name,
                app_id,
                app_name,
            )
    return list(deduped.values())


def chunked(values, size):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i : i + size]


def load_hash_to_marketplaces(full_store_db_path, hashes):
    hash_to_marketplaces = defaultdict(set)
    if not hashes:
        return hash_to_marketplaces

    conn = sqlite3.connect(full_store_db_path)
    conn.execute("PRAGMA temp_store=MEMORY")
    try:
        for chunk in chunked(sorted(hashes), 500):
            placeholders = ",".join(["?"] * len(chunk))

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
                hash_to_marketplaces[skill_hash].add(marketplace)

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
                hash_to_marketplaces[skill_hash].add(marketplace)
    finally:
        conn.close()

    return hash_to_marketplaces


def build_marketplace_summary(rows, hash_to_marketplaces, clawhub_name, unknown_hash_name):
    """Aggregate deduplicated verified secrets into marketplace summary rows."""

    apps_by_marketplace = defaultdict(set)
    files_by_marketplace = defaultdict(set)
    secret_to_apps_by_marketplace = defaultdict(lambda: defaultdict(set))
    secret_to_detectors_by_marketplace = defaultdict(lambda: defaultdict(Counter))

    unknown_hashes = set()
    multi_market_hashes = set()

    for secret_id, file_id, secret_value, detector_name, app_id, app_name in rows:
        app_hash = extract_hash_from_app_name(app_name)
        if app_hash is None:
            marketplaces = {clawhub_name}
        else:
            marketplaces = set(hash_to_marketplaces.get(app_hash, set()))
            if not marketplaces:
                marketplaces = {unknown_hash_name}
                unknown_hashes.add(app_hash)
            if len(marketplaces) > 1:
                multi_market_hashes.add(app_hash)

        for marketplace in marketplaces:
            apps_by_marketplace[marketplace].add(app_id)
            files_by_marketplace[marketplace].add(file_id)
            secret_to_apps_by_marketplace[marketplace][secret_value].add(app_name)
            secret_to_detectors_by_marketplace[marketplace][secret_value][detector_name or "unknown"] += 1

    summary_rows = []
    details_by_marketplace = defaultdict(list)
    detector_name_by_marketplace = defaultdict(Counter)

    for marketplace, secret_to_apps in secret_to_apps_by_marketplace.items():
        for secret_value, app_names in secret_to_apps.items():
            detector_counter = secret_to_detectors_by_marketplace[marketplace][secret_value]
            canonical_detector = sorted(detector_counter.items(), key=lambda item: (-item[1], item[0]))[0][0]
            detector_name_by_marketplace[marketplace][canonical_detector] += 1
            details_by_marketplace[marketplace].append((canonical_detector, secret_value, sorted(app_names)))

        secret_count = len(secret_to_apps)
        top_detector, top_detector_count = "", 0
        if detector_name_by_marketplace[marketplace]:
            top_detector, top_detector_count = detector_name_by_marketplace[marketplace].most_common(1)[0]
        detector_breakdown = ", ".join(
            f"{name} {count}"
            for name, count in detector_name_by_marketplace[marketplace].most_common()
        )

        summary_rows.append(
            {
                "marketplace": marketplace,
                "verified_secret_count": secret_count,
                "affected_app_count": len(apps_by_marketplace[marketplace]),
                "affected_file_count": len(files_by_marketplace[marketplace]),
                "top_detector": top_detector,
                "top_detector_count": top_detector_count,
                "detector_breakdown": detector_breakdown,
            }
        )

    summary_rows.sort(key=lambda row: (-row["verified_secret_count"], row["marketplace"]))
    for marketplace in details_by_marketplace:
        details_by_marketplace[marketplace].sort(key=lambda item: (item[0], item[1], ",".join(item[2])))

    return summary_rows, unknown_hashes, multi_market_hashes, details_by_marketplace


def write_summary_csv(out_path, summary_rows):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "marketplace",
                "verified_secret_count",
                "affected_app_count",
                "affected_file_count",
                "top_detector",
                "top_detector_count",
                "detector_breakdown",
            ]
        )
        for row in summary_rows:
            writer.writerow(
                [
                    row["marketplace"],
                    row["verified_secret_count"],
                    row["affected_app_count"],
                    row["affected_file_count"],
                    row["top_detector"],
                    row["top_detector_count"],
                    row["detector_breakdown"],
                ]
            )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Summarize verified secrets from final_paper.db per marketplace. "
            "Hashed app_name values are resolved by skill hash mapping in full_store_crawl.db; "
            "non-hash app_name values are assigned to ClawHub."
        )
    )
    parser.add_argument(
        "-f",
        "--final-db",
        type=Path,
        required=True,
        help="Path to final_paper.db",
    )
    parser.add_argument(
        "-s",
        "--full-store-db",
        type=Path,
        required=True,
        help="Path to full_store_crawl.db used for hash->marketplace mapping",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("analysis_outputs/verified_secrets_per_marketplace.csv"),
        help="Output CSV path",
    )
    parser.add_argument(
        "--clawhub-name",
        default="ClawHub",
        help="Marketplace label for non-hash app_name entries",
    )
    parser.add_argument(
        "--unknown-hash-name",
        default="UnknownHashed",
        help="Marketplace label for hash app_name entries that cannot be mapped by hash",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-marketplace deduplicated secret values in addition to summary counts.",
    )
    parser.add_argument("--top", type=int, default=20, help="How many marketplaces to print")
    args = parser.parse_args()

    raw_rows = fetch_verified_secret_rows(args.final_db)
    rows = deduplicate_verified_rows(raw_rows)
    hashes = {extract_hash_from_app_name(app_name) for _, _, _, _, _, app_name in rows}
    hashes.discard(None)

    hash_to_marketplaces = load_hash_to_marketplaces(args.full_store_db, hashes)
    summary_rows, unknown_hashes, multi_market_hashes, details_by_marketplace = build_marketplace_summary(
        rows,
        hash_to_marketplaces,
        args.clawhub_name,
        args.unknown_hash_name,
    )
    write_summary_csv(args.out, summary_rows)

    total_verified_raw = len(raw_rows)
    total_verified = len(rows)
    expanded_total = sum(row["verified_secret_count"] for row in summary_rows)

    print(f"Total verified secrets (raw rows): {total_verified_raw}")
    print(f"Total verified secrets (deduplicated by app+secret value): {total_verified}")
    print(f"Total verified secrets after marketplace expansion: {expanded_total}")
    print(f"Hash app names observed: {len(hashes)}")
    print(f"Hashes mapped to at least one marketplace: {len([h for h in hashes if h in hash_to_marketplaces])}")
    print(f"Hashes mapped to multiple marketplaces: {len(multi_market_hashes)}")
    print(f"Unmapped hashes: {len(unknown_hashes)}")

    print("\nMapping rules:")
    print(f"  non-hash app_name -> {args.clawhub_name}")
    print("  hash app_name -> marketplace(s) resolved via skill_hash in full_store_crawl.db")
    if unknown_hashes:
        print(f"  unmapped hash app_name -> {args.unknown_hash_name}")

    print("\nVerified secrets per marketplace:")
    for row in summary_rows[: args.top]:
        print(
            f"  {row['marketplace']}: verified_secrets={row['verified_secret_count']}, "
            f"affected_apps={row['affected_app_count']}, affected_files={row['affected_file_count']}, "
            f"top_detector={row['top_detector']} ({row['top_detector_count']}), "
            f"detectors=[{row['detector_breakdown']}]"
        )

    if args.verbose:
        print("\nDeduplicated secrets per marketplace:")
        for row in summary_rows[: args.top]:
            marketplace = row["marketplace"]
            print(f"  {marketplace}:")
            for detector_name, secret_value, app_names in details_by_marketplace.get(marketplace, []):
                apps_text = ",".join(app_names)
                print(f"    - detector={detector_name} secret={secret_value} apps=[{apps_text}]")

    print(f"\nWrote: {args.out}")


if __name__ == "__main__":
    main()
