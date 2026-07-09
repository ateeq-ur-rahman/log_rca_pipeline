"""
RCA Engine: Detecting Incidents and Root Causes

This module handles the LAST TWO STAGES of the pipeline:

STAGE 3 - CLUSTERING: Group related errors into incidents
    Input: 16 ErrorEvent objects scattered across 7.4 seconds
    Output: 2 Incident objects (noise filtered out)

STAGE 4 - RCA SCORING: Analyze each incident to find root cause
    Input: Incident with 13 related errors
    Output: RCAReport with root cause, symptom chain, and recommended action

🎯 Key Innovation: Graph-Aware Clustering
    Traditional clustering: If two errors happen within 8 seconds, group them.
    PROBLEM: This groups unrelated errors!
    
    Our approach:
    - Maintain a SERVICE DEPENDENCY GRAPH (what depends on what)
    - Only group errors from services that are connected in the graph
    - Result: Noise gets filtered out automatically!

📊 Example:
    Events at T=2.0s (auth error) and T=6.1s (MongoDB failure)
    Both within 8 seconds → Would group with traditional clustering ✗
    With our graph approach → Stay separate (auth-service is isolated) ✓

🧮 Multi-Factor RCA Scoring:
    SCORE = (0.45 × category_flag) + (0.35 × downstream_count) + (0.20 × timing)
    
    Three weighted factors:
    1. Category flag (45% weight)
       - Is this error type typically a root cause or symptom?
       - DB_POOL_EXHAUSTED = 1.0 (root cause)
       - SERVICE_UNAVAILABLE = 0.0 (symptom)
    
    2. Downstream impact (35% weight)
       - How many other services depend on this service?
       - If MongoDB fails and 4 services depend on it: downstream = 1.0
       - If order-service fails and 2 services depend: downstream = 0.5
    
    3. Timing / Earliness (20% weight)
       - Earlier errors score higher (more likely root cause)
       - Earliest error in incident: 1.0
       - Latest error in incident: 0.0
"""

from datetime import datetime, timedelta
from app.models import ErrorEvent, Incident, RCAReport


# ============================================================================
# SERVICE DEPENDENCY GRAPH
# ============================================================================
# This defines which services depend on which other services.
# If service A depends on service B, then a B failure affects A.
#
# Format: "service_name": ["list", "of", "dependencies"]
#
# Example:
#   "order-service" depends on "mongodb"
#   ↓ If mongodb fails, order-service will also fail
#   ↓ So they get grouped into the same incident
#   ↓ But auth-service doesn't depend on anything, so it stays separate
# ============================================================================

SERVICE_DEPENDENCIES = {
    # Core services
    "mongodb": [],  # No dependencies - root of the chain
    
    # Services that directly use MongoDB
    "order-service": ["mongodb"],
    "inventory-service": ["mongodb"],
    
    # Services that use order-service
    "api-gateway": ["order-service", "inventory-service", "payment-service"],
    "payment-service": ["order-service"],
    
    # Isolated services (no dependencies)
    "auth-service": [],
}


def get_service_dependencies(service_name: str) -> set:
    """
    Get all services that a given service depends on (direct + transitive).
    
    This builds the full dependency closure. For example:
    - order-service depends on mongodb (direct)
    - api-gateway depends on order-service (direct), which depends on mongodb (transitive)
    - So api-gateway's full closure = {order-service, mongodb}
    
    Args:
        service_name: Name of the service to get dependencies for
    
    Returns:
        Set of all services this service depends on (empty set if none or unknown)
    
    Example:
        >>> get_service_dependencies("api-gateway")
        {'order-service', 'inventory-service', 'payment-service', 'mongodb'}
    """
    if service_name not in SERVICE_DEPENDENCIES:
        # Unknown service - assume it has no dependencies
        return set()
    
    # Use BFS to find all transitive dependencies
    visited = set()
    to_visit = list(SERVICE_DEPENDENCIES.get(service_name, []))
    
    while to_visit:
        current = to_visit.pop(0)
        if current in visited:
            continue
        
        visited.add(current)
        # Add this service's dependencies to the queue
        to_visit.extend(SERVICE_DEPENDENCIES.get(current, []))
    
    return visited


