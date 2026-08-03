# Changelog

All notable changes to this project are documented here.

## [0.0.3b0]

Implemented the per-frame origin attribution design that `0.0.3`/`0.0.3a0` researched but
didn't ship. `navigator.usb` is available in iframes again (`setRunsOnSubFrames(True)`,
reverting the `0.0.2b0` lockdown) — but now backed by a real per-frame origin check instead of
the vulnerable page-wide one that made that lockdown necessary in the first place.

### Added

- **`frame_origin.py`** (new module) — `FrameOriginTracker`, which is what makes this safe to
  re-enable. Design: Python (not JS) walks the real frame tree via
  `QWebEnginePage.mainFrame()`/`QWebEngineFrame.children()`, mints an unguessable token
  (`secrets.token_urlsafe`) per frame, and pushes it into *that specific frame* via
  `QWebEngineFrame.runJavaScript()` — a different, cross-origin frame cannot read another
  frame's token without breaking the same-origin policy itself. `WEBUSB_POLYFILL_JS` reads its
  own `window.__pyUsbFrameToken` and sends it with every bridge call; `WebUSBBridge` resolves
  the caller's real origin by looking the token up, never by trusting anything JS claims about
  itself. Rescans are triggered by `QWebEnginePage.navigationRequested` (fires once per frame
  navigation, including subframes — confirmed in `0.0.3a0`), staggered a few times after each
  signal since the request arrives before the frame necessarily exists in the tree, plus a
  2-second periodic rescan as a backstop.

  Three things only showed up once this was tested against a **real** `QWebEnginePage` (not
  the fakes the rest of the test suite uses), which is exactly why that testing mattered:
    - `QWebEngineFrame.runJavaScript(code)` with a single argument raises *"not enough
      arguments"* — it requires a callback as a second argument, even one that does nothing.
    - `QWebEnginePage.setHtml(html, baseUrl=...)` does **not** make
      `QWebEngineFrame.url()` reflect `baseUrl` — it reports the content as a `data:` URL
      instead, which correctly (if confusingly, for testing) resolves to no origin at all.
      Real navigations (`page.load(QUrl(...))`) behave as expected; the integration test uses
      that instead.
    - `QWebEngineFrame` Python objects returned by `children()`/`mainFrame()` don't appear to
      be stable across calls — the same underlying iframe showed different `id()` values on
      successive rescans. The original design tried to track "does this frame already have a
      valid token" per frame identity to avoid re-issuing tokens; that check silently never
      matched, defeating its own purpose (harmlessly — it just re-issued tokens more than
      necessary). Removed that optimization rather than build something fragile on top of an
      identity assumption that doesn't hold; every rescan simply (re-)issues a fresh token for
      every frame it finds now, with a global cap (`_MAX_TOTAL_TOKENS`) on how many tokens are
      kept at once so this can't grow without bound.

- **`frame_token` parameter, threaded through every origin-sensitive `@Slot`** (`listDevices`,
  `requestDeviceChooser`, `openDevice`, `closeDevice`, `claimInterface`, `releaseInterface`,
  `selectConfiguration`, `selectAlternateInterface`, `resetDevice`, `clearHalt`,
  `bulkTransferIn`/`Out`, `controlTransferIn`/`Out`, `isochronousTransferIn`/`Out`,
  `forgetGrantedDevice` — 16 methods). Defaults to `""`, so every existing direct-Python test
  call site kept working unchanged (`WebUSBBridge._current_origin(frame_token="")` falls back
  to the pre-`0.0.3b0` `page.url()`-only behavior whenever `self._frame_tracker` is `None`,
  which is exactly the case for tests built via `make_bridge()` or any other use of this
  package without `install()`). `_get_open_device()` also takes `frame_token` now, since it's
  the shared choke point roughly a dozen of those methods funnel through to authorize reuse of
  an already-open handle — this is what makes the fix functionally complete rather than just
  "fails closed but broken": a handle opened by a subframe now stays usable by that *same*
  subframe for follow-up calls, not just at the moment it was opened.

  **The one rule this all depends on:** when `self._frame_tracker` is not `None`,
  `_current_origin()` resolves *exclusively* through the token — including for the main frame,
  which also gets an assigned token now like any other frame. There is no "empty token falls
  back to the top-level page" case once a tracker is wired, on purpose: allowing that would
  let a hostile subframe calling the raw `QWebChannel` object directly (bypassing the polyfill
  entirely) impersonate the top-level page just by omitting the token — reintroducing the exact
  `0.0.2b0` vulnerability this whole feature exists to close.

  Added a dedicated internal `_top_level_origin()` (always reads `page.url()` directly, no
  token involved) for the two places that were never about "which frame is calling" in the
  first place: `_on_page_navigated()` (detects the top-level page itself navigating, to drop
  stale handles) and the hotplug watcher's `deviceConnected`/`deviceDisconnected` filtering
  (these are `Signal`s broadcast to every frame on the page — there's no mechanism to target a
  `Signal` at one specific frame, so per-frame-scoped hotplug notifications are out of scope
  for now; they're filtered against the top-level page's grants, same as before).

