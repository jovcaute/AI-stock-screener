"""Free, point-in-time data provider backed by SEC EDGAR's XBRL API.

Unlike YFClient, this one satisfies the DataClient protocol's point-in-time
contract for real: every fundamentals row is anchored to the exact 10-K
filing that first disclosed it, with SEC's own `filed` date as filing_date.
That's what makes multi-year trend metrics (gross_margin_trend, bvps_cagr in
FundamentalsSnapshot) meaningful rather than lookahead-contaminated.

Coverage: US filers only (EDGAR has no non-US data). A ticker EDGAR doesn't
recognize (no CIK) transparently falls back to the composed YFClient for
*everything*, so European tickers keep working — just without point-in-time
history. Prices, news, insider trades, and earnings-history are always
delegated to YFClient; EDGAR has none of those.

ponytail: one annual (10-K) datapoint per fiscal year, not true trailing-
twelve-month. 10-K duration facts already span the full fiscal year, so this
matches "annual" exactly and skips reconstructing TTM from quarterly 10-Qs
(a genuinely hard problem — differing comparative-period tagging per filer).
Upgrade to quarterly TTM assembly if a strategy needs finer granularity than
annual.

ponytail: debt_to_equity uses total liabilities (us-gaap:Liabilities), not
a narrower "financial debt" figure — EDGAR has no single standard tag for
that. Treat it as a leverage proxy, not a precise interest-bearing-debt ratio.

ponytail: concept tag aliases below cover the common cases (ASC 606 revenue
recognition changed Apple's own tag in FY2018, e.g.) but XBRL tagging varies
per filer and per era. A company using an unlisted alias for a concept will
show None for that field rather than crash — expected, not a bug to chase
per-filer.

Requires a compliant User-Agent identifying the requester (SEC policy, not
optional): set SEC_EDGAR_USER_AGENT="Your Name your@email.com" or pass
user_agent= explicitly.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timedelta

import requests

from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsRecord,
    FinancialMetrics,
    InsiderTrade,
    Price,
)
from hedge_fund.data.yfinance_client import YFClient

logger = logging.getLogger(__name__)

_UA_ENV = "SEC_EDGAR_USER_AGENT"
_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Revenue tag changed across filers/eras (ASC 606 adoption, etc.) — try in order.
_TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet", "SalesRevenueGoodsNet"],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities",
                             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets", "PaymentsForCapitalImprovements"],
    "assets": ["Assets"],
    "assets_current": ["AssetsCurrent"],
    "liabilities": ["Liabilities"],
    "liabilities_current": ["LiabilitiesCurrent"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}
_SHARES_TAGS = ["EntityCommonStockSharesOutstanding"]  # dei namespace, cover-page value


class EdgarClientError(Exception):
    """A SEC EDGAR request failed for infrastructure reasons."""


class EdgarClient:
    """SEC EDGAR-backed DataClient with real point-in-time fundamentals for US filers."""

    # Ticker->CIK is ~800KB and near-static (SEC updates it occasionally, not
    # per-request); shared at class level so callers that construct a fresh
    # EdgarClient() per ticker (the common pattern — see masters_analysis.py)
    # don't each re-download it. One requests.Session's worth of connection
    # state is still per-instance; only this map is shared.
    _shared_cik_map: dict[str, int] | None = None
    _cik_map_lock = threading.Lock()

    def __init__(self, user_agent: str | None = None, timeout: float = 30.0) -> None:
        ua = user_agent or os.environ.get(_UA_ENV)
        if not ua:
            raise EdgarClientError(
                f"SEC EDGAR requires an identifying User-Agent (their policy, not "
                f"optional). Set {_UA_ENV}=\"Your Name your@email.com\" or pass "
                f"user_agent= explicitly."
            )
        self._session = requests.Session()
        self._session.headers["User-Agent"] = ua
        self._timeout = timeout
        self._yf = YFClient()
        self._facts_cache: dict[int, dict] = {}
        self._submissions_cache: dict[int, dict] = {}
        self._price_cache: dict[str, list[Price]] = {}
        self._split_cache: dict[str, object] = {}

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()
        self._yf.close()

    # ------------------------------------------------------------------
    # CIK resolution
    # ------------------------------------------------------------------

    def _load_cik_map(self) -> dict[str, int]:
        if EdgarClient._shared_cik_map is None:
            with EdgarClient._cik_map_lock:
                if EdgarClient._shared_cik_map is None:  # re-check inside the lock
                    data = self._get_json(_TICKER_MAP_URL)
                    EdgarClient._shared_cik_map = {
                        row["ticker"].upper(): row["cik_str"] for row in data.values()
                    }
        return EdgarClient._shared_cik_map

    def _cik_for(self, ticker: str) -> int | None:
        return self._load_cik_map().get(ticker.upper())

    # ------------------------------------------------------------------
    # Raw EDGAR fetch helpers
    # ------------------------------------------------------------------

    def _get_json(self, url: str) -> dict:
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except requests.RequestException as exc:
            raise EdgarClientError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise EdgarClientError(f"GET {url} returned {resp.status_code}")
        return resp.json()

    def _facts(self, cik: int) -> dict:
        if cik not in self._facts_cache:
            self._facts_cache[cik] = self._get_json(_FACTS_URL.format(cik=cik))
        return self._facts_cache[cik]

    def _submissions(self, cik: int) -> dict:
        if cik not in self._submissions_cache:
            self._submissions_cache[cik] = self._get_json(_SUBMISSIONS_URL.format(cik=cik))
        return self._submissions_cache[cik]

    # ------------------------------------------------------------------
    # Financial Metrics — real point-in-time annual fundamentals
    # ------------------------------------------------------------------

    def get_financial_metrics(
        self,
        ticker: str,
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
    ) -> list[FinancialMetrics]:
        cik = self._cik_for(ticker)
        if cik is None:
            logger.info("%s: no SEC CIK, falling back to YFClient", ticker)
            return self._yf.get_financial_metrics(ticker, end_date, period, limit)

        facts = self._facts(cik)
        anchors = self._fiscal_year_anchors(facts)
        anchors = [a for a in anchors if a["filed"] <= end_date]
        if not anchors:
            return []
        anchors = anchors[:limit]

        prices = self._price_series(ticker, anchors[-1]["filed"])

        rows = []
        for i, anchor in enumerate(anchors):
            prior = anchors[i + 1] if i + 1 < len(anchors) else None
            rows.append(self._build_metrics(ticker, facts, anchor, prior, prices, period))
        return rows

    def _fiscal_year_anchors(self, facts: dict) -> list[dict]:
        """One anchor per fiscal year: the first 10-K to report that year-end.

        Anchored on us-gaap:Assets (always tagged, instant, at fiscal year
        end) so every other concept is looked up from the SAME filing
        (same accn) — no mixing figures from a restated later filing.
        """
        assets = facts.get("facts", {}).get("us-gaap", {}).get("Assets", {})
        entries = [
            e for units in assets.get("units", {}).values() for e in units
            if e.get("form") in ("10-K", "10-K/A") and not e.get("start")
        ]
        best_by_end: dict[str, dict] = {}
        for e in entries:
            end = e.get("end")
            if not end:
                continue
            if end not in best_by_end or e["filed"] < best_by_end[end]["filed"]:
                best_by_end[end] = e
        return sorted(best_by_end.values(), key=lambda e: e["end"], reverse=True)

    def _lookup(self, facts: dict, tags: list[str], accn: str, end: str | None = None,
                namespace: str = "us-gaap") -> float | None:
        ns = facts.get("facts", {}).get(namespace, {})
        for tag in tags:
            concept = ns.get(tag)
            if not concept:
                continue
            for entries in concept.get("units", {}).values():
                for e in entries:
                    if e.get("accn") == accn and (end is None or e.get("end") == end):
                        return e.get("val")
        return None

    def _build_metrics(
        self, ticker: str, facts: dict, anchor: dict, prior: dict | None,
        prices: list[Price], period: str,
    ) -> FinancialMetrics:
        accn, end, filed = anchor["accn"], anchor["end"], anchor["filed"]
        g = lambda key: self._lookup(facts, _TAGS[key], accn, end)

        revenue = g("revenue")
        cost_of_revenue = g("cost_of_revenue")
        gross_profit = g("gross_profit")
        if gross_profit is None and revenue is not None and cost_of_revenue is not None:
            gross_profit = revenue - cost_of_revenue
        operating_income = g("operating_income")
        net_income = g("net_income")
        eps_diluted = g("eps_diluted")
        op_cash_flow = g("operating_cash_flow")
        capex = g("capex")
        assets_current = g("assets_current")
        liabilities = g("liabilities")
        liabilities_current = g("liabilities_current")
        equity = g("equity")
        shares = self._lookup(facts, _SHARES_TAGS, accn, end=None, namespace="dei")

        prior_revenue = None
        if prior is not None:
            prior_revenue = self._lookup(facts, _TAGS["revenue"], prior["accn"], prior["end"])

        # yfinance prices are split-adjusted to today's share basis; shares
        # and EPS here are as-filed (pre-split, for years before a later
        # split). Scale the price back up by every split since *filed* so
        # price, shares, and EPS all sit on the SAME nominal basis — else
        # market_cap and P/E silently come out ~N times too small for any
        # company that has split its stock since the filing.
        raw_price = self._price_on_or_after(prices, filed)
        price = raw_price * self._split_factor_since(ticker, filed) if raw_price else None

        return FinancialMetrics(
            ticker=ticker,
            report_period=end,
            period=period,
            currency="USD",
            filing_date=filed,
            filing_datetime=None,
            market_cap=(price * shares) if price and shares else None,
            price_to_earnings_ratio=(price / eps_diluted) if price and eps_diluted and eps_diluted > 0 else None,
            gross_margin=(gross_profit / revenue) if gross_profit is not None and revenue else None,
            operating_margin=(operating_income / revenue) if operating_income is not None and revenue else None,
            net_margin=(net_income / revenue) if net_income is not None and revenue else None,
            return_on_equity=(net_income / equity) if net_income is not None and equity else None,
            current_ratio=(assets_current / liabilities_current) if assets_current and liabilities_current else None,
            debt_to_equity=(liabilities / equity) if liabilities is not None and equity else None,
            revenue_growth=((revenue - prior_revenue) / prior_revenue)
                if revenue is not None and prior_revenue else None,
            earnings_per_share=eps_diluted,
            book_value_per_share=(equity / shares) if equity is not None and shares else None,
            free_cash_flow_per_share=((op_cash_flow - capex) / shares)
                if op_cash_flow is not None and capex is not None and shares else None,
        )

    # ------------------------------------------------------------------
    # Price lookup (for market_cap / P/E — EDGAR has no price data)
    # ------------------------------------------------------------------

    def _price_series(self, ticker: str, since: str) -> list[Price]:
        if ticker not in self._price_cache:
            start = (datetime.strptime(since, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")
            end = date.today().strftime("%Y-%m-%d")
            self._price_cache[ticker] = self._yf.get_prices(ticker, start, end)
        return self._price_cache[ticker]

    def _price_on_or_after(self, prices: list[Price], filed: str) -> float | None:
        """First close on/after *filed* — the price once the filing was public."""
        candidates = sorted((p for p in prices if p.time >= filed), key=lambda p: p.time)
        return candidates[0].close if candidates else None

    def _split_factor_since(self, ticker: str, filed: str) -> float:
        """Cumulative split ratio for every split after *filed* (1.0 if none)."""
        if ticker not in self._split_cache:
            try:
                splits = self._yf._ticker(ticker).splits
            except Exception:
                splits = None
            self._split_cache[ticker] = splits
        splits = self._split_cache[ticker]
        if splits is None or splits.empty:
            return 1.0
        filed_dt = datetime.strptime(filed, "%Y-%m-%d")
        factor = 1.0
        for ts, ratio in splits.items():
            if ts.to_pydatetime().replace(tzinfo=None) > filed_dt:
                factor *= float(ratio)
        return factor

    # ------------------------------------------------------------------
    # Company Facts
    # ------------------------------------------------------------------

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        cik = self._cik_for(ticker)
        if cik is None:
            return self._yf.get_company_facts(ticker)
        sub = self._submissions(cik)
        return CompanyFacts(
            ticker=ticker,
            is_active=True,
            name=sub.get("name"),
            cik=str(cik),
            sic_code=sub.get("sic"),
            sic_industry=sub.get("sicDescription"),
            exchange=(sub.get("exchanges") or [None])[0],
        )

    # ------------------------------------------------------------------
    # Market cap
    # ------------------------------------------------------------------

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        metrics = self.get_financial_metrics(ticker, end_date, limit=1)
        return metrics[0].market_cap if metrics else None

    # ------------------------------------------------------------------
    # Everything EDGAR doesn't have — delegate to YFClient
    # ------------------------------------------------------------------

    def get_prices(self, ticker: str, start_date: str, end_date: str,
                    interval: str = "day", interval_multiplier: int = 1) -> list[Price]:
        return self._yf.get_prices(ticker, start_date, end_date, interval, interval_multiplier)

    def get_news(self, ticker: str, end_date: str, start_date: str | None = None,
                 limit: int = 1000) -> list[CompanyNews]:
        return self._yf.get_news(ticker, end_date, start_date, limit)

    def get_insider_trades(self, ticker: str, end_date: str, start_date: str | None = None,
                            limit: int = 1000) -> list[InsiderTrade]:
        return self._yf.get_insider_trades(ticker, end_date, start_date, limit)

    def get_earnings(self, ticker: str) -> Earnings | None:
        return self._yf.get_earnings(ticker)

    def get_earnings_history(self, ticker: str, limit: int = 12) -> list[EarningsRecord]:
        return self._yf.get_earnings_history(ticker, limit)
