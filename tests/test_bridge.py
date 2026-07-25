# -*- coding: utf-8 -*-
"""WebUSBBridge.requestDeviceChooser()を、フェイクのpyusbデバイス・フェイクの
チューザーダイアログに差し替えた上で実際に呼び出して検証する統合テスト。
実USBデバイス・実GUI操作なしで、options.filters/exclusionFiltersによる
絞り込みと、選択後のリッチな記述子再構築を確認する。"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtWidgets import QApplication

from pyside6_webusb.bridge import WebUSBBridge

_app = QApplication.instance() or QApplication([])


# --- test_hardening.py と同じ形のフェイクpyusbオブジェクト ---
class FakeEndpoint:
    def __init__(self, address, attributes, max_packet=64):
        self.bEndpointAddress = address
        self.bmAttributes = attributes
        self.wMaxPacketSize = max_packet


class FakeInterface:
    def __init__(self, number, alt, iclass, isub, iproto, endpoints):
        self.bInterfaceNumber = number
        self.bAlternateSetting = alt
        self.bInterfaceClass = iclass
        self.bInterfaceSubClass = isub
        self.bInterfaceProtocol = iproto
        self._endpoints = endpoints

    def __iter__(self):
        return iter(self._endpoints)


class FakeConfiguration:
    def __init__(self, value, interfaces):
        self.bConfigurationValue = value
        self._interfaces = interfaces

    def __iter__(self):
        return iter(self._interfaces)


class FakeDevice:
    def __init__(self, idVendor, idProduct, configurations,
                 deviceClass=0, deviceSubClass=0, deviceProtocol=0,
                 bcdUSB=0x0200, bcdDevice=0x0100,
                 iManufacturer=1, iProduct=2, iSerialNumber=3):
        self.idVendor = idVendor
        self.idProduct = idProduct
        self._configurations = configurations
        self.bDeviceClass = deviceClass
        self.bDeviceSubClass = deviceSubClass
        self.bDeviceProtocol = deviceProtocol
        self.bcdUSB = bcdUSB
        self.bcdDevice = bcdDevice
        self.iManufacturer = iManufacturer
        self.iProduct = iProduct
        self.iSerialNumber = iSerialNumber

    def __iter__(self):
        return iter(self._configurations)


class FakeUsbUtil:
    STRINGS = {1: "Acme Corp", 2: "Acme Widget", 3: "SN-0001"}

    def get_string(self, dev, index):
        return self.STRINGS.get(index)


class FakeUsbCore:
    def __init__(self, devices):
        self._devices = devices

    def find(self, find_all=False, idVendor=None, idProduct=None, **kw):
        if find_all:
            return list(self._devices)
        for d in self._devices:
            if (idVendor is None or d.idVendor == idVendor) and (idProduct is None or d.idProduct == idProduct):
                return d
        return None


class FakeChooserDialog:
    """実QDialogの代わり。SELECT_INDEXで「一覧の何番目を選んだか」を制御する
    (Noneなら「キャンセルした」を意味する)。"""
    class DialogCode:
        Accepted = 1
        Rejected = 0

    SELECT_INDEX = 0
    last_devices_info = None  # ダイアログへ実際に渡された(=絞り込み後の)一覧を記録しておく

    def __init__(self, devices_info, parent, strings=None):
        FakeChooserDialog.last_devices_info = devices_info
        self.devices_info = devices_info
        idx = FakeChooserDialog.SELECT_INDEX
        self.selected_device = devices_info[idx] if (idx is not None and idx < len(devices_info)) else None

    def exec(self):
        return self.DialogCode.Accepted if self.selected_device is not None else self.DialogCode.Rejected


def make_bridge(devices):
    """QWebChannel配線なしで、pyusb部分だけをフェイクに差し替えたWebUSBBridgeを作る。"""
    bridge = WebUSBBridge()
    bridge._pyusb = lambda: (FakeUsbCore(devices), FakeUsbUtil())
    bridge._load_known_devices = lambda: []
    bridge._record_device_usage = lambda *a, **kw: None
    grants = []
    bridge._grant = lambda origin, vid, pid: grants.append((origin, vid, pid))
    bridge._current_origin = lambda: "https://example.test"
    bridge.__test_grants__ = grants
    return bridge


def test_filters_narrow_the_candidate_list(monkeypatch):
    dev_a = FakeDevice(0x2341, 0x8036, [FakeConfiguration(1, [])])
    dev_b = FakeDevice(0x1234, 0x0002, [FakeConfiguration(1, [])])
    bridge = make_bridge([dev_a, dev_b])
    monkeypatch.setattr("pyside6_webusb.bridge.WebUsbDeviceChooserDialog", FakeChooserDialog)
    FakeChooserDialog.SELECT_INDEX = 0
    result = json.loads(bridge.requestDeviceChooser(json.dumps({"filters": [{"vendorId": 0x2341}]})))
    assert result["cancelled"] is False
    assert result["device"]["vendorId"] == 0x2341
    assert len(FakeChooserDialog.last_devices_info) == 1  # dev_bは候補から除外されているはず
    print("test_filters_narrow_the_candidate_list: OK")


def test_empty_filters_array_matches_nothing(monkeypatch):
    dev_a = FakeDevice(0x2341, 0x8036, [FakeConfiguration(1, [])])
    bridge = make_bridge([dev_a])
    monkeypatch.setattr("pyside6_webusb.bridge.WebUsbDeviceChooserDialog", FakeChooserDialog)
    FakeChooserDialog.SELECT_INDEX = 0
    # 仕様どおり filters: [] (空配列)は「一致するものなし」
    result = json.loads(bridge.requestDeviceChooser(json.dumps({"filters": []})))
    assert result["cancelled"] is True
    print("test_empty_filters_array_matches_nothing: OK")


def test_exclusion_filters_remove_a_match(monkeypatch):
    dev_a = FakeDevice(0x2341, 0x8036, [FakeConfiguration(1, [])])
    bridge = make_bridge([dev_a])
    monkeypatch.setattr("pyside6_webusb.bridge.WebUsbDeviceChooserDialog", FakeChooserDialog)
    FakeChooserDialog.SELECT_INDEX = 0
    result = json.loads(bridge.requestDeviceChooser(json.dumps({
        "filters": [{}], "exclusionFilters": [{"vendorId": 0x2341}],
    })))
    assert result["cancelled"] is True
    print("test_exclusion_filters_remove_a_match: OK")


def test_selected_device_gets_rich_descriptor(monkeypatch):
    cfg = FakeConfiguration(1, [FakeInterface(0, 0, 0xFF, 0, 0, [])])
    dev_a = FakeDevice(0x2341, 0x8036, [cfg])
    bridge = make_bridge([dev_a])
    monkeypatch.setattr("pyside6_webusb.bridge.WebUsbDeviceChooserDialog", FakeChooserDialog)
    FakeChooserDialog.SELECT_INDEX = 0
    result = json.loads(bridge.requestDeviceChooser(json.dumps({"filters": [{}]})))
    assert result["cancelled"] is False
    # チューザー一覧構築時は軽量記述子だが、選ばれた1台はgetDevices()と同じ
    # リッチな記述子(configurations付き)で返るはず
    assert len(result["device"]["configurations"]) == 1
    print("test_selected_device_gets_rich_descriptor: OK")


def test_grant_recorded_only_on_selection(monkeypatch):
    dev_a = FakeDevice(0x2341, 0x8036, [FakeConfiguration(1, [])])
    bridge = make_bridge([dev_a])
    monkeypatch.setattr("pyside6_webusb.bridge.WebUsbDeviceChooserDialog", FakeChooserDialog)

    FakeChooserDialog.SELECT_INDEX = None
    result = json.loads(bridge.requestDeviceChooser(json.dumps({"filters": [{}]})))
    assert result["cancelled"] is True
    assert bridge.__test_grants__ == []

    FakeChooserDialog.SELECT_INDEX = 0
    result = json.loads(bridge.requestDeviceChooser(json.dumps({"filters": [{}]})))
    assert result["cancelled"] is False
    assert bridge.__test_grants__ == [("https://example.test", 0x2341, 0x8036)]
    print("test_grant_recorded_only_on_selection: OK")


def test_settings_fallback_uses_constructor_organization_and_application():
    bridge = WebUSBBridge(settings_organization="Acme", settings_application="Widget")
    s = bridge._known_device_settings()
    assert s is not None
    assert s.organizationName() == "Acme"
    assert s.applicationName() == "Widget"
    print("test_settings_fallback_uses_constructor_organization_and_application: OK")


if __name__ == "__main__":
    class _FakeMonkeypatch:
        """pytestなしでも走らせられるよう、monkeypatch.setattr相当を素朴に実装したもの。"""
        def setattr(self, target, value):
            module_path, attr = target.rsplit(".", 1)
            import importlib
            mod = importlib.import_module(module_path)
            setattr(mod, attr, value)

    mp = _FakeMonkeypatch()
    test_filters_narrow_the_candidate_list(mp)
    test_empty_filters_array_matches_nothing(mp)
    test_exclusion_filters_remove_a_match(mp)
    test_selected_device_gets_rich_descriptor(mp)
    test_grant_recorded_only_on_selection(mp)
    test_settings_fallback_uses_constructor_organization_and_application()
    print("ALL BRIDGE TESTS PASSED")
