# Station Master Collector - Standalone Docker Image
# https://github.com/yourusername/station-master-collector

FROM python:3.11-slim AS base

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system dependencies for SNMP
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsnmp-dev \
    && rm -rf /var/lib/apt/lists/*

# Build stage
FROM base AS builder

RUN pip install --upgrade pip build

# Copy project files
COPY collector/ ./collector/
COPY pyproject.toml README.md ./

# Install dependencies
RUN pip install --target=/app/deps .

# Production stage
FROM base AS production

# Copy installed packages
COPY --from=builder /app/deps /app/deps
COPY --from=builder /app/collector /app/collector

# Add deps to Python path
ENV PYTHONPATH=/app/deps:/app

# Create non-root user
RUN useradd --create-home --uid 1000 collector && \
    mkdir -p /config /app/buffer && \
    chown -R collector:collector /app /config

USER collector

# Volume for local buffer storage (offline resilience)
VOLUME ["/app/buffer"]

# Expose syslog and SNMP trap ports
EXPOSE 514/udp 1514/tcp 162/udp

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import collector" || exit 1

# Entrypoint
ENTRYPOINT ["python", "-m", "collector.main"]
CMD ["run", "--database", "--config", "/config/config.yaml"]
