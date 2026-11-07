#!/usr/bin/env python3
"""M0 coverage probe: how many target employers are reachable via legitimate sources?

For each company in companies.csv it:
  1. Probes public ATS job-board APIs (Tier 1) with slug guesses.
  2. Fetches likely career pages (robots.txt respected) and fingerprints the ATS
     from the final URL / HTML (Tier 2 India ATS, or enterprise ATS).
  3. Counts schema.org JobPosting JSON-LD blocks on the career page (Tier 3).

Outputs results.csv and summary.md. Standard library only.

Usage:
  python3 probe.py                       # all companies
  python3 probe.py --limit 10            # first 10, for a quick check
  python3 probe.py --only razorpay.com,cred.club
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import urlparse

USER_AGENT = "VerifiedJobsBot/0.1 (M0 coverage research; contact via github.com/suhail-070/verified-jobs-engine)"
TIMEOUT = 15
PER_HOST_DELAY = 1.0  # seconds between requests to the same host
MAX_BYTES = 3_000_000

INDIA_RE = re.compile(
    r"\b(india|bengaluru|bangalore|mumbai|pune|hyderabad|chennai|gurugram|gurgaon|noida|"
    r"delhi|kolkata|ahmedabad|jaipur|kochi|coimbatore|indore|chandigarh|thiruvananthapuram|"
    r"trivandrum|vadodara|nagpur|mysuru|mysore|bhubaneswar|lucknow)\b",
    re.I,
)

# ATS API probes: name -> (url template, function(json) -> (job_count, india_count) or None if not a real board)
def _gh(d):
    jobs = d.get("jobs") if isinstance(d, dict) else None
    if jobs is None:
        return None
    return len(jobs), sum(1 for j in jobs if INDIA_RE.search(json.dumps(j.get("location", ""))))

def _lever(d):
    if not isinstance(d, list):
        return None
    return len(d), sum(1 for j in d if INDIA_RE.search(json.dumps(j.get("categories", {}))))

def _ashby(d):
    jobs = d.get("jobs") if isinstance(d, dict) else None
    if jobs is None:
        return None
    return len(jobs), sum(
        1 for j in jobs
        if INDIA_RE.search(json.dumps([j.get("location"), j.get("secondaryLocations"), j.get("address")]))
    )

def _sr(d):
    if not isinstance(d, dict) or "content" not in d:
        return None
    total = d.get("totalFound", 0)
    if not total:  # SmartRecruiters may answer 200 with zero results for unknown ids
        return None
    return total, sum(1 for j in d["content"] if INDIA_RE.search(json.dumps(j.get("location", {}))))

def _workable(d):
    jobs = d.get("jobs") if isinstance(d, dict) else None
    if jobs is None:
        return None
    return len(jobs), sum(1 for j in jobs if INDIA_RE.search(json.dumps([j.get("city"), j.get("country"), j.get("locations")])))

ATS_APIS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", _gh),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json", _lever),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{slug}", _ashby),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100", _sr),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{slug}", _workable),
}

# Fingerprints seen in career page URLs/HTML. tier: 1 public API, 2 India ATS, "E" enterprise (ToS review needed)
FINGERPRINTS = [
    ("greenhouse", 1, r"boards\.greenhouse\.io|job-boards\.greenhouse\.io|grnh\.se"),
    ("lever", 1, r"jobs\.lever\.co"),
    ("ashby", 1, r"jobs\.ashbyhq\.com"),
    ("smartrecruiters", 1, r"jobs\.smartrecruiters\.com|careers\.smartrecruiters\.com"),
    ("workable", 1, r"apply\.workable\.com"),
    ("darwinbox", 2, r"darwinbox\.(in|com)"),
    ("keka", 2, r"\.keka\.com"),
    ("zoho_recruit", 2, r"zohorecruit\.(com|in)|recruit\.zoho"),
    ("freshteam", 2, r"freshteam\.com"),
    ("peoplestrong", 2, r"peoplestrong\.com"),
    ("ripplehire", 2, r"ripplehire\.com"),
    ("turbohire", 2, r"turbohire\.co"),
    ("mynexthire", 2, r"mynexthire\.com"),
    ("superset", 2, r"joinsuperset\.com"),
    ("instahyre", 2, r"instahyre\.com"),
    ("workday", "E", r"myworkdayjobs\.com|myworkdaysite\.com"),
    ("successfactors", "E", r"successfactors\.(com|eu)|jobs\.sap\.com|career\d*\.successfactors"),
    ("oracle_taleo", "E", r"taleo\.net|oraclecloud\.com/hcmUI"),
    ("icims", "E", r"icims\.com"),
    ("eightfold", "E", r"eightfold\.ai"),
    ("phenom", "E", r"phenompeople\.com|cdn\.phenompeople"),
    ("avature", "E", r"avature\.net"),
]

JSONLD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.I | re.S)

_host_locks: dict[str, threading.Lock] = {}
_host_last: dict[str, float] = {}
_host_guard = threading.Lock()
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_ctx = ssl.create_default_context()


def _throttle(host: str) -> threading.Lock:
    with _host_guard:
        lock = _host_locks.setdefault(host, threading.Lock())
    lock.acquire()
    wait = PER_HOST_DELAY - (time.monotonic() - _host_last.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    return lock


def fetch(url: str) -> tuple[int, str, str]:
    """Return (status, final_url, body). status 0 = network error."""
    host = urlparse(url).netloc
    lock = _throttle(host)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx) as r:
            body = r.read(MAX_BYTES).decode(r.headers.get_content_charset() or "utf-8", "replace")
            return r.status, r.geturl(), body
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception as e:  # DNS, TLS, timeout, proxy denial
        return 0, url, f"{type(e).__name__}: {e}"
    finally:
        _host_last[host] = time.monotonic()
        lock.release()


def robots_allows(url: str) -> bool:
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        status, _, body = fetch(base + "/robots.txt")
        if status == 200:
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(body.splitlines())
            _robots[base] = rp
        else:
            _robots[base] = None  # no robots.txt (or unreachable) -> allowed
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def slugs_for(name: str, domain: str) -> list[str]:
    root = domain.split(".")[0]
    base = re.sub(r"[^a-z0-9 ]", "", name.lower())
    cands = [root, base.replace(" ", ""), base.replace(" ", "-")]
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def count_jobposting(html: str) -> int:
    n = 0
    for block in JSONLD_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                t = x.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    n += 1
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    return n


@dataclass
class Result:
    name: str
    domain: str
    category: str
    tier: str = "unreachable"          # "1", "2", "3", "E", "unreachable"
    ats: str = ""
    ats_slug: str = ""
    jobs_total: int | None = None
    jobs_india: int | None = None
    careers_url: str = ""
    jsonld_jobpostings: int = 0
    robots_blocked: bool = False
    errors: list[str] = field(default_factory=list)


def probe_company(row: dict) -> Result:
    r = Result(row["name"], row["domain"], row["category"])

    # 1. Tier 1 ATS APIs
    for slug in slugs_for(row["name"], row["domain"]):
        for ats, (tmpl, parse) in ATS_APIS.items():
            status, _, body = fetch(tmpl.format(slug=slug))
            if status == 0:
                r.errors.append(f"{ats}: {body[:80]}")
                continue
            if status != 200:
                continue
            try:
                parsed = parse(json.loads(body))
            except ValueError:
                parsed = None
            if parsed:
                r.tier, r.ats, r.ats_slug = "1", ats, slug
                r.jobs_total, r.jobs_india = parsed
                break
        if r.tier == "1":
            break

    # 2/3. Career page fingerprint + JSON-LD
    d = row["domain"]
    for url in (f"https://{d}/careers", f"https://careers.{d}/", f"https://{d}/jobs", f"https://jobs.{d}/"):
        if not robots_allows(url):
            r.robots_blocked = True
            continue
        status, final, body = fetch(url)
        if status != 200:
            if status == 0:
                r.errors.append(f"page {url}: {body[:80]}")
            continue
        r.careers_url = final
        haystack = final + "\n" + body
        for ats, tier, pat in FINGERPRINTS:
            if re.search(pat, haystack, re.I):
                if r.tier == "unreachable" or (r.tier in ("E", "3") and tier in (1, 2)):
                    r.tier, r.ats = str(tier), ats
                elif not r.ats:
                    r.ats = ats
                break
        r.jsonld_jobpostings = count_jobposting(body)
        if r.jsonld_jobpostings and r.tier in ("unreachable", "E"):
            r.tier = "3"
        break  # first career page that answers is enough
    return r


def write_outputs(results: list[Result], outdir: Path) -> None:
    with open(outdir / "results.csv", "w", newline="") as f:
        cols = [k for k in asdict(results[0]) if k != "errors"] + ["errors"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in results:
            row = asdict(r)
            row["errors"] = " | ".join(r.errors[:3])
            w.writerow(row)

    n = len(results)
    by = lambda t: [r for r in results if r.tier == t]
    reachable = by("1") + by("2") + by("3")
    net_errors = [r for r in results if r.tier == "unreachable" and r.errors]
    pct = 100 * len(reachable) / n if n else 0
    verdict = "GO" if pct >= 40 else "NO-GO (rethink sourcing)"
    lines = [
        "# M0 coverage summary", "",
        f"Companies probed: **{n}**", "",
        "| Tier | Meaning | Count | % |", "|---|---|---|---|",
    ]
    for t, label in [("1", "Public ATS API"), ("2", "India ATS (ToS review needed)"),
                     ("3", "JSON-LD on career page"), ("E", "Enterprise ATS (ToS review needed)"),
                     ("unreachable", "Nothing found")]:
        c = len(by(t))
        lines.append(f"| {t} | {label} | {c} | {100*c/n:.0f}% |")
    lines += [
        "", f"**Reachable (T1+T2+T3): {len(reachable)}/{n} = {pct:.0f}% → {verdict}** (threshold 40%)", "",
        "Note: T2 counts as reachable only if that ATS's terms allow indexing — verify before M1.", "",
    ]
    if net_errors:
        lines.append(f"⚠️ {len(net_errors)} 'unreachable' companies had network errors; "
                     "results are not trustworthy until those are re-run.")
    cats: dict[str, list[Result]] = {}
    for r in results:
        cats.setdefault(r.category, []).append(r)
    lines += ["", "## Reachability by category", "",
              "| Category | Companies | Reachable (T1–T3) | % |", "|---|---|---|---|"]
    for cat, rs in sorted(cats.items(), key=lambda x: -len(x[1])):
        ok = sum(1 for r in rs if r.tier in ("1", "2", "3"))
        lines.append(f"| {cat} | {len(rs)} | {ok} | {100*ok/len(rs):.0f}% |")
    t1 = sorted(by("1"), key=lambda r: -(r.jobs_india or 0))
    if t1:
        lines += ["", "## Tier 1 boards", "", "| Company | ATS | Jobs | India jobs |", "|---|---|---|---|"]
        lines += [f"| {r.name} | {r.ats} | {r.jobs_total} | {r.jobs_india} |" for r in t1]
    ats_counts: dict[str, int] = {}
    for r in results:
        if r.ats:
            ats_counts[r.ats] = ats_counts.get(r.ats, 0) + 1
    if ats_counts:
        lines += ["", "## ATS seen", "", "| ATS | Companies |", "|---|---|"]
        lines += [f"| {a} | {c} |" for a, c in sorted(ats_counts.items(), key=lambda x: -x[1])]
    (outdir / "summary.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    here = Path(__file__).parent
    ap.add_argument("--input", default=here / "companies.csv", type=Path)
    ap.add_argument("--outdir", default=here, type=Path)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="comma-separated domains")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.input)))
    if args.only:
        keep = set(args.only.split(","))
        rows = [r for r in rows if r["domain"] in keep]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("no companies selected", file=sys.stderr)
        return 1

    results: list[Result] = []
    with ThreadPoolExecutor(args.workers) as ex:
        for i, res in enumerate(ex.map(probe_company, rows), 1):
            results.append(res)
            print(f"[{i}/{len(rows)}] {res.name:30} tier={res.tier:11} ats={res.ats or '-'}"
                  + (f" jobs={res.jobs_total} india={res.jobs_india}" if res.jobs_total is not None else ""),
                  flush=True)

    args.outdir.mkdir(parents=True, exist_ok=True)
    write_outputs(results, args.outdir)
    print(f"\nWrote {args.outdir/'results.csv'} and {args.outdir/'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
