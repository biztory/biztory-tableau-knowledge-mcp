"""Write Tableau Business Preferences files that the agent actually follows.

A Business Preferences file is a plain-text list of instructions Tableau's agent reads before it
answers: one per line, each starting with "# ". Tableau Cloud allows one file per dashboard (50
lines, 8,000 characters); Tableau Next one per semantic model (30,000 characters). The checks below
encode field-tested lessons from "Business Preferences in Tableau Cloud: What We Learned" (Celia
Fryar, XeoMatrix, Tableau Cloud 2026.2.6, September 2026), plus one thing only this server can do:
confirm every field a preference names exists, under that exact caption, in the dashboard's data.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from typing import Any

from .client import TableauClient, TableauError
from .ossie import load_datasource

# product -> (max rules, max characters); Tableau Next has no rule count limit
LIMITS: dict[str, tuple[int | None, int]] = {"cloud": (50, 8000), "next": (None, 30000)}
HEADROOM = 100

# Rules worth having in almost every file, written to avoid the failures seen in testing.
BASELINE = {
    "query": "# Keep numbers numeric in every query, rounded to 2 decimals; add currency symbols and "
    "thousands separators only in the final written answer.",
    "failure": "# When a query fails, try again with fewer fields or one step at a time; if the answer is "
    "still incomplete, say exactly which part is missing.",
}
GENERIC_CAPTIONS = {
    "amount", "value", "name", "id", "date", "type", "status", "title", "description", "country",
    "region", "closed", "won", "number", "count", "total", "category", "code", "stage",
}  # fmt: skip

RULEBOOK = """\
# Writing Tableau Business Preferences that work
Based on hands-on testing reported in "Business Preferences in Tableau Cloud: What We Learned"
(Celia Fryar, XeoMatrix, September 2026). check_business_preferences enforces what can be checked.

What a preferences file does
- It teaches the agent your business language and shapes how it PLANS an answer: which field means
  "bookings", what "this quarter" means, which threshold a policy uses.
- It does not control the SQL the agent writes. If the plan is right and the answer is still wrong,
  the fault is in the platform: split the question, work around it, report it. Stop tuning.

Format (a slip here makes Tableau silently ignore the line)
- Every line starts with "#" and exactly one space. No space before "#", none missing after it.
- Tableau Cloud: one file per dashboard, max 50 lines and 8,000 characters. Keep ~100 characters
  spare. No section headers, notes or blank lines: every line must be a rule.
