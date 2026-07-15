"""Adaptive speed measurement: second pass sizing and retry logic."""

import threading

from netmon_monitor.config import Config
from netmon_monitor.probes import adaptive_speed_bytes
from netmon_monitor import workers


def test_adaptive_size_scales_with_speed():
    # 1000 Mbit/s, min 3 s → target 6 s → 750 MB, capped at 200 MB
    assert adaptive_speed_bytes(1000, 3.0, 200_000_000) == 200_000_000
    # 100 Mbit/s → 6 s ≈ 75 MB, under the cap
    assert adaptive_speed_bytes(100, 3.0, 200_000_000) == 75_000_000
    # rounding to whole MB
    assert adaptive_speed_bytes(33, 3.0, 200_000_000) == 24_000_000


def _run_measure(monkeypatch, results, cfg=None):
    """Feed a scripted sequence of speed_test results into measure_speed."""
    calls = []

    def fake_speed_test(url, max_time=120.0, stop=None):
        calls.append(url)
        return results[len(calls) - 1]

    monkeypatch.setattr(workers.probes, "speed_test", fake_speed_test)
    cfg = cfg or Config(speed_bytes=50_000_000, speed_min_seconds=3.0,
                        speed_max_bytes=200_000_000)
    out = workers.measure_speed(cfg, threading.Event())
    return out, calls


def test_fast_result_triggers_bigger_retry(monkeypatch):
    out, calls = _run_measure(monkeypatch, [
        (1096.0, 50_000_000, 0.37, 200),    # too fast → re-measure
        (1180.0, 200_000_000, 1.4, 200),
    ])
    assert out == (1180.0, 200_000_000, 1.4, 200)
    assert len(calls) == 2
    assert "bytes=200000000" in calls[1]    # capped payload


def test_slow_result_measures_once(monkeypatch):
    out, calls = _run_measure(monkeypatch, [
        (31.0, 50_000_000, 12.9, 200),      # ≥ 3 s → good enough
    ])
    assert out == (31.0, 50_000_000, 12.9, 200)
    assert len(calls) == 1


def test_failed_retry_keeps_first_result(monkeypatch):
    out, calls = _run_measure(monkeypatch, [
        (900.0, 50_000_000, 0.44, 200),
        (None, None, 30.0, 0),              # retry failed
    ])
    assert out == (900.0, 50_000_000, 0.44, 200)
    assert len(calls) == 2


def test_no_retry_when_cap_not_above_base(monkeypatch):
    cfg = Config(speed_bytes=50_000_000, speed_min_seconds=3.0,
                 speed_max_bytes=50_000_000)   # adaptive effectively disabled
    out, calls = _run_measure(monkeypatch, [
        (1000.0, 50_000_000, 0.4, 200),
    ], cfg=cfg)
    assert out == (1000.0, 50_000_000, 0.4, 200)
    assert len(calls) == 1
