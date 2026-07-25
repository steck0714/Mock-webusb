# -*- coding: utf-8 -*-
"""
chooser_dialog.py
==================
navigator.usb.requestDevice() のポリフィルが呼び出す、実USBデバイス選択ダイアログ。
pyusb（内部でlibusbというC言語ライブラリを使用）が列挙したデバイス情報の辞書リストを扱う。

This is the native "device picker" that WebUSBBridge.requestDeviceChooser() shows to the
user. It mirrors the real browser's chooser: the site only ever learns about the single
device the user explicitly picks, never the full list of connected devices.

Adapted from the `openweb` browser project (https://github.com/ ... see project README)
for standalone reuse. The only behavioural change from the original is that UI strings are
passed in directly instead of being looked up from a global CURRENT_LANG switch, so this
module has no dependency on any particular host application's i18n setup.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
)

#: Default (English) UI strings. Pass a dict with the same keys to `WebUsbDeviceChooserDialog`
#: (or to `install()` in polyfill.py, which forwards it) to localize.
DEFAULT_STRINGS = {
    "title": "Select a USB Device",
    "prompt": "This site wants to connect to a USB device:",
    "empty": "No USB devices found.",
    "cancel": "Cancel",
    "connect": "Connect",
}


class WebUsbDeviceChooserDialog(QDialog):
    """The native device-picker dialog shown for navigator.usb.requestDevice().

    `devices` is a list of the lightweight descriptor dicts built by
    `hardening.build_device_descriptor(..., include_configurations=False)` — i.e. already
    filtered by blocklist + `options.filters`/`exclusionFilters` before the user ever sees
    this dialog. The caller (WebUSBBridge.requestDeviceChooser) is responsible for that
    filtering; this dialog only presents whatever list it's handed and reports back the
    index the user picked.
    """

    def __init__(self, devices, parent=None, strings=None):
        super().__init__(parent)
        s = dict(DEFAULT_STRINGS)
        if strings:
            s.update(strings)
        self.selected_device = None
        self.setWindowTitle("🔌 " + s["title"])
        self.setMinimumWidth(420)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        info_label = QLabel(s["prompt"])
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color:#cdd6f4;font-size:12px;")
        layout.addWidget(info_label)

        self.device_list = QListWidget()
        self.device_list.setStyleSheet(
            "QListWidget{background:#1e1e2e;color:#cdd6f4;border:1px solid #45475a;border-radius:6px;}"
            "QListWidget::item{padding:8px;} QListWidget::item:selected{background:#89b4fa;color:#1e1e2e;}"
        )
        self._devices = devices
        if devices:
            for dev in devices:
                self.device_list.addItem(self._describe_device(dev))
            self.device_list.setCurrentRow(0)
        else:
            empty_item = QListWidgetItem(s["empty"])
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.device_list.addItem(empty_item)
        layout.addWidget(self.device_list)

        btn_row = QHBoxLayout()
        btn_cancel = QPushButton(s["cancel"])
        btn_connect = QPushButton(s["connect"])
        btn_connect.setEnabled(bool(devices))
        btn_connect.setStyleSheet(
            "background:#89b4fa;color:#1e1e2e;border:none;border-radius:6px;padding:8px 18px;font-weight:bold;")
        btn_cancel.setStyleSheet("background:#45475a;color:#cdd6f4;border:none;border-radius:6px;padding:8px 16px;")
        btn_cancel.clicked.connect(self.reject)
        btn_connect.clicked.connect(self._on_connect)
        btn_row.addWidget(btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(btn_connect)
        layout.addLayout(btn_row)

        self.setStyleSheet("QDialog{background:#0f172a;color:#e2e8f0;}")

    def _describe_device(self, dev: dict) -> str:
        name = dev.get("productName") or dev.get("manufacturerName")
        vid, pid = dev.get("vendorId"), dev.get("productId")
        vid_pid = f"VID:{vid:04x} PID:{pid:04x}" if isinstance(vid, int) and isinstance(pid, int) else ""
        if name and vid_pid:
            return f"{name}  ({vid_pid})"
        return name or vid_pid or "USB Device"

    def _on_connect(self):
        row = self.device_list.currentRow()
        if 0 <= row < len(self._devices):
            self.selected_device = self._devices[row]
            self.accept()
        else:
            self.reject()
