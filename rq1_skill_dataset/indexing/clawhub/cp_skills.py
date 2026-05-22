#!/usr/bin/env python3

import argparse
import re
import shutil
import zipfile
from pathlib import Path

ZIP_RE = re.compile(r"^(?P<base>.+)-(?P<ver>\d+(?:\.\d+)*)\.zip$")


def parse_version(ver: str):
    return tuple(int(x) for x in ver.split("."))


def get_latest_versions(input_dir: Path):
    latest = {}

    for zip_path in input_dir.glob("*.zip"):
        match = ZIP_RE.match(zip_path.name)
        if not match:
            continue

        base = match.group("base")
        version = parse_version(match.group("ver"))

        if base not in latest or version > latest[base][0]:
            latest[base] = (version, zip_path)

    return {base: data[1] for base, data in latest.items()}


def extract_clean(zip_path: Path, target_dir: Path):
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(target_dir)

    # Detect single top-level directory and flatten it
    entries = [p for p in target_dir.iterdir() if p.name != "__MACOSX"]

    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for item in inner.iterdir():
            shutil.move(str(item), target_dir)
        inner.rmdir()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_dir", help="Directory containing zip files")
    parser.add_argument(
        "--root",
        default="clawhub",
        help="Root directory containing repos/ and skills/",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    root = Path(args.root).resolve()

    repos_dir = root / "repos"
    skills_dir = root / "skills"

    repos_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    latest_zips = get_latest_versions(input_dir)

    for base, zip_path in latest_zips.items():
        print(f"Processing {zip_path.name}")

        # Copy zip to repos
        target_zip = repos_dir / zip_path.name
        shutil.copy2(zip_path, target_zip)

        # Extract to skills/<base>
        skill_target = skills_dir / base
        extract_clean(target_zip, skill_target)

    print("Done.")


if __name__ == "__main__":
    main()
