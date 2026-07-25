# -*- coding: utf-8 -*-
"""
polyfill.py
===========
navigator.usb のJSポリフィル本体(WEBUSB_POLYFILL_JS)と、QWebEnginePageへの
装着を1回の呼び出しで済ませる install() を提供する。

    from pyside6_webusb import install
    install(my_web_engine_page)

だけで、そのページ上のJavaScriptから navigator.usb.getDevices() /
navigator.usb.requestDevice() などが動くようになる(実機の選択はネイティブの
Qtダイアログ、実際のUSB通信はpyusb/libusb経由)。
"""

_QWEBCHANNEL_JS_CACHE = None


def _load_qwebchannel_js():
    """Qt自身が(QtWebChannelモジュールの一部として)同梱しているqwebchannel.jsを
    実行時に読み込む。Qt公式のQWebChannel standaloneサンプルが示す標準的な取得方法
    (QFile(":/qtwebchannel/qwebchannel.js"))を使うことで、本パッケージがQt本体の
    JSファイルを別途同梱・バージョン追従する必要をなくしている
    (qwebchannel.js自体はQt側でBSD-3-Clauseライセンスとして配布されている)。

    PySide6.QtWebChannel を一度でもimportしていないと、このQtリソースパスは
    まだ登録されていないことがある。install()はQWebChannelを内部でimportするため、
    通常このモジュールを直接使わずinstall()経由で呼べば問題にならない。
    """
    global _QWEBCHANNEL_JS_CACHE
    if _QWEBCHANNEL_JS_CACHE is not None:
        return _QWEBCHANNEL_JS_CACHE
    from PySide6.QtCore import QFile, QIODevice
    f = QFile(":/qtwebchannel/qwebchannel.js")
    opened = f.open(QIODevice.OpenModeFlag.ReadOnly | QIODevice.OpenModeFlag.Text)
    if not opened:
        raise RuntimeError(
            "Could not load qwebchannel.js from Qt's built-in resources "
            "(:/qtwebchannel/qwebchannel.js). This usually means PySide6.QtWebChannel "
            "has not been imported yet. install() imports it automatically, so if you "
            "see this error you are likely calling _load_qwebchannel_js() directly, or "
            "your Qt installation does not ship the qtwebchannel resource. As a "
            "workaround, pass qwebchannel_js=<the file's contents> to install() "
            "explicitly (you can find qwebchannel.js inside your Qt/PySide6 installation)."
        )
    data = bytes(f.readAll().data()).decode("utf-8", errors="replace")
    f.close()
    _QWEBCHANNEL_JS_CACHE = data
    return data


def install(page, browser_window=None,
            settings_organization="pyside6-webusb", settings_application="WebUSBBridge",
            qwebchannel_js=None):
    """
    唯一の公開エントリポイント。QWebEnginePage に navigator.usb ポリフィルを装着する。

    page: QWebEnginePage。このページ(と、そのページで開かれる以降のドキュメント)上で
        navigator.usb が有効になる。
    browser_window: 任意。`.settings` (QSettingsインスタンス)属性を持つホストアプリの
        ウィンドウを渡すと、デバイス許可の永続化にそれを使う。省略時は
        settings_organization/settings_applicationでQSettingsへフォールバックする。
    settings_organization / settings_application: browser_window省略時に使う
        QSettingsの組織名・アプリ名(省略時は"pyside6-webusb"/"WebUSBBridge")。
    qwebchannel_js: 通常は不要(Qtの内蔵リソースから自動取得する)。取得に失敗する
        環境向けに、qwebchannel.jsの中身を直接渡すための上書き用パラメータ。

    戻り値: 生成した WebUSBBridge インスタンス(追加の配線やデバッグに使える)。
        QWebChannel自体が使えない環境では例外を送出せず None を返す
        (WebUSB機能だけが無効になり、アプリ全体は落とさない設計)。
    """
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import QWebEngineScript

    from .bridge import WebUSBBridge

    try:
        bridge = WebUSBBridge(browser_window=browser_window, parent=page,
                               settings_organization=settings_organization,
                               settings_application=settings_application)
        channel = QWebChannel(page)
        channel.registerObject("pyUsbBridge", bridge)
        page.setWebChannel(channel)
    except Exception:
        return None  # QWebChannel自体が使えない環境では静かに諦める(アプリ全体は落とさない)

    try:
        qwc_js = qwebchannel_js if qwebchannel_js is not None else _load_qwebchannel_js()
    except Exception as e:
        print(f"[pyside6-webusb] install: qwebchannel.js の読み込みに失敗しました: {e}")
        return bridge  # ブリッジ自体は生成済みだが、スクリプト注入はできていない

    for name, code in (
        ("PySide6WebUSBQWebChannelLib", qwc_js),
        ("PySide6WebUSBPolyfill", WEBUSB_POLYFILL_JS),
    ):
        try:
            script = QWebEngineScript()
            script.setName(name)
            script.setSourceCode(code)
            script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
            script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            script.setRunsOnSubFrames(True)
            page.scripts().insert(script)
        except Exception as e:
            print(f"[pyside6-webusb] install: 例外を無視: {e}")

    return bridge


