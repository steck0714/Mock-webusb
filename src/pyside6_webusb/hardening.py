# -*- coding: utf-8 -*-
"""
hardening.py
============
WebUSBBridge (bridge.py) / WEBUSB_POLYFILL_JS (polyfill.py) をWebUSB仕様(WICG Draft)に
より近づけ、かつセキュリティキー等の「保護対象インターフェースクラス」への
到達を構造的に遮断するための追加コンポーネント群。

このファイル単体では何も起動しない。bridge.py 側から import して、
WebUSBBridge の各 @Slot メソッド内から判定関数・記述子ビルダーを呼び出し、
UsbHotplugWatcher をアプリ起動時に起動する、という使い方を想定する
(pyside6-webusbパッケージの内部モジュール。単体でのimportも可能)。

--------------------------------------------------------------------------
なぜこれが必要か(2026年7月時点で確認した一次情報の要約)
--------------------------------------------------------------------------
* WebUSBはWICG(W3C Web Incubator Community Group)のDraftであり、正式なW3C
  勧告ではない。実装しているのはChromium系ブラウザ(Chrome/Edge/Opera/Samsung
  Internet等)のみ。
* Firefoxは公式スタンダードポジションでWebUSBを「harmful」(fingerprinting・
  セキュリティ上のリスクが大きい)と明記しており、実装する予定がない。
  Safari/WebKitも同様に反対の立場を表明しており、iOS/iPadOS/macOS Safariの
  いずれにも実装されていない。
  → WebUSBはWeb系APIの中でも最も慎重な扱いが必要なものの一つ、というのが
    ブラウザベンダー間でのおおむね共通した認識である。
* Chromium自身は上記のリスクを軽減するため、単なる「サイトへの許可」以外に
  少なくとも次の多層防御を実装している:
    (1) 「保護対象インターフェースクラス」: Audio / HID / Mass Storage / Hub /
        Smart Card / Video / Audio-Video / Wireless Controller の8クラスは
        claimInterface自体をブラウザ側で拒否する(これらは別の専用API
        [WebHID・WebMIDI等]や高レベルOS機能が既にあり、WebUSBで生アクセス
        させる必要が無いため)。
    (2) 既知の脆弱/セキュリティ上重要なデバイスを列挙した明示的な
        ブロックリスト(vendor_id, product_id単位)。主にFIDO/U2Fの
        セキュリティキー各種。
    (3) セキュアコンテキスト(https/localhostのみ)。
    (4) requestDevice()はユーザー操作(トラステッドイベント)からのみ呼び出し可能。
  本モジュールはこの(1)(2)をpyusb経由で再現し、(3)(4)はJS側
  (WEBUSB_POLYFILL_JS)側での追加チェックと合わせて実現する。

出典:
  - WebUSB仕様: https://wicg.github.io/webusb/
  - 保護対象インターフェースクラス8種の一覧: WebUSB仕様本文
    (index.bs の「Protected interface classes」表, #protected-interface-classes)
    を2026年7月に一次ソース(github.com/WICG/webusb)から直接取得して突き合わせ、
    Hub(0x09)が本実装から漏れていたことを確認・追加した。
  - デバイスブロックリスト: chrome/browser/usb/usb_blocklist.cc
    (https://github.com/chromium/chromium/blob/main/chrome/browser/usb/usb_blocklist.cc
     2026年7月時点のmainブランチより、vendor_id/product_idの組のみを移植。
     ★ 本家は随時更新されるため、定期的に上記URLを確認して追従すること。
     本モジュールの一覧はあくまで多層防御の一枚であり、下のHID等の
     インターフェースクラス丸ごと遮断の方が防御としては本質的に重要)
  - 参考実装 thegecko/webusb (Node.js): 2024年に非推奨化され、後継は
    npm "usb" パッケージ内蔵のWebUSB実装 (node-usb)。API形状
    (configurations/selectAlternateInterface/clearHalt/reset/serialNumber等)
    の突き合わせに使用した。isochronousTransferはnode-usb側でも
    「現状未対応」とされており、本実装でも同様に非対応として明示する
    (中途半端な実装で「動いているように見えて実は壊れている」状態を
    避けるため)。
"""

import time
import threading


