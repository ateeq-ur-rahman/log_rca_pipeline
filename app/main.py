"""
FastAPI service exposing the log error-detection + RCA pipeline.

Run:
    uvicorn app.main:app --reload --port 8000

Endpoints:
    POST /ingest/text     - body: {"lines": ["...","..."]}  -> parse + store
    POST /ingest/sample   - loads data/sample_logs.log (demo convenience)
    GET  /errors          - all classified error/warn events seen so far
    GET  /incidents       - clustered incidents (no scoring)
    GET  /rca             - full RCA report for every incident
    GET  /rca/{incident_id} - RCA report for one incident
    DELETE /reset         - clear in-memory store (demo convenience)

In-memory storage is used for this demo so it runs with zero external
dependencies. In production, swap `STORE` for Elasticsearch: ingest still
parses/classifies the same way, but `/errors` becomes an ES query and
`/incidents`+`/rca` run as a scheduled job (or Elasticsearch Watcher) over
indexed events instead of an in-memory list.
"""
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.parser import parse_lines, parse_file
from app.classifier import classify
from app.rca_engine import cluster_into_incidents, run_rca, analyze_incident
from app.models import ErrorEvent

app = FastAPI(title="Log Error Detection & RCA Pipeline", version="1.0.0")

STORE: list[ErrorEvent] = []  # demo-only in-memory store; swap for Elasticsearch in prod

SAMPLE_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "sample_logs.log")


class IngestText(BaseModel):
    lines: list[str]


def _ingest_entries(entries):
    new_events = []
    for entry in entries:
        event = classify(entry)
        if event:  # None for INFO/DEBUG lines, which aren't errors
            STORE.append(event)
            new_events.append(event)
    return new_events


@app.post("/ingest/text")
def ingest_text(payload: IngestText):
    entries = parse_lines(payload.lines)
    new_events = _ingest_entries(entries)
    return {"parsed_lines": len(entries), "error_events_detected": len(new_events)}


@app.post("/ingest/sample")
def ingest_sample():
    if not os.path.exists(SAMPLE_LOG_PATH):
        raise HTTPException(404, "Sample log file not found. Run data/log_generator.py first.")
    entries = parse_file(SAMPLE_LOG_PATH)
    new_events = _ingest_entries(entries)
    return {"parsed_lines": len(entries), "error_events_detected": len(new_events)}


@app.get("/errors")
def list_errors():
    return [e.to_dict() for e in STORE]


@app.get("/incidents")
def list_incidents():
    incidents = cluster_into_incidents(STORE)
    return [
        {
            "incident_id": inc.incident_id,
            "services_affected": inc.services_affected,
            "event_count": len(inc.events),
            "start_time": inc.start_time.isoformat(),
            "end_time": inc.end_time.isoformat(),
        }
        for inc in incidents
    ]


@app.get("/rca")
def rca_all():
    return [r.to_dict() for r in run_rca(STORE)]


@app.get("/rca/{incident_id}")
def rca_one(incident_id: str):
    incidents = cluster_into_incidents(STORE)
    match = next((i for i in incidents if i.incident_id == incident_id), None)
    if not match:
        raise HTTPException(404, f"Incident {incident_id} not found")
    return analyze_incident(match).to_dict()


@app.delete("/reset")
def reset():
    STORE.clear()
    return {"status": "cleared"}
