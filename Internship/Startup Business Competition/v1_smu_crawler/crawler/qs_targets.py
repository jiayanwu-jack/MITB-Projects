"""
Generate the target university list from the QS World University Rankings.

Two data sources, both used politely (identifying User-Agent, rate limit, on-disk cache):

  1. QS ranking order  - https://www.topuniversities.com/world-university-rankings
     The page is JS-rendered; the data comes from QS's JSON endpoint
     (`/rankings/endpoint?nid=...`). We read only factual identifiers -
     rank, name, country, city, and the QS profile path. We deliberately do NOT
     store QS's proprietary indicator scores.
  2. Official website - Wikidata property P856 ("official website", CC0 data).
     QS does not publish the institution's own domain, and we refuse to fabricate
     one, so we resolve it from Wikidata. Unresolved rows are written with an empty
     portal_url and verified=false for a human to complete.

  !!! Licensing / ToS: the QS ranking is QS Quacquarelli Symonds' proprietary dataset.
      Using its ordering may be subject to QS's terms. This helper is for building an
      internal outreach target list; confirm permitted use before any redistribution.

Public API:
    fetch_qs_rankings(top_n) -> list[dict]
    resolve_official_site(name, country=None) -> str | None
    build_target_csv(top_n, out_path, resolve_domains=True) -> (path, stats)
"""
from __future__ import annotations

import csv
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .fetch import USER_AGENT

QS_ENDPOINT = "https://www.topuniversities.com/rankings/endpoint"
QS_WUR_NID = "3897789"                    # QS World University Rankings node id
QS_BASE = "https://www.topuniversities.com"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_CACHE = Path(__file__).resolve().parents[1] / "targets" / ".domain_cache.json"


def _get_json(url: str, *, timeout: float = 30.0, retries: int = 2) -> dict | list:
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                              "X-Requested-With": "XMLHttpRequest"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:                       # noqa: BLE001 - retry any transient
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request failed: {url} ({last})")


def _clean_name(title: str) -> str:
    """'Massachusetts Institute of Technology (MIT)' -> without the trailing abbrev."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()


# --------------------------------------------------------------------------- #
# 1. QS ranking order
# --------------------------------------------------------------------------- #
def fetch_qs_rankings(top_n: int = 200, *, nid: str = QS_WUR_NID,
                      delay: float = 1.0, page_size: int = 100) -> list[dict]:
    """Return up to `top_n` rows: {rank, rank_display, name, country, city, qs_profile_url}."""
    rows: list[dict] = []
    page = 0
    while len(rows) < top_n:
        params = {
            "nid": nid, "page": page, "items_per_page": min(page_size, top_n),
            "tab": "indicators", "region": "", "countries": "", "cities": "",
            "search": "", "star": "", "sort_by": "rank", "order_by": "asc",
        }
        data = _get_json(f"{QS_ENDPOINT}?{urllib.parse.urlencode(params)}")
        nodes = data.get("score_nodes") if isinstance(data, dict) else None
        if not nodes:
            break
        for n in nodes:
            title = (n.get("title") or "").strip()
            if not title:
                continue
            rows.append({
                "rank": n.get("rank"),
                "rank_display": (n.get("rank_display") or str(n.get("rank") or "")).strip(),
                "name": _clean_name(title),
                "country": (n.get("country") or "").strip(),
                "city": (n.get("city") or "").strip(),
                "qs_profile_url": QS_BASE + n.get("path", "") if n.get("path") else "",
            })
            if len(rows) >= top_n:
                break
        total_pages = int(data.get("total_pages") or 1) if isinstance(data, dict) else 1
        page += 1
        if page >= total_pages:
            break
        time.sleep(delay)
    return rows[:top_n]


# --------------------------------------------------------------------------- #
# 2. Official website via Wikidata (P856)
# --------------------------------------------------------------------------- #
def _load_cache() -> dict:
    if _CACHE.exists():
        try:
            return json.loads(_CACHE.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), "utf-8")


def resolve_official_site(name: str, country: str | None = None, *,
                          cache: dict | None = None, delay: float = 0.5) -> str | None:
    """Look up the institution's official website (Wikidata P856). None if not found."""
    key = name.lower().strip()
    if cache is not None and key in cache:
        return cache[key] or None

    # Search by NAME first (best Wikidata entity match); only if that yields nothing
    # fall back to name+country as a disambiguator. Appending the country to the query
    # up front tends to break the entity match.
    queries = [name]
    if country:
        queries.append(f"{name} {country}")

    result = None
    for query in queries:
        s = _get_json(f"{WIKIDATA_API}?" + urllib.parse.urlencode({
            "action": "wbsearchentities", "format": "json", "language": "en",
            "type": "item", "limit": 3, "search": query}))
        for hit in (s.get("search") or []):
            qid = hit.get("id")
            if not qid:
                continue
            time.sleep(delay)
            c = _get_json(f"{WIKIDATA_API}?" + urllib.parse.urlencode({
                "action": "wbgetclaims", "format": "json", "property": "P856", "entity": qid}))
            claims = c.get("claims", {}).get("P856", [])
            if claims:
                try:
                    result = claims[0]["mainsnak"]["datavalue"]["value"]
                    break
                except (KeyError, IndexError, TypeError):
                    continue
        if result:
            break
    if cache is not None:
        cache[key] = result or ""
    return result


# --------------------------------------------------------------------------- #
# 3. Build the target CSV
# --------------------------------------------------------------------------- #
FIELDS = ["id", "name", "country", "qs_rank", "portal_url", "qs_profile_url", "verified"]


def build_target_csv(top_n: int, out_path: str | Path, *, resolve_domains: bool = True,
                     delay: float = 1.0, progress=None) -> tuple[Path, dict]:
    """Write a crawler-compatible universities CSV from the QS top-N. Returns (path, stats)."""
    ranking = fetch_qs_rankings(top_n, delay=delay)
    cache = _load_cache() if resolve_domains else None
    resolved = 0

    rows = []
    for i, u in enumerate(ranking, 1):
        portal = ""
        if resolve_domains:
            try:
                portal = resolve_official_site(u["name"], u["country"], cache=cache) or ""
            except Exception:
                portal = ""
            if portal:
                resolved += 1
            time.sleep(delay)
        rows.append({
            "id": f"qs{i:04d}",
            "name": u["name"],
            "country": u["country"],
            "qs_rank": u["rank_display"],
            "portal_url": portal,
            "qs_profile_url": u["qs_profile_url"],
            "verified": "false",
        })
        if progress:
            progress(i, len(ranking), u["name"], portal)

    if cache is not None:
        _save_cache(cache)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    stats = {"requested": top_n, "fetched": len(rows),
             "domains_resolved": resolved if resolve_domains else None,
             "unresolved": (len(rows) - resolved) if resolve_domains else None}
    return out_path, stats
