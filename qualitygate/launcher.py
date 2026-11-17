"""Desktop launcher: only open a browser after an actual loopback health check."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener
import webbrowser

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/".qualitygate-desktop"
OPENER=build_opener(ProxyHandler({}))


def healthy(port):
    try:
        with OPENER.open(f"http://127.0.0.1:{port}/api/health",timeout=.4) as response:
            body=json.load(response)
            return body.get("status")=="ok" and body.get("mode")=="local" and body.get("version")=="1.0.0" and body.get("worker_alive") is True
    except (OSError,ValueError):
        return False


def available(port):
    try:
        with socket.socket() as sock:
            if hasattr(socket,"SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            sock.bind(("127.0.0.1",port))
        return True
    except OSError:
        return False


def main():
    if sys.version_info<(3,11):
        raise SystemExit("Python 3.11 or newer is required.")
    DATA.mkdir(exist_ok=True)
    port_file=DATA/"port.txt"
    if port_file.exists():
        try:previous=int(port_file.read_text().strip())
        except ValueError:previous=0
        if 1024<=previous<=65535 and healthy(previous):
            print(f"QualityGate is already ready: http://127.0.0.1:{previous}")
            webbrowser.open(f"http://127.0.0.1:{previous}")
            return
    # Skip legacy sandbox servers and never claim that a port is ready merely
    # because a process was started. Use a separate desktop database directory.
    port=next((p for p in range(8877,8897) if available(p)),None)
    if port is None:
        raise SystemExit("Ports 8877-8896 are busy. Close an old QualityGate window and retry.")
    log_path=DATA/"server.log"
    print(f"Starting QualityGate with {sys.executable}",flush=True)
    print(f"Checking http://127.0.0.1:{port} ...",flush=True)
    with log_path.open("w",encoding="utf-8") as log:
        child=subprocess.Popen([sys.executable,"-m","qualitygate","--demo","--port",str(port),"--data-dir",str(DATA)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        try:
            ready=False
            deadline=time.monotonic()+20
            while time.monotonic()<deadline and child.poll() is None:
                if healthy(port):
                    ready=True
                    break
                time.sleep(.2)
            if not ready:
                if child.poll() is None:
                    child.terminate();child.wait(timeout=5)
                print("\nStartup/connection check failed. The browser was NOT opened.")
                print(f"Log: {log_path}")
                print(log_path.read_text(encoding="utf-8",errors="replace")[-5000:])
                print("Run this launcher from Windows File Explorer, outside the Codex preview tab.")
                return 1
            port_file.write_text(str(port),encoding="ascii")
            url=f"http://127.0.0.1:{port}"
            print(f"\nREADY: {url}\nKeep this window open. Press Ctrl+C to stop.\n",flush=True)
            webbrowser.open(url)
            return child.wait()
        except KeyboardInterrupt:
            if child.poll() is None:
                child.terminate();child.wait(timeout=5)
            print("QualityGate stopped. Saved data will be kept.")
            return 0


if __name__=="__main__":sys.exit(main())
