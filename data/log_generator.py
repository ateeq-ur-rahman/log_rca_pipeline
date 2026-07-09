"""
Generates a realistic sample log file containing:
  1. Normal background traffic (INFO logs)
  2. A cascading-failure incident: MongoDB connection pool exhaustion
     that ripples into order-service, inventory-service, api-gateway,
     and trips a circuit breaker in payment-service.
  3. Unrelated "noise" errors (e.g. a bad JWT) that should NOT be
     pulled into the incident's root-cause story.

This mirrors a typical on-call scenario so the RCA engine has something
non-trivial to reason about (multiple services failing in a short window,
only one of which is the actual root cause).
"""
from datetime import datetime, timedelta
import os

OUT_PATH = os.path.join(os.path.dirname(__file__), "sample_logs.log")

BASE_TIME = datetime(2026, 6, 17, 10, 15, 0)


def ts(offset_seconds: float) -> str:
    t = BASE_TIME + timedelta(seconds=offset_seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(t.microsecond/1000):03d}Z"


def line(offset, service, level, message):
    return f"{ts(offset)} [{service}] {level} {message}"


def build_logs():
    logs = []

    # --- normal background traffic ---
    for i in range(8):
        logs.append(line(i * 0.4, "api-gateway", "INFO", f"GET /orders/{1000+i} 200 OK 23ms"))
        logs.append(line(i * 0.4 + 0.1, "order-service", "INFO", f"Order {1000+i} fetched successfully"))

    # --- the incident begins: mongodb connection pool under pressure ---
    logs.append(line(3.2, "mongodb", "WARN", "Connection pool utilization at 92% (184/200 connections in use)"))
    logs.append(line(4.8, "mongodb", "WARN", "Connection pool utilization at 97% (194/200 connections in use)"))
    logs.append(line(6.1, "mongodb", "ERROR", "Connection pool exhausted: rejecting new connection requests"))

    # downstream effects in order-service (depends directly on mongodb)
    for i, off in enumerate([6.6, 7.0, 7.5, 8.1]):
        logs.append(line(off, "order-service", "ERROR",
                          f"DB connection timeout after 5000ms while processing order ORD-{2200+i}"))
    logs.append(line(8.4, "order-service", "ERROR",
                      "Failed to process order ORD-2204: DBConnectionError: pool exhausted"))

    # inventory-service also depends directly on mongodb
    logs.append(line(8.9, "inventory-service", "ERROR",
                      "DB connection timeout after 5000ms while checking stock for SKU-9182"))
    logs.append(line(9.3, "inventory-service", "ERROR",
                      "StockCheckFailed: DBConnectionError: pool exhausted"))

    # api-gateway depends on order-service and inventory-service
    logs.append(line(9.6, "api-gateway", "ERROR", "503 Service Unavailable from order-service (POST /orders)"))
    logs.append(line(9.9, "api-gateway", "ERROR", "503 Service Unavailable from inventory-service (GET /stock)"))

    # payment-service depends on order-service -> trips circuit breaker
    logs.append(line(10.4, "payment-service", "WARN",
                      "Circuit breaker OPEN for order-service after 5 consecutive failures"))
    logs.append(line(10.6, "payment-service", "ERROR",
                      "Payment ORD-2204 aborted: dependent service order-service unavailable"))

    # mongodb recovers
    logs.append(line(14.0, "mongodb", "INFO", "Connection pool recovered: 41/200 connections in use"))
    logs.append(line(14.5, "order-service", "INFO", "DB connectivity restored, resuming normal processing"))

    # --- unrelated noise, should NOT be attributed to the mongodb incident ---
    logs.append(line(2.0, "auth-service", "ERROR", "Invalid JWT token for request from 203.0.113.55"))
    logs.append(line(12.0, "auth-service", "WARN", "Rate limit exceeded for client 198.51.100.23"))

    # more normal traffic after recovery
    for i in range(4):
        off = 15 + i * 0.5
        logs.append(line(off, "api-gateway", "INFO", f"GET /orders/{3000+i} 200 OK 19ms"))

    logs.sort(key=lambda l: l.split("]")[0])
    return logs


def main():
    logs = build_logs()
    with open(OUT_PATH, "w") as f:
        f.write("\n".join(logs) + "\n")
    print(f"Wrote {len(logs)} log lines to {OUT_PATH}")


if __name__ == "__main__":
    main()
