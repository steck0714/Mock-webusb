# -*- coding: utf-8 -*-
"""WebUSBBridge.requestDeviceChooser()を、フェイクのpyusbデバイス・フェイクの
チューザーダイアログに差し替えた上で実際に呼び出して検証する統合テスト。
実USBデバイス・実GUI操作なしで、options.filters/exclusionFiltersによる
絞り込みと、選択後のリッチな記述子再構築を確認する。"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ★ `pytest`経由ならconftest.pyが同じ処理を先に行うが、`python tests/test_bridge.py`と
# 直接実行した場合はconftest.pyが自動では読み込まれないため、ここでも同じ
# ヘッドレス環境向けオフスクリーン自動フォールバックを行う(理由はconftest.py参照。
# QApplication([])のディスプレイ接続失敗はPythonの例外ではなくプロセスクラッシュに
# なるため、try/exceptでは救えず、生成前に検知して回避する必要がある)。
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

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

    def get_active_configuration(self):
        # 🛡️ 実pyusbのDevice.get_active_configuration()を模倣。このテストファイルの
        #    フィクスチャは常にconfigurationを1つしか構築しないため、単純に先頭を返す
        #    (test_hardening.py側のFakeDeviceのように「未設定なら例外」という
        #    より作り込んだ挙動は、このファイルでは今のところ不要)。
        if not self._configurations:
            raise ValueError("no active configuration")
        return self._configurations[0]

    def read(self, endpoint, length, timeout=None):
        # 🛡️ pyusb実物のDevice.read()を模倣: endpointはbEndpointAddress(方向ビット込み)
        #    を要求する。呼び出し時に実際に渡された値を記録しておき、
        #    bulkTransferIn()側でIN方向ビット(0x80)が正しく付与されているかを
        #    テストから検証できるようにする。
        self.last_read_call = {"endpoint": endpoint, "length": length, "timeout": timeout}
        return bytes([0xAB]) * min(length, 4)

    def write(self, endpoint, data, timeout=None):
        self.last_write_call = {"endpoint": endpoint, "data": bytes(data), "timeout": timeout}
        return len(data)

    def ctrl_transfer(self, bmRequestType, bRequest, wValue=0, wIndex=0, data_or_wLength=None, timeout=None):
        self.ctrl_transfer_calls = getattr(self, "ctrl_transfer_calls", [])
        self.ctrl_transfer_calls.append({
            "bmRequestType": bmRequestType, "bRequest": bRequest,
            "wValue": wValue, "wIndex": wIndex, "data_or_wLength": data_or_wLength, "timeout": timeout,
        })
        if isinstance(data_or_wLength, int):
            return bytes([0xCD]) * min(data_or_wLength, 4)  # IN: ダミーデータ
        return len(data_or_wLength) if data_or_wLength else 0  # OUT: 書き込みバイト数

    def is_kernel_driver_active(self, interface_number):
        return False

    def detach_kernel_driver(self, interface_number):
        pass


class FakeUsbUtil:
    STRINGS = {1: "Acme Corp", 2: "Acme Widget", 3: "SN-0001"}

    def get_string(self, dev, index):
        return self.STRINGS.get(index)

    def claim_interface(self, dev, interface):
        dev.claimed_by_util = getattr(dev, "claimed_by_util", set())
        dev.claimed_by_util.add(interface)

    def release_interface(self, dev, interface):
        dev.claimed_by_util = getattr(dev, "claimed_by_util", set())
        dev.claimed_by_util.discard(interface)


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
    last_origin = None
    last_refresh_callback = None

    def __init__(self, devices_info, parent, strings=None, origin=None, refresh_callback=None):
        FakeChooserDialog.last_devices_info = devices_info
        FakeChooserDialog.last_origin = origin
        FakeChooserDialog.last_refresh_callback = refresh_callback
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


def test_origin_is_passed_to_the_dialog(monkeypatch):
    dev_a = FakeDevice(0x2341, 0x8036, [FakeConfiguration(1, [])])
    bridge = make_bridge([dev_a])
    monkeypatch.setattr("pyside6_webusb.bridge.WebUsbDeviceChooserDialog", FakeChooserDialog)
    FakeChooserDialog.SELECT_INDEX = 0
    bridge.requestDeviceChooser(json.dumps({"filters": [{}]}))
    assert FakeChooserDialog.last_origin == "https://example.test"
    print("test_origin_is_passed_to_the_dialog: OK")


def test_refresh_callback_reflects_newly_plugged_device(monkeypatch):
    dev_a = FakeDevice(0x2341, 0x8036, [FakeConfiguration(1, [])])
    bridge = make_bridge([dev_a])
    monkeypatch.setattr("pyside6_webusb.bridge.WebUsbDeviceChooserDialog", FakeChooserDialog)
    FakeChooserDialog.SELECT_INDEX = 0
    bridge.requestDeviceChooser(json.dumps({"filters": [{}]}))
    assert len(FakeChooserDialog.last_refresh_callback()) == 1

    # ダイアログを開いたまま新しいデバイスが挿された、という状況を模擬する
    dev_b = FakeDevice(0x1234, 0x0002, [FakeConfiguration(1, [])])
    bridge._pyusb = lambda: (FakeUsbCore([dev_a, dev_b]), FakeUsbUtil())
    refreshed = FakeChooserDialog.last_refresh_callback()
    assert len(refreshed) == 2
    print("test_refresh_callback_reflects_newly_plugged_device: OK")


def test_full_flow_persists_grant_and_usage_without_mocking_internals(monkeypatch, tmp_path=None):
    """_grant()/_record_device_usage()を(テストのために上書きせず)実際に動かして
    最後まで通す。この2つはtime.time()を使っており、以前 bridge.py に
    `import time` が無いまま出荷され、実行時にNameErrorで静かに失敗していた
    (呼び出し側がtry/exceptで包んでいたため、ダイアログの選択自体は成功する
    ように見えてしまい、許可・利用実績の永続化だけがこっそり失敗していた)。
    filters/exclusionFiltersのテストのようにこれらを丸ごとモックしていると
    この種の欠陥を検出できないため、実装をそのまま通す専用のテストを分けている。
    QSettingsは実システムの設定ストアを汚さないよう、一時ファイルのIniFormatへ
    明示的に切り替える。"""
    import tempfile
    from PySide6.QtCore import QSettings

    dev_a = FakeDevice(0x2341, 0x8036, [FakeConfiguration(1, [])])
    bridge = WebUSBBridge()  # _grant/_record_device_usageは上書きしない(実装をそのまま使う)
    bridge._pyusb = lambda: (FakeUsbCore([dev_a]), FakeUsbUtil())
    bridge._current_origin = lambda: "https://example.test"

    tmp_dir = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp(prefix="pyside6_webusb_test_")
    ini_path = os.path.join(tmp_dir, "settings.ini")
    real_settings = QSettings(ini_path, QSettings.Format.IniFormat)
    bridge._known_device_settings = lambda: real_settings

    monkeypatch.setattr("pyside6_webusb.bridge.WebUsbDeviceChooserDialog", FakeChooserDialog)
    FakeChooserDialog.SELECT_INDEX = 0
    raw = bridge.requestDeviceChooser(json.dumps({"filters": [{}]}))
    result = json.loads(raw)
    assert result["cancelled"] is False
    assert result["device"]["vendorId"] == 0x2341

    granted = bridge.list_granted_origins()
    assert granted.get("https://example.test"), "実際のQSettingsへ許可が永続化されているはず"
    entry = granted["https://example.test"][0]
    assert entry["vendorId"] == 0x2341 and entry["productId"] == 0x8036
    assert isinstance(entry.get("grantedAt"), (int, float)), "grantedAtにtime.time()の値が入っているはず"

    known = bridge._load_known_devices()
    assert any(d.get("vendorId") == 0x2341 for d in known), "利用実績(known devices)にも記録されているはず"
    print("test_full_flow_persists_grant_and_usage_without_mocking_internals: OK")


def test_bulkTransferIn_adds_the_in_direction_bit():
    """USBDevice.transferIn(endpointNumber, length) の実仕様(WebUSB spec, index.bs):
    'Let endpointAddress be endpointNumber | 0x80'。JSから渡ってくるendpointは
    方向ビットを含まない生のendpointNumberであり、pyusbのDevice.read()は
    bEndpointAddress(方向ビット込みの値)を要求する(pyusb公式ドキュメント
    'The endpoint parameter corresponds to the bEndpointAddress member' で確認済み)。
    旧実装はこの変換が抜けており、endpointNumber=1のIN転送がbEndpointAddress=0x01
    (同じ番号のOUT側)を叩きにいってしまい、実機相手には常に失敗していた。
    対照として、transferOut/bulkTransferOutは元々ビット無しのendpointNumberが
    そのまま正しいbEndpointAddressになる(OUT方向は0ビット)ため変換は不要であり、
    そちらは今回変更していないことも合わせて確認する。"""
    dev_a = FakeDevice(0x2341, 0x8036, [FakeConfiguration(1, [])])
    bridge = make_bridge([dev_a])
    bridge._is_granted = lambda *a, **kw: True  # このテストの対象はopenDeviceの許可ゲートではない

    open_result = json.loads(bridge.openDevice(0x2341, 0x8036))
    assert open_result["success"] is True
    handle = open_result["handle"]

    in_result = json.loads(bridge.bulkTransferIn(handle, 1, 4))
    assert in_result["success"] is True
    assert dev_a.last_read_call["endpoint"] == 0x81, (
        "endpointNumber=1のIN転送はbEndpointAddress=0x81(=1 | 0x80)をpyusbへ渡すべき"
        f"だが、実際には {dev_a.last_read_call['endpoint']:#x} だった"
    )

    out_result = json.loads(bridge.bulkTransferOut(handle, 2, "01020304"))
    assert out_result["success"] is True
    assert dev_a.last_write_call["endpoint"] == 2, (
        "OUT方向は方向ビットが無いのが正しいので、endpointNumberはそのまま2で渡るはず"
    )
    print("test_bulkTransferIn_adds_the_in_direction_bit: OK")


def test_control_transfer_class_request_to_protected_interface_is_blocked():
    """WebUSB仕様「check the validity of the control transfer parameters」:
    requestType=='class' のとき、setup.indexの下位8bitが指すインターフェースが
    保護対象クラスなら(recipientが何であっても)SecurityError。
    旧実装はこの検証が完全に欠落しており、claimInterface()自体は拒否するHIDの
    ようなインターフェースへも、requestType:'class' のcontrolTransferOut/Inを
    直接送るだけでclaimInterfaceの保護を丸ごとバイパスできてしまっていた
    (claimInterfaceを一度も呼ばずに)。ここではrecipientをあえて'device'にして、
    recipient側の(別の)claimチェックに頼らずclass側の検証単体で
    ブロックされることを確認する。"""
    hid_intf = FakeInterface(0, 0, 0x03, 0x01, 0x01, [FakeEndpoint(0x81, 0x03)])  # HID, 未claim
    dev_a = FakeDevice(0x2341, 0x8036, [FakeConfiguration(1, [hid_intf])])
    bridge = make_bridge([dev_a])
    bridge._is_granted = lambda *a, **kw: True
    handle = json.loads(bridge.openDevice(0x2341, 0x8036))["handle"]

    # bmRequestType = direction:out(0x00) | type:class(0x20) | recipient:device(0x00) = 0x20
    # indexの下位8bit=0 -> interface 0(HID)を指す
    result = json.loads(bridge.controlTransferOut(handle, 0x20, 0x09, 0, 0, ""))
    assert result["success"] is False
    assert result["error"].startswith("SecurityError:"), result
    assert not hasattr(dev_a, "ctrl_transfer_calls"), (
        "保護対象クラスへのclass要求は実機(pyusb)へ届く前にブロックされるべき"
    )
    print("test_control_transfer_class_request_to_protected_interface_is_blocked: OK")


def test_control_transfer_interface_recipient_requires_claim():
    """recipient=='interface' のとき、対象インターフェースがclaim済みでなければ
    (保護対象クラスでなくても)InvalidStateError。claim後は同じ呼び出しが
    通ることも合わせて確認する。"""
    vendor_intf = FakeInterface(0, 0, 0xFF, 0x00, 0x00, [FakeEndpoint(0x81, 0x02)])  # vendor-specific
    dev_a = FakeDevice(0x2341, 0x8036, [FakeConfiguration(1, [vendor_intf])])
    bridge = make_bridge([dev_a])
    bridge._is_granted = lambda *a, **kw: True
    handle = json.loads(bridge.openDevice(0x2341, 0x8036))["handle"]

    # bmRequestType = out(0x00) | type:vendor(0x40) | recipient:interface(0x01) = 0x41
    not_claimed = json.loads(bridge.controlTransferOut(handle, 0x41, 0x01, 0, 0, ""))
    assert not_claimed["success"] is False
    assert not_claimed["error"].startswith("InvalidStateError:"), not_claimed
    assert not hasattr(dev_a, "ctrl_transfer_calls")

    claim_result = json.loads(bridge.claimInterface(handle, 0))
    assert claim_result["success"] is True

    claimed = json.loads(bridge.controlTransferOut(handle, 0x41, 0x01, 0, 0, ""))
    assert claimed["success"] is True, claimed
    print("test_control_transfer_interface_recipient_requires_claim: OK")


def test_control_transfer_standard_request_restrictions():
    """spec: requestType=='standard' は (1) controlTransferOut(direction=out)では
    常に拒否、(2) controlTransferInでもGET_STATUS/GET_DESCRIPTOR/
    GET_CONFIGURATION/GET_INTERFACE/SYNCH_FRAME以外は拒否、という制限がある。"""
    intf = FakeInterface(0, 0, 0xFF, 0x00, 0x00, [FakeEndpoint(0x81, 0x02)])
    dev_a = FakeDevice(0x2341, 0x8036, [FakeConfiguration(1, [intf])])
    bridge = make_bridge([dev_a])
    bridge._is_granted = lambda *a, **kw: True
    handle = json.loads(bridge.openDevice(0x2341, 0x8036))["handle"]

    # bmRequestType = out(0x00) | type:standard(0x00) | recipient:device(0x00) = 0x00
    out_result = json.loads(bridge.controlTransferOut(handle, 0x00, 0x09, 0, 0, ""))  # SET_CONFIGURATION相当
    assert out_result["success"] is False
    assert out_result["error"].startswith("SecurityError:"), out_result

    # bmRequestType = in(0x80) | type:standard(0x00) | recipient:device(0x00) = 0x80、
    # だが許可リスト外のrequest(0x05 = SET_ADDRESS相当)
    bad_in = json.loads(bridge.controlTransferIn(handle, 0x80, 0x05, 0, 0, 8))
    assert bad_in["success"] is False
    assert bad_in["error"].startswith("SecurityError:"), bad_in

    # 許可リスト内(0x00 = GET_STATUS)なら通る
    good_in = json.loads(bridge.controlTransferIn(handle, 0x80, 0x00, 0, 0, 2))
    assert good_in["success"] is True, good_in
    print("test_control_transfer_standard_request_restrictions: OK")


def test_requestDeviceChooser_reentrancy_guard(monkeypatch):
    """dlg.exec()は(実物のQtでは)ネストしたQtイベントループを回すため、その最中に
    同じWebUSBBridgeインスタンスへもう一度requestDeviceChooser()が呼ばれると、
    チューザーダイアログが二重に開いてしまう恐れがある。ここではFakeChooserDialog.exec()
    の中から(="ダイアログが開いている最中"に相当するタイミングで)
    bridge.requestDeviceChooser()を再帰的に呼び出し、内側の呼び出しが
    InvalidStateErrorで即座に弾かれ、外側の(正規の)呼び出しは通常どおり成功し、
    完了後はガードフラグが確実に解除されていることを確認する。"""
    dev_a = FakeDevice(0x2341, 0x8036, [FakeConfiguration(1, [])])
    bridge = make_bridge([dev_a])
    reentrant_raw = {}

    class ReentrantChooserDialog(FakeChooserDialog):
        def exec(self):
            reentrant_raw["result"] = bridge.requestDeviceChooser(json.dumps({"filters": [{}]}))
            return super().exec()

    monkeypatch.setattr("pyside6_webusb.bridge.WebUsbDeviceChooserDialog", ReentrantChooserDialog)
    ReentrantChooserDialog.SELECT_INDEX = 0
    outer = json.loads(bridge.requestDeviceChooser(json.dumps({"filters": [{}]})))

    assert "result" in reentrant_raw, "exec()の中からの再入呼び出しが実行されていない"
    inner = json.loads(reentrant_raw["result"])
    assert inner["cancelled"] is True
    assert inner.get("error", "").startswith("InvalidStateError:"), (
        f"再入時はInvalidStateErrorで即座に弾かれるべきだが、実際には {inner!r}"
    )
    assert outer["cancelled"] is False, "外側の(正規の)呼び出しは通常どおり成功するはず"
    assert bridge._chooser_active is False, "呼び出し完了後はガードフラグが必ず解除されているべき"
    print("test_requestDeviceChooser_reentrancy_guard: OK")


def test_requestDeviceChooser_is_registered_as_qt_slot():
    """QWebChannel's QMetaObjectPublisher only exposes methods that are registered
    as Qt Slots on staticMetaObject to the JS-side proxy object -- plain Python
    methods are invisible to it. Every test above calls
    bridge.requestDeviceChooser(...) directly from Python, so they all pass
    regardless of whether @Slot is present or attached to the right method,
    leaving a blind spot where "every test is green" while the method is
    actually unreachable from JS. (This is exactly what happened in practice:
    @Slot(str, result=str) had been misattached to the private helper
    _enumerate_filtered_devices instead of requestDeviceChooser.) This test
    closes that blind spot by inspecting the metaobject directly."""
    from PySide6.QtCore import QMetaMethod

    mo = WebUSBBridge.staticMetaObject
    slot_names = {
        bytes(mo.method(i).methodSignature()).decode().split("(", 1)[0]
        for i in range(mo.methodCount())
        if mo.method(i).methodType() == QMetaMethod.MethodType.Slot
    }
    assert "requestDeviceChooser" in slot_names, (
        "requestDeviceChooser is missing @Slot (or it was attached to the wrong "
        "method), so it can't be called from JS's navigator.usb.requestDevice() "
        "via QWebChannel"
    )
    # _enumerate_filtered_devices is a private helper whose real parameters are
    # usb_core/usb_util (pyusb modules) and filters/exclusion_filters (lists) --
    # none of which can be marshalled across a QWebChannel/JSON boundary, so it
    # must never be exposed to JS as a Slot.
    assert "_enumerate_filtered_devices" not in slot_names, (
        "_enumerate_filtered_devices must remain a private helper; it must not "
        "be registered as a @Slot exposed to JS via QWebChannel"
    )
    # _request_device_chooser_impl is the private implementation behind the
    # reentrancy-guarded requestDeviceChooser() wrapper; it must stay
    # unreachable from JS for the same reason.
    assert "_request_device_chooser_impl" not in slot_names, (
        "_request_device_chooser_impl must remain a private helper; exposing it "
        "as a @Slot would let JS bypass requestDeviceChooser()'s reentrancy guard"
    )
    # _control_transfer_validation_error enforces protected-class/claim checks for
    # controlTransferIn/Out; it must stay unreachable from JS so a page can't call
    # it directly with fabricated arguments to probe or bypass those checks.
    assert "_control_transfer_validation_error" not in slot_names, (
        "_control_transfer_validation_error must remain a private helper, not a "
        "JS-reachable @Slot"
    )
    print("test_requestDeviceChooser_is_registered_as_qt_slot: OK")


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
    test_origin_is_passed_to_the_dialog(mp)
    test_refresh_callback_reflects_newly_plugged_device(mp)
    test_full_flow_persists_grant_and_usage_without_mocking_internals(mp)
    test_bulkTransferIn_adds_the_in_direction_bit()
    test_control_transfer_class_request_to_protected_interface_is_blocked()
    test_control_transfer_interface_recipient_requires_claim()
    test_control_transfer_standard_request_restrictions()
    test_requestDeviceChooser_reentrancy_guard(mp)
    test_requestDeviceChooser_is_registered_as_qt_slot()
    print("ALL BRIDGE TESTS PASSED")
