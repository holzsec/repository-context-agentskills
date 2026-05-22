#!/usr/bin/env python3
import argparse
import datetime as dt
import gzip
import orjson
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

GHARCHIVE_BASE = "https://data.gharchive.org"

AGENT_KEYWORDS = [
    "agent", "agents", "assistant", "assistants", "autonomous",
    "skill", "skills", "tool", "tools", "tooling", "plugin", "plugins",
    "workflow", "workflows", "automation", "automations",
    "llm", "gpt", "chatgpt", "openai", "function-calling", "functions", "mcp",
    "prompt", "prompts", "rag", "retrieval", "orchestrator", "orchestration",
]
DEFAULT_MARK_SCORE = 2


def utc_today_date() -> dt.date:
    return dt.datetime.utcnow().date()


def daterange(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def run_cmd(cmd: List[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return p.returncode, p.stdout, p.stderr


def ensure_tool_exists(name: str) -> None:
    from shutil import which
    if which(name) is None:
        print(f"ERROR: required tool '{name}' not found in PATH.", file=sys.stderr)
        sys.exit(2)


def safe_rmtree(p: Path) -> None:
    try:
        shutil.rmtree(p)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"WARNING: failed to delete {p}: {e}", file=sys.stderr)


def download_hour(day: dt.date, hour: int, dest_dir: Path, downloader: str, quiet: bool) -> Optional[Path]:
    fn = f"{day.isoformat()}-{hour}.json.gz"
    url = f"{GHARCHIVE_BASE}/{fn}"
    out = dest_dir / fn

    if out.exists() and out.stat().st_size > 0:
        return out

    if downloader == "wget":
        cmd = ["wget"]
        if quiet:
            cmd += ["-q"]
        cmd += ["-O", str(out), url]
    else:
        cmd = ["curl"]
        if quiet:
            cmd += ["-sS"]
        cmd += ["-fL", "-o", str(out), url]

    rc, _, err = run_cmd(cmd)
    if rc != 0:
        if out.exists():
            try:
                out.unlink()
            except Exception:
                pass
        print(f"WARNING: download failed: {url} ({downloader}) {err.strip()}", file=sys.stderr)
        return None

    return out


def _add_text(out: List[str], x: object) -> None:
    if isinstance(x, str):
        s = x.strip()
        if s:
            out.append(s)


def iter_repo_texts_from_gz(path: Path) -> Iterable[Tuple[str, str]]:
    """
    Yield (repo_full_name, text) pairs from GHArchive events.
    Extracts useful text from payload (commit messages, PR title/body, issue title/body, comments, releases).
    Streaming-friendly: does not keep all events in memory.
    """
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            repo = (ev.get("repo") or {}).get("name")
            if not (isinstance(repo, str) and "/" in repo):
                continue

            texts: List[str] = []
            et = ev.get("type")
            _add_text(texts, et)
            _add_text(texts, (ev.get("actor") or {}).get("login"))

            payload = ev.get("payload") or {}

            # PushEvent: commit messages
            if et == "PushEvent":
                commits = payload.get("commits") or []
                if isinstance(commits, list):
                    for c in commits:
                        if isinstance(c, dict):
                            _add_text(texts, c.get("message"))

            # Pull request: title/body
            elif et == "PullRequestEvent":
                pr = payload.get("pull_request") or {}
                if isinstance(pr, dict):
                    _add_text(texts, pr.get("title"))
                    _add_text(texts, pr.get("body"))

            # Issues: title/body
            elif et == "IssuesEvent":
                iss = payload.get("issue") or {}
                if isinstance(iss, dict):
                    _add_text(texts, iss.get("title"))
                    _add_text(texts, iss.get("body"))

            # Issue comment: body
            elif et == "IssueCommentEvent":
                c = payload.get("comment") or {}
                if isinstance(c, dict):
                    _add_text(texts, c.get("body"))

            # PR review: body
            elif et == "PullRequestReviewEvent":
                r = payload.get("review") or {}
                if isinstance(r, dict):
                    _add_text(texts, r.get("body"))

            # PR review comment: body
            elif et == "PullRequestReviewCommentEvent":
                c = payload.get("comment") or {}
                if isinstance(c, dict):
                    _add_text(texts, c.get("body"))

            # Release: name/body
            elif et == "ReleaseEvent":
                rel = payload.get("release") or {}
                if isinstance(rel, dict):
                    _add_text(texts, rel.get("name"))
                    _add_text(texts, rel.get("body"))

            # CreateEvent: sometimes has ref/description
            elif et == "CreateEvent":
                _add_text(texts, payload.get("ref"))
                _add_text(texts, payload.get("ref_type"))
                _add_text(texts, payload.get("description"))

            # If we didn't capture anything, still yield repo with empty text (so discovery works)
            yield repo, ("\n".join(texts) if texts else "")


def agent_skill_score_repo_name(repo_full_name: str) -> int:
    s = repo_full_name.lower()
    tokens = re.split(r"[^a-z0-9]+", s)
    token_set = set(t for t in tokens if t)

    score = 0
    for kw in AGENT_KEYWORDS:
        kw_l = kw.lower()
        if kw_l in token_set or kw_l in s:
            score += 1

    if "skill" in token_set and ("agent" in token_set or "assistant" in token_set):
        score += 2
    return score


def agent_skill_score_text(text: str) -> int:
    if not text:
        return 0
    s = text.lower()
    tokens = re.split(r"[^a-z0-9]+", s)
    token_set = set(t for t in tokens if t)

    score = 0
    for kw in AGENT_KEYWORDS:
        kw_l = kw.lower()
        if kw_l in token_set or kw_l in s:
            score += 1

    if "skill" in token_set and ("agent" in token_set or "assistant" in token_set):
        score += 2
    return score


def choose_hours_for_day(day: dt.date, hours_per_day: int, seed: int) -> List[int]:
    r = random.Random(seed + day.toordinal() * 1000003)
    k = min(max(hours_per_day, 1), 24)
    hours = list(range(24))
    r.shuffle(hours)
    return sorted(hours[:k])


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS repos (
            repo TEXT PRIMARY KEY,
            first_seen_date TEXT,
            agent_score INTEGER
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS days_done (
            day TEXT PRIMARY KEY
        );
    """)
    return conn


def day_already_done(conn: sqlite3.Connection, day: dt.date) -> bool:
    row = conn.execute("SELECT 1 FROM days_done WHERE day = ?;", (day.isoformat(),)).fetchone()
    return row is not None


def mark_day_done(conn: sqlite3.Connection, day: dt.date) -> None:
    conn.execute("INSERT OR IGNORE INTO days_done(day) VALUES (?);", (day.isoformat(),))
    conn.commit()


def upsert_repo_max_score(conn: sqlite3.Connection, repo: str, day: dt.date, score: int) -> bool:
    """
    Insert if new; else update agent_score to max(old,new), keep earliest first_seen_date.
    Returns True if inserted (new repo).
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO repos(repo, first_seen_date, agent_score) VALUES (?, ?, ?);",
        (repo, day.isoformat(), score),
    )
    inserted = (cur.rowcount == 1)
    if not inserted:
        conn.execute(
            "UPDATE repos SET agent_score = CASE WHEN agent_score < ? THEN ? ELSE agent_score END WHERE repo = ?;",
            (score, score, repo),
        )
    return inserted


def gh_search_skillmd(repo: str, timeout_sec: int = 30) -> Tuple[bool, str]:
    cmd = ["gh", "search", "code", "filename:SKILL.md", "--repo", repo, "--limit", "1"]
    try:
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_sec
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip() or f"gh exit {p.returncode}"
        return False, msg
    out = (p.stdout or "").strip()
    return (len(out) > 0), ""


def parse_mark_scores(mark_scores_arg: Optional[str], mark_score_single: int) -> List[int]:
    if mark_scores_arg:
        parts = [p for p in re.split(r"[,\s]+", mark_scores_arg.strip()) if p]
        scores = sorted({int(x) for x in parts})
        if not scores:
            return [int(mark_score_single)]
        return scores
    return [int(mark_score_single)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-01")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--hours-per-day", type=int, default=6, help="random hours/day to download (default: 6)")
    ap.add_argument("--seed", type=int, default=1, help="sampling seed (default: 1)")
    ap.add_argument("--keep-downloads", action="store_true",
                    help="do not delete downloaded GHArchive files")
    ap.add_argument("--workdir", default="gharchive_tmp", help="temp download dir")
    ap.add_argument("--outdir", default="out", help="output dir")
    ap.add_argument("--db", default="repos.sqlite", help="sqlite db filename under outdir")

    ap.add_argument("--downloader", choices=["wget", "curl"], default="wget")
    ap.add_argument("--quiet-download", action="store_true", help="suppress wget/curl output")

    ap.add_argument("--resume", action="store_true", help="skip days already processed (requires sqlite db)")

    # legacy single threshold + new multiple thresholds
    ap.add_argument("--mark-score", type=int, default=DEFAULT_MARK_SCORE,
                    help="single mark threshold (legacy). Prefer --mark-scores.")
    ap.add_argument("--mark-scores", default=None,
                    help="comma-separated thresholds, e.g. 1,2,3 (overrides --mark-score)")

    # new: reprocess already-downloaded workdir without downloading
    ap.add_argument("--reprocess-workdir", action="store_true",
                    help="do not download; scan existing workdir/<YYYY-MM-DD>/*.json.gz")

    ap.add_argument("--skillmd", choices=["none", "all", "marked", "auto"], default="none")
    ap.add_argument("--auto-max", type=int, default=2000)
    ap.add_argument("--gh-timeout", type=int, default=30)
    ap.add_argument("--gh-sleep", type=float, default=0.2)

    args = ap.parse_args()

    mark_scores = parse_mark_scores(args.mark_scores, args.mark_score)

    ensure_tool_exists(args.downloader)
    if args.skillmd != "none":
        ensure_tool_exists("gh")

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end) if args.end else utc_today_date()

    workdir = Path(args.workdir)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)

    db_path = outdir / args.db
    conn = init_db(db_path)

    # incremental unique repos (new repos only)
    unique_file = outdir / "repos_unique.txt"
    unique_fh = unique_file.open("a", encoding="utf-8")

    # incremental marked repos (new repos only), per threshold
    marked_fhs: Dict[int, "TextIO"] = {}
    marked_files: Dict[int, Path] = {}
    for ms in mark_scores:
        mf = outdir / f"repos_marked_agent_skill_ms{ms}.txt"
        marked_files[ms] = mf
        marked_fhs[ms] = mf.open("a", encoding="utf-8")

    print(f"[+] Range UTC: {start.isoformat()} -> {end.isoformat()}")
    print(f"[+] Workdir: {workdir}")
    print(f"[+] Outdir:  {outdir}")
    print(f"[+] Downloader: {args.downloader}")
    print(f"[+] Hours/day: {args.hours_per_day} (seed={args.seed})")
    if args.resume:
        print(f"[+] Resume: enabled (db={db_path})")
    if args.reprocess_workdir:
        print("[+] Mode: reprocess-workdir (no downloads)")
    print("[+] Scoring: repo-name + event-payload text (commits/PRs/issues/comments/releases)")
    print(f"[+] Mark thresholds: {mark_scores}")

    total_new = 0

    for day in daterange(start, end):
        if args.resume and day_already_done(conn, day):
            print(f"[+] {day.isoformat()}: skipped (already done)")
            continue

        day_dir = workdir / day.isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)

        picked_hours = choose_hours_for_day(day, args.hours_per_day, args.seed)

        day_repos: Set[str] = set()
        day_repo_best_score: Dict[str, int] = {}
        gz_files: List[Path] = []

        if args.reprocess_workdir:
            print(f"[+] {day.isoformat()}: reprocess existing files in {day_dir}")
            gz_files = sorted(day_dir.glob("*.json.gz"))
            if not gz_files:
                print(f"WARNING: no .json.gz files found in {day_dir}", file=sys.stderr)
        else:
            print(f"[+] {day.isoformat()}: hours={','.join(str(h) for h in picked_hours)}")
            for hour in picked_hours:
                gz_path = download_hour(day, hour, day_dir, args.downloader, quiet=args.quiet_download)
                if gz_path is None:
                    continue
                gz_files.append(gz_path)

        ok_files = 0
        for gz_path in gz_files:
            ok_files += 1
            for repo, text in iter_repo_texts_from_gz(gz_path):
                day_repos.add(repo)
                score = agent_skill_score_repo_name(repo) + agent_skill_score_text(text)
                prev = day_repo_best_score.get(repo, 0)
                if score > prev:
                    day_repo_best_score[repo] = score

        # per-day repos file (audit)
        day_out = outdir / f"repos_{day.isoformat()}.txt"
        day_out.write_text("\n".join(sorted(day_repos)) + ("\n" if day_repos else ""), encoding="utf-8")

        # per-day marked repos files (includes score) for each threshold
        for ms in mark_scores:
            day_marked_lines: List[str] = []
            for repo in sorted(day_repos):
                score = day_repo_best_score.get(repo, agent_skill_score_repo_name(repo))
                if score >= ms:
                    day_marked_lines.append(f"{repo}\t{score}")

            day_marked_out = outdir / f"repos_marked_ms{ms}_{day.isoformat()}.txt"
            day_marked_out.write_text(
                "\n".join(day_marked_lines) + ("\n" if day_marked_lines else ""),
                encoding="utf-8"
            )

        # insert into db + append incremental unique/marked files
        new_today = 0
        new_marked_today_by_ms: Dict[int, int] = {ms: 0 for ms in mark_scores}

        for repo in sorted(day_repos):
            score = day_repo_best_score.get(repo, agent_skill_score_repo_name(repo))
            inserted = upsert_repo_max_score(conn, repo, day, score)
            if inserted:
                new_today += 1
                unique_fh.write(repo + "\n")
                for ms in mark_scores:
                    if score >= ms:
                        new_marked_today_by_ms[ms] += 1
                        marked_fhs[ms].write(f"{repo}\t{score}\n")

        conn.commit()
        total_new += new_today

        marked_summary = " ".join(f"new_marked_ms{ms}={new_marked_today_by_ms[ms]}" for ms in mark_scores)
        print(
            f"    files={ok_files}/{len(gz_files)} "
            f"repos(day)={len(day_repos)} "
            f"new_unique={new_today} {marked_summary} "
            f"total_new_unique={total_new}"
        )

        # clean up downloads only if we are in download mode and keep-downloads is not set
        if not args.reprocess_workdir:
            if not args.keep_downloads:
                safe_rmtree(day_dir)
            else:
                print(f"    keeping downloads in {day_dir}")

        mark_day_done(conn, day)

    unique_fh.close()
    for fh in marked_fhs.values():
        fh.close()

    print(f"[+] Done. Incremental unique list: {unique_file}")
    for ms in mark_scores:
        print(f"[+] Done. Incremental marked list (ms={ms}): {marked_files[ms]}")
    print(f"[+] SQLite DB (resume/dedupe): {db_path}")

    # Optional: SKILL.md search
    if args.skillmd == "none":
        return

    # NOTE: skillmd selection still uses a single threshold notion; we use the MIN threshold
    # as the definition of "marked" for the SKILL.md stage. Change if you prefer max/explicit.
    marked_threshold_for_skillmd = min(mark_scores) if mark_scores else int(args.mark_score)

    repos_all = [r for (r,) in conn.execute("SELECT repo FROM repos ORDER BY repo;").fetchall()]
    repos_marked = [r for (r,) in conn.execute(
        "SELECT repo FROM repos WHERE agent_score >= ? ORDER BY repo;", (marked_threshold_for_skillmd,)
    ).fetchall()]

    if args.skillmd == "all":
        to_search = repos_all
        mode_used = "all"
    elif args.skillmd == "marked":
        to_search = repos_marked
        mode_used = f"marked (>= {marked_threshold_for_skillmd})"
    else:
        if len(repos_all) <= args.auto_max:
            to_search = repos_all
            mode_used = f"auto->all (<= {args.auto_max})"
        else:
            to_search = repos_marked
            mode_used = f"auto->marked (> {args.auto_max}; >= {marked_threshold_for_skillmd})"

    print(f"[+] SKILL.md search mode: {mode_used}; repos to check: {len(to_search)}")

    found_file = outdir / "repos_with_SKILL_md.txt"
    err_file = outdir / "repos_skill_search_errors.txt"
    found_fh = found_file.open("w", encoding="utf-8")
    err_fh = err_file.open("w", encoding="utf-8")

    found = 0
    errors = 0
    for i, repo in enumerate(to_search, 1):
        ok, msg = gh_search_skillmd(repo, timeout_sec=args.gh_timeout)
        if ok:
            found += 1
            found_fh.write(repo + "\n")
        elif msg:
            errors += 1
            err_fh.write(f"{repo}\t{msg}\n")

        if args.gh_sleep > 0:
            time.sleep(args.gh_sleep)

        if i % 200 == 0:
            print(f"    gh checked {i}/{len(to_search)} found={found} errors={errors}")

    found_fh.close()
    err_fh.close()
    print(f"[+] Found SKILL.md in {found} repos -> {found_file}")
    print(f"[+] Errors for {errors} repos -> {err_file}")


if __name__ == "__main__":
    main()