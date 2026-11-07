"""Large-file sampling: one bounded pass, a seeded random sample, the whole file's hash, honest notes.
Expected values are worked out by hand or from an independent pandas read of the same bytes."""

from __future__ import annotations

import hashlib
import io
import tracemalloc

import pandas as pd
import pytest

from sie.datasets import DatasetError, Limits, LoadOptions, open_dataset
from sie.datasets.models import LimitExceeded
from sie.workspace import ReplayMismatch, open_stream, replay, start_session
from sie.workspace.sampling import SampleNote, sample_csv


def make_csv(n: int, *, quoted_every: int = 0) -> bytes:
    lines = ["id,grp,amt,txt"]
    for i in range(n):
        txt = f'"a,{i}\nline2"' if quoted_every and i % quoted_every == 0 else f"t{i}"
        lines.append(f"{i},{'ABC'[i % 3]},{i % 50},{txt}")
    return ("\n".join(lines) + "\n").encode()


class Trickle(io.RawIOBase):
    """A stream that hands out tiny chunks, to prove the result does not depend on chunk size."""

    def __init__(self, data: bytes, step: int) -> None:
        self.data, self.step, self.pos = data, step, 0

    def readable(self):
        return True

    def readinto(self, buf):
        chunk = self.data[self.pos : self.pos + min(self.step, len(buf))]
        self.pos += len(chunk)
        buf[: len(chunk)] = chunk
        return len(chunk)


RAW = make_csv(30_000, quoted_every=997)


def test_deterministic_and_independent_of_chunk_size():
    a = sample_csv(io.BytesIO(RAW), seed=5, max_rows=2000)
    b = sample_csv(io.BufferedReader(Trickle(RAW, 777)), seed=5, max_rows=2000)
    c = sample_csv(io.BufferedReader(Trickle(RAW, 5)), seed=5, max_rows=2000)
    assert a.data == b.data == c.data and a.note == b.note == c.note
    assert sample_csv(io.BytesIO(RAW), seed=6, max_rows=2000).data != a.data


def test_note_has_the_whole_file_facts():
    s = sample_csv(io.BytesIO(RAW), seed=1, max_rows=1000)
    assert s.note.rows_in_file == 30_000 and s.note.rows_in_sample == 1000
    assert s.note.file_bytes == len(RAW) and s.note.file_sha256 == hashlib.sha256(RAW).hexdigest()
    assert s.note.seed == 1 and "scaled up" in s.note.text() and f"{30_000:,}" in s.note.text()


def test_sample_rows_are_real_rows_in_file_order_and_quoted_cells_survive():
    full = pd.read_csv(io.BytesIO(RAW), dtype=str, keep_default_na=False).set_index("id")
    s = sample_csv(io.BytesIO(RAW), seed=2, max_rows=3000)
    got = pd.read_csv(io.BytesIO(s.data), dtype=str, keep_default_na=False)
    assert list(got.columns) == ["id", "grp", "amt", "txt"] and len(got) == 3000
    assert got["id"].astype(int).is_monotonic_increasing and got["id"].is_unique
    for row in got.itertuples(index=False):  # each kept row equals the original row, byte for byte
        assert tuple(full.loc[row.id]) == (row.grp, row.amt, row.txt)
    assert got["txt"].str.contains("\n").any()  # a quoted cell with a newline is kept whole


def test_every_row_has_the_same_chance_roughly():
    # 30 000 rows, groups A/B/C each one third; a fair 3 000-row sample is close to a third each
    s = sample_csv(io.BytesIO(RAW), seed=11, max_rows=3000)
    got = pd.read_csv(io.BytesIO(s.data), dtype=str)
    share = got["grp"].value_counts(normalize=True)
    assert all(
        abs(v - 1 / 3) < 0.04 for v in share
    )  # binomial sd about 0.009 so 4 points is > 4 sd
    ids = got["id"].astype(int)
    assert abs(ids.mean() - 15_000) < 1500  # spread across the whole file, not its head


def test_a_file_with_fewer_rows_than_the_cap_is_kept_whole():
    small = make_csv(50)
    s = sample_csv(io.BytesIO(small), max_rows=1000)
    assert s.note.rows_in_sample == 50 and s.note.rows_in_file == 50
    assert pd.read_csv(io.BytesIO(s.data), dtype=str).equals(
        pd.read_csv(io.BytesIO(small), dtype=str)
    )


def test_caps_on_cells_and_bytes_apply():
    wide = ("a,b,c,d,e\n" + "1,2,3,4,5\n" * 5000).encode()
    s = sample_csv(io.BytesIO(wide), limits=Limits(max_cells=500), max_rows=10_000)
    assert s.note.rows_in_sample == 100  # 500 cells / 5 columns
    s = sample_csv(io.BytesIO(wide), limits=Limits(max_bytes=2000), max_rows=10_000)
    assert len(s.data) <= 2000 and 0 < s.note.rows_in_sample < 5000
    # the sample always loads through the normal loader with the limits it was made for
    open_dataset(s.data, "s.csv", s.options, Limits(max_bytes=2000))


def test_the_same_rows_come_back_for_any_row_cap_prefix_property():
    # the rows kept for a smaller cap are a subset of those kept for a larger one (same seed)
    big = pd.read_csv(io.BytesIO(sample_csv(io.BytesIO(RAW), seed=3, max_rows=4000).data))["id"]
    small = pd.read_csv(io.BytesIO(sample_csv(io.BytesIO(RAW), seed=3, max_rows=500).data))["id"]
    assert set(small) <= set(big)


