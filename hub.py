# -*- coding: utf-8 -*-
"""
lan_screen_wall / hub.py
屏幕墙主控端 (服务端/查看端)

修复项:
  1. 帧率/画质设置记住当前值, 对话框默认显示当前值
  2. 帧率/画质控制帧正确下发到采集端
  3. 深蓝灰背景代替纯黑
  4. 标题显示本机 IP
  5. ≤4 台按田字格(2列), >4 台动态排布
  6. 去掉启动弹窗
  7. UDP 发现广播, 采集端自动发现连接
  8. 应用图标
"""

import argparse
import math
import os
import socket
import sys
import threading
import time

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt5.QtGui import QImage, QPixmap, QPainter, QColor, QFont, QIcon, QBrush
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout, QLabel,
    QStatusBar, QMenu, QAction, QInputDialog, QSizePolicy,
)

import protocol


def resource_path(filename):
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, filename)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def load_app_icon(png_name):
    """用 PNG 创建多尺寸 QIcon, 解决运行时图标偏小问题。"""
    icon = QIcon()
    pix = QPixmap(resource_path(png_name))
    if not pix.isNull():
        for size in [256, 128, 64, 48, 32, 16]:
            icon.addPixmap(pix.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    return icon


# ---------- UDP 发现广播线程 ----------

class DiscoveryBroadcaster(QThread):
    """每 2 秒向局域网广播主控端存在, 使采集端能自动发现并连接。"""

    def __init__(self, tcp_port, token):
        super().__init__()
        self.tcp_port = tcp_port
        self.token = token
        self._stop = threading.Event()

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        packet = protocol.pack_discovery(self.tcp_port, self.token)
        while not self._stop.is_set():
            try:
                sock.sendto(packet, ("<broadcast>", protocol.DISCOVERY_PORT))
            except OSError:
                pass
            self._stop.wait(2.0)
        sock.close()

    def stop(self):
        self._stop.set()


# ---------- TCP 服务线程 ----------

class HubServer(QThread):
    frame_received = pyqtSignal(str, str, bytes)
    client_joined = pyqtSignal(str, str)
    client_left = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, port, token):
        super().__init__()
        self.port = port
        self.token = token
        self._stop = threading.Event()
        self._srv = None
        self._clients = {}
        self._lock = threading.Lock()
        self._cid_seq = 0

    def run(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("0.0.0.0", self.port))
        self._srv.listen(64)
        self._srv.settimeout(1.0)
        self.log.emit(f"服务端已启动, 监听 0.0.0.0:{self.port}")
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle, args=(conn, addr), daemon=True)
            t.start()

    def _handle(self, conn, addr):
        ip = f"{addr[0]}:{addr[1]}"
        header = protocol.recv_handshake(conn, self.token)
        if header is None:
            self.log.emit(f"拒绝连接 {ip} (令牌错误或协议不符)")
            try:
                conn.close()
            except OSError:
                pass
            return
        with self._lock:
            self._cid_seq += 1
            cid = f"C{self._cid_seq:03d}"
            self._clients[cid] = conn
        hostname = header.get("hostname", ip)
        self.log.emit(f"[+] {hostname} ({ip}) 已上线  id={cid}")
        self.client_joined.emit(cid, hostname)
        try:
            while not self._stop.is_set():
                jpeg = protocol.recv_frame(conn)
                if jpeg is None:
                    break
                self.frame_received.emit(cid, hostname, jpeg)
        except Exception:
            pass
        finally:
            with self._lock:
                self._clients.pop(cid, None)
            try:
                conn.close()
            except OSError:
                pass
            self.client_left.emit(cid)
            self.log.emit(f"[-] {hostname} ({ip}) 已离线  id={cid}")

    def send_control(self, cid, cmd):
        with self._lock:
            conn = self._clients.get(cid)
        if conn:
            protocol.send_control(conn, cmd)

    def disconnect(self, cid):
        with self._lock:
            conn = self._clients.pop(cid, None)
        if conn:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass


# ---------- 屏幕瓦片 ----------

