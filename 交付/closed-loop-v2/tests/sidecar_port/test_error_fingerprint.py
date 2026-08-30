"""Fingerprint stability: identical failures -> identical fingerprints,
different failures -> different fingerprints."""
from loopcore.fingerprints import Fingerprinter

FP = Fingerprinter()


def test_timestamp_drift_ignored():
    a = FP.fingerprint("Error 2026-08-27T05:22:05.8404262Z: request timed out")
    b = FP.fingerprint("Error 2026-08-27T05:24:11.1Z: request timed out")
    assert a == b
    assert "<ts>" in a


def test_uuid_drift_ignored():
    a = FP.fingerprint(
        "session 01a041ab-453e-78b0-9eb6-348ce4143091 failed")
    b = FP.fingerprint(
        "session 99f3c2de-1234-5678-9abc-def012345678 failed")
    assert a == b
    assert "<id>" in a


def test_line_number_drift_ignored():
    a = FP.fingerprint("SyntaxError in app.py:42")
    b = FP.fingerprint("SyntaxError in app.py:99")
    assert a == b
    assert "app.py:<line>" in a


def test_windows_path_normalized():
    a = FP.fingerprint("File not found: C:\\Users\\abc\\temp\\f.txt")
    b = FP.fingerprint("File not found: C:\\Windows\\System32\\x.txt")
    assert a == b
    assert "<path>" in a


def test_retry_counter_drift_ignored():
    a = FP.fingerprint('provider error: {"message":"Reconnecting... 2/5"}')
    b = FP.fingerprint('provider error: {"message":"Reconnecting... 4/5"}')
    assert a == b


def test_ansi_stripped():
    a = FP.fingerprint("\x1b[31mERR\x1b[0m: timeout")
    b = FP.fingerprint("ERR: timeout")
    assert a == b


def test_different_errors_differ():
    a = FP.fingerprint("connection refused")
    b = FP.fingerprint("permission denied")
    assert a != b
