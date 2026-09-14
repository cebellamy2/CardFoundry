"""One-time cleanup (operator-approved 2026-09-14): move every token,
emblem and marker card out of the colour-identity legacy batches and into
a single dedicated Tokens batch.

Background: nothing in this app has ever had a concept of a token.
InventoryCard stores no layout/type_line/card-type column at all, so
"which rows are tokens" cannot be answered locally -- it is resolved here
against Scryfall, at run time, never from a stored snapshot.

IDENTIFICATION -- deliberately NOT a set-code prefix heuristic. Measured
against real production inventory on 2026-09-14, a `set_code LIKE 'T%'`
rule matched 1,144 rows of which only 57 were genuinely tokens: 1,087
false positives (TDM Tarkir: Dragonstorm, THS Theros, TSP Time Spiral,
TMP Tempest and friends are real sets). It also misses tokens whose set
code does not start with T (Scryfall has L12-L17, PTBRO, SBRO, SKHM,
SMOM...). Instead this asks Scryfall's /sets endpoint which sets are
set_type == "token" (one call, authoritative), then verifies every
candidate printing's own `layout` is token/emblem (one batched call).
Both checks must agree before a row is eligible.

Note on Scryfall load: an earlier attempt to verify all 6,552 distinct
printings in 88 batched calls tripped a 429 even with the shared pacer.
This approach needs two calls total.

WRITES go exclusively through the app's own guarded HTTP routes --
POST /batches and POST /inventory-cards/bulk-move-batch -- never a direct
batch_id UPDATE. That matters: the bulk-move route refuses any card that
is not `available`, all-or-nothing, precisely because consignment status
lives at the batch level and moving an already-sold card would
retroactively reattribute that sale to a different consignor. Two sold
consigned "Galactus" rows are exactly that case. This script relies on
that guard rather than reimplementing or working around it.

Dry-run by default (report only). Pass --confirm to actually write.
"""

import argparse
import base64
import collections
import json
import os
import sqlite3
import sys

import httpx

TOKEN_LAYOUTS = ("token", "double_faced_token", "emblem")
DEFAULT_BATCH_CODE = "Tokens"
SCRYFALL_SETS_URL = "https://api.scryfall.com/sets"
SCRYFALL_HEADERS = {"User-Agent": "CardFoundry/token-cleanup", "Accept": "application/json"}


def _db_path() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return "/data/cardfoundry.db"


