# -*- coding: utf-8 -*-
"""pytestに't/'を発見させた際、pip install不要でsrc/レイアウトのパッケージを
importできるようにするための設定。`pip install -e .`済みなら本来不要だが、
素のクローン直後でも `pytest` や `python tests/test_*.py` が動くようにしておく。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
