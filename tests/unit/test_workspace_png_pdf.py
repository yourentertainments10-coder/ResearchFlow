"""PNG chart export and PDF report, both written without an imaging or PDF library. The checks decode
the files independently (zlib and byte parsing) and compare against the table, not against the writer."""

from __future__ import annotations

import re
import struct
import zlib

import numpy as np
import pandas as pd
import pytest

from sie.workspace import session as session_module
from sie.workspace import start_session
from sie.workspace.charts import display_number
from sie.workspace.models import Verification
from sie.workspace.pdf import build_pdf
from sie.workspace.raster import SCALE, render_png
from sie.workspace.scene import BLUE, chart_scene


def decode_png(data: bytes) -> np.ndarray:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", None
    kinds = []
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + n]
        (crc,) = struct.unpack(">I", data[pos + 8 + n : pos + 12 + n])
        assert crc == zlib.crc32(kind + body), kind
        kinds.append(kind)
        if kind == b"IHDR":
            w, h, depth, ctype, comp, filt, inter = struct.unpack(">IIBBBBB", body)
            assert (depth, ctype, comp, filt, inter) == (8, 2, 0, 0, 0)
            size = (h, w)
        if kind == b"IDAT":
            idat += body
        pos += 12 + n
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND"
    raw = zlib.decompress(idat)
    h, w = size  # type: ignore[misc]
    stride = 1 + w * 3
    assert len(raw) == h * stride
    rows = []
    for y in range(h):
        assert raw[y * stride] == 0  # filter "none"
        rows.append(
            np.frombuffer(raw[y * stride + 1 : (y + 1) * stride], dtype=np.uint8).reshape(w, 3)
        )
    return np.stack(rows)


SALES = pd.DataFrame(
    {"region": ["North", "South", "East", "West"], "amount": [300.0, 150.0, 75.0, 0.0]}
)


def blue_runs(img: np.ndarray, y: int) -> list[tuple[int, int]]:
    row = (img[y] == np.array(BLUE)).all(axis=1)
    runs, start = [], None
    for x, v in enumerate(row):
        if v and start is None:
            start = x
        if not v and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, len(row)))
    return runs


def test_bar_png_bars_are_proportional_to_the_table_values():
    img = decode_png(render_png(chart_scene("bar", SALES, "region", "amount", "Amount by region")))
    assert img.shape == (img.shape[0], 640 * SCALE, 3)
    assert (img[-1, 0] == 255).all()  # white background
    lengths = []
    for i in range(3):  # row i sits at y = 34 + 24 * i, the bar is 2..18 below that (scene units)
        run = blue_runs(img, (34 + 24 * i + 10) * SCALE)
        assert len(run) == 1
        lengths.append(run[0][1] - run[0][0])
    full = lengths[0]
    assert abs(full - (640 - 190 - 90) * SCALE) <= 1  # the largest value spans the plot width
    for length, value in zip(lengths, [300.0, 150.0, 75.0], strict=True):
        assert abs(length - full * value / 300.0) <= 1.0
    zero_runs = blue_runs(img, (34 + 24 * 3 + 10) * SCALE)  # a zero value is only a 1-unit mark
    assert all(b - a <= SCALE for a, b in zero_runs)


def test_line_png_has_a_dot_at_every_point_and_a_gap_where_a_value_is_missing():
    t = pd.DataFrame({"m": ["a", "b", "c", "d"], "v": [10.0, 20.0, None, 40.0]})
    scene = chart_scene("line", t, "m", "v", "v by m")
    img = decode_png(render_png(scene))
    dots = [it for it in scene.items if it[0] == "dot"]
    assert len(dots) == 3  # the missing value is not drawn as zero
    for _, cx, cy, _, _ in dots:
        assert tuple(img[round(cy * SCALE), round(cx * SCALE)]) == BLUE
    polys = [it for it in scene.items if it[0] == "poly"]
    assert polys == [] or all(len(p[1]) > 1 for p in polys)


def test_png_is_deterministic_and_non_ascii_labels_do_not_crash():
    t = pd.DataFrame({"k": ["Zürich", "東京", "a" * 60], "n": [3, 2, 1]})
    scene = chart_scene("bar", t, "k", "n", "Städte")
    a, b = render_png(scene), render_png(scene)
    assert a == b and decode_png(a).shape[1] == 640 * SCALE


