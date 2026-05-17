from typing import Literal

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    barcode: str = Field(pattern=r'^\d{8,14}$')


class ScanResponse(BaseModel):
    status: str
    direction: str
    known: bool


class ModeRequest(BaseModel):
    mode: Literal["in", "out"]


class ModeResponse(BaseModel):
    mode: str
