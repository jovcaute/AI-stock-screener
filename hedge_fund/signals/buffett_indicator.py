"""Buffett, indicator-only — same checklist, no reasoning text in the output.

For a cheap first-pass gate over a large universe: the persona still works
through the full checklist internally, but the response is bare
{signal, confidence} JSON. Measured ~95% smaller output per call than the
full BuffettAgent (the reasoning text was most of the response), which
dominates cost since output tokens price several times higher than input on
most providers — roughly 60% cheaper per call, at the cost of losing the
reasoning trail and a small chance of a different verdict than the
full-reasoning agent would reach on the same data (skipping articulation of
reasoning can, on some names, change the verdict itself, not just its
format — measured on a small sample, not something this class can prevent).

Not a replacement for BuffettAgent: use this to decide whether a name is
worth spending the other three personas (and BuffettAgent itself, for a
reasoning trail) on, not as the final word on a name that passes.
"""

from __future__ import annotations

from hedge_fund.data.protocol import DataClient
from hedge_fund.features.snapshot import FundamentalsSnapshot, build_snapshot
from hedge_fund.signals.buffett import BuffettAgent


class BuffettIndicatorAgent(BuffettAgent):
    """BuffettAgent with reasoning stripped from the requested output."""

    @property
    def name(self) -> str:
        return "buffett_indicator"

    def build_snapshot(self, ticker: str, date: str, data_client: DataClient) -> FundamentalsSnapshot:
        """periods=6, not the full-agent default of 20 — this pass only
        decides whether the name is worth spending on, so a short trend
        window is enough. EdgarClient fetches the entire companyfacts JSON
        regardless of periods (network cost is fixed either way); this only
        shrinks the rendered table, i.e. cuts input tokens, not wall-clock
        time. Still comfortably above MIN_PERIODS=4.
        """
        return build_snapshot(ticker, date, data_client, periods=6)

    def get_system_prompt(self) -> str:
        full = super().get_system_prompt()
        # Swap the schema line only — the checklist/rules above it stay
        # identical, so the persona reasons the same way, it just doesn't
        # write the reasoning down.
        marker = 'Respond with JSON only, in exactly this schema:'
        head = full.split(marker)[0]
        return head + (
            'Respond with JSON only, in EXACTLY this schema — no reasoning '
            'field, no extra keys, no prose outside the JSON:\n'
            '{"signal": "bullish" | "bearish" | "neutral", "confidence": <0-100>}'
        )
