# Upstream review log

Every upstream commit a drift review has judged, and what it decided. The next
review starts from the newest `upstream-reviewed-*` tag, so it only reads what
landed after it. If no such tag exists, use the end SHA of the newest dated
range below: `1770114` for the 2026-09-29 review. Read this file before triaging
anything older. Do not re-triage a row here unless new upstream work changes
its verdict.

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

## 2026-11-11: `1770114..c8af562`

227 commits. Tagged `upstream-reviewed-2026-11-11` at `c8af562` on 2026-11-11;
the next review starts from that tag. Ported rows name the PR that carries
them, often several commits to one PR per area. FastMCP 4 stays filed under
#85 with the triggers that reopen it.

| SHA | Subject | Verdict | Reason | Date |
|---|---|---|---|---|
| 29d4c7c | chore(repo): Reduce root directory clutter (#1177) | skip | Upstream repo housekeeping. | 2026-11-11 |
| 28b1690 | chore(repo): Relocate remaining root configs (#1178) | skip | Upstream repo housekeeping. | 2026-11-11 |
| ad428bd | docs(readme): Sharpen the Cadenza sponsor line (#1179) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| abfd7ca | docs(readme): Add contributors wall (#1184) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| a657521 | docs(readme): Add setup note to Codex plugin (#1183) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 58adbbc | docs(contributing): Rewrite the contributing guide (#1182) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| a7c4a57 | chore(agents): Drop CLAUDE.md alias (#1185) | skip | Upstream repo housekeeping. | 2026-11-11 |
| ce1b425 | test(daemon): Compare host quit and a second host (#1180) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 43d5f46 | docs(readme): Describe tools as reading LinkedIn (#1186) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 6eb4d1f | docs(tools): Describe tools as reading LinkedIn (#1187) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 9d6159e | refactor(linkedin): Move the scraping package (#1189) | skip | Upstream package rename to `linkedin/`; we keep `scraping/`. | 2026-11-11 |
| 9ab5f25 | refactor(linkedin): Drop scrape from identifiers (#1190) | skip | Identifier renames that follow 9d6159e. | 2026-11-11 |
| 1c9c232 | fix(cli): Say who holds the profile on --status (#1191) | ported | This run, in #123. | 2026-11-11 |
| cf0d20d | test(daemon): Lose a call in flight (#1192) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 2b1cb6c | test(daemon): Race idle retirement and turnover (#1193) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 231aa61 | test(daemon): Lose the owner before and after dispatch (#1196) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 71553e7 | fix(daemon): Spare the owner's Job infrastructure (#1201) | ported | Windows-only; Windows CI proves it, in #121. | 2026-11-11 |
| f142502 | test(daemon): Run profile commands under an owner (#1197) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 1c89759 | test(daemon): Let ended commands settle under load (#1204) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 2b4d7bd | test(daemon): Repair a lost session through an owner (#1198) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 14c06db | test(differential): Compare the close path as code (#1208) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 176db86 | test(daemon): Keep each election test's exclusion (#1207) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 59c3737 | test(watcher): Keep the sampling loop off the disk (#1202) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| d8181d7 | test(daemon): Keep ineligible configs and rivals Direct (#1199) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| a739848 | test(watcher): Skip the idle process on Windows (#1210) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| b9d7815 | docs(readme): Attach LinkedIn to Classic in the banner (#1206) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 9cddf55 | docs(readme): Drop the changelog pointer (#1211) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 718ff9e | docs(readme): Shorten the Codex plugin section (#1205) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 31bdc7d | docs(changelog): Note the identifier renames (#1212) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 3835c24 | docs(readme): Match the Codex note to the bundle note (#1213) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| bfcf58c | chore(deps): update ruff to v0.16.10 (#1156) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| f0c1469 | fix(messaging): Accept LinkedIn's flagged profile URL (#1214) | ported | This run, in #128. | 2026-11-11 |
| 5436219 | chore: Bump version to 4.26.2 (#1215) | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-11-11 |
| 592ca36 | chore: sync versioned files to v4.26.2 [skip ci] | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-11-11 |
| 1b152ee | test(process-control): Count a reset as the abort (#1218) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| a63bdfd | feat(jobs): Add get_job_apply_url tool (#1035) | ported | Folded into `get_job_details` as an `apply` key, no second navigation, in #127. | 2026-11-11 |
| c0e1458 | test: Synchronise six racing unit tests (#1219) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| cc3625b | ci: Move to stickerdaniel/post-no-bills (#1222) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| 8fe0940 | fix(process-tree): Settle a group that ended late (#1220) | ported | This run, in #121. | 2026-11-11 |
| 27caa1e | test(differential): Read a macOS SystemError as unreadable (#1221) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| d38bdae | fix(setup): Refuse an install the watcher saw breach (#1229) | ported | This run, in #121. | 2026-11-11 |
| 53ff87a | fix(session): Name a foreign-host lock and its fix (#1228) | ported | This run, in #123. | 2026-11-11 |
| 3da596d | fix(daemon): Name a lost owner instead of masking it (#1232) | ported | This run, in #123. | 2026-11-11 |
| 4c0da72 | fix(messaging): Answer before the tool deadline (#1233) | ported | This run, in #128. | 2026-11-11 |
| 7609a66 | fix(setup): Say Ctrl+Z does not pause the download (#1230) | skip | Installer prompt wording; not worth the drift. | 2026-11-11 |
| 46d365b | fix(cli): Keep a session made after logout confirms (#1231) | skip | Race window is only our y/N prompt; needs a handover wait we lack. | 2026-11-11 |
| 17bdfe6 | test(watcher): Judge the lifecycle test on coverage (#1226) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 527349f | test: Synchronise load-dependent process tests (#1225) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| bf1591c | fix(jobs): Stop the sidebar scroll on cancel (#1234) | ported | This run, in #127. | 2026-11-11 |
| cf9a8c6 | test(differential): Compare only the close path (#1237) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 4248ee5 | test(daemon): Keep tests out of the real state root (#1188) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| ddf6a71 | feat(messaging): Send multi-line messages (#1224) | ported | Earlier, our own implementation in #107. | 2026-11-11 |
| a7c8ef1 | docs(readme): Name the limits in the Cadenza banner (#1238) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| c25b68b | test(daemon): Keep daemon tests out of the real home (#1227) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 3a49e50 | test(guardian): Observe the guardian exit before the lease (#1239) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 271c8e9 | fix(navigation): Refuse a landing off LinkedIn (#1235) | ported | Reduced scope, in #122. | 2026-11-11 |
| 6f2f6d4 | test(differential): Record why a first navigation stalls (#1241) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 33c79cf | fix(browser): Restart a browser that stopped (#1240) | ported | Earlier, in #13: restart on `is_connected()` false plus lease-held drain already in main; upstream's extra `_browser_lease` guard covers a profile-root retarget the fork never does. | 2026-11-11 |
| 71c5eb1 | fix(navigation): Time a goto from its request (#1245) | skip | Our goto clock already starts at the call; it would add stacking. | 2026-11-11 |
| 1323393 | feat(messaging): Report the thread a message landed in (#1243) | ported | This run, in #128. | 2026-11-11 |
| 28b62f1 | ci: Pin Post No Bills v3.0.0 (#1244) | skip | Upstream CI and release workflow. | 2026-11-11 |
| fcef952 | docs(readme): End founding offer before CTA (#1246) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| a83bf94 | feat(jobs): Read job tracker stages in get_saved_jobs (#1058) | ported | This run, in #127. | 2026-11-11 |
| c8e9be0 | fix(browser): Restore a rolled-back session (#1251) | ported | Earlier, in #98. | 2026-11-11 |
| 65f4e35 | fix(jobs): Read an Easy Apply that is a button (#1250) | ported | Folded into the a63bdfd `apply` key, in #127. | 2026-11-11 |
| 3252e08 | ci(checks): Run only the CI a change needs (#1253) | skip | Upstream CI and release workflow. | 2026-11-11 |
| a87c158 | feat(jobs): Report applied and closed on job details (#1249) | ported | Folded into the a63bdfd `apply` key, in #127. | 2026-11-11 |
| 89e6cc0 | test(watcher): Accept a skewed sampling cost (#1252) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 2290293 | ci(windows): Keep the evidence of a browser exit (#1242) | skip | Upstream CI and release workflow. | 2026-11-11 |
| b65dd7a | docs(readme): Clarify intro and tool overview (#1254) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 9e5375b | test(differential): Keep a cancelled lineage read (#1256) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| f4d355e | ci(checks): Bound every CI job's runtime (#1257) | skip | Upstream CI and release workflow. | 2026-11-11 |
| ccd028b | test(differential): Settle modelled censuses fast (#1258) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| b71682f | test(differential): Close two evidence gaps (#1260) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 60bb77f | fix(messaging): Exclude compose thread links (#1255) | ported | This run, in #128. | 2026-11-11 |
| f03f377 | chore(deps): lock file maintenance (#964) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| 957b88d | test(process-control): Keep the finalization test out of the real state root (#1266) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 01c2d5e | test(isolation): Keep test artifacts temporary (#1267) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 5cd1882 | ci(release): Follow convention in version sync commit (#1269) | skip | Upstream CI and release workflow. | 2026-11-11 |
| da43e04 | ci(pr): Require capitalized, short title subjects (#1271) | skip | Upstream CI and release workflow. | 2026-11-11 |
| df38e4d | fix(jobs): Bound admitted search pages by the budget (#1261) | ported | Earlier, in #98: idea carried by its per-page `asyncio.timeout` + `page_deadline`; upstream's goto-deadline plumbing depends on unported 71c5eb1. | 2026-11-11 |
| 17ea407 | test: Keep owner spawns off the real profile (#1265) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| f6903b7 | chore: Bump version to 4.27.0 (#1272) | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-11-11 |
| df3cfed | chore(release): Sync versioned files to v4.27.0 [skip ci] | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-11-11 |
| ed62338 | docs(release): Scope the version bump commit (#1273) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 8d47918 | chore(github): Add Ko-fi to funding links (#1274) | skip | Upstream repo housekeeping. | 2026-11-11 |
| 49bd062 | docs(pr): Capitalize the subject placeholder (#1275) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| a617394 | chore(deps): Update CI dependencies (#1040) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| ad00920 | test(daemon): Keep daemon state in a temporary home (#1276) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 3e054b3 | refactor(daemon): Remove predecessor compatibility (#1278) | skip | Upstream daemon package/core refactor; ours is flat and opt-in. | 2026-11-11 |
| 28ba07f | refactor(daemon): Move the daemon into one package (#1279) | skip | Upstream daemon package/core refactor; ours is flat and opt-in. | 2026-11-11 |
| f8dce98 | refactor(daemon): Start the owner over framed stdio (#1280) | skip | Upstream daemon package/core refactor; ours is flat and opt-in. | 2026-11-11 |
| c3c093e | refactor(daemon): Decide each call in a pure core (#1281) | skip | Upstream daemon package/core refactor; ours is flat and opt-in. | 2026-11-11 |
| 0536947 | refactor(daemon): Forward calls through public APIs (#1282) | skip | Upstream daemon package/core refactor; ours is flat and opt-in. | 2026-11-11 |
| f4073e2 | refactor(daemon): Decide the owner lifecycle in a core (#1283) | skip | Upstream daemon package/core refactor; ours is flat and opt-in. | 2026-11-11 |
| 45d54e1 | refactor(daemon): Decide the election in a core (#1284) | skip | Upstream daemon package/core refactor; ours is flat and opt-in. | 2026-11-11 |
| 6ebf18e | test(differential): Read the driver's end from the last sample (#1287) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 023c3fc | test(differential): Judge the black hole at the goto (#1289) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| f1ba6aa | test(differential): Order process ends by wall clock (#1290) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 5bdef1c | docs(daemon): Record the core and trim docstrings (#1291) | skip | Upstream daemon gate records or agent rules; not ours. | 2026-11-11 |
| 3d458d8 | test(daemon): Enumerate the composed reducers (#1292) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 6bf092f | chore(deps): update stickerdaniel/post-no-bills action to v3.1.0 (#1286) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| 4526159 | feat(daemon): Say when the shared owner is silent (#1295) | skip | Wording only; 16 files written against upstream's daemon core we lack. | 2026-11-11 |
| dae6521 | fix(diagnostics): Prune trace runs left behind (#1297) | ported | This run, in #125. | 2026-11-11 |
| a5fe078 | fix(daemon): Keep an answer whose body close fails (#1298) | skip | Fixes httpx2 body close in `forwarder.py`; neither exists here. | 2026-11-11 |
| 07f0ca6 | fix(daemon): Turn over an older owner of any config (#1301) | ported | This run, in #123. | 2026-11-11 |
| a8e30d0 | test(differential): Keep terminal output and held reads under load (#1300) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 4f82b09 | feat(plugins): Add a Claude Code plugin (#1296) | skip | Plugin launches upstream's pinned PyPI package, not this fork. | 2026-11-11 |
| 2dec308 | fix(daemon): Share one browser across login settings (#1304) | ported | Minimal, paired with 07f0ca6; flag removals left out, in #123. | 2026-11-11 |
| ca22166 | fix(installer): Wait out EPERM from a killed group (#1302) | ported | This run, in #121. | 2026-11-11 |
| 5ed548e | docs(agents): Reproduce timing flakes at the seam (#1306) | skip | Upstream daemon gate records or agent rules; not ours. | 2026-11-11 |
| 24e9bf9 | fix(daemon): Delete unused code and catalog forwarding (#1311) | skip | Cleanup of upstream's daemon package; ours is flat and opt-in. | 2026-11-11 |
| 2d6e0b9 | test(differential): Race a contender against a kill (#1299) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 5a18a89 | docs(daemon): Record the Windows crash overlap (#1312) | skip | Upstream daemon gate records or agent rules; not ours. | 2026-11-11 |
| 82b9856 | refactor(daemon): Drop branches no caller reaches (#1313) | skip | Upstream daemon package/core refactor; ours is flat and opt-in. | 2026-11-11 |
| c342bdb | refactor(daemon): Accept loopback literals only (#1314) | skip | Upstream daemon package/core refactor; ours is flat and opt-in. | 2026-11-11 |
| be709c5 | test(differential): Release the grandchild from the hook (#1307) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| b2bf37a | test(browser): Judge the wedged stop by its events (#1308) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 886cd99 | test(bootstrap): Drop the clock from thread timeouts (#1309) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 4e61ce7 | fix(messaging): Keep a confirmed send past the budget (#1305) | ported | This run, in #128. | 2026-11-11 |
| a4458f3 | test(daemon): Bound the planted FIFO by a hang (#1316) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 1472319 | test(installer): Expire the lifetime once installing (#1315) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 4059f39 | test(bootstrap): Arm the setup deadline after readiness (#1310) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 3cf6171 | fix(daemon): Refuse a swapped FIFO without blocking (#1317) | ported | This run, in #121. | 2026-11-11 |
| ec89257 | refactor(daemon): Move the spawn adapter out (#1318) | skip | Upstream daemon package/core refactor; ours is flat and opt-in. | 2026-11-11 |
| b58cb34 | docs(agents): Adopt the post-no-bills attribution rule (#1319) | skip | Upstream's PR attribution rule; this fork forbids attribution. | 2026-11-11 |
| d51933e | feat(cursor): Add Cursor plugin support (#1268) | skip | Editor plugin pinned to upstream's package. | 2026-11-11 |
| b18083a | docs(cursor): Fix the Add to Cursor config (#1321) | skip | Editor plugin pinned to upstream's package. | 2026-11-11 |
| d9776ad | feat(plugins): Run the latest server release (#1322) | skip | Editor plugin pinned to upstream's package. | 2026-11-11 |
| 8dc30e7 | fix(daemon): Prove the pinned hop at discovery (#1327) | skip | Pinned-hop proof in `forwarder.py`, which does not exist here. | 2026-11-11 |
| 7af6b36 | test(process): Order the delayed launch by the lease (#1328) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| a3dcf62 | refactor(daemon): Admit calls only once published (#1330) | skip | Upstream daemon package/core refactor; ours is flat and opt-in. | 2026-11-11 |
| a9636e7 | refactor(daemon): Drop the daemon log race recovery (#1331) | skip | Upstream daemon package/core refactor; ours is flat and opt-in. | 2026-11-11 |
| 2dfd474 | test(process): End the crash guardian in teardown (#1329) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| dd37303 | test(daemon): Give the owner scratch output streams (#1332) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| d05cd12 | test(differential): Run the frozen witnesses at the pin (#1333) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 3f5d5c7 | docs(decisions): Scope daemon release gate 1 (#1335) | skip | Upstream daemon gate records or agent rules; not ours. | 2026-11-11 |
| 8c5c483 | test(daemon): Witness a lost auth marker response (#1336) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 558f4dd | fix(daemon): Stop claiming a lost answer's owner left (#1337) | skip | Daemon owner-loss wording on upstream's core; daemon is opt-in here. | 2026-11-11 |
| 42aa484 | test(differential): Compare a held empty trace marker (#1338) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 4e58259 | docs(decisions): Leave HTTP on Windows out of gate 1 (#1339) | skip | Upstream daemon gate records or agent rules; not ours. | 2026-11-11 |
| 758d67a | test(differential): Repeat H-R6, H-R11 and H-R12 (#1340) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 61b3f6a | test(differential): Relaunch a staging browser that never sends (#1344) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 2124ff6 | test(differential): Close a slow writer after its file (#1346) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 8ed46dc | test(differential): Break stdout before a pipe loss's EOF (#1347) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| b833df2 | test(differential): Release a late loss after its window (#1345) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| f39a289 | test(differential): Exclude a zombie reaped mid-census (#1348) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 4a513ae | test(differential): Drop the stall's runner attribution (#1350) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 9253ea0 | docs(daemon): Record the Gate 1 evidence (#1349) | skip | Upstream daemon gate records or agent rules; not ours. | 2026-11-11 |
| a7e8044 | test(differential): Order a zombie root by its read (#1351) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| c329cc4 | test(daemon): Add the shared-browser pilot recorder (#1353) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| ed3b4dc | docs(daemon): Decide the Windows check and pilot rules (#1352) | skip | Upstream daemon gate records or agent rules; not ours. | 2026-11-11 |
| 91329ef | ci(checks): Name matrix jobs without expressions (#1355) | skip | Upstream CI and release workflow. | 2026-11-11 |
| 4c1a940 | fix(process): Wait for a slow ps in the crash guardian (#1370) | ported | This run, in #121. | 2026-11-11 |
| 9e0102e | chore(deps): Update aiohttp for security fixes (#1371) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| 89455ff | docs(readme): Remove the sponsored proxy providers (#1373) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 5a29bbf | fix(process): Let a slow ps prove a browser drain (#1368) | ported | Hand-merged; registration moved off the loop with it, in #121. | 2026-11-11 |
| baf2439 | fix(tools): Redact shaped errors in search tools (#1375) | ported | Search-tool half was earlier in #98; the rest, in #120. | 2026-11-11 |
| f51faed | test(differential): Start the idle clock when a call ends (#1374) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| c76f8b4 | refactor(tools): Share one error envelope (#1376) | skip | Error-envelope refactor across drifted tool modules. | 2026-11-11 |
| 3c203d5 | fix(auth): Stop calling a checkpoint a rate limit (#1379) | ported | Earlier, in #98. | 2026-11-11 |
| 62d7239 | chore: Update post-no-bills to v4.0.0 (#1399) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| cd46c2d | chore(deps): Update virtualenv and OpenTelemetry (#1400) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| b841031 | chore(release): Bump version to 4.28.0 (#1401) | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-11-11 |
| 7a40310 | chore(release): Sync versioned files to v4.28.0 [skip ci] | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-11-11 |
| 0c5f14b | docs(jobs): Say what the redesigned search keeps (#1362) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| a943666 | fix(deps): Raise the fastmcp floor to 4.1.2 (#1408) | filed | #85: FastMCP 4 deferred until a 3.x fix lands only on 4.x, a client drops pre-2026-07-28 protocol, #90 is adopted, or 3.x gets an EOL. | 2026-11-11 |
| 8d8861a | feat(docker): Let the login viewer paste text (#1414) | ported | This run, in #129. | 2026-11-11 |
| 07c160e | chore(deps): Update setuptools to v84.2.1 (#1412) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| 5224d05 | chore(deps): Update Ruff to v0.17.3 (#1285) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| 8ced54c | fix(browser): Restart a browser whose page crashed (#1382) | ported | This run, in #121. | 2026-11-11 |
| e00e74e | feat(session): Add a session status tool (#1392) | skip | Optional; upstream itself says not to preflight. Revisit on demand. | 2026-11-11 |
| 09ff15b | docs(readme): Shorten the session status row (#1415) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| a48e21c | feat(jobs): Report why search_jobs stopped paging (#1323) | ported | This run, in #127. | 2026-11-11 |
| 0dcffd0 | fix: Harden sidebar links and issue diagnostics (#1417) | ported | This run, in #125. | 2026-11-11 |
| ecd25f2 | chore(deps): Update authlib, dnspython and filelock (#1418) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| 5abdba3 | feat(tools): Pair feed post references with their posts (#1409) | skip | Today's feed exposes no post boundaries; recovering URNs needs decoding opaque internal keys (minimize-DOM rule). Ported then reverted after live measurement 2026-11-11. | 2026-11-11 |
| 3801ee0 | chore(deps): update ci dependencies (#1366) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| 048a7e0 | feat(mcpb): Expose CHROME_PATH as a bundle setting (#1303) | ported | This run, in #120. | 2026-11-11 |
| eeabfd8 | fix(jobs): Detect no-match on the redesigned search (#1363) | ported | This run, in #127. | 2026-11-11 |
| aa9ee81 | feat(person): Add volunteering section (#1357) | ported | This run, in #126. | 2026-11-11 |
| 0ed4d80 | fix(search): Filter people search by geo URN (#722) | skip | Superseded: we resolve free-text locations to geo ids already. | 2026-11-11 |
| d4cad45 | feat(tools): Filter search_posts by content type (#1377) | ported | This run, in #126. | 2026-11-11 |
| 4bc291e | feat(errors): Name a stable code on failed calls (#1390) | skip | Stable error codes serve programmatic clients; ours read text. Large. | 2026-11-11 |
| 6aff215 | feat(errors): Name throttling in a failed call (#1264) | skip | Per-call throttle listener; the pacing cooldown covers the harm. | 2026-11-11 |
| c8c2a0d | docs(readme): Drop the error code table (#1419) | skip | Drops a README table for 4bc291e, which is skipped. | 2026-11-11 |
| 3043e66 | feat(messaging): Add archive_conversation tool (#1354) | skip | Inbox-tidy write on the most volatile UI, no pacing kind. | 2026-11-11 |
| 6745382 | feat(jobs): Add dismiss_job tool (#1326) | skip | Write that trains the recommender, no pacing kind, low value. | 2026-11-11 |
| b8922f3 | feat(jobs): Add get_easy_apply_form tool (#1343) | skip | Fails on current postings upstream; saves drafts, form-walk pattern. | 2026-11-11 |
| 59d05a6 | test(daemon): Ignore stray probes on the fake proxy (#1422) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 975cee6 | chore(deps): Update ty to 0.0.94 (#1421) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| f45c413 | feat(linkedin_mcp_server): Send usage rules as server instructions (#1402) | ported | Our own instruction text, not upstream's, in #120. | 2026-11-11 |
| c7afa71 | chore(release): Bump version to 4.29.0 (#1423) | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-11-11 |
| 17be1db | chore(release): Sync versioned files to v4.29.0 [skip ci] | skip | Upstream version bump or versioned-file sync; this fork numbers its own releases. | 2026-11-11 |
| d8a02a3 | feat(messaging): Add reply_to_conversation tool (#1342) | ported | This run, in #128. | 2026-11-11 |
| 77f8409 | test(readme): Keep tool descriptions on one line (#1438) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| b5e65ae | docs(readme): Add LinkupAPI as the top sponsor (#1435) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| bb21b7a | chore(deps): Bump joserfc to 1.7.7 (#1440) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| fbe81ce | test(jobs): Hold the dialog until the read is cancelled (#1443) | skip | Test for b8922f3, which is skipped. | 2026-11-11 |
| c0cc8bd | test(jobs): Keep a discarded Easy Apply dialog closed (#1441) | skip | Test for b8922f3, which is skipped. | 2026-11-11 |
| b1f18e9 | test(jobs): Expire the read deadline after Next (#1444) | skip | Test for b8922f3, which is skipped. | 2026-11-11 |
| 7787ece | test(jobs): Cover the Easy Apply close shield (#1445) | skip | Test for b8922f3, which is skipped. | 2026-11-11 |
| a3aba85 | docs(readme): Add Zernio as a sponsor (#1442) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| cd5a44a | ci(release): Group new contributors on one line (#1434) | skip | Upstream CI and release workflow. | 2026-11-11 |
| 69fb6d5 | ci(readme): Require before and after screenshots (#1446) | skip | Upstream CI and release workflow. | 2026-11-11 |
| c9410fd | ci(readme): Explain how to upload the screenshots (#1452) | skip | Upstream CI and release workflow. | 2026-11-11 |
| b4e27fb | ci(readme): Move the screenshot guide to docs (#1457) | skip | Upstream CI and release workflow. | 2026-11-11 |
| 8c3bb5a | test(timing): Keep timing tests stable under load (#1460) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| aef4b3c | docs(readme): Point send_message at the reply tool (#1439) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 6df9228 | docs(notice): Shorten project disclaimers (#1468) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 48e08f1 | chore: Update post-no-bills to v4.1.0 (#1469) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| c2f22a0 | chore(deps): Bump fastmcp to 4.1.7 (#1470) | filed | #85: FastMCP 4 deferred until a 3.x fix lands only on 4.x, a client drops pre-2026-07-28 protocol, #90 is adopted, or 3.x gets an EOL. | 2026-11-11 |
| f3d3729 | ci(release): Sync versions before merge (#1473) | skip | Upstream CI and release workflow. | 2026-11-11 |
| 8580537 | docs(contributing): Pin PR media and disclosure names (#1472) | skip | Upstream README, notice or contributor docs. | 2026-11-11 |
| 488394e | test(differential): Skip a staging run that ended first (#1474) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 993143f | feat(tools): Load optional tools from a remote catalog (#1475) | skip | Loads tool definitions from a remote catalog at runtime; security risk. | 2026-11-11 |
| ebfd16f | chore(deps): update ruff to v0.17.4 (#1466) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| d8d3394 | chore(deps): update actions/checkout action to v7 (#1467) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| 1ec1120 | chore(deps): update ci dependencies (#1465) | skip | Upstream dependency or action update; our Renovate tracks our own. | 2026-11-11 |
| 5886cb6 | test(differential): Bound sampler waits by its reads (#1476) | skip | Upstream test harness for code or races not ported here. | 2026-11-11 |
| 73b2ee7 | fix(config): Refuse --port 0 like PORT=0 (#1447) | ported | This run, in #120. | 2026-11-11 |
| 58d3d16 | fix(config): Refuse an invalid LOG_LEVEL (#1461) | ported | This run, in #120. | 2026-11-11 |
| e288072 | fix(tools): Refuse bad input before the browser (#1429) | ported | Only the search_posts residual; the rest was #80, in #126. | 2026-11-11 |
| 4e52a07 | fix(connection): Report pending from the More menu (#1425) | ported | With the false-pending fix, in #124. | 2026-11-11 |
| ec3df20 | fix(config): Let --slow-mo 0 override SLOW_MO (#1431) | ported | This run, in #120. | 2026-11-11 |
| 5c50810 | fix(jobs): Accept a job id from a job search link (#1437) | ported | This run, in #127. | 2026-11-11 |
| c8af562 | fix(config): Refuse an unreadable on/off setting (#1479) | ported | Adapted to our five on/off settings, in #120. | 2026-11-11 |
