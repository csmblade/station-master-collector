"""Syslog UDP/TCP server for receiving syslog messages."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Set

import structlog

from collector.core.syslog_parser import SyslogMessage, parse_syslog_message

logger = structlog.get_logger(__name__)

# Maximum message size (64KB for UDP, larger for TCP)
MAX_UDP_SIZE = 65535
MAX_TCP_SIZE = 1024 * 1024  # 1MB


@dataclass
class SyslogSource:
    """Allowed syslog source configuration."""

    id: str
    name: str
    ip_address: str
    enabled: bool


class SyslogUDPProtocol(asyncio.DatagramProtocol):
    """UDP protocol handler for syslog messages."""

    def __init__(
        self,
        on_message: Callable[[SyslogMessage, str, str], None],
        allowed_ips: Set[str],
    ) -> None:
        self.on_message = on_message
        self.allowed_ips = allowed_ips
        self._transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        source_ip = addr[0]

        # Validate source IP
        if self.allowed_ips and source_ip not in self.allowed_ips:
            logger.debug("syslog_rejected_unknown_source", source_ip=source_ip)
            return

        # Parse message
        msg = parse_syslog_message(data, source_ip)
        if msg:
            self.on_message(msg, source_ip, "udp")

    def error_received(self, exc: Exception) -> None:
        logger.warning("syslog_udp_error", error=str(exc))


class SyslogTCPProtocol(asyncio.Protocol):
    """TCP protocol handler for syslog messages."""

    def __init__(
        self,
        on_message: Callable[[SyslogMessage, str, str], None],
        allowed_ips: Set[str],
    ) -> None:
        self.on_message = on_message
        self.allowed_ips = allowed_ips
        self._transport: Optional[asyncio.Transport] = None
        self._buffer = b""
        self._source_ip = ""

    def connection_made(self, transport: asyncio.Transport) -> None:
        self._transport = transport
        peername = transport.get_extra_info("peername")
        if peername:
            self._source_ip = peername[0]

            # Validate source IP
            if self.allowed_ips and self._source_ip not in self.allowed_ips:
                logger.debug("syslog_tcp_rejected_unknown_source", source_ip=self._source_ip)
                transport.close()
                return

            logger.debug("syslog_tcp_connection", source_ip=self._source_ip)

    def data_received(self, data: bytes) -> None:
        self._buffer += data

        # Process complete messages (newline-delimited)
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if line:
                msg = parse_syslog_message(line, self._source_ip)
                if msg:
                    self.on_message(msg, self._source_ip, "tcp")

        # Prevent buffer overflow
        if len(self._buffer) > MAX_TCP_SIZE:
            logger.warning("syslog_tcp_buffer_overflow", source_ip=self._source_ip)
            self._buffer = b""

    def connection_lost(self, exc: Optional[Exception]) -> None:
        # Process any remaining data in buffer
        if self._buffer:
            msg = parse_syslog_message(self._buffer, self._source_ip)
            if msg:
                self.on_message(msg, self._source_ip, "tcp")
        self._buffer = b""


class SyslogServer:
    """Async syslog server supporting UDP and TCP."""

    def __init__(
        self,
        on_message: Callable[[SyslogMessage, str, str], None],
        udp_port: int = 514,
        tcp_port: int = 1514,
        bind_address: str = "0.0.0.0",
    ) -> None:
        """Initialize syslog server.

        Args:
            on_message: Callback for received messages (msg, source_ip, protocol)
            udp_port: UDP listening port (default 514)
            tcp_port: TCP listening port (default 1514)
            bind_address: Address to bind to
        """
        self.on_message = on_message
        self.udp_port = udp_port
        self.tcp_port = tcp_port
        self.bind_address = bind_address

        self._allowed_ips: Set[str] = set()
        self._source_map: dict[str, SyslogSource] = {}  # IP -> Source
        self._udp_transport: Optional[asyncio.DatagramTransport] = None
        self._tcp_server: Optional[asyncio.Server] = None
        self._running = False

    def set_allowed_sources(self, sources: list[SyslogSource]) -> None:
        """Update the list of allowed source IPs.

        Args:
            sources: List of allowed syslog sources
        """
        self._allowed_ips = {s.ip_address for s in sources if s.enabled}
        self._source_map = {s.ip_address: s for s in sources if s.enabled}
        logger.info("syslog_sources_updated", count=len(self._allowed_ips))

    def get_source_id(self, ip_address: str) -> Optional[str]:
        """Get source ID for an IP address.

        Args:
            ip_address: Source IP address

        Returns:
            Source ID or None if not found
        """
        source = self._source_map.get(ip_address)
        return source.id if source else None

    async def start(self) -> None:
        """Start the syslog server (UDP and TCP)."""
        loop = asyncio.get_running_loop()

        # Start UDP server
        try:
            self._udp_transport, _ = await loop.create_datagram_endpoint(
                lambda: SyslogUDPProtocol(self.on_message, self._allowed_ips),
                local_addr=(self.bind_address, self.udp_port),
            )
            logger.info("syslog_udp_started", port=self.udp_port)
        except PermissionError:
            logger.warning(
                "syslog_udp_permission_denied",
                port=self.udp_port,
                hint="Run with elevated privileges or use port > 1024",
            )
        except OSError as e:
            logger.warning("syslog_udp_bind_failed", port=self.udp_port, error=str(e))

        # Start TCP server
        try:
            self._tcp_server = await loop.create_server(
                lambda: SyslogTCPProtocol(self.on_message, self._allowed_ips),
                self.bind_address,
                self.tcp_port,
            )
            logger.info("syslog_tcp_started", port=self.tcp_port)
        except PermissionError:
            logger.warning(
                "syslog_tcp_permission_denied",
                port=self.tcp_port,
                hint="Run with elevated privileges or use port > 1024",
            )
        except OSError as e:
            logger.warning("syslog_tcp_bind_failed", port=self.tcp_port, error=str(e))

        self._running = True

    async def stop(self) -> None:
        """Stop the syslog server."""
        self._running = False

        if self._udp_transport:
            self._udp_transport.close()
            self._udp_transport = None

        if self._tcp_server:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None

        logger.info("syslog_server_stopped")

    @property
    def is_running(self) -> bool:
        """Check if server is running."""
        return self._running
