"""Owned local downloads/permission page; never sends data to an external site."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.parse import urlsplit

FILE_CONTENT = b"Focus native browser v0.5\r\n" * 4096
SITE_PAGE = b'''<!doctype html><meta charset="utf-8"><title>Focus v0.5 site workflow</title>
<style>body{font:16px sans-serif}a,button,input{position:absolute;left:20px;width:320px;height:40px}
#normal{top:20px}#slow{top:80px}#permission{top:140px}#text{top:220px}</style>
<a id="normal" href="/file">Download normal file</a>
<a id="slow" href="/slow-file">Download slow file</a>
<button id="permission" onclick="navigator.geolocation.getCurrentPosition(()=>document.title='Permission allowed',()=>document.title='Permission ended')">Request location</button>
<input id="text" aria-label="Independent input" oninput="document.title='Independent: '+this.value">'''


class SiteFixture:
    def __init__(self):
        self.requests = []
        self.stopped = threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                owner.requests.append(path)
                content = FILE_CONTENT * (20 if path == "/slow-file" else 1)
                file = path in {"/file", "/slow-file"}
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream" if file else "text/html; charset=utf-8")
                if file:
                    self.send_header("Content-Disposition", 'attachment; filename="focus-v05.txt"')
                    self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                try:
                    if not file:
                        self.wfile.write(SITE_PAGE)
                    else:
                        for offset in range(0, len(content), 8192):
                            if owner.stopped.is_set():
                                break
                            self.wfile.write(content[offset:offset + 8192]); self.wfile.flush()
                            if path == "/slow-file":
                                time.sleep(.1)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path="/"):
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def close(self):
        self.stopped.set()
        self.server.shutdown(); self.server.server_close()
