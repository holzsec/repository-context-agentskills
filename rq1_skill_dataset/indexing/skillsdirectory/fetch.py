import csv
import json
import time
from urllib.request import urlopen
from urllib.parse import urlencode

BASE = "https://skillsdirectory.com/api/registry"
LIMIT = 100  # max 100 per docs

def fetch_page(offset: int):
    qs = urlencode({"limit": LIMIT, "offset": offset})
    with urlopen(f"{BASE}?{qs}") as r:
        return json.loads(r.read().decode("utf-8"))

all_skills = []
offset = 0

while True:
    data = fetch_page(offset)
    skills = data.get("skills", [])
    all_skills.extend(skills)

    pag = data.get("pagination", {})
    if not pag.get("hasMore"):
        break

    offset += LIMIT
    time.sleep(0.1)  # be polite

# JSON Lines (easy to stream/process)
with open("skills.jsonl", "w", encoding="utf-8") as f:
    for s in all_skills:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")

# CSV (common fields)
fields = ["name", "slug", "description", "repository", "category", "author", "stars", "verified"]
with open("skills.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for s in all_skills:
        w.writerow({k: s.get(k) for k in fields})

print(f"Downloaded {len(all_skills)} skills.")
