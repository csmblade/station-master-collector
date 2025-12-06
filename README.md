# Station Master Collector

Standalone data collection agent for the Station Master cloud platform. Collects SNMP metrics, syslog events, and SNMP traps from broadcast equipment and transmits them to the Station Master server.

## Features

- **SNMP Polling** - Collect metrics from network devices via SNMP v1/v2c/v3
- **Syslog Server** - Receive syslog messages via UDP (514) and TCP (1514)
- **SNMP Trap Receiver** - Receive SNMP traps on UDP 162
- **Offline Resilience** - Local SQLite buffer for network outages
- **LZ4 Compression** - Efficient metric transmission
- **Plugin Architecture** - Extensible device support
- **High Availability** - Primary/backup collector support
- **Health Monitoring** - Watchdog, timeouts, and health checks

## Quick Start

```bash
docker pull csmblade/station-master-collector:latest

docker run -d \
  --name collector \
  -e STATION_MASTER_URL=https://your-server.com \
  -e STATION_MASTER_API_KEY=your-api-key \
  -e STATION_ID=your-station-uuid \
  -v collector-config:/config \
  -v collector-buffer:/app/buffer \
  -p 514:514/udp \
  -p 1514:1514/tcp \
  -p 162:162/udp \
  csmblade/station-master-collector:latest
```

## Configuration

Create a `config.yaml` and mount it to `/config/config.yaml`:

```yaml
server:
  url: https://your-server.com
  api_key: your-api-key
  station_id: your-station-uuid

collector:
  heartbeat_interval: 30
  buffer_flush_interval: 10

syslog:
  enabled: true
  udp_port: 514
  tcp_port: 1514

snmp_traps:
  enabled: true
  port: 162
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `STATION_MASTER_URL` | Server URL | Required |
| `STATION_MASTER_API_KEY` | API key for authentication | Required |
| `STATION_ID` | Station UUID | Required |
| `LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING, ERROR) | INFO |

## Health Checks

The container includes a health check that verifies:
- The collector process is running and responsive
- Activity has occurred within the last 120 seconds
- No critical errors have been flagged

```bash
docker inspect --format='{{.State.Health.Status}}' collector
```

## Supported Devices

- Nautel transmitters (SNMP)
- Axia audio routers (HTTP API)
- Omnia audio processors (SNMP/HTTP)
- Zetta broadcast systems (HTTP API)
- Generic SNMP devices

## License

Proprietary - All rights reserved
