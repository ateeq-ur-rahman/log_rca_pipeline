# Log Error Detection & Root Cause Analysis Pipeline

A production-ready log ingestion and automatic root cause analysis (RCA) system that detects errors, clusters them into incidents, and identifies the underlying root cause—all while filtering out unrelated noise.

## Quick Start

### Standalone Demo (No Server)

```bash
# 1. Generate sample logs (cascading MongoDB failure with 6 services)
python3 data/log_generator.py

# 2. Run the full pipeline and print RCA results
python3 test_pipeline.py
```

Expected output: One incident identified (INC-001) with MongoDB connection pool exhaustion as root cause, 6 affected services, and a symptom chain showing the cascading failures.

### With FastAPI Server

```bash
# Install dependencies (if needed)
pip install fastapi uvicorn --break-system-packages

# Start the server
uvicorn app.main:app --reload --port 8000

# In another terminal, ingest sample logs
curl -X POST http://localhost:8000/ingest/sample

# Get the full RCA report
curl http://localhost:8000/rca | python3 -m json.tool
```

## Architecture

### Pipeline Stages

```
Raw Logs
    ↓
[PARSER]          → Regex-based log line parsing (ISO 8601 timestamps)
    ↓
LogEntry objects
    ↓
[CLASSIFIER]      → Pattern matching against known error categories
    ↓              → Marks each error as root-cause candidate or symptom
ErrorEvent objects
    ↓
[CLUSTERING]      → Graph-aware sliding-window temporal correlation
    ↓              → Groups causally-related errors; filters noise
Incident objects (grouped events)
    ↓
[RCA ENGINE]      → Scores each event by:
    ↓                  • Category type (root-cause vs symptom markers)
    ↓                  • Temporal position (earliness in the incident)
    ↓                  • Blast radius (downstream services affected)
    ↓
RCAReport
    (root cause, confidence, symptom chain, recommended action)
```

### Key Design Decisions

#### 1. **Graph-Aware Clustering**

A naive time-window-only clustering would merge unrelated errors that happen to land near each other. This pipeline tracks service dependencies (e.g., "order-service depends on mongodb") and only adds an event to an incident if its service is causally related to at least one service already in the cluster.

Example: A JWT auth failure at 10:15:02 and a MongoDB pool exhaustion at 10:15:06 are **not** merged into one incident because `auth-service` and `mongodb` have no dependency relationship.

```python
# From rca_engine.py
SERVICE_DEPENDENCIES = {
    "order-service": ["mongodb"],
    "inventory-service": ["mongodb"],
    "api-gateway": ["order-service", "inventory-service", "payment-service"],
    "payment-service": ["order-service"],
    "mongodb": [],
    "auth-service": [],
}
```

#### 2. **Category-Based Root Cause Scoring**

Errors fall into two buckets:
- **Root causes**: Connection pool exhaustion, OOM, disk full, DB unavailable
- **Symptoms**: Timeouts, 5xx responses, circuit breakers opening

The RCA engine weights root-cause categories heavily (0.45 of the score) so a single "DB_POOL_EXHAUSTED" error beats the "earliest error is root cause" heuristic.

```python
# Weighted scoring
score = (0.45 * root_flag) + (0.35 * downstream_norm) + (0.20 * earliness)
```

#### 3. **Downstream Impact Scoring**

The root cause should have "caused" the most other service failures. We count how many affected services in the incident depend (directly or transitively) on the root-cause service.

```
mongodb pool exhausted (score: 0.45*1.0 + 0.35*1.0 + 0.20*1.0 = 1.0)
  ↓ causes
order-service timeout (score: 0.45*0 + 0.35*0.5 + 0.20*0.95 = 0.38)
  ↓ causes
api-gateway 503 (score: 0.45*0 + 0.35*0.33 + 0.20*0.90 = 0.29)
```

### Data Models

```python
@dataclass
class LogEntry:
    timestamp: datetime
    service: str
    level: str              # INFO | WARN | ERROR
    message: str
    raw: str

@dataclass
class ErrorEvent:
    entry: LogEntry
    category: str           # e.g. DB_POOL_EXHAUSTED
    is_root_cause_candidate: bool
    severity: str           # LOW | MEDIUM | HIGH | CRITICAL

@dataclass
class Incident:
    incident_id: str
    events: list[ErrorEvent]

@dataclass
class RCAReport:
    root_cause: ErrorEvent | None
    confidence: float       # 0.5–1.0
    affected_services: list[str]
    symptom_chain: list[ErrorEvent]
    recommended_action: str
    duration_seconds: float
```

## API Endpoints

All endpoints are JSON. Default port: 8000.

### POST `/ingest/text`
Ingest raw log lines (one per list element).

```bash
curl -X POST http://localhost:8000/ingest/text \
  -H "Content-Type: application/json" \
  -d '{"lines": ["2026-06-17T10:15:06.100Z [mongodb] ERROR Connection pool exhausted"]}'
```

