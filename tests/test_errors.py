# -*- coding: utf-8 -*-
"""errors.py(DOMException名プレフィックス生成の一元管理)のテスト。

polyfill.py の throwFromResult() が実際にプレフィックスを見て振り分けている
"XxxError:"(コロン+半角スペース)という正確な形式を、各関数が寸分違わず
生成することを確認する。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pyside6_webusb import errors as e  # noqa: E402


def test_each_builder_produces_the_exact_prefix_throwFromResult_expects():
    """polyfill.py の throwFromResult() は 'SecurityError:'/'InvalidStateError:' 等
    (コロンの直後に半角スペース1つ)という正確な文字列で前方一致させている。
    ここが1文字でもズレると、JS側は気づかずデフォルトのエラー名へ
    フォールバックしてしまう(サイレントな不具合)。"""
    assert e.security_error("x") == "SecurityError: x"
    assert e.invalid_state_error("x") == "InvalidStateError: x"
    assert e.not_found_error("x") == "NotFoundError: x"
    assert e.invalid_access_error("x") == "InvalidAccessError: x"
    assert e.index_size_error("x") == "IndexSizeError: x"
    print("test_each_builder_produces_the_exact_prefix_throwFromResult_expects: OK")


def test_builders_preserve_the_message_verbatim():
    """メッセージ本文自体は加工されず、そのまま連結されることを確認する
    (エラーメッセージの内容が呼び出し元の意図と食い違わないように)。"""
    msg = "interface 3 is class 'HID (Human Interface Device: ...)', a protected interface class"
    assert e.security_error(msg) == f"SecurityError: {msg}"
    print("test_builders_preserve_the_message_verbatim: OK")


if __name__ == "__main__":
    test_each_builder_produces_the_exact_prefix_throwFromResult_expects()
    test_builders_preserve_the_message_verbatim()
    print("ALL ERRORS TESTS PASSED")
