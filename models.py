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