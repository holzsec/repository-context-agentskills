#!/usr/bin/env python3
"""Resolve extracted endpoints to geolocation buckets by marketplace.

The script normalizes URL/IP findings from the analysis database, resolves host
names to public IPs, applies MaxMind or cached country/continent data, and
emits CSV/LaTeX summaries for marketplace comparison.
"""

import argparse
import concurrent.futures
import csv
import ipaddress
import json
import re
import socket
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
import tldextract
from urllib.parse import urlparse
from marketplace_latex_utils import MARKETPLACE_ORDER, write_marketplace_latex_table

try:
    import maxminddb
except ImportError:
    maxminddb = None

try:
    import dns.resolver
except ImportError:
    dns = None

TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=None, cache_dir="/tmp/tldextract-cache")
HASH_APP_RE = re.compile(r"^(?P<name>.+)_(?P<hash>[0-9a-f]{64})$")
ANALYZED_DEFAULTS = {
    "ClawHub": 16755,
    "skills_sh": 125928,
    "skillsdirectory": 17611,
    "gharchive": 136095,
}
EU_COUNTRY_CODES = {
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
}
CONTINENT_CODE_TO_NAME = {
    "AF": "Africa",
    "AN": "Antarctica",
    "AS": "Asia",
    "EU": "Europe",
    "NA": "North America",
    "OC": "Oceania",
    "SA": "South America",
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
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    if level == "subdomain":
        return host

    extracted = TLD_EXTRACTOR(host)
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}".lower()
    return host


def _extract_geo_from_mmdb_record(record: object) -> dict[str, str]:
    geo = {
        "country_code": "",
        "country_name": "",
        "continent": "",
        "continent_code": "",
        "geo_asn": "",
        "geo_as_name": "",
        "geo_as_domain": "",
    }
    if not isinstance(record, dict):
        return geo

    country_raw = record.get("country")
    if isinstance(country_raw, str):
        geo["country_name"] = country_raw.strip()
    elif isinstance(country_raw, dict):
        geo["country_code"] = str(country_raw.get("iso_code") or "").strip().upper()
        geo["country_name"] = str(country_raw.get("name") or "").strip()
        if not geo["country_name"]:
            names = country_raw.get("names")
            if isinstance(names, dict):
                geo["country_name"] = str(names.get("en") or "").strip()

    if not geo["country_code"]:
        geo["country_code"] = str(record.get("country_code") or "").strip().upper()
    if not geo["country_name"]:
        geo["country_name"] = str(record.get("country_name") or "").strip()

    continent_raw = record.get("continent")
    if isinstance(continent_raw, str):
        geo["continent"] = continent_raw.strip()
    elif isinstance(continent_raw, dict):
        geo["continent"] = str(continent_raw.get("name") or "").strip()
    geo["continent_code"] = str(record.get("continent_code") or "").strip().upper()

    geo["geo_asn"] = str(record.get("asn") or "").strip()
    geo["geo_as_name"] = str(record.get("as_name") or "").strip()
    geo["geo_as_domain"] = str(record.get("as_domain") or "").strip()
    return geo


def load_ip_lookup_cache(path):
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}

    cache = {}
    for host, ips in payload.items():
        if not isinstance(host, str):
            continue
        if not isinstance(ips, list):
            continue
        cleaned = []
        seen = set()
        for ip in ips:
            if not isinstance(ip, str):
                continue
            value = ip.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            cleaned.append(value)
        cache[host] = cleaned
    return cache


def save_ip_lookup_cache(path, cache):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def load_ip_country_cache(path):
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}

    cache = {}
    for ip, country in payload.items():
        if not isinstance(ip, str):
            continue
        key = ip.strip()
        if not key:
            continue
        value = str(country).strip().upper() if country is not None else ""
        cache[key] = value or None
    return cache


def save_ip_country_cache(path, cache):
    payload = {}
    for ip, country in cache.items():
        key = str(ip).strip()
        if not key:
            continue
        payload[key] = country if country is not None else ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def resolve_ips(host, lookup_cache, nameserver=None, dns_timeout=3.0):
    """Resolve a host to IP addresses with cache support and optional DNS server."""

    cache_key = host if not nameserver else f"{host}|ns={nameserver}"
    if cache_key in lookup_cache:
        return lookup_cache[cache_key]

    try:
        ipaddress.ip_address(host)
        ips = [host]
        lookup_cache[cache_key] = ips
        return ips
    except ValueError:
        pass

    ips = []
    if nameserver:
        if dns is None:
            raise RuntimeError(
                "dnspython is required for custom nameserver lookup. Install with: pip install dnspython"
            )
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = [nameserver]
        resolver.timeout = dns_timeout
        resolver.lifetime = dns_timeout
        for rrtype in ("A", "AAAA"):
            try:
                answers = resolver.resolve(host, rrtype)
                for answer in answers:
                    ips.append(answer.to_text())
            except Exception:
                continue
    else:
        try:
            for info in socket.getaddrinfo(host, None):
                ips.append(info[4][0])
        except (socket.gaierror, UnicodeError):
            return []

    seen = set()
    out = []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    lookup_cache[cache_key] = out
    return out


