from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReportType(str, Enum):
    MOVEMENTS_SUMMARY = "MOVEMENTS_SUMMARY"  # entradas/salidas por producto en un rango de fechas
    STOCK_VALUATION = "STOCK_VALUATION"      # inventario valorizado (costo y precio de venta)
    LOW_STOCK = "LOW_STOCK"                  # productos con poco disponible


class ReportParams(BaseModel):
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    threshold: int = Field(5, ge=0, description="Para LOW_STOCK: disponible menor o igual a este valor")

    @model_validator(mode="after")
    def check_range(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from no puede ser posterior a date_to")
        return self


class ReportRequest(BaseModel):
    type: ReportType
    params: ReportParams = Field(default_factory=ReportParams)


class ReportJobAccepted(BaseModel):
    job_id: str
    status: str
    status_url: str


class ReportJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    warehouse_id: str
    report_type: str
    params: dict[str, Any]
    status: str
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    attempts: int
    correlation_id: Optional[str] = None
    requested_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
