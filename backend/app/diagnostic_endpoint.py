"""Publish the desktop Gateway's dynamic loopback port without a session secret."""
from contextlib import contextmanager
import json
import os


@contextmanager
def publish_endpoint():
    port = os.getenv("FOCUS_DESKTOP_PORT")
    if not port:
        yield
        return
    from focus.config.layered import global_home

    path = global_home() / "diagnostics-endpoint.json"
    payload = {"pid": os.getpid(), "url": f"http://127.0.0.1:{int(port)}"}
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        # Diagnostic discovery must not prevent the desktop from starting.
        pass
    try:
        yield
    finally:
        try:
            if json.loads(path.read_text(encoding="utf-8")) == payload:
                path.unlink()
        except (OSError, ValueError):
            pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
