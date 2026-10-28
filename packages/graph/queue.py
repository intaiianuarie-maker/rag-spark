"""Compatibility API; concrete SDK implementation lives behind adapters."""

import sys

from .adapters import redis_queue as _implementation
from .adapters.redis_queue import (
    Claim as Claim,
)
from .adapters.redis_queue import (
    GraphJobDisposition as GraphJobDisposition,
)
from .adapters.redis_queue import (
    GraphQueueUnavailable as GraphQueueUnavailable,
)
from .adapters.redis_queue import (
    _as_str as _as_str,
)
from .adapters.redis_queue import (
    _now as _now,
)
from .adapters.redis_queue import (
    _prefix as _prefix,
)
from .adapters.redis_queue import (
    ack as ack,
)
from .adapters.redis_queue import (
    claim as claim,
)
from .adapters.redis_queue import (
    claim_tokens_key as claim_tokens_key,
)
from .adapters.redis_queue import (
    claims_key as claims_key,
)
from .adapters.redis_queue import (
    close_client as close_client,
)
from .adapters.redis_queue import (
    delayed_key as delayed_key,
)
from .adapters.redis_queue import (
    delete_payload as delete_payload,
)
from .adapters.redis_queue import (
    enqueue as enqueue,
)
from .adapters.redis_queue import (
    enqueue_graph_job as enqueue_graph_job,
)
from .adapters.redis_queue import (
    get_client as get_client,
)
from .adapters.redis_queue import (
    get_payload as get_payload,
)
from .adapters.redis_queue import (
    lease_seconds as lease_seconds,
)
from .adapters.redis_queue import (
    promote_due as promote_due,
)
from .adapters.redis_queue import (
    queue_depth as queue_depth,
)
from .adapters.redis_queue import (
    queue_key as queue_key,
)
from .adapters.redis_queue import (
    recover_expired as recover_expired,
)
from .adapters.redis_queue import (
    renew as renew,
)
from .adapters.redis_queue import (
    schedule_retry as schedule_retry,
)

# Preserve module identity for dependency injection and existing callers.
sys.modules[__name__] = _implementation
