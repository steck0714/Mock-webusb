# -*- coding: utf-8 -*-
"""
bridge.py
=========
navigator.usb ポリフィル用のPython側ブリッジ本体 (WebUSBBridge)。QWebChannel経由で
polyfill.py 側のJSと通信し、実際のUSB通信は pyusb (内部で libusb というC言語ライブラリを
使用) に委譲する。

Typical usage (see polyfill.py's `install()` for the one-line version)::

    from pyside6_webusb import install
    install(web_engine_page)

or manually::

    from pyside6_webusb.bridge import WebUSBBridge
    from PySide6.QtWebChannel import QWebChannel

    bridge = WebUSBBridge(parent=page, settings_organization="MyApp", settings_application="MyApp")
    channel = QWebChannel(page)
    channel.registerObject("pyUsbBridge", bridge)
    page.setWebChannel(channel)
    # ...then inject qwebchannel.js + polyfill.WEBUSB_POLYFILL_JS as a QWebEngineScript
    # at DocumentCreation time. See polyfill.install() for the full wiring.
"""
import json
import time

from PySide6.QtCore import QObject, Signal, Slot, QTimer, QSettings

from .hardening import (
    UsbHotplugWatcher,
    build_device_descriptor,
    device_is_fully_blocked,
    device_matches_any_usb_filter,
    interface_class_for,
    is_blocklisted_device,
    is_protected_interface_class,
    is_stall_error,
    protected_class_name,
    safe_error_str,
)
from .chooser_dialog import WebUsbDeviceChooserDialog


