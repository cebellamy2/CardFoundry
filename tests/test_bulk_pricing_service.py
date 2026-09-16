"""Market-following prices for the whole catalogue via Mana Pool's job.

Flow B was item-limited: ~563 of 6,026 listings evaluated per run, the
same ~5,800 never reached because the sort order is stable. The bulk job
priced 6,029 of 6,029 in 13 seconds.

Two things carry the risk here and both are tested hard: the preview/apply
flag, because the difference between a report and a catalogue-wide price
change must never be a defaulted argument, and the manual-override
re-assert, because the job cannot exclude individual products.
"""
import pytest

import bulk_pricing_service as bulk
from bulk_pricing_service import BulkPricingError, parse_export, summarise

EXPORT = """Job ID,Status,Total Items,Successful,Failed,Skipped
"abc","preview",0,0,0,0

Individual Item Details
Item,Details,Current,Low,New,Change,Set Code,Collector Number,Product Type,Condition,Finish,Language,Rarity,Status,Skip Reason,Error Message,Market Price,Anomaly Type,Ratio
"Thunder Dragon","Thunder Dragon","$0.65","$0.15","$0.15","$-0.50","DDG","61","mtg_single","LP","NF","EN","rare","success","none","","$0.20","normal","0.75"
"Sheoldred","Sheoldred","$158.60","$129.98","$129.93","$-28.67","DMU","435","mtg_single","LP","NF","EN","mythic","success","none","","$156.35","underpriced","0.83"

Progress Log
Timestamp,Message,Details
"2026-09-16","done","{}"
"""


def test_the_item_section_is_parsed_not_the_summary_header():
    """Reading this file with a naive DictReader silently returns the
    job-summary columns for every row -- which is what happened the first
    time I opened one."""
    rows = parse_export(EXPORT)
    assert len(rows) == 2
    assert rows[0]["Item"] == "Thunder Dragon"
    assert rows[0]["New"] == "$0.15"
    assert rows[1]["Set Code"] == "DMU"
    assert "Job ID" not in rows[0]


def test_an_export_with_no_item_section_is_an_error_not_an_empty_list():
    with pytest.raises(BulkPricingError):
        parse_export("Job ID,Status\n\"abc\",\"preview\"\n")


def test_the_progress_log_is_not_mistaken_for_items():
    rows = parse_export(EXPORT)
    assert all(r["Item"] not in ("Timestamp", "2026-09-16") for r in rows)


def test_summary_reports_sub_floor_rows_without_clamping_them():
    """Operator decision: sub-$0.65 raw prices are acceptable, because
    Mana Pool applies the store minimum at serve time and those cards are
    filler. So the count is REPORTED and the price is left alone."""
    rows = parse_export(EXPORT)
    s = summarise({"id": "abc", "is_preview": True, "total_items": 2}, rows)
    assert s["below_floor_rows"] == 1          # the $0.15 Thunder Dragon
    assert s["priced_rows"] == 2
    assert s["current_total_cents"] == 65 + 15860
    assert s["new_total_cents"] == 15 + 12993
    assert s["change_cents"] < 0


# --- the preview/apply flag ---------------------------------------------

def test_start_job_requires_an_explicit_preview_choice():
    """Keyword-only and no default: a catalogue-wide price change must
    never be one forgotten argument away."""
    with pytest.raises(TypeError):
        bulk.start_job()


@pytest.mark.parametrize("is_preview", [True, False])
def test_start_job_sends_the_flag_it_was_given(monkeypatch, is_preview):
    sent = {}
    monkeypatch.setattr(bulk, "_post_json",
                        lambda path, body: sent.update(path=path, body=body) or {"jobId": "j1"})
    assert bulk.start_job(is_preview=is_preview) == "j1"
    assert sent["path"] == "/inventory/bulk-price"
    assert sent["body"]["isPreview"] is is_preview


def test_start_job_sends_the_operators_exact_settings(monkeypatch):
    sent = {}
    monkeypatch.setattr(bulk, "_post_json",
                        lambda path, body: sent.update(body=body) or {"jobId": "j1"})
    bulk.start_job(is_preview=True)
    pricing = sent["body"]["pricing"]
    assert pricing["strategy"] == "market_low_fixed"   # "Low Listed" + "Fixed Amount"
    assert pricing["modifier"] == -5                   # "Fixed Cent Adjustment -5"
    assert pricing["minOtherListings"] == 1            # skip items with no competitor
    assert sent["body"]["excludeLetterShippingDisabledSellers"] is True


def test_a_missing_job_id_is_an_error(monkeypatch):
    monkeypatch.setattr(bulk, "_post_json", lambda path, body: {"success": True})
    with pytest.raises(BulkPricingError):
        bulk.start_job(is_preview=True)


# --- polling -------------------------------------------------------------

def test_wait_returns_the_completed_job(monkeypatch):
    calls = {"n": 0}

    def fake(path):
        calls["n"] += 1
        return {"job": {"status": "running" if calls["n"] < 3 else "completed", "id": "j1"}}

    monkeypatch.setattr(bulk, "_get_json", fake)
    job = bulk.wait_for_job("j1", sleep=lambda _s: None)
    assert job["status"] == "completed"
    assert calls["n"] == 3


def test_a_failed_job_raises_rather_than_returning_silently(monkeypatch):
    monkeypatch.setattr(bulk, "_get_json",
                        lambda path: {"job": {"status": "failed", "error_message": "boom"}})
    with pytest.raises(BulkPricingError, match="boom"):
        bulk.wait_for_job("j1", sleep=lambda _s: None)


def test_a_job_that_never_finishes_times_out(monkeypatch):
    monkeypatch.setattr(bulk, "_get_json", lambda path: {"job": {"status": "running"}})
    with pytest.raises(BulkPricingError, match="did not finish"):
        bulk.wait_for_job("j1", timeout=0, sleep=lambda _s: None)


# --- manual overrides ----------------------------------------------------

class FakeOverride:
    def __init__(self, product_id, cents, status="active"):
        self.product_id = product_id
        self.manual_price_cents = cents
        self.status = status


class FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def query(self, _model):
        return self

    def filter(self, *_args):
        return self

    def all(self):
        return self._rows


def test_active_overrides_are_written_back_after_an_apply(monkeypatch):
    """The job selects by set, price and quantity and cannot exclude one
    product, so without this the market silently overwrites an operator's
    deliberate price."""
    sent = []
    monkeypatch.setattr(bulk, "update_inventory_prices_by_product", sent.append)
    result = bulk.reassert_manual_overrides(
        FakeSession([FakeOverride("p1", 1234), FakeOverride("p2", 99)]),
    )
    assert result["reasserted"] == 2
    assert sent and {u["product_id"]: u["price_cents"] for u in sent[0]} == {"p1": 1234, "p2": 99}


def test_an_override_with_no_product_or_price_is_skipped_not_guessed(monkeypatch):
    sent = []
    monkeypatch.setattr(bulk, "update_inventory_prices_by_product", sent.append)
    result = bulk.reassert_manual_overrides(
        FakeSession([FakeOverride(None, 100), FakeOverride("p2", None), FakeOverride("p3", 50)]),
    )
    assert result["reasserted"] == 1
    assert result["skipped"] == 2


def test_no_overrides_means_no_write_at_all(monkeypatch):
    sent = []
    monkeypatch.setattr(bulk, "update_inventory_prices_by_product", sent.append)
    assert bulk.reassert_manual_overrides(FakeSession([]))["reasserted"] == 0
    assert sent == []
