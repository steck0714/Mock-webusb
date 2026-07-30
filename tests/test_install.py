# -*- coding: utf-8 -*-
"""install()(WEBUSB_POLYFILL_JS/qwebchannel.jsの注入設定)のテスト。

🛡️ 重要: 以前はここのテストが1件も無かった。install()自体はGUIを持たない
QObjectサブクラスでも(page.url()/setWebChannel()/scripts()さえ提供すれば)
軽量に検証できるにもかかわらず、この抜けのせいで
`script.setRunsOnSubFrames(True)` という、クロスオリジンiframeへ
navigator.usbを漏らしてしまう重大な設定ミスが長らく発見されずに残っていた
(詳細は CHANGELOG.md / bridge.py の WebUSBBridge._current_origin() の
docstring、および polyfill.py の install() 内コメントを参照)。

実際のQWebEngineScript/QWebChannelオブジェクトを使い、QWebEnginePageそのものは
使わず(GUI/レンダラを起動せず軽量に済ませるため)、install()が要求する
最小限のインターフェース(url()/setWebChannel()/scripts())だけを持つ
フェイクページに対してinstall()を実行する。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QObject, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pyside6_webusb.polyfill import install  # noqa: E402


class FakeScripts:
    """QWebEngineScriptCollection の代わり。insert()された本物の
    QWebEngineScript を後から検査できるようにリストへ集める。"""

    def __init__(self):
        self.inserted = []

    def insert(self, script):
        self.inserted.append(script)


class FakePage(QObject):
    """install()が要求する最小限のインターフェース(url()/setWebChannel()/
    scripts())だけを持つ、QWebEnginePageの軽量な代役。QWebChannel(page)は
    pageが本物のQObjectであることを要求するため、QObjectを継承している。"""

    def __init__(self, url="https://example.test/"):
        super().__init__()
        self._url = QUrl(url)
        self._scripts = FakeScripts()
        self.web_channel = None

    def url(self):
        return self._url

    def setWebChannel(self, channel):
        self.web_channel = channel

    def scripts(self):
        return self._scripts


def _make_app():
    return QApplication.instance() or QApplication([])


def test_install_returns_a_bridge_and_registers_the_web_channel():
    _make_app()
    page = FakePage()
    bridge = install(page, settings_organization="pyside6-webusb-tests", settings_application="test_install")
    assert bridge is not None
    assert page.web_channel is not None
    assert page.web_channel.registeredObjects().get("pyUsbBridge") is bridge
    print("test_install_returns_a_bridge_and_registers_the_web_channel: OK")


def test_install_injects_exactly_two_scripts_with_correct_injection_point_and_world():
    _make_app()
    page = FakePage()
    install(page, settings_organization="pyside6-webusb-tests", settings_application="test_install")
    names = sorted(s.name() for s in page._scripts.inserted)
    assert names == ["PySide6WebUSBPolyfill", "PySide6WebUSBQWebChannelLib"]
    for s in page._scripts.inserted:
        from PySide6.QtWebEngineCore import QWebEngineScript
        assert s.injectionPoint() == QWebEngineScript.InjectionPoint.DocumentCreation
        assert s.worldId() == QWebEngineScript.ScriptWorldId.MainWorld
        assert s.sourceCode(), f"{s.name()} のソースコードが空になっている"
    print("test_install_injects_exactly_two_scripts_with_correct_injection_point_and_world: OK")


def test_install_does_not_run_scripts_on_subframes():
    """🚨 これが今回のセキュリティ修正そのものの回帰テスト。
    WebUSBBridge._current_origin() はQWebEnginePage.url()(常にトップレベル
    フレームのURL)しか見ておらず、QWebChannel越しの呼び出しがどのフレーム
    (iframe)から来たかを区別する手段が無い。scriptがiframe内でも実行される
    (runsOnSubFrames=True)と、クロスオリジンiframeがトップレベルページに
    成りすまして、そのページが許可済みのUSBデバイスへ完全にアクセスできて
    しまう。安全側に倒し、メインフレームでしか navigator.usb を定義しない
    (runsOnSubFrames=False)ことを確認する。"""
    _make_app()
    page = FakePage()
    install(page, settings_organization="pyside6-webusb-tests", settings_application="test_install")
    assert len(page._scripts.inserted) == 2
    for s in page._scripts.inserted:
        assert s.runsOnSubFrames() is False, (
            f"{s.name()} が runsOnSubFrames=True のままだと、クロスオリジンiframeへ"
            "navigator.usbが漏れ、トップレベルページの許可済みUSBデバイスへ"
            "iframeが成りすましてアクセスできてしまう"
        )
    print("test_install_does_not_run_scripts_on_subframes: OK")


def test_install_is_scoped_to_the_pages_own_origin_not_a_shared_default():
    """install()を異なるオリジンの2つのFakePageに対して呼び、生成される
    WebUSBBridgeがそれぞれ自分のページのURLをオリジン判定に使うこと
    (=別ページ間で許可状態が混ざらないこと)を確認する。"""
    _make_app()
    page_a = FakePage(url="https://a.example/")
    page_b = FakePage(url="https://b.example/")
    bridge_a = install(page_a, settings_organization="pyside6-webusb-tests", settings_application="test_install")
    bridge_b = install(page_b, settings_organization="pyside6-webusb-tests", settings_application="test_install")
    assert bridge_a._current_origin() == "https://a.example"
    assert bridge_b._current_origin() == "https://b.example"
    print("test_install_is_scoped_to_the_pages_own_origin_not_a_shared_default: OK")


if __name__ == "__main__":
    test_install_returns_a_bridge_and_registers_the_web_channel()
    test_install_injects_exactly_two_scripts_with_correct_injection_point_and_world()
    test_install_does_not_run_scripts_on_subframes()
    test_install_is_scoped_to_the_pages_own_origin_not_a_shared_default()
    print("ALL INSTALL TESTS PASSED")
