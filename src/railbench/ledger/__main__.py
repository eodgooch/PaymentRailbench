from railbench.common.serve import serve
from railbench.ledger.app import create_app

serve("ledger", create_app, lambda settings: settings.ledger_port)
