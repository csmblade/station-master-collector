"""Omnia VOLT audio processor plugin for Station Master.

Supports Omnia VOLT audio processors (Telos Alliance) via SNMP.
Collects system status, input levels, output levels, and preset information.

Uses MIB definitions from mibs/TLS-OMNIA-VOLT-MIB.txt.
Enterprise OID: 1.3.6.1.4.1.42463.14 (Telos Alliance - Omnia VOLT)
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

# Base OID for Omnia VOLT: 1.3.6.1.4.1.42463.14
BASE_OID = "1.3.6.1.4.1.42463.14"

# Standard MIB-2 OIDs for basic device info
STANDARD_OIDS = {
    "sys_descr": "1.3.6.1.2.1.1.1.0",
    "sys_uptime": "1.3.6.1.2.1.1.3.0",
    "sys_name": "1.3.6.1.2.1.1.5.0",
}

# System OIDs (.1 = omniaVoltSystem)
SYSTEM_OIDS = {
    "uptime": f"{BASE_OID}.1.1.0",           # voltUpTime
    "software_version": f"{BASE_OID}.1.2.0",  # voltSoftwareVersion
}

# Configuration OIDs (.2 = omniaVoltConfiguration)
CONFIG_OIDS = {
    "current_input": f"{BASE_OID}.2.1.0",       # voltCurrentInput
    "current_preset": f"{BASE_OID}.2.2.0",      # voltCurrentPreset
    "failover_state": f"{BASE_OID}.2.3.0",      # voltInputFailoverState
    "composite_out_1": f"{BASE_OID}.2.4.0",     # voltCompositeOutputOneLevel
    "composite_out_2": f"{BASE_OID}.2.5.0",     # voltCompositeOutputTwoLevel
    "pilot_level": f"{BASE_OID}.2.6.0",         # voltCompositeOutputPilotLevel
    "analog_input_level": f"{BASE_OID}.2.7.0",  # voltAnalogInputLevel
    "aes_input_level": f"{BASE_OID}.2.8.0",     # voltAesInputLevel
    "input_mode": f"{BASE_OID}.2.9.0",          # voltInputMode
}


class OmniaVoltPlugin(BasePlugin):
    """Plugin for Omnia VOLT audio processor monitoring."""

    plugin_name = "omnia_volt"
    plugin_version = "0.1.0"
    plugin_description = "Omnia VOLT audio processor monitoring (Telos Alliance)"
    plugin_author = "Station Master Team"
    supported_protocols = ["snmp"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the plugin."""
        super().__init__(config)

    async def discover_devices(self, config: dict[str, Any]) -> list[DeviceInfo]:
        """Discover Omnia VOLT devices on the network."""
        return []

    async def collect(
        self, device: dict[str, Any], config: dict[str, Any]
    ) -> list[MetricPoint]:
        """Collect metrics from an Omnia VOLT audio processor."""
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
            "device_type": "omnia_volt",
            "plugin": self.plugin_name,
        }

        # Collect all SNMP values
        snmp_values = await self._collect_snmp_values(
            host, port, community, timeout, retries
        )

        if not snmp_values:
            logger.warning("no_snmp_values", device_id=device_id, host=host)
            return metrics

        # Add software version as tag
        if snmp_values.get("software_version"):
            base_tags["software_version"] = str(snmp_values["software_version"])

        # Add current preset as tag
        if snmp_values.get("current_preset"):
            base_tags["preset"] = str(snmp_values["current_preset"])

        # Add current input as tag
        if snmp_values.get("current_input"):
            base_tags["input_source"] = str(snmp_values["current_input"])

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

        all_oids = {
            **STANDARD_OIDS,
            **SYSTEM_OIDS,
            **CONFIG_OIDS,
        }

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
        """Parse SNMP values into MetricPoint objects."""
        metrics: list[MetricPoint] = []

        # --- System Metrics ---

        # Standard MIB-2 uptime (timeticks, 1/100th of a second)
        if "sys_uptime" in snmp_values:
            uptime_ticks = self._parse_numeric(snmp_values["sys_uptime"])
            if uptime_ticks is not None:
                metrics.append(
                    MetricPoint(
                        name="system.uptime",
                        value=uptime_ticks / 100.0,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="seconds",
                    )
                )

        # Parse uptime string from VOLT (format varies, try to extract seconds)
        if "uptime" in snmp_values:
            uptime_str = str(snmp_values["uptime"])
            uptime_seconds = self._parse_uptime_string(uptime_str)
            if uptime_seconds is not None:
                metrics.append(
                    MetricPoint(
                        name="system.volt_uptime",
                        value=uptime_seconds,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="seconds",
                    )
                )

        # --- Input/Output Status ---

        # Failover state (0=normal, 1=failover)
        if "failover_state" in snmp_values:
            failover_val = self._parse_numeric(snmp_values["failover_state"])
            if failover_val is not None:
                metrics.append(
                    MetricPoint(
                        name="input.failover_state",
                        value=failover_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="state",
                    )
                )

        # Analog input level (parse dB from string)
        if "analog_input_level" in snmp_values:
            level_val = self._parse_db_value(snmp_values["analog_input_level"])
            if level_val is not None:
                metrics.append(
                    MetricPoint(
                        name="input.analog_level",
                        value=level_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="dB",
                    )
                )

        # AES input level (parse dB from string)
        if "aes_input_level" in snmp_values:
            level_val = self._parse_db_value(snmp_values["aes_input_level"])
            if level_val is not None:
                metrics.append(
                    MetricPoint(
                        name="input.aes_level",
                        value=level_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="dB",
                    )
                )

        # Composite output 1 level
        if "composite_out_1" in snmp_values:
            level_val = self._parse_db_value(snmp_values["composite_out_1"])
            if level_val is not None:
                metrics.append(
                    MetricPoint(
                        name="output.composite_1_level",
                        value=level_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="dB",
                    )
                )

        # Composite output 2 level
        if "composite_out_2" in snmp_values:
            level_val = self._parse_db_value(snmp_values["composite_out_2"])
            if level_val is not None:
                metrics.append(
                    MetricPoint(
                        name="output.composite_2_level",
                        value=level_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="dB",
                    )
                )

        # Pilot level
        if "pilot_level" in snmp_values:
            level_val = self._parse_db_value(snmp_values["pilot_level"])
            if level_val is not None:
                metrics.append(
                    MetricPoint(
                        name="output.pilot_level",
                        value=level_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="dB",
                    )
                )

        # Input mode as a presence metric with mode in tag
        if "input_mode" in snmp_values:
            mode_str = str(snmp_values["input_mode"]).strip()
            if mode_str:
                mode_tags = base_tags.copy()
                mode_tags["input_mode"] = mode_str
                metrics.append(
                    MetricPoint(
                        name="input.mode_active",
                        value=1.0,
                        timestamp=timestamp,
                        tags=mode_tags,
                        unit="status",
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

    def _parse_db_value(self, value: Any) -> float | None:
        """Parse a dB value from SNMP string response.

        Handles formats like: "-10.5 dB", "10.5dB", "-10.5", etc.
        """
        try:
            str_val = str(value).strip().lower()
            # Remove 'db' suffix if present
            str_val = str_val.replace("db", "").strip()
            match = re.search(r"[-+]?\d*\.?\d+", str_val)
            if match:
                return float(match.group())
            return None
        except (ValueError, TypeError):
            return None

    def _parse_uptime_string(self, uptime_str: str) -> float | None:
        """Parse uptime string into seconds.

        Handles formats like:
        - "1 day, 2:30:45"
        - "1d 2h 30m 45s"
        - "2:30:45"
        - "12345" (already seconds)
        """
        try:
            uptime_str = uptime_str.strip()

            # Try direct numeric first
            if uptime_str.isdigit():
                return float(uptime_str)

            total_seconds = 0.0

            # Try days pattern
            days_match = re.search(r"(\d+)\s*(?:day|d)", uptime_str, re.IGNORECASE)
            if days_match:
                total_seconds += int(days_match.group(1)) * 86400

            # Try hours pattern
            hours_match = re.search(r"(\d+)\s*(?:hour|h|:)", uptime_str, re.IGNORECASE)
            if hours_match:
                total_seconds += int(hours_match.group(1)) * 3600

            # Try HH:MM:SS pattern
            time_match = re.search(r"(\d+):(\d+):(\d+)", uptime_str)
            if time_match:
                h, m, s = map(int, time_match.groups())
                total_seconds = h * 3600 + m * 60 + s
                # Add back days if found
                if days_match:
                    total_seconds += int(days_match.group(1)) * 86400
                return total_seconds

            # Try minutes pattern
            mins_match = re.search(r"(\d+)\s*(?:min|m)", uptime_str, re.IGNORECASE)
            if mins_match:
                total_seconds += int(mins_match.group(1)) * 60

            # Try seconds pattern
            secs_match = re.search(r"(\d+)\s*(?:sec|s)", uptime_str, re.IGNORECASE)
            if secs_match:
                total_seconds += int(secs_match.group(1))

            return total_seconds if total_seconds > 0 else None

        except (ValueError, TypeError):
            return None

    def get_config_schema(self) -> dict[str, Any]:
        """Return JSON schema for Omnia VOLT plugin configuration."""
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
            {"name": "system.uptime", "description": "System uptime (MIB-2)", "unit": "seconds", "type": "counter"},
            {"name": "system.volt_uptime", "description": "VOLT reported uptime", "unit": "seconds", "type": "counter"},
            {"name": "input.failover_state", "description": "Failover state (0=normal, 1=failover)", "unit": "state", "type": "gauge"},
            {"name": "input.analog_level", "description": "Analog input level", "unit": "dB", "type": "gauge"},
            {"name": "input.aes_level", "description": "AES/EBU input level", "unit": "dB", "type": "gauge"},
            {"name": "input.mode_active", "description": "Input mode active", "unit": "status", "type": "gauge"},
            {"name": "output.composite_1_level", "description": "Composite output 1 level", "unit": "dB", "type": "gauge"},
            {"name": "output.composite_2_level", "description": "Composite output 2 level", "unit": "dB", "type": "gauge"},
            {"name": "output.pilot_level", "description": "Pilot level", "unit": "dB", "type": "gauge"},
        ]
