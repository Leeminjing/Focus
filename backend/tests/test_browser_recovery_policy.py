"""Platform default and explicit rollback contract; no native windows or devices."""
from types import SimpleNamespace

import pytest

from backend.app.desktop.browser import host_artifact


@pytest.mark.parametrize('platform,build,override,expected', [
    ('nt', 22000, None, True),
    ('nt', 26100, None, True),
    ('nt', 19045, None, False),
    ('posix', None, None, False),
    ('nt', 26100, '0', False),
    ('nt', 26100, '1', True),
    ('nt', 26100, '', False),
    ('nt', 26100, 'invalid', False),
    ('posix', None, '1', True),  # Preserve the existing explicit opt-in; transport rejects unsupported OS.
])
def test_recovery_platform_default_and_explicit_overrides(monkeypatch, platform, build, override, expected):
    environment = {} if override is None else {'FOCUS_DISTINCT_BROWSER_RECOVERY': override}
    monkeypatch.setattr(host_artifact, 'os', SimpleNamespace(name=platform, environ=environment))
    monkeypatch.setattr(host_artifact, 'sys', SimpleNamespace(getwindowsversion=lambda: SimpleNamespace(build=build)), raising=False)
    assert host_artifact.recovery_enabled() is expected
