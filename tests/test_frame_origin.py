# -*- coding: utf-8 -*-
"""frame_origin.py(フレーム単位オリジン特定)のテスト。

背景: CHANGELOG.md の 0.0.2b0 / 0.0.3 / 0.0.3a0 / 0.0.3b を参照。このテストファイルは
2つの層に分かれている:
  1. フェイク(FakeFrame/FakePage)を使った高速な単体テスト -- FrameOriginTracker
     自体のロジック(トークン発行・解決・上限・is_functional判定)を、実際の
     QWebEnginePageを起動せずに検証する。
  2. 本物のQWebEnginePage(--no-sandboxで起動)を使った統合テスト1本 -- 実際に
     iframeを含むページを読み込ませ、install()で配線したトラッカーが正しく
     iframeへ個別のトークンを配り、そのトークンが正しいオリジンへ解決される
     ことを確認する(実機検証で判明した以下の点を自動テストとして固定化した:
       - QWebEngineFrame.runJavaScript()はコード文字列だけでなくコールバックを
         含めて最低2引数必要(1引数だと'not enough arguments'になる)
       - QWebEnginePage.setHtml(html, baseUrl=...)で読み込んだメインフレームは
         QWebEngineFrame.url()がbaseUrlではなくdata:...URLになり、意図せず
         オリジン無し扱いになる(そのため統合テストはpage.load(QUrl(...))を使う)
       - file://等ホスト部を持たないURLのフレームには意図的にトークンを配らない
         (不透明オリジンとして安全側に倒す))。
     本物のQWebEnginePageの起動はChromiumの初期化を伴うため数秒〜十数秒かかる。
     このテストファイルではこの種のテストを1本だけに絞ってある。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
# 🛡️ 本物のQWebEnginePageをroot権限のコンテナ内で起動するには、Chromiumの
#    サンドボックスを明示的に無効化する必要がある(そうしないとプロセスごと
#    落ちる)。この環境変数はQWebEngineCoreの初回import前に設定されている
#    必要があるため、ファイル冒頭で設定する。
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

from PySide6.QtCore import QObject, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pyside6_webusb.frame_origin import FrameOriginTracker, url_to_origin  # noqa: E402


def _make_app():
    return QApplication.instance() or QApplication([])


# ============================================================
# 1) フェイクを使った高速な単体テスト
# ============================================================

class FakeFrame(QObject):
    def __init__(self, url, children=None, name=""):
        super().__init__()
        self._url = QUrl(url)
        self._children = children or []
        self._name = name
        self.injected_scripts = []  # runJavaScript()に渡されたコードを記録

    def url(self):
        return self._url

    def children(self):
        return self._children

    def name(self):
        return self._name

    def runJavaScript(self, code, callback=None):
        self.injected_scripts.append(code)
        if callback is not None:
            callback(None)


class FakePageWithNavigation(QObject):
    """navigationRequestedシグナルを(簡易的に)持つフェイクページ。
    実際にはQt Signalではなく、テストから直接_on_navigation_requestedを
    呼び出す代わりに使う軽量な代役(PySide6でカスタムSignalを追加するのは
    冗長なので、connect()を素朴に模倣するだけにしてある)。"""

    class _FakeSignal:
        def __init__(self):
            self._slot = None

        def connect(self, slot):
            self._slot = slot

        def emit(self, *args):
            if self._slot is not None:
                self._slot(*args)

    def __init__(self, main_frame):
        super().__init__()
        self._main_frame = main_frame
        self.navigationRequested = self._FakeSignal()

    def mainFrame(self):
        return self._main_frame


class FakePageWithoutNavigation(QObject):
    """navigationRequestedを持たない(=古いPySide6/Qtを模した)フェイクページ。"""

    def __init__(self, main_frame):
        super().__init__()
        self._main_frame = main_frame

    def mainFrame(self):
        return self._main_frame


def test_url_to_origin_normalizes_scheme_host_and_default_port():
    assert url_to_origin(QUrl("https://Example.COM:443/path")) == "https://example.com"
    assert url_to_origin(QUrl("http://example.com:80/path")) == "http://example.com"
    assert url_to_origin(QUrl("https://example.com:8443/path")) == "https://example.com:8443"
    print("test_url_to_origin_normalizes_scheme_host_and_default_port: OK")


def test_url_to_origin_returns_none_for_opaque_urls():
    """ホスト部を持たないURL(file://、data:等)は「オリジン不明」として
    Noneを返すべき(=そのフレームにはトークンを配らない安全側の判断)。"""
    assert url_to_origin(QUrl("file:///tmp/x.html")) is None
    assert url_to_origin(QUrl("data:text/html,hello")) is None
    assert url_to_origin(QUrl("")) is None
    assert url_to_origin(None) is None
    print("test_url_to_origin_returns_none_for_opaque_urls: OK")


def test_tracker_assigns_token_to_frame_and_resolves_it():
    _make_app()
    main = FakeFrame("https://top.example/")
    page = FakePageWithNavigation(main)
    tracker = FrameOriginTracker(page)
    tracker.wire()  # navigationRequestedへの接続 + 初回rescan()

    assert tracker.is_functional is True
    assert len(main.injected_scripts) == 1
    assert "window.__pyUsbFrameToken" in main.injected_scripts[0]

    # 実際に配ったトークンで解決できることを確認(発行したトークン文字列自体は
    # runJavaScript呼び出しのコード文字列から抜き出す)。
    import re
    m = re.search(r'"([^"]+)"', main.injected_scripts[0])
    assert m, "トークンがJSON文字列として埋め込まれているはず"
    token = m.group(1)
    assert tracker.origin_for_token(token) == "https://top.example"
    print("test_tracker_assigns_token_to_frame_and_resolves_it: OK")


def test_tracker_walks_nested_children_and_skips_opaque_frames():
    _make_app()
    grandchild = FakeFrame("https://deep.example/")
    child_with_origin = FakeFrame("https://child.example/", children=[grandchild])
    child_opaque = FakeFrame("file:///tmp/no-origin.html")  # トークンを配られないはず
    main = FakeFrame("https://top.example/", children=[child_with_origin, child_opaque])
    page = FakePageWithNavigation(main)
    tracker = FrameOriginTracker(page)
    tracker.wire()

    origins = set(tracker._token_to_origin.values())
    assert origins == {"https://top.example", "https://child.example", "https://deep.example"}
    assert len(child_opaque.injected_scripts) == 0, "オリジン不明なフレームにはrunJavaScriptしないはず"
    print("test_tracker_walks_nested_children_and_skips_opaque_frames: OK")


def test_tracker_is_not_functional_without_navigation_requested_signal():
    """navigationRequestedを持たない(=古いPySide6/Qtを模した)pageに対しては、
    is_functionalがFalseになるべき(install()側はこれを見て安全側に倒す)。"""
    _make_app()
    main = FakeFrame("https://top.example/")
    page = FakePageWithoutNavigation(main)
    tracker = FrameOriginTracker(page)
    tracker.wire()
    assert tracker.is_functional is False
    print("test_tracker_is_not_functional_without_navigation_requested_signal: OK")


def test_origin_for_token_rejects_empty_and_unknown_tokens():
    """空文字列・未発行のトークンはNoneに解決されるべき(=呼び出し元は拒否する)。
    これが「敵対的なサブフレームが空/でたらめなトークンでトップレベルページに
    成りすませない」というセキュリティ特性の核心部分。"""
    _make_app()
    main = FakeFrame("https://top.example/")
    page = FakePageWithNavigation(main)
    tracker = FrameOriginTracker(page)
    tracker.wire()
    assert tracker.origin_for_token("") is None
    assert tracker.origin_for_token(None) is None
    assert tracker.origin_for_token("totally-forged-token") is None
    print("test_origin_for_token_rejects_empty_and_unknown_tokens: OK")


def test_tracker_caps_total_token_count():
    """トークン総数が無制限に増え続けないよう、上限を超えたら最も古いものから
    追い出されることを確認する。"""
    _make_app()
    main = FakeFrame("https://top.example/")
    page = FakePageWithNavigation(main)
    tracker = FrameOriginTracker(page)
    tracker._MAX_TOTAL_TOKENS = 3  # テストのため小さい上限に差し替える
    tracker.wire()
    for _ in range(5):
        tracker.rescan()
    assert len(tracker._token_to_origin) <= 3
    print("test_tracker_caps_total_token_count: OK")


def test_rescan_survives_exceptions_from_a_misbehaving_frame():
    """1つのフレームでurl()やrunJavaScript()が例外を投げても、走査全体が
    落ちずに他のフレームの処理を続けられることを確認する。"""
    _make_app()

    class ExplodingFrame(FakeFrame):
        def url(self):
            raise RuntimeError("boom")

    ok_frame = FakeFrame("https://ok.example/")
    main = FakeFrame("https://top.example/", children=[ExplodingFrame("https://x/"), ok_frame])
    page = FakePageWithNavigation(main)
    tracker = FrameOriginTracker(page)
    tracker.wire()  # 例外で落ちないこと自体がこのテストの主眼
    origins = set(tracker._token_to_origin.values())
    assert "https://top.example" in origins
    assert "https://ok.example" in origins
    print("test_rescan_survives_exceptions_from_a_misbehaving_frame: OK")


# ============================================================
# 2) 本物のQWebEnginePageを使った統合テスト(1本のみ)
# ============================================================

def test_real_qwebenginepage_attributes_iframe_to_its_own_origin():
    """実機検証(このテストが自動化する前に手作業で確認した内容):
    本物のQWebEnginePageへinstall()し、メインフレームとは別オリジンのiframeを
    含むページを読み込ませると、iframeには専用のトークンが配られ、
    bridge._current_origin(token)がiframe自身の本当のオリジンに正しく解決される。
    また、空トークンでは何にも解決されない(=トップレベルページへ成りすませない)
    ことも確認する。"""
    import tempfile
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWebEngineCore import QWebEnginePage
    from pyside6_webusb.polyfill import install

    _make_app()
    html = (
        "<html><body><h1>main</h1>"
        '<iframe src="https://sub.example.org/child.html"></iframe>'
        "</body></html>"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(html)
        html_path = f.name

    try:
        page = QWebEnginePage()
        bridge = install(
            page,
            settings_organization="pyside6-webusb-tests",
            settings_application="test_frame_origin_integration",
        )
        assert bridge is not None
        assert bridge._frame_tracker is not None
        assert bridge._frame_tracker.is_functional is True

        loop = QEventLoop()

        def on_finished(_ok):
            QTimer.singleShot(1500, loop.quit)  # トークン配布(複数回の再走査)が完了するのを待つ

        page.loadFinished.connect(on_finished)
        page.load(QUrl.fromLocalFile(html_path))
        QTimer.singleShot(20000, loop.quit)  # 保険のタイムアウト
        loop.exec()

        tracker = bridge._frame_tracker
        origins_found = set(tracker._token_to_origin.values())
        assert "https://sub.example.org" in origins_found, (
            f"iframeに専用トークンが配られているはず(見つかったオリジン: {origins_found})"
        )
        # file://のメインフレームはホスト部を持たない(=不透明オリジン)ため
        # トークンが配られないのが正しい挙動。
        assert not any(o.startswith("file://") for o in origins_found)

        iframe_token = next(t for t, o in tracker._token_to_origin.items() if o == "https://sub.example.org")
        assert bridge._current_origin(iframe_token) == "https://sub.example.org"
        # 空トークンでは何にも解決されない(成りすまし防止の核心)。
        assert bridge._current_origin("") is None
        assert bridge._current_origin("forged-token") is None
        print("test_real_qwebenginepage_attributes_iframe_to_its_own_origin: OK")
    finally:
        os.unlink(html_path)


if __name__ == "__main__":
    test_url_to_origin_normalizes_scheme_host_and_default_port()
    test_url_to_origin_returns_none_for_opaque_urls()
    test_tracker_assigns_token_to_frame_and_resolves_it()
    test_tracker_walks_nested_children_and_skips_opaque_frames()
    test_tracker_is_not_functional_without_navigation_requested_signal()
    test_origin_for_token_rejects_empty_and_unknown_tokens()
    test_tracker_caps_total_token_count()
    test_rescan_survives_exceptions_from_a_misbehaving_frame()
    test_real_qwebenginepage_attributes_iframe_to_its_own_origin()
    print("ALL FRAME_ORIGIN TESTS PASSED")