# ============================================================
# 1) 保護対象インターフェースクラス
# ============================================================
# WebUSB仕様上、これらのクラスは claimInterface() 自体が拒否されるべきもの。
# 旧実装(監査時点)はHIDを含め一切のクラスチェックをしておらず、
# 許可さえ得ていれば(=ユーザーがチューザーダイアログで一度でも選んでしまえば)
# セキュリティキーやキーボード等のHIDインターフェースにも生アクセスできて
# しまっていた。ここで構造的に閉じる。
PROTECTED_INTERFACE_CLASSES = {
    0x01: "Audio",
    0x03: "HID (Human Interface Device: セキュリティキー/キーボード/マウス等の大半)",
    0x08: "Mass Storage",
    0x09: "Hub",
    0x0B: "Smart Card (CCID)",
    0x0E: "Video",
    0x10: "Audio/Video",
    0xE0: "Wireless Controller (Bluetooth/Wireless USBアダプタ等)",
}


def is_protected_interface_class(b_interface_class) -> bool:
    """このインターフェースクラスは claimInterface を拒否すべきか"""
    try:
        return int(b_interface_class) in PROTECTED_INTERFACE_CLASSES
    except (TypeError, ValueError):
        return True  # 判定できない場合は安全側(拒否)に倒す


def protected_class_name(b_interface_class) -> str:
    try:
        return PROTECTED_INTERFACE_CLASSES.get(int(b_interface_class), "Unknown")
    except (TypeError, ValueError):
        return "Unknown"


# ============================================================
# 2) 既知セキュリティキー/認証デバイスのブロックリスト(多層防御の2枚目)
# ============================================================
# Chromiumの usb_blocklist.cc (2026-07時点のmain) から vendor_id/product_id の
# 組のみを移植したもの。実際の脅威はほぼ全てHIDインターフェースとして
# 提示されるため上のPROTECTED_INTERFACE_CLASSESで既にブロックされるはずだが、
# 「非HIDインターフェースでCTAP相当を喋る」将来の変則的デバイスに備えて
# デバイス単位でも明示的に遮断する。
KNOWN_SECURITY_KEY_BLOCKLIST = frozenset([
    (0x096e, 0x0850), (0x096e, 0x0852), (0x096e, 0x0853), (0x096e, 0x0854),
    (0x096e, 0x0856), (0x096e, 0x0858), (0x096e, 0x085a), (0x096e, 0x085b),
    (0x096e, 0x0880),
    (0x09c3, 0x0023),
    (0x1050, 0x0010), (0x1050, 0x0018), (0x1050, 0x0030),
    (0x1050, 0x0110), (0x1050, 0x0111), (0x1050, 0x0112), (0x1050, 0x0113),
    (0x1050, 0x0114), (0x1050, 0x0115), (0x1050, 0x0116), (0x1050, 0x0120),
    (0x1050, 0x0200), (0x1050, 0x0211),
    (0x1050, 0x0401), (0x1050, 0x0402), (0x1050, 0x0403), (0x1050, 0x0404),
    (0x1050, 0x0405), (0x1050, 0x0406), (0x1050, 0x0407), (0x1050, 0x0410),
    (0x10c4, 0x8acf),
    (0x18d1, 0x5026),
    (0x1a44, 0x00bb),
    (0x1d50, 0x60fc),
    (0x1e0d, 0xf1ae), (0x1e0d, 0xf1d0),
    (0x1ea8, 0xf025),
    (0x20a0, 0x4287),
    (0x24dc, 0x0101),
    (0x2581, 0xf1d0),
    (0x2abe, 0x1002),
    (0x2ccf, 0x0880),
])


def is_blocklisted_device(vendor_id, product_id) -> bool:
    try:
        return (int(vendor_id), int(product_id)) in KNOWN_SECURITY_KEY_BLOCKLIST
    except (TypeError, ValueError):
        return True


def device_is_fully_blocked(dev) -> bool:
    """デバイス単位でブロックリストに一致するか(こちらはgetDevices/openDeviceの
    時点、つまりインターフェースをまだ見ていない段階での粗いチェック)。
    個々のインターフェースクラスのチェックは claimInterface 側で別途行う。"""
    try:
        return is_blocklisted_device(dev.idVendor, dev.idProduct)
    except Exception:
        return True


# ============================================================
# 3) BCDバージョン変換 (bcdUSB / bcdDevice -> major.minor.subminor)
# ============================================================
def bcd_to_version(bcd_value) -> tuple:
    """USB記述子のBCD値(例:0x0210)を(major, minor, subminor)=(2,1,0)に変換する。
    WebUSB仕様のUSBDevice.usbVersionMajor等はこの分解方式を使う。"""
    try:
        v = int(bcd_value)
    except (TypeError, ValueError):
        return (0, 0, 0)
    major = (v >> 8) & 0xFF
    minor = (v >> 4) & 0x0F
    subminor = v & 0x0F
    return (major, minor, subminor)


