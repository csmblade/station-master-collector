"""Omnia MPX Node Plugin for Station Master.

Supports Omnia MPX Encoder nodes (Telos Alliance) via SNMP.
Collects system status, audio levels, encoder state, and network status metrics.

Uses MIB definitions from mibs/omnia-mpx-mode.mib.
"""

import asyncio
import re
import time
from typing import Any

import structlog
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    get_cmd,
)

from collector.plugins.base import BasePlugin, DeviceInfo, MetricPoint

logger = structlog.get_logger(__name__)


# SNMP OIDs from Omnia MPX Encoder MIB (mibs/omnia-mpx-mode.mib)
# Enterprise: 1.3.6.1.4.1.42463 (Telos Alliance)
# MPX Encoder: .17
#
# Standard MIB-2 OIDs for basic device info
STANDARD_OIDS = {
    "sys_descr": "1.3.6.1.2.1.1.1.0",
    "sys_uptime": "1.3.6.1.2.1.1.3.0",
    "sys_name": "1.3.6.1.2.1.1.5.0",
}

# Omnia MPX Encoder enterprise OIDs (1.3.6.1.4.1.42463.17.x.x)
OMNIA_MPX_OIDS = {
    # System info (1.3.6.1.4.1.42463.17.1.x)
    "uptime": "1.3.6.1.4.1.42463.17.1.1.0",
    "active_time": "1.3.6.1.4.1.42463.17.1.2.0",
    "temperature": "1.3.6.1.4.1.42463.17.1.3.0",
    "hostname": "1.3.6.1.4.1.42463.17.1.4.0",
    "firmware_version": "1.3.6.1.4.1.42463.17.1.5.0",
    "cpu_usage": "1.3.6.1.4.1.42463.17.1.6.0",
    "umpx_app_version": "1.3.6.1.4.1.42463.17.1.7.0",
    "audio_level": "1.3.6.1.4.1.42463.17.1.8.0",
    "is_audio_clipping": "1.3.6.1.4.1.42463.17.1.9.0",
    "encoder_status": "1.3.6.1.4.1.42463.17.1.10.0",
    "encoder_enabled": "1.3.6.1.4.1.42463.17.1.11.0",
    # GPO status (1.3.6.1.4.1.42463.17.2.x)
    "network_one_up": "1.3.6.1.4.1.42463.17.2.1.0",
    "network_two_up": "1.3.6.1.4.1.42463.17.2.2.0",
    "input_audio_clipping": "1.3.6.1.4.1.42463.17.2.3.0",
}


