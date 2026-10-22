from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class AnalyzeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=12)
    timeframe: Literal["intraday"] = "intraday"


class WatchlistRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=100)


class ResearchRequest(BaseModel):
    question: str = Field(default="Research a robust, testable strategy improvement.", min_length=5, max_length=4000)
    symbols: list[str] = Field(default_factory=list, max_length=30)


class TradeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=12)
    side: Literal["long", "short"]
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    stop_loss: float = Field(gt=0, allow_inf_nan=False)
    take_profit: float = Field(gt=0, allow_inf_nan=False)
    thesis: str = Field(min_length=10, max_length=3000)
    evidence: list[str] = Field(default_factory=list, max_length=20)
    strategy_id: str = Field(default="ai-proposed", max_length=100)
    timeframe: Literal["intraday", "swing"] = "intraday"

    @field_validator("symbol")
    @classmethod
    def clean_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized.replace(".", "").replace("-", "").isalnum():
            raise ValueError("Symbol contains unsupported characters.")
        return normalized

    @model_validator(mode="after")
    def validate_price_direction(self):
        if self.side == "long" and not self.stop_loss < self.entry_price < self.take_profit:
            raise ValueError("For a long proposal, stop_loss < entry_price < take_profit is required.")
        if self.side == "short" and not self.take_profit < self.entry_price < self.stop_loss:
            raise ValueError("For a short proposal, take_profit < entry_price < stop_loss is required.")
        return self


class ApprovalRequest(BaseModel):
    version: int = Field(ge=1)


class RejectRequest(BaseModel):
    version: int = Field(ge=1)


class ClosePositionRequest(BaseModel):
    # Accepted for backwards compatibility with the current UI, but the backend deliberately
    # ignores it and closes at the last server-recorded mark.
    simulated_exit_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class UpdateStopsRequest(BaseModel):
    stop_loss: float = Field(gt=0, allow_inf_nan=False)
    take_profit: float = Field(gt=0, allow_inf_nan=False)


class JobResponse(BaseModel):
    id: str
    task: str
    status: str



class ResolveBrokerIntentRequest(BaseModel):
    # This is deliberately explicit: recovery is allowed only after the operator
    # has checked both the Alpaca Paper order list and its positions.
    confirm_no_order_and_no_position: Literal[True]