def get_service_dependents(service_name: str) -> set:
    """
    Get all services that depend ON a given service (direct + transitive).
    
    This is the REVERSE of get_service_dependencies(). Where that function
    asks "what does X need to work?", this one asks "what breaks if X goes
    down?" — which is exactly the "downstream impact" / blast-radius question
    the RCA scoring formula needs.
    
    Example: mongodb has ZERO dependencies of its own (it's the root of the
    graph), but everything else transitively depends on it. So:
    - get_service_dependencies("mongodb") → {} (mongodb depends on nothing)
    - get_service_dependents("mongodb")   → {order-service, inventory-service,
                                               payment-service, api-gateway}
                                              (4 services break if mongodb dies)
    
    This distinction matters: a service near the ROOT of the dependency graph
    (like mongodb) should score HIGH on downstream impact because its failure
    cascades widely — not LOW just because it has no dependencies of its own.
    
    Args:
        service_name: Name of the service to find dependents for
    
    Returns:
        Set of all services that (directly or transitively) depend on this
        service (empty set if nothing depends on it, e.g. api-gateway)
    
    Example:
        >>> get_service_dependents("mongodb")
        {'order-service', 'inventory-service', 'payment-service', 'api-gateway'}
        >>> get_service_dependents("api-gateway")
        set()  # Nothing depends on api-gateway - it's a leaf consumer
    """
    dependents = set()
    
    # Check every known service: does it depend (directly or transitively)
    # on service_name? If so, it's a dependent.
    for candidate_service in SERVICE_DEPENDENCIES:
        if candidate_service == service_name:
            continue
        if service_name in get_service_dependencies(candidate_service):
            dependents.add(candidate_service)
    
    return dependents


def are_services_related(service_a: str, service_b: str) -> bool:
    """
    Check if two services are causally connected.
    
    Two services are "related" if:
    0. They are the same service (trivially related — e.g. two mongodb
       errors 2 seconds apart clearly belong to the same incident), OR
    1. Service A depends on Service B, OR
    2. Service B depends on Service A, OR
    3. They both depend on the same service
    
    This is crucial for deciding whether two errors should be in the same incident.
    
    Args:
        service_a: First service name
        service_b: Second service name
    
    Returns:
        True if the services are related (should cluster together)
    
    Example:
        >>> are_services_related("order-service", "mongodb")
        True
        >>> are_services_related("mongodb", "mongodb")
        True  # Same service - trivially related
        >>> are_services_related("auth-service", "mongodb")
        False  # Auth has no dependencies
    """
    # Same service is always related to itself. Without this, a service
    # with no dependencies (like "mongodb", which is the root of the graph)
    # would never be considered related to *another event on itself* — since
    # get_service_dependencies("mongodb") is empty, none of the checks below
    # would catch two consecutive mongodb errors as belonging together.
    if service_a == service_b:
        return True
    
    # Get each service's dependencies
    a_deps = get_service_dependencies(service_a)
    b_deps = get_service_dependencies(service_b)
    
    # Check if A depends on B
    if service_b in a_deps:
        return True
    
    # Check if B depends on A
    if service_a in b_deps:
        return True
    
    # Check if they share common dependencies
    if a_deps & b_deps:
        return True
    
    return False


# ============================================================================
# CLUSTERING: Group related errors into incidents
# ============================================================================

TIME_WINDOW_SECONDS = 8  # Max seconds between errors to consider them related


def cluster_errors(errors: list[ErrorEvent]) -> list[Incident]:
    """
    Group ErrorEvents into Incidents based on time and service relationships.
    
    Algorithm (Graph-Aware Clustering):
    1. Sort errors by timestamp
    2. For each error:
       a. Check if it fits in an existing incident:
          - Last event in incident is within TIME_WINDOW_SECONDS
          - Error's service is related to at least one service in the incident
       b. If yes: add to existing incident
       c. If no: create a new incident
    3. Return all incidents
    
    Args:
        errors: List of ErrorEvent objects (should be sorted by timestamp)
    
    Returns:
        List of Incident objects
    
    Example:
        >>> errors = [event1, event2, event3, ...]  # 16 events
        >>> incidents = cluster_errors(errors)
        >>> len(incidents)
        2
        >>> incidents[0].incident_id
        'INC-001'
    """
    # Sort errors by timestamp to process chronologically
    sorted_errors = sorted(errors, key=lambda e: e.entry.timestamp)
    
    incidents = []
    
    for error in sorted_errors:
        # Try to find an existing incident this error belongs to
        added_to_incident = False
        
        for incident in incidents:
            # Check if error fits in this incident
            if _error_fits_in_incident(error, incident):
                # Yes! Add it to this incident
                incident.events.append(error)
                added_to_incident = True
                break
        
        if not added_to_incident:
            # No existing incident matched - create a new one
            incident_id = f"INC-{str(len(incidents) + 1).zfill(3)}"
            new_incident = Incident(incident_id=incident_id, events=[error])
            incidents.append(new_incident)
    
    return incidents


