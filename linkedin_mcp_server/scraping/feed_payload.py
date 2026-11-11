"""Feed permalink recognition across DOM anchors and SDUI payloads."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import re

from linkedin_mcp_server.scraping.link_metadata import (
    Reference,
    build_references,
    dedupe_references,
)

_FEED_RSC_MARKER = "sduiid=com.linkedin.sdui.pagers.feed.mainFeed"
# Matches a LinkedIn post permalink in either plain or JSON-escaped form
# (the initial /feed/ HTML embeds the RSC flight data with \u002f for slashes,
# while paginated responses use plain slashes). Captures the slug portion so
# we can rebuild a canonical URL regardless of the source encoding.
POST_SLUG_URL_RE = re.compile(
    r"linkedin\.com(?:\\u002[fF]|/)posts(?:\\u002[fF]|/)"
    r"(?P<slug>[A-Za-z0-9_-]+?-(?:ugcPost|activity|share)-\d+-[A-Za-z0-9_-]+)"
)
_FEED_DOCUMENT_URLS = {
    "https://www.linkedin.com/feed",
    "https://www.linkedin.com/feed/",
}


def is_feed_payload_response(url: str) -> bool:
    """True if the response URL is one that carries `postSlugUrl` fields."""
    if _FEED_RSC_MARKER in url:
        return True
    return url.split("?", 1)[0] in _FEED_DOCUMENT_URLS


# How much of a post's own text a reference carries: enough to recognise the
# post inside ``sections["feed"]``, which remains the full text.
_POST_EXCERPT_LIMIT = 240


def build_feed_references(
    raw_references: list[Any],
    captured_urls: list[str],
    *,
    post_boundaries: list[dict[str, Any]] | None = None,
) -> list[Reference]:
    """Compose feed references from DOM anchors + SDUI captures.

    The feed page renders many anchors that are not post permalinks:
    sidebar widgets, profile cards, employer logos, etc. Mixing them
    into ``references["feed"]`` blurs the contract and competes with
    SDUI permalinks for the per-section cap. We keep only the
    ``feed_post`` slice from the DOM:

    - DOM anchors → ``feed_post`` entries with ``/feed/update/<urn>/``
      URLs (whatever ``classify_link`` recognises).
    - SDUI captures → ``feed_post`` entries with ``/posts/<slug>`` URLs
      for permalinks that the DOM does not surface as an anchor.

    Both are deduped on exact URL string. The two shapes pointing at
    the same underlying post will *not* collapse — ``dedupe_references``
    matches strings, not URNs. Both are valid LinkedIn permalinks, so
    consumers should treat ``feed_post`` as polymorphic on URL form;
    URN-based equivalence is left to the consumer.

    ``post_boundaries`` is the per-post read of the same page (see
    ``FEED_POST_BOUNDARIES_JS`` in feed.py): each entry names one rendered
    post by its own URN, or carries ``urn: None`` when the post's own
    markup does not name exactly one, so the reference can carry the
    post's text as an excerpt and its URN as ``value``. A post without
    a URN is not paired: no URL is guessed for it. Boundary entries sit
    before the anchor-derived slice, and an anchor whose URL a boundary
    already claimed is skipped outright, so a paired post never competes
    with its bare anchor on label richness or cap space.
    """
    paired: list[Reference] = []
    for post in post_boundaries or []:
        urn = post.get("urn")
        if not urn:
            continue
        ref: Reference = {
            "kind": "feed_post",
            "url": f"/feed/update/{urn}/",
            "context": "feed",
            "value": urn,
        }
        text = post.get("text") or ""
        if text:
            ref["text"] = text[:_POST_EXCERPT_LIMIT]
        paired.append(ref)
    paired_urls = {ref["url"] for ref in paired}
    refs = paired + [
        ref
        for ref in build_references(raw_references, "feed")
        if ref["kind"] == "feed_post" and ref["url"] not in paired_urls
    ]
    existing = {r["url"] for r in refs}
    for sdui_url in captured_urls:
        # AGENTS.md mandates relative paths for LinkedIn references.
        # The SDUI capture carries fully-qualified URLs like
        # https://www.linkedin.com/posts/<slug>; strip the host so the
        # relative-path convention holds. ``classify_link`` does not
        # currently route ``/posts/<slug>`` paths to any kind, so we
        # bypass it for this fallback append.
        parsed = urlparse(sdui_url)
        if not parsed.path.startswith("/posts/"):
            continue
        relative = parsed.path
        if relative in existing:
            continue
        refs.append({"kind": "feed_post", "url": relative, "context": "feed"})
        existing.add(relative)
    # Cap kept in sync with _REFERENCE_CAPS["feed"] in link_metadata.py;
    # changing one without the other will drop or duplicate entries
    # silently. Matches get_feed's num_posts ceiling (Field(ge=1, le=50)).
    return dedupe_references(refs, cap=50)
