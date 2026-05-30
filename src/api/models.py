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
    weight: FieldStatus
    price: FieldStatus


class SessionObject(BaseModel):
    id: int
    type: str
    started_at: str
    recovered_at: Optional[str]
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
    delta: int
