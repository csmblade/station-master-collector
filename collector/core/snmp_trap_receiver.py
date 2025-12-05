"""SNMP Trap Receiver for converting traps to syslog-like events."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Set

import structlog
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import ntfrcv
from pysnmp.proto.api import v2c
from pysnmp.smi import builder, view

from collector.core.syslog_parser import SyslogMessage

logger = structlog.get_logger(__name__)

# Standard SNMP trap OIDs
TRAP_OID_MAP = {
    "1.3.6.1.6.3.1.1.5.1": ("coldStart", "System cold start", 5),  # notice
    "1.3.6.1.6.3.1.1.5.2": ("warmStart", "System warm start", 5),  # notice
    "1.3.6.1.6.3.1.1.5.3": ("linkDown", "Interface link down", 4),  # warning
    "1.3.6.1.6.3.1.1.5.4": ("linkUp", "Interface link up", 6),  # info
    "1.3.6.1.6.3.1.1.5.5": ("authenticationFailure", "SNMP authentication failure", 4),  # warning
}

# Generic trap type to severity mapping (SNMPv1)
GENERIC_TRAP_SEVERITY = {
    0: 5,  # coldStart -> notice
    1: 5,  # warmStart -> notice
    2: 4,  # linkDown -> warning
    3: 6,  # linkUp -> info
    4: 4,  # authenticationFailure -> warning
    5: 5,  # egpNeighborLoss -> notice
    6: 6,  # enterpriseSpecific -> info (default)
}


@dataclass
class TrapSource:
    """Allowed SNMP trap source configuration."""

    id: str
    name: str
    ip_address: str
    enabled: bool


class SNMPTrapReceiver:
    """Async SNMP trap receiver that converts traps to syslog events."""

    def __init__(
        self,
        on_message: Callable[[SyslogMessage, str, str], None],
        port: int = 162,
        bind_address: str = "0.0.0.0",
        community: str = "public",
    ) -> None:
        """Initialize SNMP trap receiver.

        Args:
            on_message: Callback for converted messages (msg, source_ip, protocol)
            port: UDP port to listen on (default 162)
            bind_address: Address to bind to
            community: SNMP community string for v1/v2c
        """
        self.on_message = on_message
        self.port = port
        self.bind_address = bind_address
        self.community = community

        self._allowed_ips: Set[str] = set()
        self._source_map: dict[str, TrapSource] = {}
        self._engine: Optional[engine.SnmpEngine] = None
        self._running = False
        self._mib_view: Optional[view.MibViewController] = None

    def set_allowed_sources(self, sources: list[TrapSource]) -> None:
        """Update the list of allowed source IPs.

        Args:
            sources: List of allowed trap sources
        """
        self._allowed_ips = {s.ip_address for s in sources if s.enabled}
        self._source_map = {s.ip_address: s for s in sources if s.enabled}
        logger.info("trap_sources_updated", count=len(self._allowed_ips))

    def get_source_id(self, ip_address: str) -> Optional[str]:
        """Get source ID for an IP address.

        Args:
            ip_address: Source IP address

        Returns:
            Source ID or None if not found
        """
        source = self._source_map.get(ip_address)
        return source.id if source else None

    def _trap_callback(
        self,
        snmp_engine: engine.SnmpEngine,
        state_reference: int,
        context_engine_id: bytes,
        context_name: bytes,
        var_binds: list,
        cb_ctx: dict,
    ) -> None:
        """Callback for received SNMP traps."""
        try:
            # Get transport info
            transport_domain, transport_address = snmp_engine.msgAndPduDsp.getTransportInfo(
                state_reference
            )
            source_ip = transport_address[0] if transport_address else "unknown"

            # Check if source is allowed
            if self._allowed_ips and source_ip not in self._allowed_ips:
                logger.debug("trap_rejected_unknown_source", source_ip=source_ip)
                return

            # Convert trap to syslog message
            msg = self._convert_trap_to_syslog(var_binds, source_ip)
            if msg:
                self.on_message(msg, source_ip, "snmp-trap")

        except Exception as e:
            logger.error("trap_processing_error", error=str(e))

    def _convert_trap_to_syslog(
        self, var_binds: list, source_ip: str
    ) -> Optional[SyslogMessage]:
        """Convert SNMP trap varbinds to a syslog message.

        Args:
            var_binds: List of SNMP variable bindings
            source_ip: Source IP address

        Returns:
            Converted SyslogMessage or None
        """
        try:
            trap_oid = None
            trap_values = {}
            hostname = None
            uptime = None

            # Parse varbinds
            for oid, val in var_binds:
                oid_str = str(oid)

                # sysUpTime
                if oid_str.startswith("1.3.6.1.2.1.1.3"):
                    uptime = str(val)

                # snmpTrapOID
                elif oid_str == "1.3.6.1.6.3.1.1.4.1.0":
                    trap_oid = str(val)

                # sysName
                elif oid_str.startswith("1.3.6.1.2.1.1.5"):
                    hostname = str(val)

                # Store all values
                trap_values[oid_str] = str(val)

            # Determine severity and message
            severity = 6  # default: info
            app_name = "snmptrap"
            message_parts = []

            if trap_oid:
                if trap_oid in TRAP_OID_MAP:
                    name, desc, severity = TRAP_OID_MAP[trap_oid]
                    app_name = name
                    message_parts.append(desc)
                else:
                    # Enterprise-specific trap
                    app_name = "enterprise"
                    message_parts.append(f"Trap OID: {trap_oid}")

            # Add relevant varbind values to message
            for oid, val in trap_values.items():
                if oid not in ("1.3.6.1.2.1.1.3.0", "1.3.6.1.6.3.1.1.4.1.0"):
                    # Skip sysUpTime and snmpTrapOID, include others
                    short_oid = oid.split(".")[-3:] if len(oid.split(".")) > 3 else oid
                    message_parts.append(f"{'.'.join(short_oid)}={val}")

            message = " | ".join(message_parts) if message_parts else "SNMP Trap received"

            # Build raw message for storage
            raw_parts = [f"TRAP from {source_ip}"]
            if trap_oid:
                raw_parts.append(f"OID={trap_oid}")
            for oid, val in trap_values.items():
                raw_parts.append(f"{oid}={val}")
            raw_message = " ".join(raw_parts)

            return SyslogMessage(
                timestamp=datetime.now(),
                facility=23,  # local7 - commonly used for SNMP
                severity=severity,
                hostname=hostname or source_ip,
                app_name=app_name,
                proc_id=None,
                message=message,
                raw_message=raw_message,
            )

        except Exception as e:
            logger.error("trap_conversion_error", error=str(e))
            return None

    async def start(self) -> None:
        """Start the SNMP trap receiver."""
        try:
            # Create SNMP engine
            self._engine = engine.SnmpEngine()

            # Configure transport
            config.addTransport(
                self._engine,
                udp.domainName,
                udp.UdpTransport().openServerMode((self.bind_address, self.port)),
            )

            # Configure community for v1/v2c
            config.addV1System(self._engine, "my-area", self.community)

            # Register callback
            ntfrcv.NotificationReceiver(self._engine, self._trap_callback)

            self._running = True
            logger.info(
                "snmp_trap_receiver_started",
                port=self.port,
                bind_address=self.bind_address,
            )

            # Run the dispatcher in a background task
            asyncio.create_task(self._run_dispatcher())

        except PermissionError:
            logger.warning(
                "snmp_trap_permission_denied",
                port=self.port,
                hint="Run with elevated privileges or use port > 1024",
            )
        except OSError as e:
            logger.warning("snmp_trap_bind_failed", port=self.port, error=str(e))

    async def _run_dispatcher(self) -> None:
        """Run the SNMP dispatcher in async context."""
        if not self._engine:
            return

        try:
            # Get the asyncio dispatcher and run it
            transport_dispatcher = self._engine.transportDispatcher
            transport_dispatcher.jobStarted(1)

            while self._running:
                # Process any pending SNMP messages
                transport_dispatcher.runDispatcher()
                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("snmp_dispatcher_error", error=str(e))

    async def stop(self) -> None:
        """Stop the SNMP trap receiver."""
        self._running = False

        if self._engine:
            try:
                self._engine.transportDispatcher.closeDispatcher()
            except Exception:
                pass
            self._engine = None

        logger.info("snmp_trap_receiver_stopped")

    @property
    def is_running(self) -> bool:
        """Check if receiver is running."""
        return self._running
