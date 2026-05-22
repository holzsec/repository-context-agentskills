#!/usr/bin/env python3
import argparse
import json
import sqlite3
from pathlib import Path


def existing_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def find_first_existing(name_candidates, existing):
    for name in name_candidates:
        if name in existing:
            return name
    return None


def table_columns(conn, table_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [row[1] for row in rows]


def find_column(columns, candidates):
    lowered = {c.lower(): c for c in columns}
    for candidate in candidates:
        hit = lowered.get(candidate.lower())
        if hit:
            return hit
    return None


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Extract repository/skill rows from skills_additional and write a new SQLite "
            "database with a single flattened table including marketplace names."
        )
    )
    parser.add_argument("input_db", help="Path to source SQLite DB (e.g., full_store_crawl.db)")
    parser.add_argument("output_db", help="Path to output SQLite DB")
    parser.add_argument(
        "--clawhub-json",
        default="../dataset/clawhub_hashes.json",
        help="Path to clawhub hash->skill_name JSON mapping",
    )
    parser.add_argument(
        "--strict-downloaded-inputs",
        action="store_true",
        help=(
            "Only include skill_input_metadata rows that were matched to a downloaded "
            "skill and have a real skill_hash. This excludes unmatched marketplace "
            "input rows instead of synthesizing nohash:* identifiers."
        ),
    )
    return parser.parse_args()


def normalize_additional_marketplace_name(name):
    if name == "skills_sh":
        return "skill.sh_additional"
    return name


def normalize_input_marketplace_name(name):
    if name == "skills_sh":
        return "skill.sh"
    return name


def normalize_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def synthesize_hash(repository, skill_path, skill_name):
    repo = (normalize_text(repository) or "unknown-repo").lower()
    path = (normalize_text(skill_path) or normalize_text(skill_name) or "unknown-skill").lower()
    return f"nohash:{repo}:{path}"


