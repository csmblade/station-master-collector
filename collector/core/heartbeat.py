"""Heartbeat manager for collector health reporting."""

import asyncio
import ssl
import socket
import time
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from collector.core.config import CollectorConfig

logger = structlog.get_logger(__name__)


class HeartbeatManager:
    """Manages periodic heartbeat reporting to the central server.

    Reports collector health status, system metrics, and receives
    role assignment for HA scenarios.
    """

    HEARTBEAT_INTERVAL = 30  # seconds
    HEARTBEAT_TIMEOUT = 30.0  # seconds - timeout for single heartbeat
    HEALTH_THRESHOLD = 90  # seconds - max age of last successful heartbeat

    def __init__(self, config: "CollectorConfig") -> None:
        """Initialize the heartbeat manager.

        Args:
            config: Collector configuration
        """
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None

        # Hostname will be set from server's station_name after first config fetch
        # Use collector_name from config if explicitly set, otherwise temporary socket hostname
        if config.collector_name:
            self._hostname = config.collector_name
        else:
            # Temporary - will be updated with station name from server
            self._hostname = socket.gethostname()

        # State tracking
        self._collector_id: str | None = None
        self._role: str = "primary"
        self._should_collect: bool = True
        self._config_version_hash: str | None = None
        self._config_changed: bool = False

        # Metrics counters (updated by main collector)
        self._metrics_collected: int = 0
        self._error_count: int = 0

        # Health tracking
        self._last_successful_heartbeat: float = 0.0
        self._consecutive_failures: int = 0

    @property
    def hostname(self) -> str:
        """Get the current hostname used for registration."""
        return self._hostname

    @property
    def collector_id(self) -> str | None:
        """Get the assigned collector ID."""
        return self._collector_id

    @property
    def role(self) -> str:
        """Get current role (primary/backup)."""
        return self._role

    @property
    def should_collect(self) -> bool:
        """Check if this collector should actively collect data."""
        return self._should_collect

    @property
    def config_changed(self) -> bool:
        """Check if server config has changed since last heartbeat."""
        return self._config_changed

    def set_config_version(self, version_hash: str) -> None:
        """Update the current config version hash.

        Args:
            version_hash: SHA256 hash of current config (first 16 chars)
        """
        self._config_version_hash = version_hash

    def set_hostname_from_station(self, station_name: str) -> None:
        """Set hostname based on station name from server.

        This should be called after fetching config from the server,
        before the first heartbeat is sent. Format: "{station_name} - Collector"

        Args:
            station_name: Station name from server config
        """
        if station_name and not self._config.collector_name:
            new_hostname = f"{station_name} - Collector"
            if new_hostname != self._hostname:
                logger.info(
                    "hostname_updated_from_server",
                    old_hostname=self._hostname,
                    new_hostname=new_hostname,
                    station_name=station_name,
                )
                self._hostname = new_hostname

    def increment_metrics(self, count: int = 1) -> None:
        """Increment the metrics collected counter.

        Args:
            count: Number of metrics to add
        """
        self._metrics_collected += count

    def increment_errors(self, count: int = 1) -> None:
        """Increment the error counter.

        Args:
            count: Number of errors to add
        """
        self._error_count += count

    def is_healthy(self) -> bool:
        """Check if heartbeat is healthy.

        Returns:
            True if last successful heartbeat was within threshold
        """
        if self._last_successful_heartbeat == 0.0:
            # No successful heartbeat yet - healthy if just started
            return True

        elapsed = time.time() - self._last_successful_heartbeat
        return elapsed < self.HEALTH_THRESHOLD and self._consecutive_failures < 3

    async def start(self) -> None:
        """Start the heartbeat manager."""
        # Build SSL context from TLS config
        ssl_context = self._build_ssl_context()

        self._client = httpx.AsyncClient(
            base_url=self._config.server_url,
            headers={
                "Authorization": f"Bearer {self._config.api_key.get_secret_value()}",
                "X-Station-ID": self._config.station_id,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
            verify=ssl_context if ssl_context else self._config.tls.verify_ssl,
        )

        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("heartbeat_manager_started", hostname=self._hostname)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """Build SSL context from TLS configuration.

        Returns:
            SSL context or None if using default verification
        """
        tls = self._config.tls

        # If not verifying SSL, return None (httpx will handle it)
        if not tls.verify_ssl:
            return None

        # If no custom CA or client certs, use default behavior
        if not tls.ca_bundle_path and not tls.client_cert_path:
            return None

        # Build custom SSL context
        ssl_context = ssl.create_default_context()

        # Set minimum TLS version
        if tls.min_tls_version == "1.3":
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_3
        else:
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2

        # Load custom CA bundle
        if tls.ca_bundle_path and tls.ca_bundle_path.exists():
            ssl_context.load_verify_locations(cafile=str(tls.ca_bundle_path))

        # Load client certificate for mTLS
        if tls.client_cert_path and tls.client_key_path:
            if tls.client_cert_path.exists() and tls.client_key_path.exists():
                ssl_context.load_cert_chain(
                    certfile=str(tls.client_cert_path),
                    keyfile=str(tls.client_key_path),
                )

        return ssl_context

    async def stop(self) -> None:
        """Stop the heartbeat manager."""
        self._running = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.aclose()

        logger.info("heartbeat_manager_stopped")

    async def _heartbeat_loop(self) -> None:
        """Background task for periodic heartbeat."""
        # Send initial heartbeat immediately
        await self._send_heartbeat()

        while self._running:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                await self._send_heartbeat()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("heartbeat_loop_error", error=str(e))
                # Wait a bit before retrying on error
                await asyncio.sleep(5)

    async def _send_heartbeat(self) -> None:
        """Send a single heartbeat to the server with timeout."""
        if not self._client:
            return

        # Collect system metrics
        memory_mb, cpu_percent = self._get_system_metrics()

        payload = {
            "hostname": self._hostname,
            "status": "running",
            "metrics_collected": self._metrics_collected,
            "error_count": self._error_count,
            "memory_usage_mb": memory_mb,
            "cpu_usage_percent": cpu_percent,
            "config_version_hash": self._config_version_hash,
            "collector_version": self._get_version(),
        }

        try:
            # Wrap HTTP request with timeout
            response = await asyncio.wait_for(
                self._client.post(
                    "/api/v1/collector/heartbeat",
                    json=payload,
                ),
                timeout=self.HEARTBEAT_TIMEOUT,
            )
            response.raise_for_status()

            data = response.json()

            # Update state from response
            previous_role = self._role
            self._collector_id = data.get("collector_id")
            self._role = data.get("role", "primary")
            self._should_collect = data.get("should_collect", True)
            self._config_changed = data.get("config_changed", False)

            # Mark successful heartbeat
            self._last_successful_heartbeat = time.time()
            self._consecutive_failures = 0

            # Log role changes
            if previous_role != self._role:
                logger.info(
                    "role_changed",
                    old_role=previous_role,
                    new_role=self._role,
                    should_collect=self._should_collect,
                )

            if self._config_changed:
                logger.info("server_config_changed")

            logger.debug(
                "heartbeat_sent",
                collector_id=self._collector_id,
                role=self._role,
                should_collect=self._should_collect,
            )

        except asyncio.TimeoutError:
            self._consecutive_failures += 1
            logger.warning(
                "heartbeat_timeout",
                timeout_seconds=self.HEARTBEAT_TIMEOUT,
                consecutive_failures=self._consecutive_failures,
            )
        except httpx.HTTPStatusError as e:
            self._consecutive_failures += 1
            logger.warning(
                "heartbeat_failed",
                status_code=e.response.status_code,
                error=str(e),
                consecutive_failures=self._consecutive_failures,
            )
        except Exception as e:
            self._consecutive_failures += 1
            logger.warning(
                "heartbeat_error",
                error=str(e),
                consecutive_failures=self._consecutive_failures,
            )

    def _get_system_metrics(self) -> tuple[float | None, float | None]:
        """Get current system memory and CPU usage.

        Returns:
            Tuple of (memory_mb, cpu_percent) or None values if unavailable
        """
        memory_mb = None
        cpu_percent = None

        try:
            import psutil

            # Get memory usage of current process
            process = psutil.Process()
            memory_info = process.memory_info()
            memory_mb = memory_info.rss / (1024 * 1024)

            # Get CPU usage (averaged over a short interval)
            cpu_percent = process.cpu_percent(interval=0.1)

        except ImportError:
            # psutil not available - try alternative methods
            try:
                import resource
                usage = resource.getrusage(resource.RUSAGE_SELF)
                memory_mb = usage.ru_maxrss / 1024  # Convert KB to MB on Linux
            except (ImportError, AttributeError):
                pass

        except Exception as e:
            logger.debug("system_metrics_error", error=str(e))

        return memory_mb, cpu_percent

    def _get_version(self) -> str:
        """Get collector version string.

        Returns:
            Version string
        """
        try:
            from importlib.metadata import version
            return version("station-master-collector")
        except Exception:
            return "0.1.0"

    async def send_immediate(self) -> dict[str, Any]:
        """Send an immediate heartbeat and return the response.

        Useful for checking role assignment on startup.

        Returns:
            Heartbeat response data
        """
        if not self._client:
            raise RuntimeError("HeartbeatManager not started")

        memory_mb, cpu_percent = self._get_system_metrics()

        payload = {
            "hostname": self._hostname,
            "status": "running",
            "metrics_collected": self._metrics_collected,
            "error_count": self._error_count,
            "memory_usage_mb": memory_mb,
            "cpu_usage_percent": cpu_percent,
            "config_version_hash": self._config_version_hash,
            "collector_version": self._get_version(),
        }

        # Wrap HTTP request with timeout
        response = await asyncio.wait_for(
            self._client.post(
                "/api/v1/collector/heartbeat",
                json=payload,
            ),
            timeout=self.HEARTBEAT_TIMEOUT,
        )
        response.raise_for_status()

        data = response.json()

        # Update state
        self._collector_id = data.get("collector_id")
        self._role = data.get("role", "primary")
        self._should_collect = data.get("should_collect", True)
        self._config_changed = data.get("config_changed", False)

        # Mark successful heartbeat
        self._last_successful_heartbeat = time.time()
        self._consecutive_failures = 0

        logger.info(
            "initial_heartbeat",
            collector_id=self._collector_id,
            role=self._role,
            should_collect=self._should_collect,
        )

        return data
