"""Transport layer for sending syslog events to the central API."""

import asyncio
import gzip
import json
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import aiosqlite
import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from collector.core.syslog_parser import SyslogMessage

if TYPE_CHECKING:
    from collector.core.config import CollectorConfig

logger = structlog.get_logger(__name__)


class SyslogTransport:
    """Handles transmission of syslog events to the central server."""

    def __init__(
        self,
        config: "CollectorConfig",
        batch_size: int = 100,
        flush_interval: int = 5,
    ) -> None:
        """Initialize the syslog transport.

        Args:
            config: Collector configuration
            batch_size: Number of events to batch before sending
            flush_interval: Seconds between automatic flushes
        """
        self._config = config
        self._batch_size = batch_size
        self._flush_interval = flush_interval

        self._buffer: list[dict[str, Any]] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        self._db_path = Path("./syslog_buffer.db")
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        """Initialize transport and start background flushing."""
        self._client = httpx.AsyncClient(
            base_url=self._config.server_url,
            headers={
                "Authorization": f"Bearer {self._config.api_key.get_secret_value()}",
                "X-Station-ID": self._config.station_id,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

        # Initialize buffer database
        await self._init_buffer_db()

        # Start background flush task
        self._running = True
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.info("syslog_transport_started")

    async def stop(self) -> None:
        """Stop transport and flush remaining events."""
        self._running = False

        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # Final flush
        await self.flush()

        if self._client:
            await self._client.aclose()

        logger.info("syslog_transport_stopped")

    async def _init_buffer_db(self) -> None:
        """Initialize SQLite buffer for offline resilience."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS syslog_buffer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    attempts INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_syslog_created_at
                ON syslog_buffer(created_at)
            """)
            await db.commit()

    async def enqueue(
        self,
        message: SyslogMessage,
        source_ip: str,
        source_id: str,
        protocol: str,
    ) -> None:
        """Add a syslog event to the send buffer.

        Args:
            message: Parsed syslog message
            source_ip: IP address of the source
            source_id: ID of the configured source
            protocol: Protocol used (udp/tcp)
        """
        event = {
            "time": message.timestamp.isoformat(),
            "source_id": source_id,
            "facility": message.facility,
            "severity": message.severity,
            "hostname": message.hostname,
            "app_name": message.app_name,
            "proc_id": message.proc_id,
            "message": message.message,
            "raw_message": message.raw_message,
            "source_ip": source_ip,
            "protocol": protocol,
        }

        async with self._buffer_lock:
            self._buffer.append(event)

            # Flush if buffer is full
            if len(self._buffer) >= self._batch_size:
                await self._flush_buffer()

    async def flush(self) -> None:
        """Immediately flush all buffered events."""
        async with self._buffer_lock:
            await self._flush_buffer()

        # Also try to send any persisted events
        await self._flush_persisted()

    async def _periodic_flush(self) -> None:
        """Background task for periodic flushing."""
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("syslog_periodic_flush_error", error=str(e))

    async def _flush_buffer(self) -> None:
        """Flush in-memory buffer to server or persist to disk."""
        if not self._buffer:
            return

        events = self._buffer.copy()
        self._buffer.clear()

        payload = self._prepare_payload(events)

        try:
            await self._send_with_retry(payload)
            logger.debug("syslog_buffer_flushed", event_count=len(events))
        except Exception as e:
            logger.warning("syslog_flush_failed_persisting", error=str(e))
            await self._persist_to_disk(payload)

    def _prepare_payload(self, events: list[dict]) -> bytes:
        """Prepare payload with gzip compression.

        Args:
            events: List of syslog events

        Returns:
            Compressed payload bytes
        """
        payload = {
            "station_id": self._config.station_id,
            "events": events,
            "sent_at": datetime.utcnow().isoformat(),
        }

        data = json.dumps(payload).encode()
        return gzip.compress(data)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )
    async def _send_with_retry(self, payload: bytes) -> None:
        """Send payload to server with retry logic.

        Args:
            payload: Compressed payload bytes
        """
        if not self._client:
            raise RuntimeError("Transport not started")

        response = await self._client.post(
            "/api/v1/syslog/ingest",
            content=payload,
            headers={"Content-Encoding": "gzip"},
        )
        response.raise_for_status()

    async def _persist_to_disk(self, payload: bytes) -> None:
        """Persist failed payload to SQLite for later retry.

        Args:
            payload: Payload bytes to persist
        """
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    "INSERT INTO syslog_buffer (payload, created_at) VALUES (?, ?)",
                    (payload, time.time()),
                )
                await db.commit()
            logger.debug("syslog_payload_persisted")
        except Exception as e:
            logger.error("syslog_persist_failed", error=str(e))

    async def _flush_persisted(self) -> None:
        """Try to send persisted payloads."""
        try:
            async with aiosqlite.connect(self._db_path) as db:
                cursor = await db.execute(
                    """
                    SELECT id, payload, attempts
                    FROM syslog_buffer
                    WHERE attempts < 10
                    ORDER BY created_at ASC
                    LIMIT 10
                    """
                )
                rows = await cursor.fetchall()

                for row_id, payload, attempts in rows:
                    try:
                        await self._send_with_retry(payload)
                        await db.execute(
                            "DELETE FROM syslog_buffer WHERE id = ?",
                            (row_id,),
                        )
                        logger.debug("syslog_persisted_sent", id=row_id)
                    except Exception:
                        await db.execute(
                            "UPDATE syslog_buffer SET attempts = ? WHERE id = ?",
                            (attempts + 1, row_id),
                        )

                await db.commit()

                # Clean up old entries (older than 24 hours)
                cutoff = time.time() - 86400
                await db.execute(
                    "DELETE FROM syslog_buffer WHERE created_at < ?",
                    (cutoff,),
                )
                await db.commit()

        except Exception as e:
            logger.error("syslog_flush_persisted_error", error=str(e))

    async def fetch_allowed_sources(self) -> list[dict]:
        """Fetch list of allowed syslog sources from the API.

        Returns:
            List of source configurations
        """
        if not self._client:
            raise RuntimeError("Transport not started")

        try:
            response = await self._client.get("/api/v1/syslog/collector/sources")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("syslog_fetch_sources_failed", error=str(e))
            return []
