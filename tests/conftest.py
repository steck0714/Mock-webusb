# -*- coding: utf-8 -*-
"""pytestに'tests/'を発見させた際、pip install不要でsrc/レイアウトのパッケージを
importできるようにするための設定。`pip install -e .`済みなら本来不要だが、
素のクローン直後でも `pytest` や `python tests/test_*.py` が動くようにしておく。

★ ヘッドレス環境(CI、Dockerコンテナ、ディスプレイの無いサンドボックス等)では、
QApplication([]) の生成がPythonの例外ではなく Fatal Python error: Aborted という
捕捉不可能なプロセスクラッシュになることがある(Qtのxcbプラットフォームプラグインが
ディスプレイに接続できず異常終了するため)。DISPLAYが設定されておらず、かつ
QT_QPA_PLATFORMをユーザーが明示していない場合に限り、自動的に'offscreen'へ
フォールバックする。実ディスプレイがある開発機ではこの分岐は発火せず、
通常どおり実プラットフォームで(GUIを見ながら)動く。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