- Tableau Next: one file per semantic model, 30,000 characters (the 50/8,000 figures are Cloud's).
- A rule shared by several dashboards (e.g. a discount policy) is copied into each file: keep the
  copies in sync. Number versions (v1, v2...) so each test result maps to a change.
- Upload, then publish straight away: an unpublished upload is lost and cannot be undone.

Wording
- Name fields exactly as the workbook shows them (the caption), not a model's internal name or a
  metric name from Tableau Next. Writing them as [Field Name] makes them easy to check.
- State thresholds and policies exactly and say to use only that value, or the agent substitutes a
  common rule of thumb.
- Keep display out of the query: formatting rules (currency, decimals, separators) belong to the
  written answer only. A formatting rule applied in SQL turns numbers into text and breaks filters.
- Say how to fail: retry smaller, never present a partial answer as complete, name what is missing.
- Write statements of fact and instructions, not conversation: the agent can treat a remark as a
  command.
- Word absences so they cannot be over-read: "seller names are available; only territory and
  manager are missing", not "there is no seller hierarchy".
- No two rules may contradict each other (e.g. "two decimal places" and "no cents").
- Be explicit about order and averages: "largest losses first" (not "descending" on negatives);
  "ratio of totals" or "average of ratios"; "orders" or "order lines".
- "Never tell users to contact an administrator" is not honoured: don't spend a line on it.

Prepare the workbook (matters as much as the file)
- Remove duplicate field names: Tableau suffixes copies like "Account ID (Account)" and the agent's
  SQL asks for a plain "Account ID".
- Qualify generic captions: "Opportunity Amount", not "Amount"; "Account Country", not "Country".
- Hide helper calculations (they still work in calcs and relationships); keep one clearly named
  field per metric. Check what each calculation really counts.
- Tableau Next models: row-level calculations come through and can be queried; the model's
  aggregate measures (SUM/COUNTD formulas) work on the canvas but not for the Cloud agent. Rebuild
  them as row-level workbook calcs (e.g. IF [Won] THEN [Amount] END), hide the model's version and
  point the preferences at the new field.
- Users need AI Access and Full Data Query. Row-level security must live in the data source: user
  filters on a sheet or dashboard block the agent's Q&A.

How the agent behaves
- It queries through the dashboard, so dashboard filters (including date ranges) apply.
- It gives up after about three query attempts.
- Short questions in steps beat long multi-part prompts.

How to test
- Build an answer key first; change one thing at a time; re-ask the same questions in a fresh
  conversation; test one field per question when something fails; check every number (the most
  polished answers can be the most wrong); read the trace, not just the answer; test the
  published view, which can behave differently from edit mode."""


def _issue(
    issues: list[dict[str, Any]], line: int | None, severity: str, rule: str, message: str, fix: str = ""
) -> None:
    issues.append(
        {
            "line": line,
            "severity": severity,
            "rule": rule,
            "message": message,
            **({"fix": fix} if fix else {}),
        }
    )


def lint(text: str, product: str = "cloud") -> dict[str, Any]:
    """Check a Business Preferences file for format, limits and wording problems."""
    if product not in LIMITS:
        raise TableauError("product must be 'cloud' (one file per dashboard) or 'next' (per semantic model).")
    issues: list[dict[str, Any]] = []
    lines = text.splitlines()
    rules: list[tuple[int, str]] = []
    for n, raw in enumerate(lines, start=1):
        if not raw.strip():
            _issue(
                issues,
                n,
                "warning",
                "blank_line",
                "Blank line: it counts against the limit and teaches nothing.",
                "Delete it.",
            )
            continue
        if raw[0].isspace() and raw.lstrip().startswith("#"):
            _issue(
                issues,
                n,
                "error",
                "format",
                "Whitespace before '#': Tableau silently ignores this line.",
                "Start the line with '# '.",
            )
            raw = raw.lstrip()
        if not raw.startswith("#"):
            _issue(
                issues,
                n,
                "error",
                "format",
                "Line does not start with '# ': it is not a preference.",
                "Prefix it with '# ' or delete it.",
            )
            continue
        if not raw.startswith("# "):
            _issue(
                issues,
                n,
                "error",
                "format",
                "Needs '#' followed by one space: this line is silently ignored.",
                "Write '# ' then the rule.",
            )
        elif raw.startswith("#  "):
            _issue(issues, n, "warning", "format", "More than one space after '#'.", "Use exactly '# '.")
        body = raw.lstrip("#").strip()
        if not body:
            _issue(issues, n, "error", "format", "Empty rule.", "Delete it.")
            continue
        rules.append((n, body))

    for n, body in rules:
        low = body.lower()
        words = body.split()
        if (
            re.match(r"^([-=*_#]{3,}|#)", body)
            or (body.endswith(":") and len(words) <= 5)
            or (body.isupper() and len(words) <= 5)
        ):
            _issue(
                issues,
                n,
                "warning",
                "header",
                "Looks like a section header: every line should be a rule.",
                "Fold it into the rules or delete it.",
            )
        if re.match(
            r"^(please|can you|could you|would you|i want|i'd like|i would like|we want|we would like|hi|hello|thanks|thank you)\b",
            low,
        ) or body.endswith("?"):
            _issue(
                issues,
                n,
                "warning",
                "conversational",
                "Conversational wording: the agent can mistake a remark for a command.",
                "State the fact or the instruction directly.",
            )
        threshold = re.search(
            r"\d\s*%|\b(threshold|at least|more than|less than|greater than|above|below|minimum|maximum|target|cut-?off|policy)\b.*\d|\d.*\b(threshold|minimum|maximum|target|cut-?off)\b",
            low,
        )
        if threshold and not re.search(r"\bonly\b|\bexactly\b", low):
            _issue(
                issues,
                n,
                "warning",
                "threshold",
                "Threshold without 'use only this value': the agent may fall back on a rule of thumb.",
                "Add e.g. 'Use only this value.'",
            )
        display = re.search(
            r"\b(format\w*|currency|decimals?|round(ed|ing)?|thousands|commas?|percent signs?|dollar signs?)\b|\$|€|£",
            low,
        )
        if display and not re.search(
            r"written answer|final answer|in the answer|not in (the |a )?quer|never .*\bquer|only when (writing|presenting)|in (the |your )?(text|response|reply)",
            low,
        ):
            _issue(
                issues,
                n,
                "warning",
                "display_in_query",
                "Formatting rule that can leak into the query (numbers become text and comparisons break).",
                "Say it applies only to the written answer, and keep queries numeric.",
            )
        if re.search(r"\badmin(istrator)?\b", low):
            _issue(
                issues,
                n,
                "info",
                "administrator",
                "Rules about contacting an administrator are not honoured by the agent.",
                "Use the line for something it does follow.",
            )
        if re.search(
            r"\b(has no|have no|does not have|doesn't have|there is no|there are no|no data|not available|unavailable)\b",
            low,
        ) and not re.search(r"\b(is|are) available\b|\bincludes?\b|\bonly\b", low):
            _issue(
                issues,
                n,
                "warning",
                "absence",
                "An absence statement can be over-read as 'no data at all'.",
                "Say what IS available and exactly what is missing.",
            )
        if re.search(r"\b(descending|ascending)\b", low):
            _issue(
                issues,
                n,
                "info",
                "ordering",
                "'Descending' on negative numbers puts the smallest values first.",
                "Say 'largest ... first' or 'smallest ... first'.",
            )
        if (
            re.search(r"\b(average|avg|mean)\b", low)
            and re.search(r"\b(ratio|rate|margin|percent|percentage|share)\b|%", low)
            and not re.search(r"ratio of totals|of (the )?ratios|of totals", low)
        ):
            _issue(
                issues,
                n,
                "info",
                "average_of_ratios",
                "Ambiguous: average of ratios and ratio of totals give different numbers.",
                "Say which one you mean.",
            )

    seen: Counter[str] = Counter(" ".join(b.lower().split()) for _, b in rules)
    for n, body in rules:
        if seen[" ".join(body.lower().split())] > 1:
            _issue(issues, n, "warning", "duplicate", "This rule appears more than once.", "Keep one copy.")
    two_decimals = [n for n, b in rules if re.search(r"\b(2|two) decimal", b.lower())]
    no_decimals = [
        n
        for n, b in rules
        if re.search(
            r"no cents|whole (dollars|numbers|euros)|no decimals|zero decimals|round(ed)? to (the nearest )?(whole|dollar|euro)",
            b.lower(),
        )
    ]
    if two_decimals and no_decimals:
        _issue(
            issues,
            no_decimals[0],
            "warning",
            "contradiction",
            f"Contradicts line {two_decimals[0]} (decimals vs whole numbers).",
            "Pick one.",
        )

    max_rules, max_chars = LIMITS[product]
    chars = len(text)
    if max_rules is not None and len(rules) > max_rules:
        _issue(
            issues,
            None,
            "error",
            "limit",
            f"{len(rules)} rules: Tableau Cloud allows {max_rules} per file.",
            "Merge or drop rules.",
        )
    if chars > max_chars:
        _issue(
            issues,
            None,
            "error",
            "limit",
            f"{chars} characters: the limit is {max_chars}.",
            "Shorten rules.",
        )
    elif chars > max_chars - HEADROOM:
        _issue(
            issues,
            None,
            "warning",
            "limit",
            f"Within {HEADROOM} characters of the {max_chars} limit.",
            "Leave some headroom for edits.",
        )

    all_text = text.lower()
    suggested = []
    if not re.search(r"\bquer(y|ies)\b", all_text):
        suggested.append(BASELINE["query"])
    if not re.search(r"\b(fail\w*|error|retry|try again|missing|partial)\b", all_text):
        suggested.append(BASELINE["failure"])
    for line in suggested:
        _issue(
            issues,
            None,
            "info",
            "baseline",
            "Missing a rule that prevents a common failure.",
            f"Consider adding: {line}",
        )

    return {
        "product": product,
        "stats": {
            "rules": len(rules),
            "lines": len(lines),
            "characters": chars,
            "rule_limit": max_rules,
            "character_limit": max_chars,
            "characters_left": max_chars - chars,
        },
        "issues": issues,
        "suggested_lines": suggested,
    }


# ---------------------------------------------------------------- field names against the Knowledge graph


def _sources_for(c: TableauClient, node: dict[str, Any], graph: dict[str, Any]) -> list[str]:
    """Data source ids behind a dashboard, sheet, workbook or data source node."""

    def edges(node_id: str) -> list[dict[str, Any]]:
        res = c.knowledge("POST", "/edges/search", query=graph, body={"node_id": node_id}) or {}
        return list(res.get("edges") or [])

    kind = node.get("type")
    if kind in ("PDS", "EMBEDDED_DATASOURCE"):
        return [str(node["id"])]
    if kind in ("DASHBOARD", "SHEET"):
        parents = [
            (e.get("connected_node") or {})
            for e in edges(str(node["id"]))
            if e.get("type") == "CONTAINS" and e.get("direction") == "incoming"
        ]
        workbook = next((p for p in parents if p.get("type") == "WORKBOOK"), None)
        if workbook is None:
            raise TableauError(f"Could not find the workbook that contains {node.get('name')!r}.")
        node = workbook
    elif kind != "WORKBOOK":
        raise TableauError(
            f"{node.get('name')!r} is a {kind}; pass a dashboard, sheet, workbook or data source."
        )
    return [
        str(cn["id"])
        for e in edges(str(node["id"]))
        if e.get("direction") == "outgoing"
        and (cn := e.get("connected_node") or {}).get("type") in ("EMBEDDED_DATASOURCE", "PDS")
    ]


def check_fields(c: TableauClient, node: dict[str, Any], graph: dict[str, Any], text: str) -> dict[str, Any]:
    """Compare field references in the file with the captions the agent will see."""
    fields: list[dict[str, Any]] = []
    source_names: list[str] = []
    for sid in dict.fromkeys(_sources_for(c, node, graph)):
        data = load_datasource(c, sid, graph, allowed=("PDS", "EMBEDDED_DATASOURCE"))
        source_names.append(str(data["pds"].get("name")))
        fields += [f for f in data["fields"] if f.get("properties")]
    if not fields:
        raise TableauError(f"No fields found behind {node.get('name')!r} in Tableau Knowledge.")

    def caption(f: dict[str, Any]) -> str:
        p = f["properties"]
        return str(p.get("caption") or str(p.get("name") or f.get("name", "")).strip("[]"))

    visible = {caption(f) for f in fields if not f["properties"].get("hidden")}
    hidden = {caption(f) for f in fields if f["properties"].get("hidden")}
    internal = {str(f["properties"].get("name", "")).strip("[]"): caption(f) for f in fields}
    by_lower = {v.lower(): v for v in visible}

    issues: list[dict[str, Any]] = []
    checked = 0
    for n, raw in enumerate(text.splitlines(), start=1):
        for quoted, ref in [(False, r) for r in re.findall(r"\[([^\[\]]+)\]", raw)] + [
            (True, r) for r in re.findall(r"[\"“]([^\"”]{2,60})[\"”]", raw)
        ]:
            ref = ref.strip()
            if ref in visible:
                checked += 1
                continue
            # Tableau suffixes duplicate names with their table ("Account ID (Account)"): match those first.
            suffixed = sorted(v for v in visible if v.lower().startswith(ref.lower() + " ("))
            close = list(
                dict.fromkeys(suffixed + difflib.get_close_matches(ref, sorted(visible), n=3, cutoff=0.75))
            )[:3]
            if ref.lower() in by_lower:
                checked += 1
                _issue(
                    issues,
                    n,
                    "warning",
                    "field_case",
                    f"'{ref}' differs in case from the field '{by_lower[ref.lower()]}'.",
                    f"Write it exactly as '{by_lower[ref.lower()]}'.",
                )
            elif ref in hidden:
                checked += 1
                _issue(
                    issues,
                    n,
                    "warning",
                    "field_hidden",
                    f"'{ref}' is a hidden field: the agent may not see it.",
                    "Point the rule at a visible field.",
                )
            elif ref in internal and internal[ref] != ref:
                checked += 1
                _issue(
                    issues,
                    n,
                    "error",
                    "field_internal_name",
                    f"'{ref}' is an internal name; the workbook shows '{internal[ref]}'.",
                    f"Write '{internal[ref]}'.",
                )
            elif not quoted:
                checked += 1
                hint = f" Did you mean {', '.join(repr(x) for x in close)}?" if close else ""
                if suffixed:
                    hint += " Tableau renamed duplicates by table; renaming them in the workbook helps the agent too."
                _issue(
                    issues,
                    n,
                    "error",
                    "field_unknown",
                    f"No visible field is called '{ref}'.{hint}",
                    "Use the exact caption the workbook shows.",
                )
            elif close:  # a quoted term that is almost a field name
                checked += 1
                _issue(
                    issues,
                    n,
                    "warning",
                    "field_near_miss",
                    f"'{ref}' is close to the field(s) {', '.join(repr(x) for x in close)}.",
                    "If you meant the field, use its exact caption.",
                )

    prep: list[dict[str, Any]] = []
    base = Counter(re.sub(r" \([^)]*\)$", "", v) for v in visible)
    for name in sorted(visible):
        stem = re.sub(r" \([^)]*\)$", "", name)
        if stem != name and base[stem] > 1:
            _issue(
                prep,
                None,
                "warning",
                "duplicate_field_name",
                f"'{name}' is one of several '{stem}' fields; the agent's SQL tends to ask for a plain '{stem}'.",
                "Rename the copies or hide the extras.",
            )
        elif name.lower() in GENERIC_CAPTIONS:
            _issue(
                prep,
                None,
                "info",
                "generic_caption",
                f"'{name}' is generic.",
                "Qualify it, e.g. 'Opportunity Amount', 'Account Country'.",
            )
        if re.search(r"helper|\(copy\)|\btemp\b|\btest\b|final \d|_\d{6,}", name.lower()):
            _issue(
                prep,
                None,
                "warning",
                "helper_field_visible",
                f"'{name}' looks like a helper field the agent may treat as a metric.",
                "Hide it in the workbook.",
            )
    return {
        "target": {"id": node.get("id"), "name": node.get("name"), "type": node.get("type")},
        "data_sources": source_names,
        "visible_fields": len(visible),
        "references_checked": checked,
        "issues": issues,
        "workbook_preparation": prep,
    }
