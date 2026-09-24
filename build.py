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


def _clean_img_url(url: str) -> Optional[str]:
    """Normalize a candidate image URL; reject trackers / empties."""
    if not url:
        return None
    url = html.unescape(url.strip())
    if not url.startswith(("http://", "https://")):
        return None
    low = url.lower()
    # Skip 1x1 trackers, sprites, icons, logos used as filler
    for bad in (
        "pixel", "tracking", "spacer", "1x1", "blank.gif",
        "/icon", "favicon", "logo-rss", "rss-logo", "gravatar",
        "/64x64/", "/96x96/", "/100x100/", "/128x128/", "/150x150/",
    ):
        if bad in low:
            return None
    if not _looks_like_image(url):
        return None
    return url


def extract_image(entry) -> Optional[str]:
    """Pull a real image URL from common RSS/Atom media fields + HTML bodies."""
    # media:content (may be a list of dicts; some feeds emit empty {})
    if getattr(entry, "media_content", None):
        for m in entry.media_content:
            if not isinstance(m, dict):
                continue
            url = m.get("url") or m.get("href")
            typ = (m.get("type") or "").lower()
            medium = (m.get("medium") or "").lower()
            if url and (medium == "image" or typ.startswith("image/") or _looks_like_image(url)):
                cleaned = _clean_img_url(url)
                if cleaned:
                    return cleaned
    # media:thumbnail
    if getattr(entry, "media_thumbnail", None):
        for m in entry.media_thumbnail:
            if not isinstance(m, dict):
                continue
            cleaned = _clean_img_url(m.get("url") or m.get("href") or "")
            if cleaned:
                return cleaned
    # enclosures
    for enc in getattr(entry, "enclosures", []) or []:
        href = enc.get("href") or enc.get("url")
        typ = (enc.get("type") or "").lower()
        if href and (typ.startswith("image/") or _looks_like_image(href)):
            cleaned = _clean_img_url(href)
            if cleaned:
                return cleaned
    # Atom/RSS links with type=image or rel=enclosure
    for link in entry.get("links") or []:
        typ = (link.get("type") or "").lower()
        rel = (link.get("rel") or "").lower()
        href = link.get("href") or ""
        if href and (typ.startswith("image/") or rel in ("enclosure", "image") and _looks_like_image(href)):
            cleaned = _clean_img_url(href)
            if cleaned:
                return cleaned
    # HTML bodies: summary/description + Atom content[]
    blobs: list[str] = []
    for key in ("summary", "description"):
        v = entry.get(key)
        if v:
            blobs.append(v)
    for c in entry.get("content") or []:
        v = c.get("value") if isinstance(c, dict) else None
        if v:
            blobs.append(v)
    blob = "\n".join(blobs)
    if blob:
        # Prefer <img src=...>
        for m in re.finditer(r'<img\b[^>]*(?:src|data-src)=["\']([^"\']+)["\']', blob, re.I):
            cleaned = _clean_img_url(m.group(1))
            if cleaned:
                return cleaned
        # Open Graph-ish meta / bare image URLs in content
        for m in re.finditer(
            r'(?:og:image|twitter:image)[^>\s]*content=["\']([^"\']+)["\']',
            blob,
            re.I,
        ):
            cleaned = _clean_img_url(m.group(1))
            if cleaned:
                return cleaned
        for m in re.finditer(
            r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|gif|webp)(?:\?[^\s"\'<>]*)?',
            blob,
            re.I,
        ):
            cleaned = _clean_img_url(m.group(0))
            if cleaned:
                return cleaned
    return None


def _looks_like_image(url: str) -> bool:
    """True if URL path (after unquoting, ignoring query) looks like an image."""
    from urllib.parse import unquote
    parsed = urlparse(url)
    path = unquote(parsed.path).lower()
    # NY Post etc. double-encode path segments
    path2 = unquote(path)
    for p in (path, path2):
        if any(p.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")):
            return True
        if "/image" in p or "/images/" in p or "/photos/" in p or "/thumb" in p:
            return True
        if "media." in (parsed.netloc or "").lower():
            return True
    # Query-string image hosts (NPR brightspotcdn dims proxy, etc.)
    q = (parsed.query or "").lower()
    if any(ext in q for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
        return True
    if "image" in path or "img" in path:
        return True
    return False


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
    image_bonus = 1.2 if cluster.image else 0.0
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



def normalize_image_key(url: str) -> str:
    """Collapse query/size variants so near-duplicate CDN URLs dedupe."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse, unquote
        p = urlparse(url)
        path = unquote(unquote(p.path)).lower().rstrip("/")
        # Drop common size suffixes / resize path noise for dedupe
        path = re.sub(r"/\d+x\d+/", "/", path)
        path = re.sub(r"[-_]\d{2,4}x\d{2,4}(?=\.|$)", "", path)
        return f"{p.netloc.lower()}{path}"
    except Exception:
        return url.lower().split("?")[0]


def pick_page_images(
    lead: "Cluster",
    teasers: list["Cluster"],
    columns: list[list["Cluster"]],
    *,
    max_total: int = 14,
    max_top: int = 4,
    max_per_column: int = 4,
) -> tuple[Optional[str], list[tuple["Cluster", str]], list[list[tuple[int, "Cluster", str]]]]:
    """Choose distinct real image URLs for lead, top flankers, and column slots.

    Returns:
      lead_img,
      top_images: list of (cluster, url) for teaser/flank area,
      col_images: per-column list of (insert_after_index, cluster, url)
        where insert_after_index is the story index after which to place the photo.
    """
    used_keys: set[str] = set()
    used_links: set[str] = set()

    def take(c: "Cluster") -> Optional[str]:
        url = c.image
        if not url:
            return None
        key = normalize_image_key(url)
        if not key or key in used_keys:
            return None
        if c.best.link in used_links:
            return None
        used_keys.add(key)
        used_links.add(c.best.link)
        return url

    lead_img = take(lead)
    remaining_budget = max_total - (1 if lead_img else 0)

    # Top / flank images: prefer teaser stories with images, then high-scoring column stories
    top_images: list[tuple["Cluster", str]] = []
    candidates = list(teasers)
    for col in columns:
        candidates.extend(col)
    # Prefer higher-score + varied source
    seen_sources: set[str] = set()
    ranked = sorted(
        candidates,
        key=lambda c: (
            0 if c.image else 1,
            0 if c.best.source not in seen_sources else 1,
            -c.score,
        ),
    )
    for c in ranked:
        if len(top_images) >= max_top or remaining_budget <= 0:
            break
        if c is lead:
            continue
        url = take(c)
        if url:
            top_images.append((c, url))
            seen_sources.add(c.best.source)
            remaining_budget -= 1

    # Column images: interleaved, 2–4 per column, skip already-used
    col_images: list[list[tuple[int, "Cluster", str]]] = [[] for _ in columns]
    per_col_target = min(max_per_column, max(2, remaining_budget // max(1, len(columns))))
    for ci, col in enumerate(columns):
        if remaining_budget <= 0:
            break
        # Spread inserts: after indices ~2, 6, 11, 16 ...
        slots = []
        n = len(col)
        if n == 0:
            continue
        desired = min(per_col_target, remaining_budget, max_per_column)
        if desired <= 0:
            continue
        # Evenly spaced insert points among the column
        step = max(2, n // (desired + 1))
        for k in range(desired):
            idx = min(n - 1, step * (k + 1) - 1)
            slots.append(idx)
        picked_here = 0
        used_slot_idx: set[int] = set()
        # Walk column in score order for image-bearing stories, map to nearest free slot
        img_stories = [(i, c) for i, c in enumerate(col) if c.image]
        # Prefer varied sources within column
        img_stories.sort(key=lambda pair: -pair[1].score)
        for i, c in img_stories:
            if picked_here >= desired or remaining_budget <= 0:
                break
            url = take(c)
            if not url:
                continue
            # Place after this story's own index (Drudge: photo under/near its headline)
            insert_at = i
            # Avoid stacking two images on consecutive slots
            if insert_at in used_slot_idx or (insert_at - 1) in used_slot_idx or (insert_at + 1) in used_slot_idx:
                # try shift
                alt = None
                for delta in (2, -2, 3, -3, 4, 1, -1):
                    j = insert_at + delta
                    if 0 <= j < n and j not in used_slot_idx and (j - 1) not in used_slot_idx:
                        alt = j
                        break
                if alt is None:
                    # still allow if we have budget and no better slot
                    if any(abs(insert_at - u) <= 1 for u in used_slot_idx):
                        # undo take
                        used_keys.discard(normalize_image_key(url))
                        used_links.discard(c.best.link)
                        continue
                else:
                    insert_at = alt
            used_slot_idx.add(insert_at)
            col_images[ci].append((insert_at, c, url))
            picked_here += 1
            remaining_budget -= 1
        col_images[ci].sort(key=lambda t: t[0])

    return lead_img, top_images, col_images


def render_story_image(item: "Item", url: str, *, css_class: str = "story-img") -> str:
    """Linked photo + optional caption (headline) under it — Drudge column style."""
    return (
        f'<div class="{css_class}">'
        f'<a href="{esc_attr(item.link)}" target="_blank" rel="noopener noreferrer">'
        f'<img src="{esc_attr(url)}" alt="" referrerpolicy="no-referrer" loading="lazy">'
        f"</a>"
        f'<div class="img-cap">{format_headline_link(item)}</div>'
        f"</div>"
    )


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

    # Prefer stories with images for a few red-emphasis slots when scores are close
    red_links = set()
    candidates = teasers + [c for col in columns for c in col]
    # Rank: splash-worthy first, then slight preference for having an image
    def red_key(c: Cluster):
        splash = (
            1 if len(c.sources) >= 3 else
            1 if (len(c.sources) >= 2 and c.score >= 14) else
            1 if keyword_boost(c.best.title) >= 3.5 else
            0
        )
        return (-splash, 0 if c.image else 1, -c.score)

    for c in sorted(candidates, key=red_key):
        if c is lead:
            continue
        if len(red_links) >= config.RED_EMPHASIS_COUNT:
            break
        # Keep qualification bar; image only breaks ties via sort key
        if len(c.sources) >= 3 or (len(c.sources) >= 2 and c.score >= 14) or keyword_boost(c.best.title) >= 3.5 or c.score >= 12:
            red_links.add(c.best.link)

    lead_img, top_images, col_images = pick_page_images(lead, teasers, columns)

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
  .top-photos {{
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 14px 22px;
    margin: 6px auto 12px;
    max-width: 900px;
    text-align: center;
  }}
  .top-photos .story-img {{
    max-width: 220px;
  }}
  .top-photos .story-img img {{
    max-width: 220px;
    width: 100%;
    height: auto;
    border: 1px solid #000;
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
  .lead-flank {{
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    align-items: flex-start;
    gap: 16px 20px;
    margin: 8px auto 4px;
  }}
  .lead-flank .flank-img {{
    max-width: 200px;
  }}
  .lead-flank .flank-img img {{
    max-width: 200px;
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
  .col .story-img {{
    margin: 10px auto 14px;
    text-align: center;
    max-width: 280px;
  }}
  .col .story-img img {{
    max-width: 260px;
    width: 100%;
    height: auto;
    border: 1px solid #000;
  }}
  .img-cap {{
    margin-top: 4px;
    font-size: 13px;
    line-height: 1.3;
    text-align: center;
  }}
  .img-cap a {{
    text-decoration: underline;
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
    .lead-flank {{ flex-direction: column; align-items: center; }}
    .top-photos {{ flex-direction: column; align-items: center; }}
    .col .story-img, .col .story-img img {{
      max-width: 100%;
    }}
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

    top_links = {c.best.link for c, _ in top_images}
    for c in teasers:
        # Skip duplicate headline if this teaser is shown as a top photo caption
        if c.best.link in top_links:
            continue
        red = c.best.link in red_links
        parts.append(f"    <div>{format_headline_link(c.best, red=red)}</div>\n")

    parts.append("  </div>\n")

    # Optional small photos above the lead (Drudge sprinkles)
    if top_images:
        parts.append('  <div class="top-photos">\n')
        for c, url in top_images[:2]:
            parts.append("    " + render_story_image(c.best, url, css_class="story-img") + "\n")
        parts.append("  </div>\n")

    # Lead
    parts.append('  <div class="lead-block">\n')
    if keyword_boost(lead_item.title) >= 2 or len(lead.sources) >= 3:
        parts.append('    <div class="siren">***&nbsp;&nbsp;***</div>\n')
    parts.append(f'    <div class="lead-headline">{format_headline_link(lead_item, red=True, all_caps=True)}</div>\n')

    flank = top_images[2:]  # remaining top images flank the lead photo
    if lead_img or flank:
        parts.append('    <div class="lead-flank">\n')
        if len(flank) >= 1:
            c, url = flank[0]
            parts.append(
                "      " + render_story_image(c.best, url, css_class="flank-img") + "\n"
            )
        if lead_img:
            parts.append(
                f'      <div class="lead-img"><a href="{esc_attr(lead_item.link)}" target="_blank" rel="noopener noreferrer">'
                f'<img src="{esc_attr(lead_img)}" alt="" referrerpolicy="no-referrer" loading="lazy"></a></div>\n'
            )
        if len(flank) >= 2:
            c, url = flank[1]
            parts.append(
                "      " + render_story_image(c.best, url, css_class="flank-img") + "\n"
            )
        parts.append("    </div>\n")
    if len(lead.sources) > 1:
        srcs = ", ".join(sorted(_short_source(s) for s in lead.sources))
        parts.append(f'    <div class="sources-note">also: {esc_text(srcs)}</div>\n')
    parts.append("  </div>\n")

    parts.append('  <hr class="main">\n')
    parts.append('  <div class="cols">\n')

    for ci, col in enumerate(columns):
        parts.append('    <div class="col">\n')
        # Map insert_after index -> list of images (usually one)
        by_idx: dict[int, list[tuple[Cluster, str]]] = {}
        for insert_at, c, url in col_images[ci]:
            by_idx.setdefault(insert_at, []).append((c, url))
        shown_img_links = {c.best.link for pairs in by_idx.values() for c, _ in pairs}
        for i, c in enumerate(col):
            if i > 0 and i % 5 == 0:
                parts.append("      <hr>\n")
            # If this story is shown as an inline photo caption elsewhere in the column, skip plain dup
            if c.best.link in shown_img_links:
                pass  # caption rendered with its image
            else:
                red = c.best.link in red_links
                parts.append(f'      <div class="item">{format_headline_link(c.best, red=red)}</div>\n')
            if i in by_idx:
                for ic, url in by_idx[i]:
                    # If caption story differs from the headline just above, still fine
                    parts.append("      " + render_story_image(ic.best, url) + "\n")
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
    img_srcs = sorted(set(re.findall(r'<img\b[^>]+src="([^"]+)"', html_out, flags=re.I)))
    print(f"Wrote {out_path} ({n_on_page} headlines on page, {len(img_srcs)} images)", flush=True)
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
        f"page_images={len(img_srcs)}",
        "IMAGES:",
        *[f"  {u}" for u in img_srcs],
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
