"""Public, append-only market-data facilities for the isolated ARX campaign.

Nothing in this package reads credentials or exposes an authenticated request.
"""

from .bitget_uta_v3 import BitgetUtaV3PublicClient, EndpointCapability
from .metrics import CollectionMetrics, collection_metrics
from .normalize import NormalizedRecord, normalize_response
from .persistence import AppendOnlyJsonlStore

__all__ = [
    "AppendOnlyJsonlStore", "BitgetUtaV3PublicClient", "CollectionMetrics",
    "EndpointCapability", "NormalizedRecord", "collection_metrics", "normalize_response",
]
