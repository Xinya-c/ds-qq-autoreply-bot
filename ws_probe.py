import socket, base64, os, struct, time, sys

host, port = "127.0.0.1", 3001
key = base64.b64encode(os.urandom(16)).decode()
req = (
    "GET / HTTP/1.1\r\n"
    "Host: %s:%d\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    "Sec-WebSocket-Key: %s\r\n"
    "Sec-WebSocket-Version: 13\r\n\r\n" % (host, port, key)
)
s = socket.create_connection((host, port), timeout=10)
s.sendall(req.encode())
buf = b""
while b"\r\n\r\n" not in buf:
    buf += s.recv(4096)
print("handshake:", buf.split(b"\r\n")[0].decode(errors="replace"))


def recvn(n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            return None
        b += c
    return b


t0 = time.time()
s.settimeout(45)
try:
    while time.time() - t0 < 40:
        hdr = recvn(2)
        if not hdr:
            print("[%.1fs] peer closed" % (time.time() - t0))
            break
        op = hdr[0] & 0x0F
        ln = hdr[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", recvn(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", recvn(8))[0]
        payload = recvn(ln) or b""
        preview = payload[:160].decode("utf-8", "replace").replace("\n", " ")
        print("[%5.1fs] opcode=%d len=%d %s" % (time.time() - t0, op, ln, preview))
except socket.timeout:
    print("[timeout] 40s no data")
except Exception as e:
    print("[error]", type(e).__name__, e)
finally:
    try:
        s.close()
    except Exception:
        pass
