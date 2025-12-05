"""Omnia MPX Decoder Plugin for Station Master.

Supports Omnia MPX Decoder nodes (Telos Alliance) via SNMP.
Collects system status, stream info, decoder state, and network status metrics.

Uses MIB definitions from mibs/TLS-OMNIA-MPX-DECODER-MIB.mib.
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


# SNMP OIDs from Omnia MPX Decoder MIB (mibs/TLS-OMNIA-MPX-DECODER-MIB.mib)
# Enterprise: 1.3.6.1.4.1.42463 (Telos Alliance)
# MPX Decoder: .16
#
# Standard MIB-2 OIDs for basic device info
STANDARD_OIDS = {
    "sys_descr": "1.3.6.1.2.1.1.1.0",
    "sys_uptime": "1.3.6.1.2.1.1.3.0",
    "sys_name": "1.3.6.1.2.1.1.5.0",
}

# Omnia MPX Decoder enterprise OIDs (1.3.6.1.4.1.42463.16.x.x)
OMNIA_MPX_DECODER_OIDS = {
    # System info (1.3.6.1.4.1.42463.16.1.x)
    "firmware_version": "1.3.6.1.4.1.42463.16.1.1.0",
    "hostname": "1.3.6.1.4.1.42463.16.1.2.0",
    "temperature": "1.3.6.1.4.1.42463.16.1.3.0",
    "net1_connect_time": "1.3.6.1.4.1.42463.16.1.4.0",
    "net2_connect_time": "1.3.6.1.4.1.42463.16.1.5.0",
    "cpu_usage": "1.3.6.1.4.1.42463.16.1.6.0",
    "stream_delay": "1.3.6.1.4.1.42463.16.1.7.0",
    "mpx_output_level": "1.3.6.1.4.1.42463.16.1.8.0",
    "decoder_status": "1.3.6.1.4.1.42463.16.1.9.0",
    "decoder_enabled": "1.3.6.1.4.1.42463.16.1.10.0",
    "uptime": "1.3.6.1.4.1.42463.16.1.11.0",
    "umpx_app_version": "1.3.6.1.4.1.42463.16.1.12.0",
    "umpx_data_stream": "1.3.6.1.4.1.42463.16.1.13.0",
    "umpx_output": "1.3.6.1.4.1.42463.16.1.14.0",
    "umpx_connection_time": "1.3.6.1.4.1.42463.16.1.15.0",
    "packets_received": "1.3.6.1.4.1.42463.16.1.16.0",
    "packets_recovered": "1.3.6.1.4.1.42463.16.1.17.0",
    "packets_silence": "1.3.6.1.4.1.42463.16.1.18.0",
    "keyframes_received": "1.3.6.1.4.1.42463.16.1.19.0",
    # GPO status (1.3.6.1.4.1.42463.16.3.x)
    "input_stream_received": "1.3.6.1.4.1.42463.16.3.1.0",
    "output_stream_playing": "1.3.6.1.4.1.42463.16.3.2.0",
    "network_one_up": "1.3.6.1.4.1.42463.16.3.3.0",
    "network_two_up": "1.3.6.1.4.1.42463.16.3.4.0",
}


class OmniaMPXDecoderPlugin(BasePlugin):
    """Plugin for Omnia MPX Decoder nodes."""

    plugin_name = "omnia_mpx_decoder"
    plugin_version = "0.1.0"
    plugin_description = "Omnia MPX Decoder node monitoring (Telos Alliance)"
    plugin_author = "Station Master Team"
    supported_protocols = ["snmp"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the plugin."""
        super().__init__(config)

    async def discover_devices(self, config: dict[str, Any]) -> list[DeviceInfo]:
        """Discover Omnia MPX decoder nodes on the network."""
        return []

    async def collect(
        self, device: dict[str, Any], config: dict[str, Any]
    ) -> list[MetricPoint]:
        """Collect metrics from an Omnia MPX decoder node."""
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

        base_tags = {
            "device_id": device_id,
            "device_name": device_name,
            "manufacturer": "telos_alliance",
            "device_type": "omnia_mpx_decoder",
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
        """Collect all SNMP values from the device using parallel fetching."""
        snmp_engine = SnmpEngine()
        community_data = CommunityData(community)
        context_data = ContextData()

        all_oids = {**STANDARD_OIDS, **OMNIA_MPX_DECODER_OIDS}

        tasks = [
            self._fetch_single_oid(
                snmp_engine, community_data, context_data,
                host, port, timeout, retries, name, oid
            )
            for name, oid in all_oids.items()
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

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
        """Fetch a single OID value."""
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
                logger.debug("snmp_error_indication", oid=name, error=str(error_indication))
                return None

            if error_status:
                logger.debug("snmp_error_status", oid=name, error=error_status.prettyPrint())
                return None

            for var_bind in var_binds:
                value = var_bind[1]
                logger.debug(
                    "snmp_value_received",
                    oid=name,
                    type=type(value).__name__,
                    value=str(value)[:100],
                )
                return (name, value)

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
        """Parse SNMP values into MetricPoint objects."""
        metrics: list[MetricPoint] = []

        # Standard MIB-2 uptime (timeticks)
        if "sys_uptime" in snmp_values:
            uptime_ticks = self._parse_numeric(snmp_values["sys_uptime"])
            if uptime_ticks is not None:
                metrics.append(
                    MetricPoint(
                        name="system.uptime_ticks",
                        value=uptime_ticks / 100.0,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="seconds",
                    )
                )

        # MPX-specific uptime
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

        # Temperature
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

        # CPU usage
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

        # MPX output level
        if "mpx_output_level" in snmp_values:
            level_val = self._parse_numeric(snmp_values["mpx_output_level"])
            if level_val is not None:
                metrics.append(
                    MetricPoint(
                        name="audio.output_level",
                        value=level_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="dB",
                    )
                )

        # Stream delay
        if "stream_delay" in snmp_values:
            delay_val = self._parse_numeric(snmp_values["stream_delay"])
            if delay_val is not None:
                metrics.append(
                    MetricPoint(
                        name="stream.delay",
                        value=delay_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="ms",
                    )
                )

        # Decoder enabled
        if "decoder_enabled" in snmp_values:
            enabled_val = self._parse_boolean(snmp_values["decoder_enabled"])
            metrics.append(
                MetricPoint(
                    name="decoder.enabled",
                    value=float(enabled_val),
                    timestamp=timestamp,
                    tags=base_tags.copy(),
                    unit="boolean",
                )
            )

        # Decoder status
        if "decoder_status" in snmp_values:
            status_str = str(snmp_values["decoder_status"])
            status_tags = base_tags.copy()
            status_tags["status"] = status_str
            metrics.append(
                MetricPoint(
                    name="decoder.status",
                    value=1.0 if status_str.lower() in ["ok", "running", "active", "enabled"] else 0.0,
                    timestamp=timestamp,
                    tags=status_tags,
                    unit="state",
                )
            )

        # Input stream received
        if "input_stream_received" in snmp_values:
            stream_val = self._parse_boolean(snmp_values["input_stream_received"])
            metrics.append(
                MetricPoint(
                    name="stream.input_receiving",
                    value=float(stream_val),
                    timestamp=timestamp,
                    tags=base_tags.copy(),
                    unit="boolean",
                )
            )

        # Output stream playing
        if "output_stream_playing" in snmp_values:
            output_val = self._parse_boolean(snmp_values["output_stream_playing"])
            metrics.append(
                MetricPoint(
                    name="stream.output_playing",
                    value=float(output_val),
                    timestamp=timestamp,
                    tags=base_tags.copy(),
                    unit="boolean",
                )
            )

        # Packets received
        if "packets_received" in snmp_values:
            packets_val = self._parse_numeric(snmp_values["packets_received"])
            if packets_val is not None:
                metrics.append(
                    MetricPoint(
                        name="stream.packets_received",
                        value=packets_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="count",
                    )
                )

        # Packets recovered
        if "packets_recovered" in snmp_values:
            recovered_val = self._parse_numeric(snmp_values["packets_recovered"])
            if recovered_val is not None:
                metrics.append(
                    MetricPoint(
                        name="stream.packets_recovered",
                        value=recovered_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="count",
                    )
                )

        # Packets silence
        if "packets_silence" in snmp_values:
            silence_val = self._parse_numeric(snmp_values["packets_silence"])
            if silence_val is not None:
                metrics.append(
                    MetricPoint(
                        name="stream.packets_silence",
                        value=silence_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="count",
                    )
                )

        # Network port 1 up
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

        # Network port 2 up
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

        # uMPX connection time
        if "umpx_connection_time" in snmp_values:
            conn_time = self._parse_numeric(snmp_values["umpx_connection_time"])
            if conn_time is not None:
                metrics.append(
                    MetricPoint(
                        name="stream.connection_time",
                        value=conn_time,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="seconds",
                    )
                )

        return metrics

    def _parse_numeric(self, value: Any) -> float | None:
        """Parse a numeric value from SNMP response."""
        try:
            if isinstance(value, (int, float)):
                return float(value)

            str_val = str(value).strip()
            match = re.search(r"[-+]?\d*\.?\d+", str_val)
            if match:
                return float(match.group())

            return None
        except (ValueError, TypeError):
            return None

    def _parse_boolean(self, value: Any) -> bool:
        """Parse a boolean value from SNMP response."""
        try:
            if isinstance(value, int):
                return value == 1

            str_val = str(value).strip().lower()
            return str_val in ("1", "true", "yes", "on", "enabled")
        except (ValueError, TypeError):
            return False

    def get_config_schema(self) -> dict[str, Any]:
        """Return JSON schema for configuration."""
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "snmp": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": "integer", "default": 161},
                        "community": {"type": "string", "default": "public"},
                        "timeout": {"type": "number", "default": 5},
                        "retries": {"type": "integer", "default": 3},
                    },
                    "required": ["host"],
                },
            },
            "required": ["snmp"],
        }

    def get_available_metrics(self) -> list[dict[str, Any]]:
        """Return list of metrics this plugin can collect."""
        return [
            {"name": "system.uptime", "description": "Decoder uptime", "unit": "seconds"},
            {"name": "system.temperature", "description": "System temperature", "unit": "celsius"},
            {"name": "system.cpu_usage", "description": "CPU usage", "unit": "percent"},
            {"name": "audio.output_level", "description": "MPX output level", "unit": "dB"},
            {"name": "stream.delay", "description": "Stream delay", "unit": "ms"},
            {"name": "stream.input_receiving", "description": "Input stream receiving", "unit": "boolean"},
            {"name": "stream.output_playing", "description": "Output stream playing", "unit": "boolean"},
            {"name": "stream.packets_received", "description": "Packets received", "unit": "count"},
            {"name": "stream.packets_recovered", "description": "Packets recovered (FEC)", "unit": "count"},
            {"name": "stream.packets_silence", "description": "Packets played silence", "unit": "count"},
            {"name": "decoder.enabled", "description": "Decoder enabled", "unit": "boolean"},
            {"name": "decoder.status", "description": "Decoder status", "unit": "state"},
            {"name": "network.port1_up", "description": "Network port 1 up", "unit": "boolean"},
            {"name": "network.port2_up", "description": "Network port 2 up", "unit": "boolean"},
        ]
