from pydantic import BaseModel
from typing import Literal


class FlowAlert(BaseModel):
    ticker: str
    contract: str
    premium: int
    sentiment: Literal["bullish", "bearish", "neutral"]
    note: str | None = None
    score: int | None = None
    levels: str | None = None
    catalyst: str | None = None
    flow_type: str | None = None
    source: Literal["flow", "news", "earnings", "scanner", "macro"] = "flow"
    dte_bucket: Literal["weeklies", "next_week", "monthly", "unknown"] = "unknown"
    spot_price: float | None = None
    # Real underlying spot price at time of trade, when known (e.g. from
    # JarvisFlow's own spot_Price field). When this is set, enrichment.py
    # uses it directly instead of falling back to its mock spot table —
    # this is what makes enrichment/moneyness accurate for any ticker,
    # not just the handful hardcoded in the mock table.
