from __future__ import annotations

from data_sources.config.schema import ConnectorConfig
from data_sources.core.connector import Connector
from data_sources.core.registry import create_connector
from data_sources.store.base import Store


async def init_connector(config: ConnectorConfig, store: Store) -> Connector:
    """Instantiate a connector and ensure the tables/indexes it declares exist in `store`.

    Schema provisioning happens here — at connector creation — rather than when the
    connector class is registered, since `register_connector` only runs once at import
    time and has no store to provision against.
    """
    connector = create_connector(config)
    await store.ensure_tables(connector.models)
    connector.store = store
    return connector
