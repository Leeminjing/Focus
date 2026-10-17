"""`focus doctor`: inspect a running Gateway or collect a read-only offline report.

This module initially imports only stdlib so missing backend dependencies can be
reported too. It never opens the Gateway lifespan, migrates or dispatches work.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler


def global_home() -> Path:
    return Path(os.environ.get("FOCUS_GLOBAL_HOME", str(Path.home() / ".focus"))).expanduser()


def loopback_url(value: str) -> str:
    url = urlsplit(value)
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1", "localhost"}
            or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
        raise ValueError("Expected an HTTP loopback base URL")
    if not url.port:
        raise ValueError("Expected an explicit Gateway port")
    return value.rstrip("/")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_report(url: str, timeout: float) -> dict:
    # Bypass environment proxies and refuse redirects from a stale local port.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(loopback_url(url) + "/health", timeout=timeout) as response:
        report = json.loads(response.read(2 * 1024 * 1024))
    if (not isinstance(report, dict) or report.get("report_version") != 1
            or report.get("status") not in {"ok", "degraded"}
            or not isinstance(report.get("database"), dict)
            or not isinstance(report.get("runtime"), dict)
            or not isinstance(report.get("issues"), list)
            or report["database"].get("status") not in {"ok", "mismatch", "unavailable"}
            or report["runtime"].get("status") not in {"ok", "degraded", "unavailable"}):
        raise ValueError("Gateway does not provide the diagnostic report contract")
    return report


async def offline_report(timeout: float) -> dict:
    engine = None
    try:
        from dotenv import load_dotenv
        load_dotenv()
        from focus.config.layered import load_global_dotenv
        load_global_dotenv()
        from focus.config import get_app_config
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from backend.app.diagnostics import collect_diagnostics

        config = get_app_config("config.yaml", recover_models=True)
        url = os.environ.get("FOCUS_DATABASE_URL") or (config.database.url if config.database else None)
        if not url:
            raise ValueError("Database is not configured")
        engine = create_async_engine(url)
        report = await collect_diagnostics(async_sessionmaker(engine), timeout=timeout)
    except Exception as exc:
        report = {"report_version": 1, "generated_at": datetime.now(UTC).isoformat(),
                  "status": "degraded", "issues": ["offline_check_failed"],
                  "database": {"status": "unavailable", "error_type": type(exc).__name__}}
    finally:
        if engine is not None:
            await engine.dispose()
    report["runtime"] = {"status": "unavailable"}
    report["issues"].append("gateway_unavailable")
    report["status"] = "degraded"
    return report


def positive_timeout(value: str) -> float:
    result = float(value)
    if not 0 < result <= 60:
        raise argparse.ArgumentTypeError("timeout must be between 0 and 60 seconds")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the complete JSON report")
    parser.add_argument("--output", type=Path, help="save the complete JSON report")
    parser.add_argument("--url", type=loopback_url, help="Gateway base URL (auto-discovered for desktop)")
    parser.add_argument("--timeout", type=positive_timeout, default=5.0)
    args = parser.parse_args(argv)
    if args.output and not args.output.is_absolute() and os.getenv("FOCUS_DOCTOR_CWD"):
        args.output = Path(os.environ["FOCUS_DOCTOR_CWD"]) / args.output
    url = args.url or os.getenv("FOCUS_DESKTOP_API")
    if not url:
        try:
            url = json.loads((global_home() / "diagnostics-endpoint.json").read_text(encoding="utf-8"))["url"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    try:
        if not url:
            raise ValueError("No Gateway endpoint")
        report = fetch_report(url, args.timeout)
        report["source"] = "gateway"
    except Exception as exc:
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        report = asyncio.run(offline_report(args.timeout))
        report["source"] = "offline"
        report["gateway_error_type"] = type(exc).__name__
    document = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        try:
            args.output.write_text(document, encoding="utf-8")
        except OSError as exc:
            print(f"Cannot write diagnostic report ({type(exc).__name__}).", file=sys.stderr)
            return 2
    if args.json:
        print(document, end="")
    else:
        print(f"Focus doctor: {report['status']} ({report['source']})")
        print(f"Database: {report['database']['status']}; runtime: {report['runtime']['status']}")
        version = report.get("version", {})
        print(f"Version: {version.get('release') or version.get('application', 'unknown')}; commit: {version.get('commit') or 'unknown'}")
        database = report["database"]
        for name in ("loops", "runs"):
            if name in database:
                states = ", ".join(f"{state}={count}" for state, count in sorted(database[name]["states"].items()))
                print(f"Durable {name}: {states or 'none'}")
        runtime = report["runtime"]
        if "component_count" in runtime:
            print(f"Components: {runtime['component_count']}; unhealthy: {runtime['unhealthy_components']}")
        for name, queue in database.get("queues", {}).items():
            print(f"Queue {name}: {queue['pending']} outstanding; oldest: {queue['oldest_pending_at'] or '-'}")
        for issue in report["issues"]:
            print(f"- {issue}")
        if report["source"] == "offline":
            print("Start Focus to inspect live components, or use --url http://127.0.0.1:PORT.")
        if "schema_mismatch" in report["issues"]:
            print("Close Focus and run 'focus update' to align the database schema.")
        print("Use --json or --output FILE for schema, Loop/Run, budget and queue details.")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
