# Mock-webusb
An AI-generated mock implementation of WebUSB API for PySide.
(Screenshot of the F12 console during testing.)

To run minimal_browser.py from this release on Windows, use this PowerShell command:


$env:PYTHONPATH = "src"

$env:QTWEBENGINE_REMOTE_DEBUGGING = "9222"

python examples/minimal_browser.py

(Since the F12 feature is not enabled by default, please make sure to include $env:QTWEBENGINE_REMOTE_DEBUGGING = "9222" if you need to use the F12 functionality.）

However, please be aware that this configuration might not work out of the box. It is recommended to personally implement the required F12 execution logic within the minimal_browser.py file
"Honestly, since this is just a test/check build, it'd be faster to just have each person tweak and embed the logic on their end."
<img width="1920" height="1174" alt="OpenWeb v6 3 5 2026_07_25 16_17_18" src="https://github.com/user-attachments/assets/aaba8afa-f892-405d-ae46-db6e579907f5" />
## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

### Third-Party Notices
* **qwebchannel.js**: Loaded at runtime from your own Qt installation ([BSD-3-Clause](https://opensource.org), © The Qt Company) and is not redistributed in this repository.

