"""Station Master Collector - Main entry point."""

import asyncio
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Coroutine

import structlog
import typer

from collector.core.config import CollectorConfig
from collector.core.db_loader import DatabaseConfigLoader
from collector.core.heartbeat import HeartbeatManager
from collector.core.plugin_manager import PluginManager
from collector.core.remote_logging import RemoteLogHandler
from collector.core.scheduler import CollectionScheduler
from collector.core.transport import SecureTransport
from collector.core.syslog_server import SyslogServer, SyslogSource
from collector.core.syslog_transport import SyslogTransport
from collector.core.syslog_parser import SyslogMessage
from collector.core.snmp_trap_receiver import SNMPTrapReceiver, TrapSource
from collector.plugins.base import MetricPoint

if TYPE_CHECKING:
    from collector.core.config import PluginConfig

app = typer.Typer(
    name="station-collector",
    help="Station Master Collector - Endpoint data collection agent",
)

logger = structlog.get_logger(__name__)


class Collector:
    """Main collector application."""

    CONFIG_REFRESH_INTERVAL = 60  # seconds - check for new devices every minute
    ROLE_CHECK_INTERVAL = 5  # seconds - check for role changes

    def __init__(self, config: CollectorConfig, use_database: bool = False) -> None:
        self.config = config
        self.use_database = use_database
        self.plugin_manager = PluginManager()
        self.transport = SecureTransport(config)
        self.scheduler = CollectionScheduler(
            on_metrics_collected=self._on_metrics,
            max_concurrent_devices=config.max_concurrent_devices,
        )
        self.db_loader: DatabaseConfigLoader | None = None
        self._shutdown_event = asyncio.Event()
        self._active_plugins: dict[str, "PluginConfig"] = {}
        self._known_device_ids: set[str] = set()
        self._config_refresh_task: asyncio.Task | None = None
        self._role_monitor_task: asyncio.Task | None = None

        # Enterprise features
        self.heartbeat_manager: HeartbeatManager | None = None
        self.remote_log_handler: RemoteLogHandler | None = None
        self._current_config_version: str | None = None
        self._scheduler_running: bool = False

        # Syslog components
        self.syslog_server: SyslogServer | None = None
        self.syslog_transport: SyslogTransport | None = None
        self._syslog_source_refresh_task: asyncio.Task | None = None

        # SNMP trap receiver
        self.snmp_trap_receiver: SNMPTrapReceiver | None = None

        # Reliability: watchdog and task monitoring
        self._running: bool = False
        self._last_activity: float = time.time()
        self._watchdog_task: asyncio.Task | None = None
        self._monitored_tasks: list[asyncio.Task] = []
        self._health_file = Path("/tmp/collector_heartbeat")
        self._unhealthy_file = Path("/tmp/collector_unhealthy")

    def _on_metrics(self, metrics: list[MetricPoint]) -> None:
        """Callback when metrics are collected."""
        asyncio.create_task(self.transport.enqueue(metrics))

        # Update heartbeat metrics counter
        if self.heartbeat_manager:
            self.heartbeat_manager.increment_metrics(len(metrics))

        # Touch health file to indicate activity
        self._touch_health_file()

    def _on_syslog_message(
        self, message: SyslogMessage, source_ip: str, protocol: str
    ) -> None:
        """Callback when a syslog message is received."""
        if not self.syslog_transport or not self.syslog_server:
            return

        source_id = self.syslog_server.get_source_id(source_ip)
        if not source_id:
            logger.debug("syslog_unknown_source", source_ip=source_ip)
            return

        asyncio.create_task(
            self.syslog_transport.enqueue(message, source_ip, source_id, protocol)
        )

    def _touch_health_file(self) -> None:
        """Touch the health file to indicate the collector is active."""
        try:
            self._health_file.touch()
            self._last_activity = time.time()
            # Remove unhealthy marker if present
            if self._unhealthy_file.exists():
                self._unhealthy_file.unlink()
        except Exception:
            pass  # Ignore file system errors

    def _create_monitored_task(
        self, coro: Coroutine[Any, Any, Any], name: str
    ) -> asyncio.Task:
        """Create a task with exception logging and monitoring."""
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._task_done_callback)
        self._monitored_tasks.append(task)
        return task

    def _task_done_callback(self, task: asyncio.Task) -> None:
        """Handle completed/failed background tasks."""
        # Remove from monitored list
        if task in self._monitored_tasks:
            self._monitored_tasks.remove(task)

        if task.cancelled():
            logger.debug("task_cancelled", task=task.get_name())
            return

        exc = task.exception()
        if exc:
            logger.error(
                "background_task_failed",
                task=task.get_name(),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Mark as unhealthy
            try:
                self._unhealthy_file.touch()
            except Exception:
                pass

    async def _watchdog(self) -> None:
        """Watchdog task that checks for activity and marks unhealthy if frozen."""
        WATCHDOG_INTERVAL = 60  # Check every 60 seconds
        ACTIVITY_TIMEOUT = 300  # 5 minutes without activity = unhealthy

        while self._running:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL)

                elapsed = time.time() - self._last_activity
                if elapsed > ACTIVITY_TIMEOUT:
                    logger.critical(
                        "watchdog_timeout",
                        last_activity_seconds_ago=elapsed,
                        timeout_threshold=ACTIVITY_TIMEOUT,
                    )
                    try:
                        self._unhealthy_file.touch()
                    except Exception:
                        pass
                else:
                    logger.debug(
                        "watchdog_ok",
                        last_activity_seconds_ago=int(elapsed),
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("watchdog_error", error=str(e))

    def health_check(self) -> dict:
        """Return health status for external health checks."""
        now = time.time()
        activity_age = now - self._last_activity

        # Check if heartbeat is healthy
        heartbeat_ok = True
        if self.heartbeat_manager:
            heartbeat_ok = self.heartbeat_manager.is_healthy()

        # Check if scheduler is healthy
        scheduler_ok = True
        if hasattr(self.scheduler, 'is_healthy'):
            scheduler_ok = self.scheduler.is_healthy()

        healthy = (
            self._running
            and activity_age < 120  # Activity within last 2 minutes
            and heartbeat_ok
            and scheduler_ok
            and not self._unhealthy_file.exists()
        )

        return {
            "healthy": healthy,
            "running": self._running,
            "last_activity_seconds_ago": int(activity_age),
            "heartbeat_ok": heartbeat_ok,
            "scheduler_ok": scheduler_ok,
            "monitored_tasks": len(self._monitored_tasks),
        }

    async def _refresh_syslog_sources(self) -> None:
        """Periodically refresh allowed syslog sources from API."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.config.syslog.source_refresh_interval)

                if not self.syslog_transport or not self.syslog_server:
                    continue

                sources_data = await self.syslog_transport.fetch_allowed_sources()
                sources = [
                    SyslogSource(
                        id=s["id"],
                        name=s["name"],
                        ip_address=s["ip_address"],
                        enabled=s["enabled"],
                    )
                    for s in sources_data
                ]
                self.syslog_server.set_allowed_sources(sources)

                # Also update SNMP trap receiver if running
                if self.snmp_trap_receiver:
                    trap_sources = [
                        TrapSource(
                            id=s["id"],
                            name=s["name"],
                            ip_address=s["ip_address"],
                            enabled=s["enabled"],
                        )
                        for s in sources_data
                    ]
                    self.snmp_trap_receiver.set_allowed_sources(trap_sources)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("syslog_source_refresh_error", error=str(e))

    async def _load_plugins_from_config(
        self, plugin_configs: list["PluginConfig"]
    ) -> None:
        """Load and schedule plugins from configuration."""
        for plugin_config in plugin_configs:
            if not plugin_config.enabled:
                continue

            # Track device IDs we've seen
            for device in plugin_config.devices:
                self._known_device_ids.add(device.id)

            plugin = self.plugin_manager.instantiate_plugin(
                plugin_config.plugin_name,
                plugin_config,
            )
            if plugin:
                await plugin.initialize()
                self.scheduler.schedule_plugin(plugin, plugin_config)
                self._active_plugins[plugin_config.plugin_name] = plugin_config

    async def _refresh_config(self) -> None:
        """Periodically check for device changes and update the scheduler."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.CONFIG_REFRESH_INTERVAL)

                if not self.db_loader:
                    continue

                # Check if heartbeat indicates config changed
                if self.heartbeat_manager and not self.heartbeat_manager.config_changed:
                    # Config hasn't changed, skip refresh
                    continue

                api_config = await self.db_loader.fetch_config()
                plugin_configs = self.db_loader.build_plugin_configs(api_config)

                # Update config version hash
                config_version = api_config.get("config_version")
                if config_version and config_version != self._current_config_version:
                    self._current_config_version = config_version
                    if self.heartbeat_manager:
                        self.heartbeat_manager.set_config_version(config_version)
                    logger.info("config_version_updated", version=config_version)

                # Get all current device IDs from API
                api_device_ids: set[str] = set()
                for plugin_config in plugin_configs:
                    if plugin_config.enabled:
                        for device in plugin_config.devices:
                            api_device_ids.add(device.id)

                # Find removed devices
                removed_device_ids = self._known_device_ids - api_device_ids
                if removed_device_ids:
                    for device_id in removed_device_ids:
                        self._known_device_ids.discard(device_id)
                        self.scheduler.unschedule_device(device_id)
                        logger.info("device_removed", device_id=device_id)

                # Find new devices
                new_devices_found = False
                for plugin_config in plugin_configs:
                    if not plugin_config.enabled:
                        continue

                    new_devices = [
                        d for d in plugin_config.devices
                        if d.id not in self._known_device_ids
                    ]

                    if new_devices:
                        new_devices_found = True
                        for device in new_devices:
                            self._known_device_ids.add(device.id)
                            logger.info(
                                "new_device_discovered",
                                device_id=device.id,
                                device_name=device.name,
                                plugin=plugin_config.plugin_name,
                            )

                        # Get or create plugin instance and schedule new devices
                        plugin = self.plugin_manager.instantiate_plugin(
                            plugin_config.plugin_name,
                            plugin_config,
                        )
                        if plugin:
                            # Create a config with only the new devices
                            from collector.core.config import PluginConfig as PC
                            new_device_config = PC(
                                plugin_name=plugin_config.plugin_name,
                                enabled=True,
                                devices=new_devices,
                            )
                            self.scheduler.schedule_plugin(plugin, new_device_config)

                if new_devices_found or removed_device_ids:
                    logger.info(
                        "config_refreshed",
                        total_devices=len(self._known_device_ids),
                        added=len(api_device_ids - self._known_device_ids) if new_devices_found else 0,
                        removed=len(removed_device_ids),
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("config_refresh_error", error=str(e))
                if self.heartbeat_manager:
                    self.heartbeat_manager.increment_errors()

    async def _monitor_role(self) -> None:
        """Monitor role changes and start/stop scheduler accordingly.

        This task runs continuously checking if the collector's role has changed
        (e.g., promoted from backup to primary) and adjusts the scheduler.
        """
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.ROLE_CHECK_INTERVAL)

                if not self.heartbeat_manager:
                    continue

                should_collect = self.heartbeat_manager.should_collect

                if should_collect and not self._scheduler_running:
                    # Need to start collecting
                    logger.info("starting_scheduler", role=self.heartbeat_manager.role)
                    self.scheduler.start()
                    self._scheduler_running = True

                elif not should_collect and self._scheduler_running:
                    # Need to stop collecting (demoted to backup)
                    logger.info("stopping_scheduler", role=self.heartbeat_manager.role)
                    self.scheduler.stop()
                    self._scheduler_running = False

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("role_monitor_error", error=str(e))

    async def start(self) -> None:
        """Start the collector."""
        self._running = True
        self._last_activity = time.time()

        # Clean up any stale health files from previous runs
        try:
            if self._unhealthy_file.exists():
                self._unhealthy_file.unlink()
        except Exception:
            pass

        logger.info(
            "collector_starting",
            station_id=self.config.station_id,
            mode="database" if self.use_database else "config_file",
        )

        # Discover available plugins
        self.plugin_manager.discover_plugins()
        logger.info(
            "plugins_discovered",
            plugins=self.plugin_manager.list_plugins(),
        )

        # Initialize transport
        await self.transport.start()

        if self.use_database:
            # Database mode: fetch config from API
            self.db_loader = DatabaseConfigLoader(self.config)
            await self.db_loader.start()

            # Fetch config first to get station name for collector registration
            try:
                api_config = await self.db_loader.fetch_config()
                server_station_name = api_config.get("station_name", "")
            except Exception as e:
                logger.error("failed_to_fetch_initial_config", error=str(e))
                raise

            # Start heartbeat manager with station name from server
            self.heartbeat_manager = HeartbeatManager(self.config)
            if server_station_name:
                self.heartbeat_manager.set_hostname_from_station(server_station_name)
            await self.heartbeat_manager.start()

            logger.info(
                "collector_identity",
                hostname=self.heartbeat_manager.hostname,
                station_name=server_station_name,
            )

            # Start remote log handler
            self.remote_log_handler = RemoteLogHandler(self.config)
            await self.remote_log_handler.start()

            # Send initial heartbeat and get role assignment
            try:
                heartbeat_response = await self.heartbeat_manager.send_immediate()
                should_collect = heartbeat_response.get("should_collect", True)
            except Exception as e:
                logger.warning("initial_heartbeat_failed", error=str(e))
                should_collect = True  # Assume primary if can't reach server

            try:
                # Build plugin configs from already-fetched api_config
                plugin_configs = self.db_loader.build_plugin_configs(api_config)

                # Store config version
                self._current_config_version = api_config.get("config_version")
                if self._current_config_version and self.heartbeat_manager:
                    self.heartbeat_manager.set_config_version(self._current_config_version)

                # Diagnostic logging: show device summary
                total_devices = sum(len(pc.devices) for pc in plugin_configs)
                enabled_devices = sum(
                    len([d for d in pc.devices if d.enabled])
                    for pc in plugin_configs
                )
                plugin_names = [pc.plugin_name for pc in plugin_configs if pc.enabled]

                if total_devices == 0:
                    logger.warning(
                        "no_devices_configured",
                        station_id=self.config.station_id,
                        message="No devices returned from server. Check dashboard configuration.",
                    )
                else:
                    logger.info(
                        "device_config_summary",
                        total_devices=total_devices,
                        enabled_devices=enabled_devices,
                        plugins=plugin_names,
                    )

                await self._load_plugins_from_config(plugin_configs)
            except Exception as e:
                logger.error("failed_to_fetch_db_config", error=str(e))
                raise

            # Start scheduler only if we should collect (primary or HA disabled)
            if should_collect:
                self.scheduler.start()
                self._scheduler_running = True
                logger.info(
                    "collection_active",
                    role=self.heartbeat_manager.role if self.heartbeat_manager else "unknown",
                    scheduled_devices=len(self._known_device_ids),
                )
            else:
                logger.warning(
                    "collection_inactive",
                    role=self.heartbeat_manager.role if self.heartbeat_manager else "unknown",
                    reason="backup_role",
                    message="Collector is in backup mode. It will not collect data until promoted to primary.",
                )

            # Start role monitor task for HA
            self._role_monitor_task = self._create_monitored_task(
                self._monitor_role(), "role_monitor"
            )

        else:
            # Config file mode: use plugins from config
            await self._load_plugins_from_config(self.config.plugins)
            self.scheduler.start()
            self._scheduler_running = True

        # Start config refresh task (database mode only)
        if self.use_database:
            self._config_refresh_task = self._create_monitored_task(
                self._refresh_config(), "config_refresh"
            )

        # Start syslog server if enabled
        if self.config.syslog.enabled:
            await self._start_syslog_server()

        # Start watchdog task
        self._watchdog_task = self._create_monitored_task(
            self._watchdog(), "watchdog"
        )

        # Touch health file to indicate successful startup
        self._touch_health_file()

        logger.info(
            "collector_started",
            scheduled_devices=self.scheduler.get_scheduled_devices(),
            syslog_enabled=self.config.syslog.enabled,
            scheduler_running=self._scheduler_running,
        )

    async def _start_syslog_server(self) -> None:
        """Initialize and start the syslog server."""
        # Initialize syslog transport
        self.syslog_transport = SyslogTransport(
            config=self.config,
            batch_size=self.config.syslog.batch_size,
            flush_interval=self.config.syslog.flush_interval,
        )
        await self.syslog_transport.start()

        # Fetch initial allowed sources
        sources_data = await self.syslog_transport.fetch_allowed_sources()
        sources = [
            SyslogSource(
                id=s["id"],
                name=s["name"],
                ip_address=s["ip_address"],
                enabled=s["enabled"],
            )
            for s in sources_data
        ]

        # Initialize and start syslog server
        self.syslog_server = SyslogServer(
            on_message=self._on_syslog_message,
            udp_port=self.config.syslog.udp_port,
            tcp_port=self.config.syslog.tcp_port,
            bind_address=self.config.syslog.bind_address,
        )
        self.syslog_server.set_allowed_sources(sources)
        await self.syslog_server.start()

        # Start source refresh task
        self._syslog_source_refresh_task = self._create_monitored_task(
            self._refresh_syslog_sources(), "syslog_source_refresh"
        )

        logger.info(
            "syslog_server_started",
            udp_port=self.config.syslog.udp_port,
            tcp_port=self.config.syslog.tcp_port,
            allowed_sources=len(sources),
        )

        # Start SNMP trap receiver if enabled
        if self.config.syslog.snmp_trap_enabled:
            await self._start_snmp_trap_receiver(sources_data)

    async def _start_snmp_trap_receiver(self, sources_data: list[dict]) -> None:
        """Initialize and start the SNMP trap receiver."""
        # Convert sources to TrapSource objects
        trap_sources = [
            TrapSource(
                id=s["id"],
                name=s["name"],
                ip_address=s["ip_address"],
                enabled=s["enabled"],
            )
            for s in sources_data
        ]

        # Initialize and start SNMP trap receiver
        self.snmp_trap_receiver = SNMPTrapReceiver(
            on_message=self._on_syslog_message,  # Reuse syslog callback
            port=self.config.syslog.snmp_trap_port,
            bind_address=self.config.syslog.bind_address,
            community=self.config.syslog.snmp_trap_community,
        )
        self.snmp_trap_receiver.set_allowed_sources(trap_sources)
        await self.snmp_trap_receiver.start()

        logger.info(
            "snmp_trap_receiver_started",
            port=self.config.syslog.snmp_trap_port,
            allowed_sources=len(trap_sources),
        )

    async def stop(self) -> None:
        """Stop the collector gracefully."""
        logger.info("collector_stopping")
        self._running = False

        # Cancel watchdog task
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass

        # Cancel config refresh task
        if self._config_refresh_task:
            self._config_refresh_task.cancel()
            try:
                await self._config_refresh_task
            except asyncio.CancelledError:
                pass

        # Cancel role monitor task
        if self._role_monitor_task:
            self._role_monitor_task.cancel()
            try:
                await self._role_monitor_task
            except asyncio.CancelledError:
                pass

        # Cancel syslog source refresh task
        if self._syslog_source_refresh_task:
            self._syslog_source_refresh_task.cancel()
            try:
                await self._syslog_source_refresh_task
            except asyncio.CancelledError:
                pass

        # Stop syslog components
        if self.syslog_server:
            await self.syslog_server.stop()
        if self.syslog_transport:
            await self.syslog_transport.stop()

        # Stop SNMP trap receiver
        if self.snmp_trap_receiver:
            await self.snmp_trap_receiver.stop()

        # Stop enterprise components
        if self.remote_log_handler:
            await self.remote_log_handler.stop()
        if self.heartbeat_manager:
            await self.heartbeat_manager.stop()

        self.scheduler.stop()
        await self.transport.stop()
        await self.plugin_manager.shutdown_all()

        if self.db_loader:
            await self.db_loader.stop()

        logger.info("collector_stopped")

    async def run(self) -> None:
        """Run the collector until shutdown signal."""
        await self.start()

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        await self.stop()

    def signal_shutdown(self) -> None:
        """Signal the collector to shut down."""
        self._shutdown_event.set()


def setup_logging(log_level: str, log_format: str) -> None:
    """Configure structured logging."""
    import logging

    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
            if log_format == "console"
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            level_map.get(log_level.upper(), logging.INFO)
        ),
    )


