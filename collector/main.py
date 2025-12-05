"""Station Master Collector - Main entry point."""

import asyncio
import signal
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import typer

from collector.core.config import CollectorConfig
from collector.core.db_loader import DatabaseConfigLoader
from collector.core.plugin_manager import PluginManager
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

        # Syslog components
        self.syslog_server: SyslogServer | None = None
        self.syslog_transport: SyslogTransport | None = None
        self._syslog_source_refresh_task: asyncio.Task | None = None

        # SNMP trap receiver
        self.snmp_trap_receiver: SNMPTrapReceiver | None = None

    def _on_metrics(self, metrics: list[MetricPoint]) -> None:
        """Callback when metrics are collected."""
        asyncio.create_task(self.transport.enqueue(metrics))

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
        """Periodically check for new devices and add them to the scheduler."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.CONFIG_REFRESH_INTERVAL)

                if not self.db_loader:
                    continue

                api_config = await self.db_loader.fetch_config()
                plugin_configs = self.db_loader.build_plugin_configs(api_config)

                # Check for new devices
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

                if new_devices_found:
                    logger.info(
                        "config_refreshed",
                        total_devices=len(self._known_device_ids),
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("config_refresh_error", error=str(e))

    async def start(self) -> None:
        """Start the collector."""
        logger.info(
            "collector_starting",
            station_id=self.config.station_id,
            station_name=self.config.station_name,
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

            try:
                api_config = await self.db_loader.fetch_config()
                plugin_configs = self.db_loader.build_plugin_configs(api_config)
                await self._load_plugins_from_config(plugin_configs)
            except Exception as e:
                logger.error("failed_to_fetch_db_config", error=str(e))
                raise
        else:
            # Config file mode: use plugins from config
            await self._load_plugins_from_config(self.config.plugins)

        # Start scheduler
        self.scheduler.start()

        # Start config refresh task (database mode only)
        if self.use_database:
            self._config_refresh_task = asyncio.create_task(self._refresh_config())

        # Start syslog server if enabled
        if self.config.syslog.enabled:
            await self._start_syslog_server()

        logger.info(
            "collector_started",
            scheduled_devices=self.scheduler.get_scheduled_devices(),
            syslog_enabled=self.config.syslog.enabled,
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
        self._syslog_source_refresh_task = asyncio.create_task(
            self._refresh_syslog_sources()
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

        # Cancel config refresh task
        if self._config_refresh_task:
            self._config_refresh_task.cancel()
            try:
                await self._config_refresh_task
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
