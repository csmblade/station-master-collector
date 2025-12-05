"""Database configuration loader - fetches device config from API."""

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from collector.core.config import CollectorConfig, DeviceConfig, PluginConfig

logger = structlog.get_logger(__name__)


class DatabaseConfigLoader:
    """Loads device configuration from the Station Master API."""

    def __init__(self, config: "CollectorConfig") -> None:
        """Initialize the database config loader.

        Args:
            config: Collector configuration with server URL and API key
        """
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._running = False
        self._refresh_task: asyncio.Task | None = None
        self._on_config_updated: list[Any] = []

    async def start(self) -> None:
        """Start the config loader."""
        self._client = httpx.AsyncClient(
            base_url=self._config.server_url,
            headers={
                "Authorization": f"Bearer {self._config.api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._running = True
        logger.info("db_config_loader_started")

    async def stop(self) -> None:
        """Stop the config loader."""
        self._running = False

        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.aclose()

        logger.info("db_config_loader_stopped")

    async def fetch_config(self) -> dict[str, Any]:
        """Fetch device configuration from the API.

        Returns:
            Dictionary with station_id, station_name, and devices list
        """
        if not self._client:
            raise RuntimeError("Config loader not started")

        try:
            response = await self._client.get("/api/v1/collector/config")
            response.raise_for_status()
            data = response.json()

            logger.info(
                "config_fetched",
                station_id=data.get("station_id"),
                device_count=len(data.get("devices", [])),
            )

            return data

        except httpx.HTTPStatusError as e:
            logger.error(
                "config_fetch_http_error",
                status_code=e.response.status_code,
                detail=e.response.text,
            )
            raise
        except Exception as e:
            logger.error("config_fetch_error", error=str(e))
            raise

    def build_plugin_configs(
        self, api_response: dict[str, Any]
    ) -> list["PluginConfig"]:
        """Convert API response to PluginConfig objects.

        Groups devices by plugin and creates PluginConfig for each.

        Args:
            api_response: Response from /api/v1/collector/config

        Returns:
            List of PluginConfig objects for the scheduler
        """
        from collector.core.config import DeviceConfig, PluginConfig, SNMPConfig

        devices = api_response.get("devices", [])

        # Group devices by plugin
        plugins_map: dict[str, list[dict]] = {}
        for device in devices:
            plugin_name = device.get("plugin", "")
            if not plugin_name:
                continue
            if plugin_name not in plugins_map:
                plugins_map[plugin_name] = []
            plugins_map[plugin_name].append(device)

        # Build PluginConfig for each plugin
        plugin_configs = []
        for plugin_name, plugin_devices in plugins_map.items():
            device_configs = []

            for dev in plugin_devices:
                config = dev.get("config", {})

                # Build SNMP config if present
                snmp_config = None
                if "snmp" in config:
                    snmp_data = config["snmp"]
                    snmp_config = SNMPConfig(
                        host=snmp_data.get("host", ""),
                        port=snmp_data.get("port", 161),
                        version=snmp_data.get("version", "v2c"),
                        community=snmp_data.get("community"),
                        timeout=snmp_data.get("timeout", 5),
                        retries=snmp_data.get("retries", 3),
                    )

                device_config = DeviceConfig(
                    id=dev.get("id", ""),
                    name=dev.get("name", ""),
                    enabled=dev.get("enabled", True),
                    poll_interval=dev.get("poll_interval", 30),
                    snmp=snmp_config,
                )
                device_configs.append(device_config)

            plugin_config = PluginConfig(
                plugin_name=plugin_name,
                enabled=True,
                devices=device_configs,
            )
            plugin_configs.append(plugin_config)

            logger.info(
                "plugin_config_built",
                plugin=plugin_name,
                device_count=len(device_configs),
            )

        return plugin_configs