Response:
```json
{"parsed_lines": 1, "error_events_detected": 1}
```

### POST `/ingest/sample`
Convenience endpoint: loads the demo `data/sample_logs.log`.

```bash
curl -X POST http://localhost:8000/ingest/sample
```

### GET `/errors`
List all classified ERROR/WARN events currently in memory.

```bash
curl http://localhost:8000/errors | python3 -m json.tool
```

### GET `/incidents`
List all clustered incidents (no RCA scoring yet).

```bash
curl http://localhost:8000/incidents
```

Response:
```json
[
  {
    "incident_id": "INC-001",
    "services_affected": ["api-gateway", "inventory-service", "mongodb", "order-service", "payment-service"],
    "event_count": 9,
    "start_time": "2026-06-17T10:15:03.200Z",
    "end_time": "2026-06-17T10:15:10.600Z"
  }
]
```

### GET `/rca`
Full RCA reports for all incidents.

```bash
curl http://localhost:8000/rca | python3 -m json.tool
```

Response:
```json
[
  {
    "incident_id": "INC-001",
    "root_cause": {
      "timestamp": "2026-06-17T10:15:06.100Z",
      "service": "mongodb",
      "level": "ERROR",
      "message": "Connection pool exhausted: rejecting new connection requests",
      "category": "DB_POOL_EXHAUSTED",
      "severity": "CRITICAL",
      "is_root_cause_candidate": true
    },
    "confidence": 0.98,
    "affected_services": ["api-gateway", "inventory-service", "mongodb", "order-service", "payment-service"],
    "symptom_chain": [...],
    "recommended_action": "Scale DB connection pool size / add read replicas; review slow queries holding connections.",
    "duration_seconds": 7.4
  }
]
```

### GET `/rca/{incident_id}`
RCA report for a single incident.

```bash
curl http://localhost:8000/rca/INC-001 | python3 -m json.tool
```

### DELETE `/reset`
Clear the in-memory event store.

```bash
curl -X DELETE http://localhost:8000/reset
```

## Log Format

The parser expects ISO 8601 timestamps and a bracketed service name:

```
2026-06-17T10:15:06.100Z [mongodb] ERROR Connection pool exhausted: rejecting new connection requests
2026-06-17T10:15:06.600Z [order-service] ERROR DB connection timeout after 5000ms while processing order ORD-2200
```

The regex is permissive about the message body, so you can adapt it to your logging format by tweaking `app/parser.py`:

```python
LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\s+"
    r"\[(?P<service>[\w\-]+)\]\s+"
    r"(?P<level>INFO|WARN|ERROR|DEBUG|CRITICAL)\s+"
    r"(?P<message>.+)$"
)
```

## Error Categories & Classification Rules

All rules are in `app/classifier.py`. Examples:

| Pattern | Category | Root Cause? | Severity |
|---------|----------|-------------|----------|
| `connection pool exhausted` | `DB_POOL_EXHAUSTED` | ✅ Yes | CRITICAL |
| `connection pool utilization at 92%` | `DB_POOL_PRESSURE` | ✅ Yes | MEDIUM |
| `out of memory` | `OOM_KILL` | ✅ Yes | CRITICAL |
| `DB connection timeout` | `DB_CONNECTION_TIMEOUT` | ❌ No (symptom) | HIGH |
| `503 service unavailable` | `SERVICE_UNAVAILABLE` | ❌ No (symptom) | HIGH |
| `circuit breaker open` | `CIRCUIT_BREAKER_OPEN` | ❌ No (symptom) | MEDIUM |
| `invalid jwt` | `AUTH_ERROR` | ❌ No (noise) | LOW |
| `rate limit exceeded` | `RATE_LIMIT` | ❌ No (noise) | LOW |

Add your own rules by extending `RULES` in `app/classifier.py`:

```python
RULES = [
    (re.compile(r"my custom error pattern", re.I),
     "MY_CUSTOM_CATEGORY", "HIGH", True,  # is root cause?
     "Recommended action text here."),
    ...
]
```

## Configurable Parameters

**`rca_engine.py`**:
- `CORRELATION_WINDOW_SECONDS = 8` — gap larger than this starts a new incident
- `SERVICE_DEPENDENCIES` — add/remove services and their direct dependencies

**`parser.py`**:
- `LOG_PATTERN` — regex to parse raw log lines
- `TS_FORMAT` — timestamp parsing format

## Production Deployment

This demo uses in-memory storage. For production:

1. **Replace STORE with Elasticsearch**
   - Parse & classify logs the same way
   - Index each ErrorEvent as a doc in an ES index
   - POST `/ingest/text` still parses/classifies, but writes to ES instead of STORE
   - GET `/errors` becomes an ES query (`GET myindex/_search`)
   - Run RCA as a scheduled job (or Elasticsearch Watcher) over recent events

