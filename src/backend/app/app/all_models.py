"""
Importa TODOS los modelos de SQLAlchemy. Los workers corren sin la API, así que
deben importar esto para que las llaves foráneas entre tablas (por ejemplo
inventory_alerts.product_id -> products.id) se puedan resolver.
"""

from app.events import models as events_models  # noqa: F401
from app.modules.alerts import models as alerts_models  # noqa: F401
from app.modules.inventory import models as inventory_models  # noqa: F401
from app.modules.products import models as products_models  # noqa: F401
from app.modules.reports import models as reports_models  # noqa: F401
from app.modules.transfers import models as transfers_models  # noqa: F401
