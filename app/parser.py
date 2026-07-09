"""
Log Parser: Converting Raw Logs to Structured Data

This module handles the FIRST STAGE of the pipeline: parsing raw log lines
into structured LogEntry objects.

Input Format:
    A raw log line like this:
    "2026-06-17T10:15:06.100Z [mongodb] ERROR Connection pool exhausted..."

Output Format:
    A LogEntry object with:
    - timestamp: datetime
    - service: "mongodb"
    - level: "ERROR"
    - message: "Connection pool exhausted..."

The regex pattern expects ISO 8601 timestamps, bracketed service names, severity levels,
and a message body. It's flexible enough to handle variations in the message content.

Typical Usage:
    >>> entry = parse_line("2026-06-17T10:15:06.100Z [mongodb] ERROR Pool exhausted")
    >>> entry.service
    'mongodb'
    >>> entry.timestamp
    datetime.datetime(2026, 6, 17, 10, 15, 6, 100000)
"""

import re
from datetime import datetime
from app.models import LogEntry

# The regex pattern that matches our log format.
# Format: TIMESTAMP [SERVICE] LEVEL MESSAGE
# Breaking it down:
#   (?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)
#       → ISO 8601 timestamp with milliseconds, e.g., "2026-06-17T10:15:06.100Z"
#   \[(?P<service>[\w\-]+)\]
#       → Bracketed service name (alphanumeric + hyphens), e.g., "[mongodb]" or "[order-service]"
#   (?P<level>INFO|WARN|ERROR|DEBUG|CRITICAL)
#       → One of these severity levels
#   (?P<message>.+)$
#       → Everything else until end of line is the message
LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\s+"
    r"\[(?P<service>[\w\-]+)\]\s+"
    r"(?P<level>INFO|WARN|ERROR|DEBUG|CRITICAL)\s+"
    r"(?P<message>.+)$"
)

# Timestamp format string for parsing ISO 8601 with milliseconds
# %Y-%m-%d: Year-Month-Day
# %H:%M:%S: Hour:Minute:Second
# %f: Microseconds (we provide milliseconds which convert to microseconds)
# Z: UTC timezone indicator (we ignore with explicit format)
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def parse_line(line: str) -> LogEntry | None:
    """
    Parse a single log line into a LogEntry object.
    
    This function attempts to parse one line of raw log text. If the line
    matches our expected format, we extract and structure the data.
    If it doesn't match (e.g., malformed, extra whitespace), we return None.
    
    Args:
        line: A single log line as a string
    
    Returns:
        LogEntry object if parsing succeeds, None if the line doesn't match our pattern
    
    Example:
        >>> entry = parse_line("2026-06-17T10:15:06.100Z [mongodb] ERROR Pool exhausted")
        >>> entry is not None
        True
        >>> entry.service
        'mongodb'
        >>> entry.level
        'ERROR'
    
    Note:
        We strip whitespace and skip empty lines without error.
        The regex pattern is rigid (ISO 8601 format required), but the message
        body accepts any characters.
    """
    # Clean up the line first (remove leading/trailing whitespace)
    line = line.strip()
    
    # Skip empty lines silently
    if not line:
        return None
    
    # Attempt to match the pattern
    match = LOG_PATTERN.match(line)
    if not match:
        # Line doesn't match our expected format - skip it
        return None
    
    # Extract matched groups from the regex
    timestamp_str = match.group("timestamp")
    service_name = match.group("service")
    severity_level = match.group("level")
    message_content = match.group("message")
    
    # Parse the timestamp string into a datetime object
    # Note: We parse as UTC (the Z in the format indicates UTC)
    parsed_timestamp = datetime.strptime(timestamp_str, TS_FORMAT)
    
    # Create and return the LogEntry object
    return LogEntry(
        timestamp=parsed_timestamp,
        service=service_name,
        level=severity_level,
        message=message_content,
        raw=line,  # Keep the original line for debugging/tracing
    )


def parse_lines(lines: list[str]) -> list[LogEntry]:
    """
    Parse multiple log lines into LogEntry objects.
    
    This function is useful when you have a batch of log lines (e.g., from
    a file or API request). It processes each line, skips unparseable ones,
    and returns only the successfully parsed LogEntry objects.
    
    Args:
        lines: List of raw log line strings
    
    Returns:
        List of successfully parsed LogEntry objects, sorted by timestamp
    
    Example:
        >>> lines = [
        ...     "2026-06-17T10:15:00.100Z [api] INFO Request received",
        ...     "2026-06-17T10:15:01.200Z [db] ERROR Connection timeout",
        ... ]
        >>> entries = parse_lines(lines)
        >>> len(entries)
        2
        >>> entries[0].service
        'api'
        >>> entries[1].service
        'db'
    
    Performance Note:
        This function is O(n) where n is the number of lines.
        It sorts by timestamp (O(n log n)), which helps with later processing.
    """
    entries = []
    
    # Process each line
    for line in lines:
        entry = parse_line(line)
        if entry:
            # Only keep successfully parsed entries
            entries.append(entry)
    
    # Sort by timestamp so events are in chronological order
    # This makes it much easier to reason about incident sequences
    entries.sort(key=lambda e: e.timestamp)
    
    return entries


def parse_file(file_path: str) -> list[LogEntry]:
    """
    Parse all log lines from a file.
    
    This is a convenience function for reading logs from disk. It opens
    the file, reads all lines, and passes them to parse_lines().
    
    Args:
        file_path: Path to the log file (local or relative path)
    
    Returns:
        List of successfully parsed LogEntry objects, sorted by timestamp
    
    Raises:
        FileNotFoundError: If the file doesn't exist
        IOError: If there's a problem reading the file
    
    Example:
        >>> entries = parse_file("data/sample_logs.log")
        >>> print(f"Parsed {len(entries)} log entries")
        Parsed 38 log entries
    
    Note:
        Each line in the file is processed independently. If a line doesn't
        match our expected format, it's silently skipped.
    """
    with open(file_path, "r") as f:
        # Read all lines from the file (includes newlines)
        raw_lines = f.readlines()
    
    # Parse all lines using the batch function
    return parse_lines(raw_lines)