# ============================================================
# 4) リッチなデバイス記述子ビルダー
# ============================================================
# 旧実装はvendorId/productId/manufacturerName/productName/deviceClassのみを
# JSへ返しており、実際のWebUSB仕様が持つ configurations (interfaces/alternates/
# endpointsのツリー)や serialNumber, usbVersion*, deviceVersion* 等を
# 一切返していなかった。これでは「エンドポイント番号やインターフェース番号を
# 呼び出し元サイトが事前に知っている」ことが前提になってしまい、汎用の
# WebUSB対応ハードウェアSDK(例: thegecko/webusb同梱のデモや、市販USB機器の
# 公式Web SDK)がそのままでは動かない。ここで実機の記述子を可能な限り
# 汎用ブラウザ相当の形へ組み立てる。
def build_device_descriptor(dev, usb_util, include_configurations=True) -> dict:
    manufacturer = product = serial = None
    try:
        if getattr(dev, "iManufacturer", 0):
            manufacturer = usb_util.get_string(dev, dev.iManufacturer)
    except Exception:
        pass
    try:
        if getattr(dev, "iProduct", 0):
            product = usb_util.get_string(dev, dev.iProduct)
    except Exception:
        pass
    try:
        if getattr(dev, "iSerialNumber", 0):
            serial = usb_util.get_string(dev, dev.iSerialNumber)
    except Exception:
        pass

    usb_major, usb_minor, usb_sub = bcd_to_version(getattr(dev, "bcdUSB", 0))
    dev_major, dev_minor, dev_sub = bcd_to_version(getattr(dev, "bcdDevice", 0))

    # 🛡️ 実機が「今実際にどのコンフィグレーションで動作しているか」(GET_CONFIGURATION相当)。
    #    旧実装はこれを一切取得しておらず、JS側は複数コンフィグレーションを持つ機器で
    #    常に配列の先頭を暫定activeとして扱っていた(pyusbのDevice.get_active_configuration()
    #    はGET_CONFIGURATIONに相当する情報を返すことをpyusb公式ドキュメント/ソースで確認済み)。
    #    取得できない場合はNoneのままにし、JS側で従来どおり先頭要素へ安全側フォールバックする。
    active_cfg_value = None
    try:
        active_cfg_value = dev.get_active_configuration().bConfigurationValue
    except Exception:
        active_cfg_value = None

    info = {
        "vendorId": dev.idVendor,
        "productId": dev.idProduct,
        "manufacturerName": manufacturer,
        "productName": product,
        "serialNumber": serial,
        "deviceClass": getattr(dev, "bDeviceClass", 0),
        "deviceSubclass": getattr(dev, "bDeviceSubClass", 0),
        "deviceProtocol": getattr(dev, "bDeviceProtocol", 0),
        "usbVersionMajor": usb_major, "usbVersionMinor": usb_minor, "usbVersionSubminor": usb_sub,
        "deviceVersionMajor": dev_major, "deviceVersionMinor": dev_minor, "deviceVersionSubminor": dev_sub,
        "configurations": [],
        "activeConfigurationValue": active_cfg_value,
    }

    if include_configurations:
        info["configurations"] = build_configurations_tree(dev, usb_util)
    return info


