"""EdgarClient — pure-logic and concurrency contract tests.

No real network calls: HTTP fetch points (_get_json) are stubbed so these
run offline and fast. Covers the parts of this session's free-data-provider
work that are easy to get subtly wrong and hard to notice when wrong:

1. Shared CIK-map caching is actually thread-safe (this was a real bug:
   every EdgarClient() used to re-download the ~800KB map per instance).
2. Fiscal-year anchor selection dedups to the earliest-filed 10-K per
   period-end, so a later restatement never silently swaps in.
3. Split-adjustment math on the market-cap/P-E path, since a missed
   adjustment silently understates market cap by the split ratio for any
   company that has split since the anchored filing.
"""

import threading

import pytest

from hedge_fund.data.edgar_client import EdgarClient


@pytest.fixture(autouse=True)
def _reset_shared_cik_map():
    """The CIK map is a class-level cache — isolate tests from each other."""
    EdgarClient._shared_cik_map = None
    yield
    EdgarClient._shared_cik_map = None


@pytest.fixture
def client():
    c = EdgarClient(user_agent="test-suite test@example.com")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Shared CIK map: thread-safety
# ---------------------------------------------------------------------------

def test_cik_map_fetched_once_across_concurrent_calls(client, monkeypatch):
    calls = []

    def fake_get_json(self, url):
        calls.append(url)
        return {"0": {"ticker": "AAPL", "cik_str": 320193}}

    monkeypatch.setattr(EdgarClient, "_get_json", fake_get_json)

    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()  # line every thread up so they race _load_cik_map together
        return client._cik_for("AAPL")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert client._cik_for("AAPL") == 320193


def test_cik_map_shared_across_instances(monkeypatch):
    calls = []

    def fake_get_json(self, url):
        calls.append(url)
        return {"0": {"ticker": "MSFT", "cik_str": 789019}}

    monkeypatch.setattr(EdgarClient, "_get_json", fake_get_json)

    c1 = EdgarClient(user_agent="a b@c.com")
    c2 = EdgarClient(user_agent="a b@c.com")
    try:
        assert c1._cik_for("MSFT") == 789019
        assert c2._cik_for("MSFT") == 789019  # no second fetch
        assert len(calls) == 1
    finally:
        c1.close()
        c2.close()


# ---------------------------------------------------------------------------
# Fiscal-year anchor selection
# ---------------------------------------------------------------------------

def _facts_with_assets(entries):
    return {"facts": {"us-gaap": {"Assets": {"units": {"USD": entries}}}}}


def test_anchor_picks_earliest_filed_10k_per_fiscal_year_end(client):
    """A later restatement (10-K/A filed after the original) must not
    silently replace the original anchor — earliest filed wins."""
    facts = _facts_with_assets([
        {"form": "10-K", "end": "2023-12-31", "filed": "2024-02-01", "accn": "original", "val": 100},
        {"form": "10-K/A", "end": "2023-12-31", "filed": "2024-06-01", "accn": "restated", "val": 105},
        {"form": "10-K", "end": "2022-12-31", "filed": "2023-02-01", "accn": "prior-year", "val": 90},
    ])

    anchors = client._fiscal_year_anchors(facts)

    assert [a["accn"] for a in anchors] == ["original", "prior-year"]


def test_anchor_excludes_duration_facts_and_non_10k_forms(client):
    facts = _facts_with_assets([
        {"form": "10-K", "end": "2023-12-31", "filed": "2024-02-01", "accn": "keep", "val": 100},
        # A duration fact (has "start") — Assets is an instant concept; a
        # duration-tagged entry here would be a different concept's leak.
        {"form": "10-K", "start": "2023-01-01", "end": "2023-12-31", "filed": "2024-02-01", "accn": "drop-duration", "val": 1},
        {"form": "10-Q", "end": "2023-09-30", "filed": "2023-11-01", "accn": "drop-10q", "val": 1},
    ])

    anchors = client._fiscal_year_anchors(facts)

    assert [a["accn"] for a in anchors] == ["keep"]


# ---------------------------------------------------------------------------
# Split-adjustment math (market_cap / P-E path)
# ---------------------------------------------------------------------------

class _FakeSeries:
    """Minimal stand-in for the pandas Series yfinance's .splits returns."""

    def __init__(self, items):
        self._items = items  # list[(datetime, ratio)]

    @property
    def empty(self):
        return not self._items

    def items(self):
        for ts, ratio in self._items:
            yield _FakeTimestamp(ts), ratio


class _FakeTimestamp:
    def __init__(self, dt):
        self._dt = dt

    def to_pydatetime(self):
        return self._dt


def test_split_factor_compounds_splits_after_filing_only(client, monkeypatch):
    from datetime import datetime

    splits = _FakeSeries([
        (datetime(2020, 8, 31), 4.0),   # after filed -> counts
        (datetime(2022, 6, 6), 20.0),   # after filed -> counts
        (datetime(2005, 2, 28), 2.0),   # before filed -> ignored
    ])

    class _FakeYfTicker:
        def __init__(self, splits):
            self.splits = splits

    monkeypatch.setattr(client._yf, "_ticker", lambda symbol: _FakeYfTicker(splits))

    factor = client._split_factor_since("AAPL", "2019-01-01")

    assert factor == pytest.approx(80.0)  # 4x * 20x


def test_split_factor_is_one_when_no_splits(client, monkeypatch):
    class _FakeYfTicker:
        splits = _FakeSeries([])

    monkeypatch.setattr(client._yf, "_ticker", lambda symbol: _FakeYfTicker())

    assert client._split_factor_since("KO", "2019-01-01") == 1.0
