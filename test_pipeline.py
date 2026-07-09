"""
Standalone demo: runs the full pipeline (parse -> classify -> cluster ->
RCA) directly against data/sample_logs.log with no server needed.

Run:
    python3 test_pipeline.py
"""
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from app.parser import parse_file
from app.classifier import classify
from app.rca_engine import run_rca

LOG_PATH = os.path.join(os.path.dirname(__file__), "data", "sample_logs.log")


def main():
    if not os.path.exists(LOG_PATH):
        print(f"ERROR: {LOG_PATH} not found. Run: python3 data/log_generator.py")
        return 1

    entries = parse_file(LOG_PATH)
    print(f"✓ Parsed {len(entries)} log lines from {LOG_PATH}\n")

    events = [e for e in (classify(entry) for entry in entries) if e]
    print(f"✓ Detected {len(events)} WARN/ERROR events:")
    for e in events:
        flag = " [root-candidate]" if e.is_root_cause_candidate else ""
        print(f"  {e.entry.timestamp.time()}  {e.entry.service:<18} {e.severity:<8} {e.category}{flag}")

    print("\n" + "=" * 80)
    print("RCA ANALYSIS RESULTS")
    print("=" * 80)

    for report in run_rca(events):
        print(f"\n[{report.incident_id}]")
        if report.root_cause is None:
            print("  No events.")
            continue
        rc = report.root_cause
        print(f"  ROOT CAUSE")
        print(f"    Service  : {rc.entry.service}")
        print(f"    Category : {rc.category}")
        print(f"    Message  : \"{rc.entry.message}\"")
        print(f"    Time     : {rc.entry.timestamp}")
        print(f"")
        print(f"  INCIDENT SCOPE")
        print(f"    Affected services : {', '.join(report.affected_services)}")
        print(f"    Duration          : {report.duration_seconds:.1f}s")
        print(f"    Confidence        : {report.confidence:.0%}")
        print(f"")
        print(f"  RECOMMENDED ACTION")
        print(f"    {report.recommended_action}")
        if report.symptom_chain:
            print(f"")
            print(f"  SYMPTOM CHAIN ({len(report.symptom_chain)} downstream):")
            for i, s in enumerate(report.symptom_chain, 1):
                print(f"    {i}. [{s.entry.service}] {s.category}")
                print(f"       {s.entry.message}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
