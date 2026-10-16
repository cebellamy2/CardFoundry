"""Classify every route's bottom action against the operator's rule.

ONE implementation, used by BOTH the safety test
(tests/test_top_action_copy.py) and the report table, so the two can never
disagree about where a route belongs. That mattered: the first version of
the report table was a separate ad-hoc script with three buckets and it
SILENTLY DROPPED every route matching none of them -- eleven routes that
qualified for a top copy went unlisted and unbuilt. Here there are five
buckets and every one of them carries a reason.

THE RULE (the operator, 2026-10-14): "the ones that require you to type
something to confirm it you can just leave at the bottom. Anything else
that doesn't require input at the bottom of the screen can be added at the
top and the button at the bottom left there."

★ THE UNIT OF ANALYSIS IS THE FORM, NOT THE PAGE. A button's bucket
depends on the fields in ITS OWN form. Judging by "is there a text field
anywhere in this function" fails in both directions and both failures have
actually happened:
  - too narrow: a 60-line window around the button missed the typed
    confirmation on /inventory-sync/<job>/apply, and filed a
    typed-confirm route as safe to add a top copy to;
  - too wide: resolving helpers without excluding the page chrome made
    every page inherit the nav's own logout form and its login inputs,
    and 150 routes came back as "fill-in" including ones whose action
    form has no fields at all.

★ IT RESOLVES SHARED HELPERS, MINUS THE CHROME. A route's action button is
often rendered by a helper rather than by the route function -- which is
how _decklist_bulk_action_confirm_page's button escaped the first pass. So
the call graph is followed, but CHROME_HELPERS are cut out of it: every
page calls them, they contribute the nav and the footer, and counting
their markup as the page's own is what produced the 150.
"""
import ast
import re
from pathlib import Path

TOP_COPY_CALL = "_top_action_copy("

# Rendered on every single page. Their markup is not the page's own.
CHROME_HELPERS = {
    "page_start", "page_end", "_html_head", "_portal_page_start",
    "_portal_page_end", "_page_header", "_breadcrumbs",
    "_shipment_sync_alert_banner", "_nav_group_html", "_attention_badge_html",
}

FORM_OPEN = re.compile(r"<form\b[^>]*>", re.I)
SUBMIT = re.compile(r'<button[^>]*type="submit"|<input[^>]*type="submit"|<button(?![^>]*type=)', re.I)
CONFIRMATION_INPUT = re.compile(r'name="confirmation"', re.I)
# A control the operator must type into or choose from. Hidden, submit,
# checkbox, radio and button inputs are not that: ticking a box is not
# typing, and the boxes sit in the content above the button anyway.
NEEDS_TYPED_INPUT = re.compile(
    r'<textarea\b|<select\b|<input\b(?![^>]*type="(?:hidden|submit|checkbox|radio|button)")',
    re.I,
)
LABEL = re.compile(r"<button[^>]*>\s*([^<]{1,70}?)\s*</button>", re.I | re.S)

BUCKET_TOP_COPY = "top-copy"
BUCKET_TYPED_CONFIRM = "typed-confirm"
BUCKET_FILL_IN = "fill-in"
BUCKET_MULTI_ACTION = "multi-action"
BUCKET_UNCLASSIFIED = "unclassified"


def _functions(source: str) -> dict:
    tree = ast.parse(source)
    lines = source.split("\n")
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        routes = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                if isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app":
                    if dec.args and isinstance(dec.args[0], ast.Constant):
                        routes.append(f"{dec.func.attr.upper()} {dec.args[0].value}")
        out[node.name] = {
            "body": "\n".join(lines[node.lineno - 1:node.end_lineno]),
            "routes": routes,
            "calls": {
                sub.func.id for sub in ast.walk(node)
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
            },
        }
    return out


def _own_markup(name: str, funcs: dict, seen=None) -> str:
    """This route's markup plus that of every non-chrome helper it calls."""
    if seen is None:
        seen = set()
    if name in seen or name not in funcs or name in CHROME_HELPERS:
        return ""
    seen.add(name)
    info = funcs[name]
    parts = [info["body"]]
    for callee in sorted(info["calls"]):
        parts.append(_own_markup(callee, funcs, seen))
    return "\n".join(parts)