def main():
    args = parse_args()
    input_path = Path(args.input_db)
    output_path = Path(args.output_db)
    clawhub_json_path = Path(args.clawhub_json)

    if not input_path.exists():
        raise SystemExit(f"Input DB not found: {input_path}")

    src = sqlite3.connect(f"file:{input_path}?mode=ro&immutable=1", uri=True)
    src.row_factory = sqlite3.Row

    tables = existing_tables(src)

    skills_table = "skills_additional"
    if skills_table not in tables:
        raise SystemExit("Required table not found: skills_additional")
    input_metadata_table = "skill_input_metadata"
    if input_metadata_table not in tables:
        raise SystemExit("Required table not found: skill_input_metadata")

    links_table = find_first_existing(
        [
            "skills_additional_marketplace_links",
            "skill_additionall_marketplace_link",
            "skill_additional_marketplace_link",
        ],
        tables,
    )
    if not links_table:
        raise SystemExit(
            "Could not find marketplace link table. Tried: "
            "skills_additional_marketplace_links, skill_additionall_marketplace_link, skill_additional_marketplace_link"
        )

    marketplaces_table = "marketplaces"
    if marketplaces_table not in tables:
        raise SystemExit("Required table not found: marketplaces")

    skills_cols = table_columns(src, skills_table)
    links_cols = table_columns(src, links_table)
    marketplaces_cols = table_columns(src, marketplaces_table)

    repo_col = find_column(skills_cols, ["repository"])
    hash_col = find_column(skills_cols, ["skill_hash"])
    name_col = find_column(skills_cols, ["skill_name"])
    path_col = find_column(skills_cols, ["skill_path"])

    link_repo_col = find_column(links_cols, ["repository"])
    link_path_col = find_column(links_cols, ["skill_path"])
    link_marketplace_id_col = find_column(links_cols, ["marketplace_id"])

    marketplace_id_col = find_column(marketplaces_cols, ["id"])
    marketplace_name_col = find_column(marketplaces_cols, ["name"])

    required = {
        "skills_additional.repository": repo_col,
        "skills_additional.skill_hash": hash_col,
        "skills_additional.skill_name": name_col,
        "skills_additional.skill_path": path_col,
        f"{links_table}.repository": link_repo_col,
        f"{links_table}.skill_path": link_path_col,
        f"{links_table}.marketplace_id": link_marketplace_id_col,
        "marketplaces.id": marketplace_id_col,
        "marketplaces.name": marketplace_name_col,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise SystemExit("Could not resolve required columns: " + ", ".join(missing))

    query = f"""
        SELECT
            sa.{repo_col} AS repository,
            sa.{hash_col} AS skill_hash,
            sa.{name_col} AS skill_name,
            sa.{path_col} AS skill_path,
            m.{marketplace_name_col} AS marketplace
        FROM {skills_table} sa
        JOIN {links_table} l
          ON l.{link_repo_col} = sa.{repo_col}
         AND l.{link_path_col} = sa.{path_col}
        JOIN {marketplaces_table} m
          ON m.{marketplace_id_col} = l.{link_marketplace_id_col}
    """

    additional_rows = src.execute(query).fetchall()

    input_cols = table_columns(src, input_metadata_table)
    input_repo_col = find_column(input_cols, ["repository"])
    input_hash_col = find_column(input_cols, ["skill_hash"])
    input_path_col = find_column(input_cols, ["skill_path"])
    input_marketplace_col = find_column(input_cols, ["marketplace"])
    input_name_col = find_column(input_cols, ["input_skill_name", "name", "slug"])
    input_downloaded_col = find_column(input_cols, ["skill_downloaded"])
    input_required = {
        f"{input_metadata_table}.repository": input_repo_col,
        f"{input_metadata_table}.skill_hash": input_hash_col,
        f"{input_metadata_table}.skill_path": input_path_col,
        f"{input_metadata_table}.marketplace": input_marketplace_col,
        f"{input_metadata_table}.name-like": input_name_col,
    }
    input_missing = [k for k, v in input_required.items() if not v]
    if input_missing:
        raise SystemExit("Could not resolve required columns: " + ", ".join(input_missing))
    if args.strict_downloaded_inputs and not input_downloaded_col:
        raise SystemExit(
            f"--strict-downloaded-inputs requires column: {input_metadata_table}.skill_downloaded"
        )

    strict_input_filter = ""
    if args.strict_downloaded_inputs:
        strict_input_filter = f"""
          AND COALESCE(sim.{input_downloaded_col}, 0) = 1
          AND sim.{input_hash_col} IS NOT NULL
          AND TRIM(sim.{input_hash_col}) <> ''
          AND sim.{input_hash_col} NOT LIKE 'nohash:%'
        """
    input_query = f"""
        SELECT
            sim.{input_repo_col} AS repository,
            sim.{input_hash_col} AS skill_hash,
            sim.{input_name_col} AS skill_name,
            sim.{input_path_col} AS skill_path,
            sim.{input_marketplace_col} AS marketplace
        FROM {input_metadata_table} sim
        WHERE COALESCE(sim.{input_marketplace_col}, '') <> ''
        {strict_input_filter}
    """
    input_rows = src.execute(input_query).fetchall()
    src.close()

    combined_rows = []
    nohash_rows_added = 0
    for row in additional_rows:
        skill_hash = normalize_text(row["skill_hash"])
        if not skill_hash:
            continue
        combined_rows.append(
            (
                normalize_text(row["repository"]),
                skill_hash,
                normalize_text(row["skill_name"]),
                normalize_text(row["skill_path"]),
                normalize_additional_marketplace_name(normalize_text(row["marketplace"])),
            )
        )

    for row in input_rows:
        repository = normalize_text(row["repository"])
        skill_name = normalize_text(row["skill_name"])
        skill_path = normalize_text(row["skill_path"])
        marketplace = normalize_input_marketplace_name(normalize_text(row["marketplace"]))
        skill_hash = normalize_text(row["skill_hash"])
        if not skill_hash:
            if args.strict_downloaded_inputs:
                continue
            skill_hash = synthesize_hash(repository, skill_path, skill_name)
            nohash_rows_added += 1
        combined_rows.append((repository, skill_hash, skill_name, skill_path, marketplace))

    if not clawhub_json_path.exists():
        raise SystemExit(f"ClawHub JSON not found: {clawhub_json_path}")
    with clawhub_json_path.open("r", encoding="utf-8") as f:
        clawhub_hashes = json.load(f)
    if not isinstance(clawhub_hashes, dict):
        raise SystemExit("ClawHub JSON must be an object of {hash: skill_name}.")
    combined_rows.extend(
        [
            (
                None,
                skill_hash,
                skill_name,
                None,
                "clawhub",
            )
            for skill_hash, skill_name in clawhub_hashes.items()
            if skill_hash
        ]
    )

    hashes_with_skill_sh = {row[1] for row in combined_rows if row[1] and row[4] == "skill.sh"}
    filtered_rows = [
        row
        for row in combined_rows
        if not (row[1] in hashes_with_skill_sh and row[4] == "skill.sh_additional")
    ]
    deduped_rows = []
    seen_hash_marketplace = set()
    for row in filtered_rows:
        key = (row[1], row[4])
        if key in seen_hash_marketplace:
            continue
        seen_hash_marketplace.add(key)
        deduped_rows.append(row)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    dst = sqlite3.connect(str(output_path))
    dst.execute(
        """
        CREATE TABLE skills_additional_flat (
            repository TEXT,
            skill_hash TEXT,
            skill_name TEXT,
            skill_path TEXT,
            marketplace TEXT
        )
        """
    )
    dst.executemany(
        """
        INSERT INTO skills_additional_flat (
            repository,
            skill_hash,
            skill_name,
            skill_path,
            marketplace
        ) VALUES (?, ?, ?, ?, ?)
        """,
        deduped_rows,
    )
    dst.commit()
    dst.close()

    dropped_overlap = len(combined_rows) - len(filtered_rows)
    dropped_dupes = len(filtered_rows) - len(deduped_rows)
    print(
        f"Wrote {len(deduped_rows)} rows to {output_path} (table: skills_additional_flat). "
        f"Dropped {dropped_overlap} skill.sh_additional rows due to matching skill.sh hash. "
        f"Dropped {dropped_dupes} duplicate (skill_hash, marketplace) rows. "
        f"Added {nohash_rows_added} skill_input_metadata rows with synthetic nohash:* hashes. "
        f"Strict downloaded inputs: {args.strict_downloaded_inputs}."
    )


if __name__ == "__main__":
    main()
