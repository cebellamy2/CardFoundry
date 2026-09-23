# Diagnostics

Raw evidence from approved, single-use diagnostics run directly against
live Mana Pool production data. Each file is the complete, unedited log of
one run: the request payloads sent, the marketplace responses received
verbatim, seller- and buyer-side readbacks, and the restore step that put
the listing back as it was found.

## Why this is not `audits/`

`audits/README.md` scopes that directory to `production-*.json` files and
states plainly:

> Diagnostic logs that contain API payloads, marketplace responses,
> credentials, or customer/order information do not belong here and must
> not be committed.

Every file here contains API payloads and marketplace responses, so
`audits/` is the wrong home for them by its own policy — a policy that
holds across all 23 files currently in that directory, none of which
contains any of it. These were sitting in the repository root instead,
committed without a home; this directory gives them one rather than
either weakening that policy or deleting real evidence of a production
write.

The distinction is worth keeping straight:

| | `audits/` | `diagnostics/` |
|---|---|---|
| Content | sanitized summaries | raw, unedited run logs |
| Includes API payloads / responses | never | yes, that is the point |
| Naming | `production-*.json` | named after the script that wrote it |
| Purpose | what a completed operation did | what the marketplace actually did |

## Rules

Same immutability rule as `audits/`: **do not edit a historical
diagnostic.** If a run needs correcting, keep the original and add the new
run alongside it — that is exactly why the failed `20260813` zero run is
still here next to its `_rerun`. The failure is the evidence.

Before committing anything here, confirm it carries no credentials,
access tokens, `Authorization` headers, or any third party's customer or
order data. Product identifiers, the store's own listings, quantities,
prices, timestamps and verbatim marketplace responses about the store's
own inventory are all fine. The three files here were checked against
that bar: zero occurrences of token, authorization, api_key, Bearer,
email or address.

## Contents

The 2026-08-13 Mana Pool quantity round-trip series, against one listing
(`Aatchik, Emerald Radian`, DFT 187, LP, NF — inventory id
`51ff0a6b…`), establishing how Mana Pool handles a quantity write and a
write to zero:

- `quantity_write_diagnostic_aatchik_20260813.json` — 2 → 1 → 2. Succeeded.
- `quantity_zero_diagnostic_aatchik_20260813.json` — 2 → 0 → 2, **first
  attempt, which failed.** Carries `diagnostic_error` and `restore_error`.
  Kept deliberately: a production write that errored is the most useful
  file in this directory.
- `quantity_zero_diagnostic_aatchik_20260813_rerun.json` — the successful
  retry, including a `zero_buyer_behavior` observation of how a
  zero-quantity listing appears to buyers.

Their producing scripts are `quantity_write_diagnostic_aatchik.py` and
`quantity_zero_diagnostic_aatchik.py` at the repository root. Both write
here now, and both refuse to start if their log file already exists — so
neither can overwrite the evidence, and neither is casually re-runnable
against production. That refusal is a feature, not a bug: each run
performs real writes against a live listing.
