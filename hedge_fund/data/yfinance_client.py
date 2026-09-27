"""Free data provider backed by yfinance (unofficial Yahoo Finance scraper).

Drop-in alternative to FDClient for anyone without a Financial Datasets API
key — see the DataClient protocol docstring for the extension contract this
implements.

ponytail: NOT point-in-time. Yahoo only exposes today's live snapshot of
fundamentals (no historical filing-date archive), so get_financial_metrics
and get_market_cap always return current data regardless of the requested
end_date/backtest date — using this client in a historical backtest risks
look-ahead bias. Fine for live/paper runs against today; upgrade to FDClient
(or another point-in-time provider) before trusting backtest results.

ponytail: also unofficial/undocumented — Yahoo can rate-limit or change
response shapes without notice (see yfinance's own disclaimers). Upgrade to
a licensed provider if reliability matters more than cost.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import yfinance as yf

from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsData,
    EarningsRecord,
    FinancialMetrics,
    InsiderTrade,
    Price,
)

logger = logging.getLogger(__name__)

_INTERVAL_MAP = {"day": "1d", "week": "1wk", "month": "1mo"}


class YFClientError(Exception):
    """A yfinance call failed for infrastructure reasons (network, rate limit)."""


class YFClient:
    """yfinance-backed implementation of the DataClient protocol."""

    def __init__(self) -> None:
        self._tickers: dict[str, yf.Ticker] = {}

    def __enter__(self) -> YFClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._tickers.clear()

    def _ticker(self, symbol: str) -> yf.Ticker:
        if symbol not in self._tickers:
            self._tickers[symbol] = yf.Ticker(symbol)
        return self._tickers[symbol]

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

    def get_prices(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        interval: str = "day",
        interval_multiplier: int = 1,
    ) -> list[Price]:
        yf_interval = _INTERVAL_MAP.get(interval, "1d")
        try:
            hist = self._ticker(ticker).history(
                start=start_date, end=end_date, interval=yf_interval,
            )
        except Exception as exc:  # yfinance raises assorted requests/HTTP errors
            raise YFClientError(f"get_prices({ticker}) failed: {exc}") from exc
        if hist.empty:
            return []
        return [
            Price(
                open=float(row.Open),
                close=float(row.Close),
                high=float(row.High),
                low=float(row.Low),
                volume=int(row.Volume) if row.Volume == row.Volume else 0,  # NaN guard
                time=idx.strftime("%Y-%m-%d"),
            )
            for idx, row in hist.iterrows()
        ]

    # ------------------------------------------------------------------
    # Financial Metrics (current snapshot only — see module docstring)
    # ------------------------------------------------------------------

    def get_financial_metrics(
        self,
        ticker: str,
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
    ) -> list[FinancialMetrics]:
        try:
            info = self._ticker(ticker).info
        except Exception as exc:
            raise YFClientError(f"get_financial_metrics({ticker}) failed: {exc}") from exc
        if not info or info.get("regularMarketPrice") is None and info.get("marketCap") is None:
            return []
        m = FinancialMetrics(
            ticker=ticker,
            report_period=end_date,
            period=period,
            currency=info.get("currency"),
            filing_date=None,  # unknown — yfinance has no filing-date archive
            market_cap=info.get("marketCap"),
            enterprise_value=info.get("enterpriseValue"),
            price_to_earnings_ratio=info.get("trailingPE"),
            price_to_book_ratio=info.get("priceToBook"),
            price_to_sales_ratio=info.get("priceToSalesTrailing12Months"),
            enterprise_value_to_ebitda_ratio=info.get("enterpriseToEbitda"),
            enterprise_value_to_revenue_ratio=info.get("enterpriseToRevenue"),
            peg_ratio=info.get("pegRatio") or info.get("trailingPegRatio"),
            gross_margin=info.get("grossMargins"),
            operating_margin=info.get("operatingMargins"),
            net_margin=info.get("profitMargins"),
            return_on_equity=info.get("returnOnEquity"),
            return_on_assets=info.get("returnOnAssets"),
            current_ratio=info.get("currentRatio"),
            quick_ratio=info.get("quickRatio"),
            debt_to_equity=info.get("debtToEquity"),
            revenue_growth=info.get("revenueGrowth"),
            earnings_growth=info.get("earningsGrowth"),
            payout_ratio=info.get("payoutRatio"),
            earnings_per_share=info.get("trailingEps"),
            book_value_per_share=info.get("bookValue"),
        )
        return [m][:limit]

    # ------------------------------------------------------------------
    # News
    # ------------------------------------------------------------------

    def get_news(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[CompanyNews]:
        try:
            raw = self._ticker(ticker).news or []
        except Exception as exc:
            raise YFClientError(f"get_news({ticker}) failed: {exc}") from exc
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_dt = (
            datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if start_date else None
        )
        out = []
        for item in raw:
            content = item.get("content", item)
            pub = content.get("pubDate")
            date_str = None
            if pub:
                try:
                    pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                except ValueError:
                    pub_dt = None
                if pub_dt is not None:
                    if pub_dt > end_dt or (start_dt and pub_dt < start_dt):
                        continue
                    date_str = pub_dt.strftime("%Y-%m-%d")
            out.append(CompanyNews(
                ticker=ticker,
                title=content.get("title", ""),
                source=(content.get("provider") or {}).get("displayName", "Yahoo Finance"),
                date=date_str,
                url=(content.get("canonicalUrl") or {}).get("url"),
            ))
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------
    # Insider Trades
    # ------------------------------------------------------------------

    def get_insider_trades(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[InsiderTrade]:
        try:
            df = self._ticker(ticker).insider_transactions
        except Exception as exc:
            raise YFClientError(f"get_insider_trades({ticker}) failed: {exc}") from exc
        if df is None or df.empty:
            return []
        out = []
        for _, row in df.iterrows():
            filing_date = str(row.get("Start Date") or "")[:10]
            if end_date and filing_date and filing_date > end_date:
                continue
            if start_date and filing_date and filing_date < start_date:
                continue
            shares = row.get("Shares")
            value = row.get("Value")
            position = str(row.get("Position") or "")
            out.append(InsiderTrade(
                ticker=ticker,
                name=str(row.get("Insider") or ""),
                filing_date=filing_date or end_date,
                is_board_director="director" in position.lower(),
                title=position or None,
                transaction_date=filing_date or None,
                transaction_type=str(row.get("Transaction") or "") or None,
                transaction_shares=float(shares) if shares is not None else None,
                transaction_value=float(value) if value is not None else None,
                transaction_price_per_share=(
                    float(value) / float(shares) if shares else None
                ),
            ))
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------
    # Company Facts
    # ------------------------------------------------------------------

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        try:
            info = self._ticker(ticker).info
        except Exception as exc:
            raise YFClientError(f"get_company_facts({ticker}) failed: {exc}") from exc
        if not info or not info.get("longName"):
            return None
        return CompanyFacts(
            ticker=ticker,
            is_active=True,
            name=info.get("longName") or info.get("shortName"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            exchange=info.get("exchange"),
            location=", ".join(
                p for p in (info.get("city"), info.get("country")) if p
            ) or None,
        )

    # ------------------------------------------------------------------
    # Earnings
    # ------------------------------------------------------------------

    def get_earnings(self, ticker: str) -> Earnings | None:
        history = self.get_earnings_history(ticker, limit=1)
        if not history:
            return None
        r = history[0]
        return Earnings(
            ticker=r.ticker,
            report_period=r.report_period,
            fiscal_period=r.fiscal_period,
            currency=r.currency,
            quarterly=r.quarterly,
        )

    def get_earnings_history(
        self,
        ticker: str,
        limit: int = 12,
    ) -> list[EarningsRecord]:
        try:
            dates = self._ticker(ticker).get_earnings_dates(limit=limit)
        except Exception as exc:
            raise YFClientError(f"get_earnings_history({ticker}) failed: {exc}") from exc
        if dates is None or dates.empty:
            return []
        out = []
        for idx, row in dates.iloc[:limit].iterrows():
            estimate = row.get("EPS Estimate")
            actual = row.get("Reported EPS")
            surprise_pct = row.get("Surprise(%)")
            surprise = None
            if surprise_pct == surprise_pct:  # not NaN
                surprise = "BEAT" if surprise_pct > 0 else "MISS" if surprise_pct < 0 else "MEET"
            out.append(EarningsRecord(
                ticker=ticker,
                report_period=idx.strftime("%Y-%m-%d"),
                source_type="yfinance",
                filing_date=idx.strftime("%Y-%m-%d"),
                fiscal_period=None,
                quarterly=EarningsData(
                    earnings_per_share=float(actual) if actual == actual else None,
                    estimated_earnings_per_share=float(estimate) if estimate == estimate else None,
                    eps_surprise=surprise,
                ),
            ))
        return out

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        try:
            info = self._ticker(ticker).info
        except Exception as exc:
            raise YFClientError(f"get_market_cap({ticker}) failed: {exc}") from exc
        return info.get("marketCap")
