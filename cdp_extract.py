"""cdp_extract.py - 用 Edge CDP 提取 opus 页面评论"""
import json
import socket
import struct
import time
import urllib.request


def ws_connect(ws_url):
    """极简 WebSocket 客户端连接"""
    # 解析 ws://127.0.0.1:9223/devtools/page/xxx
    host_port = ws_url.split("/")[2]
    host, port = host_port.split(":")
    path = "/" + "/".join(ws_url.split("/")[3:])

    sock = socket.create_connection((host, int(port)), timeout=30)

    # WebSocket 握手
    key = "x3JJHMbDL1EzLkh9GBhXDw=="
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(handshake.encode())

    # 读取握手响应
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += sock.recv(4096)

    if b"101" not in resp.split(b"\r\n")[0]:
        raise Exception("WebSocket handshake failed: " + resp.decode()[:200])

    return sock


def ws_send(sock, msg):
    """发送 WebSocket 文本帧"""
    data = json.dumps(msg).encode("utf-8")
    header = bytearray()
    header.append(0x81)  # FIN + text frame

    mask = True
    if mask:
        header.append(0x80 | len(data) if len(data) < 126 else (0x80 | 126))
        if len(data) >= 126:
            header += struct.pack(">H", len(data))
        mask_key = b"\x12\x34\x56\x78"
        header += mask_key
        masked = bytearray(data[i] ^ mask_key[i % 4] for i in range(len(data)))
        sock.sendall(bytes(header) + bytes(masked))
    else:
        header.append(len(data) if len(data) < 126 else 126)
        if len(data) >= 126:
            header += struct.pack(">H", len(data))
        sock.sendall(bytes(header) + data)


def ws_recv(sock, timeout=30):
    """接收一个 WebSocket 帧"""
    sock.settimeout(timeout)
    header = sock.recv(2)
    if len(header) < 2:
        raise Exception("No data")

    opcode = header[0] & 0x0F
    masked = (header[1] & 0x80) != 0
    length = header[1] & 0x7F

    if length == 126:
        ext = sock.recv(2)
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = sock.recv(8)
        length = struct.unpack(">Q", ext)[0]

    if masked:
        mask_key = sock.recv(4)

    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            break
        data += chunk

    if masked:
        data = bytearray(data[i] ^ mask_key[i % 4] for i in range(len(data)))

    if opcode == 1:  # text
        return json.loads(data.decode("utf-8"))
    elif opcode == 8:  # close
        raise Exception("WebSocket closed")
    return None


def cdp_call(sock, method, params=None, msg_id=1):
    """发送 CDP 命令并等待结果"""
    msg = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params
    ws_send(sock, msg)

    # 等待对应 id 的响应
    while True:
        resp = ws_recv(sock, timeout=30)
        if resp and resp.get("id") == msg_id:
            return resp
        # 忽略事件通知


def main():
    import yaml

    with open("config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    sessdata = cfg["bilibili"]["sessdata"]
    opus_id = cfg["opus_id"]

    # 1. 创建新 tab 导航到 opus 页面
    port = 9223
    tab_url = f"http://127.0.0.1:{port}/json/new?about:blank"
    req = urllib.request.Request(tab_url, method="PUT")
    resp = urllib.request.urlopen(req, timeout=10)
    tab = json.loads(resp.read())
    ws_url = tab["webSocketDebuggerUrl"]
    print("Tab created:", tab["id"])

    # 2. 连接 WebSocket
    sock = ws_connect(ws_url)
    print("WebSocket connected")

    # 3. 注入 cookie
    cdp_call(sock, "Network.enable", msg_id=1)
    cdp_call(
        sock,
        "Network.setCookie",
        {
            "name": "SESSDATA",
            "value": sessdata,
            "domain": ".bilibili.com",
            "path": "/",
        },
        msg_id=2,
    )
    print("Cookie injected")

    # 4. 导航到页面
    cdp_call(
        sock,
        "Page.navigate",
        {"url": f"https://www.bilibili.com/opus/{opus_id}"},
        msg_id=3,
    )
    print("Navigating...")

    # 5. 等待页面加载
    time.sleep(8)

    # 6. 滚动到底部触发评论加载
    cdp_call(
        sock,
        "Runtime.evaluate",
        {"expression": "window.scrollTo(0, document.body.scrollHeight); 'scrolled'"},
        msg_id=4,
    )
    print("Scrolled down")
    time.sleep(5)

    # 7. 提取评论
    js = """
    (function() {
        var results = [];
        // 找所有评论元素
        var items = document.querySelectorAll('[class*="reply"], [class*="comment"], [class*="Reply"], [class*="Comment']");
        results.push('items found: ' + items.length);
        for (var i = 0; i < Math.min(items.length, 5); i++) {
            results.push(items[i].className + ': ' + items[i].textContent.substring(0, 50));
        }
        // 也看 body 里有没有评论相关内容
        var bodyText = document.body.innerText;
        var replyIdx = bodyText.indexOf('回复');
        results.push('has 回复: ' + (replyIdx >= 0));
        if (replyIdx >= 0) {
            results.push('context: ' + bodyText.substring(replyIdx, replyIdx + 100));
        }
        return results.join('\\n');
    })()
    """
    result = cdp_call(
        sock, "Runtime.evaluate", {"expression": js, "returnByValue": True}, msg_id=5
    )
    value = result.get("result", {}).get("result", {}).get("value", "")
    print("Extract result:")
    print(value)

    # 8. 关闭
    sock.close()

    # 关闭 tab
    close_url = f"http://127.0.0.1:{port}/json/close/{tab['id']}"
    try:
        urllib.request.urlopen(close_url, timeout=5)
    except Exception:
        pass


if __name__ == "__main__":
    main()
