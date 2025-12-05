# Station Master Collector

Endpoint data collection agent for the Station Master observability platform. Deploys to client sites to collect metrics, syslog events, and SNMP traps from network infrastructure.

## Features

- **SNMP Polling** - Collect metrics from network devices via SNMP v1/v2c/v3
- **Syslog Server** - Receive syslog messages over UDP (514) and TCP (1514)
- **SNMP Trap Receiver** - Convert SNMP traps to normalized events (port 162)
- **Offline Resilience** - Local buffer for data during network outages
- **Secure Transport** - TLS encryption with LZ4 compression to central server
- **Plugin Architecture** - Extensible for custom device types

## Quick Start

### 1. Prerequisites

- Docker and Docker Compose installed
- Station ID and API key from Station Master dashboard

### 2. Deploy with Docker Compose

```bash
# Clone this repository
git clone https://github.com/csmblade/station-master-collector.git
cd station-master-collector

# Copy and configure
cp config.example.yaml config.yaml

# Edit config.yaml with your station details:
# - station_id: Get from Station Master dashboard
# - station_name: Friendly name for this station
# - server_url: Your Station Master server URL
# - api_key: API key from dashboard

# Start the collector
docker-compose up -d

# Check logs
docker-compose logs -f
```

### 3. Alternative: Docker Run

```bash
docker run -d \
  --name station_collector \
  --restart unless-stopped \
  -v $(pwd)/config.yaml:/config/config.yaml:ro \
  -v collector_buffer:/app/buffer \
  -p 514:514/udp \
  -p 1514:1514/tcp \
  -p 162:162/udp \
  csmblade/station-master-collector:latest
```

## Configuration

### Minimal Configuration

```yaml
station_id: "your-station-uuid"
station_name: "My Station"
server_url: "https://your-domain.com"
api_key: "your-api-key"

syslog:
  enabled: true
  snmp_trap_enabled: true
```

### Full Configuration Options

| Setting | Default | Description |
|---------|---------|-------------|
| `station_id` | required | UUID from Station Master dashboard |
| `station_name` | "" | Human-readable station name |
| `server_url` | required | Central server URL (https://...) |
| `api_key` | required | API key for authentication |
| `batch_size` | 100 | Metrics per batch |
| `flush_interval` | 10 | Seconds between flushes |
| `compression` | "lz4" | Compression: none, gzip, lz4 |
| `max_concurrent_devices` | 10 | Parallel device polling |

### Syslog Configuration

```yaml
syslog:
  enabled: true
  udp_port: 514
  tcp_port: 1514
  bind_address: "0.0.0.0"
  batch_size: 100
  flush_interval: 5
  snmp_trap_enabled: true
  snmp_trap_port: 162
  snmp_trap_community: "public"
```

## Port Requirements

| Port | Protocol | Service | Privilege Required |
|------|----------|---------|-------------------|
| 514 | UDP | Syslog | Yes (< 1024) |
| 1514 | TCP | Syslog | No |
| 162 | UDP | SNMP Traps | Yes (< 1024) |

**Note**: Ports below 1024 require root/elevated privileges. The Docker container handles this automatically.

## Network Device Configuration

### Syslog

Configure your network devices to send syslog to the collector's IP address:

```
# Cisco IOS
logging host <collector-ip>
logging trap informational

# Juniper
set system syslog host <collector-ip> any info
```

### SNMP Traps

Configure trap destinations:

```
# Cisco IOS
snmp-server host <collector-ip> traps version 2c public

# Juniper
set snmp trap-group public targets <collector-ip>
```

## Troubleshooting

### Check Container Status

```bash
docker-compose ps
docker-compose logs collector
```

### Verify Connectivity

```bash
# Test syslog
echo "<14>Test message" | nc -u -w1 localhost 514

# Check ports
docker-compose exec collector netstat -tuln
```

### Common Issues

1. **Port already in use**: Another service using 514/162
   - Solution: Use alternative ports or stop conflicting service

2. **Permission denied on ports**: Non-root container
   - Solution: Use Docker's port mapping (handled automatically)

3. **Connection refused to server**: Network/firewall issue
   - Solution: Check server_url and firewall rules

## Development

### Build Locally

```bash
docker build -t station-master-collector:local .
```

### Run Tests

```bash
pip install -e ".[dev]"
pytest
```

## License

Apache License 2.0 - see LICENSE file for details.

## Support

- Documentation: https://your-docs-site.com
- Issues: https://github.com/csmblade/station-master-collector/issues