- **Tests**: `tests/test_frame_origin.py` — eight fast tests against fakes (token issuance,
  nested-frame walking, opaque-URL rejection, the `is_functional` fallback, the total-token
  cap, exception resilience) plus one real-`QWebEnginePage` integration test (loads local HTML
  containing a cross-origin iframe, confirms the iframe gets a token that resolves to its own
  real origin, and that an empty or forged token resolves to nothing). `tests/test_bridge.py`
  gained two more: `test_frame_tracker_wired_denies_empty_and_forged_tokens` and
  `test_frame_tracker_wired_isolates_handles_between_different_frame_origins`, exercising the
  exact attack shape this feature closes without needing a real browser context for every run.

### Fixed
- `install()`'s two injected `QWebEngineScript`s now call `setRunsOnSubFrames(True)` again —
  but only when `FrameOriginTracker.wire()` actually managed to connect to
  `navigationRequested` (`tracker.is_functional`). On a PySide6/Qt version old enough not to
  have that signal, `install()` falls back to the `0.0.2b0` behavior
  (`setRunsOnSubFrames(False)`) automatically rather than silently running with no real
  per-frame protection.

### Project metadata
- Version bumped to `0.0.3b0`.

## [0.0.3a0]

Follow-up to `0.0.3`'s research question: does a per-frame navigation/load signal actually
exist? Also extracted the DOMException-prefix convention into its own module and added its own
test coverage.

### Researched further: `navigationRequested` is the per-frame signal that was missing

`0.0.3` confirmed `QWebEngineFrame` can enumerate frames and their real origins, but noted
`loadFinished`/`loadStarted` only fire once per page load, not once per frame — leaving "how do
we know when a new/navigating frame appears" unanswered. Dumped `QWebEnginePage`'s full signal
list via `QMetaObject` (rather than guessing from documentation) and found
`navigationRequested(QWebEngineNavigationRequest&)`, which carries `.url()` and
`.isMainFrame()`. Verified empirically against a real `QWebEnginePage` loading local HTML with
two iframes: it fired **three times** — once for the top-level page (`isMainFrame=True`) and
once for *each* iframe (`isMainFrame=False`), each with its own correct URL. This is exactly
the per-frame trigger `0.0.3` said didn't exist at the `QWebEnginePage` level.

This doesn't complete the design by itself. Correlating a `navigationRequested` event to the
specific `QWebEngineFrame` object needed for `runJavaScript()`-based token delivery still
requires re-walking `mainFrame()`/`children()` after the signal fires (the navigation request
arrives before the frame necessarily exists in the tree). More significantly, re-checking the
full design against `_get_open_device()` — the choke point roughly a dozen other `@Slot`
methods funnel through to authorize use of an already-open handle — showed that a token can't
be limited to just the three entry-point methods (`getDevices`, `requestDevice`, `open`) the
way `0.0.3` hoped: unless *every* handle-consuming call also resolves the real calling frame's
origin, a handle legitimately opened by a subframe would fail every subsequent operation
against the (unfixed) top-level-page-only origin check — safe, but non-functional, not a real
fix. Confirming this needs the token threaded through every handle-consuming method (not just
three) before it can be both correct and usable; not attempted in this version for the same
reason as `0.0.3` — it's a wide, security-sensitive change that deserves to land as its own
reviewable unit, not mixed in with this version's other fixes.

### Added
- **`errors.py`**: centralizes the `"SecurityError:"`/`"InvalidStateError:"`/`"NotFoundError:"`/
  `"InvalidAccessError:"`/`"IndexSizeError:"` prefix convention that `polyfill.py`'s
  `throwFromResult()` matches against. Previously these were hand-typed as raw f-strings in
  (at least) 10 places in `bridge.py` — functional, but a typo in any of them (e.g.
  `"SecurtyError:"`) wouldn't be a syntax error, just a silently-wrong `DOMException` name
  reaching JS, since `throwFromResult()` would fail to recognize the misspelled prefix and fall
  back to whatever generic default the caller specified. `bridge.py` now calls
  `security_error(msg)` etc. instead of constructing the string inline, including
  `_control_transfer_validation_error()`'s local `_err()` helper, which now delegates to the
  same functions rather than duplicating the prefix format. Added `tests/test_errors.py`.

### Project metadata
- Version bumped to `0.0.3a0`.

## [0.0.3]

Investigated whether per-frame origin attribution — the thing `0.0.2b0`'s `Security` entry
concluded "isn't currently possible through PySide6's public API" — is actually possible after
all, given a newer PySide6/Qt version. Also confirmed the currently-installed `pyusb` (1.3.1)
is the latest release on PyPI, so the isochronous-transfer limitations documented in `0.0.2`
aren't an artifact of using an outdated `pyusb`.

### Researched: per-frame origin attribution (not yet implemented)

**The earlier conclusion needs updating.** `QWebEngineFrame` — confirmed present in the
installed `PySide6` 6.11.1 (`from PySide6.QtWebEngineCore import QWebEngineFrame`) — did not
exist in older PySide6/Qt WebEngine releases, which is presumably why the `0.0.2b0` fix landed
on "disable subframes entirely" as the only safe option available at the time. Verified
empirically in this environment, against a real `QWebEnginePage` loading real HTML with an
iframe (not just reading documentation):

