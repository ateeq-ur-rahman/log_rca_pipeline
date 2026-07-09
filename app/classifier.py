"""
Error Classifier: Categorizing Parsed Logs

This module handles the SECOND STAGE of the pipeline: taking parsed LogEntry
objects and classifying them into error categories.

Key Concept:
    Not all errors are created equal. Some errors are SYMPTOMS of deeper problems:
    - "DB connection timeout" is usually a SYMPTOM of a resource exhaustion
    - "Service unavailable" is a SYMPTOM of downstream failures
    
    Other errors are ROOT CAUSES - the actual problems:
    - "Connection pool exhausted" is a ROOT CAUSE (resource problem)
    - "Disk full" is a ROOT CAUSE (storage problem)
    - "Out of memory" is a ROOT CAUSE (memory problem)

The Classifier uses pattern matching to:
1. Extract error category from the log message
2. Determine if this error is typically a root cause or a symptom
3. Assign a severity level

Typical Usage:
    >>> entry = LogEntry(..., message="Connection pool exhausted: 0 connections available")
    >>> error = classify(entry)
    >>> error.category
    'DB_POOL_EXHAUSTED'
    >>> error.is_root_cause_candidate
    True
    >>> error.severity
    'CRITICAL'
"""

from app.models import LogEntry, ErrorEvent


# ============================================================================
# PATTERN RULES: How to categorize different error messages
# ============================================================================
# Each rule has:
#   - Pattern: What to look for in the log message (as a string, not regex)
#   - Category: The standardized error type name
#   - IsRootCause: Is this error the START of a problem? (True = root cause)
#   - Severity: How bad is it? (LOW, MEDIUM, HIGH, CRITICAL)
# ============================================================================

CLASSIFICATION_RULES = [
    # 🔴 Resource Exhaustion Errors (These are ROOT CAUSES)
    {
        "pattern": "pool exhausted",
        "category": "DB_POOL_EXHAUSTED",
        "is_root_cause": True,
        "severity": "CRITICAL",
        "description": "Database connection pool has no available connections",
    },
    {
        "pattern": "pool utilization at",
        "category": "DB_POOL_PRESSURE",
        "is_root_cause": True,
        "severity": "MEDIUM",
        "description": "Database pool getting full (warning before exhaustion)",
    },
    {
        "pattern": "out of memory",
        "category": "OOM_KILL",
        "is_root_cause": True,
        "severity": "CRITICAL",
        "description": "Process or system has exhausted available memory",
    },
    {
        "pattern": "disk full",
        "category": "DISK_FULL",
        "is_root_cause": True,
        "severity": "CRITICAL",
        "description": "No more disk space available",
    },
    
    # 🟠 Connection Issues (These are usually SYMPTOMS of root causes)
    {
        "pattern": "connection timeout",
        "category": "DB_CONNECTION_TIMEOUT",
        "is_root_cause": False,
        "severity": "HIGH",
        "description": "Couldn't connect to database in time (symptom of DB being slow/unavailable)",
    },
    {
        "pattern": "timeout",
        "category": "TIMEOUT",
        "is_root_cause": False,
        "severity": "HIGH",
        "description": "Operation took too long to complete",
    },
    
    # 🟡 Application Errors (SYMPTOMS of upstream issues)
    {
        "pattern": "503",
        "category": "SERVICE_UNAVAILABLE",
        "is_root_cause": False,
        "severity": "HIGH",
        "description": "Service returned 503 - temporarily unavailable",
    },
    {
        "pattern": "service unavailable",
        "category": "SERVICE_UNAVAILABLE",
        "is_root_cause": False,
        "severity": "HIGH",
        "description": "A dependent service is down or unreachable",
    },
    
    # ⚫ Protective Mechanisms (SYMPTOMS that show the system is recovering)
    {
        "pattern": "circuit breaker",
        "category": "CIRCUIT_BREAKER_OPEN",
        "is_root_cause": False,
        "severity": "MEDIUM",
        "description": "Circuit breaker opened - service protecting itself",
    },
    
    # ⚪ Noise / Expected Errors (Usually not part of incidents)
    {
        "pattern": "invalid jwt",
        "category": "AUTH_ERROR",
        "is_root_cause": False,
        "severity": "LOW",
        "description": "Client sent invalid authentication token (usually isolated)",
    },
    {
        "pattern": "rate limit",
        "category": "RATE_LIMIT",
        "is_root_cause": False,
        "severity": "LOW",
        "description": "Client exceeded rate limit (expected, not an incident)",
    },
    {
        "pattern": "recovered",
        "category": "RECOVERY",
        "is_root_cause": False,
        "severity": "LOW",
        "description": "System has recovered from a previous failure",
    },
]


