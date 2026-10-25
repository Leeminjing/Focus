"""Short-lived launch parent. Prints only a bootstrap endpoint, never credentials."""
import json
import queue
import subprocess
import sys
import threading


def main():
    # Refuse a job-bound fallback: it cannot promise survival on backend exit.
    host = subprocess.Popen([sys.argv[1], '--reconnect'], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    lines = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: lines.put(host.stdout.readline(16385)), daemon=True).start()
    try:
        ready = json.loads(lines.get(timeout=10))
        if ready.get('transport') != 'named_pipe' or ready.get('protocol_version') != 1 or ready.get('pid') != host.pid:
            raise RuntimeError('Selected browser does not support authenticated recovery')
        print(json.dumps(ready), flush=True)
    except BaseException:
        # This launcher has not handed out an authenticated connection or
        # created any pages. Only its own empty child can be cleaned up here.
        if host.poll() is None: host.terminate()
        host.wait(5)
        raise
    finally:
        host.stdout.close(); host.stderr.close()


if __name__ == '__main__':
    main()
