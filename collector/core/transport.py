"""Secure transport layer for sending metrics to the server."""

import asyncio
import gzip
import json
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    from collector.core.config import CollectorConfig
    from collector.plugins.base import MetricPoint

logger = structlog.get_logger(__name__)


class SecureTransport:
    """Handles encrypted transmission of metrics to the central server."""

    def __init__(self, config: "CollectorConfig") -> None:
        """Initialize the transport layer.

        Args:
            config: Collector configuration
        """
        self._config = config
        self._buffer: list[dict[str, Any]] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._running = False
        self._db_path = config.buffer_db_path
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Initialize transport and start background flushing."""
        # Initialize HTTP client
        self._client = httpx.AsyncClient(
            base_url=self._config.server_url,
            headers={
                "Authorization": f"Bearer {self._config.api_key.get_secret_value()}",
                "X-Station-ID": self._config.station_id,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

        # Initialize local buffer database
        await self._init_buffer_db()

        # Start background flush task
        self._running = True
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.info("transport_started", server=self._config.server_url)

    async def stop(self) -> None:
        """Stop transport and flush remaining metrics."""
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

        logger.info("transport_stopped")

    async def _init_buffer_db(self) -> None:
        """Initialize SQLite buffer for offline resilience."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS metric_buffer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    attempts INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_created_at
                ON metric_buffer(created_at)
            """)
            await db.commit()

    async def enqueue(self, metrics: list["MetricPoint"]) -> None:
        """Add metrics to the send buffer.

        Args:
            metrics: List of metric points to send
        """
        if not metrics:
            return

        # Group metrics by device_id (from tags)
        device_batches: dict[str, dict[str, Any]] = {}
        for m in metrics:
            device_id = m.tags.get("device_id", "unknown")
            plugin = m.tags.get("plugin", "")
            if device_id not in device_batches:
                device_batches[device_id] = {
                    "plugin": plugin,
                    "firmware": m.tags.get("firmware"),
                    "metrics": [],
                }
            # Update firmware if present in this metric's tags
            if m.tags.get("firmware") and not device_batches[device_id].get("firmware"):
                device_batches[device_id]["firmware"] = m.tags.get("firmware")
            device_batches[device_id]["metrics"].append({
                "name": m.name,
                "value": m.value,
                "timestamp": m.timestamp,
                "tags": m.tags,
            })

        # Create a batch for each device
        async with self._buffer_lock:
            for device_id, data in device_batches.items():
                batch = {
                    "station_id": self._config.station_id,
                    "device_id": device_id,
                    "plugin": data["plugin"],
                    "firmware": data.get("firmware"),
                    "metrics": data["metrics"],
                    "collected_at": datetime.utcnow().isoformat(),
                }
                self._buffer.append(batch)

            # Flush if buffer is full
            if len(self._buffer) >= self._config.batch_size:
                await self._flush_buffer()

    async def flush(self) -> None:
        """Immediately flush all buffered metrics."""
        async with self._buffer_lock:
            await self._flush_buffer()

        # Also try to send any persisted metrics
        await self._flush_persisted()

    async def _periodic_flush(self) -> None:
        """Background task for periodic flushing."""
        while self._running:
            try:
                await asyncio.sleep(self._config.flush_interval)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("periodic_flush_error", error=str(e))

    async def _flush_buffer(self) -> None:
        """Flush in-memory buffer to server or persist to disk."""
        if not self._buffer:
            return

        batches = self._buffer.copy()
        self._buffer.clear()

        payload = self._prepare_payload(batches)

        try:
            await self._send_with_retry(payload)
            logger.debug("buffer_flushed", batch_count=len(batches))
        except Exception as e:
            logger.warning("flush_failed_persisting", error=str(e))
            await self._persist_to_disk(payload)

    def _prepare_payload(self, batches: list[dict]) -> bytes:
        """Prepare payload with optional compression.

        Args:
            batches: List of metric batches

        Returns:
            Serialized and optionally compressed payload
        """
        payload = {
            "batches": batches,
            "sent_at": datetime.utcnow().isoformat(),
            "compression": self._config.compression,
        }

        data = json.dumps(payload).encode()

        if self._config.compression == "gzip":
            return gzip.compress(data)
        elif self._config.compression == "lz4":
            try:
                import lz4.frame

                return lz4.frame.compress(data)
            except ImportError:
                logger.warning("lz4_not_available_using_gzip")
                return gzip.compress(data)
        else:
            return data

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

        headers = {}
        if self._config.compression == "gzip":
            headers["Content-Encoding"] = "gzip"
        elif self._config.compression == "lz4":
            headers["Content-Encoding"] = "lz4"

        response = await self._client.post(
            "/api/v1/ingest",
            content=payload,
            headers=headers,
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
                    "INSERT INTO metric_buffer (payload, created_at) VALUES (?, ?)",
                    (payload, time.time()),
                )
                await db.commit()
            logger.debug("payload_persisted_to_disk")
        except Exception as e:
            logger.error("persist_to_disk_failed", error=str(e))

    async def _flush_persisted(self) -> None:
        """Try to send persisted payloads."""
        try:
            async with aiosqlite.connect(self._db_path) as db:
                # Get oldest persisted payloads
                cursor = await db.execute(
                    """
                    SELECT id, payload, attempts
                    FROM metric_buffer
                    WHERE attempts < 10
                    ORDER BY created_at ASC
                    LIMIT 10
                    """
                )
                rows = await cursor.fetchall()

                for row_id, payload, attempts in rows:
                    try:
                        await self._send_with_retry(payload)
                        # Delete on success
                        await db.execute(
                            "DELETE FROM metric_buffer WHERE id = ?",
                            (row_id,),
                        )
                        logger.debug("persisted_payload_sent", id=row_id)
                    except Exception:
                        # Increment attempt counter
                        await db.execute(
                            "UPDATE metric_buffer SET attempts = ? WHERE id = ?",
                            (attempts + 1, row_id),
                        )

                await db.commit()

                # Clean up old failed entries (older than 24 hours)
                cutoff = time.time() - 86400
                await db.execute(
                    "DELETE FROM metric_buffer WHERE created_at < ?",
                    (cutoff,),
                )
                await db.commit()

        except Exception as e:
            logger.error("flush_persisted_error", error=str(e))

    async def health_check(self) -> dict[str, Any]:
        """Check transport health and connectivity.

        Returns:
            Health status information
        """
        result = {
            "connected": False,
            "buffer_size": len(self._buffer),
            "persisted_count": 0,
        }

        # Check persisted count
        try:
            async with aiosqlite.connect(self._db_path) as db:
                cursor = await db.execute("SELECT COUNT(*) FROM metric_buffer")
                row = await cursor.fetchone()
                result["persisted_count"] = row[0] if row else 0
        except Exception:
            pass

        # Check server connectivity
        if self._client:
            try:
                response = await self._client.get("/health", timeout=5.0)
                result["connected"] = response.status_code == 200
            except Exception:
                pass

        return result