def build_configurations_tree(dev, usb_util) -> list:
    """dev配下の全Configuration/Interface(=各AlternateSetting)/Endpointを
    WebUSB仕様相当のツリー構造に変換する。1個の記述子取得に失敗しても
    全体を巻き込んで失敗させない(壊れた/変則的な記述子を持つ安物USB機器が
    実世界には少なくないため)。"""
    configurations = []
    try:
        cfg_iter = list(dev)
    except Exception:
        return configurations

    for cfg in cfg_iter:
        try:
            interfaces_by_number = {}
            for intf in cfg:
                try:
                    inum = intf.bInterfaceNumber
                    # 🛡️ spec: USBAlternateInterface.interfaceName は
                    #    「interface descriptorのiInterfaceが指すstring descriptorの値」
                    #    (pyusbのInterfaceオブジェクトはiInterface属性を公開していることを
                    #    ソース確認済み)。取得できない/未定義な機器も多いため、他の文字列
                    #    記述子(manufacturer/product/serial)と同じくベストエフォートで
                    #    Noneにフォールバックする。
                    interface_name = None
                    try:
                        if getattr(intf, "iInterface", 0):
                            interface_name = usb_util.get_string(dev, intf.iInterface)
                    except Exception:
                        pass
                    alt = {
                        "alternateSetting": intf.bAlternateSetting,
                        "interfaceClass": intf.bInterfaceClass,
                        "interfaceSubclass": intf.bInterfaceSubClass,
                        "interfaceProtocol": intf.bInterfaceProtocol,
                        "interfaceProtected": is_protected_interface_class(intf.bInterfaceClass),
                        "interfaceName": interface_name,
                        "endpoints": [],
                    }
                    for ep in intf:
                        try:
                            ep_type = usb_util.endpoint_type(ep.bmAttributes)
                            # 🛡️ spec(USBAlternateInterface constructor): bmAttributesが
                            #    Control Transfer Type(下位2bitが00)を示す記述子は
                            #    endpoints一覧から除外する("There shouldn't be any endpoint
                            #    object belongs to Control Transfer Type" との注記どおり)。
                            if ep_type == usb_util.ENDPOINT_TYPE_CTRL:
                                continue
                            direction = usb_util.endpoint_direction(ep.bEndpointAddress)
                            alt["endpoints"].append({
                                "endpointNumber": ep.bEndpointAddress & 0x0F,
                                "direction": "in" if direction == usb_util.ENDPOINT_IN else "out",
                                "type": {
                                    usb_util.ENDPOINT_TYPE_BULK: "bulk",
                                    usb_util.ENDPOINT_TYPE_INTR: "interrupt",
                                    usb_util.ENDPOINT_TYPE_ISO: "isochronous",
                                }.get(ep_type, "unknown"),
                                "packetSize": getattr(ep, "wMaxPacketSize", 0),
                            })
                        except Exception:
                            continue
                    interfaces_by_number.setdefault(inum, []).append(alt)
                except Exception:
                    continue

            interfaces_list = [
                {"interfaceNumber": inum, "alternates": alts}
                for inum, alts in sorted(interfaces_by_number.items())
            ]
            # 🛡️ spec: USBConfiguration.configurationName は
            #    「configuration descriptorのiConfigurationが指すstring descriptorの値」
            #    (pyusbのConfigurationオブジェクトはiConfiguration属性を公開していることを
            #    ソース確認済み)。
            configuration_name = None
            try:
                if getattr(cfg, "iConfiguration", 0):
                    configuration_name = usb_util.get_string(dev, cfg.iConfiguration)
            except Exception:
                pass
            configurations.append({
                "configurationValue": getattr(cfg, "bConfigurationValue", 0),
                "configurationName": configuration_name,
                "interfaces": interfaces_list,
            })
        except Exception:
            continue
    return configurations


def interface_class_for(dev, interface_number):
    """指定インターフェース番号のbInterfaceClassを取得する
    (claimInterface時のHID等ブロック判定に使う)。取得できない場合は
    -1ではなくNoneを返す。
    🛡️ 修正: 以前は見つからない場合に-1を返していたが、int(-1)は例外を
    投げないため is_protected_interface_class() の「判定不能なら安全側(拒否)に
    倒す」というTypeError/ValueErrorフォールバックを素通りしてしまい、
    -1 not in PROTECTED_INTERFACE_CLASSES → False(=保護対象でない)と
    誤判定されていた(実際にclaimInterface()でこの経路を通ると確認済み)。
    Noneであればint(None)がTypeErrorを送出するため、既存のフォールバックが
    意図どおり「不明なら拒否」として機能する。"""
    try:
        for cfg in dev:
            for intf in cfg:
                if intf.bInterfaceNumber == interface_number:
                    return intf.bInterfaceClass
    except Exception:
        pass
    return None