class ScreenTile(QLabel):
    def __init__(self, cid, hostname, parent_hub):
        super().__init__()
        self.cid = cid
        self.hostname = hostname
        self.hub = parent_hub
        self.last_pixmap = None
        self.last_ts = 0.0
        self.fps = 0.0
        self._frames = 0
        self._fps_t0 = time.time()
        # 记住当前设置的帧率/画质 (用于对话框默认值)
        self.target_fps = 8
        self.target_quality = 55
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(240, 150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet(
            "QLabel { background-color:#2d3548; color:#9aa8b8; "
            "border:2px solid #4a5a78; border-radius:6px; }"
        )
        self.setText(f"{hostname}\n等待画面...")

    def update_frame(self, jpeg: bytes):
        img = QImage.fromData(jpeg, "JPEG")
        if img.isNull():
            return
        self.last_pixmap = QPixmap.fromImage(img)
        self.setPixmap(
            self.last_pixmap.scaled(
                self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )
        self._frames += 1
        now = time.time()
        if now - self._fps_t0 >= 1.0:
            self.fps = self._frames / (now - self._fps_t0)
            self._frames = 0
            self._fps_t0 = now
        self.last_ts = now

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.last_pixmap is None:
            return
        p = QPainter(self)
        # 毛玻璃风格标题条: 半透明渐变背景
        from PyQt5.QtGui import QLinearGradient, QBrush
        grad = QLinearGradient(0, 0, 0, 28)
        grad.setColorAt(0, QColor(45, 53, 72, 200))
        grad.setColorAt(1, QColor(30, 36, 50, 160))
        p.fillRect(0, 0, self.width(), 28, QBrush(grad))
        p.setPen(QColor(255, 159, 67))
        p.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
        title = f"{self.hostname}  ({self.fps:.1f} fps)"
        p.drawText(10, 19, title)
        if time.time() - self.last_ts > 3.0 and self.last_pixmap:
            p.fillRect(self.rect(), QColor(0, 0, 0, 140))
            p.setPen(QColor(255, 80, 80))
            p.setFont(QFont("Microsoft YaHei", 14, QFont.Bold))
            p.drawText(self.rect(), Qt.AlignCenter, "信号中断")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.last_pixmap:
            self.setPixmap(
                self.last_pixmap.scaled(
                    self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
            )

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        a_fps = QAction("设置帧率...", self)
        a_q = QAction("设置画质...", self)
        a_disc = QAction("断开此终端", self)
        menu.addAction(a_fps)
        menu.addAction(a_q)
        menu.addSeparator()
        menu.addAction(a_disc)
        act = menu.exec_(self.mapToGlobal(e.pos()))
        if act is a_fps:
            v, ok = QInputDialog.getInt(
                self, "帧率", "目标 FPS (1-30):",
                value=self.target_fps, min=1, max=30,
            )
            if ok:
                self.target_fps = v
                self.hub.server.send_control(self.cid, {"fps": v})
        elif act is a_q:
            v, ok = QInputDialog.getInt(
                self, "画质", "JPEG 质量 (10-95):",
                value=self.target_quality, min=10, max=95,
            )
            if ok:
                self.target_quality = v
                self.hub.server.send_control(self.cid, {"quality": v})
        elif act is a_disc:
            self.hub.server.disconnect(self.cid)


# ---------- 网格布局计算 ----------

def calc_grid(n, fixed_cols):
    if fixed_cols > 0:
        cols = fixed_cols
    elif n <= 1:
        cols = 1
    elif n <= 4:
        cols = 2           # 田字格
    else:
        cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return rows, cols


# ---------- 主窗口 ----------

class HubWindow(QMainWindow):
    def __init__(self, port, token, cols):
        super().__init__()
        self.port = port
        self.token = token
        self.fixed_cols = cols
        self.tiles = {}
        self.local_ip = protocol.get_local_ip()
        self._build_ui()

        self.server = HubServer(port, token)
        self.server.frame_received.connect(self.on_frame)
        self.server.client_joined.connect(self.on_join)
        self.server.client_left.connect(self.on_left)
        self.server.log.connect(self.on_log)
        self.server.start()

        self.discovery = DiscoveryBroadcaster(port, token)
        self.discovery.start()

        self.watcher = QTimer(self)
        self.watcher.timeout.connect(self.refresh_tiles)
        self.watcher.start(1000)

    def _build_ui(self):
        self.setWindowTitle(f"局域网屏幕墙  {self.local_ip}:{self.port}")
        self.setWindowIcon(load_app_icon("hub.png"))
        self.resize(1280, 760)
        self.setStyleSheet("QMainWindow { background:#252d40; }")

        self.grid_widget = QWidget()
        self.grid = QGridLayout(self.grid_widget)
        self.grid.setSpacing(8)
        self.grid.setContentsMargins(8, 8, 8, 8)
        self.setCentralWidget(self.grid_widget)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.setStyleSheet("color:#ccc; background:#1e2538;")
        self.status.showMessage("等待终端连接...  (采集端将自动发现本机)")

    def relayout(self):
        n = len(self.tiles)
        # 先清除所有旧的行/列 stretch (关键: 否则列数减少时旧列仍占空间)
        for c in range(self.grid.columnCount()):
            self.grid.setColumnStretch(c, 0)
        for r in range(self.grid.rowCount()):
            self.grid.setRowStretch(r, 0)
        if n == 0:
            self.status.showMessage("等待终端连接...  (采集端将自动发现本机)")
            return
        rows, cols = calc_grid(n, self.fixed_cols)
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w:
                self.grid.removeWidget(w)
        for i, cid in enumerate(sorted(self.tiles.keys())):
            r, c = divmod(i, cols)
            self.grid.addWidget(self.tiles[cid], r, c)
        for c in range(cols):
            self.grid.setColumnStretch(c, 1)
        for r in range(rows):
            self.grid.setRowStretch(r, 1)
        self.status.showMessage(f"在线终端: {n}  (网格 {rows}x{cols})")

    def on_join(self, cid, hostname):
        tile = ScreenTile(cid, hostname, self)
        self.tiles[cid] = tile
        self.relayout()

    def on_left(self, cid):
        tile = self.tiles.pop(cid, None)
        if tile:
            self.grid.removeWidget(tile)
            tile.deleteLater()
        self.relayout()

    def on_frame(self, cid, hostname, jpeg):
        tile = self.tiles.get(cid)
        if tile:
            tile.update_frame(jpeg)

    def on_log(self, msg):
        self.status.showMessage(msg, 4000)

    def refresh_tiles(self):
        for tile in self.tiles.values():
            tile.update()

    def closeEvent(self, e):
        self.discovery.stop()
        self.server.stop()
        self.discovery.wait(1500)
        self.server.wait(1500)
        super().closeEvent(e)


def main():
    ap = argparse.ArgumentParser(description="局域网屏幕墙 - 主控端(查看端)")
    ap.add_argument("--port", type=int, default=5000, help="监听端口 (默认 5000)")
    ap.add_argument("--token", default="change-me", help="连接令牌, 需与采集端一致")
    ap.add_argument("--cols", type=int, default=0, help="固定列数, 0=自动 (默认 0)")
    args = ap.parse_args()

    app = QApplication(sys.argv)
    app.setWindowIcon(load_app_icon("hub.png"))
    win = HubWindow(args.port, args.token, args.cols)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