def _error_fits_in_incident(error: ErrorEvent, incident: Incident) -> bool:
    """
    Determine if an error should be added to an existing incident.
    
    Two conditions must be met:
    1. Time is close (error happened within TIME_WINDOW_SECONDS of incident's last event)
    2. Services are related (error's service is causally connected to incident's services)
    
    Args:
        error: ErrorEvent to check
        incident: Incident to potentially add it to
    
    Returns:
        True if error should be added to this incident
    """
    if not incident.events:
        # Empty incident (shouldn't happen, but be safe)
        return False
    
    # Check time window: is the error within TIME_WINDOW_SECONDS of last event?
    last_event_time = incident.end_time
    time_gap = (error.entry.timestamp - last_event_time).total_seconds()
    
    if time_gap > TIME_WINDOW_SECONDS:
        # Too far in time - skip this incident
        return False
    
    # Check service relationship: is error's service related to incident's services?
    incident_services = incident.services_affected
    error_service = error.entry.service
    
    # Check if error's service is related to ANY service in the incident
    for incident_service in incident_services:
        if are_services_related(error_service, incident_service):
            # Found a relationship!
            return True
    
    # No relationship found
    return False


# ============================================================================
# RCA SCORING: Identify root cause and symptom chain
# ============================================================================

# Error categories that are typically ROOT CAUSES vs SYMPTOMS
# (These should match the classifier's rules)
ROOT_CAUSE_CATEGORIES = {
    "DB_POOL_EXHAUSTED",
    "DB_POOL_PRESSURE",
    "OOM_KILL",
    "DISK_FULL",
}

# Weight for the "root cause category" scoring factor, per category.
#
# Not every root-cause candidate deserves equal weight. DB_POOL_PRESSURE is
# an early WARNING that precedes a failure (e.g. "pool at 92% full") — it's
# useful context, but it isn't the actual failure. DB_POOL_EXHAUSTED,
# OOM_KILL, and DISK_FULL are the real failure events (CRITICAL severity).
#
# Without this distinction, a MEDIUM-severity warning that happens to occur
# a few seconds earlier than the actual CRITICAL failure would win purely on
# the "earliness" factor — which is wrong. The genuine failure should always
# outrank its own precursor warning.
ROOT_CAUSE_WEIGHTS = {
    "DB_POOL_EXHAUSTED": 1.0,   # Actual failure - full weight
    "OOM_KILL": 1.0,            # Actual failure - full weight
    "DISK_FULL": 1.0,           # Actual failure - full weight
    "DB_POOL_PRESSURE": 0.5,    # Precursor warning - partial weight
}

# Recommended actions for different categories
ACTION_RECOMMENDATIONS = {
    "DB_POOL_EXHAUSTED": "Scale DB connection pool size / add read replicas; review slow queries holding connections",
    "DB_POOL_PRESSURE": "Monitor pool usage; consider increasing pool size or optimizing query performance",
    "OOM_KILL": "Increase available memory / check for memory leaks; review process memory limits",
    "DISK_FULL": "Free up disk space immediately; implement log rotation / cleanup policies",
    "SERVICE_UNAVAILABLE": "Check downstream service status; restart if needed; review error logs on that service",
    "TIMEOUT": "Increase timeout thresholds / improve upstream service performance",
    "CIRCUIT_BREAKER_OPEN": "Wait for circuit to reset OR check upstream service status",
    "DEFAULT": "Investigate error patterns; check service logs and resource utilization",
}


def analyze_incident(incident: Incident) -> RCAReport:
    """
    Analyze an incident to determine root cause and generate a report.
    
    Scoring Algorithm:
    1. Score each event using multi-factor formula:
       SCORE = (0.45 × root_flag) + (0.35 × downstream) + (0.20 × earliness)
    
    2. Identify root cause (highest score)
    3. Identify symptoms (other events, ranked by score)
    4. Calculate confidence based on score gap
    5. Generate recommended action
    
    Args:
        incident: Incident object with events to analyze
    
    Returns:
        RCAReport with root cause, symptom chain, and recommendations
    
    Example:
        >>> report = analyze_incident(incident)
        >>> report.root_cause.category
        'DB_POOL_EXHAUSTED'
        >>> report.confidence
        0.98
    """
    if not incident.events:
        # Empty incident
        return RCAReport(
            incident_id=incident.incident_id,
            root_cause=None,
            confidence=0.0,
            affected_services=[],
            symptom_chain=[],
            recommended_action="No events in incident",
            duration_seconds=0.0,
        )
    
    # Score each event
    event_scores = []
    for event in incident.events:
        score = _score_event(event, incident)
        event_scores.append((event, score))
    
    # Sort by score (highest first)
    event_scores.sort(key=lambda x: x[1], reverse=True)
    
    # Identify root cause (highest score)
    root_event, root_score = event_scores[0]
    
    # Identify symptoms (remaining events)
    symptom_events = [e for e, _ in event_scores[1:]]
    
    # Calculate confidence
    if len(event_scores) > 1:
        runner_up_score = event_scores[1][1]
        # Confidence is the gap between root cause and next highest
        confidence = min(root_score - runner_up_score + 0.5, 1.0)
    else:
        # Only one event - maximum confidence
        confidence = 1.0
    
    # Generate recommended action
    action = _get_recommended_action(root_event)
    
    # Calculate duration
    duration = (incident.end_time - incident.start_time).total_seconds()
    
    return RCAReport(
        incident_id=incident.incident_id,
        root_cause=root_event,
        confidence=confidence,
        affected_services=incident.services_affected,
        symptom_chain=symptom_events,
        recommended_action=action,
        duration_seconds=duration,
    )


