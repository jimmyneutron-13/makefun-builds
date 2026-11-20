import os, socket, sys
from urllib.parse import urlparse
proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
u = urlparse(proxy)
host, port = sys.argv[1], int(sys.argv[2])
s = socket.create_connection((u.hostname, u.port or 3128), timeout=30)
req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
if u.username:
    import base64
    cred = base64.b64encode(f"{u.username}:{u.password or ''}".encode()).decode()
    req += f"Proxy-Authorization: Basic {cred}\r\n"
req += "\r\n"
s.sendall(req.encode())
resp = b""
while b"\r\n\r\n" not in resp:
    chunk = s.recv(4096)
    if not chunk: break
    resp += chunk
if b" 200" not in resp.split(b"\r\n")[0]:
    sys.stderr.write("CONNECT failed: " + resp.split(b"\r\n")[0].decode(errors="replace") + "\n"); sys.exit(1)
# relay stdin/stdout <-> socket
import threading, select
def pump(a, b):
    try:
        while True:
            data = a.recv(65536) if hasattr(a, "recv") else os.read(a, 65536)
            if not data: break
            (b.sendall(data) if hasattr(b, "sendall") else os.write(b, data))
    except Exception: pass
t1 = threading.Thread(target=pump, args=(0, s), daemon=True)
t2 = threading.Thread(target=pump, args=(s, 1), daemon=True)
t1.start(); t2.start()
t1.join(); t2.join()
