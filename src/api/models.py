from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    barcode: str = Field(pattern=r'^\d{8,14}$')


class ScanResponse(BaseModel):
    barcode: str
    in_session: bool
    session_delta: Optional[int] = None


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
    product_page_url: str
    price_url: Optional[str]


class SessionObject(BaseModel):
    id: int
    type: str
    started_at: str
    recovered_at: Optional[str]
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


# --- Product groups ----------------------------------------------

class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1)
    minimum_quantity: int = Field(default=0, ge=0)


class UpdateGroupRequest(BaseModel):
    """Partial update: send name, minimum_quantity, or both. ``minimum_quantity`` of 0
    clears the minimum (organisational-only group)."""
    name: Optional[str] = Field(default=None, min_length=1)
    minimum_quantity: Optional[int] = Field(default=None, ge=0)


class AddVariantMemberRequest(BaseModel):
    """Group-side: add a variant (by barcode; retailer id from the request dependency).

    Barcode format is not pattern-validated here, consistent with the path-based barcode
    endpoints (set-minimum, product-side membership): membership relies on the barcodes-row
    existence check (404 barcode_not_found), not a syntactic guard."""
    barcode: str = Field(min_length=1)


class AddSubgroupRequest(BaseModel):
    child_group_id: int


class AddGroupMembershipRequest(BaseModel):
    """Product-side: add this variant to the given group."""
    group_id: int


class GroupSummary(BaseModel):
    group_id: int
    name: str
    minimum_quantity: int
    total_quantity: int
    low_stock: bool
    shortfall: int
    group_page_url: str


class VariantMember(BaseModel):
    barcode: str
    retailer_id: int
    name: Optional[str]
    brand: Optional[str]
    product_quantity: Optional[str]
    current_quantity: int
    product_page_url: str


class SubgroupMember(BaseModel):
    id: int
    name: str
    group_page_url: str


class GroupDetail(GroupSummary):
    variants: list[VariantMember]
    subgroups: list[SubgroupMember]


class ProductGroupMembership(BaseModel):
    id: int
    name: str
    group_page_url: str


class ProductDetail(BaseModel):
    barcode: str
    name: Optional[str]
    brand: Optional[str]
    product_quantity: Optional[str]
    minimum_quantity: int
    current_quantity: int
    price_pence: Optional[int]
    price_type: Optional[str]
    product_url: Optional[str]
    off_url: str
    groups: list[ProductGroupMembership]


class LowStockGroupItem(BaseModel):
    group_id: int
    name: str
    have: int
    need: int
    short: int


class LowStockProductItem(BaseModel):
    barcode: str
    name: str
    brand: Optional[str]
    have: int
    need: int
    short: int


class LowStockReport(BaseModel):
    groups: list[LowStockGroupItem]
    products: list[LowStockProductItem]
