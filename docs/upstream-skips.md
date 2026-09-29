# Upstream review log

Every upstream commit a drift review has judged, and what it decided. The next
review starts from the newest `upstream-reviewed-*` tag, so it only reads what
landed after it. Read this file before triaging anything older. Do not
re-triage a row here unless new upstream work changes its verdict.

This fork ports by hand, cherry-pick and squash merge, so a ported commit keeps
showing in `main..upstream/main`, usually under another subject. Presence in
that range says nothing. The `ported` rows name the PR that carries each one.

Verdicts:

- **ported**: the behaviour is on `main`, or in the named PR.
- **filed**: needs a decision or live measurement; the issue says what it buys and costs.
- **skip**: ruled out for the reason given.

## 2026-09-29: `1972748..1770114`

137 commits. There was no earlier tag, so this run started at the merge-base
with `upstream/main` and also records what earlier backport PRs carried.

| SHA | Subject | Verdict | Reason | Date |
|---|---|---|---|---|
| 858c268 | test(daemon): Reap failed frontends without EOF (#966) | ported | Earlier, in #52. | 2026-09-29 |
| ca59992 | docs(readme): Remove architecture warning (#967) | skip | Deletes an architecture warning that is still accurate here. | 2026-09-29 |
| 33f48c8 | docs(README): Refactor README for clarity and conciseness (#979) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| 5a430c0 | docs(README): Refine sponsor descriptions (#980) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| 336cd42 | docs(README): Align sponsor CTA and shorten license note (#981) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| d925652 | chore(license): Extend copyright to 2026 (#982) | skip | Upstream's license year. | 2026-09-29 |
| 656e3a1 | fix(readme): Render the Codex badge icon and drop Development (#984) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| 49e0f5f | fix(browser): Replace colliding import cookies (#978) | ported | Earlier, in #46. | 2026-09-29 |
| df07225 | chore: Bump version to 4.24.3 (#985) | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-09-29 |
| 013c8a9 | chore: sync versioned files to v4.24.3 [skip ci] | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-09-29 |
| c82e3f7 | chore(issues): Standardize issue forms around packet schema | skip | Issue-packet intake; this fork deliberately has no packet skill or schema. | 2026-09-29 |
| 954b182 | chore(intake): Add issue-packet skill and root instructions pointer (#992) | skip | Issue-packet intake; this fork deliberately has no packet skill or schema. | 2026-09-29 |
| 2f2ad52 | chore(intake): Write packet description as triggers (#993) | skip | Issue-packet intake; this fork deliberately has no packet skill or schema. | 2026-09-29 |
| 8480b83 | fix(diagnostics): Keep scrape notes off GitHub (#994) | ported | Earlier, in #55, without the c82e3f7 packet pointer. | 2026-09-29 |
| 3a7de97 | chore(intake): Evaluate packets before live repro (#995) | skip | Issue-packet intake; this fork deliberately has no packet skill or schema. | 2026-09-29 |
| 91a8bd7 | test(daemon): Skip the 3.12 election crash | skip | Deferred: restoring the Windows 3.12.4 eight-client stress can only be proven on Windows CI and does nothing for rate limits. 88dd0f1 does not apply as-is (soak gate from 65d27ac). | 2026-09-29 |
| d65c28f | test: Reject tautological tests (#1001) | skip | Our 'A test that cannot fail' rule in CLAUDE.md is more specific than the replacement, and the test deletions collide with fork-only tools. | 2026-09-29 |
| 2aab5e4 | feat(scraping): capture post permalinks on content-search pages (#999) | filed | #87: hand port through `claim_soft_retry` and the reference cap; needs live verification. | 2026-09-29 |
| bddded1 | docs: Record the rendered-page decision (#1002) | skip | Rendered-page ADR. It would put the fork-only messaging replay (`messaging_api.py`, in-page fetch with forwarded csrf headers, up to 4 requests per call, not charged to pacing) out of policy. Decide on that replay before adopting it. | 2026-09-29 |
| b74530e | ci: Cut Windows CI legs and cancel stale runs (#1003) | skip | Upstream's CI matrix budget and #838 soak workflow; not ours. | 2026-09-29 |
| e38f9a3 | fix(ci): Keep main runs and 3.13 Windows coverage (#1004) | skip | Upstream's CI matrix budget and #838 soak workflow; not ours. | 2026-09-29 |
| 65d27ac | ci(daemon): Capture a native dump for #838 (#1005) | skip | Upstream's CI matrix budget and #838 soak workflow; not ours. | 2026-09-29 |
| 824c0f4 | fix(daemon): Report an unknown outcome after owner loss | ported | Earlier, in #52. | 2026-09-29 |
| bdcbd51 | fix(daemon): Keep a closing session from burying the answer | ported | Earlier, in #52. | 2026-09-29 |
| 9e8e4ca | ci(daemon): Capture a native stack for #838 | skip | Upstream's CI matrix budget and #838 soak workflow; not ours. | 2026-09-29 |
| 54baf84 | fix(session): Name the Windows arch without WMI | ported | Earlier, in #52. | 2026-09-29 |
| 77b5d89 | fix(daemon): Stop post-call schema discovery | ported | Earlier, in #52. | 2026-09-29 |
| 88dd0f1 | test(daemon): Restore election stress | skip | Deferred: restoring the Windows 3.12.4 eight-client stress can only be proven on Windows CI and does nothing for rate limits. 88dd0f1 does not apply as-is (soak gate from 65d27ac). | 2026-09-29 |
| d7e99fb | test(control): Measure peer drain directly | ported | This run, in #93. | 2026-09-29 |
| 26a8f84 | fix(scraping): Accept periods in company slugs | ported | Earlier, in #46. | 2026-09-29 |
| f80ed68 | chore: Bump version to 4.24.4 | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-09-29 |
| 9a0eda8 | chore: sync versioned files to v4.24.4 [skip ci] | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-09-29 |
| 07f4125 | fix(daemon): Report startup log path (#1022) | ported | Earlier, in #52. | 2026-09-29 |
| b3b8f54 | fix(daemon): Refresh lookup after spawn (#1023) | ported | Earlier, in #52. | 2026-09-29 |
| 441f5e9 | fix(daemon): Settle owner election safely | ported | Earlier, in #52. | 2026-09-29 |
| da5da20 | test(bootstrap): Remove scheduler timing race | ported | This run, in #93. | 2026-09-29 |
| b461d41 | test(daemon): Probe stable group signals | skip | Linux pidfd research probe for upstream #809; no pidfd code here. | 2026-09-29 |
| 10cd55b | test(daemon): Measure Windows crash fence | skip | Windows Job/guardian research harness for upstream #808; tests the harness, not shipped code. | 2026-09-29 |
| 1f40ce0 | fix(jobs): Ignore no-match recommendations (#974) | ported | Earlier, in #46. | 2026-09-29 |
| f0ba90c | fix(jobs): Keep More jobs references on postings | ported | Earlier, in #46. | 2026-09-29 |
| ffa330c | feat(jobs): Report search total and promoted jobs | ported | Earlier, in #51. | 2026-09-29 |
| 0090dd4 | test(daemon): Measure Windows guardian loss (#1028) | skip | Windows Job/guardian research harness for upstream #808; tests the harness, not shipped code. | 2026-09-29 |
| 9caaf4d | ci: Require PR model attribution | skip | Upstream's PR model-attribution checker; this fork forbids attribution instead. | 2026-09-29 |
| 4680f1d | test(daemon): Probe Windows conjunction fence (#1038) | skip | Windows Job/guardian research harness for upstream #808; tests the harness, not shipped code. | 2026-09-29 |
| 4649bdb | fix(ci): Allow model-only attribution | skip | Upstream's PR model-attribution checker; this fork forbids attribution instead. | 2026-09-29 |
| 64518fb | fix(ci): Allow unpunctuated model attribution | skip | Upstream's PR model-attribution checker; this fork forbids attribution instead. | 2026-09-29 |
| 0ca770d | docs(proxy): Prefer sticky residential | skip | Swaps the recommended proxy type without a measurement; our text already forbids per-request rotation. | 2026-09-29 |
| 46294f4 | test(daemon): Falsify region inversion (#1044) | skip | Windows Job/guardian research harness for upstream #808; tests the harness, not shipped code. | 2026-09-29 |
| 8ddab1c | test(daemon): Probe Windows Job topology (#1045) | skip | Windows Job/guardian research harness for upstream #808; tests the harness, not shipped code. | 2026-09-29 |
| 2451bf0 | test(person): Pin navigation pacing | ported | This run, in #93, adapted with `_identity_jitter()` because this fork jitters every pause. | 2026-09-29 |
| c58af34 | fix(feed): Report listener cleanup failures | ported | Earlier, in #76. c58af34's capture.py hunk waits on 2aab5e4 (#87). | 2026-09-29 |
| 7ac1190 | test(daemon): Stabilize winner settlement (#1050) | ported | This run, in #93. | 2026-09-29 |
| 460acea | test(daemon): Retry successor lock rundown (#1051) | skip | Windows Job/guardian research harness for upstream #808; tests the harness, not shipped code. | 2026-09-29 |
| 3fef9c7 | fix(search): Encode job filter tokens | ported | Earlier, in #72. | 2026-09-29 |
| f52cc5e | test(windows): Add browser launch evidence (#1055) | skip | Windows launch evidence built on the #808 harness, pinned to 149.0.7827.55. Guard-before-launch is already pinned in tests/test_browser_downgrade.py. | 2026-09-29 |
| f4fc67d | test(windows): Poll Job termination drain (#1059) | skip | Windows Job/guardian research harness for upstream #808; tests the harness, not shipped code. | 2026-09-29 |
| 2b754d4 | test: Give DOM tests the caller's browser cache | ported | This run, in #93. | 2026-09-29 |
| ed0d036 | fix(jobs): Say where dropped filters go on redesign | ported | Earlier, in #72. | 2026-09-29 |
| 827047f | fix(jobs): Wait for job description to load | ported | Earlier, in #76. c58af34's capture.py hunk waits on 2aab5e4 (#87). | 2026-09-29 |
| 14394b6 | docs(docker): Render prerequisites in details | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| 9f2cc9c | fix(tools): Validate references before startup | ported | Earlier, in #80, without the people-search hunk: this fork resolves company names to URNs, which needs the browser. | 2026-09-29 |
| 03828d5 | docs(readme): Shorten tool descriptions | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| 068ba2d | docs(readme): Fill tool table width (#1066) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| 1476651 | fix(connection): Keep invite notes with Premium nudge | ported | Earlier, in #72. | 2026-09-29 |
| 47df07b | docs(daemon): Keep default enablement blocked | skip | Superseded upstream by 9029441 and cites evidence records this fork does not carry. | 2026-09-29 |
| 930df23 | docs(readme): Simplify section headings (#1071) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| 3722a87 | test(daemon): Measure Linux pidfd preflight | skip | Linux pidfd research probe for upstream #809; no pidfd code here. | 2026-09-29 |
| 8acfc1f | chore: Bump version to 4.25.0 (#1072) | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-09-29 |
| 15000e6 | chore: sync versioned files to v4.25.0 [skip ci] | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-09-29 |
| ccd86a1 | ci(release): Lead release notes with changes (#1074) | skip | Upstream release-note, labelling and review-bot plumbing (towncrier, Macroscope). | 2026-09-29 |
| 098eef3 | fix(capture): Classify parsed URL paths | ported | Earlier, in #76. c58af34's capture.py hunk waits on 2aab5e4 (#87). | 2026-09-29 |
| d3f685e | docs(readme): Add Setup Help to proxy section (#1079) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| 4b8117b | docs(agents): Keep the PR number in squash titles (#1081) | skip | Already practised: squash titles here keep `(#N)`. | 2026-09-29 |
| 2cba701 | docs(readme): Split MCP Bundle early-call note (#1085) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| d1c1a98 | docs: Simplify contribution intro (#1087) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| c7b84fc | docs(readme): Use Installation heading for bundle (#1090) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| 5f39be5 | ci(release): Build release notes with towncrier (#1082) | skip | Upstream release-note, labelling and review-bot plumbing (towncrier, Macroscope). | 2026-09-29 |
| c5e4ffd | docs(readme): Align Codex plugin setup section (#1092) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| c8021e2 | fix(capture): Report a missing contact overlay (#1096) | ported | Earlier, in #76. c58af34's capture.py hunk waits on 2aab5e4 (#87). | 2026-09-29 |
| 49902cd | docs(readme): Clarify setup snippets and session import (#1098) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| 2e1f5b5 | fix(messaging): Verify row thread ownership (#1102) | ported | Earlier, in #78, caller-facing half only: this fork resolves threads from conversation entities (3785275), not row clicks. | 2026-09-29 |
| 8be7230 | test(daemon): Skip pidfd probes that never spawn (#1101) | skip | Linux pidfd research probe for upstream #809; no pidfd code here. | 2026-09-29 |
| 4170958 | fix(jobs): Report a posting without its description (#1088) | ported | Earlier, in #76. c58af34's capture.py hunk waits on 2aab5e4 (#87). | 2026-09-29 |
| a98989c | fix(messaging): Find the nested top card (#1105) | ported | Earlier, in #78, by hand. | 2026-09-29 |
| 617818f | ci(pr): Label dependency PRs as dependencies (#1112) | skip | Upstream release-note, labelling and review-bot plumbing (towncrier, Macroscope). | 2026-09-29 |
| 9029441 | docs(daemon): Record the default-on contract (#1113) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
| dc7d4a6 | chore: Bump version to 4.25.1 (#1114) | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-09-29 |
| 5380ba6 | chore: sync versioned files to v4.25.1 [skip ci] | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-09-29 |
| 0253421 | test(daemon): Witness daemon-only regressions (#1115) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
| 3b0a659 | chore(renovate): Widen requires-python upper bound (#1118) | ported | This run, in #91. | 2026-09-29 |
| 5f58595 | chore(deps): Update actions and setuptools majors (#1116) | skip | CI and build pin churn (checkout v7, setup-uv v10, setuptools 84) with identical artifacts; our own Renovate will propose it. | 2026-09-29 |
| 74daf70 | chore(renovate): Move ruff and its hook together (#1121) | ported | This run, in #91. | 2026-09-29 |
| f35d741 | docs(agents): Add live request limits (#1119) | skip | Its live request limits (10/min per tool, 30 invites/day) contradict this fork's enforced pacing (`pacing.py`: 20 invites/24h, 40/h, per-kind caps). | 2026-09-29 |
| 436c6e5 | fix(daemon): Match Direct signals on owner exit (#1122) | filed | #89: touches election and the lease; a different-version successor would be refused naming a dead holder. | 2026-09-29 |
| 639016b | fix(deps): Move the lock to patchright 1.63.0 (#1123) | filed | #86: needs a detector run under this fork's resource blocking, and a rewrite of the `_COMPARABLE_PRODUCTS` rule. | 2026-09-29 |
| 750309c | fix(daemon): Keep custom browsers on Direct (#1125) | ported | This run, in #92, without the changelog fragment and witness file this fork lacks. | 2026-09-29 |
| 2acb631 | feat(daemon): Keep the daemon off shared storage (#1126) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
| 164e188 | feat(daemon): Mark every call to a shared owner (#1127) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
| ec47118 | feat(cli): Retire an idle shared owner on request (#1128) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
| b1eeaab | test(daemon): Load a synthetic origin through a proxy (#1129) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
| 9760128 | chore(macroscope): Add check run agents (#1133) | skip | Upstream release-note, labelling and review-bot plumbing (towncrier, Macroscope). | 2026-09-29 |
| 1a5a88c | ci(pr): Exempt Renovate from changelog fragments (#1132) | skip | Upstream release-note, labelling and review-bot plumbing (towncrier, Macroscope). | 2026-09-29 |
| ccf29cb | fix(connect): Keep a failed note fill off the quota (#1136) | ported | Earlier, in #72. | 2026-09-29 |
| cea70b2 | ci(pr): Skip Macroscope's summary in attribution (#1135) | skip | Upstream's PR model-attribution checker; this fork forbids attribution instead. | 2026-09-29 |
| beb63d9 | refactor(tools): Remove hidden extractor seam (#1138) | filed | #85: FastMCP 4 line. No rate-limit or logout value; will not replay onto our daemon files. | 2026-09-29 |
| a454fa2 | fix(connect): Recheck a sent invite before failing (#1137) | skip | Replaced here: fe6eaa3 verifies an invite on the sent list instead of re-reading the profile, and upstream's extra profile re-read would add 1-2 charged page loads. | 2026-09-29 |
| b3fcac2 | feat(deps): Migrate to FastMCP 4 (#1139) | filed | #85: FastMCP 4 line. No rate-limit or logout value; will not replay onto our daemon files. | 2026-09-29 |
| f918f4b | feat(daemon): Talk to the owner in the 2026-07-28 era (#1140) | filed | #85: FastMCP 4 line. No rate-limit or logout value; will not replay onto our daemon files. | 2026-09-29 |
| a2e1e40 | test(daemon): Compare Direct and daemon on one row (#1130) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
| 30f7bf0 | test(daemon): Freeze a baseline and compare R12 (#1141) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
| 251361f | feat(company): Add max_scrolls to get_company_posts (#1104) | ported | Earlier, in #72. | 2026-09-29 |
| 87fb87c | fix(messaging): Confirm sends from LinkedIn's server acknowledgement (#1108) | ported | Earlier, in #72. | 2026-09-29 |
| d8a20e3 | test(daemon): Observe signals and kill the owner (#1142) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
| a996e9f | fix(auth): Stop on a restricted account (#1147) | ported | Earlier, in #77. | 2026-09-29 |
| 1e3158a | chore: Bump version to 4.26.0 (#1151) | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-09-29 |
| c9c50d5 | chore: sync versioned files to v4.26.0 [skip ci] | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-09-29 |
| d6d6e68 | test(daemon): Keep a known root's reading on a failed read (#1149) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
| 6e73447 | ci(pr): Check PR body for bot co-author trailers (#1148) | skip | Upstream's PR model-attribution checker; this fork forbids attribution instead. | 2026-09-29 |
| 9d9f5c3 | test(messaging): Give DOM test setup room to load (#1155) | ported | This run, in #93. | 2026-09-29 |
| 7176626 | test(daemon): Run the watcher ahead of the row (#1153) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
| 056a56f | ci(release): Restyle the release notes (#1158) | skip | Upstream release-note, labelling and review-bot plumbing (towncrier, Macroscope). | 2026-09-29 |
| 5366182 | ci(pr): Check only the PR's own commits (#1159) | skip | Upstream's PR model-attribution checker; this fork forbids attribution instead. | 2026-09-29 |
| e6fea6b | fix(messaging): Report LinkedIn's Press Enter to Send preference (#1109) | filed | #88: detects on a LinkedIn layout class (`.msg-form__send-toggle`), which CLAUDE.md forbids; needs a decision or a live-measured structural signal. | 2026-09-29 |
| 411b68f | fix(connection): Keep chat overlays out of the invite dialog (#1110) | skip | Already handled by `_MODAL_DIALOG_INDEX_JS` (horizontally centred dialog). The upstream hunk narrows only `_DIALOG_SELECTOR`, which would desync the JS index, and its test expects a 3-tuple where ours returns 4. | 2026-09-29 |
| db55010 | test(messaging): Cancel after confirmation begins (#1160) | ported | This run, in #93. | 2026-09-29 |
| 6f3bc25 | docs(readme): Add hosted Cadenza block (#1083) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| cee6ec2 | fix(bootstrap): Accept peer install metadata (#1162) | ported | Earlier, in #77. | 2026-09-29 |
| 03c2bc0 | docs(readme): Tag the Cadenza links with UTM (#1165) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| bf021f1 | fix(bootstrap): Recover from Windows temp ACLs (#1163) | ported | Earlier, in #77. | 2026-09-29 |
| 0dcb08c | ci(release): Preserve the strict setting (#1164) | skip | Upstream release-note, labelling and review-bot plumbing (towncrier, Macroscope). | 2026-09-29 |
| 7f4a125 | chore: Bump version to 4.26.1 (#1166) | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-09-29 |
| b0dd016 | chore: sync versioned files to v4.26.1 [skip ci] | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-09-29 |
| 3fc16a6 | test(daemon): Plant a failed Job query (#1157) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
| 806c5a1 | docs(readme): Trim installer troubleshooting (#1168) | skip | Upstream README restyle, sponsor or marketing copy. | 2026-09-29 |
| 2ab6132 | ci(pr): Use the released agent guardrails | skip | Upstream's PR model-attribution checker; this fork forbids attribution instead. | 2026-09-29 |
| 19e5d9b | test(windows): Record baseline fence overlap (#1175) | skip | Windows Job/guardian research harness for upstream #808; tests the harness, not shipped code. | 2026-09-29 |
| 1770114 | test(daemon): Plant an unconfirmed close (#1154) | filed | #90: upstream's daemon default-on line. The daemon is opt-in here. | 2026-09-29 |
