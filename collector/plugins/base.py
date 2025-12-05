"""Base plugin interface for all collector plugins."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MetricPoint:
    """A single metric data point."""

    name: str
    value: float
    timestamp: float
    tags: dict[str, str] = field(default_factory=dict)
    unit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        result = {
            "name": self.name,
            "value": self.value,
            "timestamp": self.timestamp,
            "tags": self.tags,
        }
        if self.unit:
            result["unit"] = self.unit
        return result


@dataclass
class PluginMetadata:
    """Metadata about a plugin."""

    name: str
    version: str
    description: str
    author: str
    supported_protocols: list[str]
    default_poll_interval: int = 30


@dataclass
class DeviceInfo:
    """Information about a discovered device."""

    id: str
    name: str
    model: str
    manufacturer: str
    firmware_version: str | None = None
    serial_number: str | None = None
    capabilities: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class BasePlugin(ABC):
    """Abstract base class for all collector plugins.

    All plugins must inherit from this class and implement the required methods.
    """

    # Class attributes - override in subclasses
    plugin_name: str = "base"
    plugin_version: str = "0.0.0"
    plugin_description: str = ""
    plugin_author: str = ""
    supported_protocols: list[str] = []

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the plugin.

        Args:
            config: Plugin-specific configuration
        """
        self._config = config or {}
        self._initialized = False

    @property
    def metadata(self) -> PluginMetadata:
        """Get plugin metadata."""
        return PluginMetadata(
            name=self.plugin_name,
            version=self.plugin_version,
            description=self.plugin_description,
            author=self.plugin_author,
            supported_protocols=self.supported_protocols,
        )

    async def initialize(self) -> None:
        """Initialize plugin resources.

        Override this method to perform async initialization.
        """
        self._initialized = True

    async def shutdown(self) -> None:
        """Clean up plugin resources.

        Override this method to perform cleanup on shutdown.
        """
        self._initialized = False

    @abstractmethod
    async def discover_devices(self, config: dict[str, Any]) -> list[DeviceInfo]:
        """Discover devices on the network.

        Args:
            config: Discovery configuration (network ranges, credentials, etc.)

        Returns:
            List of discovered devices
        """
        ...

    @abstractmethod
    async def collect(
        self, device: dict[str, Any], config: dict[str, Any]
    ) -> list[MetricPoint]:
        """Collect metrics from a device.

        Args:
            device: Device information
            config: Collection configuration

        Returns:
            List of collected metric points
        """
        ...

    @abstractmethod
    def get_config_schema(self) -> dict[str, Any]:
        """Return JSON schema for plugin configuration.

        Returns:
            JSON schema dictionary
        """
        ...

    def get_available_metrics(self) -> list[dict[str, Any]]:
        """Return list of metrics this plugin can collect.

        Override to provide metric documentation.

        Returns:
            List of metric definitions
        """
        return []

    def validate_device_config(self, config: dict[str, Any]) -> list[str]:
        """Validate device configuration.

        Args:
            config: Device configuration to validate

        Returns:
            List of validation error messages (empty if valid)
        """
        return []

    async def test_connection(self, device: dict[str, Any]) -> tuple[bool, str]:
        """Test connection to a device.

        Args:
            device: Device configuration

        Returns:
            Tuple of (success, message)
        """
        try:
            metrics = await self.collect(device, {})
            if metrics:
                return True, f"Successfully collected {len(metrics)} metrics"
            return True, "Connection successful but no metrics returned"
        except Exception as e:
            return False, str(e)
