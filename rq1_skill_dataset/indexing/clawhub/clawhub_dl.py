#!/usr/bin/env python3
"""
ClawHub modular pipeline (rate-limit aware) — lister + multiple downloader workers.

What it does
- Lister (high frequency): polls /api/v1/skills starting from the top (cursor=None),
  scans the first N pages each run, and appends NEW (slug, latestVersion) items to pipeline.jsonl.
- Downloader (many workers): claims items from pipeline.jsonl under a file lock and downloads them
  into download_dir (zip if possible, else SKILL.md fallback).

Rate limiting
- Reads x-ratelimit-remaining / x-ratelimit-reset.
- If remaining is low, sleeps until reset.
- If 429, sleeps until reset (or backs off).

Files (inside state_dir)
  pipeline.jsonl    # append-only queue produced by lister
  claimed.jsonl     # claim log (prevents double-download across workers)
  downloaded.jsonl  # success log
  failed.jsonl      # failure log
  lister_state.json # stores "seen latest per slug" (dedupe state)
  .lock             # advisory lock file (fcntl)

Run
  python3 clawhub_pipeline.py lister --loop --interval 30 --pages 5 --limit 50 --state-dir state
  python3 clawhub_pipeline.py downloader --loop --worker-id w1 --state-dir state --download-dir downloads
  python3 clawhub_pipeline.py downloader --loop --worker-id w2 --state-dir state --download-dir downloads
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode

import requests

BASES = ["https://clawhub.ai", "https://www.clawhub.ai"]
UA = "clawhub-pipeline/2.0"
TIMEOUT = 30


# ---------------- utils ----------------

def mkdirp(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def safe_name(s: str) -> str:
    s = re.sub(r"[^\w.\-]+", "_", s.strip())
    return s[:180] if len(s) > 180 else s


def now_ms() -> int:
    return int(time.time() * 1000)


def atomic_write_json(path: str, obj: Any) -> None:
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_jsonl(path: str, record: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _parse_int(v: Optional[str]) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


# ---------------- file lock (Ubuntu) ----------------

class FileLock:
    """Advisory lock using fcntl (works on Ubuntu)."""
    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self._fh = None

    def __enter__(self):
        import fcntl
        mkdirp(os.path.dirname(self.lock_path))
        self._fh = open(self.lock_path, "a", encoding="utf-8")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        import fcntl
        try:
            if self._fh:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
        finally:
            self._fh = None


# ---------------- rate-limit aware requester ----------------

class RateLimitAwareRequester:
    """
    Honors:
      x-ratelimit-remaining: int
      x-ratelimit-reset: unix epoch seconds
    """
    def __init__(self, session: requests.Session, min_remaining: int = 1, buffer_s: float = 0.5):
        self.s = session
        self.min_remaining = min_remaining
        self.buffer_s = buffer_s

    def _maybe_sleep_from_headers(self, r: requests.Response) -> None:
        remaining = _parse_int(r.headers.get("x-ratelimit-remaining"))
        reset = _parse_int(r.headers.get("x-ratelimit-reset"))  # unix seconds

        if remaining is None or reset is None:
            return

        if remaining <= self.min_remaining:
            sleep_s = max(0.0, reset - time.time() + self.buffer_s)
            if sleep_s > 0:
                time.sleep(sleep_s)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        # small bounded retry loop
        for _ in range(10):
            r = self.s.request(method, url, **kwargs)

            if r.status_code == 429:
                reset = _parse_int(r.headers.get("x-ratelimit-reset"))
                sleep_s = max(1.0, (reset - time.time() + self.buffer_s)) if reset else 5.0
                time.sleep(sleep_s)
                continue

            self._maybe_sleep_from_headers(r)
            return r

        # last attempt (let it error naturally)
        r = self.s.request(method, url, **kwargs)
        self._maybe_sleep_from_headers(r)
        return r


# ---------------- API client ----------------

class ClawHubClient:
    def __init__(self, session: requests.Session):
        self.s = session
        self.rl = RateLimitAwareRequester(session)

    def _get_json_any_base(self, path: str, params: Optional[dict] = None) -> Dict[str, Any]:
        last_err = None
        for base in BASES:
            url = f"{base}{path}"
            try:
                r = self.rl.request("GET", url, params=params, timeout=TIMEOUT, allow_redirects=True)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err = e
        raise RuntimeError(f"Failed to fetch {path} from any base. Last error: {last_err}")

    def list_skills_page(self, limit: int, cursor: Optional[str], query: Optional[str]) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if query:
            params["q"] = query
        return self._get_json_any_base("/api/v1/skills", params=params)

    def download_bundle_or_skillmd(self, slug: str, version: Optional[str], zip_path: str, md_path: str) -> bool:
        """
        Try bundle endpoints first; fallback to fetching SKILL.md.
        """
        # 1) bundle candidates (endpoint may vary; this set is intentionally broad)
        bundle_urls: List[str] = []
        for base in BASES:
            if version:
                bundle_urls.append(f"{base}/api/v1/skills/{slug}/download?{urlencode({'version': version})}")
            bundle_urls.append(f"{base}/api/v1/skills/{slug}/download?{urlencode({'tag': 'latest'})}")
            bundle_urls.append(f"{base}/api/v1/skills/{slug}/download")
            bundle_urls.append(f"{base}/api/v1/download?{urlencode({'slug': slug})}")

        for url in bundle_urls:
            if stream_to_file(self.rl, url, zip_path):
                return True

        # 2) fallback: SKILL.md
        for base in BASES:
            params = {"path": "SKILL.md"}
            if version:
                params["version"] = version
            url = f"{base}/api/v1/skills/{slug}/file?{urlencode(params)}"
            if stream_to_file(self.rl, url, md_path):
                return True

        return False


def stream_to_file(rl: RateLimitAwareRequester, url: str, dest: str) -> bool:
    r = rl.request("GET", url, timeout=TIMEOUT, allow_redirects=True, stream=True)
    if r.status_code != 200:
        return False
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(256 * 1024):
            if chunk:
                f.write(chunk)
    os.replace(tmp, dest)
    return True


# ---------------- pipeline record ----------------

@dataclass(frozen=True)
class PipelineItem:
    slug: str
    latest: Optional[str]
    displayName: Optional[str]
    summary: Optional[str]
    createdAt: Optional[int]
    updatedAt: Optional[int]
    stats: Dict[str, Any]

    @staticmethod
    def from_api_item(it: Dict[str, Any]) -> "PipelineItem":
        tags = it.get("tags") or {}
        latest = tags.get("latest") if isinstance(tags, dict) else None
        if not latest:
            lv = it.get("latestVersion") or {}
            if isinstance(lv, dict):
                latest = lv.get("version")

        stats = it.get("stats") or {}
        if not isinstance(stats, dict):
            stats = {}

        return PipelineItem(
            slug=str(it.get("slug")),
            latest=str(latest) if latest else None,
            displayName=it.get("displayName"),
            summary=it.get("summary"),
            createdAt=it.get("createdAt"),
            updatedAt=it.get("updatedAt"),
            stats=stats,
        )

    def key(self) -> str:
        return f"{self.slug}@{self.latest or 'unknown'}"

    def to_record(self) -> Dict[str, Any]:
        return {
            "type": "skill",
            "slug": self.slug,
            "latest": self.latest,
            "displayName": self.displayName,
            "summary": self.summary,
            "createdAt": self.createdAt,
            "updatedAt": self.updatedAt,
            "stats": self.stats,
            "ts": now_ms(),
        }


# ---------------- lister ----------------

class SkillLister:
    """
    Safer approach: always scan from the TOP of the feed each run (cursor=None),
    for a fixed number of pages. Deduplicate with state["seen"][slug] = latest.
    """
    def __init__(self, client: ClawHubClient, state_dir: str):
        self.client = client
        self.state_dir = state_dir
        mkdirp(state_dir)

        self.state_path = os.path.join(state_dir, "lister_state.json")
        self.pipeline_path = os.path.join(state_dir, "pipeline.jsonl")
        self.lock_path = os.path.join(state_dir, ".lock")

        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    st = json.load(f)
                if isinstance(st, dict):
                    st.setdefault("seen", {})
                    return st
            except Exception:
                pass
        return {"seen": {}}  # seen: {slug: latest}

    def _save_state(self) -> None:
        atomic_write_json(self.state_path, self.state)

    def run_once(self, limit: int, pages: int, query: Optional[str]) -> int:
        enqueued = 0

        with FileLock(self.lock_path):
            seen_map = self.state.get("seen") or {}
            if not isinstance(seen_map, dict):
                seen_map = {}

            cursor: Optional[str] = None  # ALWAYS start at the top each run
            for _ in range(max(1, pages)):
                data = self.client.list_skills_page(limit=limit, cursor=cursor, query=query)
                items = data.get("items") or []
                next_cursor = data.get("nextCursor")

                for raw in items:
                    if not isinstance(raw, dict):
                        continue
                    slug = raw.get("slug")
                    if not slug:
                        continue

                    item = PipelineItem.from_api_item(raw)
                    prev_latest = seen_map.get(item.slug)

                    if prev_latest == item.latest:
                        continue

                    # enqueue new/latest version
                    seen_map[item.slug] = item.latest
                    append_jsonl(self.pipeline_path, item.to_record())
                    enqueued += 1

                if not next_cursor:
                    break
                cursor = next_cursor

            self.state["seen"] = seen_map
            self._save_state()

        return enqueued


# ---------------- downloader (multi-worker) ----------------

class SkillDownloaderWorker:
    def __init__(self, client: ClawHubClient, state_dir: str, download_dir: str, worker_id: str):
        self.client = client
        self.state_dir = state_dir
        self.download_dir = download_dir
        self.worker_id = worker_id

        mkdirp(state_dir)
        mkdirp(download_dir)

        self.pipeline_path = os.path.join(state_dir, "pipeline.jsonl")
        self.claimed_path = os.path.join(state_dir, "claimed.jsonl")
        self.downloaded_path = os.path.join(state_dir, "downloaded.jsonl")
        self.failed_path = os.path.join(state_dir, "failed.jsonl")
        self.lock_path = os.path.join(state_dir, ".lock")

        self.claimed: Set[str] = set()
        self.done: Set[str] = set()
        self._load_logs()

    def _load_logs(self) -> None:
        def load_keys(path: str, out: Set[str]) -> None:
            if not os.path.exists(path):
                return
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            k = obj.get("key")
                            if k:
                                out.add(str(k))
                        except Exception:
                            continue
            except Exception:
                return

        load_keys(self.claimed_path, self.claimed)
        load_keys(self.downloaded_path, self.done)

    def _download_paths(self, slug: str, version: Optional[str]) -> Tuple[str, str]:
        suffix = f"-{version}" if version else ""
        zip_path = os.path.join(self.download_dir, safe_name(f"{slug}{suffix}.zip"))
        md_path = os.path.join(self.download_dir, safe_name(f"{slug}{suffix}__SKILL.md"))
        return zip_path, md_path

    def _already_have_files(self, slug: str, version: Optional[str]) -> bool:
        zip_path, md_path = self._download_paths(slug, version)
        return os.path.exists(zip_path) or os.path.exists(md_path)

    def _read_pipeline_lines(self) -> List[str]:
        if not os.path.exists(self.pipeline_path):
            return []
        with open(self.pipeline_path, "r", encoding="utf-8") as f:
            return f.readlines()

    def claim_next_item(self) -> Optional[Dict[str, Any]]:
        """
        Under lock, find the first unclaimed+undone item in pipeline and claim it.
        """
        with FileLock(self.lock_path):
            for line in self._read_pipeline_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue

                if item.get("type") != "skill":
                    continue

                slug = item.get("slug")
                ver = item.get("latest")
                if not slug:
                    continue
                ver = str(ver) if ver is not None else None

                key = f"{slug}@{ver or 'unknown'}"

                if key in self.done or key in self.claimed:
                    continue

                # if files already exist, mark done
                if self._already_have_files(slug, ver):
                    rec = {"ts": now_ms(), "key": key, "slug": slug, "latest": ver, "worker": self.worker_id, "note": "files_exist"}
                    append_jsonl(self.downloaded_path, rec)
                    self.done.add(key)
                    continue

                # claim
                rec = {"ts": now_ms(), "key": key, "slug": slug, "latest": ver, "worker": self.worker_id}
                append_jsonl(self.claimed_path, rec)
                self.claimed.add(key)
                return item

        return None

    def process_one(self) -> bool:
        item = self.claim_next_item()
        if not item:
            return False

        slug = str(item["slug"])
        ver = item.get("latest")
        ver = str(ver) if ver is not None else None
        key = f"{slug}@{ver or 'unknown'}"
        zip_path, md_path = self._download_paths(slug, ver)

        try:
            ok = self.client.download_bundle_or_skillmd(slug, ver, zip_path, md_path)
            if ok:
                rec = {"ts": now_ms(), "key": key, "slug": slug, "latest": ver, "worker": self.worker_id}
                append_jsonl(self.downloaded_path, rec)
                self.done.add(key)
            else:
                rec = {"ts": now_ms(), "key": key, "slug": slug, "latest": ver, "worker": self.worker_id, "error": "download_failed"}
                append_jsonl(self.failed_path, rec)
            return True
        except Exception as e:
            rec = {"ts": now_ms(), "key": key, "slug": slug, "latest": ver, "worker": self.worker_id, "error": str(e)}
            append_jsonl(self.failed_path, rec)
            return True


# ---------------- CLI ----------------

def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "application/json",
            "Referer": "https://clawhub.ai/skills",
        }
    )
    return s


def cmd_lister(args: argparse.Namespace) -> int:
    s = build_session()
    client = ClawHubClient(s)
    lister = SkillLister(client, state_dir=args.state_dir)

    if args.loop:
        while True:
            n = lister.run_once(limit=args.limit, pages=args.pages, query=args.query)
            print(f"[lister] scanned pages={args.pages} limit={args.limit} enqueued={n}")
            time.sleep(args.interval)
    else:
        n = lister.run_once(limit=args.limit, pages=args.pages, query=args.query)
        print(f"[lister] enqueued {n}")
    return 0


def cmd_downloader(args: argparse.Namespace) -> int:
    s = build_session()
    client = ClawHubClient(s)
    worker = SkillDownloaderWorker(
        client,
        state_dir=args.state_dir,
        download_dir=args.download_dir,
        worker_id=args.worker_id,
    )

    if args.loop:
        while True:
            did = worker.process_one()
            if did:
                time.sleep(args.sleep)
            else:
                time.sleep(args.idle_sleep)
    else:
        count = 0
        for _ in range(args.max_items):
            did = worker.process_one()
            if not did:
                break
            count += 1
            time.sleep(args.sleep)
        print(f"[downloader:{args.worker_id}] processed {count}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("lister", help="Poll /api/v1/skills and append NEW latest versions to pipeline.jsonl")
    p_list.add_argument("--state-dir", default="clawhub_state", help="Directory for pipeline + logs + state")
    p_list.add_argument("--limit", type=int, default=50, help="Page size for listing")
    p_list.add_argument("--pages", type=int, default=5, help="How many pages from the top to scan each run")
    p_list.add_argument("--query", default=None, help="Optional search query")
    p_list.add_argument("--loop", action="store_true", help="Run continuously")
    p_list.add_argument("--interval", type=float, default=30.0, help="Seconds between lister runs when looping")
    p_list.set_defaults(func=cmd_lister)

    p_dl = sub.add_parser("downloader", help="Consume pipeline.jsonl and download (multiple workers ok)")
    p_dl.add_argument("--state-dir", default="clawhub_state", help="Directory for pipeline + logs + state")
    p_dl.add_argument("--download-dir", default="clawhub_downloads", help="Where to save downloads")
    p_dl.add_argument("--worker-id", default="w1", help="Unique id for this worker")
    p_dl.add_argument("--loop", action="store_true", help="Run continuously")
    p_dl.add_argument("--sleep", type=float, default=0.1, help="Sleep after doing work (seconds)")
    p_dl.add_argument("--idle-sleep", type=float, default=2.0, help="Sleep when nothing to do (seconds)")
    p_dl.add_argument("--max-items", type=int, default=1000000, help="If not looping, max items to process then exit")
    p_dl.set_defaults(func=cmd_downloader)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
