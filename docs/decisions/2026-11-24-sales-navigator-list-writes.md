# Sales Navigator account-list writes: the create through the app's form, the add and the delete shaped like the app's request

- Date: 2026-11-24
- Supersedes: none

Every write in this project lets the app send its own request, driven
through its UI, and a page-scoped guard window admits that request only when
it is exactly the planned one, with a read-back where the target can be read
(the Campaign Manager writes). The Sales Navigator account-list writes follow
that for the create and depart from it for the add and the delete.

## Create: the app's own form

The account-lists page (`/sales/lists/company`) opens its create-list form
from `button[data-x--list-nav--create-list-dropdown-trigger]`; the form's
name field is `input[data-x--text-input-field]` (maxlength 75), its
description a `textarea` (maxlength 350), and its Create button
`button[data-x--lists--create-modal-save-button]`. Every selector is an
untranslated `data-x` attribute. In the 2026-11-24 recon, typing a name key
by key and pressing Create sent exactly one request,
`POST /sales-api/salesApiLists` with `{"name", "listType": "ACCOUNT", "role":
"OWNER"}` (aborted by the recon's guard). A value set in one input event
(Playwright `fill`) left the Create button disabled, so the tool types key by
key. The tool admits that request only when it is exactly its plan, reads the
new id from the answer's text in Python, then loads the new list's page and
reports `created` only when that page shows an account list of that name.

## Add and delete: a request shaped like the app's

`add_accounts_to_sales_navigator_list` sends `POST
salesApiCompanies?action=save` with `{"companyIds": [int...], "lists":
["<list id>"]}`, the body of the app's `companyActions.saveAccounts(ids,
lists)`. `delete_sales_navigator_account_list` sends `DELETE
salesApiLists/<id>?unsaveEntity=false&doUnsaveLeadsUnderAccount=false`, the
app's `lists.delete` with its default of keeping the accounts saved. Both go
from the page with the headers of the app's own Sales Navigator GETs, still
through `sales_navigator_list_guard`'s window, which admits only that one
request and aborts every other write while the tool holds the page.

Why not the app's controls:

- The app sends Save to list only from a list picker on accounts a page is
  showing: one account's page, or a page of search or list results. An
  arbitrary set of company ids has no page that sends them as one request,
  and driving the picker account by account costs a page load and a write
  each, 25 where the tool spends one.
- The delete: the recon looked only at the list hub, where the delete is
  behind a row menu and a confirmation dialog. The row menu's trigger
  carries an untranslated attribute (`data-x--list-hub--action-menu--trigger`),
  but the recon recorded neither the menu's items nor the dialog, and the hub
  shows 25 lists per page, so no locale-free path to the delete is known
  there. The list's own page (`/sales/accounts/dashboard?listId=<id>`), which
  the tool already loads to check the name, was not examined for a delete
  control at all.

## How each write is confirmed

- Create: Sales Navigator's answer names the new id, and the list's page then
  shows an account list of that name.
- Delete: a 2xx answer, then one more load of the list's page, which must be
  refused for that list id (the reader's `_Refusal` on `salesApiLists/<id>`,
  a 403/404 with no OK answer for the same id). A page that still loads the
  list, or does not answer either way, is `delete_unconfirmed`.
- Add: Sales Navigator's own per-account answer is the confirmation. The save
  action answers `{value: {listEntityActionResponses, failed, successful,
  status}}` (measured live 2026-11-24, HTTP 200, no `x-restli-id`). Each
  `listEntityActionResponses` entry is `{entity, entityUnion: {organization},
  list, status}` with `entity` an `urn:li:organization:<id>` (the same number
  as the account's `urn:li:fs_salesCompany:<id>`), `list` the
  `urn:li:fs_salesList:<id>` and `status` `SUCCESS`, `CONFLICT` or a failure.
  `successful` holds `{companyUrn}` records and the top-level `status` is the
  whole call's; neither is per list, so neither adds an account. An account
  the entries do not name stays `unconfirmed`. The first version read `entity`
  as an `fs_salesCompany` URN only, so a live add that landed was reported
  `add_unconfirmed`.
- Add, when a 2xx answer names no account at all: the list is read once (its
  first page, one charged page load, up to 25 rows) and each account found
  there is `added`, with a `detail` saying it was confirmed by read-back. An
  account not found, or a read-back that fails, stays `unconfirmed`. Only
  this case pays for the read: an answer that names the accounts is enough,
  and a full read costs a page load per 25 rows. The list's `entityCount` is
  not used: it is one number for the whole list, so it cannot say which
  accounts went in, `CONFLICT` accounts leave it unchanged, and another writer
  (the member in another tab) moves it at the same time.

## The `lists` element is a string

`salesApiLists` answers each list's `id` as a JSON string, and the app's
`_saveAccountsToList` passes the picked list's id on unchanged (`lists =
listId ? [listId] : []`), so the tool sends a string. The live add of
2026-11-24 that sent the id as a JSON number was answered HTTP 400; that body
was serialised in Python, so the id's digits were exact and JavaScript
precision was not the cause. Whether the string is what Sales Navigator
needs was not yet re-checked live.

## Revisit

Switch the add or the delete to the app's control once a recon records a
locale-free control that sends the planned request. The first target is the
list's own page (`/sales/accounts/dashboard?listId=<id>`): record its controls
for a delete (and for adding accounts) before looking anywhere else.
