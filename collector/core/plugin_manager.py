"""Plugin discovery and lifecycle management."""

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collector.core.config import PluginConfig
    from collector.plugins.base import BasePlugin

logger = structlog.get_logger(__name__)


class PluginManager:
    """Manages plugin discovery, loading, and lifecycle."""

    # Built-in plugins
    BUILTIN_PLUGINS = {
        "nautel": "collector.plugins.nautel",
        "axia": "collector.plugins.axia",
        "omnia": "collector.plugins.omnia",
        "omnia_mpx": "collector.plugins.omnia_mpx",
        "omnia_mpx_decoder": "collector.plugins.omnia_mpx_decoder",
        "omnia_9sg": "collector.plugins.omnia_9sg",
        "omnia_volt": "collector.plugins.omnia_volt",
        "zetta": "collector.plugins.zetta",
    }

    def __init__(self, custom_plugin_path: Path | None = None) -> None:
        """Initialize the plugin manager.

        Args:
            custom_plugin_path: Optional path to custom plugins directory
        """
        self._plugins: dict[str, type["BasePlugin"]] = {}
        self._instances: dict[str, "BasePlugin"] = {}
        self._custom_plugin_path = custom_plugin_path

    def discover_plugins(self) -> dict[str, type["BasePlugin"]]:
        """Discover all available plugins.

        Returns:
            Dictionary mapping plugin names to plugin classes
        """
        discovered: dict[str, type["BasePlugin"]] = {}

        # Load built-in plugins
        for name, module_path in self.BUILTIN_PLUGINS.items():
            try:
                plugin_class = self._load_plugin_module(module_path)
                if plugin_class:
                    discovered[name] = plugin_class
                    logger.info("discovered_builtin_plugin", plugin=name)
                else:
                    logger.error(
                        "plugin_class_not_found",
                        plugin=name,
                        module_path=module_path,
                        message="Module loaded but no BasePlugin subclass found.",
                    )
            except Exception as e:
                logger.error(
                    "failed_to_load_builtin_plugin",
                    plugin=name,
                    error=str(e),
                    message="Plugin failed to load. Devices using this plugin will not be collected.",
                )

        # Load custom plugins
        if self._custom_plugin_path and self._custom_plugin_path.exists():
            for plugin_dir in self._custom_plugin_path.iterdir():
                if plugin_dir.is_dir() and (plugin_dir / "__init__.py").exists():
                    try:
                        plugin_class = self._load_custom_plugin(plugin_dir)
                        if plugin_class:
                            name = plugin_dir.name
                            discovered[name] = plugin_class
                            logger.info("discovered_custom_plugin", plugin=name)
                        else:
                            logger.error(
                                "custom_plugin_class_not_found",
                                path=str(plugin_dir),
                                message="Plugin loaded but no BasePlugin subclass found.",
                            )
                    except Exception as e:
                        logger.error(
                            "failed_to_load_custom_plugin",
                            path=str(plugin_dir),
                            error=str(e),
                            message="Custom plugin failed to load.",
                        )

        self._plugins = discovered
        return discovered

    def _load_plugin_module(self, module_path: str) -> type["BasePlugin"] | None:
        """Load a plugin from a module path."""
        from collector.plugins.base import BasePlugin

        module = importlib.import_module(module_path)

        # Find the plugin class in the module
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BasePlugin)
                and attr is not BasePlugin
            ):
                return attr

        return None

    def _load_custom_plugin(self, plugin_dir: Path) -> type["BasePlugin"] | None:
        """Load a custom plugin from a directory."""
        from collector.plugins.base import BasePlugin

        init_file = plugin_dir / "__init__.py"
        spec = importlib.util.spec_from_file_location(plugin_dir.name, init_file)
        if spec is None or spec.loader is None:
            return None

        module = importlib.util.module_from_spec(spec)
        sys.modules[plugin_dir.name] = module
        spec.loader.exec_module(module)

        # Find the plugin class
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BasePlugin)
                and attr is not BasePlugin
            ):
                return attr

        return None

    def get_plugin_class(self, name: str) -> type["BasePlugin"] | None:
        """Get a plugin class by name."""
        return self._plugins.get(name)

    def instantiate_plugin(
        self, name: str, config: "PluginConfig"
    ) -> "BasePlugin | None":
        """Create an instance of a plugin.

        Args:
            name: Plugin name
            config: Plugin configuration

        Returns:
            Plugin instance or None if plugin not found
        """
        if name in self._instances:
            return self._instances[name]

        plugin_class = self.get_plugin_class(name)
        if plugin_class is None:
            logger.error("plugin_not_found", plugin=name)
            return None

        try:
            instance = plugin_class(config)
            self._instances[name] = instance
            logger.info("plugin_instantiated", plugin=name)
            return instance
        except Exception as e:
            logger.error("plugin_instantiation_failed", plugin=name, error=str(e))
            return None

    def get_instance(self, name: str) -> "BasePlugin | None":
        """Get an existing plugin instance."""
        return self._instances.get(name)

    def list_plugins(self) -> list[str]:
        """List all discovered plugin names."""
        return list(self._plugins.keys())

    def list_instances(self) -> list[str]:
        """List all instantiated plugin names."""
        return list(self._instances.keys())

    async def shutdown_all(self) -> None:
        """Gracefully shutdown all plugin instances."""
        for name, instance in self._instances.items():
            try:
                if hasattr(instance, "shutdown"):
                    await instance.shutdown()
                logger.info("plugin_shutdown", plugin=name)
            except Exception as e:
                logger.error("plugin_shutdown_failed", plugin=name, error=str(e))

        self._instances.clear()
