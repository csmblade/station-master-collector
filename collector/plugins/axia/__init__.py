"""Axia Audio Plugin for Station Master.

Supports Axia Livewire audio routing systems.
Collects audio levels, stream status, GPIO states, and routing information.
"""

import time
from typing import Any

from collector.plugins.base import BasePlugin, DeviceInfo, MetricPoint


class AxiaPlugin(BasePlugin):
    """Plugin for Axia Livewire audio systems."""

    plugin_name = "axia"
    plugin_version = "0.1.0"
    plugin_description = "Axia Livewire audio routing monitoring"
    plugin_author = "Station Master Team"
    supported_protocols = ["http", "livewire"]

    async def discover_devices(self, config: dict[str, Any]) -> list[DeviceInfo]:
        """Discover Axia devices on the network.

        Args:
            config: Discovery configuration

        Returns:
            List of discovered Axia devices
        """
        # TODO: Implement Livewire advertisement discovery
        return []

    async def collect(
        self, device: dict[str, Any], config: dict[str, Any]
    ) -> list[MetricPoint]:
        """Collect metrics from an Axia device.

        Args:
            device: Device configuration
            config: Collection configuration

        Returns:
            List of collected metrics
        """
        metrics: list[MetricPoint] = []
        timestamp = time.time()
        device_id = device.get("id", "unknown")
        device_name = device.get("name", "unknown")

        base_tags = {
            "device_id": device_id,
            "device_name": device_name,
            "manufacturer": "axia",
            "plugin": self.plugin_name,
        }

        http_config = device.get("http")
        if http_config:
            # TODO: Implement actual HTTP API collection
            # Axia devices expose HTTP API for status

            # Placeholder metrics
            metric_defs = [
                ("audio.input_level_left", "dBFS"),
                ("audio.input_level_right", "dBFS"),
                ("audio.output_level_left", "dBFS"),
                ("audio.output_level_right", "dBFS"),
                ("stream.status", "state"),
                ("stream.packet_loss", "percent"),
                ("gpio.state", "bitmap"),
            ]

            for name, unit in metric_defs:
                metrics.append(
                    MetricPoint(
                        name=name,
                        value=0.0,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit=unit,
                    )
                )

        return metrics

    def get_config_schema(self) -> dict[str, Any]:
        """Return JSON schema for Axia plugin configuration."""
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "http": {
                    "type": "object",
                    "properties": {
                        "base_url": {"type": "string", "format": "uri"},
                        "username": {"type": "string"},
                        "password": {"type": "string"},
                    },
                    "required": ["base_url"],
                },
                "channels": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Specific channels to monitor",
                },
            },
        }

    def get_available_metrics(self) -> list[dict[str, Any]]:
        """Return list of metrics this plugin can collect."""
        return [
            {
                "name": "audio.input_level_left",
                "description": "Left channel input audio level",
                "unit": "dBFS",
                "type": "gauge",
            },
            {
                "name": "audio.input_level_right",
                "description": "Right channel input audio level",
                "unit": "dBFS",
                "type": "gauge",
            },
            {
                "name": "audio.output_level_left",
                "description": "Left channel output audio level",
                "unit": "dBFS",
                "type": "gauge",
            },
            {
                "name": "audio.output_level_right",
                "description": "Right channel output audio level",
                "unit": "dBFS",
                "type": "gauge",
            },
            {
                "name": "stream.status",
                "description": "Livewire stream status",
                "unit": "state",
                "type": "gauge",
            },
            {
                "name": "stream.packet_loss",
                "description": "Stream packet loss percentage",
                "unit": "percent",
                "type": "gauge",
            },
            {
                "name": "gpio.state",
                "description": "GPIO pin states as bitmap",
                "unit": "bitmap",
                "type": "gauge",
            },
        ]
