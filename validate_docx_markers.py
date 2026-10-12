#!/usr/bin/env python3
import hashlib
import re
import sys
import zipfile


src, out = sys.argv[1:3]
pat = re.compile(rb"(\xe5\x9f\x8b\xe7\x82\xb9 T)\d+")

with zipfile.ZipFile(src) as before, zipfile.ZipFile(out) as after:
    assert before.namelist() == after.namelist()
    diffs = []
    for name in before.namelist():
        old, new = before.read(name), after.read(name)
        if old != new:
            diffs.append(name)
        if name != "word/document.xml":
            assert old == new, name

    count = 0

    def repl(match: re.Match[bytes]) -> bytes:
        nonlocal_placeholder = None
        global count
        count += 1
        return match.group(1) + f"{count:02d}".encode("ascii")

    expected = pat.sub(repl, before.read("word/document.xml"))
    actual = after.read("word/document.xml")
    assert expected == actual
    labels = [m.group(0).decode("utf-8") for m in pat.finditer(actual)]
    assert labels == [f"埋点 T{i:02d}" for i in range(1, 45)]

print(f"validated_markers={len(labels)}")
print(f"changed_parts={diffs}")
with open(out, "rb") as handle:
    print(f"output_sha256={hashlib.sha256(handle.read()).hexdigest()}")