def _submitting_forms(markup: str) -> list[dict]:
    """Each <form> that contains a submit control, with its own fields."""
    forms = []
    for match in FORM_OPEN.finditer(markup):
        close = markup.find("</form>", match.end())
        span = markup[match.start():close + 7] if close != -1 else markup[match.start():]
        if not SUBMIT.search(span):
            continue
        forms.append({
            "open_tag": match.group(0),
            "labels": [l.strip() for l in LABEL.findall(span) if l.strip()],
            "typed_confirm": bool(CONFIRMATION_INPUT.search(span)),
            "needs_input": bool(NEEDS_TYPED_INPUT.search(span)),
            # Any *_FORM_ID constant, not only PRIMARY_ACTION_FORM_ID:
            # _top_action_copy takes the target form id as an argument,
            # and /orders now points one at its own select-all form.
            # Matching only the default would have left that route
            # looking like a top copy with no form behind it.
            "is_primary": "_FORM_ID" in match.group(0),
        })
    return forms


def classify(source: str | None = None) -> list[dict]:
    """Every route that renders an action button, with its bucket.

    NOTHING IS EVER DROPPED. A route that renders a submit button and
    matches none of the other buckets comes back as `unclassified` with a
    reason, which is the whole point of this module existing.
    """
    if source is None:
        source = Path(__file__).with_name("main.py").read_text()
    funcs = _functions(source)
    rows = []
    for name, info in funcs.items():
        if not info["routes"]:
            continue
        markup = _own_markup(name, funcs)
        forms = _submitting_forms(markup)
        if not forms:
            continue
        route = info["routes"][0]
        has_top = TOP_COPY_CALL in markup
        labels = [l for form in forms for l in form["labels"]]

        if has_top:
            bucket, reason = BUCKET_TOP_COPY, ""
        elif any(form["typed_confirm"] for form in forms):
            bucket = BUCKET_TYPED_CONFIRM
            reason = "its action form asks for a typed confirmation phrase"
        elif len(forms) > 1:
            bucket = BUCKET_MULTI_ACTION
            reason = (
                f"{len(forms)} action forms: buttons sit beside the row or section "
                "they act on, so there is no single bottom action (operator "
                "decision open)"
            )
        elif forms[0]["needs_input"]:
            bucket = BUCKET_FILL_IN
            reason = "its one action form has a field the operator must fill in"
        else:
            bucket = BUCKET_UNCLASSIFIED
            reason = "one action form, no field to fill: QUALIFIES for a top copy and has not been given one"
        rows.append({
            "route": route, "fn": name, "bucket": bucket, "reason": reason,
            "labels": labels, "form_count": len(forms), "has_top": has_top,
        })
    return sorted(rows, key=lambda r: (r["bucket"], r["route"]))


def primary_form_of(route_fn: str, source: str | None = None) -> dict | None:
    """The form a top copy targets on this route, for the safety test."""
    if source is None:
        source = Path(__file__).with_name("main.py").read_text()
    funcs = _functions(source)
    for form in _submitting_forms(_own_markup(route_fn, funcs)):
        if form["is_primary"]:
            return form
    return None


def table(rows=None) -> str:
    rows = rows if rows is not None else classify()
    by = {}
    for row in rows:
        by.setdefault(row["bucket"], []).append(row)
    parts = []
    for bucket in (BUCKET_TOP_COPY, BUCKET_UNCLASSIFIED, BUCKET_TYPED_CONFIRM,
                   BUCKET_FILL_IN, BUCKET_MULTI_ACTION):
        group = by.get(bucket, [])
        parts.append("=" * 78)
        parts.append(f"{bucket.upper()}  ({len(group)} routes)")
        parts.append("=" * 78)
        for row in group:
            label = row["labels"][0] if row["labels"] else "(submit)"
            parts.append(f"  {row['route']:54s} {label[:38]}")
        if group and group[0]["reason"]:
            parts.append(f"     -> {group[0]['reason']}")
        parts.append("")
    parts.append(f"TOTAL ROUTES WITH AN ACTION BUTTON: {len(rows)}")
    parts.append("NO ROUTE IS DROPPED: every bucket above is printed, including unclassified.")
    return "\n".join(parts)


if __name__ == "__main__":
    print(table())
