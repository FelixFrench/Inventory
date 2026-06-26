"""Single source of truth for the application version.

Both the OFF worker's User-Agent (src/worker/off.py) and the FastAPI app's
OpenAPI version (src/api/main.py) read from here. Carries the v3.0.0 target,
ratified at sprint 4d.
"""

__version__: str = "3.0.0"
