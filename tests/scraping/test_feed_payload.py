"""Tests for feed permalink recognition across DOM anchors and SDUI payloads."""

from linkedin_mcp_server.scraping.feed_payload import build_feed_references


class TestBuildFeedReferences:
    """Tests for build_feed_references SDUI-capture / DOM-anchor merging."""

    def test_sdui_urls_become_relative_feed_post_references(self):
        captured = [
            "https://www.linkedin.com/posts/alice_some-slug-ugcPost-1-xx",
            "https://www.linkedin.com/posts/bob_other-post-share-2-yy",
        ]
        refs = build_feed_references([], captured)
        assert refs == [
            {
                "kind": "feed_post",
                "url": "/posts/alice_some-slug-ugcPost-1-xx",
                "context": "feed",
            },
            {
                "kind": "feed_post",
                "url": "/posts/bob_other-post-share-2-yy",
                "context": "feed",
            },
        ]

    def test_duplicate_sdui_urls_are_deduped(self):
        captured = [
            "https://www.linkedin.com/posts/alice_x-ugcPost-1-xx",
            "https://www.linkedin.com/posts/alice_x-ugcPost-1-xx",
        ]
        refs = build_feed_references([], captured)
        assert len(refs) == 1
        assert refs[0]["url"] == "/posts/alice_x-ugcPost-1-xx"

    def test_dom_anchor_feed_update_passes_through(self):
        # DOM anchors that classify_link recognises as feed_post survive
        # the merge alongside SDUI captures.
        raw_anchors = [
            {
                "href": "https://www.linkedin.com/feed/update/urn:li:activity:1234567890/",
                "text": "View post",
            }
        ]
        refs = build_feed_references(raw_anchors, [])
        assert any(
            r["url"] == "/feed/update/urn:li:activity:1234567890/"
            and r["kind"] == "feed_post"
            for r in refs
        )

    def test_non_posts_paths_in_sdui_capture_are_skipped(self):
        # Defensive: only /posts/<slug> shapes count for SDUI append.
        captured = [
            "https://www.linkedin.com/in/someuser/",
            "https://www.linkedin.com/posts/alice_x-ugcPost-1-xx",
        ]
        refs = build_feed_references([], captured)
        assert [r["url"] for r in refs] == ["/posts/alice_x-ugcPost-1-xx"]

    def test_cap_matches_num_posts_ceiling(self):
        captured = [
            f"https://www.linkedin.com/posts/p{i}-ugcPost-{i}-xx" for i in range(60)
        ]
        refs = build_feed_references([], captured)
        # Cap is 50, mirroring _REFERENCE_CAPS["feed"] / num_posts <= 50.
        assert len(refs) == 50

    def test_non_feed_post_dom_anchors_are_filtered(self):
        # Sidebar profile / company / external anchors must not crowd
        # out SDUI permalinks — references["feed"] is feed_post-only.
        raw_anchors = [
            {
                "href": "https://www.linkedin.com/in/sidebar-user/",
                "text": "Sidebar User",
            },
            {
                "href": "https://www.linkedin.com/company/some-corp/",
                "text": "Some Corp",
            },
            {
                "href": "https://example.com/external/",
                "text": "External Link",
            },
        ]
        refs = build_feed_references(raw_anchors, [])
        assert refs == []

    def test_feed_post_dom_anchors_coexist_with_sdui_captures(self):
        # The two sources fold into the same feed_post kind without
        # collapsing across URL shapes pointing at the same post.
        raw_anchors = [
            {
                "href": "https://www.linkedin.com/feed/update/urn:li:activity:111/",
                "text": "View post",
            }
        ]
        captured = ["https://www.linkedin.com/posts/alice_x-ugcPost-1-xx"]
        refs = build_feed_references(raw_anchors, captured)
        urls = [r["url"] for r in refs]
        kinds = {r["kind"] for r in refs}
        assert urls == [
            "/feed/update/urn:li:activity:111/",
            "/posts/alice_x-ugcPost-1-xx",
        ]
        assert kinds == {"feed_post"}


