"""Omnia.9sg Stereo Generator Plugin for Station Master.

Supports Omnia.9sg Stereo Generators (Telos Alliance / Linear Acoustic) via SNMP.
Collects system status, audio levels, loudness measurements, RDS data, and output status.

Uses MIB definitions from mibs/Omnia9sg-MIB.txt.
Enterprise OID: 1.3.6.1.4.1.28660.9008 (Linear Acoustic - Omnia 9sg)
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


# Base OID for Omnia 9sg: 1.3.6.1.4.1.28660.9008
BASE_OID = "1.3.6.1.4.1.28660.9008"

# Standard MIB-2 OIDs for basic device info
STANDARD_OIDS = {
    "sys_descr": "1.3.6.1.2.1.1.1.0",
    "sys_uptime": "1.3.6.1.2.1.1.3.0",
    "sys_name": "1.3.6.1.2.1.1.5.0",
}

# Product Info OIDs (.1.2 = prodinfo-main)
PRODINFO_OIDS = {
    "model_name": f"{BASE_OID}.1.2.1.0",
    "software_version": f"{BASE_OID}.1.2.2.0",
    "firmware_version": f"{BASE_OID}.1.2.3.0",
}

# System Monitoring OIDs (.1.3 = sys-main)
SYSTEM_OIDS = {
    "engine_status": f"{BASE_OID}.1.3.1010.0",
    "ref_rate_status": f"{BASE_OID}.1.3.1011.0",
    "psu1_status": f"{BASE_OID}.1.3.1020.0",
    "psu2_status": f"{BASE_OID}.1.3.1021.0",
    "cpu_utilization": f"{BASE_OID}.1.3.1030.0",
    "cpu_status": f"{BASE_OID}.1.3.1040.0",
    "ram_available": f"{BASE_OID}.1.3.1050.0",
    "ram_status": f"{BASE_OID}.1.3.1060.0",
    "cpu_temperature": f"{BASE_OID}.1.3.1070.0",
    "cpu_temp_status": f"{BASE_OID}.1.3.1080.0",
    "chassis_temperature": f"{BASE_OID}.1.3.1090.0",
    "chassis_temp_status": f"{BASE_OID}.1.3.1100.0",
    "cpu_fan_speed": f"{BASE_OID}.1.3.1110.0",
    "cpu_fan_status": f"{BASE_OID}.1.3.1120.0",
}

# Input Status OIDs (.1.4.1 = input-status-main)
INPUT_OIDS = {
    "backup_input_status": f"{BASE_OID}.1.4.1.1.0",
    "local_input_status": f"{BASE_OID}.1.4.1.2.0",
    "input_silent_status": f"{BASE_OID}.1.4.1.3.0",
    "internal_player_status": f"{BASE_OID}.1.4.1.4.0",
    "input_ch_balance": f"{BASE_OID}.1.4.1.5.0",
    "input_overload_status": f"{BASE_OID}.1.4.1.6.0",
    "digital_input_status": f"{BASE_OID}.1.4.1.7.0",
    "reference_input_status": f"{BASE_OID}.1.4.1.8.0",
    "primary_silent_status": f"{BASE_OID}.1.4.1.9.0",
}

# Loudness Measurement OIDs (.1.4.2 = cur-loudness-main)
LOUDNESS_OIDS = {
    "local_input_loudness": f"{BASE_OID}.1.4.2.1.0",
    "pre_clip_loudness": f"{BASE_OID}.1.4.2.2.0",
    "mpx_out_loudness": f"{BASE_OID}.1.4.2.3.0",
}

# Output Status OIDs (.1.4.3 = output-status-main)
OUTPUT_OIDS = {
    "mpx_out_voltage_1": f"{BASE_OID}.1.4.3.1.0",
    "mpx_out_voltage_2": f"{BASE_OID}.1.4.3.2.0",
    "mpx_power_limit": f"{BASE_OID}.1.4.3.3.0",
    "pilot_level": f"{BASE_OID}.1.4.3.4.0",
    "pre_emphasis": f"{BASE_OID}.1.4.3.5.0",
    "kantar_status": f"{BASE_OID}.1.4.3.6.0",
}

# RDS Status OIDs (.1.4.4 = rds-status-main)
RDS_OIDS = {
    "rds_injection_level": f"{BASE_OID}.1.4.4.1.0",
    "rds_current_ps": f"{BASE_OID}.1.4.4.2.0",
    "rds_current_rt": f"{BASE_OID}.1.4.4.3.0",
    "rds_ta_status": f"{BASE_OID}.1.4.4.4.0",
}

# Preset OIDs (.1.5 = preset-main)
PRESET_OIDS = {
    "sg_preset": f"{BASE_OID}.1.5.1.0",
    "local_proc_preset": f"{BASE_OID}.1.5.2.0",
    "hp_mon_preset": f"{BASE_OID}.1.5.3.0",
    "spk_mon_preset": f"{BASE_OID}.1.5.4.0",
    "aux_mon_preset": f"{BASE_OID}.1.5.5.0",
}


class Omnia9sgPlugin(BasePlugin):
    """Plugin for Omnia.9sg Stereo Generator monitoring."""

    plugin_name = "omnia_9sg"
    plugin_version = "0.1.0"
    plugin_description = "Omnia.9sg Stereo Generator monitoring (Telos Alliance)"
    plugin_author = "Station Master Team"
    supported_protocols = ["snmp"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the plugin."""
        super().__init__(config)

    async def discover_devices(self, config: dict[str, Any]) -> list[DeviceInfo]:
        """Discover Omnia 9sg devices on the network.

        Args:
            config: Discovery configuration

        Returns:
            List of discovered devices
        """
        # TODO: Implement network discovery via SNMP broadcast
        return []

    async def collect(
        self, device: dict[str, Any], config: dict[str, Any]
    ) -> list[MetricPoint]:
        """Collect metrics from an Omnia.9sg Stereo Generator.

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
            "device_type": "omnia_9sg",
            "plugin": self.plugin_name,
        }

        # Collect all SNMP values
        snmp_values = await self._collect_snmp_values(
            host, port, community, timeout, retries
        )

        if not snmp_values:
            logger.warning("no_snmp_values", device_id=device_id, host=host)
            return metrics

        # Add firmware/software version as tags for device tracking
        if snmp_values.get("firmware_version"):
            base_tags["firmware"] = str(snmp_values["firmware_version"])
        if snmp_values.get("software_version"):
            base_tags["software_version"] = str(snmp_values["software_version"])

        # Add preset names as tags if available
        if snmp_values.get("sg_preset"):
            base_tags["sg_preset"] = str(snmp_values["sg_preset"])
        if snmp_values.get("local_proc_preset"):
            base_tags["proc_preset"] = str(snmp_values["local_proc_preset"])

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
        all_oids = {
            **STANDARD_OIDS,
            **PRODINFO_OIDS,
            **SYSTEM_OIDS,
            **INPUT_OIDS,
            **LOUDNESS_OIDS,
            **OUTPUT_OIDS,
            **RDS_OIDS,
            **PRESET_OIDS,
        }

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

        # CPU utilization (percentage)
        if "cpu_utilization" in snmp_values:
            cpu_val = self._parse_numeric(snmp_values["cpu_utilization"])
            if cpu_val is not None:
                metrics.append(
                    MetricPoint(
                        name="system.cpu_utilization",
                        value=cpu_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="percent",
                    )
                )

        # RAM available (MB)
        if "ram_available" in snmp_values:
            ram_val = self._parse_numeric(snmp_values["ram_available"])
            if ram_val is not None:
                metrics.append(
                    MetricPoint(
                        name="system.ram_available",
                        value=ram_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="MB",
                    )
                )

        # CPU temperature (Celsius)
        if "cpu_temperature" in snmp_values:
            temp_val = self._parse_numeric(snmp_values["cpu_temperature"])
            if temp_val is not None and temp_val >= 0:  # -1 means unavailable
                metrics.append(
                    MetricPoint(
                        name="system.cpu_temperature",
                        value=temp_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="celsius",
                    )
                )

        # Chassis temperature (Celsius)
        if "chassis_temperature" in snmp_values:
            temp_val = self._parse_numeric(snmp_values["chassis_temperature"])
            if temp_val is not None and temp_val >= 0:
                metrics.append(
                    MetricPoint(
                        name="system.chassis_temperature",
                        value=temp_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="celsius",
                    )
                )

        # CPU fan speed (RPM)
        if "cpu_fan_speed" in snmp_values:
            fan_val = self._parse_numeric(snmp_values["cpu_fan_speed"])
            if fan_val is not None and fan_val >= 0:  # -1 means unavailable
                metrics.append(
                    MetricPoint(
                        name="system.cpu_fan_speed",
                        value=fan_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="rpm",
                    )
                )

        # Status metrics (ok=1, fail=2 in MIB, convert to 1=ok, 0=fail)
        status_metrics = [
            ("engine_status", "system.engine_status"),
            ("ref_rate_status", "system.ref_rate_status"),
            ("psu1_status", "system.psu1_status"),
            ("psu2_status", "system.psu2_status"),
            ("cpu_status", "system.cpu_status"),
            ("ram_status", "system.ram_status"),
            ("cpu_temp_status", "system.cpu_temp_status"),
            ("chassis_temp_status", "system.chassis_temp_status"),
            ("cpu_fan_status", "system.cpu_fan_status"),
        ]

        for snmp_name, metric_name in status_metrics:
            if snmp_name in snmp_values:
                status_val = self._parse_status(snmp_values[snmp_name])
                metrics.append(
                    MetricPoint(
                        name=metric_name,
                        value=float(status_val),
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="status",
                    )
                )

        # --- Input Status Metrics ---

        input_status_metrics = [
            ("backup_input_status", "input.backup_active"),
            ("local_input_status", "input.local_active"),
            ("input_silent_status", "input.silent"),
            ("internal_player_status", "input.internal_player_active"),
            ("input_overload_status", "input.overload"),
            ("digital_input_status", "input.digital_available"),
            ("reference_input_status", "input.reference_ok"),
            ("primary_silent_status", "input.primary_silent"),
        ]

        for snmp_name, metric_name in input_status_metrics:
            if snmp_name in snmp_values:
                status_val = self._parse_status(snmp_values[snmp_name])
                metrics.append(
                    MetricPoint(
                        name=metric_name,
                        value=float(status_val),
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="status",
                    )
                )

        # Channel balance (ok=1, left-low=2, right-low=3)
        if "input_ch_balance" in snmp_values:
            balance_val = self._parse_numeric(snmp_values["input_ch_balance"])
            if balance_val is not None:
                metrics.append(
                    MetricPoint(
                        name="input.channel_balance",
                        value=balance_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="state",
                    )
                )

        # --- Loudness Metrics ---

        # Loudness values are in centibelFS (divide by 100 for dBFS)
        if "local_input_loudness" in snmp_values:
            loudness_val = self._parse_numeric(snmp_values["local_input_loudness"])
            if loudness_val is not None:
                metrics.append(
                    MetricPoint(
                        name="audio.input_loudness",
                        value=loudness_val / 100.0,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="LUFS",
                    )
                )

        if "pre_clip_loudness" in snmp_values:
            loudness_val = self._parse_numeric(snmp_values["pre_clip_loudness"])
            if loudness_val is not None:
                metrics.append(
                    MetricPoint(
                        name="audio.pre_clip_loudness",
                        value=loudness_val / 100.0,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="LUFS",
                    )
                )

        if "mpx_out_loudness" in snmp_values:
            loudness_val = self._parse_numeric(snmp_values["mpx_out_loudness"])
            if loudness_val is not None:
                metrics.append(
                    MetricPoint(
                        name="audio.mpx_output_loudness",
                        value=loudness_val / 100.0,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="LUFS",
                    )
                )

        # --- Output Status Metrics ---

        # MPX output voltages (millivolts, convert to volts)
        if "mpx_out_voltage_1" in snmp_values:
            voltage_val = self._parse_numeric(snmp_values["mpx_out_voltage_1"])
            if voltage_val is not None and voltage_val >= 0:
                metrics.append(
                    MetricPoint(
                        name="output.mpx_voltage_1",
                        value=voltage_val / 1000.0,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="volts",
                    )
                )

        if "mpx_out_voltage_2" in snmp_values:
            voltage_val = self._parse_numeric(snmp_values["mpx_out_voltage_2"])
            if voltage_val is not None and voltage_val >= 0:
                metrics.append(
                    MetricPoint(
                        name="output.mpx_voltage_2",
                        value=voltage_val / 1000.0,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="volts",
                    )
                )

        # MPX power limit (centibels, convert to dB)
        if "mpx_power_limit" in snmp_values:
            power_val = self._parse_numeric(snmp_values["mpx_power_limit"])
            if power_val is not None:
                metrics.append(
                    MetricPoint(
                        name="output.mpx_power_limit",
                        value=power_val / 100.0,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="dB",
                    )
                )

        # Pilot level (promille, convert to percent)
        if "pilot_level" in snmp_values:
            pilot_val = self._parse_numeric(snmp_values["pilot_level"])
            if pilot_val is not None:
                metrics.append(
                    MetricPoint(
                        name="output.pilot_level",
                        value=pilot_val / 10.0,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="percent",
                    )
                )

        # Pre-emphasis (microseconds)
        if "pre_emphasis" in snmp_values:
            preemph_val = self._parse_numeric(snmp_values["pre_emphasis"])
            if preemph_val is not None:
                metrics.append(
                    MetricPoint(
                        name="output.pre_emphasis",
                        value=preemph_val,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="microseconds",
                    )
                )

        # Kantar status
        if "kantar_status" in snmp_values:
            status_val = self._parse_status(snmp_values["kantar_status"])
            metrics.append(
                MetricPoint(
                    name="output.kantar_status",
                    value=float(status_val),
                    timestamp=timestamp,
                    tags=base_tags.copy(),
                    unit="status",
                )
            )

        # --- RDS Metrics ---

        # RDS injection level (promille, convert to percent)
        if "rds_injection_level" in snmp_values:
            rds_level = self._parse_numeric(snmp_values["rds_injection_level"])
            if rds_level is not None and rds_level >= 0:
                metrics.append(
                    MetricPoint(
                        name="rds.injection_level",
                        value=rds_level / 10.0,
                        timestamp=timestamp,
                        tags=base_tags.copy(),
                        unit="percent",
                    )
                )

        # RDS TA status
        if "rds_ta_status" in snmp_values:
            ta_val = self._parse_status(snmp_values["rds_ta_status"])
            metrics.append(
                MetricPoint(
                    name="rds.ta_active",
                    value=float(ta_val),
                    timestamp=timestamp,
                    tags=base_tags.copy(),
                    unit="status",
                )
            )

        # RDS PS and RT as string metrics (store in tags)
        if "rds_current_ps" in snmp_values:
            ps_text = str(snmp_values["rds_current_ps"]).strip()
            if ps_text:
                rds_tags = base_tags.copy()
                rds_tags["rds_ps"] = ps_text
                metrics.append(
                    MetricPoint(
                        name="rds.ps_present",
                        value=1.0,
                        timestamp=timestamp,
                        tags=rds_tags,
                        unit="status",
                    )
                )

        if "rds_current_rt" in snmp_values:
            rt_text = str(snmp_values["rds_current_rt"]).strip()
            if rt_text:
                rds_tags = base_tags.copy()
                rds_tags["rds_rt"] = rt_text[:64]  # Truncate for tag length
                metrics.append(
                    MetricPoint(
                        name="rds.rt_present",
                        value=1.0,
                        timestamp=timestamp,
                        tags=rds_tags,
                        unit="status",
                    )
                )

        return metrics

    def _parse_numeric(self, value: Any) -> float | None:
        """Parse a numeric value from SNMP response.

        Args:
            value: SNMP value to parse

        Returns:
            Parsed float or None if not parseable
        """
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

    def _parse_status(self, value: Any) -> int:
        """Parse a status value from SNMP response.

        MIB uses: ok(1), fail(2) for most status fields.
        Convert to: 1=ok, 0=fail

        Args:
            value: SNMP value to parse

        Returns:
            1 for ok/active, 0 for fail/inactive
        """
        try:
            if isinstance(value, int):
                return 1 if value == 1 else 0

            str_val = str(value).strip().lower()
            if str_val in ("1", "ok", "true", "yes", "active", "enabled"):
                return 1
            return 0
        except (ValueError, TypeError):
            return 0

    def get_config_schema(self) -> dict[str, Any]:
        """Return JSON schema for Omnia 9sg plugin configuration."""
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
            # System metrics
            {"name": "system.uptime", "description": "System uptime", "unit": "seconds", "type": "counter"},
            {"name": "system.cpu_utilization", "description": "CPU usage", "unit": "percent", "type": "gauge"},
            {"name": "system.ram_available", "description": "Available RAM", "unit": "MB", "type": "gauge"},
            {"name": "system.cpu_temperature", "description": "CPU temperature", "unit": "celsius", "type": "gauge"},
            {"name": "system.chassis_temperature", "description": "Chassis temperature", "unit": "celsius", "type": "gauge"},
            {"name": "system.cpu_fan_speed", "description": "CPU fan speed", "unit": "rpm", "type": "gauge"},
            {"name": "system.engine_status", "description": "Processing engine status", "unit": "status", "type": "gauge"},
            {"name": "system.psu1_status", "description": "PSU 1 status", "unit": "status", "type": "gauge"},
            {"name": "system.psu2_status", "description": "PSU 2 status", "unit": "status", "type": "gauge"},
            # Input metrics
            {"name": "input.silent", "description": "Input silence detected", "unit": "status", "type": "gauge"},
            {"name": "input.overload", "description": "Input overload/clipping", "unit": "status", "type": "gauge"},
            {"name": "input.channel_balance", "description": "Channel balance state", "unit": "state", "type": "gauge"},
            # Audio metrics
            {"name": "audio.input_loudness", "description": "Input loudness (ITU 1770)", "unit": "LUFS", "type": "gauge"},
            {"name": "audio.pre_clip_loudness", "description": "Pre-clipper loudness", "unit": "LUFS", "type": "gauge"},
            {"name": "audio.mpx_output_loudness", "description": "MPX output loudness", "unit": "LUFS", "type": "gauge"},
            # Output metrics
            {"name": "output.mpx_voltage_1", "description": "MPX output 1 voltage", "unit": "volts", "type": "gauge"},
            {"name": "output.mpx_voltage_2", "description": "MPX output 2 voltage", "unit": "volts", "type": "gauge"},
            {"name": "output.mpx_power_limit", "description": "MPX power limit", "unit": "dB", "type": "gauge"},
            {"name": "output.pilot_level", "description": "Pilot level", "unit": "percent", "type": "gauge"},
            {"name": "output.pre_emphasis", "description": "Pre-emphasis", "unit": "microseconds", "type": "gauge"},
            # RDS metrics
            {"name": "rds.injection_level", "description": "RDS injection level", "unit": "percent", "type": "gauge"},
            {"name": "rds.ta_active", "description": "Traffic Announcement active", "unit": "status", "type": "gauge"},
            {"name": "rds.ps_present", "description": "RDS PS text present", "unit": "status", "type": "gauge"},
            {"name": "rds.rt_present", "description": "RDS RadioText present", "unit": "status", "type": "gauge"},
        ]
