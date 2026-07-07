# -*- coding: utf-8 -*-
"""
lan_screen_wall / agent.py
采集端 (被控端) - 带 GUI 界面

功能:
  1. 小型 UI: 令牌/端口/主机/帧率/画质/显示器/最大宽度 配置
  2. 自动发现: 主机留空时, 通过 UDP 广播自动发现主控端并连接
  3. 断线自动重连 (通过发现或直连重试)
  4. 开机自启动选项 (写入/删除注册表 Run 键)
  5. 配置自动保存/加载
  6. 应用图标
"""

import io
import json
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QFormLayout, QHBoxLayout,
    QLineEdit, QSpinBox, QCheckBox, QPushButton, QLabel, QGroupBox,
    QMessageBox,
)

try:
    import mss
except ImportError:
    print("缺少依赖 mss, 请执行: pip install mss Pillow")
    raise

from PIL import Image

import protocol


# ---------- 资源路径 ----------

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


# ---------- 配置文件 ----------

def get_config_path():
    base = os.environ.get("APPDATA", os.path.expanduser("~"))
    d = os.path.join(base, "ScreenWallAgent")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "config.json")


def save_config(settings):
    try:
        with open(get_config_path(), "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_config():
    try:
        with open(get_config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ---------- 开机自启动 ----------

try:
    import winreg
    _AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _AUTOSTART_NAME = "ScreenWallAgent"

    def _autostart_command():
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        return f'pythonw "{os.path.abspath(__file__)}"'

    def is_autostart_enabled():
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY)
            winreg.QueryValueEx(key, _AUTOSTART_NAME)
            winreg.CloseKey(key)
            return True
        except (FileNotFoundError, OSError):
            return False

    def set_autostart(enabled):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE)
            if enabled:
                winreg.SetValueEx(key, _AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, _AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except OSError:
            pass
except ImportError:
    def is_autostart_enabled():
        return False
    def set_autostart(enabled):
        pass


# ---------- 采集工作线程 ----------

class AgentWorker(QThread):
    """后台线程: 屏幕采集 + 推流, 支持自动发现和直连两种模式。"""
    status_changed = pyqtSignal(str, str)  # level, message  level: info/success/error

    def __init__(self, token, port, host, fps, quality, max_width, monitor):
        super().__init__()
        self.token = token
        self.port = port
        self.host = host
        self.fps = fps
        self.quality = quality
        self.max_width = max_width
        self.monitor = monitor
        self._stop = threading.Event()
        self._sock = None
        self._ctl = {"fps": fps, "quality": quality}
        self._ctl_lock = threading.Lock()

    def run(self):
        if self.host:
            self._run_direct()
        else:
            self._run_discovery()

    # ----- 直连模式 -----
    def _run_direct(self):
        while not self._stop.is_set():
            self._connect_and_stream(self.host, self.port)
            if not self._stop.is_set():
                self.status_changed.emit("info", "连接断开, 5 秒后重试...")
                self._stop.wait(5)

    # ----- 自动发现模式 -----
    def _run_discovery(self):
        while not self._stop.is_set():
            # 第一步: 尝试 UDP 广播发现 (快速, 同子网)
            self.status_changed.emit("info", "正在搜索局域网内的屏幕墙主控端...")
            hub = self._discover_hub_udp(timeout=4)
            if hub is None:
                if self._stop.is_set():
                    return
                # 第二步: UDP 失败, 用 TCP 扫描发现 (跨子网)
                self.status_changed.emit("info", "广播未收到响应, 正在扫描附近子网...")
                hub = self._discover_hub_scan()
            if hub is None:
                if self._stop.is_set():
                    return
                self.status_changed.emit("info", "未找到主控端, 5 秒后重新搜索...")
                self._stop.wait(5)
                continue
            host, port = hub
            # 扫描发现时 socket 已建立并握手完成, 直接推流
            if self._sock is not None:
                self._stream_existing(host, port, self._sock)
            else:
                self._connect_and_stream(host, port)
            if not self._stop.is_set():
                self.status_changed.emit("info", "连接断开, 重新搜索主控端...")
                self._stop.wait(2)

    def _discover_hub_udp(self, timeout=4):
        """监听 UDP 广播, 返回 (ip, tcp_port) 或 None(超时/停止)。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", protocol.DISCOVERY_PORT))
        except OSError as e:
            self.status_changed.emit("error", f"无法监听发现端口 {protocol.DISCOVERY_PORT}: {e}")
            self._stop.wait(2)
            return None
        sock.settimeout(2.0)
        deadline = time.time() + timeout
        try:
            while not self._stop.is_set() and time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                except OSError:
                    break
                info = protocol.unpack_discovery(data)
                if info and info["token"] == self.token:
                    self.status_changed.emit("info", f"UDP 发现主控端 {addr[0]}:{info['tcp_port']}")
                    return (addr[0], info["tcp_port"])
        finally:
            sock.close()
        return None

    def _get_scan_subnets(self):
        """获取需要扫描的子网列表 (本机子网 + 常见 192.168 子网)。"""
        subnets = set()
        # 本机所在子网 (可能有多个网卡, 跨子网时尤其重要)
        local_ip = protocol.get_local_ip()
        parts = local_ip.split(".")
        if len(parts) == 4:
            subnets.add(f"{parts[0]}.{parts[1]}")
        # 尝试获取所有网卡 IP
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                p = ip.split(".")
                if len(p) == 4 and p[0] == "192" and p[1] == "168":
                    subnets.add(f"{p[0]}.{p[1]}")
        except Exception:
            pass
        # 常见 192.168 子网 (覆盖家用路由器默认网段)
        for i in range(0, 11):
            subnets.add(f"192.168.{i}")
        return sorted(subnets)

    def _probe_tcp(self, ip, port):
        """TCP 探测单个 IP:端口, 返回 True 如果是主控端。"""
        try:
            sock = socket.create_connection((ip, port), timeout=0.4)
            sock.settimeout(0.5)
            # 发送握手包验证是否是主控端 (用真实令牌, 成功就直接用)
            header = {
                "token": self.token,
                "hostname": socket.gethostname(),
                "monitor": self.monitor,
            }
            if protocol.send_handshake(sock, header):
                sock.settimeout(None)
                self._sock = sock
                return True
            else:
                sock.close()
        except (ConnectionRefusedError, socket.timeout, OSError):
            pass
        return False

    def _discover_hub_scan(self):
        """TCP 并发扫描多个子网, 找到主控端后直接返回已连接的 socket。"""
        subnets = self._get_scan_subnets()
        total = len(subnets) * 254
        self.status_changed.emit("info", f"扫描 {len(subnets)} 个子网 ({total} 个地址)...")
        
        for subnet in subnets:
            if self._stop.is_set():
                return None
            # 并发扫描一个子网的 254 个地址
            with ThreadPoolExecutor(max_workers=64) as pool:
                futures = {}
                for i in range(1, 255):
                    if self._stop.is_set():
                        return None
                    ip = f"{subnet}.{i}"
                    futures[pool.submit(self._probe_tcp, ip, self.port)] = ip
                for future in as_completed(futures):
                    if self._stop.is_set():
                        return None
                    ip = futures[future]
                    if future.result():
                        self.status_changed.emit("success", f"扫描发现主控端 {ip}:{self.port}")
                        return (ip, self.port)
        return None

    # ----- 连接并推流 -----
    def _connect_and_stream(self, host, port):
        self.status_changed.emit("info", f"正在连接 {host}:{port}...")
        try:
            sock = socket.create_connection((host, port), timeout=5)
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            self.status_changed.emit("error", f"连接失败: {e}")
            return
        sock.settimeout(None)
        self._sock = sock

        header = {
            "token": self.token,
            "hostname": socket.gethostname(),
            "monitor": self.monitor,
        }
        if not protocol.send_handshake(sock, header):
            self.status_changed.emit("error", "握手失败 (令牌错误或协议不符)")
            sock.close()
            return

        self._stream_existing(host, port, sock)

    def _stream_existing(self, host, port, sock):
        """复用已连接并握手完成的 socket, 直接开始推流。"""
        self.status_changed.emit("success", f"已连接 {host}:{port}, 开始推流")

        ctl_thread = threading.Thread(target=self._control_loop, args=(sock,), daemon=True)
        ctl_thread.start()

        try:
            with mss.mss() as sct:
                while not self._stop.is_set():
                    t0 = time.time()
                    try:
                        jpeg = self._grab_jpeg(sct)
                    except Exception as e:
                        self.status_changed.emit("error", f"抓屏失败: {e}")
                        break
                    if not protocol.send_frame(sock, jpeg):
                        self.status_changed.emit("error", "发送失败, 连接已断开")
                        break
                    with self._ctl_lock:
                        fps = self._ctl["fps"]
                    interval = 1.0 / fps
                    elapsed = time.time() - t0
                    if elapsed < interval:
                        self._stop.wait(interval - elapsed)
        except Exception as e:
            self.status_changed.emit("error", f"运行错误: {e}")
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None

    # ----- 控制参数接收 -----
    def _control_loop(self, sock):
        while not self._stop.is_set():
            try:
                head = protocol.recv_exactly(sock, 3)
                if head != protocol.CTL:
                    return
                ln = protocol.recv_exactly(sock, 4)
                if len(ln) != 4:
                    return
                raw = protocol.recv_exactly(sock, protocol.unpack_u32(ln))
                if not raw:
                    return
                cmd = json.loads(raw.decode("utf-8"))
                with self._ctl_lock:
                    if "fps" in cmd:
                        self._ctl["fps"] = max(1, min(30, int(cmd["fps"])))
                    if "quality" in cmd:
                        self._ctl["quality"] = max(10, min(95, int(cmd["quality"])))
                self.status_changed.emit("info", f"参数已更新: 帧率={self._ctl['fps']} 画质={self._ctl['quality']}")
            except Exception:
                return

    # ----- 屏幕抓取与编码 -----
    def _grab_jpeg(self, sct):
        mon = sct.monitors[self.monitor]
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        if self.max_width and img.width > self.max_width:
            new_h = int(img.height * self.max_width / img.width)
            img = img.resize((self.max_width, new_h), Image.BILINEAR)
        buf = io.BytesIO()
        with self._ctl_lock:
            q = self._ctl["quality"]
        img.save(buf, format="JPEG", quality=q)
        return buf.getvalue()

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


# ---------- UI 窗口 ----------

STYLE = """
QWidget { background:#1a1f2e; color:#e0e6ed; font-size:13px; }
QGroupBox { border:1px solid #3a4458; border-radius:6px; margin-top:10px; padding-top:14px; font-weight:bold; color:#58a6ff; }
QGroupBox::title { subcontrol-origin:margin; left:10px; }
QLineEdit, QSpinBox { background:#232936; border:1px solid #3a4458; border-radius:4px; padding:4px 6px; color:#e0e6ed; }
QLineEdit:focus, QSpinBox:focus { border:1px solid #58a6ff; }
QPushButton { background:#2d4a6e; border:1px solid #3a6ea5; border-radius:4px; padding:6px 18px; color:#fff; font-weight:bold; }
QPushButton:hover { background:#3a6ea5; }
QPushButton:disabled { background:#232936; color:#666; }
QPushButton#stopBtn { background:#6e2d2d; border-color:#a53a3a; }
QPushButton#stopBtn:hover { background:#a53a3a; }
QCheckBox { color:#e0e6ed; }
QCheckBox::indicator { width:16px; height:16px; }
QCheckBox::indicator:unchecked { background:#232936; border:1px solid #3a4458; border-radius:3px; }
QCheckBox::indicator:checked { background:#58a6ff; border:1px solid #58a6ff; border-radius:3px; }
"""

class AgentWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None
        cfg = load_config()
        self._build_ui(cfg)
        self._load_autostart()
        # 启动后自动连接 (延迟 500ms 让 UI 先显示)
        QTimer.singleShot(500, self._start)

    def _build_ui(self, cfg):
        self.setWindowTitle("局域网屏幕墙 - 采集端")
        self.setWindowIcon(load_app_icon("agent.png"))
        self.setFixedSize(360, 460)
        self.setStyleSheet(STYLE)

        layout = QVBoxLayout(self)

        # --- 连接设置 ---
        g1 = QGroupBox("连接设置")
        f1 = QFormLayout(g1)
        self.token_edit = QLineEdit(cfg.get("token", "change-me"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(cfg.get("port", 5000))
        self.host_edit = QLineEdit(cfg.get("host", ""))
        self.host_edit.setPlaceholderText("留空 = 自动发现主控端")

        f1.addRow("令牌:", self.token_edit)
        f1.addRow("端口:", self.port_spin)
        f1.addRow("主机IP:", self.host_edit)

        # --- 采集参数 ---
        g2 = QGroupBox("采集参数")
        f2 = QFormLayout(g2)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 30)
        self.fps_spin.setValue(cfg.get("fps", 8))
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(10, 95)
        self.quality_spin.setValue(cfg.get("quality", 55))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(0, 3840)
        self.width_spin.setValue(cfg.get("max_width", 1280))
        self.monitor_spin = QSpinBox()
        self.monitor_spin.setRange(1, 10)
        self.monitor_spin.setValue(cfg.get("monitor", 1))

        f2.addRow("帧率(FPS):", self.fps_spin)
        f2.addRow("画质:", self.quality_spin)
        f2.addRow("最大宽度:", self.width_spin)
        f2.addRow("显示器:", self.monitor_spin)

        # --- 选项 ---
        self.autostart_check = QCheckBox("开机自启动")
        self.autostart_check.toggled.connect(self._on_autostart_toggled)

        # --- 状态 ---
        self.status_label = QLabel("● 准备就绪")
        self.status_label.setStyleSheet("color:#8899aa; padding:6px; font-size:12px;")
        self.status_label.setWordWrap(True)

        # --- 按钮 ---
        btn_layout = QHBoxLayout()
        self.connect_btn = QPushButton("连接")
        self.stop_btn = QPushButton("断开")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.connect_btn)
        btn_layout.addWidget(self.stop_btn)

        layout.addWidget(g1)
        layout.addWidget(g2)
        layout.addWidget(self.autostart_check)
        layout.addWidget(self.status_label)
        layout.addLayout(btn_layout)

        self.connect_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)

    def _load_autostart(self):
        self.autostart_check.blockSignals(True)
        self.autostart_check.setChecked(is_autostart_enabled())
        self.autostart_check.blockSignals(False)

    def _on_autostart_toggled(self, checked):
        set_autostart(checked)
        self.status_label.setText(f"● 开机自启动 {'已开启' if checked else '已关闭'}")

    def _save_settings(self):
        save_config({
            "token": self.token_edit.text(),
            "port": self.port_spin.value(),
            "host": self.host_edit.text().strip(),
            "fps": self.fps_spin.value(),
            "quality": self.quality_spin.value(),
            "max_width": self.width_spin.value(),
            "monitor": self.monitor_spin.value(),
        })

    def _start(self):
        self._save_settings()
        self._set_fields_enabled(False)
        self.connect_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self.worker = AgentWorker(
            token=self.token_edit.text(),
            port=self.port_spin.value(),
            host=self.host_edit.text().strip(),
            fps=self.fps_spin.value(),
            quality=self.quality_spin.value(),
            max_width=self.width_spin.value(),
            monitor=self.monitor_spin.value(),
        )
        self.worker.status_changed.connect(self._on_status)
        self.worker.start()

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self.worker.wait(2000)
            self.worker = None
        self._set_fields_enabled(True)
        self.connect_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("● 已断开")

    def _set_fields_enabled(self, enabled):
        for w in [self.token_edit, self.port_spin, self.host_edit,
                  self.fps_spin, self.quality_spin, self.width_spin, self.monitor_spin]:
            w.setEnabled(enabled)

    def _on_status(self, level, msg):
        colors = {"info": "#8899aa", "success": "#3fb950", "error": "#f85149"}
        c = colors.get(level, "#8899aa")
        prefix = {"info": "●", "success": "●", "error": "●"}.get(level, "●")
        self.status_label.setText(f"{prefix} {msg}")
        self.status_label.setStyleSheet(f"color:{c}; padding:6px; font-size:12px;")

    def closeEvent(self, e):
        if self.worker:
            self.worker.stop()
            self.worker.wait(2000)
        self._save_settings()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setWindowIcon(load_app_icon("agent.png"))
    win = AgentWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
