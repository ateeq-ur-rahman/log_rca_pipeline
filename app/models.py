"""
Core Data Models for the Log Error Detection Pipeline

This module defines the data structures that flow through each stage of the pipeline.
Think of these as checkpoints where raw logs gradually become actionable insights.

📊 Data Flow:
    Raw Log String
        ↓
    LogEntry (parsed)
        ↓
    ErrorEvent (classified)
        ↓
    Incident (clustered)
        ↓
    RCAReport (analyzed & scored)
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any


@dataclass
class LogEntry:
    """
    Represents a single parsed log line from a raw log file.
    
    This is the output of the Parser stage. We extract timestamp, service name,
    severity level, and message from a raw log string like:
        "2026-06-17T10:15:06.100Z [mongodb] ERROR Connection pool exhausted"
    
    Attributes:
        timestamp: When the event occurred (ISO 8601 format)
        service: Which service/component generated this log (e.g., "mongodb", "order-service")
        level: Severity level - INFO, WARN, ERROR, DEBUG, CRITICAL
        message: The actual log message content (what happened)
        raw: The original unparsed log line (for debugging)
    
    Example:
        >>> entry = LogEntry(
        ...     timestamp=datetime(2026, 6, 17, 10, 15, 6, 100000),
        ...     service="mongodb",
        ...     level="ERROR",
        ...     message="Connection pool exhausted",
        ...     raw="2026-06-17T10:15:06.100Z [mongodb] ERROR Connection pool exhausted"
        ... )
    """
    timestamp: datetime
    service: str
    level: str
    message: str
    raw: str

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary for JSON serialization.
        
        Returns:
            Dict with timestamp, service, level, and message fields
        """
        return {
            "timestamp": self.timestamp.isoformat(),
            "service": self.service,
            "level": self.level,
            "message": self.message,
        }


