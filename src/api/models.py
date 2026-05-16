from pydantic import BaseModel
from typing import Literal


class ScanRequest(BaseModel):
    barcode: str


class ScanResponse(BaseModel):
    status: str
    direction: str
    known: bool


class ModeRequest(BaseModel):
    mode: Literal["in", "out"]


class ModeResponse(BaseModel):
    mode: str
