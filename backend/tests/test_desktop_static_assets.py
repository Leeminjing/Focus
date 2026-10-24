"""Verify the desktop entry pages and their resource graph at the public URL.

Uses the production static root without starting databases, models or Electron.
"""
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
import pytest

from backend.app.desktop.app import DESKTOP_DIR


class PageResources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        key = "href" if tag == "link" else "src" if tag in {"script", "img"} else None
        if key and attributes.get(key):
            self.urls.append(attributes[key])


@pytest.mark.parametrize("entry", ["", "desktop-patrol.html", "splash.html"])
def test_entry_resources_load_from_existing_desktop_url(entry):
    app = FastAPI()
    app.mount("/desktop", StaticFiles(directory=str(DESKTOP_DIR), html=True))
    with TestClient(app) as client:
        page_url = f"/desktop/{entry}"
        response = client.get(page_url)
        assert response.status_code == 200
        page = PageResources()
        page.feed(response.text)
        assert page.urls
        pending = [(page_url, url) for url in page.urls]
        visited = set()
        while pending:
            parent, url = pending.pop()
            if url.startswith(("data:", "blob:", "#")):
                continue
            resolved = urljoin(parent, url)
            assert resolved.startswith("/desktop/"), (parent, url)
            if resolved in visited:
                continue
            visited.add(resolved)
            asset = client.get(resolved)
            assert asset.status_code == 200, resolved
            assert asset.content, resolved
            suffix = urlsplit(resolved).path.rsplit(".", 1)[-1]
            if suffix == "css":
                for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", asset.text):
                    pending.append((resolved, match[2]))
            elif suffix == "js":
                # Worker URLs resolve from the page; importScripts from the worker.
                for match in re.finditer(r"new Worker\(['\"]([^'\"]+)", asset.text):
                    pending.append((page_url, match[1]))
                for match in re.finditer(r"importScripts\(['\"]([^'\"]+)", asset.text):
                    pending.append((resolved, match[1]))
