# Sales Navigator inbox read-state preservation

`get_sales_navigator_inbox`, `get_sales_navigator_conversation` and
`search_sales_navigator_conversations` are read-only from LinkedIn's
perspective. They must leave every conversation's read state as it was: no
mark-read, no seen receipt, no compensating mark-unread.

## Why a guard and not avoidance

Unlike `/messaging/`, the Sales Navigator inbox cannot be read without opening
a conversation. Every inbox view, every filter and every search runs the app's
own transition to its top conversation, which loads that thread's messages,
its participant's `salesApiProfiles` record (contact details included),
`seenReceipts` and insights. The app then marks the thread read once its newest
message is in view, with `POST salesApiMessagingThreads/<id>?action=markAsRead`.
Listing threads alone does not mark anything read; opening one can.

So the reads cannot rely on not opening threads. They rely on the read-state
guard (`scraping/messaging_read_state.py`): its routes, registered on every
page at creation, include every POST under `/sales-api/salesApiMessagingThreads`
(mark read or unread, the archive patch, seen receipts) and under
`/sales-api/salesApiMessageActions` (sends). While a passive read has latched
the guard, those POSTs are aborted in the browser. Nothing else is: the app's
boot, coach, heartbeat and telemetry POSTs pass, since blocking them protects
nothing and makes the session look unlike a browser's.

## How it works

- The conversation list, its filters and its scrolled pages come from the
  page's own `salesApiMessagingThreads?q=filter` responses (a time cursor,
  loaded by scrolling the list). Search types into the inbox's own search box,
  because no URL carries it, and reads `q=keyword` responses for exactly the
  typed words.
- One conversation is its route, `/sales/inbox/<thread id>`, read from the
  single-thread response (the newest 10 messages). Older messages repeat the
  page's own `.../messages?deliveredBefore=` request with the page's own
  headers, at most three batches. That replay is the one request these tools
  make themselves.
- A `lead_id` is matched only against the participants' `fs_salesProfile` ids
  in the list data, never by name, in the 200 most recent conversations.
- The signed-in member is a participant of every thread and is told apart by
  `degree` 0.
- Every result carries `marked_read`: true only when LinkedIn answered a
  mark-read during the call. With the guard on, that cannot happen; it is the
  evidence, not a promise. If a thread could only be read by marking it read,
  the owner's decision is to read it and report `marked_read: true`.

## Failure behavior and limits

- A response in a shape the builders do not recognise refuses the whole read
  rather than returning part of it.
- A list that stops loading, cannot be scrolled or runs out of time returns
  what it read with `paging.reason` and `section_errors` (`page_limit`).
- A refused older-messages batch keeps the messages already read and reports
  `history.complete: false`.
- The participant-profile fetch of the conversation the app opens is the app's
  own behaviour and is not blocked; it is a data request, not a lead-page view.

## Measurement record

Measured 2026-11-24 against the owner's account, headless, through the
pacing ledger, by calling the reader directly (outside the tool middleware):

- An unread-view read (40 rows): the app sent one `markAsRead` POST for the
  thread it opened; it was aborted. The three tracked unread threads stayed
  unread, with the same unread counts, on the next read.
- A 5-row and a 45-row inbox read: rows built for every conversation, ids
  unique across scrolled pages, `marked_read` false, no messaging write left
  the browser.
- Reading an unread conversation by thread id: the right thread, messages
  oldest first, the member's own messages flagged, the thread still unread in
  the response, no mark-read request sent at all.

These are provider behavior, not a public LinkedIn API guarantee; the guard
and the fail-closed checks above are the permanent contract.