def _base_url() -> str:
    explicit = os.environ.get("CARDFOUNDRY_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"


def _auth_header() -> dict:
    """Basic auth built straight from the container's own env var. The
    password is never printed, logged, or returned by this script."""
    password = os.environ.get("CARDFOUNDRY_ADMIN_PASSWORD", "")
    if not password:
        return {}
    raw = base64.b64encode(f"cleanup:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def token_set_codes(client: httpx.Client) -> set[str]:
    response = client.get(SCRYFALL_SETS_URL, headers=SCRYFALL_HEADERS, timeout=45)
    response.raise_for_status()
    return {
        str(entry["code"]).upper()
        for entry in response.json().get("data", [])
        if entry.get("set_type") == "token"
    }


def verify_layouts(client: httpx.Client, scryfall_ids: list[str]) -> dict:
    """Per-printing layout, in batches of 75 (Scryfall's collection cap)."""
    resolved = {}
    unique = sorted(set(scryfall_ids))
    for start in range(0, len(unique), 75):
        chunk = unique[start:start + 75]
        response = client.post(
            "https://api.scryfall.com/cards/collection",
            headers={**SCRYFALL_HEADERS, "Content-Type": "application/json"},
            json={"identifiers": [{"id": sid} for sid in chunk]},
            timeout=45,
        )
        response.raise_for_status()
        for card in response.json().get("data", []):
            if card.get("id"):
                resolved[card["id"]] = card
    return resolved


def gather(client: httpx.Client) -> dict:
    connection = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    rows = connection.execute(
        """
        select ic.id, ic.name, upper(ic.set_code), ic.collector_number, ic.scryfall_id,
               ic.status, b.batch_code, b.id, b.is_consignment
        from inventory_cards ic join batches b on b.id = ic.batch_id
        """
    ).fetchall()

    sets = token_set_codes(client)
    candidates = [r for r in rows if (r[2] or "") in sets and r[4]]
    layouts = verify_layouts(client, [r[4] for r in candidates])

    eligible, rejected_layout, excluded = [], [], []
    for row in candidates:
        card = layouts.get(row[4])
        layout = str((card or {}).get("layout") or "").lower()
        if layout not in TOKEN_LAYOUTS:
            rejected_layout.append((row, layout or "(unresolved)"))
            continue
        if row[5] != "available":
            excluded.append((row, f"status={row[5]} -- bulk-move route refuses non-available"))
            continue
        eligible.append((row, layout))

    connection.close()
    return {
        "all_rows": rows, "token_sets": len(sets), "candidates": candidates,
        "eligible": eligible, "rejected_layout": rejected_layout, "excluded": excluded,
        "layouts": layouts,
    }


def report(found: dict) -> None:
    eligible = found["eligible"]
    print(f"Scryfall token-type sets: {found['token_sets']}")
    print(f"Rows in those sets:       {len(found['candidates'])}")
    print(f"  layout-verified:        {len(eligible) + len(found['excluded'])}")
    print(f"  rejected (not token):   {len(found['rejected_layout'])}")
    print()
    print(f"=== ELIGIBLE TO MOVE: {len(eligible)} ===")
    by_layout = collections.Counter(layout for _, layout in eligible)
    print("  by layout:", dict(by_layout))
    print("  by source batch:", dict(collections.Counter(r[6] for r, _ in eligible)))
    print()
    for row, layout in sorted(eligible, key=lambda x: (x[0][6], x[0][1], x[0][0])):
        print(f"    #{row[0]:<6} {row[1][:38]:<38} {row[2]}#{row[3]:<5} "
              f"{layout:<8} from={row[6]}")
    print()
    print(f"=== EXCLUDED: {len(found['excluded'])} ===")
    for row, why in found["excluded"]:
        flag = " [CONSIGNED]" if row[8] else ""
        print(f"    #{row[0]:<6} {row[1][:34]:<34} {row[2]}#{row[3]:<5} "
              f"batch={row[6]:<12} {why}{flag}")
    if found["rejected_layout"]:
        print()
        print(f"=== REJECTED BY LAYOUT CHECK: {len(found['rejected_layout'])} ===")
        for row, layout in found["rejected_layout"]:
            print(f"    #{row[0]} {row[1]!r} {row[2]} layout={layout}")


def source_batch_counts(card_ids: list[int]) -> dict:
    connection = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    counts = dict(connection.execute(
        "select b.batch_code, count(*) from inventory_cards ic "
        "join batches b on b.id = ic.batch_id group by b.batch_code"
    ).fetchall())
    connection.close()
    return counts


def apply_move(found: dict, batch_code: str) -> None:
    eligible = found["eligible"]
    card_ids = [row[0] for row, _ in eligible]
    base = _base_url()
    headers = _auth_header()

    with httpx.Client(timeout=120, follow_redirects=False) as client:
        created = client.post(
            f"{base}/batches", headers=headers,
            data={"batch_code": batch_code, "is_consignment": "", "consignor_id": ""},
        )
        if created.status_code not in (200, 303):
            print(f"Batch creation failed: {created.status_code}\n{created.text[:500]}")
            sys.exit(1)
        print(f"POST /batches -> {created.status_code}")

        connection = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
        target = connection.execute(
            "select id, batch_code from batches where upper(batch_code)=upper(?)", (batch_code,)
        ).fetchone()
        connection.close()
        if not target:
            print("Target batch not found after creation -- aborting before any move.")
            sys.exit(1)
        target_id, actual_code = target
        print(f"Target batch: id={target_id} code={actual_code!r}")

        # httpx's `data=` takes a MAPPING; repeated form fields are a list
        # VALUE under one key, not a list of (key, value) tuples. Passing
        # tuples makes httpx treat the whole thing as a raw content stream
        # and blow up inside h11 ("expected a bytes-like object, tuple
        # found") -- which is exactly how the first apply attempt failed,
        # after the batch was created but before anything moved.
        moved = client.post(
            f"{base}/inventory-cards/bulk-move-batch", headers=headers,
            data={
                "target_batch_id": str(target_id),
                "back_link": "/inventory",
                "card_ids": [str(cid) for cid in card_ids],
            },
        )
        print(f"POST /inventory-cards/bulk-move-batch -> {moved.status_code}")
        if moved.status_code != 200:
            print(moved.text[:1500])
            sys.exit(1)
        body = moved.text
        for marker in ("Move blocked", "not eligible"):
            if marker in body:
                print("ROUTE REFUSED THE MOVE (guard fired) -- nothing was changed:")
                print(body[:1500])
                sys.exit(1)
        print("  route reported success")


def verify(found: dict, batch_code: str, before: dict) -> None:
    connection = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    q = connection.execute
    row = q("select id, batch_code from batches where upper(batch_code)=upper(?)", (batch_code,)).fetchone()
    target_id, actual_code = row
    in_batch = q("select count(*) from inventory_cards where batch_id=?", (target_id,)).fetchone()[0]
    print()
    print(f"=== VERIFY ===")
    print(f"  {actual_code!r} (id {target_id}) now holds: {in_batch} cards")

    moved_ids = [r[0] for r, _ in found["eligible"]]
    logged = q(
        "select count(*) from inventory_change_logs where inventory_card_id in (%s) "
        "and change_summary like '%%(bulk move)%%'" % ",".join("?" * len(moved_ids)),
        moved_ids,
    ).fetchone()[0]
    print(f"  InventoryChangeLog bulk-move entries for those cards: {logged}")

    after = dict(q(
        "select b.batch_code, count(*) from inventory_cards ic "
        "join batches b on b.id = ic.batch_id group by b.batch_code"
    ).fetchall())
    print("  source batch counts before -> after:")
    for code in sorted(set(before) | set(after)):
        b, a = before.get(code, 0), after.get(code, 0)
        if b != a:
            print(f"    {code:<18} {b} -> {a}   ({a - b:+d})")

    print("  excluded rows, re-read:")
    for row_data, _why in found["excluded"]:
        current = q(
            "select b.batch_code, ic.status from inventory_cards ic "
            "join batches b on b.id = ic.batch_id where ic.id=?", (row_data[0],)
        ).fetchone()
        same = "UNCHANGED" if current[0] == row_data[6] else f"CHANGED -> {current[0]}"
        print(f"    #{row_data[0]:<6} batch={current[0]:<12} status={current[1]:<10} {same}")
    connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually write (default: dry run)")
    parser.add_argument("--batch-code", default=DEFAULT_BATCH_CODE)
    args = parser.parse_args()

    with httpx.Client(timeout=60) as client:
        found = gather(client)
    report(found)

    print()
    print(f"Mode: {'WRITE (--confirm)' if args.confirm else 'DRY RUN (report only)'}")
    # POST /batches upper-cases whatever it is given, so the created code
    # will not be the mixed-case string requested. Surfaced rather than
    # silently accepted: existing batches use BOTH conventions (A1/CON_*/
    # TEST uppercase, leg_* lowercase, "Foreign Language" title case).
    if args.batch_code != args.batch_code.upper():
        print(f"  NOTE: POST /batches upper-cases codes -- {args.batch_code!r} "
              f"will be created as {args.batch_code.upper()!r}.")
    if not args.confirm:
        print("\nDRY RUN -- nothing written. Re-run with --confirm to apply.")
        return

    before = source_batch_counts([row[0] for row, _ in found["eligible"]])
    apply_move(found, args.batch_code)
    verify(found, args.batch_code, before)


if __name__ == "__main__":
    main()
