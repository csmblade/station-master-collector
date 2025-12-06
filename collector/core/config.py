"""Configuration management for the collector agent."""

from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SNMPConfig(BaseSettings):
    """SNMP connection configuration."""

    host: str
    port: int = 161
    version: str = "v2c"
    community: SecretStr | None = None
    v3_user: str | None = None
    v3_auth_protocol: str | None = None
    v3_auth_password: SecretStr | None = None
    v3_priv_protocol: str | None = None
    v3_priv_password: SecretStr | None = None
    timeout: int = 5
    retries: int = 3

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        if v not in ("v1", "v2c", "v3"):
            raise ValueError("SNMP version must be v1, v2c, or v3")
        return v


class HTTPConfig(BaseSettings):
    """HTTP API connection configuration."""

    base_url: str
    auth_type: str = "none"
    username: str | None = None
    password: SecretStr | None = None
    api_key: SecretStr | None = None
    api_key_header: str = "X-API-Key"
    timeout: int = 30
    verify_ssl: bool = True


class DeviceConfig(BaseSettings):
    """Configuration for a single device."""

    id: str
    name: str
    enabled: bool = True
    poll_interval: int = 30
    snmp: SNMPConfig | None = None
    http: HTTPConfig | None = None
    custom_tags: dict[str, str] = Field(default_factory=dict)


class PluginConfig(BaseSettings):
    """Configuration for a plugin instance."""

    plugin_name: str
    enabled: bool = True
    devices: list[DeviceConfig] = Field(default_factory=list)
    plugin_settings: dict[str, Any] = Field(default_factory=dict)


class SyslogConfig(BaseSettings):
    """Syslog server configuration."""

    enabled: bool = False
    udp_port: int = 514
    tcp_port: int = 1514
    bind_address: str = "0.0.0.0"
    batch_size: int = 100
    flush_interval: int = 5
    source_refresh_interval: int = 60  # seconds

    # SNMP trap receiver settings
    snmp_trap_enabled: bool = False
    snmp_trap_port: int = 162
    snmp_trap_community: str = "public"


class TLSConfig(BaseSettings):
    """TLS/HTTPS configuration for server communication."""

    # Verify server certificate (should be True in production)
    verify_ssl: bool = True

    # Path to CA bundle (optional, uses system defaults if not set)
    ca_bundle_path: Path | None = None

    # Client certificate authentication (mTLS)
    client_cert_path: Path | None = None
    client_key_path: Path | None = None

    # Minimum TLS version (1.2 or 1.3)
    min_tls_version: str = "1.2"

    @field_validator("min_tls_version")
    @classmethod
    def validate_tls_version(cls, v: str) -> str:
        if v not in ("1.2", "1.3"):
            raise ValueError("min_tls_version must be 1.2 or 1.3")
        return v


class CollectorConfig(BaseSettings):
    """Main collector configuration."""

    model_config = SettingsConfigDict(
        env_prefix="COLLECTOR_",
        env_file=".env",
        env_nested_delimiter="__",
    )

    # Station identity
    station_id: str
    station_name: str = ""  # Optional - will be fetched from server

    # Collector identity (optional override)
    # If empty, auto-generated as "{station_name} - Collector" using name from server
    collector_name: str = ""

    # Server connection
    server_url: str
    api_key: SecretStr

    # TLS settings
    tls: TLSConfig = Field(default_factory=TLSConfig)

    # Environment mode (affects security defaults)
    environment: str = "production"

    # Plugins (loaded from config file)
    plugins: list[PluginConfig] = Field(default_factory=list)

    # Transport settings
    batch_size: int = 100
    flush_interval: int = 10
    compression: str = "lz4"

    # Concurrency settings
    max_concurrent_devices: int = 10  # Max devices polling simultaneously

    # Local storage
    buffer_db_path: Path = Path("./collector_buffer.db")
    config_cache_path: Path = Path("./config_cache.json")

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    # Syslog server settings
    syslog: SyslogConfig = Field(default_factory=SyslogConfig)

    @field_validator("compression")
    @classmethod
    def validate_compression(cls, v: str) -> str:
        if v not in ("none", "gzip", "lz4"):
            raise ValueError("compression must be none, gzip, or lz4")
        return v

    @field_validator("server_url")
    @classmethod
    def validate_server_url(cls, v: str) -> str:
        """Validate server URL format."""
        if not v.startswith(("http://", "https://")):
            raise ValueError("server_url must start with http:// or https://")
        return v.rstrip("/")

    @property
    def requires_https(self) -> bool:
        """Check if HTTPS is required based on environment."""
        return self.environment == "production"

    def validate_security(self) -> list[str]:
        """Validate security settings and return warnings.

        Returns:
            List of warning messages (empty if all checks pass)
        """
        warnings = []

        # Check HTTPS in production
        if self.requires_https and not self.server_url.startswith("https://"):
            warnings.append(
                "SECURITY WARNING: Using HTTP in production. "
                "Set server_url to https:// or set environment=development"
            )

        # Check certificate verification
        if not self.tls.verify_ssl:
            warnings.append(
                "SECURITY WARNING: SSL certificate verification is disabled. "
                "This exposes you to man-in-the-middle attacks."
            )

        return warnings

    @classmethod
    def from_file(cls, config_path: Path) -> "CollectorConfig":
        """Load configuration from a YAML or JSON file."""
        import json

        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        content = config_path.read_text()

        if config_path.suffix in (".yaml", ".yml"):
            try:
                import yaml

                data = yaml.safe_load(content)
            except ImportError:
                raise ImportError("PyYAML required for YAML config files")
        else:
            data = json.loads(content)

        return cls(**data)
