"""YFClient — regression test for a real bug this session found: yfinance's
get_earnings_dates(limit=N) does not always honor N, so callers requesting
a small window could silently get back far more rows than asked for."""

import pandas as pd
import pytest

from hedge_fund.data.yfinance_client import YFClient


@pytest.fixture
def client():
    c = YFClient()
    yield c
    c.close()


def _fake_earnings_dates(n):
    idx = pd.date_range("2024-01-01", periods=n, freq="90D")
    return pd.DataFrame(
        {
            "EPS Estimate": [1.0] * n,
            "Reported EPS": [1.1] * n,
            "Surprise(%)": [10.0] * n,
        },
        index=idx,
    )


def test_get_earnings_history_truncates_when_provider_ignores_limit(client, monkeypatch):
    class _FakeTicker:
        def get_earnings_dates(self, limit):
            # Simulate yfinance returning more than asked for.
            return _fake_earnings_dates(n=20)

    monkeypatch.setattr(client, "_ticker", lambda symbol: _FakeTicker())

    records = client.get_earnings_history("AAPL", limit=4)

    assert len(records) == 4


def test_get_earnings_history_empty_frame_returns_empty_list(client, monkeypatch):
    class _FakeTicker:
        def get_earnings_dates(self, limit):
            return pd.DataFrame()

    monkeypatch.setattr(client, "_ticker", lambda symbol: _FakeTicker())

    assert client.get_earnings_history("AAPL", limit=4) == []
