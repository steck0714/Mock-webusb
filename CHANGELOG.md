# Changelog

All notable changes to this project are documented here.

## [0.0.0] — initial extraction

Initial standalone extraction of the WebUSB implementation from the `openweb` browser
project, generalized for use in any PySide6/QtWebEngine app.

### Core functionality
- `WebUSBBridge`: QWebChannel bridge backed by pyusb/libusb — full `navigator.usb` surface
  (`getDevices`, `requestDevice`, all `USBDevice` methods, bulk/control transfers with
  spec-accurate STALL handling, hotplug `connect`/`disconnect` events).
- `WEBUSB_POLYFILL_JS`: the JavaScript polyfill implementing `navigator.usb` against that
  bridge, including `options.filters`/`exclusionFilters` matching per the WICG spec
  algorithm (vendor/product ID, serial number, and the composite-device
  matches-via-any-interface classCode rule).
- `WebUsbDeviceChooserDialog`: native device picker, referenced against Chrome's actual
  chooser UX — always names the requesting origin, requires an explicit device selection
  before "Connect" is enabled (no auto-selecting the first row), and live-updates the list
  if a device is plugged in while the dialog is still open.
- Security hardening: the 7 WebUSB protected interface classes, a Chromium-blocklist-derived
  known-security-key blocklist, per-origin permission storage, and origin binding read
  independently from the page URL (never trusted from JS).
- `install(page)`: one-call integration — wires the bridge, loads `qwebchannel.js` from your
  Qt installation's built-in resources, and injects the polyfill script.

### Fixed before first use
An external code review (cross-checked against the actual source rather than taken at face
value) surfaced one real, confirmed bug and several other findings:
- **Fixed**: `bridge.py` was missing `import time` despite using `time.time()` in `_grant()`
  and `_record_device_usage()` — both call sites were wrapped in `try/except`, so the
  `NameError` was silently swallowed rather than crashing; the practical effect was that
  granted permissions and device-usage records silently failed to persist. Added a
  regression test that exercises these methods *without* mocking them (and verified the
  test actually catches the bug by reintroducing it and confirming the failure, before
  re-fixing it).
- **Fixed**: `revoke_origin_grant()`/`revoke_all_for_origin()` had no exception handling
  (inconsistent with the rest of the class, which never lets an exception escape).
- **Fixed**: error strings returned to JS could contain raw newlines/tabs from underlying
  pyusb/libusb exceptions; added `safe_error_str()` and applied it at all 19 call sites.
- **Hardened**: `_on_page_navigated()` now explicitly clears every open handle when the
  current origin can't be determined at all, rather than relying solely on a per-handle
  inequality check.
- **Fixed**: `@Slot(str, result=str)` was misattached to the private helper
  `_enumerate_filtered_devices()` instead of `requestDeviceChooser()` — the actual
  `navigator.usb.requestDevice()` implementation that `WEBUSB_POLYFILL_JS` calls via
  `callBridge('requestDeviceChooser', JSON.stringify(...))`. Confirmed against a real
  `staticMetaObject` (not just static reading) that `requestDeviceChooser` was completely
  absent from the registered Qt slots — QWebChannel could never have exposed it to JS — while
  the helper (whose real parameters are `usb_core`/`usb_util` module handles and
  `filters`/`exclusion_filters` lists, nothing resembling a `QString`) was registered instead.
  All existing tests call `requestDeviceChooser()` as a plain Python method, so none of them
  could catch this class of bug. Added `test_requestDeviceChooser_is_registered_as_qt_slot`,
  which asserts slot registration directly via `staticMetaObject`/`QMetaMethod`, and confirmed
  it fails against the broken version before re-fixing.
- Considered and deliberately **not** implemented: a lock around `_open_devices` (Qt's
  single-threaded event loop model means the claimed race condition doesn't apply here, and
  a mis-applied lock would be a worse bug than none) and USB handle-ID recycling (recycling
  IDs risks a *different*, worse bug — a stale JS reference resolving to the wrong device).