class OmniaMPXPlugin(BasePlugin):
    """Plugin for Omnia MPX Encoder nodes."""

    plugin_name = "omnia_mpx"
    plugin_version = "0.1.0"
    plugin_description = "Omnia MPX Encoder node monitoring (Telos Alliance)"
    plugin_author = "Station Master Team"
    supported_protocols = ["snmp"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the plugin."""
        super().__init__(config)

    async def discover_devices(self, config: dict[str, Any]) -> list[DeviceInfo]:
        """Discover Omnia MPX nodes on the network.

        Args:
            config: Discovery configuration

        Returns:
            List of discovered MPX nodes
        """
        # TODO: Implement network discovery via SNMP broadcast
        return []

    async def collect(
        self, device: dict[str, Any], config: dict[str, Any]
    ) -> list[MetricPoint]:
        """Collect metrics from an Omnia MPX node.

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

        snmp_config = device.get("snmp", {})

        if not snmp_config:
            logger.warning("no_snmp_config", device_id=device_id)
            return metrics

        host = snmp_config.get("host")
        if not host:
            logger.warning("no_snmp_host", device_id=device_id)
            return metrics

        port = snmp_config.get("port", 161)
        community = snmp_config.get("community", "public")
        timeout = snmp_config.get("timeout", 5)
        retries = snmp_config.get("retries", 3)

        logger.debug(
            "snmp_settings",
            host=host,
            port=port,
            community_len=len(str(community)) if community else 0,
            timeout=timeout,
            retries=retries,
        )

        base_tags = {
            "device_id": device_id,
            "device_name": device_name,
            "manufacturer": "telos_alliance",
            "device_type": "omnia_mpx_encoder",
            "plugin": self.plugin_name,
        }

        # Collect all SNMP values
        snmp_values = await self._collect_snmp_values(
            host, port, community, timeout, retries
        )

        if not snmp_values:
            logger.warning("no_snmp_values", device_id=device_id, host=host)
            return metrics

        # Add hostname and firmware as tags if available
        if snmp_values.get("hostname"):
            base_tags["hostname"] = str(snmp_values["hostname"])
        if snmp_values.get("firmware_version"):
            base_tags["firmware"] = str(snmp_values["firmware_version"])
        if snmp_values.get("umpx_app_version"):
            base_tags["umpx_version"] = str(snmp_values["umpx_app_version"])

        # Parse and create metrics
        metrics.extend(self._parse_metrics(snmp_values, base_tags, timestamp))

        logger.info(
            "collected_metrics",
            device_id=device_id,
            host=host,
            metric_count=len(metrics),
        )

        return metrics

    async def _collect_snmp_values(
        self,
        host: str,
        port: int,
        community: str,
        timeout: float,
        retries: int,
    ) -> dict[str, Any]:
        """Collect all SNMP values from the device using parallel fetching.

        Args:
            host: Device IP address
            port: SNMP port
            community: SNMP community string
            timeout: Request timeout in seconds
            retries: Number of retries

        Returns:
            Dictionary of OID names to values
        """
        # Create shared engine and community data for all requests
        snmp_engine = SnmpEngine()
        community_data = CommunityData(community)
        context_data = ContextData()

        # Combine all OIDs to collect
        all_oids = {**STANDARD_OIDS, **OMNIA_MPX_OIDS}

        # Fetch all OIDs in parallel
        tasks = [
            self._fetch_single_oid(
                snmp_engine, community_data, context_data,
                host, port, timeout, retries, name, oid
            )
            for name, oid in all_oids.items()
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect successful results
        values: dict[str, Any] = {}
        for result in results:
            if isinstance(result, Exception):
                continue
            if result is not None:
                name, value = result
                values[name] = value

        return values

    async def _fetch_single_oid(
        self,
        snmp_engine: SnmpEngine,
        community_data: CommunityData,
        context_data: ContextData,
        host: str,
        port: int,
        timeout: float,
        retries: int,
        name: str,
        oid: str,
    ) -> tuple[str, Any] | None:
        """Fetch a single OID value.

        Args:
            snmp_engine: Shared SNMP engine
            community_data: Community credentials
            context_data: SNMP context
            host: Device IP address
            port: SNMP port
            timeout: Request timeout
            retries: Number of retries
            name: OID name for logging
            oid: OID string to fetch

        Returns:
            Tuple of (name, value) or None on error
        """
        try:
            transport = await UdpTransportTarget.create(
                (host, port), timeout=timeout, retries=retries
            )
            error_indication, error_status, error_index, var_binds = await get_cmd(
                snmp_engine,
                community_data,
                transport,
                context_data,
                ObjectType(ObjectIdentity(oid)),
            )

            if error_indication:
                logger.debug(
                    "snmp_error_indication",
                    oid=name,
                    error=str(error_indication),
                )
                return None

            if error_status:
                logger.debug(
                    "snmp_error_status",
                    oid=name,
                    error=error_status.prettyPrint(),
                    index=error_index,
                )
                return None

            for var_bind in var_binds:
                logger.debug(
                    "snmp_value_received",
                    oid=name,
                    value=str(var_bind[1]),
                    type=type(var_bind[1]).__name__,
                )
                return (name, var_bind[1])

            return None

        except Exception as e:
            logger.warning("snmp_collection_error", oid=name, error=str(e))
            return None

    def _parse_metrics(
        self,
        snmp_values: dict[str, Any],
        base_tags: dict[str, str],
        timestamp: float,
    ) -> list[MetricPoint]:
        """Parse SNMP values into MetricPoint objects.

        Args:
            snmp_values: Raw SNMP values
            base_tags: Base tags for all metrics
            timestamp: Collection timestamp

        Returns:
            List of parsed metrics
        """
        metrics: list[MetricPoint] = []

        # Standard MIB-2 uptime (timeticks, 1/100th of a second)
        if "sys_uptime" in snmp_values:
            uptime_ticks = self._parse_numeric(snmp_values["sys_uptime"])
            if uptime_ticks is not None:
                # Convert timeticks to seconds
                metrics.append(
                    MetricPoint(
                        name="system.uptime_ticks",
                        value=uptime_ticks / 100.0,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="seconds",
                    )
                )

        # MPX-specific uptime (seconds)
        if "uptime" in snmp_values:
            uptime_val = self._parse_numeric(snmp_values["uptime"])
            if uptime_val is not None:
                metrics.append(
                    MetricPoint(
                        name="system.uptime",
                        value=uptime_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="seconds",
                    )
                )

        # Active time (seconds)
        if "active_time" in snmp_values:
            active_val = self._parse_numeric(snmp_values["active_time"])
            if active_val is not None:
                metrics.append(
                    MetricPoint(
                        name="system.active_time",
                        value=active_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="seconds",
                    )
                )

        # Temperature (parse from string like "45.2 C" or "45.2")
        if "temperature" in snmp_values:
            temp_val = self._parse_numeric(snmp_values["temperature"])
            if temp_val is not None:
                metrics.append(
                    MetricPoint(
                        name="system.temperature",
                        value=temp_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="celsius",
                    )
                )

        # CPU usage (parse from string like "25%" or "25")
        if "cpu_usage" in snmp_values:
            cpu_val = self._parse_numeric(snmp_values["cpu_usage"])
            if cpu_val is not None:
                metrics.append(
                    MetricPoint(
                        name="system.cpu_usage",
                        value=cpu_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="percent",
                    )
                )

        # Audio level (parse from string, may be in dB)
        if "audio_level" in snmp_values:
            audio_val = self._parse_numeric(snmp_values["audio_level"])
            if audio_val is not None:
                metrics.append(
                    MetricPoint(
                        name="audio.level",
                        value=audio_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="dB",
                    )
                )

        # Audio clipping (boolean -> 1/0)
        if "is_audio_clipping" in snmp_values:
            clipping_val = self._parse_boolean(snmp_values["is_audio_clipping"])
            metrics.append(
                MetricPoint(
                    name="audio.clipping",
                    value=float(clipping_val),
                    timestamp=timestamp,
                    tags=base_tags.copy(),
                    unit="boolean",
                )
            )

        # Input audio clipping (boolean -> 1/0)
        if "input_audio_clipping" in snmp_values:
            input_clipping_val = self._parse_boolean(snmp_values["input_audio_clipping"])
            metrics.append(
                MetricPoint(
                    name="audio.input_clipping",
                    value=float(input_clipping_val),
                    timestamp=timestamp,
                    tags=base_tags.copy(),
                    unit="boolean",
                )
            )

        # Encoder enabled (boolean -> 1/0)
        if "encoder_enabled" in snmp_values:
            enabled_val = self._parse_boolean(snmp_values["encoder_enabled"])
            metrics.append(
                MetricPoint(
                    name="encoder.enabled",
                    value=float(enabled_val),
                    timestamp=timestamp,
                    tags=base_tags.copy(),
                    unit="boolean",
                )
            )

        # Encoder status (add as tag, also as numeric if parseable)
        if "encoder_status" in snmp_values:
            status_str = str(snmp_values["encoder_status"])
            status_tags = base_tags.copy()
            status_tags["status"] = status_str
            # Status as 1 (healthy) - could be refined based on actual status values
            metrics.append(
                MetricPoint(
                    name="encoder.status",
                    value=1.0 if status_str.lower() in ["ok", "running", "active", "enabled"] else 0.0,
                    timestamp=timestamp,
                    tags=status_tags,
                    unit="state",
                )
            )

        # Network port 1 up (boolean -> 1/0)
        if "network_one_up" in snmp_values:
            net1_val = self._parse_boolean(snmp_values["network_one_up"])
            metrics.append(
                MetricPoint(
                    name="network.port1_up",
                    value=float(net1_val),
                    timestamp=timestamp,
                    tags=base_tags.copy(),
                    unit="boolean",
                )
            )

        # Network port 2 up (boolean -> 1/0)
        if "network_two_up" in snmp_values:
            net2_val = self._parse_boolean(snmp_values["network_two_up"])
            metrics.append(
                MetricPoint(
                    name="network.port2_up",
                    value=float(net2_val),
                    timestamp=timestamp,
                    tags=base_tags.copy(),
                    unit="boolean",
                )
            )

        return metrics

    def _parse_numeric(self, value: Any) -> float | None:
        """Parse a numeric value from SNMP response.

        Handles DisplayString values like "45.2 C", "25%", "1234".

        Args:
            value: SNMP value to parse

        Returns:
            Parsed float or None if not parseable
        """
        try:
            # If already numeric
            if isinstance(value, (int, float)):
                return float(value)

            # Convert to string and extract numeric part
            str_val = str(value).strip()

            # Try to find a number in the string
            match = re.search(r"[-+]?\d*\.?\d+", str_val)
            if match:
                return float(match.group())

            return None
        except (ValueError, TypeError):
            return None

    def _parse_boolean(self, value: Any) -> bool:
        """Parse a boolean value from SNMP response.

        TruthValue: true(1), false(2)
        Some OIDs use 0 for false, 1 for true

        Args:
            value: SNMP value to parse

        Returns:
            Boolean value
        """
        try:
            # Handle integer values
            if isinstance(value, int):
                # TruthValue uses 1 for true, 2 for false
                # Some OIDs use 0 for false, 1 for true
                return value == 1

            str_val = str(value).strip().lower()

            # Check for common true values
            if str_val in ("1", "true", "yes", "on", "enabled"):
                return True

            # TruthValue uses 2 for false, but we also accept 0 and other false values
            return False
        except (ValueError, TypeError):
            return False

    def get_config_schema(self) -> dict[str, Any]:
        """Return JSON schema for Omnia MPX plugin configuration."""
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "snmp": {
                    "type": "object",
                    "properties": {
                        "host": {
                            "type": "string",
                            "description": "Device IP address or hostname",
                        },
                        "port": {
                            "type": "integer",
                            "default": 161,
                            "description": "SNMP port",
                        },
                        "community": {
                            "type": "string",
                            "default": "public",
                            "description": "SNMP community string",
                        },
                        "timeout": {
                            "type": "number",
                            "default": 5,
                            "description": "SNMP request timeout in seconds",
                        },
                        "retries": {
                            "type": "integer",
                            "default": 3,
                            "description": "Number of SNMP retries",
                        },
                    },
                    "required": ["host"],
                },
            },
            "required": ["snmp"],
        }

    def get_available_metrics(self) -> list[dict[str, Any]]:
        """Return list of metrics this plugin can collect."""
        return [
            {
                "name": "system.uptime",
                "description": "MPX encoder uptime",
                "unit": "seconds",
                "type": "counter",
            },
            {
                "name": "system.active_time",
                "description": "uMPX active time",
                "unit": "seconds",
                "type": "counter",
            },
            {
                "name": "system.temperature",
                "description": "System temperature",
                "unit": "celsius",
                "type": "gauge",
            },
            {
                "name": "system.cpu_usage",
                "description": "CPU usage percentage",
                "unit": "percent",
                "type": "gauge",
            },
            {
                "name": "audio.level",
                "description": "Audio level",
                "unit": "dB",
                "type": "gauge",
            },
            {
                "name": "audio.clipping",
                "description": "Audio is clipping (1=true, 0=false)",
                "unit": "boolean",
                "type": "gauge",
            },
            {
                "name": "audio.input_clipping",
                "description": "Input audio is clipping (1=true, 0=false)",
                "unit": "boolean",
                "type": "gauge",
            },
            {
                "name": "encoder.enabled",
                "description": "Encoder is enabled (1=true, 0=false)",
                "unit": "boolean",
                "type": "gauge",
            },
            {
                "name": "encoder.status",
                "description": "Encoder status (1=healthy, 0=unhealthy)",
                "unit": "state",
                "type": "gauge",
            },
            {
                "name": "network.port1_up",
                "description": "Network port 1 is up (1=true, 0=false)",
                "unit": "boolean",
                "type": "gauge",
            },
            {
                "name": "network.port2_up",
                "description": "Network port 2 is up (1=true, 0=false)",
                "unit": "boolean",
                "type": "gauge",
            },
        ]
