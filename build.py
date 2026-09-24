#!/usr/bin/env python3
"""HEADLINE REPORT — Drudge-style static news aggregator.

Fetches real RSS/Atom feeds, clusters/scores headlines, writes public/index.html.
Never invents headlines or URLs. Deterministic; no LLM calls.
"""

from __future__ import annotations

import html
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

try:
    import feedparser  # noqa: F401
    import requests  # noqa: F401
except ImportError:
    import sys
    sys.stderr.write(
        "Missing deps. Use:  .venv/bin/python build.py\n"
        "Or:                 ./run.sh\n"
        "First-time setup:   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n"
    )
    raise SystemExit(1)

import feedparser
import requests

import config

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Item:
    title: str
    link: str
    source: str
    published: Optional[datetime]
    image: Optional[str] = None
    category: str = ""
    raw_title: str = ""


@dataclass
class Cluster:
    items: list[Item] = field(default_factory=list)
    score: float = 0.0

    @property
    def best(self) -> Item:
        # Prefer non-Google-News link when available; else first by recency
        def key(it: Item):
            is_gn = 1 if "news.google.com" in it.link else 0
            ts = it.published.timestamp() if it.published else 0
            return (is_gn, -ts)

        return sorted(self.items, key=key)[0]

    @property
    def sources(self) -> set[str]:
        return {it.source for it in self.items}

    @property
    def latest(self) -> Optional[datetime]:
        times = [it.published for it in self.items if it.published]
        return max(times) if times else None

    @property
    def image(self) -> Optional[str]:
        for it in self.items:
            if it.image:
                return it.image
        return None


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "from", "by", "with", "as", "is", "are", "was", "were", "be", "been",
    "its", "it", "this", "that", "these", "those", "his", "her", "their",
    "has", "have", "had", "will", "would", "could", "should", "may", "might",
    "after", "before", "over", "under", "into", "about", "than", "then",
    "not", "no", "yes", "new", "says", "say", "said", "amid", "near", "vs",
}


def normalize_title(title: str) -> str:
    t = html.unescape(title or "")
    # Strip Google News " - Source" / " | Source" suffixes
    t = re.sub(r"\s+[\-\|]\s+[A-Za-z0-9 .,'&]+$", "", t)
    t = t.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def title_tokens(title: str) -> frozenset[str]:
    toks = [w for w in normalize_title(title).split() if w not in STOPWORDS and len(w) > 2]
    return frozenset(toks)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def extract_image(entry) -> Optional[str]:
    if getattr(entry, "media_content", None):
        for m in entry.media_content:
            url = m.get("url")
            if url and _looks_like_image(url):
                return url
    if getattr(entry, "media_thumbnail", None):
        for m in entry.media_thumbnail:
            url = m.get("url")
            if url:
                return url
    for enc in getattr(entry, "enclosures", []) or []:
        href = enc.get("href") or enc.get("url")
        typ = (enc.get("type") or "").lower()
        if href and (typ.startswith("image/") or _looks_like_image(href)):
            return href
    # Sometimes images are buried in summary HTML
    summary = entry.get("summary") or entry.get("description") or ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary, re.I)
    if m:
        return m.group(1)
    return None