@dataclass
class ErrorEvent:
    """
    A LogEntry that the Classifier has identified as an ERROR or WARNING.
    
    The Classifier doesn't just pass through all errors - it categorizes them:
    - Is this a ROOT CAUSE (like DB_POOL_EXHAUSTED) or a SYMPTOM (like SERVICE_UNAVAILABLE)?
    - How severe is it?
    - What pattern does it match?
    
    This classification is CRUCIAL for the RCA engine to later distinguish between:
    ✓ "The thing that broke" (root cause)
    ✗ "The things that broke because of it" (symptoms)
    
    Attributes:
        entry: The original LogEntry object
        category: Standardized error type (DB_POOL_EXHAUSTED, SERVICE_UNAVAILABLE, etc.)
        is_root_cause_candidate: True if this error marks the start of a problem
        severity: LOW (noise), MEDIUM (warning), HIGH (error), CRITICAL (disaster)
    
    Example:
        >>> error = ErrorEvent(
        ...     entry=log_entry,
        ...     category="DB_POOL_EXHAUSTED",
        ...     is_root_cause_candidate=True,
        ...     severity="CRITICAL"
        ... )
    """
    entry: LogEntry
    category: str
    is_root_cause_candidate: bool
    severity: str

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary, merging entry info with classification details.
        
        Returns:
            Dict containing all LogEntry fields plus category, severity, is_root_cause_candidate
        """
        data = self.entry.to_dict()
        data.update({
            "category": self.category,
            "severity": self.severity,
            "is_root_cause_candidate": self.is_root_cause_candidate,
        })
        return data


@dataclass
class Incident:
    """
    A cluster of ErrorEvents that occurred together and are causally related.
    
    The Clustering stage groups errors that meet BOTH conditions:
    1. Occurred within an 8-second time window, AND
    2. Involve services that depend on each other (via dependency graph)
    
    Example: MongoDB fails at T=6.1s → order-service fails at T=6.6s
    These are in the SAME incident because order-service depends on MongoDB.
    
    Counter-example: auth-service fails at T=2.0s (unrelated)
    This stays isolated as its own incident because auth-service has no dependencies.
    
    Attributes:
        incident_id: Unique identifier (e.g., "INC-001")
        events: List of ErrorEvent objects in this incident
    
    Properties:
        services_affected: Which services were impacted?
        start_time: When did this incident begin?
        end_time: When did it end?
    """
    incident_id: str
    events: List[ErrorEvent] = field(default_factory=list)

    @property
    def services_affected(self) -> List[str]:
        """
        Return sorted list of unique services involved in this incident.
        
        Returns:
            Sorted list of service names (e.g., ["api-gateway", "mongodb", "order-service"])
        """
        return sorted({event.entry.service for event in self.events})

    @property
    def start_time(self) -> datetime:
        """
        When did this incident begin (the earliest event)?
        
        Returns:
            Datetime of the first error in this incident
        """
        return min(event.entry.timestamp for event in self.events)

    @property
    def end_time(self) -> datetime:
        """
        When did this incident end (the latest event)?
        
        Returns:
            Datetime of the last error in this incident
        """
        return max(event.entry.timestamp for event in self.events)


@dataclass
class RCAReport:
    """
    The final output of the RCA Engine - a root cause analysis report.
    
    This is what gets delivered to on-call engineers. It answers the key questions:
    
    ❓ WHAT BROKE?
        → root_cause: The ErrorEvent that started everything
    
    ❓ HOW CONFIDENT ARE WE?
        → confidence (0.0-1.0): Based on score gap between root cause and runner-up
    
    ❓ WHAT BROKE BECAUSE OF IT?
        → affected_services: Full list of impacted services
        → symptom_chain: Downstream events, ranked by impact
    
    ❓ HOW LONG DID IT LAST?
        → duration_seconds: From first error to last error
    
    ❓ WHAT SHOULD WE DO?
        → recommended_action: Specific remediation steps
    
    🧮 Scoring Algorithm:
        The RCA Engine scores each event using three weighted factors:
        1. Is it a known root-cause type? (45% weight)
           - Examples: DB_POOL_EXHAUSTED, OOM_KILL, DISK_FULL
        2. How many other services depend on it? (35% weight)
           - E.g., MongoDB failure impacts 4 downstream services
        3. How early did it occur in the incident? (20% weight)
           - Earlier errors score higher (more likely to be root cause)
        
        SCORE = (0.45 × category_flag) + (0.35 × downstream) + (0.20 × timing)
    
    Attributes:
        incident_id: Reference to the incident being analyzed (e.g., "INC-001")
        root_cause: The ErrorEvent determined to be the root cause (or None)
        confidence: How confident are we? (0.5-1.0)
        affected_services: List of all services impacted
        symptom_chain: List of downstream ErrorEvent objects, ranked by impact
        recommended_action: What should ops do to fix it?
        duration_seconds: How long the incident lasted (end_time - start_time)
    
    Example Output:
        >>> report.root_cause.entry.service
        'mongodb'
        >>> report.root_cause.category
        'DB_POOL_EXHAUSTED'
        >>> report.confidence
        0.98
        >>> report.affected_services
        ['api-gateway', 'inventory-service', 'mongodb', 'order-service', 'payment-service']
    """
    incident_id: str
    root_cause: Optional[ErrorEvent]
    confidence: float
    affected_services: List[str]
    symptom_chain: List[ErrorEvent]
    recommended_action: str
    duration_seconds: float

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary for JSON APIs and storage.
        
        Flattens ErrorEvent objects to dicts and rounds numeric fields for readability.
        
        Returns:
            Dict representation suitable for JSON serialization and APIs
        """
        return {
            "incident_id": self.incident_id,
            "root_cause": self.root_cause.to_dict() if self.root_cause else None,
            "confidence": round(self.confidence, 2),
            "affected_services": self.affected_services,
            "symptom_chain": [event.to_dict() for event in self.symptom_chain],
            "recommended_action": self.recommended_action,
            "duration_seconds": round(self.duration_seconds, 2),
        }
