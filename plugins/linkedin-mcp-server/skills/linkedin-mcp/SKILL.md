---
name: linkedin-mcp
description: Use LinkedIn data for profile, company, job, post, feed, inbox, recruiting, and outreach work through the bundled LinkedIn MCP server. Use only for an explicit LinkedIn request.
---

# LinkedIn MCP

Use the bundled `linkedin` MCP server only when the user explicitly asks for
LinkedIn data or an action on LinkedIn. Do not call LinkedIn tools for unrelated
work, health checks, or background maintenance.

## Operating rules

- Start with the smallest read operation that can answer the request.
- Treat profile, company, job, post, feed, and message results as live LinkedIn
  evidence. Distinguish retrieved facts from inference.
- Keep searches and result pages modest. Do not bulk scrape or spam.
- Never enable the plugin or its MCP server, edit Codex configuration, or start
  a login flow merely because LinkedIn might be useful. If either component is
  disabled, explain that state and stop.
- `send_message`, `reply_to_conversation`, `send_sales_navigator_message`,
  `reply_to_sales_navigator_conversation` and `connect_with_person` are write
  actions. A Sales Navigator InMail also spends a credit, and a
  send whose cost is unknown may: set `spend_inmail_credit` only when the user
  approved spending one. Use them only when the user explicitly authorizes the
  exact recipient and action (for a reply, the exact thread). Confirm the
  final message or connection note unless the user has already supplied it.
- `invite_to_follow_page` invites people to follow a page as the user. Call it
  with `confirm_send` false first, show the user who would be invited and the
  credits it uses, and confirm only the people they approved.
- `update_campaign`, `set_campaign_status`, `update_campaign_targeting`,
  `create_campaign_draft`, `add_post_to_campaign`,
  `create_company_list_audience` and `create_saved_audience` change the
  user's ad account. Call them with `confirm_send` false first, show the user
  the planned change, and confirm only what they approved. Confirming
  `create_company_list_audience` accepts LinkedIn's Ads Agreement for the
  upload; say so before confirming. None of them launches a campaign or
  touches billing.
- On an ad set that targets the EU, saving asks whether its ads are political
  (`declaration_required`, or `planned.politicalAdQuestion` on a dry run;
  `create_campaign_draft` too).
  That answer is a legal declaration the user makes for the advertiser: ask
  them, and pass `political_ad` true or false only as they declared it. Never
  choose it yourself.
- `declaration_would_clear`, `declaration_cleared_by_linkedin` and
  `declaration_not_offered` mean Campaign Manager would have changed that
  declaration on its own (it clears it for targeting without an EU location;
  its newer editor only saves "not political"). Nothing was sent; tell the
  user what the message says instead of retrying.
- Do not retry a failed write action unless the result proves it was not sent.

## Browser and authentication

The server uses a managed Chromium browser and the user's own LinkedIn session.
Installing or enabling the plugin does not sign in. The first LinkedIn data
request may prepare the browser, import an existing local session, or require a
visible login window. If LinkedIn presents a captcha, two-factor prompt, or
other user-owned authentication step, stop and ask the user to complete it.

If browser setup or authentication is still in progress, report that exact
state and retry once after it finishes. Do not loop, launch extra browser
instances, clear profiles, or replace the user's browser session.

## Tool selection

- Profiles: `get_my_profile`, `get_person_profile`, `search_people`, and
  `get_sidebar_profiles`. `search_people` takes Boolean `keywords` and
  optional `location`, `network`, `current_company`, `past_company`
  (names, slugs or ids; lists accepted), `title`, `industry`, `school`
  (numeric id only), `first_name`, `last_name` and `profile_language` facets
  plus `max_pages`; every page is one navigation, and each company name that
  is not already cached costs one or two more. LinkedIn ignores `title` on
  its current results page: put the title in `keywords` as a quoted phrase.
  `title` as the only criterion is refused rather than returning an
  unfiltered list. Only `current_company` and `first_name` are verified
  live; `past_company`, `industry`, `school`, `last_name` and
  `profile_language` are unverified against live LinkedIn, and a wrong
  parameter name is ignored, so cross-check results.
  The result carries `people` rows (`name`, `degree`, `headline`, `location`,
  `snippet`, `url`) parsed from the page text and a `result_count`; `url` is
  null when a card could not be paired with a profile link, so keep the raw
  text for anything a row does not carry.
- Companies: `get_company_profile`, `get_company_posts`, `search_companies`,
  and `get_company_employees`. `search_companies` takes optional `industry`,
  `size`, `hq_location` and `has_jobs` facets plus `max_pages`; narrow there
  first so `enrich_companies` only spends navigations on the shortlist.
  `industry` and `size` are unverified against live LinkedIn, and a wrong
  parameter name is ignored, so cross-check results. The
  result carries `companies` rows (`name`, `industry`, `location`, `tagline`,
  `followers`, `url`) parsed from the page text and a `result_count`.
- Company research at scale: `enrich_companies` (paced, cache-first; pass
  `about=true` for About-tab facets at one extra navigation per company),
  `enrich_company_deep` for one company's facets plus open roles,
  `get_company_cache` to read a record, and `query_company_cache` to filter
  the cache by industry, headquarters, headcount, hiring, or founding year
  without touching LinkedIn.
- Jobs: `search_jobs`, `get_saved_jobs`, and `get_job_details`.
- Content: `get_feed` and `search_posts`.
- Messages: `get_inbox`, `get_conversation`, and `search_conversations`.
- Campaign Manager (read-only): `list_ad_accounts`, `list_campaigns`,
  `get_campaign`, `get_campaign_analytics`, `search_targeting_entities`,
  `list_audiences` and `list_sponsorable_posts` (the post URNs
  `add_post_to_campaign` takes; a feed activity id is not one). An API
  campaign group is the UI's "Campaign", an API
  campaign its "Ad set" and a creative its "Ad".
- Sales Navigator (read-only, needs a Sales Navigator seat):
  `search_sales_navigator_accounts`, `search_sales_navigator_leads`,
  `get_sales_navigator_account`, `list_sales_navigator_account_lists`,
  `get_sales_navigator_account_list`, and the inbox reads
  `get_sales_navigator_inbox`, `get_sales_navigator_conversation` (by
  `thread_id`, or `lead_id` as lead search returns it) and
  `search_sales_navigator_conversations`, which leave unread conversations
  unread.
- Company pages you administer: `list_page_follow_candidates` (read-only)
  and `invite_to_follow_page`.
- Writes: `send_message`, `reply_to_conversation` (answer an existing thread,
  InMail included, by its thread ID), `send_sales_navigator_message` (a lead
  by lead id; dry run first to see the InMail cost),
  `reply_to_sales_navigator_conversation` and `connect_with_person`, and the
  Campaign Manager writes (`update_campaign`, `set_campaign_status`,
  `update_campaign_targeting`, `create_campaign_draft`,
  `add_post_to_campaign`, `create_company_list_audience`,
  `create_saved_audience`), subject to the explicit authorization rules above.
  A Campaign Manager result with `retry_safe` false may have been saved: ask
  the user to check Campaign Manager before calling again.
- Cleanup: use `close_session` only when the user asks to end the managed
  browser session or when the current LinkedIn task is finished and no follow-up
  call is expected.
