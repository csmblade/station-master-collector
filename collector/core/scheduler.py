"""Collection scheduling for metrics gathering."""

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import SecretStr

if TYPE_CHECKING:
    from collector.core.config import DeviceConfig, PluginConfig
    from collector.plugins.base import BasePlugin, MetricPoint

logger = structlog.get_logger(__name__)


class CollectionScheduler:
    """Manages scheduled metric collection from devices."""

    def __init__(
        self,
        on_metrics_collected: Callable[[list["MetricPoint"]], None] | None = None,
        max_concurrent_devices: int = 10,
    ) -> None:
        """Initialize the scheduler.

        Args:
            on_metrics_collected: Callback when metrics are collected
            max_concurrent_devices: Maximum number of devices polling simultaneously
        """
        self._scheduler = AsyncIOScheduler()
        self._on_metrics_collected = on_metrics_collected
        self._collection_tasks: dict[str, str] = {}  # device_id -> job_id
        self._running = False
        self._max_concurrent = max_concurrent_devices
        self._collection_semaphore: asyncio.Semaphore | None = None

    def start(self) -> None:
        """Start the scheduler."""
        if not self._running:
            self._collection_semaphore = asyncio.Semaphore(self._max_concurrent)
            self._scheduler.start()
            self._running = True
            logger.info(
                "scheduler_started",
                max_concurrent_devices=self._max_concurrent,
            )

    def stop(self) -> None:
        """Stop the scheduler."""
        if self._running:
            self._scheduler.shutdown(wait=True)
            self._running = False
            logger.info("scheduler_stopped")

    def schedule_plugin(
        self,
        plugin: "BasePlugin",
        plugin_config: "PluginConfig",
    ) -> None:
        """Schedule collection for all devices in a plugin.

        Args:
            plugin: Plugin instance
            plugin_config: Plugin configuration with devices
        """
        for device_config in plugin_config.devices:
            if device_config.enabled:
                self.schedule_device(plugin, device_config)

    def schedule_device(
        self,
        plugin: "BasePlugin",
        device_config: "DeviceConfig",
    ) -> str:
        """Schedule metric collection for a single device.

        Args:
            plugin: Plugin instance to use for collection
            device_config: Device configuration

        Returns:
            Job ID for the scheduled task
        """
        device_id = device_config.id

        # Remove existing job if any
        if device_id in self._collection_tasks:
            self.unschedule_device(device_id)

        job = self._scheduler.add_job(
            self._collect_metrics,
            trigger=IntervalTrigger(seconds=device_config.poll_interval),
            args=[plugin, device_config],
            id=f"collect_{device_id}",
            name=f"Collect from {device_config.name}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=device_config.poll_interval // 2,
        )

        self._collection_tasks[device_id] = job.id
        logger.info(
            "device_scheduled",
            device_id=device_id,
            device_name=device_config.name,
            interval=device_config.poll_interval,
        )

        return job.id

    def unschedule_device(self, device_id: str) -> None:
        """Remove scheduled collection for a device.

        Args:
            device_id: Device identifier
        """
        if device_id in self._collection_tasks:
            job_id = self._collection_tasks.pop(device_id)
            try:
                self._scheduler.remove_job(job_id)
                logger.info("device_unscheduled", device_id=device_id)
            except Exception:
                pass  # Job may have already been removed

    def _serialize_config(self, config: "DeviceConfig") -> dict[str, Any]:
        """Serialize device config, exposing SecretStr values.

        Pydantic's model_dump() masks SecretStr values, but we need the
        actual values for SNMP community strings, passwords, etc.

        Args:
            config: Device configuration

        Returns:
            Dictionary with exposed secret values
        """

        def expose_secrets(obj: Any) -> Any:
            if isinstance(obj, SecretStr):
                return obj.get_secret_value()
            elif isinstance(obj, dict):
                return {k: expose_secrets(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [expose_secrets(item) for item in obj]
            elif hasattr(obj, "model_dump"):
                return expose_secrets(obj.model_dump())
            return obj

        return expose_secrets(config)

    async def _collect_metrics(
        self,
        plugin: "BasePlugin",
        device_config: "DeviceConfig",
    ) -> None:
        """Execute metric collection for a device.

        Uses a semaphore to limit concurrent collections.

        Args:
            plugin: Plugin instance
            device_config: Device configuration
        """
        if self._collection_semaphore is None:
            logger.warning("scheduler_not_started_properly")
            return

        async with self._collection_semaphore:
            await self._do_collect(plugin, device_config)

    async def _do_collect(
        self,
        plugin: "BasePlugin",
        device_config: "DeviceConfig",
    ) -> None:
        """Actually perform the metric collection.

        Args:
            plugin: Plugin instance
            device_config: Device configuration
        """
        device_id = device_config.id
        start_time = datetime.utcnow()

        try:
            logger.debug(
                "collection_started",
                device_id=device_id,
                plugin=plugin.plugin_name,
            )

            # Collect metrics from the device
            # Serialize config, exposing SecretStr values for SNMP/HTTP auth
            device_dict = self._serialize_config(device_config)
            metrics = await plugin.collect(
                device=device_dict,
                config=device_dict,
            )

            elapsed = (datetime.utcnow() - start_time).total_seconds()

            logger.info(
                "collection_completed",
                device_id=device_id,
                plugin=plugin.plugin_name,
                metric_count=len(metrics),
                elapsed_seconds=elapsed,
            )

            # Send metrics to callback
            if self._on_metrics_collected and metrics:
                self._on_metrics_collected(metrics)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            logger.error(
                "collection_failed",
                device_id=device_id,
                plugin=plugin.plugin_name,
                error=str(e),
                elapsed_seconds=elapsed,
            )

    async def collect_now(
        self,
        plugin: "BasePlugin",
        device_config: "DeviceConfig",
    ) -> list["MetricPoint"]:
        """Immediately collect metrics from a device (bypass scheduler).

        Args:
            plugin: Plugin instance
            device_config: Device configuration

        Returns:
            List of collected metrics
        """
        return await plugin.collect(
            device=device_config.model_dump(),
            config=device_config.model_dump(),
        )

    def get_scheduled_devices(self) -> list[str]:
        """Get list of device IDs with scheduled collection."""
        return list(self._collection_tasks.keys())

    def get_job_info(self, device_id: str) -> dict | None:
        """Get information about a scheduled job.

        Args:
            device_id: Device identifier

        Returns:
            Job information or None if not found
        """
        if device_id not in self._collection_tasks:
            return None

        job_id = self._collection_tasks[device_id]
        job = self._scheduler.get_job(job_id)

        if job is None:
            return None

        return {
            "job_id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "pending": job.pending,
        }