def _score_event(event: ErrorEvent, incident: Incident) -> float:
    """
    Calculate the RCA score for an event using the multi-factor formula.
    
    SCORE = (0.45 × root_flag) + (0.35 × downstream_norm) + (0.20 × earliness)
    
    Components:
    1. root_flag: How strongly this event's category indicates a root cause.
       1.0 for actual failures (DB_POOL_EXHAUSTED, OOM_KILL, DISK_FULL),
       0.5 for precursor warnings (DB_POOL_PRESSURE), 0.0 for symptoms.
       This prevents an early WARNING from outscoring the CRITICAL failure
       that follows it just because it happened a few seconds sooner.
    2. downstream_norm: Normalized count of services depending on this service (0.0-1.0)
    3. earliness: 1.0 if earliest event, 0.0 if latest event
    
    Args:
        event: ErrorEvent to score
        incident: Incident containing the event (for context)
    
    Returns:
        Score between 0.0 and 1.0
    
    Example:
        >>> score = _score_event(mongodb_error, incident)
        >>> score
        1.0  # Root cause of this incident
    """
    # FACTOR 1: Root Cause Category (45% weight)
    # How strongly does this event's category indicate a root cause?
    # Actual failures score 1.0, precursor warnings score 0.5, symptoms score 0.0.
    is_root_type = ROOT_CAUSE_WEIGHTS.get(event.category, 0.0)
    
    # FACTOR 2: Downstream Impact (35% weight)
    # How many other services depend ON this service? (blast radius)
    # Note: this uses get_service_dependents() (the reverse graph), NOT
    # get_service_dependencies(). A service near the root of the graph
    # (like mongodb, which nothing depends ON... wait, which everything
    # depends on) should score high here even though it has zero
    # dependencies of its own.
    dependent_services = get_service_dependents(event.entry.service)
    max_possible_deps = len(SERVICE_DEPENDENCIES) - 1  # All other services
    
    if max_possible_deps > 0:
        downstream = len(dependent_services) / max_possible_deps
    else:
        downstream = 0.0
    
    # FACTOR 3: Earliness / Timing (20% weight)
    # Earlier events score higher (more likely root cause)
    time_from_start = (event.entry.timestamp - incident.start_time).total_seconds()
    incident_duration = (incident.end_time - incident.start_time).total_seconds()
    
    if incident_duration > 0:
        earliness = 1.0 - (time_from_start / incident_duration)
    else:
        earliness = 1.0  # Only one event
    
    # Combine factors using weighted formula
    score = (0.45 * is_root_type) + (0.35 * downstream) + (0.20 * earliness)
    
    return score


def _get_recommended_action(root_event: ErrorEvent) -> str:
    """
    Get a recommended action based on the root cause category.
    
    Args:
        root_event: The ErrorEvent identified as root cause
    
    Returns:
        A human-readable string with recommended actions
    """
    category = root_event.category
    return ACTION_RECOMMENDATIONS.get(category, ACTION_RECOMMENDATIONS["DEFAULT"])


# ============================================================================
# CONVENIENCE / COMPATIBILITY FUNCTIONS
# ============================================================================

def cluster_into_incidents(errors: list[ErrorEvent]) -> list[Incident]:
    """
    Alias for cluster_errors() — group ErrorEvents into Incidents.
    
    Kept for backward compatibility with callers (main.py) that use this
    name. See cluster_errors() for full documentation.
    """
    return cluster_errors(errors)


def run_rca(errors: list[ErrorEvent]) -> list[RCAReport]:
    """
    Run the full RCA pipeline over a flat list of ErrorEvents.
    
    This is a convenience wrapper that combines clustering and analysis
    into a single call — exactly what you want for a quick end-to-end run:
    
        events = [classify(entry) for entry in log_entries]
        reports = run_rca(events)
    
    Internally this:
    1. Clusters the events into incidents (cluster_errors)
    2. Analyzes each incident to find its root cause (analyze_incident)
    
    Args:
        errors: Flat list of ErrorEvent objects (from the Classifier stage)
    
    Returns:
        List of RCAReport objects, one per incident, in the same order
        the incidents were formed (roughly chronological)
    
    Example:
        >>> reports = run_rca(all_error_events)
        >>> for report in reports:
        ...     print(report.root_cause.entry.service, report.confidence)
        mongodb 0.98
    """
    incidents = cluster_errors(errors)
    return [analyze_incident(incident) for incident in incidents]