WEBUSB_POLYFILL_JS = r"""
(function() {
    'use strict';
    if (navigator.usb) return;  // 既にネイティブAPIがあれば上書きしない（将来Qtが対応した場合の保険）
    if (typeof qt === 'undefined' || !qt.webChannelTransport) return;  // QWebChannel未提供の文脈では何もしない
    // 🛡️ 本物のWebUSB同様、セキュアコンテキスト(https/localhost)以外では一切定義しない。
    if (typeof window.isSecureContext !== 'undefined' && !window.isSecureContext) return;

    var _bridgeReady = new Promise(function(resolve) {
        try {
            new QWebChannel(qt.webChannelTransport, function(channel) {
                resolve(channel.objects.pyUsbBridge || null);
            });
        } catch (e) { resolve(null); }
    });

    function callBridge(method) {
        var args = Array.prototype.slice.call(arguments, 1);
        return _bridgeReady.then(function(bridge) {
            if (!bridge) throw new Error('WebUSB bridge unavailable');
            return new Promise(function(resolve) {
                bridge[method].apply(bridge, args.concat([function(res) { resolve(JSON.parse(res)); }]));
            });
        });
    }

    function bytesToHex(view) {
        var arr = view instanceof ArrayBuffer ? new Uint8Array(view) :
                   (view.buffer ? new Uint8Array(view.buffer, view.byteOffset || 0, view.byteLength) : new Uint8Array(view));
        var out = '';
        for (var i = 0; i < arr.length; i++) out += arr[i].toString(16).padStart(2, '0');
        return out;
    }
    function hexToUint8(hex) {
        var bytes = new Uint8Array((hex || '').length / 2);
        for (var i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
        return bytes;
    }

    // 🛡️ 実仕様(wicg.github.io/webusb#dom-usb-requestdevice)の
    //    「A USBDeviceFilter filter is valid」をそのまま再現。より上位の
    //    フィールドを伴わない下位フィールドの指定はTypeErrorで拒否する対象
    //    (例: vendorId無しでproductIdだけを指定 等)。
    function isValidUsbDeviceFilter(f) {
        if (!f || typeof f !== 'object') return false;
        if (('productId' in f) && !('vendorId' in f)) return false;
        if (('subclassCode' in f) && !('classCode' in f)) return false;
        if (('protocolCode' in f) && !('subclassCode' in f)) return false;
        return true;
    }

    // 🛡️ 実WebUSB仕様(wicg.github.io/webusb)を確認して比較した結果に基づく修正。
    //    claimInterface()は仕様上「保護対象クラスによる拒否」だけがSecurityError、
    //    それ以外(ハンドル不正・libusb側のclaim失敗等)はNetworkErrorが正しい
    //    (open()の「ブロックリスト機器による拒否」も同様にSecurityErrorが正しい)。
    //    Python側は保護クラス/ブロックリスト拒否の場合にだけエラー文字列の先頭へ
    //    "SecurityError:"を付けて返す取り決めなので、ここではそれだけを見て
    //    DOMExceptionの種別を仕様どおりに振り分ける。それ以外の失敗はメソッドごとの
    //    デフォルト(通常はNetworkError)のままにする。
    function throwFromResult(res, defaultMessage, defaultErrorName) {
        var msg = (res && res.error) || defaultMessage;
        var name = defaultErrorName || 'NetworkError';
        if (typeof msg === 'string' && msg.indexOf('SecurityError:') === 0) {
            name = 'SecurityError';
            msg = msg.slice('SecurityError:'.length).trim();
        }
        throw new DOMException(msg, name);
    }

    // 🛡️ 実仕様のUSBInterface.alternate/.claimedを再現する。
    //    Python側(webusb_hardening.build_configurations_tree)はinterfaceNumberごとに
    //    alternates配列だけを組み立てて返すため、「今どのalternateが有効か」
    //    「このインターフェースは今claim済みか」はJS側で補う必要がある。
    //    MDN: 「USBInterface.alternate ... By default this is the USBAlternateInterface
    //    from alternates with alternateSetting equal to 0.」を再現(先頭要素決め打ちではなく
    //    alternateSetting===0を探す。無ければ安全側で先頭にフォールバック)。
    function deriveInterfaceState(configurations) {
        (configurations || []).forEach(function(cfg) {
            (cfg.interfaces || []).forEach(function(iface) {
                var alts = iface.alternates || [];
                var zero = alts.filter(function(a) { return a.alternateSetting === 0; })[0];
                iface.alternate = zero || alts[0] || null;
                if (typeof iface.claimed !== 'boolean') iface.claimed = false;
            });
        });
        return configurations;
    }

    function setInterfaceClaimed(device, interfaceNumber, claimed) {
        var iface = ((device.configuration && device.configuration.interfaces) || [])
            .filter(function(i) { return i.interfaceNumber === interfaceNumber; })[0];
        if (iface) iface.claimed = claimed;
    }

    function OpenWebUSBDevice(info) {
        info = info || {};
        this.vendorId = info.vendorId;
        this.productId = info.productId;
        this.productName = info.productName || null;
        this.manufacturerName = info.manufacturerName || null;
        this.serialNumber = info.serialNumber || null;
        this.deviceClass = info.deviceClass || 0;
        this.deviceSubclass = info.deviceSubclass || 0;
        this.deviceProtocol = info.deviceProtocol || 0;
        this.usbVersionMajor = info.usbVersionMajor || 0;
        this.usbVersionMinor = info.usbVersionMinor || 0;
        this.usbVersionSubminor = info.usbVersionSubminor || 0;
        this.deviceVersionMajor = info.deviceVersionMajor || 0;
        this.deviceVersionMinor = info.deviceVersionMinor || 0;
        this.deviceVersionSubminor = info.deviceVersionSubminor || 0;
        this.configurations = deriveInterfaceState(info.configurations || []);
        // 実仕様のconfiguration getterは「bConfigurationValueが現在値と一致するもの」を
        // 都度探す形。Python側(dev.get_active_configuration())が実機の値を
        // activeConfigurationValueとして渡してくればそれを使い、取得できなかった
        // 場合(古いデバイス等)のみ従来どおり先頭要素へ安全側フォールバックする。
        var activeMatch = null;
        if (info.activeConfigurationValue !== undefined && info.activeConfigurationValue !== null) {
            activeMatch = (this.configurations || []).filter(function(c) {
                return c.configurationValue === info.activeConfigurationValue;
            })[0] || null;
        }
        this.configuration = activeMatch || (this.configurations && this.configurations[0]) || null;
        this.opened = false;
        this._handle = null;
    }
    OpenWebUSBDevice.prototype.open = function() {
        var self = this;
        return callBridge('openDevice', this.vendorId, this.productId).then(function(res) {
            if (!res.success) throwFromResult(res, 'Failed to open device');
            self._handle = res.handle;
            self.opened = true;
        });
    };
    OpenWebUSBDevice.prototype.close = function() {
        var self = this;
        return _bridgeReady.then(function(bridge) {
            if (bridge && self._handle != null) bridge.closeDevice(self._handle);
            self.opened = false;
        });
    };
    OpenWebUSBDevice.prototype.selectConfiguration = function(configurationValue) {
        var self = this;
        return callBridge('selectConfiguration', this._handle, configurationValue).then(function(res) {
            if (!res.success) throwFromResult(res, 'Failed to select configuration');
            // 実仕様どおり、選択成功後はconfigurationが新しい設定を指すよう更新する。
            var match = (self.configurations || []).filter(function(c) { return c.configurationValue === configurationValue; })[0];
            if (match) self.configuration = match;
        });
    };
    OpenWebUSBDevice.prototype.claimInterface = function(n) {
        var self = this;
        return callBridge('claimInterface', this._handle, n).then(function(res) {
            // 保護対象クラスによる拒否だけがSecurityError、それ以外(ハンドル不正・
            // libusb側のclaim失敗)はNetworkErrorが実仕様どおりの振り分け。
            if (!res.success) throwFromResult(res, 'Failed to claim interface');
            setInterfaceClaimed(self, n, true);
        });
    };
    OpenWebUSBDevice.prototype.releaseInterface = function(n) {
        var self = this;
        return callBridge('releaseInterface', this._handle, n).then(function(res) {
            if (!res.success) throwFromResult(res, 'Failed to release interface');
            setInterfaceClaimed(self, n, false);
        });
    };
    OpenWebUSBDevice.prototype.selectAlternateInterface = function(interfaceNumber, alternateSetting) {
        var self = this;
        return callBridge('selectAlternateInterface', this._handle, interfaceNumber, alternateSetting).then(function(res) {
            if (!res.success) throwFromResult(res, 'Failed to select alternate interface');
            var iface = ((self.configuration && self.configuration.interfaces) || [])
                .filter(function(i) { return i.interfaceNumber === interfaceNumber; })[0];
            if (iface) {
                var alt = (iface.alternates || []).filter(function(a) { return a.alternateSetting === alternateSetting; })[0];
                if (alt) iface.alternate = alt;
            }
        });
    };
    OpenWebUSBDevice.prototype.reset = function() {
        return callBridge('resetDevice', this._handle).then(function(res) {
            if (!res.success) throwFromResult(res, 'Failed to reset device');
        });
    };
    OpenWebUSBDevice.prototype.clearHalt = function(direction, endpointNumber) {
        return callBridge('clearHalt', this._handle, direction, endpointNumber).then(function(res) {
            if (!res.success) throwFromResult(res, 'Failed to clear halt');
        });
    };
    OpenWebUSBDevice.prototype.forget = function() {
        var self = this;
        return callBridge('forgetGrantedDevice', this.vendorId, this.productId).then(function() {
            self.opened = false;
        });
    };
    OpenWebUSBDevice.prototype.transferIn = function(endpoint, length) {
        return callBridge('bulkTransferIn', this._handle, endpoint, length).then(function(res) {
            if (!res.success) throwFromResult(res, 'Transfer failed');
            // 🛡️ 実仕様(USBTransferStatus): STALLはrejectではなくstatus:'stall'を
            //    伴う成功resolveとして返る。Python側がstall検出時はres.statusに
            //    'stall'を入れてくる(それ以外はres.status==='ok')。
            var bytes = hexToUint8(res.data || '');
            return { status: res.status || 'ok', data: new DataView(bytes.buffer) };
        });
    };
    OpenWebUSBDevice.prototype.transferOut = function(endpoint, data) {
        var hex = bytesToHex(data);
        return callBridge('bulkTransferOut', this._handle, endpoint, hex).then(function(res) {
            if (!res.success) throwFromResult(res, 'Transfer failed');
            return { status: res.status || 'ok', bytesWritten: res.bytesWritten };
        });
    };
    OpenWebUSBDevice.prototype.isochronousTransferIn = function() {
        return Promise.reject(new DOMException('isochronous transfers are not supported by this WebUSB bridge', 'NotSupportedError'));
    };
    OpenWebUSBDevice.prototype.isochronousTransferOut = function() {
        return Promise.reject(new DOMException('isochronous transfers are not supported by this WebUSB bridge', 'NotSupportedError'));
    };
    OpenWebUSBDevice.prototype.controlTransferIn = function(setup, length) {
        var reqType = (setup.requestType === 'standard' ? 0x00 : setup.requestType === 'class' ? 0x20 : 0x40) |
                      (setup.recipient === 'interface' ? 0x01 : setup.recipient === 'endpoint' ? 0x02 : setup.recipient === 'other' ? 0x03 : 0x00) |
                      0x80; // Device-to-host
        return callBridge('controlTransferIn', this._handle, reqType, setup.request, setup.value, setup.index, length).then(function(res) {
            if (!res.success) throwFromResult(res, 'Control transfer failed');
            var bytes = hexToUint8(res.data || '');
            return { status: res.status || 'ok', data: new DataView(bytes.buffer) };
        });
    };
    OpenWebUSBDevice.prototype.controlTransferOut = function(setup, data) {
        var reqType = (setup.requestType === 'standard' ? 0x00 : setup.requestType === 'class' ? 0x20 : 0x40) |
                      (setup.recipient === 'interface' ? 0x01 : setup.recipient === 'endpoint' ? 0x02 : setup.recipient === 'other' ? 0x03 : 0x00);
        var hex = data ? bytesToHex(data) : '';
        return callBridge('controlTransferOut', this._handle, reqType, setup.request, setup.value, setup.index, hex).then(function(res) {
            if (!res.success) throwFromResult(res, 'Control transfer failed');
            return { status: res.status || 'ok', bytesWritten: res.bytesWritten };
        });
    };

    // --- connect/disconnect イベント ---
    // Python側(PyUsbBridge)がホットプラグ監視タイマーで差分検出し、許可済み
    // オリジンに関係するデバイスの抜き挿しだけをQtシグナルとして送ってくる。
    // ここではEventTargetを継承する代わりに、addEventListener/removeEventListener/
    // on(connect|disconnect)プロパティの両方に対応した最小限のディスパッチャを実装する。
    var _listeners = { connect: [], disconnect: [] };
    function _dispatchUsbEvent(type, device) {
        var evt = { type: type, device: device };
        (_listeners[type] || []).forEach(function(fn) {
            try { fn(evt); } catch (e) { /* リスナー内の例外はここで握りつぶす(1つの失敗で他を止めない) */ }
        });
        var handlerProp = 'on' + type;
        if (typeof navigator.usb[handlerProp] === 'function') {
            try { navigator.usb[handlerProp](evt); } catch (e) { /* 同上 */ }
        }
    }
    _bridgeReady.then(function(bridge) {
        if (!bridge) return;
        if (bridge.deviceConnected && bridge.deviceConnected.connect) {
            bridge.deviceConnected.connect(function(infoJson) {
                try { _dispatchUsbEvent('connect', new OpenWebUSBDevice(JSON.parse(infoJson))); } catch (e) {}
            });
        }
        if (bridge.deviceDisconnected && bridge.deviceDisconnected.connect) {
            bridge.deviceDisconnected.connect(function(infoJson) {
                try { _dispatchUsbEvent('disconnect', new OpenWebUSBDevice(JSON.parse(infoJson))); } catch (e) {}
            });
        }
    });

    navigator.usb = {
        onconnect: null,
        ondisconnect: null,
        getDevices: function() {
            return callBridge('listDevices').then(function(res) {
                return (res.devices || []).map(function(d) { return new OpenWebUSBDevice(d); });
            });
        },
        requestDevice: function(_options) {
            // 🛡️ 実仕様: USBDeviceRequestOptions.filtersは必須(required)フィールド。
            //    省略された場合、実ブラウザではWebIDLの辞書変換の時点でTypeErrorになる
            //    (wicg.github.io/webusb の USBDeviceRequestOptions定義)。旧実装は
            //    optionsを一切見ておらず、常に全デバイスをチューザーに表示していた。
            if (!_options || !Array.isArray(_options.filters)) {
                return Promise.reject(new TypeError(
                    "Failed to execute 'requestDevice' on 'USB': required member filters is undefined."));
            }
            var exclusionFilters = Array.isArray(_options.exclusionFilters) ? _options.exclusionFilters : [];
            // 🛡️ 実仕様: filters/exclusionFiltersの各要素が「A USBDeviceFilter filter
            //    is valid」に反する場合はTypeErrorで拒否する(例: vendorId無しで
            //    productIdだけを指定 等)。
            var allFilters = _options.filters.concat(exclusionFilters);
            for (var fi = 0; fi < allFilters.length; fi++) {
                if (!isValidUsbDeviceFilter(allFilters[fi])) {
                    return Promise.reject(new TypeError(
                        "Failed to execute 'requestDevice' on 'USB': the provided filter value is invalid."));
                }
            }
            // 🛡️ 本物のWebUSB同様、信頼できるユーザー操作(クリック等)のハンドラ内から
            //    呼ばれた場合のみ受け付ける。navigator.userActivationが無い古い/簡易な
            //    エンジンでは判定できないため、その場合はチェックをスキップする
            //    (その場合でも実際のデバイス選択にはネイティブのチューザーダイアログでの
            //    明示的なユーザー操作が別途必要であり、無許可アクセスには繋がらない)。
            if (typeof navigator.userActivation !== 'undefined' && navigator.userActivation &&
                navigator.userActivation.isActive === false) {
                return Promise.reject(new DOMException(
                    'Must be handling a user gesture to call navigator.usb.requestDevice().', 'SecurityError'));
            }
            return callBridge('requestDeviceChooser', JSON.stringify({
                filters: _options.filters,
                exclusionFilters: exclusionFilters,
            })).then(function(res) {
                if (res.cancelled || !res.device) {
                    throw new DOMException('No device selected.', 'NotFoundError');
                }
                return new OpenWebUSBDevice(res.device);
            });
        },
        addEventListener: function(type, fn) {
            if (_listeners[type] && typeof fn === 'function') _listeners[type].push(fn);
        },
        removeEventListener: function(type, fn) {
            if (_listeners[type]) _listeners[type] = _listeners[type].filter(function(f) { return f !== fn; });
        },
    };
})();
"""