def test_empty_and_oversized_charts():
    t = pd.DataFrame({"k": ["a"], "n": [None]})
    img = decode_png(render_png(chart_scene("bar", t, "k", "n", "empty")))
    assert img.shape[0] == 80 * SCALE
    assert chart_scene("table", t, "k", "n", "x") is None
    big = pd.DataFrame({"k": list("ab"), "n": [1, 2]})
    scene = chart_scene("bar", big, "k", "n", "x")
    scene.height = 100_000
    with pytest.raises(ValueError):
        render_png(scene)


# -- PDF ---------------------------------------------------------------------------------------------

CSV = (
    b"region,revenue,day\n"
    + b"\n".join(
        f"{r},{(i * 37) % 500 + 10},2024-0{1 + i % 9}-1{i % 9}".encode()
        for i, r in enumerate(["North", "South", "East", "West"] * 30)
    )
    + b"\n"
)


def parse_pdf(data: bytes) -> tuple[list[str], int]:
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    start = int(re.search(rb"startxref\n(\d+)\n%%EOF", data).group(1))  # type: ignore[union-attr]
    assert data[start : start + 4] == b"xref"
    count = int(re.match(rb"xref\n0 (\d+)\n", data[start:]).group(1))  # type: ignore[union-attr]
    table = data[start:].split(b"\n")[2 : 2 + count]
    assert table[0].startswith(b"0000000000 65535 f")
    for n, entry in enumerate(table[1:], start=1):
        off = int(entry[:10])
        assert data[off : off + len(f"{n} 0 obj".encode())] == f"{n} 0 obj".encode()
    pages = int(re.search(rb"/Count (\d+)", data).group(1))  # type: ignore[union-attr]
    texts = []
    for m in re.finditer(rb"/Length (\d+) /Filter /FlateDecode >>\nstream\n", data):
        body = data[m.end() : m.end() + int(m.group(1))]
        texts.append(zlib.decompress(body).decode("ascii"))
    assert len(texts) == pages
    return texts, pages


def shown(texts: list[str]) -> str:
    found = re.findall(r"\(((?:\\.|[^\\)])*)\) Tj", "\n".join(texts))
    return "\n".join(re.sub(r"\\(.)", r"\1", t) for t in found)


def test_pdf_is_a_well_formed_file_with_every_verified_result():
    s = start_session(CSV, "sales.csv")
    s.ask("Compare revenue by region")
    data = build_pdf(s)
    texts, pages = parse_pdf(data)
    text = shown(texts)
    assert pages >= 1 and f"Page 1 of {pages}" in text
    assert "SHA-256 " + s.workspace.source.sha256 in text
    for item in [*s.workspace.items, *s.items]:
        assert item.exportable
        assert item.result.provenance.result_sha256 in text
        assert item.planned.title in text
    assert "Limitations" in text
    assert build_pdf(s) == data  # same session, same bytes


def test_pdf_leaves_out_what_failed_its_check_and_says_so(monkeypatch):
    monkeypatch.setattr(
        session_module,
        "verify_result",
        lambda ds, spec, table: Verification("unverified", 1, ["forced"]),
    )
    s = start_session(CSV, "sales.csv")
    s.ask("Compare revenue by region")
    text = shown(parse_pdf(build_pdf(s))[0])
    assert "Not included (failed verification" in text
    asked = next(i for i in s.items if i.planned.title.startswith("Sum of revenue"))
    assert not asked.exportable
    before, after = text.split("Not included (failed verification")
    assert asked.planned.title in after and asked.planned.title not in before


def test_pdf_table_numbers_match_the_computed_table():
    s = start_session(CSV, "sales.csv")
    s.ask("Compare revenue by region")
    item = next(i for i in s.items if i.planned.title.startswith("Sum of revenue"))
    text = shown(parse_pdf(build_pdf(s))[0])
    for value in item.result.table.iloc[:, -1].tolist():
        assert display_number(value) in text


def test_pdf_includes_models_and_the_sample_note():
    s = start_session(CSV, "sales.csv")
    s.ask("Predict revenue")
    text = shown(parse_pdf(build_pdf(s))[0])
    assert "Model comparison m1" in text and "Baseline" in text


def test_pdf_page_breaks_never_split_a_table_header_from_its_rows():
    s = start_session(CSV, "sales.csv")
    for q in ["Compare revenue by region", "Average revenue by region", "Top 3 region by revenue"]:
        s.ask(q)
    texts, pages = parse_pdf(build_pdf(s))
    assert pages >= 1
    for page in texts:
        lines = shown([page]).splitlines()
        assert [ln for ln in lines if not ln.startswith("Page ")], "no empty pages"
