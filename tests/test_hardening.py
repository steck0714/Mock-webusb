# -*- coding: utf-8 -*-
"""実USBデバイスが無い環境でも pyside6_webusb.hardening のロジックを検証するための
モックベース単体テスト。pyusbのDevice/Configuration/Interface/Endpointの
「構造(イテレーション形状と属性名)」だけを模倣する。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pyside6_webusb import hardening as h


class FakeEndpoint:
    def __init__(self, address, attributes, max_packet=64):
        self.bEndpointAddress = address
        self.bmAttributes = attributes
        self.wMaxPacketSize = max_packet


class FakeInterface:
    def __init__(self, number, alt, iclass, isub, iproto, endpoints, iInterface=0):
        self.bInterfaceNumber = number
        self.bAlternateSetting = alt
        self.bInterfaceClass = iclass
        self.bInterfaceSubClass = isub
        self.bInterfaceProtocol = iproto
        self._endpoints = endpoints
        self.iInterface = iInterface

    def __iter__(self):
        return iter(self._endpoints)


class FakeConfiguration:
    def __init__(self, value, interfaces, iConfiguration=0):
        self.bConfigurationValue = value
        self._interfaces = interfaces
        self.iConfiguration = iConfiguration

    def __iter__(self):
        return iter(self._interfaces)


class FakeDevice:
    def __init__(self, idVendor, idProduct, configurations,
                 deviceClass=0, deviceSubClass=0, deviceProtocol=0,
                 bcdUSB=0x0200, bcdDevice=0x0100,
                 iManufacturer=1, iProduct=2, iSerialNumber=3,
                 active_config_value=None):
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
        self._active_config_value = active_config_value

    def __iter__(self):
        return iter(self._configurations)

    def get_active_configuration(self):
        # 実pyusbのDevice.get_active_configuration()を模倣。active_config_value未指定時は
        # 「取得できない実機」を模して例外を送出し、呼び出し側のフォールバックを検証できるようにする。
        if self._active_config_value is None:
            raise RuntimeError("no active configuration (simulated)")
        class _FakeActiveConfig:
            pass
        c = _FakeActiveConfig()
        c.bConfigurationValue = self._active_config_value
        return c


class FakeUsbUtil:
    ENDPOINT_IN = 0x80
    ENDPOINT_OUT = 0x00
    ENDPOINT_TYPE_CTRL = 0
    ENDPOINT_TYPE_ISO = 1
    ENDPOINT_TYPE_BULK = 2
    ENDPOINT_TYPE_INTR = 3

    STRINGS = {1: "Acme Corp", 2: "Acme Widget", 3: "SN-0001", 4: "Default Config", 5: "Data Interface"}

    def get_string(self, dev, index):
        return self.STRINGS.get(index)

    def endpoint_direction(self, address):
        return self.ENDPOINT_IN if (address & 0x80) else self.ENDPOINT_OUT

    def endpoint_type(self, attributes):
        return attributes & 0x03


def test_protected_interface_classes():
    assert h.is_protected_interface_class(0x03) is True   # HID
    assert h.is_protected_interface_class(0x08) is True   # Mass Storage
    assert h.is_protected_interface_class(0xE0) is True   # Wireless Controller
    assert h.is_protected_interface_class(0xFF) is False  # Vendor specific: OK
    assert h.is_protected_interface_class(None) is True   # 不明 -> 安全側
    print("test_protected_interface_classes: OK")


def test_blocklist():
    assert h.is_blocklisted_device(0x1050, 0x0407) is True   # YubiKey系
    assert h.is_blocklisted_device(0x2341, 0x8036) is False  # Arduino Leonardo (無関係)
    print("test_blocklist: OK")


def test_bcd_to_version():
    assert h.bcd_to_version(0x0210) == (2, 1, 0)
    assert h.bcd_to_version(0x0100) == (1, 0, 0)
    assert h.bcd_to_version(None) == (0, 0, 0)
    print("test_bcd_to_version: OK")


def test_descriptor_builder_arduino_like():
    util = FakeUsbUtil()
    # Arduino的なCDC複合デバイス: interface0=CDC control, interface1=CDC data(bulk in/out)
    ep_in = FakeEndpoint(0x81, 0x02)   # bulk in
    ep_out = FakeEndpoint(0x02, 0x02)  # bulk out
    intf0 = FakeInterface(0, 0, 0x02, 0x02, 0x01, [])           # CDC control (vendor-neutral)
    intf1 = FakeInterface(1, 0, 0xFF, 0x00, 0x00, [ep_in, ep_out])  # vendor-specific bulk
    cfg = FakeConfiguration(1, [intf0, intf1])
    dev = FakeDevice(0x2341, 0x8036, [cfg])

    info = h.build_device_descriptor(dev, util)
    assert info["vendorId"] == 0x2341
    assert info["serialNumber"] == "SN-0001"
    assert info["manufacturerName"] == "Acme Corp"
    assert (info["usbVersionMajor"], info["usbVersionMinor"], info["usbVersionSubminor"]) == (2, 0, 0)
    assert len(info["configurations"]) == 1
    interfaces = info["configurations"][0]["interfaces"]
    assert len(interfaces) == 2
    vendor_iface = [i for i in interfaces if i["interfaceNumber"] == 1][0]
    eps = vendor_iface["alternates"][0]["endpoints"]
    assert {"endpointNumber": 1, "direction": "in", "type": "bulk", "packetSize": 64} in eps
    assert {"endpointNumber": 2, "direction": "out", "type": "bulk", "packetSize": 64} in eps
    print("test_descriptor_builder_arduino_like: OK")


def test_hid_interface_flagged_protected():
    util = FakeUsbUtil()
    intf_hid = FakeInterface(0, 0, 0x03, 0x01, 0x01, [FakeEndpoint(0x81, 0x03)])  # HID keyboard-like
    cfg = FakeConfiguration(1, [intf_hid])
    dev = FakeDevice(0x1234, 0x5678, [cfg], deviceClass=0)
    info = h.build_device_descriptor(dev, util)
    iface = info["configurations"][0]["interfaces"][0]
    assert iface["alternates"][0]["interfaceProtected"] is True
    assert h.interface_class_for(dev, 0) == 0x03
    print("test_hid_interface_flagged_protected: OK")


def test_hub_interface_flagged_protected():
    """WebUSB仕様本文(index.bs の「Protected interface classes」表,
    #protected-interface-classes)を一次ソースから直接確認したところ、
    Audio/HID/Mass Storage/Smart Card/Video/Audio-Video/Wireless Controllerに加えて
    Hub(0x09)も保護対象クラスに含まれていた。旧実装はHubが抜けたまま7クラスしか
    登録されておらず、Hub機器のインターフェースをclaimInterfaceできてしまう状態
    だった。"""
    util = FakeUsbUtil()
    assert h.is_protected_interface_class(0x09) is True  # Hub
    intf_hub = FakeInterface(0, 0, 0x09, 0x00, 0x00, [FakeEndpoint(0x81, 0x03)])
    cfg = FakeConfiguration(1, [intf_hub])
    dev = FakeDevice(0x1234, 0x5678, [cfg], deviceClass=0)
    info = h.build_device_descriptor(dev, util)
    iface = info["configurations"][0]["interfaces"][0]
    assert iface["alternates"][0]["interfaceProtected"] is True
    print("test_hub_interface_flagged_protected: OK")


def test_configuration_and_interface_names():
    """spec: USBConfiguration.configurationName / USBAlternateInterface.interfaceName は
    それぞれconfiguration/interface descriptorのiConfiguration/iInterfaceが指す
    string descriptorの値(未定義の場合はNone)。旧実装はどちらも未実装で、
    生成されるdictにキー自体が存在しなかった。"""
    util = FakeUsbUtil()
    ep = FakeEndpoint(0x81, 0x02)
    intf_named = FakeInterface(0, 0, 0xFF, 0x00, 0x00, [ep], iInterface=5)
    intf_unnamed = FakeInterface(1, 0, 0xFF, 0x00, 0x00, [ep])  # iInterface省略 -> 0 -> None
    cfg = FakeConfiguration(1, [intf_named, intf_unnamed], iConfiguration=4)
    dev = FakeDevice(0x1234, 0x5678, [cfg])
    info = h.build_device_descriptor(dev, util)
    config_info = info["configurations"][0]
    assert config_info["configurationName"] == "Default Config"
    interfaces = config_info["interfaces"]
    named = [i for i in interfaces if i["interfaceNumber"] == 0][0]
    unnamed = [i for i in interfaces if i["interfaceNumber"] == 1][0]
    assert named["alternates"][0]["interfaceName"] == "Data Interface"
    assert unnamed["alternates"][0]["interfaceName"] is None, \
        "iInterface==0(文字列記述子なし)はNoneになるべき"
    print("test_configuration_and_interface_names: OK")


def test_control_type_endpoint_excluded_from_endpoints():
    """spec注記(USBAlternateInterfaceコンストラクタ手順): bmAttributesが
    Control Transfer Type(下位2bit==00)を示すendpoint記述子はendpoints一覧から
    除外されるべき("There shouldn't be any endpoint object belongs to Control
    Transfer Type")。旧実装は除外しておらず、実仕様のUSBEndpointType enum
    ("bulk"/"interrupt"/"isochronous")には存在しない"control"という値を
    返しうる状態だった。"""
    util = FakeUsbUtil()
    ep_ctrl = FakeEndpoint(0x00, 0x00)  # bmAttributes下位2bit=00 -> Control
    ep_bulk = FakeEndpoint(0x81, 0x02)  # bulk in
    intf = FakeInterface(0, 0, 0xFF, 0x00, 0x00, [ep_ctrl, ep_bulk])
    cfg = FakeConfiguration(1, [intf])
    dev = FakeDevice(0x1234, 0x5678, [cfg])
    info = h.build_device_descriptor(dev, util)
    eps = info["configurations"][0]["interfaces"][0]["alternates"][0]["endpoints"]
    assert len(eps) == 1, "Control転送タイプのendpointは一覧から除外されるべき"
    assert eps[0]["endpointNumber"] == 1
    assert all(e["type"] != "control" for e in eps)
    print("test_control_type_endpoint_excluded_from_endpoints: OK")


def test_unknown_interface_number_treated_as_protected():
    """interface_class_for()が対象のインターフェース番号を見つけられなかった場合
    (存在しないインターフェース番号が渡された等)、is_protected_interface_class()は
    安全側(=保護対象として拒否)に倒れるべき。旧実装はinterface_class_for()の
    「見つからない」センチネル値が-1で、int(-1)は例外を投げないため
    is_protected_interface_class()の安全側フォールバック(TypeError/ValueError捕捉)を
    素通りしてしまい、-1 not in PROTECTED_INTERFACE_CLASSES つまりFalse(保護対象で
    ない)と誤判定していた(claimInterfaceが実在しないインターフェース番号に対して
    fail-openしていた、実際にPythonで再現・確認済みのバグ)。"""
    util = FakeUsbUtil()
    intf = FakeInterface(0, 0, 0xFF, 0x00, 0x00, [FakeEndpoint(0x81, 0x02)])
    cfg = FakeConfiguration(1, [intf])
    dev = FakeDevice(0x1234, 0x5678, [cfg])
    assert h.interface_class_for(dev, 99) is None  # インターフェース0しか無いので99は存在しない
    assert h.is_protected_interface_class(h.interface_class_for(dev, 99)) is True
    print("test_unknown_interface_number_treated_as_protected: OK")


def test_hotplug_watcher_diff():
    state = {"devices": {(0x2341, 0x8036)}}
    watcher = h.UsbHotplugWatcher(lambda: set(state["devices"]))
    connected, disconnected = watcher.poll()
    assert connected == [(0x2341, 0x8036)] and disconnected == []
    state["devices"].add((0x1050, 0x0407))
    connected, disconnected = watcher.poll()
    assert connected == [(0x1050, 0x0407)] and disconnected == []
    state["devices"].discard((0x2341, 0x8036))
    connected, disconnected = watcher.poll()
    assert connected == [] and disconnected == [(0x2341, 0x8036)]
    print("test_hotplug_watcher_diff: OK")


# ==================== ここから: requestDevice()フィルタ照合 ====================
# 出典: https://wicg.github.io/webusb/#dom-usb-requestdevice の
# 'A USBDeviceFilter filter is valid' / 'A USB device device matches a
# device filter filter' / 'A USB interface interface matches an interface
# filter filter' を実装したもの。旧実装はrequestDevice(options)のfilters/
# exclusionFiltersを一切見ておらず、常に全デバイスをチューザーに表示していた。

def test_is_valid_usb_device_filter():
    assert h.is_valid_usb_device_filter({}) is True
    assert h.is_valid_usb_device_filter({"vendorId": 0x2341}) is True
    assert h.is_valid_usb_device_filter({"vendorId": 0x2341, "productId": 0x8036}) is True
    assert h.is_valid_usb_device_filter({"productId": 0x8036}) is False          # vendorId無しでproductIdだけ
    assert h.is_valid_usb_device_filter({"classCode": 3, "subclassCode": 1}) is True
    assert h.is_valid_usb_device_filter({"subclassCode": 1}) is False            # classCode無しでsubclassCodeだけ
    assert h.is_valid_usb_device_filter({"classCode": 3, "subclassCode": 1, "protocolCode": 2}) is True
    assert h.is_valid_usb_device_filter({"classCode": 3, "protocolCode": 2}) is False  # subclassCode無しでprotocolCodeだけ
    assert h.is_valid_usb_device_filter("not a dict") is False
    print("test_is_valid_usb_device_filter: OK")


def _arduino_like_device():
    util = FakeUsbUtil()
    ep_in = FakeEndpoint(0x81, 0x02)
    ep_out = FakeEndpoint(0x02, 0x02)
    intf0 = FakeInterface(0, 0, 0x02, 0x02, 0x01, [])
    intf1 = FakeInterface(1, 0, 0xFF, 0x00, 0x00, [ep_in, ep_out])
    cfg = FakeConfiguration(1, [intf0, intf1])
    dev = FakeDevice(0x2341, 0x8036, [cfg])
    return dev, util


def test_device_matches_filter_vendor_and_product_id():
    dev, util = _arduino_like_device()
    assert h.device_matches_usb_filter(dev, util, {"vendorId": 0x2341}) is True
    assert h.device_matches_usb_filter(dev, util, {"vendorId": 0x9999}) is False
    assert h.device_matches_usb_filter(dev, util, {"vendorId": 0x2341, "productId": 0x8036}) is True
    assert h.device_matches_usb_filter(dev, util, {"vendorId": 0x2341, "productId": 0x0001}) is False
    print("test_device_matches_filter_vendor_and_product_id: OK")


def test_device_matches_filter_serial_number():
    dev, util = _arduino_like_device()  # FakeUsbUtil.STRINGS[3] == "SN-0001"
    assert h.device_matches_usb_filter(dev, util, {"serialNumber": "SN-0001"}) is True
    assert h.device_matches_usb_filter(dev, util, {"serialNumber": "SN-9999"}) is False
    print("test_device_matches_filter_serial_number: OK")


def test_device_matches_filter_class_code_device_level():
    util = FakeUsbUtil()
    intf_hid = FakeInterface(0, 0, 0x03, 0x01, 0x01, [])
    cfg = FakeConfiguration(1, [intf_hid])
    # bDeviceClass=0x03自体をHIDとして申告している(複合デバイスではない単純な機器)
    dev = FakeDevice(0x1234, 0x5678, [cfg], deviceClass=0x03)
    assert h.device_matches_usb_filter(dev, util, {"classCode": 0x03}) is True
    assert h.device_matches_usb_filter(dev, util, {"classCode": 0x08}) is False
    print("test_device_matches_filter_class_code_device_level: OK")


def test_device_matches_filter_class_code_via_interface():
    # 複合デバイス: bDeviceClass=0xFF(vendor-specific)を名乗りつつ、
    # インターフェース1つがHID(0x03)を申告しているケース。
    # 仕様どおりなら「いずれかのインターフェースが一致すればデバイス全体もmatch」。
    dev, util = _arduino_like_device()  # bDeviceClass=0, インターフェース1が0xFF
    assert h.device_matches_usb_filter(dev, util, {"classCode": 0xFF}) is True  # interface1経由でmatch
    assert h.device_matches_usb_filter(dev, util, {"classCode": 0x02}) is True  # interface0(CDC control)経由でmatch
    assert h.device_matches_usb_filter(dev, util, {"classCode": 0x08}) is False  # どこにも無い
    # サブクラス/プロトコルまで含めた絞り込みもインターフェース単位で見る
    assert h.device_matches_usb_filter(dev, util, {"classCode": 0xFF, "subclassCode": 0x00, "protocolCode": 0x00}) is True
    assert h.device_matches_usb_filter(dev, util, {"classCode": 0xFF, "subclassCode": 0x99}) is False
    print("test_device_matches_filter_class_code_via_interface: OK")


def test_device_matches_any_usb_filter_empty_list_matches_nothing():
    dev, util = _arduino_like_device()
    # 仕様どおり: filters=[](空配列)は「一致するものなし」。
    # 「全デバイスを見せたい」場合はfilters:[{}](空オブジェクト)が正しい書き方。
    assert h.device_matches_any_usb_filter(dev, util, []) is False
    assert h.device_matches_any_usb_filter(dev, util, [{}]) is True
    assert h.device_matches_any_usb_filter(dev, util, [{"vendorId": 0x9999}, {"vendorId": 0x2341}]) is True
    assert h.device_matches_any_usb_filter(dev, util, [{"vendorId": 0x9999}]) is False
    print("test_device_matches_any_usb_filter_empty_list_matches_nothing: OK")


# ==================== ここから: STALL検出(USBTransferStatus) ====================

def test_is_stall_error():
    class FakeUSBErrorWithErrno(Exception):
        errno = 32
    class FakeUSBErrorNoErrno(Exception):
        pass
    assert h.is_stall_error(FakeUSBErrorWithErrno("some backend text")) is True
    assert h.is_stall_error(FakeUSBErrorNoErrno("[Errno 32] Pipe error")) is True
    assert h.is_stall_error(FakeUSBErrorNoErrno("Resource busy")) is False
    assert h.is_stall_error(FakeUSBErrorNoErrno("device STALLed the endpoint")) is True
    print("test_is_stall_error: OK")


# ==================== ここから: activeConfigurationValue ====================

def test_active_configuration_value_reported_when_available():
    util = FakeUsbUtil()
    cfg1 = FakeConfiguration(1, [])
    cfg2 = FakeConfiguration(2, [])
    dev = FakeDevice(0x1234, 0x5678, [cfg1, cfg2], active_config_value=2)
    info = h.build_device_descriptor(dev, util, include_configurations=False)
    assert info["activeConfigurationValue"] == 2
    print("test_active_configuration_value_reported_when_available: OK")


def test_active_configuration_value_none_when_unavailable():
    util = FakeUsbUtil()
    cfg1 = FakeConfiguration(1, [])
    dev = FakeDevice(0x1234, 0x5678, [cfg1])  # active_config_value未指定 -> get_active_configuration()が例外
    info = h.build_device_descriptor(dev, util, include_configurations=False)
    assert info["activeConfigurationValue"] is None
    print("test_active_configuration_value_none_when_unavailable: OK")


if __name__ == "__main__":
    test_protected_interface_classes()
    test_blocklist()
    test_bcd_to_version()
    test_descriptor_builder_arduino_like()
    test_hid_interface_flagged_protected()
    test_hub_interface_flagged_protected()
    test_configuration_and_interface_names()
    test_control_type_endpoint_excluded_from_endpoints()
    test_unknown_interface_number_treated_as_protected()
    test_hotplug_watcher_diff()
    test_is_valid_usb_device_filter()
    test_device_matches_filter_vendor_and_product_id()
    test_device_matches_filter_serial_number()
    test_device_matches_filter_class_code_device_level()
    test_device_matches_filter_class_code_via_interface()
    test_device_matches_any_usb_filter_empty_list_matches_nothing()
    test_is_stall_error()
    test_active_configuration_value_reported_when_available()
    test_active_configuration_value_none_when_unavailable()
    print("ALL WEBUSB HARDENING TESTS PASSED")
