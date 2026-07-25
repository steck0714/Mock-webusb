# -*- coding: utf-8 -*-
"""tests/test_polyfill.js が読み込む純粋な.jsファイルを、パッケージ本体の
WEBUSB_POLYFILL_JS定数から生成する。(Node.js側からPythonの文字列定数を直接
importすることはできないため、テスト実行前にこのスクリプトで一度書き出す)

使い方:
    python tests/extract_polyfill_js.py && node tests/test_polyfill.js
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pyside6_webusb.polyfill import WEBUSB_POLYFILL_JS

out_path = os.path.join(os.path.dirname(__file__), "_polyfill_extracted.js")
with open(out_path, "w", encoding="utf-8") as f:
    f.write(WEBUSB_POLYFILL_JS)
print(f"wrote {len(WEBUSB_POLYFILL_JS)} bytes to {out_path}")
