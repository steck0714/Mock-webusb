# -*- coding: utf-8 -*-
"""
chooser_dialog.py
==================
navigator.usb.requestDevice() のポリフィルが呼び出す、実USBデバイス選択ダイアログ。
pyusb（内部でlibusbというC言語ライブラリを使用）が列挙したデバイス情報の辞書リストを扱う。

Chrome自身の実際のWebUSBチューザー(developer.chrome.com/docs/capabilities/usb や
support.google.com/chrome/answer/12576972 に記載の実際の操作フロー)を参考に、
以下の挙動を踏襲している:
  - どのサイトがデバイスへの接続を求めているか(オリジン)を必ず明示する
    ("{origin} wants to connect to a USB device" という文言。Chromeも常に
    要求元のサイト名を明示する)
  - 「信頼できるサイトにだけ許可するように」という注意書きを表示する
  - デバイスを明示的に選択するまでConnectボタンは無効(先頭行を自動選択しない。
    複数デバイスが並んでいる時に誤って先頭を確定してしまう事故を防ぐ、
    Chromeと同じ安全側の挙動)
  - ダイアログを開いたまま新しいデバイスが挿された場合、一覧がライブ更新される
    (refresh_callbackが渡された場合のみ。Chromeのチューザーは開いたまま
    新規デバイスを挿すと一覧に追加されることを踏まえた挙動)

This is the native "device picker" that WebUSBBridge.requestDeviceChooser() shows to the
user. It mirrors the real browser's chooser: the site only ever learns about the single
device the user explicitly picks, never the full list of connected devices.
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QWidget, QSizePolicy,
)

#: Default (English) UI strings. Pass a dict with the same keys to `WebUsbDeviceChooserDialog`
#: (or to `install()` in polyfill.py, which forwards it) to localize.
#: `heading` is a str.format() template with an `{origin}` placeholder.
DEFAULT_STRINGS = {
    "title": "Select a USB Device",
    "heading": "{origin} wants to connect to a USB device",
    "heading_no_origin": "This page wants to connect to a USB device",
    "trust_reminder": "Only connect devices from sites you trust.",
    "empty": "No compatible devices found.",
    "cancel": "Cancel",
    "connect": "Connect",
}


def _device_name_and_detail(dev: dict):
    """(主表示名, 副次的な技術詳細)のタプルを返す。Chromeが製品名を主表示にし、
    VID/PID等はあくまで補助情報として扱っているのに合わせた表示階層。"""
    name = dev.get("productName") or dev.get("manufacturerName")
    vid, pid = dev.get("vendorId"), dev.get("productId")
    vid_pid = f"VID:{vid:04x} PID:{pid:04x}" if isinstance(vid, int) and isinstance(pid, int) else ""
    if not name:
        return (vid_pid or "Unknown device", "")
    return (name, vid_pid)


class _DeviceRowWidget(QWidget):
    """一覧の1行。製品名を主(太字)、VID/PID等を副次(小さく薄い色)で
    縦に並べる。プレーンテキストの1行表示よりも、実機を複数繋いだ状態での
    見分けやすさが上がる。"""

    def __init__(self, dev: dict, parent=None):
        super().__init__(parent)
        name, detail = _device_name_and_detail(dev)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        name_label = QLabel(name)
        name_label.setStyleSheet("color:#e2e8f0;font-size:13px;font-weight:600;")
        layout.addWidget(name_label)
        if detail:
            detail_label = QLabel(detail)
            detail_label.setStyleSheet("color:#8b93a7;font-size:11px;")
            layout.addWidget(detail_label)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class WebUsbDeviceChooserDialog(QDialog):
    """The native device-picker dialog shown for navigator.usb.requestDevice().

    `devices` is a list of the lightweight descriptor dicts built by
    `hardening.build_device_descriptor(..., include_configurations=False)` — i.e. already
    filtered by blocklist + `options.filters`/`exclusionFilters` before the user ever sees
    this dialog. The caller (WebUSBBridge.requestDeviceChooser) is responsible for that
    filtering; this dialog only presents whatever list it's handed and reports back the
    device the user picked.

    origin: the requesting site's origin (e.g. "https://example.com"), read independently
        from the page's actual URL — never trust a value the page could have supplied itself.
        Shown prominently, the same way Chrome always names the requesting site.
    refresh_callback: optional zero-argument callable returning a fresh list of device dicts
        in the same shape as `devices`. If given, the dialog polls it periodically and live-
        updates the list (matching Chrome's chooser, which picks up newly-plugged-in devices
        without needing to be reopened) while preserving the current selection where possible.
    """

    REFRESH_INTERVAL_MS = 1500  # 他のホットプラグ監視(UsbHotplugWatcher)と揃えた間隔

    def __init__(self, devices, parent=None, strings=None, origin=None, refresh_callback=None):
        super().__init__(parent)
        s = dict(DEFAULT_STRINGS)
        if strings:
            s.update(strings)
        self._strings = s
        self.selected_device = None
        self._refresh_callback = refresh_callback
        self.setWindowTitle("🔌 " + s["title"])
        self.setMinimumWidth(440)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)

        heading_text = s["heading"].format(origin=origin) if origin else s["heading_no_origin"]
        heading_label = QLabel(heading_text)
        heading_label.setWordWrap(True)
        heading_label.setStyleSheet("color:#e2e8f0;font-size:14px;font-weight:600;")
        layout.addWidget(heading_label)

        trust_label = QLabel(s["trust_reminder"])
        trust_label.setWordWrap(True)
        trust_label.setStyleSheet("color:#8b93a7;font-size:11px;")
        layout.addWidget(trust_label)
        layout.addSpacing(4)

        self.device_list = QListWidget()
        self.device_list.setStyleSheet(
            "QListWidget{background:#1e1e2e;color:#cdd6f4;border:1px solid #45475a;border-radius:6px;}"
            "QListWidget::item{padding:2px;} QListWidget::item:selected{background:#39435c;}"
        )
        self.device_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.device_list.itemDoubleClicked.connect(lambda _item: self._on_connect())
        layout.addWidget(self.device_list)

        btn_row = QHBoxLayout()
        self.btn_cancel = QPushButton(s["cancel"])
        self.btn_connect = QPushButton(s["connect"])
        self.btn_connect.setStyleSheet(
            "QPushButton{background:#89b4fa;color:#1e1e2e;border:none;border-radius:6px;"
            "padding:8px 18px;font-weight:bold;}"
            "QPushButton:disabled{background:#45475a;color:#8b93a7;}"
        )
        self.btn_cancel.setStyleSheet("background:#45475a;color:#cdd6f4;border:none;border-radius:6px;padding:8px 16px;")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_connect.clicked.connect(self._on_connect)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_connect)
        layout.addLayout(btn_row)

        self.setStyleSheet("QDialog{background:#0f172a;color:#e2e8f0;}")

        # 🛡️ Chromeと同様、明示的にユーザーが1台選ぶまでConnectは押せない
        # (複数デバイスが並んでいる時に「先頭が暫定選択された状態」のまま
        # 誤ってConnectを押してしまう事故を防ぐ)。
        self._devices = []
        self._set_devices(devices)

        if refresh_callback is not None:
            self._refresh_timer = QTimer(self)
            self._refresh_timer.setInterval(self.REFRESH_INTERVAL_MS)
            self._refresh_timer.timeout.connect(self._poll_refresh)
            self._refresh_timer.start()
        else:
            self._refresh_timer = None

    # ---- デバイス一覧の構築・更新 ----

    @staticmethod
    def _device_identity(dev: dict):
        """同一デバイス判定用のキー。シリアル番号があればそれを、無ければ
        vendorId/productIdの組で代用する(同一機種を複数挿している場合の
        区別はできないが、ライブ更新時に選択状態を保つ目的には十分)。"""
        serial = dev.get("serialNumber")
        if serial:
            return ("serial", serial)
        return ("vidpid", dev.get("vendorId"), dev.get("productId"))

    def _set_devices(self, devices):
        previously_selected = self._identity_of_selected()
        self._devices = list(devices or [])
        self.device_list.clear()
        if self._devices:
            for dev in self._devices:
                item = QListWidgetItem()
                row_widget = _DeviceRowWidget(dev)
                item.setSizeHint(row_widget.sizeHint())
                self.device_list.addItem(item)
                self.device_list.setItemWidget(item, row_widget)
        else:
            empty_item = QListWidgetItem(self._strings["empty"])
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.device_list.addItem(empty_item)

        # ライブ更新後、以前選ばれていたのと同じデバイスがまだ一覧にあれば
        # 選択状態を復元する(無関係なデバイスの抜き差しで選択が消えて
        # ユーザー体験が悪くなるのを防ぐ)。
        restored = False
        if previously_selected is not None:
            for row, dev in enumerate(self._devices):
                if self._device_identity(dev) == previously_selected:
                    self.device_list.setCurrentRow(row)
                    restored = True
                    break
        if not restored:
            self.device_list.setCurrentRow(-1)  # Chromeと同様、自動選択はしない
        self._on_selection_changed()

    def _identity_of_selected(self):
        row = self.device_list.currentRow()
        if 0 <= row < len(self._devices):
            return self._device_identity(self._devices[row])
        return None

    def _poll_refresh(self):
        try:
            fresh = self._refresh_callback()
        except Exception:
            return  # 取得失敗時は何もしない(次回のポーリングに任せる)
        self._set_devices(fresh)

    # ---- 選択・確定 ----

    def _on_selection_changed(self):
        row = self.device_list.currentRow()
        self.btn_connect.setEnabled(0 <= row < len(self._devices))

    def _on_connect(self):
        row = self.device_list.currentRow()
        if 0 <= row < len(self._devices):
            self.selected_device = self._devices[row]
            self.accept()
        else:
            self.reject()

    def closeEvent(self, event):
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
        super().closeEvent(event)
