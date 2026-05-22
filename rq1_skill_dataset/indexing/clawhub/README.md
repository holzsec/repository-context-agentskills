# ClawHub Data Collection Pipeline

This project continuously collects “skills” metadata from ClawHub and downloads new/updated skills into a data directory.

It is split into two roles:

- **Lister** (high frequency): polls the ClawHub skills listing API and appends *new* `(slug, latestVersion)` items into a pipeline file.
- **Downloader workers** (parallel): consume the pipeline file and download skills into a download folder (zip if available, otherwise `SKILL.md` fallback).

---

# Directory Layout

## Program files (code only)
```
/opt/openclaw/clawhub_dl.py
/opt/openclaw/.venv-clawenv/
```

## Data/output
```
/scans/08_DATA_crawlhub/state/
/scans/08_DATA_crawlhub/downloads/
```

---

# State Files (inside state/)

```
pipeline.jsonl      # append-only queue produced by lister
lister_state.json   # lister dedupe state (seen versions per slug)
claimed.jsonl       # items claimed by workers
downloaded.jsonl    # successful downloads
failed.jsonl        # failed downloads
```

All `*.jsonl` files are JSON Lines format (one JSON object per line).

---

# How It Works

## Lister

- Runs every **30 seconds**
- Scans first **5 pages**
- Page size **50**
- Covers 250 most recent skills per run
- Always starts from the top (`cursor=None`)
- Only enqueues if `(slug, latestVersion)` changed

This prevents missing newly published or updated skills.

## Downloader Workers

- Multiple workers allowed
- Use file locking to avoid duplicate downloads
- Skip if file already exists
- Log claims and completions

## Rate Limit Handling

Reads headers:

- `x-ratelimit-remaining`
- `x-ratelimit-reset`

If remaining is low or HTTP 429 occurs:
- Sleep until reset time
- Resume automatically

---

# Running Manually

## Lister

```
/opt/openclaw/.venv-clawenv/bin/python \
/opt/openclaw/clawhub_dl.py \
lister \
--loop \
--interval 30 \
--pages 5 \
--limit 50 \
--state-dir /scans/08_DATA_crawlhub/state
```

## Downloader Worker

```
/opt/openclaw/.venv-clawenv/bin/python \
/opt/openclaw/clawhub_dl.py \
downloader \
--loop \
--worker-id w1 \
--state-dir /scans/08_DATA_crawlhub/state \
--download-dir /scans/08_DATA_crawlhub/downloads
```

Start multiple workers by changing worker-id:

```
--worker-id w2
--worker-id w3
```

---

# systemd Services

## Lister Service

File:
```
/etc/systemd/system/data_collection-clawhub-lister.service
```

```
[Unit]
Description=Data Collection - ClawHub Skill Lister
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/openclaw

ExecStart=/opt/openclaw/.venv-clawenv/bin/python \
          /opt/openclaw/clawhub_dl.py \
          lister \
          --loop \
          --interval 30 \
          --pages 5 \
          --limit 50 \
          --state-dir /scans/08_DATA_crawlhub/state

Restart=always
RestartSec=2
KillSignal=SIGINT
TimeoutStopSec=20

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

---

## Downloader Template Service

File:
```
/etc/systemd/system/data_collection-clawhub-downloader@.service
```

```
[Unit]
Description=Data Collection - ClawHub Downloader Worker (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/openclaw

ExecStart=/opt/openclaw/.venv-clawenv/bin/python \
          /opt/openclaw/clawhub_dl.py \
          downloader \
          --loop \
          --worker-id %i \
          --state-dir /scans/08_DATA_crawlhub/state \
          --download-dir /scans/08_DATA_crawlhub/downloads

Restart=always
RestartSec=2
KillSignal=SIGINT
TimeoutStopSec=20

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

---

# Enable Services

```
sudo systemctl daemon-reload

sudo systemctl enable --now data_collection-clawhub-lister.service

sudo systemctl enable --now data_collection-clawhub-downloader@w1.service
sudo systemctl enable --now data_collection-clawhub-downloader@w2.service
sudo systemctl enable --now data_collection-clawhub-downloader@w3.service
```

---

# Monitoring

## View logs

```
journalctl -u data_collection-clawhub-lister -f
journalctl -u data_collection-clawhub-downloader@w1 -f
```

---

# Inspecting State

## Count Seen Skills

```
jq '.seen | length' /scans/08_DATA_crawlhub/state/lister_state.json
```

## Count Pipeline Entries

```
wc -l /scans/08_DATA_crawlhub/state/pipeline.jsonl
```

## Count Downloaded Items

```
jq -r '.key' /scans/08_DATA_crawlhub/state/downloaded.jsonl 2>/dev/null | sort -u | wc -l
```

## Count Claimed Items

```
jq -r '.key' /scans/08_DATA_crawlhub/state/claimed.jsonl 2>/dev/null | sort -u | wc -l
```

## Pending Downloads

```
comm -23 \
  <(jq -r '.slug + "@" + (.latest // "unknown")' /scans/08_DATA_crawlhub/state/pipeline.jsonl | sort -u) \
  <(jq -r '.key' /scans/08_DATA_crawlhub/state/downloaded.jsonl | sort -u) \
  | wc -l
```

## Count Downloaded Files

```
find /scans/08_DATA_crawlhub/downloads -type f | wc -l
```

---

# Security Note

Downloaded skills should be treated as untrusted content.
This pipeline only downloads files; it does not execute them.

Review any skill before running it.
