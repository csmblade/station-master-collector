"""Remote log handler for sending collector logs to the central server."""

import asyncio
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from collector.core.config import CollectorConfig

logger = structlog.get_logger(__name__)


class RemoteLogHandler:
    """Handler that buffers and sends logs to the central server.

    Batches logs and sends them periodically to reduce API calls.
    Includes local buffering for offline resilience.
    """

    MAX_BUFFER_SIZE = 1000
    BATCH_SIZE = 100
    FLUSH_INTERVAL = 10  # seconds

    def __init__(self, config: "CollectorConfig") -> None:
        """Initialize the remote log handler.

        Args:
            config: Collector configuration
        """
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._running = False
        self._flush_task: asyncio.Task | None = None
        self._buffer: deque[dict[str, Any]] = deque(maxlen=self.MAX_BUFFER_SIZE)
        self._buffer_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the remote log handler."""
        self._client = httpx.AsyncClient(
            base_url=self._config.server_url,
            headers={
                "Authorization": f"Bearer {self._config.api_key.get_secret_value()}",
                "X-Station-ID": self._config.station_id,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.debug("remote_log_handler_started")

    async def stop(self) -> None:
        """Stop the remote log handler and flush remaining logs."""
        self._running = False

        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # Final flush
        await self._flush()

        if self._client:
            await self._client.aclose()

        logger.debug("remote_log_handler_stopped")

    async def add_log(
        self,
        level: str,
        logger_name: str,
        message: str,
        context: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        """Add a log entry to the buffer.

        Args:
            level: Log level (DEBUG, INFO, WARNING, ERROR)
            logger_name: Name of the logger
            message: Log message
            context: Additional context data
            timestamp: Log timestamp (defaults to now)
        """
        entry = {
            "time": (timestamp or datetime.utcnow()).isoformat(),
            "level": level.upper(),
            "logger": logger_name,
            "message": message,
            "context": context or {},
        }

        async with self._buffer_lock:
            self._buffer.append(entry)

            # Flush if buffer is full
            if len(self._buffer) >= self.BATCH_SIZE:
                await self._flush()

    async def _flush_loop(self) -> None:
        """Background task for periodic flushing."""
        while self._running:
            try:
                await asyncio.sleep(self.FLUSH_INTERVAL)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Don't log to remote here to avoid infinite loop
                print(f"Remote log flush error: {e}")

    async def _flush(self) -> None:
        """Flush buffered logs to the server."""
        if not self._buffer or not self._client:
            return

        async with self._buffer_lock:
            # Take up to BATCH_SIZE logs
            logs = []
            while self._buffer and len(logs) < self.BATCH_SIZE:
                logs.append(self._buffer.popleft())

        if not logs:
            return

        payload = {"logs": logs}

        try:
            response = await self._client.post(
                "/api/v1/collector/logs",
                json=payload,
            )
            response.raise_for_status()

            data = response.json()
            stored = data.get("stored", 0)

            if stored != len(logs):
                print(f"Warning: Sent {len(logs)} logs, server stored {stored}")

        except Exception as e:
            # On failure, put logs back at the front of the buffer
            async with self._buffer_lock:
                for log in reversed(logs):
                    self._buffer.appendleft(log)
            print(f"Failed to send logs: {e}")


class StructlogProcessor:
    """Structlog processor that sends logs to a RemoteLogHandler.

    Add this processor to your structlog configuration to automatically
    send logs to the remote server.
    """

    def __init__(self, handler: RemoteLogHandler) -> None:
        """Initialize the processor.

        Args:
            handler: Remote log handler to use
        """
        self._handler = handler
        self._loop: asyncio.AbstractEventLoop | None = None

    def __call__(
        self,
        logger: Any,
        method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        """Process a log event.

        Args:
            logger: The wrapped logger object
            method_name: The method name (debug, info, warning, error)
            event_dict: The event dictionary

        Returns:
            The event dictionary (unchanged)
        """
        # Get or create event loop
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        if self._loop and self._handler._running:
            # Extract log data
            level = method_name.upper()
            logger_name = event_dict.get("logger", "collector")
            message = event_dict.get("event", "")
            timestamp = event_dict.get("timestamp")

            # Build context from remaining event_dict items
            context = {
                k: v
                for k, v in event_dict.items()
                if k not in ("event", "logger", "timestamp", "level")
            }

            # Schedule log submission (non-blocking)
            self._loop.create_task(
                self._handler.add_log(
                    level=level,
                    logger_name=logger_name,
                    message=message,
                    context=context,
                    timestamp=datetime.fromisoformat(timestamp) if timestamp else None,
                )
            )

        return event_dict
