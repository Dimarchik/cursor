"""Round-robin failover across several RPC/WSS endpoints.

Free-tier Solana RPC/WSS endpoints rate-limit or drop connections under load.
Rather than hardcode one provider, `EndpointRotator` holds an ordered list and
rotates past any endpoint that keeps failing, so one flaky free node degrades
throughput instead of stalling the bot.
"""

import time

from utils.logger import get_logger

logger = get_logger(__name__)

# Consecutive failures on the active endpoint before rotating to the next one.
DEFAULT_FAILURE_THRESHOLD = 3

# How long a failed endpoint is skipped before it gets another chance. Short
# enough that a transient blip recovers within a run, long enough that a
# dead/rate-limited endpoint does not keep eating the rotation's attention.
DEFAULT_COOLDOWN_SECONDS = 60.0


def parse_endpoints(value: str) -> list[str]:
    """Split a config value into one or more endpoint URLs.

    Args:
        value: Either a single URL, or several comma-separated URLs (the
            `.env` convention this bot uses for RPC/WSS failover lists).

    Returns:
        Non-empty, order-preserving, de-duplicated list of trimmed URLs.

    Raises:
        ValueError: If no URL survives splitting and trimming.
    """
    endpoints = [part.strip() for part in value.split(",") if part.strip()]
    # dict.fromkeys preserves first-seen order while dropping duplicates --
    # a plain set() would make rotation order nondeterministic.
    endpoints = list(dict.fromkeys(endpoints))
    if not endpoints:
        raise ValueError("No endpoint found in config value")
    return endpoints


class EndpointRotator:
    """Tracks one active endpoint out of a list, rotating past failing ones.

    Single-endpoint lists behave exactly as before: `current` never changes
    and failures are simply counted. The rotation only activates with 2+
    endpoints, so existing single-URL configs and tests are unaffected.
    """

    def __init__(
        self,
        endpoints: str | list[str],
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        """Build the rotator.

        Args:
            endpoints: A single URL, a comma-separated string of URLs, or an
                already-split list of URLs.
            failure_threshold: Consecutive failures before rotating.
            cooldown_seconds: How long a failed endpoint is skipped.
        """
        self._endpoints = (
            parse_endpoints(endpoints) if isinstance(endpoints, str) else list(endpoints)
        )
        if not self._endpoints:
            raise ValueError("EndpointRotator requires at least one endpoint")
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._index = 0
        self._consecutive_failures = 0
        # endpoint -> monotonic timestamp it becomes eligible again
        self._cooldown_until: dict[str, float] = {}

    @property
    def current(self) -> str:
        """The currently active endpoint URL."""
        return self._endpoints[self._index]

    @property
    def endpoints(self) -> list[str]:
        """All configured endpoints, in rotation order."""
        return list(self._endpoints)

    def mark_success(self) -> None:
        """Reset the failure streak on the active endpoint."""
        self._consecutive_failures = 0

    def mark_failure(self) -> bool:
        """Record a failure on the active endpoint, rotating if it crossed
        the threshold.

        Returns:
            True if this call rotated to a different endpoint.
        """
        self._consecutive_failures += 1
        if self._consecutive_failures < self._failure_threshold:
            return False
        if len(self._endpoints) < 2:
            # Nothing to rotate to -- keep counting so a caller that only
            # logs on rotation doesn't go silent, but don't reset the streak:
            # every subsequent failure would otherwise look like a fresh one.
            return False

        failed = self.current
        self._cooldown_until[failed] = time.monotonic() + self._cooldown_seconds
        previous = self._index
        self._advance_to_next_available()
        self._consecutive_failures = 0
        if self._index != previous:
            logger.warning(
                f"Endpoint rotation: {failed!r} failed "
                f"{self._failure_threshold}x in a row, switching to "
                f"{self.current!r}"
            )
            return True
        return False

    def _advance_to_next_available(self) -> None:
        """Move `_index` to the next endpoint not in cooldown.

        Falls back to the plain next endpoint (ignoring cooldown) if every
        endpoint is currently cooling down -- a bot with no endpoint at all is
        worse than one retrying a recently-bad one.
        """
        now = time.monotonic()
        n = len(self._endpoints)
        for offset in range(1, n + 1):
            candidate = (self._index + offset) % n
            url = self._endpoints[candidate]
            until = self._cooldown_until.get(url)
            if until is None or until <= now:
                self._index = candidate
                return
        # Every endpoint is cooling down: just move to the next one anyway.
        self._index = (self._index + 1) % n
