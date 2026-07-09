# System Design: Log Error Detection & RCA Pipeline

## Pipeline Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                               │
│  RAW LOGS (from app, infrastructure, etc.)                                   │
│  2026-06-17T10:15:06.100Z [mongodb] ERROR Connection pool exhausted...      │
│  2026-06-17T10:15:06.600Z [order-service] ERROR DB connection timeout...   │
│  ...                                                                          │
│                                                                               │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │
                    ┌────────────▼───────────┐
                    │   PARSING STAGE       │
                    │  (app/parser.py)      │
                    │                        │
                    │  • Regex-based        │
                    │  • ISO 8601 timestamps│
                    │  • Extract service,   │
                    │    level, message     │
                    │                        │
                    │  Output: LogEntry[]   │
                    └────────────┬──────────┘
                                 │
                    ┌────────────▼────────────┐
                    │ CLASSIFICATION STAGE   │
                    │ (app/classifier.py)    │
                    │                         │
                    │ • Pattern matching     │
                    │ • Categorize error     │
                    │ • Mark root-cause vs   │
                    │   symptom vs noise     │
                    │                         │
                    │ Output: ErrorEvent[]   │
                    └────────────┬───────────┘
                                 │
        ┌────────────────────────┴────────────────────────┐
        │                                                  │
        │         EXAMPLE CLASSIFICATIONS                │
        │                                                 │
        │  "pool exhausted"           → DB_POOL_EXHAUSTED  │
        │                              root-cause=TRUE     │
        │                                                 │
        │  "DB connection timeout"    → DB_CONNECTION_TIMEOUT
        │                              root-cause=FALSE    │
        │                                                 │
        │  "503 service unavailable"  → SERVICE_UNAVAILABLE│
        │                              root-cause=FALSE    │
        │                                                 │
        │  "invalid JWT token"        → AUTH_ERROR        │
        │                              root-cause=FALSE    │
        │                                                 │
        └────────────────────┬─────────────────────────────┘
                             │
                    ┌────────▼─────────────┐
                    │ CLUSTERING STAGE    │
                    │ (rca_engine.py)     │
                    │                      │
                    │ • Time-window        │
                    │   correlation        │
                    │ • Service dependency │
                    │   graph awareness    │
                    │ • Filter noise       │
                    │                      │
                    │ Output: Incident[]  │
                    └────────────┬────────┘
                                 │
                    ┌────────────▼──────────────────┐
                    │  RCA SCORING STAGE           │
                    │  (rca_engine.py)             │
                    │                               │
                    │ For each ErrorEvent, score:   │
                    │   • Root-cause category flag  │
                    │   • Temporal position         │
                    │   • Downstream impact         │
                    │                               │
                    │ score = 0.45*root_flag        │
                    │       + 0.35*impact           │
                    │       + 0.20*earliness        │
                    │                               │
                    │ Output: RCAReport            │
                    └────────────┬──────────────────┘
                                 │
        ┌────────────────────────┴────────────────────┐
        │                                              │
        │      EXAMPLE RCA OUTPUT (INC-001)           │
        │                                              │
        │  Root Cause:                                 │
        │    [mongodb] DB_POOL_EXHAUSTED              │
        │    "Connection pool exhausted..."           │
        │    Timestamp: 2026-06-17T10:15:06.100Z      │
        │    Confidence: 98%                          │
        │                                              │
        │  Affected Services: [mongodb, order-service,│
        │    inventory-service, api-gateway,          │
        │    payment-service]                         │
        │                                              │
        │  Duration: 7.4 seconds                      │
        │                                              │
        │  Recommended Action:                        │
        │    Scale DB connection pool size / add      │
        │    read replicas; review slow queries...   │
        │                                              │
        │  Symptom Chain:                             │
        │    1. [order-service] DB_CONNECTION_TIMEOUT │
        │       "DB connection timeout..."            │
        │                                              │
        │    2. [inventory-service] DEPENDENT_DB_...  │
        │       "StockCheckFailed..."                │
        │                                              │
        │    3. [api-gateway] SERVICE_UNAVAILABLE     │
        │       "503 Service Unavailable..."         │
        │                                              │
        │    4. [payment-service] CIRCUIT_BREAKER_OPEN
        │       "Circuit breaker OPEN..."            │
        │                                              │
        └─────────────────────────────────────────────┘

```

## Architecture: How Clustering Avoids Noise

```
SCENARIO: MongoDB fails at T=6.1s, but also there's an unrelated 
         JWT auth error at T=2.0s and a rate limit at T=12.0s

