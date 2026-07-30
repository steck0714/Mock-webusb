# -*- coding: utf-8 -*-
"""
pyside6-webusb
==============
A WebUSB API (https://wicg.github.io/webusb/) implementation for PySide6 / QtWebEngine
apps. QtWebEngine (the Chromium build PySide6 ships) does not implement WebUSB, so this
package reproduces it: a QWebChannel bridge backed by pyusb/libusb on the Python side, and
a JavaScript polyfill that makes `navigator.usb` behave like the real thing on the page
side.

Quick start
-----------
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from pyside6_webusb import install

    view = QWebEngineView()
    install(view.page())          # that's it -- navigator.usb now works on this page
    view.load("https://your-site.example")

See README.md for the security model, spec-compliance notes, and a full example app
(examples/minimal_browser.py).
"""

from .bridge import WebUSBBridge
from .chooser_dialog import WebUsbDeviceChooserDialog
from .polyfill import WEBUSB_POLYFILL_JS, install

__all__ = [
    "install",
    "WebUSBBridge",
    "WebUsbDeviceChooserDialog",
    "WEBUSB_POLYFILL_JS",
]

__version__ = "0.0.3"
