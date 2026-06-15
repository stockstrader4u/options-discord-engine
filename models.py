from pydantic import BaseModel


class FlowAlert(BaseModel):
    ticker: str
    contract: str
    premium: str
    sentiment: str
    note: str | None = None
    score: int | None = None
    levels: str | None = None
    catalyst: str | None = None
    flow_type: str | None = None