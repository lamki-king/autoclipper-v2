import importlib
import sys

# Keep Render's existing `uvicorn app.main:app` command, but route it to the
# production application. The legacy main.py remains available as the engine.
production = importlib.import_module('.production', __name__)
sys.modules[__name__ + '.main'] = production
main = production
