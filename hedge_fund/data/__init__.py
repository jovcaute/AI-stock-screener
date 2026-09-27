"""v2 data pipeline — data provider protocol, FD client, and response models."""

import os

from hedge_fund.data.cached import CachedDataClient
from hedge_fund.data.client import FDClient, FDClientError
from hedge_fund.data.edgar_client import EdgarClient, EdgarClientError
from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsData,
    EarningsRecord,
    Filing,
    FinancialMetrics,
    InsiderTrade,
    Price,
)
from hedge_fund.data.protocol import DataClient
from hedge_fund.data.yfinance_client import YFClient, YFClientError


def default_client() -> DataClient:
    """FDClient if a paid key is configured, else the free EdgarClient.

    EdgarClient gives real point-in-time fundamentals for US filers (falls
    back to YFClient's current-snapshot for anything EDGAR doesn't cover,
    e.g. European tickers) — a strict upgrade over bare YFClient, so it's
    the free default. Needs SEC_EDGAR_USER_AGENT set; falls back to plain
    YFClient (no point-in-time history) if that's missing.

    ponytail: env-var switch, not a plugin registry — add one if a fourth
    provider shows up.
    """
    if os.environ.get("FINANCIAL_DATASETS_API_KEY"):
        return FDClient()
    if os.environ.get("SEC_EDGAR_USER_AGENT"):
        return EdgarClient()
    return YFClient()


__all__ = [
    "default_client",
    "CachedDataClient",
    "CompanyFacts",
    "CompanyNews",
    "DataClient",
    "Earnings",
    "EarningsData",
    "EarningsRecord",
    "EdgarClient",
    "EdgarClientError",
    "FDClient",
    "FDClientError",
    "Filing",
    "FinancialMetrics",
    "InsiderTrade",
    "Price",
    "YFClient",
    "YFClientError",
]
