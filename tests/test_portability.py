"""Windows reads files as cp1252 unless told otherwise; every text read/write must say UTF-8."""

import re

from tests.conftest import ROOT

CALL = re.compile(r"\.(read_text|write_text)\(|(?<![\w.])open\(")


def _calls(src: str):
    for m in CALL.finditer(src):
        depth, j = 1, m.end()
        while depth and j < len(src):
            depth += {"(": 1, ")": -1}.get(src[j], 0)
            j += 1
        yield m, src[m.start():j]


def test_text_io_always_names_an_encoding():
    offenders = []
    for d in ("hub", "apps", "tests"):
        for path in (ROOT / d).rglob("*.py"):
            if path.name == "test_portability.py":
                continue
            src = path.read_text(encoding="utf-8")
            for m, call in _calls(src):
                if "encoding=" in call or ("open(" in call and re.search(r"['\"][rwax]*b", call)):
                    continue
                line = src.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {call.splitlines()[0]}")
    assert not offenders, "add encoding=\"utf-8\":\n" + "\n".join(offenders)
