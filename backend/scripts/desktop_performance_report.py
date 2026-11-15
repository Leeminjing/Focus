"""Summarize content-free focus_metric records from backend/Electron logs.

Usage: python -m backend.scripts.desktop_performance_report log.txt --output report.json
Provider rates use HTTP attempts; logical retry rates group correlated invocations.
Missing observations remain null/empty rather than being presented as zero latency.
"""

import argparse
from collections import defaultdict
import json
from pathlib import Path


def distribution(values):
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return {"samples": 0, "p50": None, "p95": None, "max": None}
    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (position - lower)
    return {"samples": len(ordered), "p50": percentile(0.5), "p95": percentile(0.95), "max": ordered[-1]}


def summarize(records):
    categories = defaultdict(list)
    processes = defaultdict(list)
    queues = defaultdict(list)
    cancellations = defaultdict(list)
    sql = defaultdict(lambda: {"queries": 0, "duration_ms": 0, "errors": 0})
    scans = None
    first_tokens = []
    structured = defaultdict(list)
    for record in records:
        kind = record.get("event")
        if kind == "provider_request":
            categories[(record.get("category", "unclassified"), record.get("provider"), record.get("model"))].append(record)
        elif kind == "structured_request":
            structured[(record.get("category"), record.get("model"), record.get("schema"))].append(record)
        elif kind in {"run_dispatch", "index_invocation"}:
            queues[record["category"]].append(record)
        elif kind == "process_sample":
            processes[(record["process"], record["pid"])].append(record)
            for category, values in record.get("sql", {}).items():
                for name in sql[category]:
                    sql[category][name] += values.get(name, 0)
        elif kind in {"run_cancelled", "index_cancelled"}:
            cancellations[kind].append(record)
        elif kind == "material_scan":
            if scans is None:
                scans = {"stat_checks": 0, "digest_reads": 0}
            for name in scans:
                scans[name] += record.get(name, 0)
        elif kind == "run_first_token":
            first_tokens.append(record["execution_to_first_token_ms"])
    requests = []
    for (category, provider, model), attempts in sorted(categories.items(), key=lambda item: str(item[0])):
        invocations = defaultdict(list)
        for attempt in attempts:
            invocations[attempt["invocation_id"]].append(attempt)
        requests.append({"category": category, "provider": provider, "model": model,
                         "http_attempts": len(attempts), "invocations": len(invocations),
                         "rate_429": sum(a.get("status_code") == 429 for a in attempts) / len(attempts),
                         "invocation_retry_rate": sum(len(group) > 1 or any(a.get("sdk_retry", 0) > 0 for a in group)
                                                      for group in invocations.values()) / len(invocations),
                         "extra_http_attempts": len(attempts) - len(invocations),
                         "headers_ms": distribution(a.get("headers_ms") for a in attempts),
                         "duration_ms": distribution(a.get("duration_ms") for a in attempts)})
    return {
        "provider_requests": requests,
        "structured_requests": [
            {"category": category, "model": model, "schema": schema, "requests": len(items),
             "schema_success_rate": sum(item.get("contract_valid") is True for item in items) / len(items),
             "usage_reported_requests": sum(item.get("usage_reported") is True for item in items),
             **{field: distribution(item.get(field) for item in items) for field in
                ("estimated_input_tokens", "input_tokens", "output_tokens", "ttft_ms", "output_duration_ms", "duration_ms")}}
            for (category, model, schema), items in sorted(structured.items(), key=lambda item: str(item[0]))
        ],
        "queues": {category: {name: distribution(r.get(name) for r in items)
                              for name in ("queue_wait_ms", "startup_ms", "admission_ms", "execution_ms")}
                   for category, items in queues.items()},
        "cancellations": {kind: distribution(r.get("local_completion_ms", r.get("release_ms")) for r in items)
                          for kind, items in cancellations.items()},
        "processes": [{"process": name, "pid": pid, "rss_bytes": distribution(r.get("rss_bytes") for r in items),
                       "rss_delta_bytes": (items[-1]["rss_bytes"] - items[0]["rss_bytes"] if len(items) > 1 else None),
                       "cpu_percent": distribution(r.get("cpu_percent") for r in items)}
                      for (name, pid), items in processes.items()],
        "sql": dict(sql), "material_scans": scans,
        "execution_to_first_token_ms": distribution(first_tokens),
        "limitations": ["Header latency is not first visible token latency.",
                        "Local cancellation does not prove upstream cancellation or settled usage.",
                        "Caller-supplied HTTP clients require their own request instrumentation.",
                        "Transport fallback attempts may have unknown upstream execution."],
    }


def read_records(path):
    records = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if "focus_metric " in line:
            payload = line.split("focus_metric ", 1)[1]
            records.append(json.loads(payload))
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = [record for path in args.logs for record in read_records(path)]
    if not records:
        parser.error("No focus_metric records found; enable INFO logging and FOCUS_PERFORMANCE_METRICS=1.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summarize(records), ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