@app.command()
def run(
    config_file: Path = typer.Option(
        "config.yaml",
        "--config",
        "-c",
        help="Path to configuration file",
    ),
    database: bool = typer.Option(
        False,
        "--database",
        "-d",
        help="Fetch device configuration from database API instead of config file",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        "-l",
        help="Logging level",
    ),
    log_format: str = typer.Option(
        "console",
        "--log-format",
        help="Log format: console or json",
    ),
) -> None:
    """Run the Station Master collector agent."""
    setup_logging(log_level, log_format)

    # Load configuration
    try:
        config = CollectorConfig.from_file(config_file)
    except FileNotFoundError:
        logger.error("config_file_not_found", path=str(config_file))
        raise typer.Exit(1)
    except Exception as e:
        logger.error("config_load_error", error=str(e))
        raise typer.Exit(1)

    # Create collector
    collector = Collector(config, use_database=database)

    # Setup signal handlers
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, collector.signal_shutdown)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        loop.run_until_complete(collector.run())
    except KeyboardInterrupt:
        collector.signal_shutdown()
        loop.run_until_complete(collector.stop())
    finally:
        loop.close()


@app.command()
def discover(
    plugin: str = typer.Argument(..., help="Plugin name to use for discovery"),
    network: str = typer.Option(
        "192.168.1.0/24",
        "--network",
        "-n",
        help="Network range to scan",
    ),
) -> None:
    """Discover devices on the network using a specific plugin."""
    setup_logging("INFO", "console")

    plugin_manager = PluginManager()
    plugin_manager.discover_plugins()

    plugin_class = plugin_manager.get_plugin_class(plugin)
    if not plugin_class:
        logger.error("plugin_not_found", plugin=plugin)
        raise typer.Exit(1)

    async def do_discovery() -> None:
        instance = plugin_class({})
        devices = await instance.discover_devices({"network": network})

        if devices:
            typer.echo(f"Discovered {len(devices)} device(s):")
            for device in devices:
                typer.echo(f"  - {device.name} ({device.model}) at {device.id}")
        else:
            typer.echo("No devices discovered")

    asyncio.run(do_discovery())


@app.command()
def plugins() -> None:
    """List available plugins."""
    plugin_manager = PluginManager()
    discovered = plugin_manager.discover_plugins()

    typer.echo("Available plugins:")
    for name, plugin_class in discovered.items():
        instance = plugin_class({})
        meta = instance.metadata
        typer.echo(f"  {name} (v{meta.version})")
        typer.echo(f"    {meta.description}")
        typer.echo(f"    Protocols: {', '.join(meta.supported_protocols)}")


@app.command()
def validate(
    config_file: Path = typer.Argument(..., help="Configuration file to validate"),
) -> None:
    """Validate a configuration file."""
    try:
        config = CollectorConfig.from_file(config_file)
        typer.echo(f"Configuration valid: {config.station_name}")
        typer.echo(f"  Station ID: {config.station_id}")
        typer.echo(f"  Server: {config.server_url}")
        typer.echo(f"  Plugins: {len(config.plugins)}")
        for plugin in config.plugins:
            status = "enabled" if plugin.enabled else "disabled"
            typer.echo(f"    - {plugin.plugin_name}: {len(plugin.devices)} devices ({status})")
    except Exception as e:
        typer.echo(f"Configuration error: {e}", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
