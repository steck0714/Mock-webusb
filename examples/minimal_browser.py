# -*- coding: utf-8 -*-
"""
examples/minimal_browser.py
============================
A ~60-line runnable demo showing the entire integration surface of pyside6-webusb:

    view = QWebEngineView()
    install(view.page())

That's the whole API. Everything else below is just enough browser chrome (address bar,
back/forward) to make it a usable window, plus a small self-contained HTML/JS test page
(DEMO_HTML) so you can try navigator.usb.getDevices() / requestDevice() immediately
without depending on any particular external website.

Run it with:
    python examples/minimal_browser.py

then click "List paired devices" (should be empty on first run) or "Request a device"
(shows the native chooser dialog for whatever's plugged in over USB).
"""
import sys

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication, QLineEdit, QMainWindow, QToolBar
from PySide6.QtWebEngineWidgets import QWebEngineView

from pyside6_webusb import install

DEMO_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>pyside6-webusb demo</title>
<style>
  body { font-family: system-ui, sans-serif; background:#0f172a; color:#e2e8f0; padding: 2rem; }
  button { font-size: 14px; padding: 8px 14px; margin-right: 8px; border-radius: 6px; border: none;
           background:#89b4fa; color:#1e1e2e; cursor: pointer; }
  pre { background:#1e1e2e; padding: 1rem; border-radius: 8px; white-space: pre-wrap; }
</style></head>
<body>
  <h2>pyside6-webusb demo page</h2>
  <p>This page is served from a local data: URL (no external site needed) and talks
     directly to <code>navigator.usb</code>, which this window's page has just had
     the pyside6-webusb polyfill installed on.</p>
  <button id="list">List paired devices</button>
  <button id="request">Request a device&hellip;</button>
  <pre id="out">(nothing yet)</pre>
  <script>
    const out = document.getElementById('out');
    const show = (v) => { out.textContent = JSON.stringify(v, null, 2); };
    document.getElementById('list').onclick = async () => {
      try {
        const devices = await navigator.usb.getDevices();
        show(devices.map(d => ({vendorId: d.vendorId, productId: d.productId,
                                 productName: d.productName, serialNumber: d.serialNumber})));
      } catch (e) { show('Error: ' + e.name + ': ' + e.message); }
    };
    document.getElementById('request').onclick = async () => {
      try {
        // filters: [{}] (an empty filter object) matches every connected device, per spec.
        const device = await navigator.usb.requestDevice({ filters: [{}] });
        show({picked: device.productName || '(unnamed)', vendorId: device.vendorId, productId: device.productId});
      } catch (e) { show('Error: ' + e.name + ': ' + e.message); }
    };
  </script>
</body></html>
"""


class DemoWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("pyside6-webusb minimal demo")
        self.resize(900, 650)

        self.view = QWebEngineView()
        self.setCentralWidget(self.view)

        # This is the entire integration: one function call.
        install(self.view.page())

        toolbar = QToolBar()
        self.addToolBar(toolbar)
        self.address_bar = QLineEdit()
        self.address_bar.returnPressed.connect(self._navigate)
        toolbar.addWidget(self.address_bar)

        self.view.urlChanged.connect(lambda u: self.address_bar.setText(u.toString()))
        self.view.setHtml(DEMO_HTML, baseUrl=QUrl("https://pyside6-webusb.example/"))
        # ^ a fake https:// baseUrl gives the page a stable, secure-context origin (matching
        # how a real deployed site would look) instead of the special `data:` origin, so the
        # per-origin permission storage in WebUSBBridge behaves like it would in production.

    def _navigate(self):
        text = self.address_bar.text().strip()
        if text and "://" not in text:
            text = "https://" + text
        if text:
            self.view.setUrl(QUrl(text))


def main():
    app = QApplication(sys.argv)
    win = DemoWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