class WebUSBBridge(QObject):
    """
    navigator.usb ポリフィル用のPython側ブリッジ（QWebChannel経由でJSと通信）。
    実際のUSB通信は pyusb（内部で libusb というC言語ライブラリを使用）が担う。
    QtWebEngine自体にはWebUSBのAPIが存在しないため、この仕組みで代替する。

    ★ フェイルセーフ設計: 全ての@Slotメソッドは、例外が絶対にQtのメタオブジェクト
    呼び出し境界を越えて漏れないよう、メソッド全体を単一のtry/exceptで包んでいる。
    PySide6ではQtのC++側から呼ばれるスロット内で未捕捉のPython例外が発生すると、
    単に無視されるだけでなくアプリケーション全体が強制終了する場合があるため、
    「何が起きても必ず有効なJSON文字列を返す」ことを徹底している。

    セキュリティモデルの要点(詳しくはREADME参照):
      - Audio/HID/Mass Storage/Hub/Smart Card/Video/Audio-Video/Wireless Controller
        の8つの「保護対象インターフェースクラス」はclaimInterface自体を拒否する
        (WebUSB仕様 #protected-interface-classes が定めるものと同じ一覧)。
      - 上記に加えて、既知のFIDO/セキュリティキー製品をvendor_id/product_id単位で
        ブロックリスト化(Chromiumのusb_blocklist.cc準拠)。
      - claimInterfaceの保護は controlTransferIn/Out からも回避できない:
        requestType:'class'や recipient:'interface'/'endpoint' で保護対象クラスや
        未claimのインターフェースを直接狙う呼び出しは、実機に届く前に
        _control_transfer_validation_error() で拒否する
        (WebUSB仕様の「check the validity of the control transfer parameters」
        アルゴリズム相当)。
      - requestDevice()はユーザーが実際に選んだ1台の情報しかサイトに渡さない
        (チューザーダイアログでの明示的な操作が必須)。
      - オリジン単位の許可の一覧化・個別失効・全失効API。
    """

    # 🛡️ navigator.usb の 'connect'/'disconnect' 相当。JSON化したデバイス記述子を
    #    引数に取る。許可されていないオリジンには(JS側で)配送しない。
    deviceConnected = Signal(str)
    deviceDisconnected = Signal(str)

    def __init__(self, browser_window=None, parent=None,
                 settings_organization="pyside6-webusb", settings_application="WebUSBBridge"):
        """
        browser_window: 任意。`.settings` 属性(QSettingsオブジェクト)を持つホストアプリの
            メインウィンドウ等を渡すと、許可の永続化にそれを使う。渡さない場合は
            QSettings(settings_organization, settings_application) にフォールバックする。
        parent: 通常はこのブリッジを保持する QWebEnginePage を渡す(ページ遷移時に
            開きっぱなしのハンドルを破棄するため)。
        settings_organization / settings_application: browser_windowが無い場合に使う
            QSettingsの組織名・アプリ名。ホストアプリ独自の値を渡すことを推奨する
            (省略時は "pyside6-webusb"/"WebUSBBridge" になる)。
        """
        super().__init__(parent)
        self.browser_window = browser_window
        self._settings_organization = settings_organization
        self._settings_application = settings_application
        self._open_devices = {}   # handle_id(int) -> {"device":.., "origin":.., "claimed_interfaces": set()}
        self._next_handle = 1
        # 🛡️ requestDeviceChooser()の再入防止フラグ。dlg.exec()はネストしたQtイベント
        #    ループを回すため、その最中に(同じページからの連打や、別タブ/別フレーム
        #    経由で)requestDeviceChooser()がもう一度呼ばれると、このメソッドが
        #    再入してチューザーダイアログが二重に開いてしまう恐れがある
        #    (WebUSBBridgeインスタンスはページ単位なので、同一インスタンスへの
        #    再入だけを防げば十分)。
        self._chooser_active = False
        # 🛡️ ページが別オリジンへ遷移したら、開きっぱなしのUSBハンドルを即座に破棄する。
        #    (サイトAが開いたハンドル番号をサイトBが使い回して乗っ取る、を防ぐ)
        #    parentは実際にはこのブリッジを保持する QWebEnginePage。
        try:
            if parent is not None and hasattr(parent, "urlChanged"):
                parent.urlChanged.connect(self._on_page_navigated)
        except Exception as e:
            print(f"[pyside6-webusb] __init__: 例外を無視: {e}")

        # --- ホットプラグ(接続/切断)監視 ---
        self._hotplug_watcher = None
        self._hotplug_timer = None
        try:
            def _enum_vid_pid_set():
                usb_core, _u = self._pyusb()
                return {(d.idVendor, d.idProduct) for d in usb_core.find(find_all=True)}
            self._hotplug_watcher = UsbHotplugWatcher(_enum_vid_pid_set)
            self._hotplug_timer = QTimer(self)
            self._hotplug_timer.setInterval(1500)  # 1.5秒間隔。頻度と消費電力のバランス
            self._hotplug_timer.timeout.connect(self._poll_hotplug)
            self._hotplug_timer.start()
        except Exception as e:
            print(f"[pyside6-webusb] PyUsbBridge hotplug watcher init: 例外を無視: {e}")

    def _poll_hotplug(self):
        """1.5秒ごとに接続USBデバイス一覧を差分検出し、現在のオリジンに許可済みの
        デバイスについてのみ connect/disconnect をJSへ配送する(未許可オリジンへは
        デバイスの抜き挿し情報すら渡さない=フィンガープリンティング対策)。"""
        if self._hotplug_watcher is None:
            return
        try:
            connected, disconnected = self._hotplug_watcher.poll()
            if not connected and not disconnected:
                return
            origin = self._current_origin()
            if not origin:
                return
            usb_core, usb_util = self._pyusb()
            for vid, pid in connected:
                if not self._is_granted(origin, vid, pid):
                    continue
                try:
                    dev = usb_core.find(idVendor=vid, idProduct=pid)
                    if dev is None:
                        continue
                    info = build_device_descriptor(dev, usb_util, include_configurations=False)
                    self.deviceConnected.emit(json.dumps(info))
                except Exception as e:
                    print(f"[pyside6-webusb] _poll_hotplug(connect): 例外を無視: {e}")
            for vid, pid in disconnected:
                if not self._is_granted(origin, vid, pid):
                    continue
                try:
                    self.deviceDisconnected.emit(json.dumps({"vendorId": vid, "productId": pid}))
                except Exception as e:
                    print(f"[pyside6-webusb] _poll_hotplug(disconnect): 例外を無視: {e}")
        except Exception as e:
            print(f"[pyside6-webusb] _poll_hotplug: 例外を無視: {e}")

    def _pyusb(self):
        """pyusbを遅延インポートし、未インストール環境でも他機能に影響を与えないようにする"""
        import usb.core
        import usb.util
        return usb.core, usb.util

    # ==================== オリジン(サイト単位)権限管理 ====================
    # WebUSB本来の仕様では「どのサイトが許可されたか」をオリジン単位で厳密に区別する。
    # 旧実装はvendorId/productIdだけでゲートしており、任意のWebサイトがgetDevices()で
    # 接続中の全USBデバイスを確認なしで取得でき、openDevice()もチューザーダイアログを
    # 経由せず直接デバイスを掴めてしまっていた。以下はその是正。

    def _origin_from_url(self, qurl):
        """QUrlから 'scheme://host[:port]' 形式の正規化オリジン文字列を作る。
        判定不能な場合はNone(=どのオリジンにも許可を出さない、安全側に倒す)。"""
        try:
            if qurl is None or not qurl.isValid():
                return None
            scheme = (qurl.scheme() or "").lower()
            host = (qurl.host() or "").lower()
            if not scheme or not host:
                return None
            default_ports = {"http": 80, "https": 443}
            port = qurl.port(-1)
            if port == -1 or port == default_ports.get(scheme):
                return f"{scheme}://{host}"
            return f"{scheme}://{host}:{port}"
        except Exception as e:
            print(f"[pyside6-webusb] _origin_from_url: 例外を無視: {e}")
            return None

    def _current_origin(self):
        """このブリッジを保持するページの「現在表示中」のオリジンを取得する。
        JSからの自己申告originを信用するのではなく、Qt/Python側で独立に確認することで、
        ページ自身(=攻撃者が完全に制御できる側)による偽装を防ぐのが目的。"""
        page = self.parent()
        if page is None or not hasattr(page, "url"):
            return None
        try:
            return self._origin_from_url(page.url())
        except Exception as e:
            print(f"[pyside6-webusb] _current_origin: 例外を無視: {e}")
            return None

    def _get_open_device(self, handle_id):
        """ハンドルからusb.core.Deviceを取り出す。ハンドルを開いた本人(オリジン)と
        現在のオリジンが一致しない場合はNoneを返す(サイトを跨いだハンドル乗っ取り防止)。"""
        info = self._open_devices.get(handle_id)
        if info is None:
            return None
        if info.get("origin") != self._current_origin():
            return None
        return info.get("device")

    def _on_page_navigated(self, *_args):
        """別オリジンへ遷移した瞬間、開いていたUSBハンドルを破棄する。
        ★ current(遷移先のオリジン)がNone(=判定不能。about:blank等)の場合は、
        「安全にどのハンドルも維持できる根拠がない」とみなし、個別のorigin比較に
        頼らず全ハンドルを破棄する(念のための安全側強化。理論上は_grant()が
        falsyなoriginへの許可を発行しないためinfo["origin"]がNoneになることは
        無いはずだが、比較ロジックだけに依存しない形にしておく)。"""
        try:
            current = self._current_origin()
            if current is None:
                stale_ids = list(self._open_devices.keys())
            else:
                stale_ids = [hid for hid, info in self._open_devices.items() if info.get("origin") != current]
            for hid in stale_ids:
                info = self._open_devices.pop(hid, None)
                if info and info.get("device") is not None:
                    try:
                        _usb_core, usb_util = self._pyusb()
                        usb_util.dispose_resources(info["device"])
                    except Exception as e:
                        print(f"[pyside6-webusb] _on_page_navigated: 例外を無視: {e}")
        except Exception as e:
            print(f"[pyside6-webusb] _on_page_navigated: 例外を無視: {e}")

    def _known_device_settings(self):
        """設定を保存するQSettingsを取得する。browser_window経由で取得できない場合でも、
        コンストラクタで指定された(既定は"pyside6-webusb"/"WebUSBBridge")
        QSettings(organization, application)へフォールバックし、常に永続化できるようにする。"""
        try:
            if self.browser_window is not None and hasattr(self.browser_window, "settings"):
                s = self.browser_window.settings
                if s is not None:
                    return s
        except Exception as e:
            print(f"[pyside6-webusb] _known_device_settings: 例外を無視: {e}")
        try:
            return QSettings(self._settings_organization, self._settings_application)
        except Exception as e:
            print(f"[pyside6-webusb] _known_device_settings(fallback): 例外を無視: {e}")
            return None

    def _load_granted_origins(self):
        """{origin: [{"vendorId":.., "productId":.., "grantedAt":..}, ...]} を読み込む"""
        s = self._known_device_settings()
        if s is None:
            return {}
        try:
            raw = s.value("webusb_granted_origins", "{}", type=str)
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"[pyside6-webusb] _load_granted_origins: 例外を無視: {e}")
            return {}

    def _save_granted_origins(self, data):
        s = self._known_device_settings()
        if s is None:
            return
        try:
            s.setValue("webusb_granted_origins", json.dumps(data))
        except Exception as e:
            print(f"[pyside6-webusb] _save_granted_origins: 例外を無視: {e}")

    def _is_granted(self, origin, vendor_id, product_id):
        if not origin:
            return False
        grants = self._load_granted_origins().get(origin, [])
        return any(g.get("vendorId") == vendor_id and g.get("productId") == product_id for g in grants)

    def _grant(self, origin, vendor_id, product_id):
        if not origin:
            return
        data = self._load_granted_origins()
        grants = data.setdefault(origin, [])
        if not any(g.get("vendorId") == vendor_id and g.get("productId") == product_id for g in grants):
            grants.append({"vendorId": vendor_id, "productId": product_id, "grantedAt": time.time()})
            self._save_granted_origins(data)

    def _load_known_devices(self):
        """設定に保存済みの既知デバイス一覧（優先順位・接続履歴付き）を取得する"""
        s = self._known_device_settings()
        if s is None:
            return []
        try:
            raw = s.value("webusb_known_devices", "[]", type=str)
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"[pyside6-webusb] _load_known_devices: 例外を無視: {e}")
            return []

    def _save_known_devices(self, devices_list):
        s = self._known_device_settings()
        if s is None:
            return
        try:
            s.setValue("webusb_known_devices", json.dumps(devices_list))
        except Exception as e:
            print(f"[pyside6-webusb] _save_known_devices: 例外を無視: {e}")

    def _record_device_usage(self, vendor_id, product_id, product_name, manufacturer_name):
        """接続したデバイスを既知一覧に記録し、最終接続時刻・接続回数を更新する（優先順位付けに使用）"""
        try:
            devices = self._load_known_devices()
            now = time.time()
            found = False
            for d in devices:
                if d.get("vendorId") == vendor_id and d.get("productId") == product_id:
                    d["lastConnected"] = now
                    d["connectCount"] = d.get("connectCount", 0) + 1
                    if product_name: d["productName"] = product_name
                    if manufacturer_name: d["manufacturerName"] = manufacturer_name
                    found = True
                    break
            if not found:
                devices.append({
                    "vendorId": vendor_id, "productId": product_id,
                    "productName": product_name, "manufacturerName": manufacturer_name,
                    "firstSeen": now, "lastConnected": now, "connectCount": 1,
                    "priority": len(devices),  # 新規追加分は末尾優先度
                })
            self._save_known_devices(devices)
        except Exception as e:
            print(f"[pyside6-webusb] _record_device_usage: 例外を無視: {e}")  # 記録の失敗は致命的ではないため静かに無視（接続自体は継続させる）

    @Slot(result=str)
    def isAvailable(self):
        """pyusb / libusb が実際に使える状態か確認する"""
        try:
            usb_core, _usb_util = self._pyusb()
            usb_core.find()  # バックエンド疎通確認（デバイスの有無は問わない）
            return json.dumps({"available": True})
        except Exception as e:
            return json.dumps({"available": False, "error": safe_error_str(e)})

    @Slot(result=str)
    def listDevices(self):
        """navigator.usb.getDevices() が呼ぶ。WebUSB本来の仕様どおり、
        「現在のオリジンがrequestDevice()で過去に許可したデバイス」だけを返す。
        旧実装は_is_granted()を一切参照せず接続中の全USBデバイスを無条件で返しており、
        任意のサイトがダイアログ無しでベンダーID/製品名等を収集できてしまっていた。"""
        try:
            origin = self._current_origin()
            if not origin:
                return json.dumps({"devices": []})
            usb_core, usb_util = self._pyusb()
            devices = []
            for dev in usb_core.find(find_all=True):
                if not self._is_granted(origin, dev.idVendor, dev.idProduct):
                    continue
                # 🛡️ 既知のセキュリティキー等はオリジンへ許可済みであっても列挙自体を拒否する
                #    (多層防御。本来のブロックはclaimInterface時のインターフェースクラス
                #     チェックが主だが、デバイス単位でも念のため塞ぐ)
                if device_is_fully_blocked(dev):
                    continue
                try:
                    devices.append(build_device_descriptor(dev, usb_util))
                    continue
                except Exception as e:
                    print(f"[pyside6-webusb] listDevices(rich descriptor): 例外を無視: {e}")
                # リッチな記述子の構築に失敗した場合のみ簡易記述子へフォールバックする
                manufacturer = product = None
                try:
                    if dev.iManufacturer:
                        manufacturer = usb_util.get_string(dev, dev.iManufacturer)
                except Exception as e:
                    print(f"[pyside6-webusb] listDevices: 例外を無視: {e}")
                try:
                    if dev.iProduct:
                        product = usb_util.get_string(dev, dev.iProduct)
                except Exception as e:
                    print(f"[pyside6-webusb] listDevices: 例外を無視: {e}")
                devices.append({
                    "vendorId": dev.idVendor,
                    "productId": dev.idProduct,
                    "manufacturerName": manufacturer,
                    "productName": product,
                    "deviceClass": dev.bDeviceClass,
                })
            return json.dumps({"devices": devices})
        except Exception as e:
            return json.dumps({"devices": [], "error": safe_error_str(e)})

    def _enumerate_filtered_devices(self, usb_core, usb_util, filters, exclusion_filters):
        """列挙 + ブロックリスト除外 + filters/exclusionFiltersでの絞り込みを行い、
        チューザー表示用の軽量デバイス記述子のリストを返す。requestDeviceChooser()の
        初回一覧構築と、ダイアログのライブ更新(refresh_callback)の両方から使う
        共通ロジック。"""
        raw_devices = list(usb_core.find(find_all=True))
        devices_info = []
        for dev in raw_devices:
            # 🛡️ 既知のセキュリティキー等はチューザーダイアログの選択肢にすら出さない
            #    (Chromium実装と同様の挙動。ユーザーが誤って許可してしまう余地を無くす)
            if device_is_fully_blocked(dev):
                continue
            # 🛡️ WebUSB仕様どおり、options.filters/exclusionFiltersに一致しない
            #    デバイスはチューザーの候補から除外する。
            if not device_matches_any_usb_filter(dev, usb_util, filters):
                continue
            if exclusion_filters and device_matches_any_usb_filter(dev, usb_util, exclusion_filters):
                continue
            try:
                devices_info.append(build_device_descriptor(dev, usb_util, include_configurations=False))
                continue
            except Exception as e:
                print(f"[pyside6-webusb] _enumerate_filtered_devices(rich descriptor): 例外を無視: {e}")
            manufacturer = product = None
            try:
                if dev.iManufacturer: manufacturer = usb_util.get_string(dev, dev.iManufacturer)
                if dev.iProduct: product = usb_util.get_string(dev, dev.iProduct)
            except Exception as e:
                print(f"[pyside6-webusb] _enumerate_filtered_devices: 例外を無視: {e}")
            devices_info.append({
                "vendorId": dev.idVendor, "productId": dev.idProduct,
                "manufacturerName": manufacturer, "productName": product,
                "deviceClass": dev.bDeviceClass,
            })

        # 既知デバイス（過去に接続実績あり）を優先順位/最終接続日時順に並べ替える
        try:
            known = self._load_known_devices()
            known_map = {(d.get("vendorId"), d.get("productId")): d for d in known}
            def _sort_key(dev):
                k = known_map.get((dev["vendorId"], dev["productId"]))
                if k is None:
                    return (1, 0, 0)  # 未知デバイスは後ろへ
                return (0, -(k.get("connectCount", 0)), -(k.get("lastConnected", 0)))
            devices_info.sort(key=_sort_key)
        except Exception as e:
            print(f"[pyside6-webusb] _enumerate_filtered_devices: 例外を無視: {e}")  # 並べ替えに失敗しても一覧表示自体は継続する
        return devices_info

    @Slot(str, result=str)
    def requestDeviceChooser(self, options_json):
        """navigator.usb.requestDevice() 相当。実処理は _request_device_chooser_impl()
        に委譲し、ここでは再入防止ガードだけを担う。
        🛡️ dlg.exec()(_request_device_chooser_impl内)はネストしたQtイベントループを
        回すため、その最中に同じWebUSBBridgeインスタンスへ対してもう一度
        requestDeviceChooser()が呼ばれる(ページの連打や、QWebChannelメッセージが
        ネストループ中に処理される等)と、チューザーダイアログが二重に開いてしまう
        恐れがある。try/finallyで確実にフラグを解除することで、内部実装側の
        どの return/例外経路を通っても再入状態が残留しないようにしている。"""
        if self._chooser_active:
            return json.dumps({
                "cancelled": True,
                "error": "InvalidStateError: a device chooser is already open for this page",
            })
        self._chooser_active = True
        try:
            return self._request_device_chooser_impl(options_json)
        finally:
            self._chooser_active = False

    def _request_device_chooser_impl(self, options_json):
        """navigator.usb.requestDevice() の実処理本体。実デバイス選択ダイアログを表示し、
        ユーザーが明示的に選んだ場合のみデバイス情報を返す（WebUSB本来のセキュリティ設計を踏襲）。
        ★ メソッド全体を try/except で包み、ダイアログ表示中の例外でアプリが落ちないようにしている。
        ★ options.filters/exclusionFiltersによる絞り込み(WebUSB仕様
        「requestDevice(options)」のenumerate〜絞り込み手順を再現)。filters自体の
        必須チェック・各フィルタの妥当性検証("is a valid filter")はJS側
        (WEBUSB_POLYFILL_JS)で完了させた上でここへ渡す設計なので、ここでは
        構造的に妥当なfilters/exclusionFiltersが来る前提で一致判定だけを行う。
        filtersが空リストの場合は仕様どおり「一致するデバイスなし」となる。
        ★ Chromeの実際のチューザーを参考に、(1)要求元オリジンを明示、
        (2)ダイアログを開いたままの接続/切断でライブ更新、を行う。
        ★ @Slotをあえて付けていない: QWebChannel/JSから直接叩けるのは
        requestDeviceChooser()(再入防止ガード込み)だけにするため。"""
        try:
            try:
                options = json.loads(options_json) if options_json else {}
                if not isinstance(options, dict):
                    options = {}
            except Exception as e:
                print(f"[pyside6-webusb] requestDeviceChooser(options parse): 例外を無視: {e}")
                options = {}
            filters = options.get("filters")
            exclusion_filters = options.get("exclusionFilters")
            filters = filters if isinstance(filters, list) else []
            exclusion_filters = exclusion_filters if isinstance(exclusion_filters, list) else []

            try:
                usb_core, usb_util = self._pyusb()
            except Exception as e:
                return json.dumps({"cancelled": True, "error": safe_error_str(e)})

            try:
                devices_info = self._enumerate_filtered_devices(usb_core, usb_util, filters, exclusion_filters)
            except Exception as e:
                return json.dumps({"cancelled": True, "error": safe_error_str(e)})

            def _refresh():
                # 🛡️ ダイアログが開いている間、新しく挿された/抜かれたデバイスを
                #    反映する(Chromeのチューザーと同じ挙動)。再度pyusbから
                #    取り直す必要があるため、ここでも_pyusb()を呼び直す。
                u_core, u_util = self._pyusb()
                return self._enumerate_filtered_devices(u_core, u_util, filters, exclusion_filters)

            parent = None
            try:
                from PySide6.QtWidgets import QApplication
                parent = QApplication.activeWindow()
            except Exception as e:
                print(f"[pyside6-webusb] requestDeviceChooser(activeWindow): 例外を無視: {e}")
                parent = None

            try:
                dlg = WebUsbDeviceChooserDialog(
                    devices_info, parent,
                    origin=self._current_origin(),
                    refresh_callback=_refresh,
                )
                result = dlg.exec()
                accepted = (result == WebUsbDeviceChooserDialog.DialogCode.Accepted)
                selected = dlg.selected_device if accepted else None
            except Exception as e:
                return json.dumps({"cancelled": True, "error": f"Dialog error: {e}"})

            if selected is not None:
                try:
                    self._record_device_usage(
                        selected.get("vendorId"), selected.get("productId"),
                        selected.get("productName"), selected.get("manufacturerName"))
                except Exception as e:
                    print(f"[pyside6-webusb] requestDeviceChooser: 例外を無視: {e}")
                # ユーザーがダイアログで明示的に選んだ場合のみ、このオリジンに対する
                # 恒久的な許可を記録する(listDevices/openDeviceはこれを介してのみ許可を判定する)。
                try:
                    self._grant(self._current_origin(), selected.get("vendorId"), selected.get("productId"))
                except Exception as e:
                    print(f"[pyside6-webusb] requestDeviceChooser: 例外を無視: {e}")
                # 🛡️ チューザー一覧はパフォーマンスのため軽量記述子(configurations無し)で
                #    構築しているが、requestDevice()がJSへ返す「選ばれた1台」は
                #    getDevices()と同じリッチな記述子でなければならない。仕様6節の
                #    使用例が示すとおり、requestDevice()の戻り値へ直接
                #    .open()→.selectConfiguration()→.claimInterface() を呼ぶのが
                #    標準的な使い方であり、configurationsが空だとこの一連の流れが
                #    機能しない(旧実装はここが軽量記述子のまま返ってしまっていた)。
                rich_selected = selected
                try:
                    real_dev = usb_core.find(idVendor=selected.get("vendorId"), idProduct=selected.get("productId"))
                    if real_dev is not None:
                        rich_selected = build_device_descriptor(real_dev, usb_util, include_configurations=True)
                except Exception as e:
                    print(f"[pyside6-webusb] requestDeviceChooser(rich rebuild): 例外を無視: {e}")
                return json.dumps({"cancelled": False, "device": rich_selected})
            return json.dumps({"cancelled": True})
        except Exception as e:
            # 最外殻の保険: ここまでの個別try/exceptで拾いきれない想定外の例外も必ず捕捉する
            return json.dumps({"cancelled": True, "error": f"Unexpected error: {e}"})

    @Slot(int, int, result=str)
    def openDevice(self, vendor_id, product_id):
        """requestDeviceChooser()で許可されたオリジンだけがデバイスを開けるようにする。
        旧実装はvendorId/productIdさえ知っていれば任意のサイトが直接開けてしまっていた
        (チューザーダイアログを経由しないバイパス経路)。ここで許可をゲートする。"""
        try:
            origin = self._current_origin()
            if not self._is_granted(origin, vendor_id, product_id):
                return json.dumps({"success": False, "error": "Permission denied: this origin has not been granted access to this device"})
            if is_blocklisted_device(vendor_id, product_id):
                return json.dumps({"success": False, "error": "SecurityError: this device is on the protected security-key blocklist and cannot be accessed via WebUSB"})
            usb_core, _usb_util = self._pyusb()
            dev = usb_core.find(idVendor=vendor_id, idProduct=product_id)
            if dev is None:
                return json.dumps({"success": False, "error": "Device not found"})
            handle_id = self._next_handle
            self._next_handle += 1
            self._open_devices[handle_id] = {"device": dev, "origin": origin, "claimed_interfaces": set()}
            return json.dumps({"success": True, "handle": handle_id})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    @Slot(int)
    def closeDevice(self, handle_id):
        try:
            # 別オリジンのハンドルは「存在しない」ものとして扱う(_get_open_deviceがオリジン照合する)
            if self._get_open_device(handle_id) is None:
                return
            info = self._open_devices.pop(handle_id, None)
            dev = info.get("device") if info else None
            if dev is not None:
                try:
                    _usb_core, usb_util = self._pyusb()
                    usb_util.dispose_resources(dev)
                except Exception as e:
                    print(f"[pyside6-webusb] closeDevice: 例外を無視: {e}")
        except Exception as e:
            print(f"[pyside6-webusb] closeDevice: 例外を無視: {e}")

    @Slot(int, int, result=str)
    def claimInterface(self, handle_id, interface_number):
        """🛡️ WebUSB仕様が定める「保護対象インターフェースクラス」
        (Audio/HID/Mass Storage/Smart Card/Video/Audio-Video/Wireless Controller)は
        ここで一律拒否する。旧実装はインターフェースクラスを一切見ておらず、
        オリジンへの許可さえあればセキュリティキーやキーボードのHIDインターフェースにも
        生アクセスできてしまっていた(WebHID等、別の専用APIが本来担うべき領域)。"""
        try:
            info = self._open_devices.get(handle_id)
            dev = self._get_open_device(handle_id)
            if dev is None:
                return json.dumps({"success": False, "error": "Invalid device handle"})

            iface_class = interface_class_for(dev, interface_number)
            if is_protected_interface_class(iface_class):
                name = protected_class_name(iface_class)
                return json.dumps({
                    "success": False,
                    "error": f"SecurityError: interface {interface_number} is class '{name}', "
                             f"which is a protected interface class and cannot be claimed via WebUSB",
                })

            _usb_core, usb_util = self._pyusb()
            try:
                if dev.is_kernel_driver_active(interface_number):
                    dev.detach_kernel_driver(interface_number)
            except Exception as e:
                print(f"[pyside6-webusb] claimInterface: 例外を無視: {e}")  # OSによっては未対応/不要な場合がある
            usb_util.claim_interface(dev, interface_number)
            if info is not None:
                info.setdefault("claimed_interfaces", set()).add(interface_number)
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    @Slot(int, int, result=str)
    def releaseInterface(self, handle_id, interface_number):
        """旧実装はJS側のreleaseInterface()がno-op(Promise.resolve()するだけ)で
        Python側に一切届いておらず、一度claimしたインターフェースは
        デバイスを閉じるまで解放されなかった。"""
        try:
            info = self._open_devices.get(handle_id)
            dev = self._get_open_device(handle_id)
            if dev is None:
                return json.dumps({"success": False, "error": "Invalid device handle"})
            _usb_core, usb_util = self._pyusb()
            usb_util.release_interface(dev, interface_number)
            if info is not None:
                info.get("claimed_interfaces", set()).discard(interface_number)
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    @Slot(int, int, result=str)
    def selectConfiguration(self, handle_id, configuration_value):
        """旧実装はJS側のselectConfiguration()がno-opで、複数コンフィグレーションを
        持つデバイスでは常に(pyusbが自動選択した)最初のコンフィグレーションしか
        使えなかった。"""
        try:
            dev = self._get_open_device(handle_id)
            if dev is None:
                return json.dumps({"success": False, "error": "Invalid device handle"})
            dev.set_configuration(configuration_value)
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    @Slot(int, int, int, result=str)
    def selectAlternateInterface(self, handle_id, interface_number, alternate_setting):
        """USBInterface.selectAlternateInterface() 相当。pyusbの
        Device.set_interface_altsetting()へ実配線する(調査の結果、pyusb 1.x系の
        公開APIとして存在することを確認済み)。旧実装はJS側で常にNotSupportedErrorを
        返すだけのスタブだった。
        ★ 保護対象インターフェースクラスの判定は「インターフェース番号」単位で
        行っており、alternate setting違いでクラスが変わるような変則的デバイスは
        (稀だが)想定していない。claimInterfaceの時点で拒否されていれば
        そもそもこのSlotへは到達しない。"""
        try:
            dev = self._get_open_device(handle_id)
            if dev is None:
                return json.dumps({"success": False, "error": "Invalid device handle"})
            dev.set_interface_altsetting(interface=interface_number, alternate_setting=alternate_setting)
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    @Slot(int, result=str)
    def resetDevice(self, handle_id):
        """USBDevice.reset() 相当。デバイスのUSBバスリセットを行う。"""
        try:
            dev = self._get_open_device(handle_id)
            if dev is None:
                return json.dumps({"success": False, "error": "Invalid device handle"})
            dev.reset()
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    @Slot(int, str, int, result=str)
    def clearHalt(self, handle_id, direction, endpoint_number):
        """USBDevice.clearHalt(direction, endpointNumber) 相当。
        directionは 'in' または 'out'。"""
        try:
            dev = self._get_open_device(handle_id)
            if dev is None:
                return json.dumps({"success": False, "error": "Invalid device handle"})
            _usb_core, usb_util = self._pyusb()
            address = endpoint_number | (0x80 if direction == "in" else 0x00)
            dev.clear_halt(address)
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    @Slot(int, int, int, result=str)
    def bulkTransferIn(self, handle_id, endpoint, length):
        """USBDevice.transferIn(endpointNumber, length) 相当。
        🛡️ 実仕様: 'Let endpointAddress be endpointNumber | 0x80' — JSから渡ってくる
        endpointはIN/OUTの方向ビットを含まない生のendpointNumber(spec/JS両方の呼称)
        であり、実際にpyusbへ渡す必要があるbEndpointAddress(方向ビット込み)へは
        ここで変換しなければならない(pyusb公式ドキュメント: 'The endpoint parameter
        corresponds to the bEndpointAddress member' — endpointNumberそのものではない)。
        旧実装はこの変換が丸ごと抜けており、endpoint=1のIN転送がbEndpointAddress=0x01
        (=同じ番号のOUT側)を叩きにいってしまい、実機相手には常に失敗していた。"""
        try:
            dev = self._get_open_device(handle_id)
            if dev is None:
                return json.dumps({"success": False, "error": "Invalid device handle"})
            endpoint_address = endpoint | 0x80
            try:
                data = dev.read(endpoint_address, length, timeout=5000)
            except Exception as e:
                # 🛡️ 実仕様: STALLはPromiseのreject対象ではなく、status:'stall'を
                #    伴う"成功"resolveとして返す(呼び出し側がclearHalt()で解除して
                #    続行するのが仕様が想定する標準的な流れ)。それ以外の失敗理由は
                #    従来どおり外側のexceptでNetworkError相当として扱う。
                if is_stall_error(e):
                    return json.dumps({"success": True, "status": "stall", "data": ""})
                raise
            return json.dumps({"success": True, "status": "ok", "data": bytes(data).hex()})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    @Slot(int, int, str, result=str)
    def bulkTransferOut(self, handle_id, endpoint, data_hex):
        try:
            dev = self._get_open_device(handle_id)
            if dev is None:
                return json.dumps({"success": False, "error": "Invalid device handle"})
            try:
                written = dev.write(endpoint, bytes.fromhex(data_hex), timeout=5000)
            except Exception as e:
                if is_stall_error(e):
                    return json.dumps({"success": True, "status": "stall", "bytesWritten": 0})
                raise
            return json.dumps({"success": True, "status": "ok", "bytesWritten": written})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    def _control_transfer_validation_error(self, handle_id, dev, request_type, request, index):
        """WebUSB仕様「check the validity of the control transfer parameters」
        (index.bs, controlTransferIn/Outの手前で毎回走る前提のアルゴリズム)の
        うちPython側でしか判定できない部分 -- インターフェースが保護対象クラスか、
        実際にclaim済みか -- をここで最終防衛として検証する。
        🛡️ この検証が無いと、claimInterface()自体は保護対象クラス(HID等)を
        拒否していても、controlTransferIn/Out に requestType:'class' や
        recipient:'interface'/'endpoint' を指定して直接その保護対象インターフェースへ
        生のコントロール転送を送れてしまい、claimInterfaceの保護を完全にバイパス
        できる状態だった(実際にJS/Python双方のコードを読んで確認した抜け穴で、
        テストも無かった)。
        妥当なら None、そうでなければ json.dumps済みのエラーレスポンス文字列を返す。
        bmRequestType(request_type)からの復号は仕様どおりのビット割り当て:
          bit7    : 方向(1=IN)。ここではrequestTypeそのものから読めるので
                    JSから別途directionを受け取る必要が無い。
          bit6-5  : requestType種別(00=standard, 01=class, 10=vendor)
          bit1-0  : recipient種別(00=device, 01=interface, 10=endpoint, 11=other)
        (polyfill.py側のreqType組み立てロジックと1対1で対応する)。"""
        info = self._open_devices.get(handle_id) or {}
        claimed = info.get("claimed_interfaces", set())
        direction_in = bool(request_type & 0x80)
        req_kind = (request_type >> 5) & 0x03      # 0=standard,1=class,2=vendor
        recipient = request_type & 0x03            # 0=device,1=interface,2=endpoint,3=other

        def _err(name, msg):
            return json.dumps({"success": False, "error": f"{name}: {msg}"})

        if req_kind == 0:  # standard
            if not direction_in:
                return _err("SecurityError", "standard requests are not allowed for controlTransferOut")
            if request not in (0x00, 0x06, 0x08, 0x0A, 0x0C):
                return _err(
                    "SecurityError",
                    f"standard request {request:#04x} is not one of the requests allowed by the "
                    "WebUSB spec (GET_STATUS/GET_DESCRIPTOR/GET_CONFIGURATION/GET_INTERFACE/SYNCH_FRAME)",
                )

        if req_kind == 1:  # class
            iface_number = index & 0xFF
            iface_class = interface_class_for(dev, iface_number)
            if is_protected_interface_class(iface_class):
                name = protected_class_name(iface_class)
                return _err(
                    "SecurityError",
                    f"interface {iface_number} is class '{name}', a protected interface class, "
                    "and cannot receive class-specific control requests",
                )

        if recipient == 1:  # interface
            iface_number = index & 0xFF
            iface_class = interface_class_for(dev, iface_number)
            if iface_class is None:
                return _err("NotFoundError", f"interface {iface_number} was not found on this device")
            if is_protected_interface_class(iface_class):
                name = protected_class_name(iface_class)
                return _err("SecurityError", f"interface {iface_number} is class '{name}', a protected interface class")
            if iface_number not in claimed:
                return _err("InvalidStateError", f"interface {iface_number} has not been claimed")

        if recipient == 2:  # endpoint
            # 仕様: recipient=="endpoint"の場合は setup.index そのものがendpointAddress
            # (interface/classのように下位8bitへ切り詰めない)。実運用上のindexは
            # 常に1バイトに収まる値なので & 0xFF は安全側の正規化として扱う。
            endpoint_address = index & 0xFF
            owner_number, owner_class = None, None
            try:
                for cfg in dev:
                    for intf in cfg:
                        for ep in intf:
                            if getattr(ep, "bEndpointAddress", None) == endpoint_address:
                                owner_number, owner_class = intf.bInterfaceNumber, intf.bInterfaceClass
                                raise StopIteration
            except StopIteration:
                pass
            except Exception:
                pass
            if owner_number is None:
                return _err("NotFoundError", f"endpoint {endpoint_address:#04x} was not found on this device")
            if is_protected_interface_class(owner_class):
                name = protected_class_name(owner_class)
                return _err(
                    "SecurityError",
                    f"endpoint {endpoint_address:#04x} belongs to interface class '{name}', "
                    "a protected interface class",
                )
            if owner_number not in claimed:
                return _err("InvalidStateError", f"interface {owner_number} owning endpoint {endpoint_address:#04x} has not been claimed")

        return None

    @Slot(int, int, int, int, int, int, result=str)
    def controlTransferIn(self, handle_id, request_type, request, value, index, length):
        try:
            dev = self._get_open_device(handle_id)
            if dev is None:
                return json.dumps({"success": False, "error": "Invalid device handle"})
            validation_error = self._control_transfer_validation_error(handle_id, dev, request_type, request, index)
            if validation_error is not None:
                return validation_error
            try:
                data = dev.ctrl_transfer(request_type, request, value, index, length, timeout=5000)
            except Exception as e:
                if is_stall_error(e):
                    return json.dumps({"success": True, "status": "stall", "data": ""})
                raise
            return json.dumps({"success": True, "status": "ok", "data": bytes(data).hex()})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    @Slot(int, int, int, int, int, str, result=str)
    def controlTransferOut(self, handle_id, request_type, request, value, index, data_hex):
        try:
            dev = self._get_open_device(handle_id)
            if dev is None:
                return json.dumps({"success": False, "error": "Invalid device handle"})
            validation_error = self._control_transfer_validation_error(handle_id, dev, request_type, request, index)
            if validation_error is not None:
                return validation_error
            try:
                written = dev.ctrl_transfer(request_type, request, value, index, bytes.fromhex(data_hex), timeout=5000)
            except Exception as e:
                if is_stall_error(e):
                    return json.dumps({"success": True, "status": "stall", "bytesWritten": 0})
                raise
            return json.dumps({"success": True, "status": "ok", "bytesWritten": written})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    @Slot(result=str)
    def listKnownDevices(self):
        """設定内に保存済みの既知USBデバイス一覧を返す（設定画面のUSB管理パネル用）"""
        try:
            devices = self._load_known_devices()
            devices.sort(key=lambda d: (-(d.get("connectCount", 0)), -(d.get("lastConnected", 0))))
            return json.dumps({"devices": devices})
        except Exception as e:
            return json.dumps({"devices": [], "error": safe_error_str(e)})

    @Slot(int, int, result=str)
    def forgetKnownDevice(self, vendor_id, product_id):
        """既知デバイス一覧から特定の1台を削除する"""
        try:
            devices = self._load_known_devices()
            new_devices = [d for d in devices
                           if not (d.get("vendorId") == vendor_id and d.get("productId") == product_id)]
            self._save_known_devices(new_devices)
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    @Slot(result=str)
    def forgetAllKnownDevices(self):
        """既知デバイス一覧を全削除する"""
        try:
            self._save_known_devices([])
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    # --- isochronous転送: 明示的に非対応とする ---
    # WebUSB仕様には存在するが、OS横断で確実に動くisochronous実装は難易度が高く、
    # 参照実装であるnode-usb(旧thegecko/webusb後継)自身も「現状未対応」としている。
    # 何もしない/沈黙して失敗するより、呼び出し元が確実にフォールバック処理へ
    # 分岐できるよう、はっきりしたNotSupportedErrorを返す。
    @Slot(int, int, str, result=str)
    def isochronousTransferIn(self, handle_id, endpoint, packet_lengths_json):
        return json.dumps({"success": False, "error": "NotSupportedError: isochronous transfers are not supported by this WebUSB bridge"})

    @Slot(int, int, str, str, result=str)
    def isochronousTransferOut(self, handle_id, endpoint, data_hex, packet_lengths_json):
        return json.dumps({"success": False, "error": "NotSupportedError: isochronous transfers are not supported by this WebUSB bridge"})

    # --- オリジン権限の管理 ---
    @Slot(int, int, result=str)
    def forgetGrantedDevice(self, vendor_id, product_id):
        """USBDevice.forget() 相当。★あえて対象オリジンを引数に取らない:
        現在のページ自身(self._current_origin())の許可だけを取り消せるようにし、
        任意のサイトが他サイトの許可を操作できないようにしている。"""
        try:
            origin = self._current_origin()
            if not origin:
                return json.dumps({"success": False})
            data = self._load_granted_origins()
            grants = data.get(origin, [])
            new_grants = [g for g in grants if not (g.get("vendorId") == vendor_id and g.get("productId") == product_id)]
            if len(new_grants) != len(grants):
                data[origin] = new_grants
                self._save_granted_origins(data)
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"success": False, "error": safe_error_str(e)})

    # ↓↓↓ 以下3つは意図的に @Slot を付けていない(=QWebChannel経由でJS/Webページからは
    # 一切呼び出せない)。任意のオリジン一覧の閲覧・他オリジンの許可取り消しは、
    # 設定画面のようなアプリ内部の信頼された経路からのみ行うべき情報/操作であり、
    # 表示中のWebページに公開すべきではないため。設定UIを追加する際はここから
    # Python側で直接呼び出す想定。
    def list_granted_origins(self):
        """{origin: [{"vendorId":.., "productId":.., "grantedAt":..}, ...]}"""
        try:
            return self._load_granted_origins()
        except Exception as e:
            print(f"[pyside6-webusb] list_granted_origins: 例外を無視: {e}")
            return {}

    def revoke_origin_grant(self, origin, vendor_id, product_id):
        try:
            data = self._load_granted_origins()
            grants = data.get(origin, [])
            new_grants = [g for g in grants if not (g.get("vendorId") == vendor_id and g.get("productId") == product_id)]
            if len(new_grants) != len(grants):
                data[origin] = new_grants
                self._save_granted_origins(data)
                return True
            return False
        except Exception as e:
            # 🛡️ 呼び出し元(設定パネル等)が「取り消しに成功したか」を正しく判断できるよう、
            #    保存失敗時にTrueを誤って返さない。
            print(f"[pyside6-webusb] revoke_origin_grant: 例外を無視: {e}")
            return False

    def revoke_all_for_origin(self, origin):
        try:
            data = self._load_granted_origins()
            if origin in data:
                del data[origin]
                self._save_granted_origins(data)
                return True
            return False
        except Exception as e:
            print(f"[pyside6-webusb] revoke_all_for_origin: 例外を無視: {e}")
            return False

