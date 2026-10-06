"""引擎纯函数测试：工作日推算、事件物化。"""
from datetime import date
from types import SimpleNamespace

import pytest

from app import constants as C
from app.engine import add_business_days, is_business_day, materialize, parse_dt


def test_weekend_is_non_business_by_default():
    # 2025-03-08 周六，无特殊安排
    assert is_business_day(date(2025, 3, 7), "CN") is True
    assert is_business_day(date(2025, 3, 8), "CN") is False


def test_spring_festival_holidays_are_skipped():
    # 2025 春节假期 1/28-2/4，1/26(周日)、2/8(周六) 调休上班
    assert is_business_day(date(2025, 1, 29), "CN") is False
    assert is_business_day(date(2025, 2, 2), "CN") is False
    assert is_business_day(date(2025, 1, 26), "CN") is True   # 周日补班
    assert is_business_day(date(2025, 2, 8), "CN") is True    # 周六补班


def test_add_business_days_skips_holidays_and_uses_makeup_days():
    # 锚点 1/24(周五)，5 个工作日：
    # 1/25(六)休，1/26(日·调休补班)=1，1/27(一)=2，
    # 1/28-2/4 春节假期，2/5=3，2/6=4，2/7=5
    assert add_business_days(date(2025, 1, 24), 5, "CN") == date(2025, 2, 7)


def test_add_business_days_counts_sunday_makeup():
    # 锚点 1/24，2 个工作日：1/27(一)=1，1/28 起放假，1/26 是此前周日不算
    # 反向验证：锚点 1/23(周四)，2 个工作日 => 1/26(周日补班)
    assert add_business_days(date(2025, 1, 23), 2, "CN") == date(2025, 1, 26)


def test_add_business_days_normal_run():
    # 锚点周五 2/14，10 个工作日 => 2/28
    assert add_business_days(date(2025, 2, 14), 10, "CN") == date(2025, 2, 28)


def _row(event_id, event_type, event_time, arrived_at, is_correction=0,
         action=None, target=None, note=""):
    return SimpleNamespace(
        event_id=event_id, event_type=event_type,
        event_time=event_time.isoformat(), arrived_at=arrived_at.isoformat(),
        is_correction=is_correction, correction_action=action,
        target_event_id=target, note=note,
    )


def test_materialize_keeps_earliest_effective_event():
    rows = [
        _row("s2", C.SIGNED, parse_dt("2025-02-11T15:00:00+08:00"),
             parse_dt("2025-02-11T15:05:00+08:00")),
        _row("s1", C.SIGNED, parse_dt("2025-02-11T10:00:00+08:00"),
             parse_dt("2025-02-11T10:05:00+08:00")),
    ]
    events, amendments = materialize(rows)
    assert events[C.SIGNED].event_id == "s1"
    assert amendments == []


def test_materialize_amend_cancel_insert():
    rows = [
        _row("s1", C.SIGNED, parse_dt("2025-02-10T10:00:00+08:00"),
             parse_dt("2025-02-10T10:05:00+08:00")),
        _row("c1", C.SIGNED, parse_dt("2025-02-12T09:00:00+08:00"),
             parse_dt("2025-02-20T08:00:00+08:00"),
             is_correction=1, action=C.AMEND, target="s1"),
    ]
    events, amendments = materialize(rows)
    assert events[C.SIGNED].effective_time == parse_dt("2025-02-12T09:00:00+08:00")
    assert len(amendments) == 1 and "修正" in amendments[0]

    rows.append(_row("c2", C.SIGNED, parse_dt("2025-02-12T09:00:00+08:00"),
                     parse_dt("2025-02-21T08:00:00+08:00"),
                     is_correction=1, action=C.CANCEL, target="s1"))
    events, _ = materialize(rows)
    assert C.SIGNED not in events

    rows.append(_row("c3", C.LOST_CONFIRMED, parse_dt("2025-02-15T09:00:00+08:00"),
                     parse_dt("2025-02-22T08:00:00+08:00"),
                     is_correction=1, action=C.INSERT, note="客服补录"))
    events, amendments = materialize(rows)
    assert C.LOST_CONFIRMED in events and C.SIGNED not in events
    assert any("补录" in a for a in amendments)


def test_parse_dt_rejects_naive():
    with pytest.raises(ValueError):
        parse_dt("2025-02-11T10:00:00")