TIME
 ↑
 │ 2.0s  ┌─────────────┐   (auth-service ERROR: Invalid JWT)
 │       │ AUTH_ERROR  │   NOISE: No dependency link to mongodb
 │       │ [isolated]  │
 │       └─────────────┘
 │
 │ 3.2-10.6s  ┌────────────────────────────────────────────┐
 │            │ INCIDENT INC-001                           │
 │            │ ┌──────────────────────────────────────┐   │
 │            │ │ mongodb: DB_POOL_EXHAUSTED (root)   │   │
 │            │ │ order-service: DB_TIMEOUT (symptom) │   │
 │            │ │ inventory-svc: DB_TIMEOUT (symptom) │   │
 │            │ │ api-gateway: 503 (symptom)          │   │
 │            │ │ payment-svc: CIRCUIT_BREAKER (symp) │   │
 │            │ └──────────────────────────────────────┘   │
 │            │ (All services linked via dependencies)     │
 │            └────────────────────────────────────────────┘
 │
 │ 12.0s ┌─────────────┐   (auth-service WARN: Rate limit)
 │       │ RATE_LIMIT  │   NOISE: No dependency link to mongodb
 │       │ [isolated]  │
 │       └─────────────┘
 │
 └──────────────────────────────────────────────────────────► TIME