def first_public_ip(ips):
    for ip in ips:
        try:
            obj = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not (obj.is_private or obj.is_loopback or obj.is_link_local or obj.is_multicast or obj.is_reserved):
            return ip
    return None


def geo_bucket_from_ip(ip, mmdb_reader, cache, geo_level):
    """Return the configured geolocation bucket for an IP address."""

    cache_key = f"{geo_level}:{ip}"
    if cache_key in cache:
        return cache[cache_key]

    try:
        ipaddress.ip_address(ip)
    except ValueError:
        cache[cache_key] = None
        return None

    try:
        if hasattr(mmdb_reader, "get_with_prefix_len"):
            geo_response = mmdb_reader.get_with_prefix_len(ip)[0]
        else:
            geo_response = mmdb_reader.get(ip)
    except Exception:
        cache[cache_key] = None
        return None

    parsed = _extract_geo_from_mmdb_record(geo_response)
    if geo_level == "continent":
        bucket = (parsed.get("continent_code") or parsed.get("continent") or "").strip().upper() or None
    else:
        bucket = (parsed.get("country_code") or parsed.get("country_name") or "").strip().upper() or None
    cache[cache_key] = bucket
    return bucket


def load_findings_with_skills(conn):
    try:
        rows = conn.execute(
            """
            SELECT rf.finding, lower(rf.finding_type), f.app_id, a.app_name
            FROM regex_findings rf
            LEFT JOIN files f ON f.id = rf.file_id
            LEFT JOIN apps a ON a.id = f.app_id
            WHERE lower(rf.finding_type) IN ('url', 'ip_address')
            """
        ).fetchall()
        return rows, True
    except sqlite3.OperationalError:
        rows = conn.execute(
            """
            SELECT finding, lower(finding_type), NULL AS app_id, NULL AS app_name
            FROM regex_findings
            WHERE lower(finding_type) IN ('url', 'ip_address')
            """
        ).fetchall()
        return rows, False


def normalize_country(country, geo_level="country"):
    value = (country or "").strip().upper()
    if not value:
        return "UNKNOWN"
    if geo_level == "continent":
        return CONTINENT_CODE_TO_NAME.get(value, value.title())
    if value in EU_COUNTRY_CODES:
        return "EU"
    return value


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

    conn = sqlite3.connect(marketplace_db_path)
    conn.execute("PRAGMA temp_store=MEMORY")
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
    conn = sqlite3.connect(marketplace_db_path)
    conn.execute("PRAGMA temp_store=MEMORY")
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
        app_to_marketplaces[app_id] = marketplaces
    return app_to_marketplaces


