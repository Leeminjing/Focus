"""Keep validated host images outside the installation, keyed by content hash.

Saved bindings contain a hash, not an arbitrary executable path. Existing images
are verified and never overwritten/pruned while a live host may use them.
"""
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile

from .errors import BrowserError


def recovery_enabled():
    override = os.environ.get('FOCUS_DISTINCT_BROWSER_RECOVERY')
    if override is not None:
        return override == '1'
    # Match the native host's Windows 11 minimum build; other platforms keep
    # their existing default. Explicit values retain the prior opt-in contract.
    return os.name == 'nt' and sys.getwindowsversion().build >= 22000


def stage(executable, cache_root=None, artifact_sha256=None):
    root = Path(cache_root or os.environ.get('FOCUS_DISTINCT_BROWSER_CACHE') or
                Path(os.environ['LOCALAPPDATA']) / 'Focus' / 'BrowserRuntime').absolute()
    if artifact_sha256 is not None:
        if not re.fullmatch('[a-f0-9]{64}', artifact_sha256):
            raise BrowserError('host_unrecoverable', '保存的浏览器产物身份无效')
        target = root / artifact_sha256 / 'distinct-browser.exe'
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != artifact_sha256:
            raise BrowserError('host_unrecoverable', '原宿主的固定产物不存在或校验失败；没有重建页面')
        return target, artifact_sha256
    payload = Path(executable).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    target = root / digest / 'distinct-browser.exe'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise BrowserError('artifact_mismatch', '浏览器版本缓存校验失败')
    else:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(payload); output.flush(); os.fsync(output.fileno())
            try:
                os.replace(temporary, target)
            except OSError:
                if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise
        finally:
            if temporary: temporary.unlink(missing_ok=True)
    return target, digest