def classify_log_entry(entry: LogEntry) -> ErrorEvent | None:
    """
    Classify a single LogEntry into an ErrorEvent.
    
    This function only processes ERROR and WARN level logs.
    INFO, DEBUG, and other levels return None (not relevant to incidents).
    
    Classification Process:
    1. Check if log level is ERROR or WARN
    2. Convert message to lowercase (for case-insensitive matching)
    3. Iterate through classification rules
    4. Return the FIRST rule that matches (rules are ordered by priority)
    
    Args:
        entry: A LogEntry object to classify
    
    Returns:
        ErrorEvent object if the entry matches a rule, None otherwise
    
    Example:
        >>> entry = LogEntry(..., level="ERROR", message="Connection pool exhausted")
        >>> error = classify_log_entry(entry)
        >>> error.category
        'DB_POOL_EXHAUSTED'
        >>> error.is_root_cause_candidate
        True
    
    Note:
        If multiple rules could match (e.g., both "timeout" and "connection timeout"),
        we return the FIRST match. So rule order matters! More specific patterns
        should come before general patterns.
    """
    # Only classify ERROR and WARN level entries
    if entry.level not in ("ERROR", "WARN"):
        return None
    
    # Convert message to lowercase for case-insensitive pattern matching
    message_lower = entry.message.lower()
    
    # Try each rule in order
    for rule in CLASSIFICATION_RULES:
        pattern = rule["pattern"].lower()
        
        # Simple substring matching (case-insensitive)
        if pattern in message_lower:
            # Found a match! Create and return the ErrorEvent
            return ErrorEvent(
                entry=entry,
                category=rule["category"],
                is_root_cause_candidate=rule["is_root_cause"],
                severity=rule["severity"],
            )
    
    # No rules matched - return None (not an error we recognize)
    # This effectively filters out noisy or unexpected error types
    return None


def classify(entry: LogEntry) -> ErrorEvent | None:
    """
    Alias for classify_log_entry() — classify a single LogEntry.
    
    Kept for backward compatibility with callers (test_pipeline.py, main.py)
    that use the shorter name. See classify_log_entry() for full documentation.
    """
    return classify_log_entry(entry)


def classify_entries(entries: list[LogEntry]) -> list[ErrorEvent]:
    """
    Classify multiple LogEntry objects into ErrorEvent objects.
    
    This is the batch processing version - useful when you have all parsed
    logs and want to filter them to only the errors we care about.
    
    Args:
        entries: List of LogEntry objects (typically from the Parser stage)
    
    Returns:
        List of ErrorEvent objects (only entries that matched a rule)
    
    Example:
        >>> log_entries = parse_lines(raw_lines)
        >>> print(f"Parsed {len(log_entries)} total log lines")
        Parsed 38 total log lines
        >>> error_events = classify_entries(log_entries)
        >>> print(f"Classified {len(error_events)} errors")
        Classified 16 errors
    
    Performance:
        O(n × m) where n = number of entries, m = number of rules
        In practice: 38 entries × 12 rules ≈ 456 comparisons (very fast)
    """
    errors = []
    
    for entry in entries:
        error = classify_log_entry(entry)
        if error is not None:
            # Only keep entries that matched a classification rule
            errors.append(error)
    
    return errors


def get_rules_by_category(category: str) -> dict:
    """
    Lookup the rule details for a specific category.
    
    This is useful when you want to understand why an error was classified
    a certain way, or what the expected properties are.
    
    Args:
        category: The error category name (e.g., "DB_POOL_EXHAUSTED")
    
    Returns:
        Dict with rule details (pattern, is_root_cause, severity, description)
        or None if category not found
    
    Example:
        >>> rule = get_rules_by_category("DB_POOL_EXHAUSTED")
        >>> rule["description"]
        'Database connection pool has no available connections'
        >>> rule["is_root_cause"]
        True
    """
    for rule in CLASSIFICATION_RULES:
        if rule["category"] == category:
            return rule
    return None


def print_rule_summary():
    """
    Print a human-readable summary of all classification rules.
    
    Useful for debugging or documentation. Shows all patterns, categories,
    root cause flags, and descriptions.
    
    Example:
        >>> print_rule_summary()
        
        Classification Rules Summary
        ============================
        
        🔴 ROOT CAUSE errors (the actual problems):
        • pool exhausted → DB_POOL_EXHAUSTED (CRITICAL)
        • pool utilization at → DB_POOL_PRESSURE (MEDIUM)
        ...
    """
    print("\n" + "=" * 70)
    print("Classification Rules Summary")
    print("=" * 70)
    
    # Separate rules into root causes and symptoms
    root_causes = [r for r in CLASSIFICATION_RULES if r["is_root_cause"]]
    symptoms = [r for r in CLASSIFICATION_RULES if not r["is_root_cause"]]
    
    print("\n🔴 ROOT CAUSE errors (the actual problems):")
    print("-" * 70)
    for rule in root_causes:
        print(
            f"  • {rule['pattern']:30} → {rule['category']:25} ({rule['severity']})"
        )
        print(f"    {rule['description']}")
    
    print("\n⚪ SYMPTOM errors (results of root causes):")
    print("-" * 70)
    for rule in symptoms:
        print(
            f"  • {rule['pattern']:30} → {rule['category']:25} ({rule['severity']})"
        )
        print(f"    {rule['description']}")
    
    print("\n" + "=" * 70)
    print(f"Total rules: {len(CLASSIFICATION_RULES)} patterns")
    print("=" * 70 + "\n")
