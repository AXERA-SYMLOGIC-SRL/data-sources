try:
    import fastapi  # noqa: F401
except ImportError as exc:
    raise ImportError(
        "data_sources.web requires the 'api' extra. Install it with "
        "`pip install axera-data-sources[api]`."
    ) from exc

from data_sources.web.app import build_connectors_router
from data_sources.web.router import build_router

__all__ = [
    "build_connectors_router",
    "build_router",
]