def main():
    parser = argparse.ArgumentParser(description="Analyze geolocation of collected endpoints from regex_findings.")
    parser.add_argument("--db", default="../results/final_paper.db", help="Path to SQLite DB")
    parser.add_argument(
        "--mmdb",
        default="latest.mmdb",
        help="Path to MaxMind MMDB (fallbacks to location_extended_latest.mmdb if missing)",
    )
    parser.add_argument("--out", default="analysis_outputs/geolocation_by_country.csv", help="Output CSV")
    parser.add_argument(
        "--out-marketplace",
        default="analysis_outputs/geolocation_by_country_marketplace.csv",
        help="Output CSV for geolocation stats per marketplace",
    )
    parser.add_argument(
        "--marketplace-db",
        default="../dataset/full_skill_marketplace.db",
        help="Path to full_skill_marketplace.db for hash->marketplace mapping",
    )
    parser.add_argument("--clawhub-name", default="ClawHub", help="Marketplace label for non-hash app_name rows")
    parser.add_argument("--unknown-hash-name", default="UnknownHashed", help="Marketplace label for unmapped hash app_name rows")
    parser.add_argument("--latex-out", default="analysis_outputs/geolocation_by_country_marketplace.tex", help="Output LaTeX table path")
    parser.add_argument("--latex-caption", default="Endpoint geolocation by marketplace", help="LaTeX table caption")
    parser.add_argument("--latex-label", default="table:geolocation_by_country_marketplace", help="LaTeX table label")
    parser.add_argument("--total-skills-clawhub", type=int, default=ANALYZED_DEFAULTS["ClawHub"])
    parser.add_argument("--total-skills-skills-sh", type=int, default=ANALYZED_DEFAULTS["skills_sh"])
    parser.add_argument("--total-skills-skillsdirectory", type=int, default=ANALYZED_DEFAULTS["skillsdirectory"])
    parser.add_argument("--total-skills-gharchive", type=int, default=ANALYZED_DEFAULTS["gharchive"])
    parser.add_argument(
        "--ip-lookup-cache",
        default="analysis_outputs/geolocation_ip_lookup_cache.json",
        help="Cache file for hostname -> resolved IP list",
    )
    parser.add_argument(
        "--ip-country-cache",
        default="analysis_outputs/geolocation_ip_country_cache.json",
        help="Cache file for public IP -> country code/name",
    )
    parser.add_argument(
        "--dns-server",
        default="8.8.8.8",
        help="DNS nameserver IP for domain lookup (default: 8.8.8.8)",
    )
    parser.add_argument(
        "--dns-timeout",
        type=float,
        default=3.0,
        help="DNS query timeout in seconds when --dns-server is set",
    )
    parser.add_argument("--top", type=int, default=20, help="Rows to print")
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of worker threads for hostname -> IP lookup",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print periodic progress updates while processing hosts and lookups.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=2000,
        help="Print verbose progress every N processed items.",
    )
    parser.add_argument(
        "--top-countries",
        type=int,
        default=20,
        help="Keep only the top N geo buckets by endpoint_count; aggregate the rest into OTHERS",
    )
    parser.add_argument(
        "--geo-level",
        choices=["country", "continent"],
        default="continent",
        help="Geolocation aggregation level for output buckets",
    )
    parser.add_argument(
        "--level",
        choices=["domain", "subdomain"],
        default="subdomain",
        help="URL host aggregation level before geolocation",
    )
    args = parser.parse_args()

    if maxminddb is None:
        raise RuntimeError("maxminddb is required. Install with: pip install maxminddb")

    mmdb_path = Path(args.mmdb)
    if not mmdb_path.exists():
        fallback = Path("location_extended_latest.mmdb")
        if fallback.exists():
            mmdb_path = fallback
        else:
            raise FileNotFoundError(f"MMDB file not found: {mmdb_path}")

    mmdb_reader = maxminddb.open_database(str(mmdb_path))
    conn = sqlite3.connect(args.db)
    rows, has_skill_mapping = load_findings_with_skills(conn)
    app_to_marketplaces = {}
    if has_skill_mapping:
        try:
            app_to_marketplaces = load_app_marketplaces(
                conn,
                args.marketplace_db,
                args.clawhub_name,
                args.unknown_hash_name,
            )
        except sqlite3.OperationalError:
            app_to_marketplaces = {}
    total_skills = 0
    if has_skill_mapping:
        try:
            total_skills = conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0] or 0
        except sqlite3.OperationalError:
            total_skills = 0
    conn.close()

    country_counts_raw = Counter()
    country_skill_sets_raw = defaultdict(set)
    host_to_skills = defaultdict(set)
    host_to_marketplaces = defaultdict(set)
    marketplace_total_skills_observed = defaultdict(set)
    unique_hosts = set()
    unresolved_hosts = 0
    no_country_match = 0
    ip_country_cache_path = Path(args.ip_country_cache)
    ip_country_cache = load_ip_country_cache(ip_country_cache_path)
    ip_lookup_cache_path = Path(args.ip_lookup_cache)
    ip_lookup_cache = load_ip_lookup_cache(ip_lookup_cache_path)

    for finding, finding_type, app_id, app_name in rows:
        host = extract_host_from_finding(finding, finding_type)
        if finding_type == "url":
            host = normalize_host_level(host, args.level)
        if not host:
            continue
        unique_hosts.add(host)
        if app_id is not None:
            host_to_skills[host].add((app_id, app_name or ""))
            for marketplace in app_to_marketplaces.get(app_id, {args.unknown_hash_name}):
                host_to_marketplaces[host].add(marketplace)
                marketplace_total_skills_observed[marketplace].add(app_id)
        if args.verbose and args.progress_every > 0 and len(unique_hosts) % args.progress_every == 0:
            print(f"[progress] unique hosts collected: {len(unique_hosts)}")

    if total_skills == 0:
        total_skills = len({skill for skills in host_to_skills.values() for skill in skills})

    host_country = {}
    marketplace_country_counts_raw = Counter()
    marketplace_country_skill_sets_raw = defaultdict(set)
    try:
        host_public_ips = {}
        max_workers = max(1, int(args.workers))
        dns_server = args.dns_server.strip() or None
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_host = {
                executor.submit(
                    resolve_ips,
                    host,
                    ip_lookup_cache,
                    nameserver=dns_server,
                    dns_timeout=args.dns_timeout,
                ): host
                for host in unique_hosts
            }
            for future in concurrent.futures.as_completed(future_to_host):
                processed_futures = len(host_public_ips) + unresolved_hosts + 1
                host = future_to_host[future]
                try:
                    ips = future.result()
                except Exception:
                    ips = []
                public_ip = first_public_ip(ips)
                if not public_ip:
                    unresolved_hosts += 1
                    if (
                        args.verbose
                        and args.progress_every > 0
                        and processed_futures % args.progress_every == 0
                    ):
                        print(
                            f"[progress] dns resolved {processed_futures}/{len(unique_hosts)} hosts "
                            f"(mapped_public_ip={len(host_public_ips)}, unresolved={unresolved_hosts})"
                        )
                    continue
                host_public_ips[host] = public_ip
                if (
                    args.verbose
                    and args.progress_every > 0
                    and processed_futures % args.progress_every == 0
                ):
                    print(
                        f"[progress] dns resolved {processed_futures}/{len(unique_hosts)} hosts "
                        f"(mapped_public_ip={len(host_public_ips)}, unresolved={unresolved_hosts})"
                    )

        total_geolookups = len(host_public_ips)
        for idx, (host, public_ip) in enumerate(host_public_ips.items(), start=1):
            geo_bucket = geo_bucket_from_ip(public_ip, mmdb_reader, ip_country_cache, args.geo_level)
            if geo_bucket is None:
                no_country_match += 1
                if args.verbose and args.progress_every > 0 and idx % args.progress_every == 0:
                    print(
                        f"[progress] geolocated {idx}/{total_geolookups} hosts "
                        f"(no_country_match={no_country_match})"
                    )
                continue
            grouped_country = normalize_country(geo_bucket, args.geo_level)
            host_country[host] = grouped_country
            country_counts_raw[grouped_country] += 1
            for skill in host_to_skills.get(host, set()):
                country_skill_sets_raw[grouped_country].add(skill)
            for marketplace in host_to_marketplaces.get(host, {args.unknown_hash_name}):
                marketplace_country_counts_raw[(marketplace, grouped_country)] += 1
                for app_id, app_name in host_to_skills.get(host, set()):
                    if marketplace in app_to_marketplaces.get(app_id, {args.unknown_hash_name}):
                        marketplace_country_skill_sets_raw[(marketplace, grouped_country)].add((app_id, app_name))
            if args.verbose and args.progress_every > 0 and idx % args.progress_every == 0:
                print(
                    f"[progress] geolocated {idx}/{total_geolookups} hosts "
                    f"(no_country_match={no_country_match})"
                )
    finally:
        mmdb_reader.close()

    top_n = max(args.top_countries, 0)
    top_countries = {country for country, _count in country_counts_raw.most_common(top_n)}
    country_alias = {}
    for country in country_counts_raw:
        country_alias[country] = country if country in top_countries else "OTHERS"

    country_counts = Counter()
    country_skill_sets = defaultdict(set)
    for country, count in country_counts_raw.items():
        bucket = country_alias[country]
        country_counts[bucket] += count
        country_skill_sets[bucket].update(country_skill_sets_raw.get(country, set()))

    marketplace_country_counts = Counter()
    marketplace_country_skill_sets = defaultdict(set)
    for (marketplace, country), count in marketplace_country_counts_raw.items():
        bucket = country_alias.get(country, country)
        marketplace_country_counts[(marketplace, bucket)] += count
        marketplace_country_skill_sets[(marketplace, bucket)].update(
            marketplace_country_skill_sets_raw.get((marketplace, country), set())
        )

    total_geolocated_endpoints = sum(country_counts.values())
    geo_col = "continent" if args.geo_level == "continent" else "country"
    row_header = "Continent" if args.geo_level == "continent" else "Country"
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
                geo_col,
                "geo_level",
                "endpoint_count",
                "endpoint_pct_of_geolocated",
                "skill_count",
                "skill_pct_of_total",
            ]
        )
        for country, count in country_counts.most_common():
            endpoint_pct = (count / total_geolocated_endpoints * 100.0) if total_geolocated_endpoints else 0.0
            skill_count = len(country_skill_sets.get(country, set()))
            skill_pct = (skill_count / total_skills * 100.0) if total_skills else 0.0
            writer.writerow([country, geo_col, count, f"{endpoint_pct:.2f}", skill_count, f"{skill_pct:.2f}"])

    out_marketplace_path = Path(args.out_marketplace)
    out_marketplace_path.parent.mkdir(parents=True, exist_ok=True)
    with out_marketplace_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "marketplace",
                geo_col,
                "geo_level",
                "endpoint_count",
                "endpoint_pct_of_marketplace_geolocated",
                "skill_count",
                "skill_pct_of_marketplace_analyzed",
            ]
        )
        marketplace_totals = Counter()
        for (marketplace, _country), count in marketplace_country_counts.items():
            marketplace_totals[marketplace] += count
        for (marketplace, country), count in sorted(
            marketplace_country_counts.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        ):
            marketplace_total = marketplace_totals[marketplace]
            endpoint_pct = (count / marketplace_total * 100.0) if marketplace_total else 0.0
            skill_count = len(marketplace_country_skill_sets.get((marketplace, country), set()))
            skill_total = marketplace_skill_totals.get(marketplace, 0)
            skill_pct = (skill_count / skill_total * 100.0) if skill_total else 0.0
            writer.writerow([marketplace, country, geo_col, count, f"{endpoint_pct:.2f}", skill_count, f"{skill_pct:.2f}"])

    marketplace_finding_totals = Counter()
    for (marketplace, _country), count in marketplace_country_counts.items():
        marketplace_finding_totals[marketplace] += count
    row_names = sorted({country for _, country in marketplace_country_counts.keys()}, key=lambda x: (-country_counts.get(x, 0), x))
    skills_ratios = {}
    findings_ratios = {}
    for row_name in row_names:
        for marketplace, _display in MARKETPLACE_ORDER:
            skill_total = marketplace_skill_totals.get(marketplace, 0)
            finding_total = marketplace_finding_totals.get(marketplace, 0)
            skill_count = len(marketplace_country_skill_sets.get((marketplace, row_name), set()))
            finding_count = marketplace_country_counts.get((marketplace, row_name), 0)
            skills_ratios[(row_name, marketplace)] = (skill_count / skill_total) if skill_total else 0.0
            findings_ratios[(row_name, marketplace)] = (finding_count / finding_total) if finding_total else 0.0

    write_marketplace_latex_table(
        args.latex_out,
        row_header=row_header,
        row_names=row_names,
        skills_ratios=skills_ratios,
        findings_ratios=findings_ratios,
        caption=args.latex_caption,
        label=args.latex_label,
    )
    save_ip_lookup_cache(ip_lookup_cache_path, ip_lookup_cache)
    save_ip_country_cache(ip_country_cache_path, ip_country_cache)

    print(f"Processed unique hosts ({args.level} level): {len(unique_hosts)}")
    print(f"Could not resolve to public IP: {unresolved_hosts}")
    print(f"Resolved IP without country range match: {no_country_match}")
    print(f"Total geolocated endpoints: {total_geolocated_endpoints}")
    print(f"Total skills: {total_skills}")
    print(f"IP lookup cache entries: {len(ip_lookup_cache)}")
    print(f"IP country cache entries: {len(ip_country_cache)}")
    print(f"MMDB path: {mmdb_path}")
    print(f"DNS server: {args.dns_server.strip() or 'system-default'}")
    print(f"Lookup workers: {max(1, int(args.workers))}")
    print(f"Top {geo_col}s kept before OTHERS aggregation: {top_n}")
    print(f"Top {geo_col}s:")
    for country, count in country_counts.most_common(args.top):
        endpoint_pct = (count / total_geolocated_endpoints * 100.0) if total_geolocated_endpoints else 0.0
        skill_count = len(country_skill_sets.get(country, set()))
        skill_pct = (skill_count / total_skills * 100.0) if total_skills else 0.0
        print(
            f"  {country}: {count} endpoints ({endpoint_pct:.2f}%), "
            f"{skill_count} skills ({skill_pct:.2f}%)"
        )
    print(f"Wrote: {out_path}")
    print(f"Wrote: {out_marketplace_path}")
    print(f"Wrote: {args.latex_out}")
    print("Marketplace skill totals used for LaTeX ratios:")
    for marketplace, _ in MARKETPLACE_ORDER:
        observed = len(marketplace_total_skills_observed.get(marketplace, set()))
        configured = marketplace_skill_totals.get(marketplace, 0)
        print(f"  {marketplace}: configured={configured}, observed_in_db={observed}")


if __name__ == "__main__":
    main()
