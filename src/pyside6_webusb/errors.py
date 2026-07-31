# -*- coding: utf-8 -*-
"""DOMException名のプレフィックス規約を一箇所にまとめるモジュール。

polyfill.py の throwFromResult() は、Pythonから返ってきたエラー文字列の先頭が
"XxxError:" という決まった形式になっているかどうかで、投げるDOMExceptionの
種別(SecurityError/InvalidStateError/NotFoundError/InvalidAccessError/
IndexSizeError)を振り分けている。

これまではbridge.py内の10箇所以上で `f"SecurityError: {msg}"` のような
文字列をその場で手書きしていた。これ自体は動くが、
  - "SecurtyError:"のようなタイプミスがあっても構文エラーにはならず、
    JS側のthrowFromResult()が単に気づかずデフォルトのNetworkErrorへ
    フォールバックしてしまう(=気づきにくい形で誤ったDOMException名が
    JSへ届く)
  - 同じ規約を守る責任が呼び出し側每回に分散している
という問題があるため、関数として一箇所にまとめておく。

使い方:
    from .errors import security_error, invalid_state_error
    return json.dumps({"success": False, "error": security_error("...")})
"""


def _prefixed(name, message):
    return f"{name}: {message}"


def security_error(message):
    """保護対象インターフェースクラス/ブロックリスト機器の拒否など。"""
    return _prefixed("SecurityError", message)


def invalid_state_error(message):
    """「今この操作を受け付けられる状態ではない」系(configuration未選択、
    interface未claim、チューザー多重起動等)。"""
    return _prefixed("InvalidStateError", message)


def not_found_error(message):
    """指定されたinterface/endpoint/デバイスが見つからない、またはチューザーで
    ユーザーがキャンセルした場合。"""
    return _prefixed("NotFoundError", message)


def invalid_access_error(message):
    """指定はできる(=見つかる)が、要求された操作の対象として不適切
    (例: isochronous以外のendpointへisochronous転送を要求した)。"""
    return _prefixed("InvalidAccessError", message)


def index_size_error(message):
    """数値が許容範囲外(例: endpoint番号が1-15の範囲外)。"""
    return _prefixed("IndexSizeError", message)
