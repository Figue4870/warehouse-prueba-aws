from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class AlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    warehouse_id: str
    type: str
    message: str
    movement_id: Optional[int]
    created_at: datetime
    notified_at: Optional[datetime] = None
    notification_channel: Optional[str] = None