def test_memory_stays_bounded_while_the_file_grows():
    class Lazy(io.RawIOBase):  # produces rows on demand; the file is never held in memory
        def __init__(self, rows):
            self.rows, self.i, self.buf = rows, 0, b"id,grp,amt\n"

        def readable(self):
            return True

        def readinto(self, b):
            while len(self.buf) < len(b) and self.i < self.rows:
                self.buf += "".join(
                    f"{j},{'ABC'[j % 3]},{j % 50}\n"
                    for j in range(self.i, min(self.i + 2000, self.rows))
                ).encode()
                self.i = min(self.i + 2000, self.rows)
            n = min(len(b), len(self.buf))
            b[:n], self.buf = self.buf[:n], self.buf[n:]
            return n

    peaks = {}
    for rows in (50_000, 400_000):
        tracemalloc.start()
        s = sample_csv(io.BufferedReader(Lazy(rows)), max_rows=1000)
        peaks[rows] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert s.note.rows_in_file == rows and s.note.rows_in_sample == 1000
    assert peaks[400_000] < 6 * 1024 * 1024  # a ~6 MB file's worth of rows... the file is ~5 MB
    assert peaks[400_000] < peaks[50_000] * 1.5  # eight times the rows, about the same memory


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (b"", "empty"),
        (b"a,b\n", "no data rows"),
        (b"a,b\n1,2,3\n", "fields"),
        (b"a,b\n1,2\x00\n", "NUL"),
        (b"a,b\n\xff\xfe,2\n", "not valid"),
        (b'a,b\n"1,2\n', "malformed"),
    ],
)
def test_bad_files_are_refused_like_the_loader_would(raw, fragment):
    with pytest.raises(DatasetError) as err:
        sample_csv(io.BytesIO(raw))
    assert fragment in str(err.value)


def test_ragged_rows_can_be_set_aside_and_other_encodings_work():
    s = sample_csv(io.BytesIO(b"a,b\n1,2\n3\n4,5\n"), options=LoadOptions(ragged="reject"))
    assert s.note.rows_in_file == 2
    latin = "name,v\ncafé,1\n".encode("latin-1")
    s = sample_csv(io.BytesIO(latin), options=LoadOptions(encoding="latin-1"))
    assert "café" in s.data.decode("utf-8") and s.options.encoding == "utf-8"
    bom = b"\xef\xbb\xbfa,b\n1,2\n"
    assert sample_csv(io.BytesIO(bom)).data.startswith(b"a,b")


def test_stream_size_cap_refuses_instead_of_truncating():
    with pytest.raises(LimitExceeded):
        sample_csv(io.BytesIO(RAW), max_stream_bytes=1000)


def test_note_text_has_only_its_own_figures():
    note = SampleNote(1234, 100, 0, "ab" * 32, 99)
    assert "100 of its 1,234 rows" in note.text() and "ab" * 32 in note.text()


# ---- through the session --------------------------------------------------------------------------
LIM = Limits(max_bytes=60_000)
BIG = make_csv(8000)  # about 150 kB: over LIM


def test_a_file_within_the_limits_is_loaded_whole_exactly_as_before():
    small = make_csv(500)
    a = open_stream(io.BytesIO(small), "s.csv", limits=LIM)
    b = start_session(small, "s.csv", limits=LIM)
    assert a.workspace.sample is None and a.sha256() == b.sha256()
    assert "sample" not in a.history()


def test_a_large_file_runs_on_the_sample_and_every_number_is_checked_against_pandas():
    s = open_stream(io.BytesIO(BIG), "big.csv", limits=LIM)
    note = s.workspace.sample
    assert note["rows_in_file"] == 8000 and note["file_sha256"] == hashlib.sha256(BIG).hexdigest()
    assert s.workspace.profile.rows == note["rows_in_sample"] < 8000
    step = s.ask("Sum amt by grp")
    assert step.kind == "plan" and step.verification == "verified"
    # independent: the same sum from pandas over the sample rows
    sample_rows = sample_csv(io.BytesIO(BIG), limits=LIM).data
    want = pd.read_csv(io.BytesIO(sample_rows)).groupby("grp")["amt"].sum().to_dict()
    t = s.items[-1].result.table
    assert dict(zip(t["grp"], t["amt:sum"], strict=True)) == want
    assert s.history()["sample"] == note


def test_replay_reproduces_a_sampled_session_from_the_stream_only():
    s = open_stream(io.BytesIO(BIG), "big.csv", limits=LIM)
    s.ask("Sum amt by grp")
    s.ask("Show as table")
    again = replay(io.BytesIO(BIG), "big.csv", s.history(), limits=LIM)
    assert again.sha256() == s.sha256()
    assert replay(BIG, "big.csv", s.history(), limits=LIM).sha256() == s.sha256()  # bytes work too
    other = make_csv(8001)
    with pytest.raises(ReplayMismatch):
        replay(io.BytesIO(other), "big.csv", s.history(), limits=LIM)


def test_too_many_rows_for_the_limits_are_sampled_not_refused():
    s = open_stream(io.BytesIO(make_csv(3000)), "r.csv", limits=Limits(max_rows=1000))
    assert s.workspace.sample["rows_in_file"] == 3000 and s.workspace.profile.rows <= 1000


def test_big_non_csv_and_bad_csv_are_refused():
    with pytest.raises(LimitExceeded):
        open_stream(io.BytesIO(b"x" * 70_000), "big.xlsx", limits=LIM)
    with pytest.raises(DatasetError):
        open_stream(io.BytesIO(b"a,b\n" + b"1,2,3\n" * 20_000), "bad.csv", limits=LIM)
