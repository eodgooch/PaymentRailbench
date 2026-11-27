from railbench.common.serve import serve
from railbench.simulator.app import create_app

serve("simulator", create_app, lambda settings: settings.simulator_port)
