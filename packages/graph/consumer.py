"""Compatibility API; concrete SDK implementation lives behind adapters."""

import sys

from .adapters import redis_consumer as _implementation
from .adapters.redis_consumer import (
    DEFAULT_HEARTBEAT_STALE_SECONDS as DEFAULT_HEARTBEAT_STALE_SECONDS,
)
from .adapters.redis_consumer import (
    DEFAULT_MAX_BACKOFF_SECONDS as DEFAULT_MAX_BACKOFF_SECONDS,
)
from .adapters.redis_consumer import (
    DEFAULT_MAX_CONSECUTIVE_FAILURES as DEFAULT_MAX_CONSECUTIVE_FAILURES,
)
from .adapters.redis_consumer import (
    DEFAULT_RESTART_BACKOFF_SECONDS as DEFAULT_RESTART_BACKOFF_SECONDS,
)
from .adapters.redis_consumer import (
    JOB_PAYLOAD_REQUIRED as JOB_PAYLOAD_REQUIRED,
)
from .adapters.redis_consumer import (
    QueueObservables as QueueObservables,
)
from .adapters.redis_consumer import (
    SupervisorState as SupervisorState,
)
from .adapters.redis_consumer import (
    _default_consumer as _default_consumer,
)
from .adapters.redis_consumer import (
    _heartbeat_loop as _heartbeat_loop,
)
from .adapters.redis_consumer import (
    _invoke_consumer as _invoke_consumer,
)
from .adapters.redis_consumer import (
    _now as _now,
)
from .adapters.redis_consumer import (
    _prefixed_payloads as _prefixed_payloads,
)
from .adapters.redis_consumer import (
    _sleep_or_stop as _sleep_or_stop,
)
from .adapters.redis_consumer import (
    collect_queue_observables as collect_queue_observables,
)
from .adapters.redis_consumer import (
    consumer_enabled as consumer_enabled,
)
from .adapters.redis_consumer import (
    graph_mode as graph_mode,
)
from .adapters.redis_consumer import (
    heartbeat_key as heartbeat_key,
)
from .adapters.redis_consumer import (
    logger as logger,
)
from .adapters.redis_consumer import (
    publish_heartbeat as publish_heartbeat,
)
from .adapters.redis_consumer import (
    run_supervised_graph_consumer as run_supervised_graph_consumer,
)

# Preserve module identity for dependency injection and existing callers.
sys.modules[__name__] = _implementation