2. **Add Kafka/Pub-Sub Ingest**
   - Subscribe to a Kafka topic (or Pub/Sub channel) streaming raw logs
   - Same parsing/classification pipeline
   - Batch write ErrorEvents to Elasticsearch

3. **Add Persistence**
   - Store RCA reports in a timeseries DB (InfluxDB, TimescaleDB)
   - Query historical incidents for trend analysis

4. **Scale the Classifier**
   - Move pattern rules to a database
   - Add ML-based anomaly detection alongside pattern rules
   - Use vector embeddings for semantic error matching

### Example Elasticsearch Integration

```python
from elasticsearch import Elasticsearch

es = Elasticsearch(["http://localhost:9200"])

# In POST /ingest/text:
for event in new_events:
    es.index(index="error_events", body=event.to_dict())

# In GET /errors:
response = es.search(index="error_events", body={
    "query": {"match_all": {}},
    "size": 1000
})
return [h["_source"] for h in response["hits"]["hits"]]

# In GET /rca (run as scheduled job):
# Query last 1 hour of events, cluster & score them
response = es.search(index="error_events", body={
    "query": {
        "range": {
            "timestamp": {"gte": "now-1h"}
        }
    },
    "size": 10000
})
events = [...]  # hydrate from response
incidents = cluster_into_incidents(events)
reports = [analyze_incident(inc) for inc in incidents]
```

## Sample Log Scenario

The `data/log_generator.py` creates a realistic cascading failure:

1. **10:15:03.200Z** — MongoDB pool at 92% utilization (warning sign)
2. **10:15:04.800Z** — MongoDB pool at 97% utilization (escalating)
3. **10:15:06.100Z** — **MongoDB pool exhausted** ← ROOT CAUSE
4. **10:15:06.600–08.400Z** — order-service hits DB timeouts (symptom)
5. **10:15:08.900Z** — inventory-service hits DB timeouts (symptom)
6. **10:15:09.600Z** — api-gateway returns 503 from downstream (symptom)
7. **10:15:10.400Z** — payment-service circuit breaker opens (protective symptom)
8. **10:15:14.000Z** — MongoDB recovers
9. **10:15:02.000Z** — Unrelated auth failure (noise, ignored)
10. **10:15:12.000Z** — Unrelated rate limit (noise, ignored)

The RCA engine correctly identifies the MongoDB pool exhaustion (event #3) as the root cause, not the JWT auth failure (#9) even though it happens first.

## File Structure

```
log-rca-pipeline/
├── app/
│   ├── __init__.py
│   ├── models.py           # Data classes (LogEntry, ErrorEvent, Incident, RCAReport)
│   ├── parser.py           # Log line parsing (regex)
│   ├── classifier.py       # Error categorization & root-cause marking
│   ├── rca_engine.py       # Clustering & RCA scoring
│   └── main.py             # FastAPI app
├── data/
│   └── log_generator.py    # Generate demo scenario
├── diagrams/
│   └── system_design.svg   # Architecture diagram
├── test_pipeline.py        # Standalone CLI test
├── README.md               # This file
└── requirements.txt        # Dependencies (FastAPI, Uvicorn)
```

## Testing

**Unit tests** (add to `tests/` directory):

```python
# tests/test_classifier.py
from app.classifier import classify
from app.parser import parse_line

def test_db_pool_exhausted():
    entry = parse_line("2026-06-17T10:15:06.100Z [mongodb] ERROR Connection pool exhausted")
    event = classify(entry)
    assert event.category == "DB_POOL_EXHAUSTED"
    assert event.is_root_cause_candidate == True
    assert event.severity == "CRITICAL"

def test_auth_error_is_not_root_cause():
    entry = parse_line("2026-06-17T10:15:02.000Z [auth-service] ERROR Invalid JWT token")
    event = classify(entry)
    assert event.category == "AUTH_ERROR"
    assert event.is_root_cause_candidate == False
```

**Integration test**:

```bash
python3 test_pipeline.py
# Verifies the full pipeline and prints RCA results
```

## Contributing

To add a new error pattern:

1. Update `RULES` in `app/classifier.py`
2. Decide: is it a root cause or symptom?
3. Add a test in `tests/test_classifier.py`
4. Update the `SERVICE_DEPENDENCIES` in `app/rca_engine.py` if introducing a new service

## License

MIT

## References

- **Observability**: Distributed tracing pairs naturally with log analysis. Use this RCA engine as part of a broader observability platform (e.g., Grafana, Datadog, New Relic).
- **Alerting**: Pipe RCA reports into incident management (PagerDuty, Opsgenie) to auto-page oncall when critical incidents are detected.
- **Feedback Loop**: Track which RCA recommendations resolved incidents to continuously improve the scoring model.
