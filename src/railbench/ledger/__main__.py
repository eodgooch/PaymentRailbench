from railbench.common.serve import serve
from railbench.ledger.app import create_app

serve(
    "ledger",
    create_app,
    port=lambda settings: settings.ledger_port,
    database_role=lambda settings: settings.ledger_database_role,
)
