#!/usr/bin/env python3
"""Paper triage for the /papers/ page.

Fetch recent arXiv and ePrint papers, score each title and abstract with the
weighted keyword rules in keywords.txt, and write papers.json, which
papers/index.html reads and renders. Run daily by
.github/workflows/papers.yml.

    python3 papers/triage.py [--previous OLD.json] [--out papers.json]

--previous is the papers.json of the last run: its papers are kept (and
rescored with the current rules) until they are older than KEEP_DAYS.

Each paper has two dates: "date" is when it was submitted, "seen" is when a
run first found it. Both sites list a paper a day or more after submission,
so the page groups by "seen", the day the paper actually showed up.

Standard library only.
"""

import argparse
import datetime as dt
import email.utils
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def env(name, default):
    return os.environ.get(name) or default


HERE = os.path.dirname(os.path.abspath(__file__))

ARXIV_QUERY = env("ARXIV_QUERY", "cat:cs.CR")   # e.g. "cat:cs.CR OR cat:cs.IT"
ARXIV_MAX = int(env("ARXIV_MAX", "300"))
INCLUDE_EPRINT = env("INCLUDE_EPRINT", "1") == "1"
THRESHOLD = int(env("THRESHOLD", "3"))          # papers below are hidden by default
KEEP_DAYS = int(env("KEEP_DAYS", "14"))
KEYWORDS_FILE = env("KEYWORDS_FILE", os.path.join(HERE, "keywords.txt"))

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
DC = "{http://purl.org/dc/elements/1.1/}"


def log(*args):
    print(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def squash(s):
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------- fetching

def http_get(url, tries=4):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-triage/1.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as ex:
            if attempt == tries - 1:
                raise
            log("fetch failed, retrying:", ex)
            time.sleep(20 * (attempt + 1))


def fetch_arxiv():
    params = urllib.parse.urlencode({
        "search_query": ARXIV_QUERY,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": ARXIV_MAX,
    })
    root = ET.fromstring(http_get("https://export.arxiv.org/api/query?" + params))
    papers = []
    for e in root.iter(ATOM + "entry"):
        m = re.search(r"abs/(.+?)(v\d+)?$", e.findtext(ATOM + "id") or "")
        if not m:
            continue
        cat = e.find(ARXIV_NS + "primary_category")
        papers.append({
            "id": "arxiv:" + m.group(1),
            "source": "arXiv",
            "category": cat.get("term", "") if cat is not None else "",
            "title": squash(e.findtext(ATOM + "title")),
            "abstract": squash(e.findtext(ATOM + "summary")),
            "authors": ", ".join(squash(a.findtext(ATOM + "name"))
                                 for a in e.findall(ATOM + "author")),
            "link": "https://arxiv.org/abs/" + m.group(1),
            "date": e.findtext(ATOM + "published") or now_iso(),
        })
    return papers


def fetch_eprint():
    root = ET.fromstring(http_get("https://eprint.iacr.org/rss/rss.xml"))
    papers = []
    for it in root.iter("item"):
        link = squash(it.findtext("link"))
        m = re.search(r"(\d{4}/\d+)", link)
        if not m:
            continue
        try:
            date = email.utils.parsedate_to_datetime(it.findtext("pubDate"))
            date = date.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            date = now_iso()
        papers.append({
            "id": "eprint:" + m.group(1),
            "source": "ePrint",
            "category": squash(it.findtext("category")),
            "title": squash(it.findtext("title")),
            "abstract": squash(re.sub(r"<[^>]+>", " ", it.findtext("description") or "")),
            "authors": ", ".join(squash(c.text) for c in it.findall(DC + "creator")),
            "link": link,
            "date": date,
        })
    return papers


# ----------------------------------------------------------------- scoring

def load_rules():
    """Returns [(weight, compiled regex)] from keywords.txt."""
    rules = []
    with open(KEYWORDS_FILE, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                weight, pattern = line.split(None, 1)
                rules.append((int(weight), re.compile(pattern, re.I)))
            except (ValueError, re.error) as ex:
                log(f"keywords.txt line {n} skipped ({ex}): {line}")
    if not rules:
        raise ValueError(f"no usable rules in {KEYWORDS_FILE}")
    return rules


def score_paper(paper, rules):
    """Returns (score, hits): the summed weights and [[matched text, weight]]."""
    text = f"{paper['title']}. {paper['abstract']}"
    score, hits = 0, []
    for weight, regex in rules:
        m = regex.search(text)
        if m:
            score += weight
            hits.append([squash(m.group(0)), weight])
    return score, hits


# --------------------------------------------------------------------- run

def load_previous(path):
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["papers"]
    except (OSError, ValueError, KeyError) as ex:
        log(f"no previous papers ({ex})")
        return []


def estimate_seen(paper):
    """Likely first-seen time of a paper stored before "seen" was recorded.

    arXiv announces at 00:00 UTC what was submitted before 18:00 UTC the day
    before; ePrint lists a paper the evening after its submission day, which
    the next morning's run picks up. Weekends are ignored.
    """
    submitted = dt.datetime.fromisoformat(paper["date"])
    late = paper["source"] == "ePrint" or submitted.hour >= 18
    day = submitted.date() + dt.timedelta(days=2 if late else 1)
    return day.strftime("%Y-%m-%dT00:00:00Z")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--previous", help="papers.json from the last run")
    ap.add_argument("--out", default=os.path.join(HERE, "papers.json"))
    args = ap.parse_args()

    rules = load_rules()
    now = now_iso()
    papers = {p["id"]: p for p in load_previous(args.previous)}
    known = len(papers)
    for p in papers.values():
        if "seen" not in p:
            p["seen"] = min(estimate_seen(p), now)

    # One source being down should not lose the other, or the papers we have.
    failed = []
    sources = [("arXiv", fetch_arxiv)]
    if INCLUDE_EPRINT:
        sources.append(("ePrint", fetch_eprint))
    for name, fetch in sources:
        try:
            got = fetch()
            log(f"{name}: {len(got)} papers")
            for p in got:
                p["seen"] = papers[p["id"]]["seen"] if p["id"] in papers else now
                papers[p["id"]] = p
        except Exception as ex:
            log(f"{name} failed: {ex}")
            failed.append(name)
    if len(failed) == len(sources):
        sys.exit("every source failed, keeping the old papers.json")

    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kept = [p for p in papers.values() if p["date"] >= cutoff]
    for p in kept:
        p["score"], p["hits"] = score_paper(p, rules)
    kept.sort(key=lambda p: (p["seen"][:10], p["score"], p["date"]), reverse=True)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"updated": now, "threshold": THRESHOLD,
                   "failed": failed, "papers": kept},
                  f, ensure_ascii=False, separators=(",", ":"))
    passed = sum(1 for p in kept if p["score"] >= THRESHOLD)
    log(f"wrote {len(kept)} papers ({known} known before), "
        f"{passed} at or above threshold {THRESHOLD}")


if __name__ == "__main__":
    main()