class TestFeedPostBoundaries:
    """Post-boundary extraction merged into build_feed_references."""

    def test_a_urn_boundary_pairs_text_with_the_canonical_url(self):
        posts = [
            {
                "text": "Ada writes about engines",
                "urn": "urn:li:activity:7214916299838103554",
            }
        ]
        refs = build_feed_references([], [], post_boundaries=posts)
        assert refs == [
            {
                "kind": "feed_post",
                "url": "/feed/update/urn:li:activity:7214916299838103554/",
                "context": "feed",
                "text": "Ada writes about engines",
                "value": "urn:li:activity:7214916299838103554",
            }
        ]

    def test_a_boundary_without_a_urn_is_not_paired(self):
        # A post whose own markup names no URN gets no guessed URL; the
        # anchor and SDUI fallbacks still cover it.
        posts = [{"text": "Grace on compilers", "urn": None}]
        refs = build_feed_references([], [], post_boundaries=posts)
        assert refs == []

    def test_the_excerpt_is_capped(self):
        posts = [
            {
                "text": "x" * 900,
                "urn": "urn:li:activity:7000000000000000000",
            }
        ]
        (ref,) = build_feed_references([], [], post_boundaries=posts)
        assert len(ref["text"]) == 240

    def test_a_boundary_richer_than_the_same_dom_anchor_wins(self):
        raw_anchors = [
            {
                "href": "https://www.linkedin.com/feed/update/urn:li:activity:1234567890/",
            }
        ]
        posts = [
            {
                "text": "The post's own words",
                "urn": "urn:li:activity:1234567890",
            }
        ]
        refs = build_feed_references(raw_anchors, [], post_boundaries=posts)
        assert len(refs) == 1
        assert refs[0]["text"] == "The post's own words"

    def test_boundaries_precede_sdui_appends_and_share_the_cap(self):
        posts = [
            {
                "text": f"Post {i}",
                "urn": f"urn:li:activity:{7000000000000000000 + i}",
            }
            for i in range(55)
        ]
        captured = ["https://www.linkedin.com/posts/sdui-ugcPost-1-xx"]
        refs = build_feed_references([], captured, post_boundaries=posts)
        assert len(refs) == 50
        assert all(r["url"].startswith("/feed/update/") for r in refs)
        assert "/posts/sdui-ugcPost-1-xx" not in [r["url"] for r in refs]

    def test_no_text_still_yields_the_identified_post(self):
        posts = [
            {
                "text": "",
                "urn": "urn:li:activity:7000000000000000000",
            }
        ]
        (ref,) = build_feed_references([], [], post_boundaries=posts)
        assert ref["url"] == "/feed/update/urn:li:activity:7000000000000000000/"
        assert "text" not in ref

    def test_boundaries_survive_a_cap_filling_anchor_slice(self):
        # A rendered post must not lose its paired entry because earlier
        # anchor-derived references already filled the 50-entry cap.
        raw_anchors = [
            {
                "href": "https://www.linkedin.com/feed/update/"
                f"urn:li:activity:{7000000000000000000 + i}/"
            }
            for i in range(50)
        ]
        posts = [
            {
                "text": "The paired post",
                "urn": "urn:li:activity:1",
            }
        ]
        refs = build_feed_references(raw_anchors, [], post_boundaries=posts)
        assert len(refs) == 50
        assert refs[0]["value"] == "urn:li:activity:1"

    def test_a_wordier_same_url_anchor_loses_to_the_boundary(self):
        # A permalink anchor's label ("3 comments") can outscore a short
        # excerpt on text length; the boundary's pairing must still win
        # or the URN and the post's own words are lost.
        raw_anchors = [
            {
                "href": "https://www.linkedin.com/feed/update/urn:li:activity:1234567890/",
                "text": "42 comments and a very long anchor label",
            }
        ]
        posts = [
            {
                "text": "Short",
                "urn": "urn:li:activity:1234567890",
            }
        ]
        refs = build_feed_references(raw_anchors, [], post_boundaries=posts)
        assert len(refs) == 1
        assert refs[0]["text"] == "Short"
        assert refs[0]["value"] == "urn:li:activity:1234567890"