# ============================================================
# 5) 接続監視(connect/disconnectイベント)
# ============================================================
class UsbHotplugWatcher:
    """
    navigator.usb の 'connect'/'disconnect' イベント相当を実現するための
    ポーリング監視。pyusbはOS横断のホットプラグコールバックAPIを持たない
    (libusb自体にはあるがOS依存で不安定なため)、実用上は短間隔ポーリングで
    十分かつ確実。

    QTimer等のイベントループ駆動はbridge.py側(Qt依存)に任せ、
    このクラスはQtに依存しない「純粋な差分検出ロジック」だけを提供する。
    呼び出し側は一定間隔で poll() を呼び、戻り値の (connected, disconnected)
    のうち許可済みオリジンに関係するものだけをJSへ 'connect'/'disconnect'
    として配送する。
    """

    def __init__(self, pyusb_finder):
        # pyusb_finder: 引数無しで呼ぶと現在のUSBデバイス一覧
        # (vendor_id, product_id)のsetを返す callable
        self._pyusb_finder = pyusb_finder
        self._known = set()
        self._lock = threading.Lock()
        self._last_poll = 0.0

    def poll(self):
        with self._lock:
            try:
                current = self._pyusb_finder()
            except Exception:
                return [], []
            connected = sorted(current - self._known)
            disconnected = sorted(self._known - current)
            self._known = current
            self._last_poll = time.time()
            return connected, disconnected


# ============================================================
# 6) requestDevice()/getDevices() のフィルタ照合 (WebUSB仕様 §5 Device Enumeration)
# ============================================================
# 出典: https://wicg.github.io/webusb/#dom-usb-requestdevice 内で定義されている
#   「A USB device device matches a device filter filter」
#   「A USB interface interface matches an interface filter filter」
#   「A USBDeviceFilter filter is valid」
# の3アルゴリズムをそのまま移植したもの。
#
# 旧実装はrequestDevice(options)に渡されたoptions.filters/exclusionFiltersを
# 一切参照しておらず、チューザーダイアログには(ブロックリスト機器を除く)
# 接続中の全デバイスが常に表示されていた。仕様は「filtersに一致しない
# デバイスは列挙結果から除外する」ことを必須(MUST)としており、これは
# 単なる見た目の問題ではない: 汎用WebUSB SDKの中にはfilters無し
# (=呼び出し側のミス)や意図せず広すぎるfiltersを渡すものもあり、
# 本来隠れているはずの無関係なデバイスまでチューザーに出てしまうと
# ユーザーが誤って無関係な(あるいは無関係に見えて実は機微な)デバイスを
# 選んでしまうリスクがある。
#
# is_valid_usb_device_filter()の妥当性チェック自体はJS側
# (WEBUSB_POLYFILL_JS)でTypeErrorとして先に弾く設計とし、ここへ渡って
# くるfiltersは構造的に妥当なものだけという前提を置いている(Python側は
# 念のため存在チェック程度は行うが、ここでの主目的は「一致判定」)。
def is_valid_usb_device_filter(filt) -> bool:
    """'A USBDeviceFilter filter is valid'(仕様7章相当)。より上位の
    フィールドを伴わない下位フィールドの指定はinvalid
    (例: vendorId無しでproductIdだけを指定 等)。"""
    if not isinstance(filt, dict):
        return False
    if "productId" in filt and "vendorId" not in filt:
        return False
    if "subclassCode" in filt and "classCode" not in filt:
        return False
    if "protocolCode" in filt and "subclassCode" not in filt:
        return False
    return True


def _device_interface_class_tuples(dev) -> list:
    """このデバイスが持つ全インターフェース(・全alternate)の
    (bInterfaceClass, bInterfaceSubClass, bInterfaceProtocol)を集めたもの。
    フィルタのclassCode照合で「インターフェース単位のクラス」も見るために使う
    (仕様: デバイス全体のbDeviceClassだけでなく、いずれかのインターフェースが
    一致すればそれだけでデバイス全体もmatch扱いになる。複合デバイス
    [composite device] がbDeviceClass=0xFF[vendor-specific]を名乗りつつ
    個々のインターフェースで実際のクラスを申告するケースに対応するため)。
    記述子が壊れている/読めない機器を1台巻き込んで全体を失敗させないよう、
    個々の失敗は握りつぶして安全側(=そのインターフェースは無視)に倒す。"""
    tuples = []
    try:
        for cfg in dev:
            try:
                for intf in cfg:
                    try:
                        tuples.append((intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol))
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return tuples


def _interface_matches_filter(iface_triplet, filt) -> bool:
    """'A USB interface interface matches an interface filter filter'"""
    cls, sub, proto = iface_triplet
    if "classCode" in filt and cls != filt["classCode"]:
        return False
    if "subclassCode" in filt and sub != filt["subclassCode"]:
        return False
    if "protocolCode" in filt and proto != filt["protocolCode"]:
        return False
    return True


