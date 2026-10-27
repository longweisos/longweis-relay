"""Public Relay mode identifiers shared by server and thin clients."""

from enum import Enum


class RelayBillingMode(str, Enum):
    EXECUTION_OPTIMIZATION = "execution_optimization"
    MEMORY_RETRIEVAL = "memory_retrieval"
