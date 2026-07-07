# -*- coding: utf-8 -*-
"""
lan_screen_wall / protocol.py
通信协议 + UDP 局域网发现

帧格式:
  握手(客户端->服务端): MAGIC(4) | header_len(4,BE) | header_json
  握手应答: b"OK"(2) / b"NO"(2)
  图像帧: frame_len(4,BE) | jpeg_bytes
  控制帧: b"CTL"(3) | cmd_len(4,BE) | cmd_json

UDP 发现:
  主控端每 2s 广播: MAGIC(4) | b"HUB" | tcp_port(4,BE) | token(utf8)
  采集端监听 DISCOVERY_PORT, 收到后提取发送方 IP + tcp_port, 发起 TCP 连接
"""

import json
import socket
import struct

MAGIC = b"LSW1"
OK = b"OK"
NO = b"NO"
CTL = b"CTL"

DISCOVERY_PORT = 5001
HUB_TAG = b"HUB"


# ---------- 工具函数 ----------

def recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (ConnectionResetError, OSError):
            return b""
        if not chunk:
            return b""
        buf.extend(chunk)
    return bytes(buf)


def send_exactly(sock: socket.socket, data: bytes) -> bool:
    try:
        sock.sendall(data)
        return True
    except (ConnectionResetError, OSError):
        return False


def pack_u32(value: int) -> bytes:
    return struct.pack(">I", value)


def unpack_u32(data: bytes) -> int:
    return struct.unpack(">I", data)[0]


# ---------- TCP 握手 / 帧收发 ----------

def send_handshake(sock: socket.socket, header: dict) -> bool:
    raw = json.dumps(header, ensure_ascii=False).encode("utf-8")
    pkt = MAGIC + pack_u32(len(raw)) + raw
    if not send_exactly(sock, pkt):
        return False
    resp = recv_exactly(sock, 2)
    return resp == OK


def recv_handshake(sock: socket.socket, expected_token: str) -> dict | None:
    magic = recv_exactly(sock, 4)
    if magic != MAGIC:
        send_exactly(sock, NO)
        return None
    ln = recv_exactly(sock, 4)
    if len(ln) != 4:
        return None
    raw = recv_exactly(sock, unpack_u32(ln))
    if not raw:
        return None
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        send_exactly(sock, NO)
        return None
    if header.get("token") != expected_token:
        send_exactly(sock, NO)
        return None
    send_exactly(sock, OK)
    return header


def send_frame(sock: socket.socket, jpeg: bytes) -> bool:
    return send_exactly(sock, pack_u32(len(jpeg)) + jpeg)


def recv_frame(sock: socket.socket) -> bytes | None:
    ln = recv_exactly(sock, 4)
    if len(ln) != 4:
        return None
    size = unpack_u32(ln)
    if size == 0 or size > 64 * 1024 * 1024:
        return None
    data = recv_exactly(sock, size)
    return data if len(data) == size else None


def send_control(sock: socket.socket, cmd: dict) -> bool:
    raw = json.dumps(cmd, ensure_ascii=False).encode("utf-8")
    return send_exactly(sock, CTL + pack_u32(len(raw)) + raw)


# ---------- UDP 局域网发现 ----------

def pack_discovery(tcp_port: int, token: str) -> bytes:
    """构造主控端广播包。"""
    return MAGIC + HUB_TAG + pack_u32(tcp_port) + token.encode("utf-8")


def unpack_discovery(data: bytes) -> dict | None:
    """解析广播包, 返回 {tcp_port, token} 或 None。"""
    if len(data) < 4 + 3 + 4:
        return None
    if data[:4] != MAGIC or data[4:7] != HUB_TAG:
        return None
    tcp_port = unpack_u32(data[7:11])
    token = data[11:].decode("utf-8", errors="replace")
    return {"tcp_port": tcp_port, "token": token}


def get_local_ip() -> str:
    """获取本机局域网 IP。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
