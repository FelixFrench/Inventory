from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    barcode: str = Field(pattern=r'^\d{8,14}$')


class ScanResponse(BaseModel):
    barcode: str
    session_delta: int


class StartSessionRequest(BaseModel):
    type: Literal["in", "out"]


class FieldStatus(BaseModel):
    value: Optional[Any]
    status: Literal["resolved", "loading", "failed"]


class SessionItem(BaseModel):
    barcode: str
    delta: int
    inventory_quantity: int
    first_scanned_at: str
    name: FieldStatus
    brand: FieldStatus
    quantity: FieldStatus
    price: FieldStatus
    off_url: str
    price_url: Optional[str]


class SessionObject(BaseModel):
    id: int
    type: str
    started_at: str
    recovered_at: Optional[str]
    # retailer_id integer (always Sainsbury's this sprint); provisional wire identity
    # for 1b, to be ratified in 2c. Frontend does not consume it yet (deferred to 2e).
    retailer: int
    total_delta: int
    items: list[SessionItem]


class SessionResponse(BaseModel):
    session: Optional[SessionObject]


class ConfirmResponse(BaseModel):
    applied_items: int
    session_id: int


class DiscardResponse(BaseModel):
    discarded_session_id: int


class DeltaUpdateRequest(BaseModel):
    delta: int = Field(ge=0)


class SetMinimumQuantityRequest(BaseModel):
    minimum_quantity: int = Field(ge=0)


class DocsLoginRequest(BaseModel):
    api_key: str