def _looks_like_image(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")) or "image" in path


def parse_published(entry) -> Optional[datetime]:
    for key in ("published", "updated"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                return datetime(*struct[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def strip_html(s: str) -> str:
    """Remove tags; if the whole title is an <a>...</a>, keep inner text."""
    s = s or ""
    m = re.match(r"(?is)^\s*<a\b[^>]*>(.*?)</a>\s*$", s)
    if m:
        s = m.group(1)
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return s


def clean_display_title(title: str) -> str:
    t = strip_html(html.unescape(title or "")).strip()
    t = re.sub(r"\s+", " ", t)
    # Strip trailing " - Source Name" common in Google News
    t = re.sub(
        r"\s+[\-\|]\s+(AP News|Reuters|BBC News|NPR|The Guardian|New York Times|"
        r"Fox News|CNN|Politico|The Hill|Axios|Bloomberg|CNBC|Washington Post|"
        r"NBC News|CBS News|ABC News|Al Jazeera|Sky News|UPI|The Verge|"
        r"Ars Technica|TechCrunch|WIRED|Wired|NY Post|New York Post|"
        r"Hacker News|Oddity Central)\s*$",
        "",
        t,
        flags=re.I,
    )
    return t.strip()


def fetch_feed(feed_meta: dict) -> tuple[str, list[Item], Optional[str]]:
    """Return (name, items, error_or_None)."""
    name = feed_meta["name"]
    url = feed_meta["url"]
    category = feed_meta.get("category", "")
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
            timeout=config.FETCH_TIMEOUT,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return name, [], f"HTTP {resp.status_code}"
        parsed = feedparser.parse(resp.content)
        items: list[Item] = []
        for entry in parsed.entries[:40]:
            raw = entry.get("title") or ""
            link = entry.get("link") or ""
            if not raw or not link:
                continue
            title = clean_display_title(raw)
            if len(title) < 12:
                continue
            # Drop obvious CNN/Fox legal-disclaimer leftovers if any slip through
            if "dominion voting" in title.lower():
                continue
            items.append(
                Item(
                    title=title,
                    link=link.strip(),
                    source=name,
                    published=parse_published(entry),
                    image=extract_image(entry),
                    category=category,
                    raw_title=raw,
                )
            )
        if not items:
            return name, [], "0 usable entries"
        return name, items, None
    except Exception as exc:
        return name, [], f"{type(exc).__name__}: {exc}"


def fetch_all(feeds: list[dict]) -> tuple[list[Item], list[tuple[str, str]], list[str]]:
    all_items: list[Item] = []
    failed: list[tuple[str, str]] = []
    ok_names: list[str] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = {pool.submit(fetch_feed, f): f for f in feeds}
        for fut in as_completed(futs):
            name, items, err = fut.result()
            if err:
                failed.append((name, err))
                print(f"  FAIL  {name}: {err}", flush=True)
            else:
                ok_names.append(name)
                all_items.extend(items)
                print(f"  OK    {name}: {len(items)} items", flush=True)
    return all_items, failed, sorted(ok_names)


# ---------------------------------------------------------------------------
# Cluster + score
# ---------------------------------------------------------------------------

def cluster_items(items: list[Item], threshold: float = 0.45) -> list[Cluster]:
    """Greedy clustering by title-token Jaccard similarity."""
    # Sort by recency so newer items seed clusters
    def sort_key(it: Item):
        return -(it.published.timestamp() if it.published else 0)

    ordered = sorted(items, key=sort_key)
    clusters: list[Cluster] = []
    centroids: list[frozenset[str]] = []

    for it in ordered:
        toks = title_tokens(it.title)
        if not toks:
            continue
        best_i = -1
        best_sim = 0.0
        for i, cent in enumerate(centroids):
            sim = jaccard(toks, cent)
            if sim > best_sim:
                best_sim = sim
                best_i = i
        if best_i >= 0 and best_sim >= threshold:
            clusters[best_i].items.append(it)
            # expand centroid lightly
            centroids[best_i] = centroids[best_i] | toks
        else:
            clusters.append(Cluster(items=[it]))
            centroids.append(toks)
    return clusters


def keyword_boost(title: str) -> float:
    t = title.lower()
    boost = 0.0
    for kw in config.INTEREST_KEYWORDS:
        # word-boundary-ish for short tokens like "ai"
        if len(kw) <= 3:
            if re.search(rf"(^|[^a-z]){re.escape(kw)}([^a-z]|$)", t):
                boost += 1.2
        elif kw in t:
            boost += 1.0
    return min(boost, 6.0)  # cap



def proper_nouns(title: str) -> frozenset[str]:
    words = re.findall(r"[A-Za-z][A-Za-z\-']+", title)
    props = {w.lower() for w in words if w[0].isupper() and len(w) > 2}
    anchors = {
        "trump", "xi", "putin", "zelensky", "biden", "harris", "netanyahu",
        "weinstein", "nato", "ukraine", "israel", "gaza", "china",
        "russia", "iran", "openai", "meta", "google", "apple", "tesla",
    }
    t = title.lower()
    props |= {a for a in anchors if re.search(rf"(^|[^a-z]){re.escape(a)}([^a-z]|$)", t)}
    return frozenset(props)


def merge_related_clusters(clusters: list[Cluster], sim: float = 0.32) -> list[Cluster]:
    """Merge clusters that share strong proper-noun overlap and decent token similarity."""
    clusters = sorted(clusters, key=lambda c: -(c.latest.timestamp() if c.latest else 0))
    merged: list[Cluster] = []
    used = [False] * len(clusters)
    for i, a in enumerate(clusters):
        if used[i]:
            continue
        cur = Cluster(items=list(a.items))
        at = title_tokens(a.best.title)
        ap = proper_nouns(a.best.title)
        used[i] = True
        for j in range(i + 1, len(clusters)):
            if used[j]:
                continue
            b = clusters[j]
            bt = title_tokens(b.best.title)
            bp = proper_nouns(b.best.title)
            if jaccard(at, bt) >= sim and len(ap & bp) >= 2:
                cur.items.extend(b.items)
                at = at | bt
                ap = ap | bp
                used[j] = True
        merged.append(cur)
    return merged


def score_cluster(cluster: Cluster, now: datetime) -> float:
    n_sources = len(cluster.sources)
    # Multi-outlet coverage is the strongest signal
    coverage = (n_sources - 1) * 4.5

    latest = cluster.latest
    if latest:
        age_hours = max(0.0, (now - latest).total_seconds() / 3600.0)
        # Fresh stories preferred; decay over ~36h
        recency = max(0.0, 12.0 - age_hours * (12.0 / 36.0))
    else:
        recency = 2.0  # unknown age — mild penalty vs fresh

    boost = keyword_boost(cluster.best.title)
    # Slight preference for having an image (lead candidate)
    image_bonus = 0.8 if cluster.image else 0.0
    # Prefer political/wire categories slightly for lead
    cat_bonus = 0.0
    cats = {it.category for it in cluster.items}
    if cats & {"politics", "wire", "us", "world"}:
        cat_bonus = 0.5

    return coverage + recency + boost + image_bonus + cat_bonus


def dedupe_clusters(clusters: list[Cluster]) -> list[Cluster]:
    """Second-pass: merge/drop near-identical display titles across clusters."""
    kept: list[Cluster] = []
    seen_norms: list[frozenset[str]] = []
    for c in sorted(clusters, key=lambda x: -x.score):
        toks = title_tokens(c.best.title)
        if any(jaccard(toks, s) >= 0.7 for s in seen_norms):
            continue
        # Also exact normalized title
        norm = normalize_title(c.best.title)
        if any(normalize_title(k.best.title) == norm for k in kept):
            continue
        kept.append(c)
        seen_norms.append(toks)
    return kept


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def esc_text(s: str) -> str:
    return html.escape(s, quote=False)


def esc_attr(s: str) -> str:
    return html.escape(s, quote=True)


def format_headline_link(item: Item, *, red: bool = False, big: bool = False, all_caps: bool = False) -> str:
    text = item.title
    if all_caps:
        text = text.upper()
    style_bits = []
    if red:
        style_bits.append("color:#c00")
    if big:
        style_bits.append("font-size:36px")
        style_bits.append("font-weight:bold")
        style_bits.append("line-height:1.15")
    style = f' style="{";".join(style_bits)}"' if style_bits else ""
    cls = ' class="red"' if red and not style_bits else ""
    return (
        f'<a href="{esc_attr(item.link)}" target="_blank" rel="noopener noreferrer"'
        f'{cls}{style}>{esc_text(text)}</a>'
    )


def render_html(
    teasers: list[Cluster],
    lead: Cluster,
    columns: list[list[Cluster]],
    ok_sources: list[str],
    updated: datetime,
) -> str:
    updated_str = updated.strftime("%a %b %d %Y %I:%M:%S %p").upper().replace(" 0", " ")
    # Drudge shows like: Wed Sep 23 2026 10:06:32 PM ET — keep simple
    updated_display = updated.strftime("%a %b %-d %Y %-I:%M:%S %p ET")

    lead_item = lead.best
    lead_img = lead.image

    # Collect red emphasis targets (cluster ids by object identity of best link)
    red_links = set()
    candidates = teasers + [lead] + [c for col in columns for c in col]
    # Skip lead itself for the count of "few red" — lead is always red
    for c in candidates:
        if c is lead:
            continue
        if len(red_links) >= config.RED_EMPHASIS_COUNT:
            break
        # Prefer multi-source or strongly keyworded stories for splash red
        if len(c.sources) >= 3 or (len(c.sources) >= 2 and c.score >= 14) or keyword_boost(c.best.title) >= 3.5:
            red_links.add(c.best.link)

    parts: list[str] = []
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache">
<title>{esc_text(config.SITE_NAME)}</title>
<style>
  html, body {{
    margin: 0;
    padding: 0;
    background: #ffffff;
    color: #000000;
    font-family: "Courier New", Courier, "Liberation Mono", monospace;
    font-size: 14px;
  }}
  a {{
    color: #000000;
    text-decoration: underline;
  }}
  a:visited {{ color: #000000; }}
  a.red, a[style*="color:#c00"] {{ color: #cc0000 !important; }}
  .wrap {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 8px 10px 40px;
  }}
  .topbar {{
    text-align: center;
    padding: 6px 0 2px;
    font-size: 12px;
  }}
  .sitename {{
    text-align: center;
    font-size: 18px;
    font-weight: bold;
    letter-spacing: 3px;
    margin: 2px 0 2px;
  }}
  .updated {{
    text-align: center;
    font-size: 11px;
    margin: 0 0 10px;
    color: #333;
  }}
  .teasers {{
    text-align: center;
    margin: 8px auto 14px;
    max-width: 720px;
    line-height: 1.55;
  }}
  .teasers div {{
    margin: 3px 0;
  }}
  .lead-block {{
    text-align: center;
    margin: 10px auto 18px;
    max-width: 780px;
  }}
  .lead-block .siren {{
    color: #cc0000;
    font-size: 13px;
    letter-spacing: 3px;
    margin-bottom: 6px;
  }}
  .lead-headline {{
    margin: 6px 0 10px;
  }}
  .lead-headline a {{
    color: #cc0000 !important;
    font-size: 36px;
    font-weight: bold;
    line-height: 1.12;
    text-decoration: underline;
  }}
  .lead-img {{
    margin: 8px auto 4px;
  }}
  .lead-img img {{
    max-width: 420px;
    width: 100%;
    height: auto;
    border: 1px solid #000;
  }}
  .sources-note {{
    font-size: 11px;
    color: #444;
    margin-top: 4px;
  }}
  hr.main {{
    border: 0;
    border-top: 1px solid #000;
    margin: 14px 0;
  }}
  .cols {{
    display: flex;
    gap: 18px;
    align-items: flex-start;
    justify-content: space-between;
  }}
  .col {{
    flex: 1 1 0;
    min-width: 0;
    line-height: 1.45;
  }}
  .col .item {{
    margin: 0 0 9px;
    word-wrap: break-word;
  }}
  .col hr {{
    border: 0;
    border-top: 1px solid #999;
    margin: 10px 0;
  }}
  .footer {{
    margin-top: 28px;
    text-align: center;
    font-size: 11px;
    line-height: 1.7;
  }}
  .footer a {{ margin: 0 4px; }}
  @media (max-width: 800px) {{
    .cols {{ flex-direction: column; }}
    .lead-headline a {{ font-size: 22px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">{esc_text(config.SITE_TAGLINE)}</div>
  <div class="sitename">{esc_text(config.SITE_NAME)}</div>
  <div class="updated">updated {esc_text(updated_display)}</div>

  <div class="teasers">
""")

    for c in teasers:
        red = c.best.link in red_links
        parts.append(f"    <div>{format_headline_link(c.best, red=red)}</div>\n")

    parts.append("  </div>\n")

    # Lead
    parts.append('  <div class="lead-block">\n')
    if keyword_boost(lead_item.title) >= 2 or len(lead.sources) >= 3:
        parts.append('    <div class="siren">***&nbsp;&nbsp;***</div>\n')
    parts.append(f'    <div class="lead-headline">{format_headline_link(lead_item, red=True, all_caps=True)}</div>\n')
    if lead_img:
        parts.append(
            f'    <div class="lead-img"><a href="{esc_attr(lead_item.link)}" target="_blank" rel="noopener noreferrer">'
            f'<img src="{esc_attr(lead_img)}" alt="" referrerpolicy="no-referrer"></a></div>\n'
        )
    if len(lead.sources) > 1:
        srcs = ", ".join(sorted(_short_source(s) for s in lead.sources))
        parts.append(f'    <div class="sources-note">also: {esc_text(srcs)}</div>\n')
    parts.append("  </div>\n")

    parts.append('  <hr class="main">\n')
    parts.append('  <div class="cols">\n')

    for col in columns:
        parts.append('    <div class="col">\n')
        for i, c in enumerate(col):
            if i > 0 and i % 5 == 0:
                parts.append("      <hr>\n")
            red = c.best.link in red_links
            parts.append(f'      <div class="item">{format_headline_link(c.best, red=red)}</div>\n')
        parts.append("    </div>\n")

    parts.append("  </div>\n")

    parts.append('  <hr class="main">\n')
    parts.append('  <div class="footer">\n')
    parts.append(f"    <div><b>{esc_text(config.SITE_NAME)}</b> — links open original articles</div>\n")
    parts.append("    <div>\n")
    for i, src in enumerate(ok_sources):
        # Source "homes" — best-effort root from feed name mapping
        home = _source_home(src)
        if home:
            parts.append(f'      <a href="{esc_attr(home)}" target="_blank" rel="noopener noreferrer">{esc_text(src)}</a>')
        else:
            parts.append(f"      <span>{esc_text(src)}</span>")
        if i < len(ok_sources) - 1:
            parts.append(" &nbsp;\n")
    parts.append("\n    </div>\n")
    parts.append("  </div>\n")
    parts.append("</div>\n</body>\n</html>\n")
    return "".join(parts)


def _short_source(name: str) -> str:
    return (
        name.replace(" (via Google News)", "")
        .replace("Bloomberg Politics", "Bloomberg")
        .replace("Bloomberg Markets", "Bloomberg")
        .replace("Politico Picks", "Politico")
        .replace("Fox Politics", "Fox")
        .replace("WaPo Politics", "WaPo")
        .replace("Guardian World", "Guardian")
        .replace("BBC World", "BBC")
        .replace("NYT World", "NYT")
        .replace("CNN World", "CNN")
        .replace("CNN US", "CNN")
    )


def _source_home(name: str) -> Optional[str]:
    mapping = {
        "AP (via Google News)": "https://apnews.com/",
        "Reuters (via Google News)": "https://www.reuters.com/",
        "BBC": "https://www.bbc.com/news",
        "BBC World": "https://www.bbc.com/news/world",
        "NPR": "https://www.npr.org/",
        "The Guardian": "https://www.theguardian.com/us",
        "Guardian World": "https://www.theguardian.com/world",
        "NYT": "https://www.nytimes.com/",
        "NYT World": "https://www.nytimes.com/section/world",
        "Fox News": "https://www.foxnews.com/",
        "Fox Politics": "https://www.foxnews.com/politics",
        "CNN US": "https://www.cnn.com/us",
        "CNN World": "https://www.cnn.com/world",
        "Politico": "https://www.politico.com/",
        "Politico Picks": "https://www.politico.com/",
        "The Hill": "https://thehill.com/",
        "Axios": "https://www.axios.com/",
        "Bloomberg Politics": "https://www.bloomberg.com/politics",
        "Bloomberg Markets": "https://www.bloomberg.com/markets",
        "CNBC": "https://www.cnbc.com/",
        "Washington Post": "https://www.washingtonpost.com/",
        "WaPo Politics": "https://www.washingtonpost.com/politics/",
        "NBC News": "https://www.nbcnews.com/",
        "CBS News": "https://www.cbsnews.com/",
        "ABC News": "https://abcnews.go.com/",
        "Al Jazeera": "https://www.aljazeera.com/",
        "Sky News": "https://news.sky.com/",
        "UPI": "https://www.upi.com/",
        "Hacker News": "https://news.ycombinator.com/",
        "Ars Technica": "https://arstechnica.com/",
        "The Verge": "https://www.theverge.com/",
        "TechCrunch": "https://techcrunch.com/",
        "Wired": "https://www.wired.com/",
        "NY Post": "https://nypost.com/",
        "Oddity Central": "https://www.odditycentral.com/",
    }
    return mapping.get(name)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def diversify(clusters: list[Cluster], limit: int, near: float = 0.22) -> list[Cluster]:
    """Greedy pick by score while suppressing near-duplicate angles of the same story."""
    picked: list[Cluster] = []
    picked_toks: list[frozenset[str]] = []
    picked_props: list[frozenset[str]] = []
    # Cap how many stories can share a strong 2-entity pair (e.g. trump+xi)
    pair_counts: dict[frozenset[str], int] = {}
    PAIR_CAP = 1

    for c in clusters:
        if len(picked) >= limit:
            break
        toks = title_tokens(c.best.title)
        props = proper_nouns(c.best.title)
        if any(jaccard(toks, pt) >= near for pt in picked_toks):
            continue
        # Limit repeat coverage of the same major entity pair
        significant = frozenset(
            p for p in props
            if p in {
                "trump", "xi", "putin", "zelensky", "china", "russia", "iran",
                "israel", "gaza", "ukraine", "weinstein", "openai", "meta",
                "lincoln", "suicide", "navy", "des", "moines",
            }
        )
        skip = False
        if len(significant) >= 2:
            # count pairs within significant set
            sig = sorted(significant)
            for i in range(len(sig)):
                for j in range(i + 1, len(sig)):
                    pair = frozenset((sig[i], sig[j]))
                    if pair_counts.get(pair, 0) >= PAIR_CAP:
                        skip = True
                        break
                if skip:
                    break
        if skip:
            continue
        picked.append(c)
        picked_toks.append(toks)
        picked_props.append(props)
        if len(significant) >= 2:
            sig = sorted(significant)
            for i in range(len(sig)):
                for j in range(i + 1, len(sig)):
                    pair = frozenset((sig[i], sig[j]))
                    pair_counts[pair] = pair_counts.get(pair, 0) + 1
    return picked


def distribute_columns(clusters: list[Cluster], n_cols: int, per_col: int) -> list[list[Cluster]]:
    """Round-robin into columns so each has content; pad by taking more if needed."""
    total_needed = n_cols * per_col
    pool = clusters[:total_needed]
    # If short, use what we have but keep columns non-empty when possible
    cols: list[list[Cluster]] = [[] for _ in range(n_cols)]
    for i, c in enumerate(pool):
        cols[i % n_cols].append(c)
    # Balance: if some columns empty, rebalance
    if any(len(c) == 0 for c in cols) and pool:
        cols = [[] for _ in range(n_cols)]
        for i, c in enumerate(pool):
            cols[i % n_cols].append(c)
    return cols


def main() -> int:
    print(f"Building {config.SITE_NAME}…", flush=True)
    now = datetime.now(tz=ET)

    items, failed, ok_names = fetch_all(config.FEEDS)
    print(f"Fetched {len(items)} raw headlines from {len(ok_names)} feeds "
          f"({len(failed)} failed)", flush=True)

    if len(items) < 30:
        print("ERROR: too few headlines to build a page.", file=sys.stderr)
        return 1

    # Keep build snappy: prefer freshest items when volume is huge
    def _recency_key(it: Item):
        return -(it.published.timestamp() if it.published else 0)
    if len(items) > 500:
        items = sorted(items, key=_recency_key)[:500]
    clusters = cluster_items(items, threshold=0.40)
    clusters = merge_related_clusters(clusters)
    for c in clusters:
        c.score = score_cluster(c, now.astimezone(timezone.utc))
    clusters = dedupe_clusters(clusters)
    clusters.sort(key=lambda c: -c.score)

    if not clusters:
        print("ERROR: no clusters.", file=sys.stderr)
        return 1

    # Prefer a lead that has an image among top few if scores are close
    lead = clusters[0]
    for c in clusters[:8]:
        if c.image and c.score >= lead.score * 0.85:
            lead = c
            break

    remaining = [c for c in clusters if c is not lead]
    needed = config.TOP_TEASER_COUNT + config.COLUMN_COUNT * config.PER_COLUMN + 10
    remaining = diversify(remaining, needed)
    teasers = remaining[: config.TOP_TEASER_COUNT]
    rest = remaining[config.TOP_TEASER_COUNT :]
    columns = distribute_columns(rest, config.COLUMN_COUNT, config.PER_COLUMN)

    # Ensure no empty columns — pull from leftovers if needed
    leftovers = rest[config.COLUMN_COUNT * config.PER_COLUMN :]
    for col in columns:
        while len(col) == 0 and leftovers:
            col.append(leftovers.pop(0))

    html_out = render_html(teasers, lead, columns, ok_names, now)
    out_path = ROOT / config.OUTPUT_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8")

    n_on_page = 1 + len(teasers) + sum(len(c) for c in columns)
    print(f"Wrote {out_path} ({n_on_page} headlines on page)", flush=True)
    print(f"LEAD: {lead.best.title}", flush=True)
    print(f"Lead sources ({len(lead.sources)}): {', '.join(sorted(lead.sources))}", flush=True)
    if failed:
        print("Failed feeds:", flush=True)
        for n, e in failed:
            print(f"  - {n}: {e}", flush=True)

    # Write a small build report for the parent agent
    report = ROOT / "build-report.txt"
    lines = [
        f"updated={now.isoformat()}",
        f"ok_feeds={len(ok_names)}",
        f"failed_feeds={len(failed)}",
        f"raw_items={len(items)}",
        f"clusters={len(clusters)}",
        f"on_page={n_on_page}",
        f"lead={lead.best.title}",
        f"lead_link={lead.best.link}",
        f"lead_image={lead.image or ''}",
        "OK_FEEDS:",
        *[f"  {n}" for n in ok_names],
        "FAILED_FEEDS:",
        *[f"  {n}: {e}" for n, e in failed],
        "COLUMN_COUNTS:",
        *[f"  col{i}: {len(col)}" for i, col in enumerate(columns)],
    ]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