def device_matches_usb_filter(dev, usb_util, filt) -> bool:
    """'A USB device device matches a device filter filter'(仕様の手順をそのまま)。
    filtに存在しないキーは無条件一致(=絞り込みなし)として扱う。"""
    try:
        if not isinstance(filt, dict):
            return False
        if "vendorId" in filt and getattr(dev, "idVendor", None) != filt["vendorId"]:
            return False
        if "productId" in filt and getattr(dev, "idProduct", None) != filt["productId"]:
            return False
        if "serialNumber" in filt:
            serial = None
            try:
                if getattr(dev, "iSerialNumber", 0):
                    serial = usb_util.get_string(dev, dev.iSerialNumber)
            except Exception:
                serial = None  # 仕様: 読み取りエラー時はmismatch扱い
            if serial != filt["serialNumber"]:
                return False
        if "classCode" in filt:
            iface_tuples = _device_interface_class_tuples(dev)
            if any(_interface_matches_filter(t, filt) for t in iface_tuples):
                return True  # 仕様どおり: いずれか1つのインターフェースが一致すれば即match
            if getattr(dev, "bDeviceClass", None) != filt["classCode"]:
                return False
            if "subclassCode" in filt and getattr(dev, "bDeviceSubClass", None) != filt["subclassCode"]:
                return False
            if "protocolCode" in filt and getattr(dev, "bDeviceProtocol", None) != filt["protocolCode"]:
                return False
        return True
    except Exception:
        return False


def device_matches_any_usb_filter(dev, usb_util, filters) -> bool:
    """filtersが空リストの場合は仕様どおり「一致するものなし」
    (=1台も候補に残らない)。サイトが「フィルタなしで全デバイスを見せたい」
    場合、仕様上は filters: [{}] (空オブジェクト。どのフィールドも
    指定しないので無条件一致)を渡す必要がある——これは実際のChromiumの
    挙動も同じで、本実装だけの制約ではない。"""
    if not filters:
        return False
    try:
        return any(device_matches_usb_filter(dev, usb_util, f) for f in filters)
    except Exception:
        return False


# ============================================================
# 7) 転送失敗のうち「STALL」を仕様どおりUSBTransferStatus('stall')として扱う
# ============================================================
# 出典: https://wicg.github.io/webusb/#usbtransferstatus および6.1.2節の
# transferIn()等の仕様、および仕様6節の使用例(データチャンネルがエラーを
# STALLで通知し、呼び出し側がresult.status==='stall'を見てclearHalt()で
# 解除してから続行する、という一連の流れそのものが仕様の主要な用例)。
# 実際のブラウザはSTALLをPromiseのrejectではなく、status:'stall'を伴う
# "成功"resolveとして返す。
#
# pyusb(libusb1バックエンド)はSTALL(libusbのLIBUSB_ERROR_PIPE)を検出すると
# usb.core.USBError を送出し、errno=32("Pipe error")として報告することを、
# 複数件のpyusb issue/discussion(pyusb/pyusb#207, #351, #414, #415等、
# Linux/Windows双方の報告例)で確認した。この errno=32 は生のOS errnoでは
# なく、pyusbが usb/backend/libusb1.py 内で libusbのエラーコードから
# 固定的に変換した値であるため、OS非依存で信頼できる。
def is_stall_error(exc) -> bool:
    try:
        if getattr(exc, "errno", None) == 32:
            return True
    except Exception:
        pass
    try:
        msg = str(exc).lower()
        return "pipe error" in msg or "stall" in msg
    except Exception:
        return False


# ============================================================
# 8) JSへ返すエラーメッセージの無害化
# ============================================================
# pyusb/libusb由来の例外メッセージをそのままJSON化してJS側へ渡していたが、
# バックエンド・OSによっては改行やタブを含むメッセージを返すことがあり、
# 極端に長いメッセージが返る可能性もゼロではない。JSON自体はこれらの文字を
# 正しくエスケープするので壊れはしないが、JS側でのログ表示崩れや、
# 万一の下流ログインジェクションを避けるための保険として正規化する。
def safe_error_str(exc, max_len: int = 500) -> str:
    """例外を、制御文字を含まない・長さの上限が保証された文字列に変換する。
    "SecurityError:"のような、こちら側で明示的に付けている振り分け用の
    接頭辞(文字列リテラルとして自前で組み立てているもので、str(exc)の
    生の出力ではない)には影響しない。"""
    try:
        msg = str(exc)
    except Exception:
        return "unknown error"
    msg = msg.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    if len(msg) > max_len:
        msg = msg[:max_len] + "…"
    return msg