- `page.mainFrame()` returns a `QWebEngineFrame` for the top-level frame.
- `frame.children()` recursively returns child frames — a real iframe
  (`<iframe src="https://example.org/child.html" name="childframe">`) showed up correctly,
  with its own accurate `.url()` (`https://example.org/child.html`) and `.name()`
  (`childframe`), both read from Qt/Chromium's own frame-tracking (not from any
  JS-supplied value) via `page.mainFrame().children()`.
- `frame.runJavaScript(code, callback)` exists and accepts a per-frame target, though its
  exact reliability (e.g. against a frame with no content loaded) wasn't fully pinned down in
  this pass and needs more testing before depending on it.

This changes the picture, but **does not by itself solve the problem** — `QWebEnginePage`'s
`setWebChannel(channel, worldId=None)` (confirmed via its actual overload signature) is still
page-scoped, not frame-scoped, and there's still no signal or callback that tells Python
"this specific incoming `QWebChannel` call came from *this* frame." Enumerating frames answers
"what frames currently exist and what are their real origins" — it doesn't answer "which frame
just invoked `requestDeviceChooser()`."

**A viable design, sketched but not implemented:** have Python (not JS) mint an unguessable,
per-frame token, push it into that specific frame via `frame.runJavaScript()` (so only code
genuinely running in that frame ever sees its own token — a different frame can't read it
without breaking same-origin isolation itself), and have every bridge call include that token
as an actual parameter (not a separately-set piece of state, which would race across frames
issuing concurrent calls) so Python can look up the real origin the token was issued for. This
needs re-verifying tokens periodically (there's no per-iframe navigation signal at the
`QWebEnginePage` level — confirmed empirically: `loadFinished`/`loadStarted` only fired once
for a page containing an iframe, not once per frame — so detecting a frame's origin changing
out from under an issued token means polling `mainFrame()`/`children()` on a timer, similar to
the existing hotplug-device watcher).

**Why this isn't implemented yet:** doing it properly means adding a token parameter to every
transfer/permission `@Slot` (roughly 19 methods), the matching change to every `callBridge()`
call site in the polyfill, and re-validating the entire existing test suite against the new
signatures — a substantially larger and more security-sensitive change than the surgical fixes
in this and the previous few versions, and one that deserves dedicated review rather than
landing alongside unrelated work. Recorded here as a concrete, verified-feasible design so it
doesn't have to be re-researched from scratch, and so `setRunsOnSubFrames(False)` in `install()`
is understood as "safe default pending this work," not "believed impossible."

### Verified, no change needed
- `pyusb` 1.3.1 (currently installed) is the latest version on PyPI (checked via
  `pip index versions pyusb`) — the isochronous-transfer caveats from `0.0.2` (no public
  high-level API, uniform-packet-length-only backend) reflect `pyusb`'s actual current state,
  not an outdated dependency.

### Project metadata
- Version bumped to `0.0.3`.

## [0.0.2b0]

Continued the spec/Chrome-source comparison from `0.0.1a0`, this time pulling actual Chromium
source (Blink's `usb_device.cc`, fetched live from `github.com/chromium/chromium`) rather than
spec prose alone, plus a first attempt at isochronous transfer support.

### Security
- **🚨 Cross-origin iframes could access the top-level page's granted USB devices.**
  `install()` injected the WebUSB polyfill with `script.setRunsOnSubFrames(True)`, meaning it
  ran inside every iframe on a page, not just the top frame. `WebUSBBridge._current_origin()`
  determines the origin to check permissions against from `QWebEnginePage.url()` — which is
  *always* the top-level frame's URL; there's no way to determine which frame inside a page a
  given `QWebChannel` call actually came from through PySide6's public API. The combination
  meant a cross-origin iframe (a compromised ad, a malicious embed, anything the top-level page
  didn't fully trust) calling `navigator.usb.requestDevice()`/`getDevices()` had its request
  attributed to the *top-level page's* origin — not its own — giving it full access to every
  USB device the top-level page had ever been granted, a complete bypass of the origin
  isolation the rest of this codebase's permission model depends on. Since there's no
  currently-available way to correctly attribute a `QWebChannel` call to the specific frame
  that made it, the fix is to fail closed: `setRunsOnSubFrames(False)`, so `navigator.usb`
  simply isn't defined inside any iframe at all (a real capability loss relative to real
  Chrome, which does support properly-isolated WebUSB in cross-origin iframes — but a correct
  and safe default given what this codebase can actually verify). Found by reading `install()`
  end to end while looking for anything below `WebUSBBridge` itself that could affect the
  security model, rather than only the bridge's own methods.
  Added `tests/test_install.py` — there was **no test coverage of `install()` at all**
  before this, using real `QWebEngineScript`/`QWebChannel` objects against a lightweight fake
  page (no full `QWebEnginePage`/renderer needed). Confirmed
  `test_install_does_not_run_scripts_on_subframes` fails against a copy with
  `setRunsOnSubFrames(True)` restored.

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
- Version bumped to `0.0.2b0`.



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
