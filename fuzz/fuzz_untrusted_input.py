"""Fuzz the places where untrusted input meets parsing code.

1. ``describe_http_error``: parses error bodies straight from the network into the text the
   model reads. Invariants: never raises, always names the real HTTP status, stays short.
2. ``segment``: turns a model-supplied id into one URL path segment. Invariants: nothing that
   could end the segment or start a query survives, and decoding returns the id unchanged.
3. ``tableau_calc.translate``: Tableau formulas are written by anyone on the site. Invariant:
   it returns SQL or raises ``Untranslatable``, nothing else.
4. ``ossie.parse_document`` + ``validate_document``: Ossie YAML comes from anywhere. Invariant:
   parsing raises only ``TableauError``; validating any parsed value never raises.

Run on Linux with Python 3.12+ (atheris ships Linux wheels only):

    uv run --python 3.12 --group fuzz python fuzz/fuzz_untrusted_input.py -max_total_time=60
"""

import contextlib
import json
import sys
import urllib.parse

import atheris

with atheris.instrument_imports(include=["biztory_tableau_knowledge_mcp"]):
    from biztory_tableau_knowledge_mcp.client import TableauError, describe_http_error, segment
    from biztory_tableau_knowledge_mcp.ossie import parse_document, validate_document
    from biztory_tableau_knowledge_mcp.tableau_calc import Untranslatable, translate

_CALC_ATOMS = [
    "SUM(",
    "COUNTD(",
    "IF ",
    " THEN ",
    " ELSE ",
    " END",
    "{FIXED ",
    ":",
    "}",
    "[Sales]",
    "[Region]",
    "'x'",
    "(",
    ")",
    ",",
    " + ",
    " = ",
    "IIF(",
    "DATEADD('day', ",
    "1",
    "#2026-01-01#",
]


def _value(fdp: atheris.FuzzedDataProvider, depth: int = 0) -> object:
    """A random JSON-able value, biased towards the shapes the error envelope uses."""
    kind = fdp.ConsumeIntInRange(0, 6 if depth < 3 else 3)
    if kind == 0:
        return fdp.ConsumeUnicodeNoSurrogates(64)
    if kind == 1:
        return fdp.ConsumeInt(4)
    if kind == 2:
        return None
    if kind == 3:
        return fdp.ConsumeBool()
    if kind == 4:
        return [_value(fdp, depth + 1) for _ in range(fdp.ConsumeIntInRange(0, 4))]
    keys = ["code", "message", "detail", "trace_id", "loc", "msg", fdp.ConsumeUnicodeNoSurrogates(8)]
    return {fdp.PickValueInList(keys): _value(fdp, depth + 1) for _ in range(fdp.ConsumeIntInRange(0, 5))}


def test_one_input(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    status = fdp.ConsumeIntInRange(400, 599)
    if fdp.ConsumeBool():
        body = json.dumps(_value(fdp)).encode()
    else:
        body = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 4096))
    msg, ecode = describe_http_error(status, "reason", body)  # must never raise
    assert msg.startswith(f"HTTP {status}"), msg[:80]
    assert len(msg) < 2000 and isinstance(ecode, str)

    node_id = fdp.ConsumeUnicodeNoSurrogates(256)
    if node_id:
        enc = segment(node_id)
        assert not set(enc) & set("/?#&= \r\n\t"), enc
        assert urllib.parse.unquote(enc) == node_id

    formula = "".join(fdp.PickValueInList(_CALC_ATOMS) for _ in range(fdp.ConsumeIntInRange(0, 30)))
    formula += fdp.ConsumeUnicodeNoSurrogates(32)
    with contextlib.suppress(Untranslatable):  # refusing is fine; any other exception is a bug
        sql = translate(formula, lambda name: name.lower().replace(" ", "_"), row_level=fdp.ConsumeBool())
        assert isinstance(sql, str)

    try:
        doc = parse_document(
            fdp.ConsumeUnicodeNoSurrogates(2048) if fdp.ConsumeBool() else json.dumps(_value(fdp))
        )
    except TableauError:
        return
    result = validate_document(doc)  # must never raise, whatever the shape
    assert isinstance(result["valid"], bool)


if __name__ == "__main__":
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
