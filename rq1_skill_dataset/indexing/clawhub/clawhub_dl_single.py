#!/usr/bin/env python3
import argparse
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

BASES = ["https://clawhub.ai", "https://www.clawhub.ai"]
UA = "clawhub-skill-downloader/1.2"
TIMEOUT = 30


def safe_name(s: str) -> str:
    s = re.sub(r"[^\w.\-]+", "_", s.strip())
    return s[:180] if len(s) > 180 else s


def mkdirp(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def get_json(session: requests.Session, base: str, path: str, params: Optional[dict] = None) -> Dict[str, Any]:
    url = f"{base}{path}"
    r = session.get(url, params=params, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r.json()


def stream_to_file(session: requests.Session, url: str, dest: str) -> bool:
    r = session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
    if r.status_code != 200:
        return False
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(256 * 1024):
            if chunk:
                f.write(chunk)
    os.replace(tmp, dest)
    return True


def list_skills(
    session: requests.Session,
    limit: int = 50,
    query: Optional[str] = None,
    max_total: Optional[int] = None,
) -> List[Tuple[str, Optional[str]]]:
    """
    Returns [(slug, latest_version), ...] using:
      GET /api/v1/skills?limit=...&cursor=...&q=...
    """
    out: List[Tuple[str, Optional[str]]] = []
    cursor: Optional[str] = None

    while True:
        params = {"limit": limit}
        if query:
            params["q"] = query
        if cursor:
            params["cursor"] = cursor

        data = None
        last_err = None
        for base in BASES:
            try:
                data = get_json(session, base, "/api/v1/skills", params=params)
                break
            except Exception as e:
                last_err = e

        if data is None:
            raise RuntimeError(f"Could not fetch /api/v1/skills from any base. Last error: {last_err}")

        items = data.get("items") or []
        for it in items:
            slug = it.get("slug")
            if not slug:
                continue
            latest = None
            tags = it.get("tags") or {}
            if isinstance(tags, dict):
                latest = tags.get("latest")
            if not latest:
                lv = it.get("latestVersion") or {}
                if isinstance(lv, dict):
                    latest = lv.get("version")

            out.append((str(slug), str(latest) if latest else None))
            if max_total is not None and len(out) >= max_total:
                return out

        cursor = data.get("nextCursor")
        if not cursor:
            break

    return out


def download_skill(session: requests.Session, outdir: str, slug: str, version: Optional[str]) -> bool:
    """
    Try to download a bundle first; if that doesn't work, fetch SKILL.md.
    """
    # Candidate bundle endpoints (registry implementations vary)
    bundle_urls = []
    for base in BASES:
        # common patterns
        if version:
            bundle_urls.append(f"{base}/api/v1/skills/{slug}/download?{urlencode({'version': version})}")
        bundle_urls.append(f"{base}/api/v1/skills/{slug}/download?{urlencode({'tag': 'latest'})}")
        bundle_urls.append(f"{base}/api/v1/skills/{slug}/download")
        bundle_urls.append(f"{base}/api/v1/download?{urlencode({'slug': slug})}")

    zip_path = os.path.join(outdir, safe_name(f"{slug}{('-' + version) if version else ''}.zip"))
    for url in bundle_urls:
        if stream_to_file(session, url, zip_path):
            return True

    # Fallback: SKILL.md
    for base in BASES:
        params = {"path": "SKILL.md"}
        if version:
            params["version"] = version
        md_url = f"{base}/api/v1/skills/{slug}/file?{urlencode(params)}"
        md_path = os.path.join(outdir, safe_name(f"{slug}{('-' + version) if version else ''}__SKILL.md"))
        if stream_to_file(session, md_url, md_path):
            return True

    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/scans/08_DATA_clawhub", help="Output directory")
    ap.add_argument("--limit", type=int, default=50, help="Page size for listing")
    ap.add_argument("--query", default=None, help="Optional search query")
    ap.add_argument("--max", type=int, default=None, help="Max number of skills to download")
    ap.add_argument("--sleep", type=float, default=0.25, help="Delay between downloads (seconds)")
    args = ap.parse_args()

    mkdirp(args.out)

    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://clawhub.ai/skills",
    })

    skills = list_skills(session, limit=args.limit, query=args.query, max_total=args.max)
    print(f"Found {len(skills)} skills")

    ok = 0
    for i, (slug, ver) in enumerate(skills, 1):
        print(f"[{i}/{len(skills)}] {slug} (latest={ver})")
        try:
            if download_skill(session, args.out, slug, ver):
                ok += 1
            else:
                print("  !! download failed (no bundle + no SKILL.md)")
        except Exception as e:
            print(f"  !! error: {e}")
        if args.sleep:
            time.sleep(args.sleep)

    print(f"Done. Downloaded {ok}/{len(skills)}")
    return 0 if ok == len(skills) else 2


if __name__ == "__main__":
    raise SystemExit(main())
