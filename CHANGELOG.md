# Changelog

All notable changes to this project are documented here.

## [0.0.2]

Continued the spec/Chrome-source comparison from `0.0.1a0`, this time pulling actual Chromium
source (Blink's `usb_device.cc`, fetched live from `github.com/chromium/chromium`) rather than
spec prose alone, plus a first attempt at isochronous transfer support.

### Fixed
- **`bulkTransferIn`/`bulkTransferOut`/`clearHalt` had no equivalent of Chrome's
  `USBDevice::EnsureEndpointAvailable()`.** Fetched and read Blink's actual
  `third_party/blink/renderer/modules/webusb/usb_device.cc`: `transferIn`/`transferOut`/
  `clearHalt` all unconditionally call this before touching the device, and it requires the
  target endpoint to belong to a **claimed** interface. This implementation had that check for
  `controlTransferIn`/`controlTransferOut` (added in `0.0.1a0`) but never extended it to plain
  bulk/interrupt transfers or `clearHalt` — so a page could skip `claimInterface()` (and the
  protected-class rejection that comes with it) entirely and still read/write a protected
  interface's bulk or interrupt endpoints directly. Added `_endpoint_available_or_error()`,
  shared by all three methods, plus the endpoint-number range check (`1`-`15`, matching
  Chrome's `IndexSizeError` for out-of-range numbers). Added
  `test_bulk_transfer_and_clearHalt_require_claimed_interface` and
  `test_bulk_transfer_rejects_out_of_range_endpoint_number`; confirmed both fail against a
  copy with the checks removed.
- **`open()` (JS) had no idempotency check.** Real Chrome's `USBDevice::open()` resolves
  immediately without doing anything if the device is already open. This implementation always
  called through to `openDevice()` regardless, which — since `openDevice()` mints a brand new
  handle every call — meant calling `open()` twice silently orphaned the first handle (and
  anything claimed on it) with no way to ever close it again, since `this._handle` gets
  overwritten by the second call. Added an early return when `this.opened` is already true.
  Extended the JS test's exact-call-sequence assertion (now also tracking `openDevice` calls,
  which the fake bridge previously didn't record) to confirm a second `open()` adds no new
  bridge call.
- **`selectAlternateInterface()` had no equivalent of Chrome's `EnsureInterfaceClaimed()`.**
  Confirmed from the same Blink source: it requires the target interface to already be
  claimed, rejecting with `InvalidStateError` otherwise. This implementation had no such
  check at all — a page could change a never-claimed (including protected-class) interface's
  alternate setting without ever calling `claimInterface()`. Added the check, matching
  Chrome's exact error message. Added `test_selectAlternateInterface_requires_claimed_interface`;
  confirmed it fails against a copy with the check removed.
- **`claimInterface()`/`releaseInterface()` had no equivalent of Chrome's
  `EnsureDeviceConfigured()`.** Also confirmed from Blink's source: both require a
  configuration to already be selected, before anything else. Without an explicit check, a
  device with no active configuration was *still* rejected by this implementation (since
  `interface_class_for()` already falls back to "protected" when it can't determine an active
  configuration — see the `0.0.1a0` entry above) but with a misleading `SecurityError:
  ...protected interface class` message instead of the real reason. Added an explicit check at
  the top of both methods with Chrome's exact wording
  (`InvalidStateError: "the device must have a configuration selected"`). Added
  `test_claimInterface_and_releaseInterface_require_configuration_selected`; confirmed it
  fails (with the old, misleading `SecurityError` message) against a copy with the check
  removed.

### Added
- **Isochronous transfer support (`isochronousTransferIn`/`isochronousTransferOut`), best
  effort.** These previously always returned `NotSupportedError`. `pyusb`'s public API
  (`usb.core.Device`) has no isochronous method — `read()`/`write()` are documented as
  bulk/interrupt only — but its `libusb1` backend does expose `iso_read()`/`iso_write()`
  (confirmed by inspecting the installed `pyusb` package directly). Reaching them requires
  `dev.backend` and the private `dev._ctx.handle`, which is a real departure from the
  public-API-only approach the rest of this codebase follows, and is the reason this is
  labeled best-effort rather than held to the same confidence bar as the rest of `0.0.1a0`.
  Two known, deliberate limitations:
    - `pyusb`'s `iso_read`/`iso_write` split one buffer into **uniform**-length packets
      (`libusb_get_max_iso_packet_size()`-derived, last packet takes the remainder); the
      spec's `packetLengths` allows a different length per packet. Non-uniform
      `packetLengths` are rejected with `NotSupportedError` rather than silently
      mis-transferred.
    - If `dev.backend`/`dev._ctx.handle` aren't available (non-`libusb1` backend, or a future
      `pyusb` version restructures these attributes), both methods fall back to
      `NotSupportedError` instead of raising.
    Added the endpoint-type check (`InvalidAccessError` for a non-isochronous endpoint,
    reusing `_endpoint_available_or_error()` with a new `required_type` parameter) and
    per-packet `status`/`data`/`bytesWritten` result shapes matching
    `USBIsochronousInTransferResult`/`USBIsochronousOutTransferResult`. Tested: parameter
    validation, the claimed/endpoint-type gates, and packet-splitting arithmetic against a
    fake backend (Python `test_isochronousTransfer_*`, JS `isochronousTransferIn/Out`
    fixture tests covering `NotFoundError`/`InvalidAccessError`/success). **Not tested: an
    actual isochronous transfer against real hardware** — there is no USB device available in
    this environment to verify against. Treat this specific feature as unverified until
    someone runs it against a real isochronous device (a USB audio or webcam interface is a
    good candidate) and reports back.

### Project metadata
- Version bumped to `0.0.2`.



A line-by-line audit against the actual WebUSB spec source (`WICG/webusb`'s `index.bs`,
fetched directly from GitHub rather than relying on recollection of the rendered page) and
the real `pyusb` API (installed and introspected, not assumed from memory), looking
specifically for spec-defined behavior this implementation didn't yet have, and for bugs the
existing test suite's blind spots could be hiding.

### Fixed
- **`bulkTransferIn()` was missing the IN direction bit.** The spec's
  `transferIn(endpointNumber, length)` algorithm computes
  `endpointAddress = endpointNumber | 0x80` before touching the device; this implementation
  passed `endpointNumber` straight through to `pyusb`'s `Device.read()`, whose `endpoint`
  parameter is documented (confirmed from the installed pyusb source) to require the full
  `bEndpointAddress`, not the bare number. In practice `device.transferIn(1, ...)` was
  targeting address `0x01` (endpoint 1 **OUT**) instead of `0x81` (endpoint 1 **IN**) — real
  IN transfers would have failed against essentially any actual device.
  `bulkTransferOut`/`transferOut` were unaffected (OUT is address `endpointNumber` unchanged,
  per the same spec algorithm). Added `test_bulkTransferIn_adds_the_in_direction_bit`, which
  opens a fake device and asserts the exact byte passed to the mocked `.read()`/`.write()`
  calls; confirmed it fails against the unfixed code (`assert 1 == 129`) before re-fixing.
- **`requestDeviceChooser()` had no reentrancy guard.** The chooser dialog is shown with
  `QDialog.exec()`, which runs a nested Qt event loop; a second call to
  `requestDeviceChooser()` arriving on the same `WebUSBBridge` instance while that nested loop
  is running (double-invocation from the page, a queued `QWebChannel` message serviced
  mid-loop, etc.) would reenter the method and could open a second chooser on top of the
  first. Added a `_chooser_active` guard — `requestDeviceChooser()` is now a thin wrapper with
  a `try`/`finally` around the actual implementation, which moved to
  `_request_device_chooser_impl` (deliberately **not** `@Slot`-decorated, and now covered by
  the existing `test_requestDeviceChooser_is_registered_as_qt_slot` so it can't silently
  become JS-reachable later). A reentrant call now gets an immediate `InvalidStateError`
  instead of a second dialog. Added `test_requestDeviceChooser_reentrancy_guard`, which
  reenters from inside the (fake) dialog's `exec()`; confirmed it fails against the unguarded
  code — it actually hits Python's recursion limit (`maximum recursion depth exceeded`), a
  fairly vivid demonstration of why the guard matters — before re-fixing.
  `polyfill.py`'s `requestDevice()` previously discarded `res.error` for any
  `cancelled: true` response and always reported a generic `NotFoundError('No device
  selected.')`; it now routes through `throwFromResult` (extended to recognize an
  `InvalidStateError:` prefix, alongside the existing `SecurityError:` one) so this new
  rejection reason — and any other real failure — is no longer indistinguishable from the user
  simply clicking Cancel.
- **`controlTransferIn`/`controlTransferOut` could bypass `claimInterface()`'s protected-class
  rejection entirely.** The spec runs a [control transfer validation
  algorithm](https://wicg.github.io/webusb/#control-transfer-validation-algorithm) before
  every control transfer — reject `requestType: 'class'` requests targeting a protected-class
  interface, reject `recipient: 'interface'`/`'endpoint'` requests targeting a protected-class
  interface, require the owning interface to actually be claimed for those two recipients, and
  restrict `requestType: 'standard'` to a handful of read-only requests — and this
  implementation ran none of it. In practice a page could skip `claimInterface()` altogether
  (which does reject protected classes like HID) and reach the same interface directly with
  `controlTransferOut({requestType: 'class', recipient: 'interface', index: <that interface
  number>, ...}, data)`, which went straight through to `pyusb.ctrl_transfer()` with no check
  at all. Added `_control_transfer_validation_error()`, called from both methods before
  touching the device, decoding `requestType`/`recipient` directly from the `bmRequestType`
  byte already being constructed (no new parameters needed from JS). Added three regression
  tests covering the class-request bypass, the interface-recipient claim requirement, and the
  standard-request restrictions; confirmed each fails against a copy with the two call sites
  removed.
- **`interface_class_for()`'s "not found" sentinel silently defeated the safety fallback it
  was meant to feed.** It returned `-1` for an interface number that doesn't exist on the
  device, documented as "let the caller fail safe (reject)" — but `is_protected_interface_class()`
  only falls back to "reject" when `int(...)` *raises* (`TypeError`/`ValueError`), and
  `int(-1)` doesn't raise; `-1 not in PROTECTED_INTERFACE_CLASSES` cleanly evaluates to
  `False`, i.e. "not protected." Reproduced directly at a Python prompt:
  `is_protected_interface_class(-1)` really does return `False`. `claimInterface()` uses
  exactly this pair of calls, so an interface number that doesn't match any real interface was
  being treated as safe to claim instead of rejected. Changed the sentinel to `None`, which
  `is_protected_interface_class()` already handles correctly through the same fallback
  (`int(None)` raises `TypeError`). Added `test_unknown_interface_number_treated_as_protected`;
  confirmed it fails against the `-1` version.
- **`interface_class_for()` (and the new endpoint-recipient check above) searched every
  configuration the device declares, not just the currently active one.** For the very common
  case of a single-configuration device this made no difference, but on a device with more
  than one configuration, an inactive configuration's interface could be found first and used
  for the protected-class/claimed check instead of the interface that's actually reachable
  right now. Both now call `pyusb`'s `get_active_configuration()` first and search only within
  it, falling back to "unknown → protected" (same as the previous fix) if the active
  configuration itself can't be determined. Added
  `test_interface_class_for_scoped_to_active_configuration`, using two configurations that
  deliberately disagree about interface 0's class so a wrong scope produces a wrong class;
  confirmed it fails against the unscoped search.

### Added
Missing pieces found by comparing the descriptor-building code against the spec's IDL and
algorithms — gaps, not bugs in existing behavior:
- **`PROTECTED_INTERFACE_CLASSES` was missing Hub (`0x09`).** The spec's own
  [protected interface classes](https://wicg.github.io/webusb/#h-protected-classes) table
  lists 8 classes; this implementation had 7 (Hub was the omission). `claimInterface()` would
  previously have allowed claiming a Hub-class interface. Added
  `test_hub_interface_flagged_protected`.
- **`USBConfiguration.configurationName`** and **`USBAlternateInterface.interfaceName`** —
  spec-defined attributes (the `iConfiguration`/`iInterface` string descriptors, confirmed
  present on `pyusb`'s `Configuration`/`Interface` objects from their actual source) that
  `build_configurations_tree()` never populated; the keys simply didn't exist in the returned
  descriptor. Both fall back to `None` when the device doesn't define the string (index `0`),
  matching how `manufacturerName`/`productName`/`serialNumber` already behave. Added
  `test_configuration_and_interface_names`.
- **Endpoints list no longer includes Control-Transfer-Type descriptors.** The spec's
  `USBAlternateInterface` construction steps explicitly skip descriptors whose `bmAttributes`
  indicates Control Transfer Type, noting "there shouldn't be any endpoint object belongs to
  Control Transfer Type" — and `USBEndpointType` doesn't even define a `"control"` value
  (only `"bulk"`/`"interrupt"`/`"isochronous"`). Real device descriptors essentially never
  trigger this in practice, but the implementation now matches the spec's own stated
  invariant instead of an implicit assumption. Added
  `test_control_type_endpoint_excluded_from_endpoints`.

Every item above (in both this section and the three new entries added to *Fixed*) was
verified to fail against a reverted copy of the code before being re-fixed, following the same
practice as the `0.0.0` entries below.

### Verified against the spec / real sources — no change needed
Things this audit specifically checked and found already correct, recorded here rather than
silently passed over:
- The known-security-key blocklist (43 entries) matches Chromium's actual `usb_blocklist.cc`
  byte-for-byte (fetched live from `github.com/chromium/chromium`).
- `device_matches_usb_filter()`'s classCode/subclassCode/protocolCode precedence — including
  the "any interface matches → match, independent of the device-level class" short-circuit —
  matches the spec's filter-matching algorithm step for step.
- `claimInterface()`/`releaseInterface()` on an already-claimed/already-released interface
  resolve successfully rather than erroring, per spec — confirmed this falls out of `pyusb`'s
  own `claim_interface()`/`release_interface()` being idempotent (read from the installed
  `pyusb` source), so no explicit guard was needed on top.
- `set_configuration()`'s parameter is the actual `bConfigurationValue`, not a 0-based index
  (confirmed from the installed `pyusb` source), matching how `selectConfiguration()` already
  called it.

### Project metadata
- Version bumped to `0.0.1a0` — this is pre-1.0, alpha-stage software, and the version number
  now says so explicitly rather than reading `0.0.0`.
- `pyproject.toml`'s `Homepage`/`Issues` URLs point at the actual repository,
  `https://github.com/steck0714/Mock-webusb`, instead of the `YOUR_USERNAME` placeholder.

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
