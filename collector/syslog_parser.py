"""Syslog message parser for RFC 3164 (BSD) and RFC 5424 (IETF) formats."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# RFC 5424 severity levels
SEVERITY_NAMES = [
    "emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"
]

# RFC 5424 facility codes
FACILITY_NAMES = [
    "kern", "user", "mail", "daemon", "auth", "syslog", "lpr", "news",
    "uucp", "cron", "authpriv", "ftp", "ntp", "audit", "alert", "clock",
    "local0", "local1", "local2", "local3", "local4", "local5", "local6", "local7"
]

# RFC 3164 (BSD) pattern: <PRI>TIMESTAMP HOSTNAME TAG: MESSAGE
RFC3164_PATTERN = re.compile(
    r"^<(\d{1,3})>"  # Priority
    r"(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"  # Timestamp (Mmm dd HH:MM:SS)
    r"(\S+)\s+"  # Hostname
    r"(\S+?)(?:\[(\d+)\])?:\s*"  # Tag and optional PID
    r"(.*)$",  # Message
    re.DOTALL
)

# RFC 5424 (IETF) pattern: <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID STRUCTURED-DATA MSG
RFC5424_PATTERN = re.compile(
    r"^<(\d{1,3})>"  # Priority
    r"(\d+)\s+"  # Version
    r"(\S+)\s+"  # Timestamp (ISO 8601)
    r"(\S+)\s+"  # Hostname
    r"(\S+)\s+"  # App-name
    r"(\S+)\s+"  # Proc-id
    r"(\S+)\s+"  # Msg-id
    r"(\[.*?\]|-)\s*"  # Structured data
    r"(.*)$",  # Message
    re.DOTALL
)


@dataclass
class SyslogMessage:
    """Parsed syslog message."""

    timestamp: datetime
    facility: int
    severity: int
    hostname: Optional[str]
    app_name: Optional[str]
    proc_id: Optional[str]
    message: str
    raw_message: str

    @property
    def facility_name(self) -> str:
        """Get facility name."""
        if 0 <= self.facility < len(FACILITY_NAMES):
            return FACILITY_NAMES[self.facility]
        return f"facility{self.facility}"

    @property
    def severity_name(self) -> str:
        """Get severity name."""
        if 0 <= self.severity < len(SEVERITY_NAMES):
            return SEVERITY_NAMES[self.severity]
        return f"severity{self.severity}"


def parse_priority(pri: int) -> tuple[int, int]:
    """Parse priority value into facility and severity.

    Args:
        pri: Priority value (0-191)

    Returns:
        Tuple of (facility, severity)
    """
    facility = pri >> 3
    severity = pri & 0x07
    return facility, severity


def parse_rfc3164_timestamp(timestamp_str: str) -> datetime:
    """Parse RFC 3164 timestamp (e.g., 'Dec  5 12:34:56').

    Args:
        timestamp_str: Timestamp string

    Returns:
        Parsed datetime (with current year assumed)
    """
    # Add current year since RFC 3164 doesn't include it
    current_year = datetime.now().year
    # Handle single-digit day with extra space
    timestamp_str = re.sub(r'\s+', ' ', timestamp_str.strip())
    try:
        dt = datetime.strptime(f"{current_year} {timestamp_str}", "%Y %b %d %H:%M:%S")
        # Handle year rollover (message from December when it's January)
        if dt.month == 12 and datetime.now().month == 1:
            dt = dt.replace(year=current_year - 1)
        return dt
    except ValueError:
        return datetime.now()


def parse_rfc5424_timestamp(timestamp_str: str) -> datetime:
    """Parse RFC 5424 timestamp (ISO 8601 format).

    Args:
        timestamp_str: Timestamp string

    Returns:
        Parsed datetime
    """
    if timestamp_str == "-":
        return datetime.now()

    # Handle various ISO 8601 formats
    formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
    ]

    # Remove timezone offset for parsing
    ts = timestamp_str
    for fmt in formats:
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue

    # Fallback
    return datetime.now()


def parse_syslog_message(raw: bytes, source_ip: str = "unknown") -> Optional[SyslogMessage]:
    """Parse a syslog message in RFC 3164 or RFC 5424 format.

    Args:
        raw: Raw message bytes
        source_ip: Source IP address of the message

    Returns:
        Parsed SyslogMessage or None if parsing failed
    """
    try:
        # Decode message
        try:
            message = raw.decode("utf-8")
        except UnicodeDecodeError:
            message = raw.decode("latin-1")

        message = message.strip()
        raw_message = message

        # Try RFC 5424 first (has version number after PRI)
        match = RFC5424_PATTERN.match(message)
        if match:
            pri = int(match.group(1))
            facility, severity = parse_priority(pri)
            timestamp = parse_rfc5424_timestamp(match.group(3))
            hostname = match.group(4) if match.group(4) != "-" else None
            app_name = match.group(5) if match.group(5) != "-" else None
            proc_id = match.group(6) if match.group(6) != "-" else None
            msg = match.group(9).strip()

            return SyslogMessage(
                timestamp=timestamp,
                facility=facility,
                severity=severity,
                hostname=hostname,
                app_name=app_name,
                proc_id=proc_id,
                message=msg,
                raw_message=raw_message,
            )

        # Try RFC 3164
        match = RFC3164_PATTERN.match(message)
        if match:
            pri = int(match.group(1))
            facility, severity = parse_priority(pri)
            timestamp = parse_rfc3164_timestamp(match.group(2))
            hostname = match.group(3)
            app_name = match.group(4)
            proc_id = match.group(5)  # May be None
            msg = match.group(6).strip()

            return SyslogMessage(
                timestamp=timestamp,
                facility=facility,
                severity=severity,
                hostname=hostname,
                app_name=app_name,
                proc_id=proc_id,
                message=msg,
                raw_message=raw_message,
            )

        # Fallback: try to extract just the priority
        pri_match = re.match(r"^<(\d{1,3})>(.*)$", message, re.DOTALL)
        if pri_match:
            pri = int(pri_match.group(1))
            facility, severity = parse_priority(pri)
            msg = pri_match.group(2).strip()

            return SyslogMessage(
                timestamp=datetime.now(),
                facility=facility,
                severity=severity,
                hostname=None,
                app_name=None,
                proc_id=None,
                message=msg,
                raw_message=raw_message,
            )

        # Last resort: treat as plain message with default facility/severity
        return SyslogMessage(
            timestamp=datetime.now(),
            facility=1,  # user
            severity=6,  # info
            hostname=None,
            app_name=None,
            proc_id=None,
            message=message,
            raw_message=raw_message,
        )

    except Exception:
        return None