Clustering Logic:
  • Start: mongodb error at T=6.1s → INC-001
  • Consider order-service error at T=6.6s:
      Gap = 0.5s < 8s ✓
      Related? order-service depends on mongodb ✓
      → Add to INC-001
  • Consider auth-service error at T=2.0s:
      (time-sorted, so happens before, but won't be revisited)
  • Consider auth-service error at T=12.0s:
      Gap = 1.4s < 8s ✓
      Related? auth-service does NOT depend on mongodb ✗
      → Start new INC-002 (single-event incident, likely noise)
  
Result: INC-001 correctly contains only mongodb cascade, not noise.
```

## Service Dependency Graph

```
                    ┌──────────────────┐
                    │   api-gateway    │
                    └────────┬────┬────┘
                    ┌────────┘    └────────┐
                    │                      │
        ┌───────────▼──────────┐   ┌──────▼──────────┐
        │ order-service        │   │inventory-service│
        └────────────┬─────────┘   └─────────┬───────┘
                     │                       │
                     │        payment-svc    │
                     │         depends on    │
                     │       order-service   │
                     │                       │
                    ┌┴───────────────────────┴────┐
                    │        mongodb              │
                    │    (no dependencies)        │
                    └─────────────────────────────┘

Dependency matrix (for scoring):
  mongodb               depends on: []              # Root of chain
  order-service        depends on: [mongodb]
  inventory-service    depends on: [mongodb]
  api-gateway          depends on: [order-service, inventory-service, payment-service]
  payment-service      depends on: [order-service]
  auth-service         depends on: []              # Isolated

When mongodb fails:
  • order-service sees DB timeouts (direct dep)
  • inventory-service sees DB timeouts (direct dep)
  • api-gateway sees 503s from order & inventory (indirect dep via 2 hops)
  • payment-service sees circuit breaker (indirect dep via order-service)
  • auth-service unaffected (no dep path)
```

## Scoring Example: MongoDB Pool Exhaustion Incident

```
Events in the incident (sorted by score):

1. mongodb@T=6.1s
   Category: DB_POOL_EXHAUSTED (is_root_cause=TRUE)
   Elapsed: 0s
   
   Downstream impact: 4 services depend on it
     → order-service (direct)
     → inventory-service (direct)
     → api-gateway (indirect via order-service, inventory-service)
     → payment-service (indirect via order-service)
   downstream_norm = 4 / 4 = 1.0
   
   Earliness = 1.0 - (0s / 7.4s) = 1.0
   
   SCORE = 0.45*1.0 + 0.35*1.0 + 0.20*1.0 = 1.00
   ✓ SELECTED AS ROOT CAUSE

2. order-service@T=6.6s
   Category: DB_CONNECTION_TIMEOUT (is_root_cause=FALSE)
   Elapsed: 0.5s
   
   Downstream impact: 2 services depend on it
     → api-gateway (direct)
     → payment-service (direct)
   downstream_norm = 2 / 4 = 0.5
   
   Earliness = 1.0 - (0.5s / 7.4s) = 0.93
   
   SCORE = 0.45*0 + 0.35*0.5 + 0.20*0.93 = 0.36
   → Becomes symptom #1

3. inventory-service@T=8.9s
   Category: DEPENDENT_DB_FAILURE (is_root_cause=FALSE)
   Elapsed: 2.8s
   
   Downstream impact: 1 service depends on it
     → api-gateway (direct)
   downstream_norm = 1 / 4 = 0.25
   
   Earliness = 1.0 - (2.8s / 7.4s) = 0.62
   
   SCORE = 0.45*0 + 0.35*0.25 + 0.20*0.62 = 0.21
   → Becomes symptom #2

4. api-gateway@T=9.6s
   Category: SERVICE_UNAVAILABLE (is_root_cause=FALSE)
   Elapsed: 3.5s
   
   Downstream impact: 0 services depend on it
   downstream_norm = 0 / 4 = 0.0
   
   Earliness = 1.0 - (3.5s / 7.4s) = 0.53
   
   SCORE = 0.45*0 + 0.35*0.0 + 0.20*0.53 = 0.11
   → Becomes symptom #3

5. payment-service@T=10.4s
   Category: CIRCUIT_BREAKER_OPEN (is_root_cause=FALSE)
   Elapsed: 4.3s
   
   Downstream impact: 0 services depend on it
   downstream_norm = 0 / 4 = 0.0
   
   Earliness = 1.0 - (4.3s / 7.4s) = 0.42
   
   SCORE = 0.45*0 + 0.35*0.0 + 0.20*0.42 = 0.08
   → Becomes symptom #4

CONFIDENCE = min(1.0, max(0.5, 1.00 - 0.36 + 0.5)) = 1.0
```

## State Machine: Incident Lifecycle

```
                  ┌─────────────┐
                  │   Created   │
                  │  (event 1)  │
                  └──────┬──────┘
                         │
                         │ Add event within time window
                         │ & dependency-related
                         │
                  ┌──────▼──────┐
                  │   Collecting│
                  │  (events 2+)│
                  └──────┬──────┘
                         │
                    ┌────┴────┐
           ┌────────┘         └────────┐
           │                           │
    Event beyond   Unrelated event OR  │
    time window    time window expired │
           │                           │
    ┌──────▼──────┐                    │
    │   Closed    │◄───────────────────┘
    │   (scoring)  │
    │  (RCA runs)  │
    └─────────────┘
```

## Production Deployment Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                     PRODUCTION SETUP                               │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  Apps/Services                                                    │
│  ├─ app-1: stdout/stderr → log shipper                           │
│  ├─ app-2: structured JSON → log shipper                         │
│  └─ infra: syslog → log shipper                                  │
│                 │                                                │
│                 ▼                                                │
│  ┌───────────────────────┐                                       │
│  │  Kafka / Pub-Sub      │  (streaming log events)                │
│  └───────────┬───────────┘                                       │
│              │                                                   │
│              ▼                                                   │
│  ┌────────────────────────────────────┐                         │
│  │  Log RCA Pipeline                  │                         │
│  │  (This project)                    │                         │
│  │                                     │                         │
│  │  1. Parse                          │                         │
│  │  2. Classify                       │                         │
│  │  3. Index to Elasticsearch         │                         │
│  │  4. Run RCA (scheduled job)        │                         │
│  │  5. Write reports                  │                         │
│  └────────────┬──────────┬────────────┘                         │
│               │          │                                       │
│      ┌────────▼─┐  ┌─────▼──────┐                              │
│      │Elasticsearch│  │TimescaleDB│                              │
│      │ (events)    │  │ (reports) │                              │
│      └────────┬─┘  └─────┬──────┘                              │
│               │          │                                       │
│      ┌────────▼──────────▼──┐                                   │
│      │  Grafana Dashboard   │                                   │
│      │  - Incident timeline │                                   │
│      │  - Root causes       │                                   │
│      │  - Trends            │                                   │
│      └──────────┬───────────┘                                   │
│               │                                                 │
│      ┌────────▼──────────┐                                      │
│      │  PagerDuty/Opsgenie│  (auto-page on critical)           │
│      └───────────────────┘                                      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Performance Characteristics

| Stage | Time Complexity | Space | Notes |
|-------|-----------------|-------|-------|
| Parse | O(n) | O(n) | Regex matching per line |
| Classify | O(n * m) | O(n) | m = # of rules (~12) |
| Cluster | O(n log n + n*d) | O(n) | Dependency graph lookups |
| RCA Score | O(n^2 * s) | O(n) | n = events, s = services |

For typical incident sizes (10–100 errors):
- **Parse**: <10ms (100 lines)
- **Classify**: <50ms
- **Cluster**: <30ms
- **RCA**: <20ms

Total latency (request → RCA report): **~100ms**

Bottleneck in production will be Elasticsearch query latency, not the RCA logic.
