"""Local page whose DOM reports the actual effects of native browser input."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

PAGE = b'''<!doctype html><html><head><meta charset="utf-8"><style>
input{position:absolute;left:20px;top:20px;width:300px;height:40px}
button{position:absolute;left:20px;top:90px;width:180px;height:40px}
output{position:absolute;left:20px;top:150px}body{height:1800px}
</style></head><body><input aria-label="Text"><button>Apply</button><output>0</output><script>
const name=location.pathname.slice(1), input=document.querySelector('input'), output=document.querySelector('output');
if(!localStorage.identity)localStorage.identity=crypto.randomUUID();
if(!document.cookie)document.cookie='identity='+crypto.randomUUID();
function report(){fetch('/report',{method:'POST',body:JSON.stringify({name,text:input.value,count:+output.textContent,cookie:document.cookie,storage:localStorage.identity})})}
input.addEventListener('input',report);document.querySelector('button').onclick=()=>{output.textContent=+output.textContent+1;report()};report();
</script></body></html>'''


class BrowserFixture:
    def __init__(self):
        self.records = []
        self.lock = threading.Lock()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/slow"):
                    time.sleep(1)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(PAGE)
            def do_POST(self):
                record = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with owner.lock:
                    owner.records.append(record)
                self.send_response(204)
                self.end_headers()
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
    def url(self, name):
        return f"http://127.0.0.1:{self.server.server_port}/{name}"
    def wait(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with self.lock:
                result = next((item for item in reversed(self.records) if predicate(item)), None)
            if result:
                return result
            time.sleep(.02)
        raise AssertionError(f"DOM result missing; records={self.records}")
    def close(self):
        self.server.shutdown()
        self.server.server_close()
