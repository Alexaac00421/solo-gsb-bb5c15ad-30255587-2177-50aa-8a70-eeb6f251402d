"""节假日表工作日诊断 API 测试：逐日判定、补班/放假、只读性与参数拒绝。"""
import pytest

from app.engine import diagnose_business_days, parse_cutoff, parse_dt


def _diag(client, **over):
    payload = {
        "calendar_code": "CN",
        "timezone": "Asia/Shanghai",
        "start_at": "2025-09-30T10:00:00+08:00",
        "business_days": 10,
        "cutoff": "18:00",
    }
    payload.update(over)
    return client.post("/api/v1/calendars/diagnose", json=payload)


def _by_date(b):
    return {e["date"]: e for e in b["entries"]}


# ------------------------------------------------------------ 正常路径

def test_golden_week_diagnosis_counts_and_skips(client):
    r = _diag(client)
    assert r.status_code == 200, r.text
    b = r.json()

    assert b["calendar_code"] == "CN"
    assert b["count_start_date"] == "2025-10-01"          # 自起始日次日起
    assert b["deadline_date"] == "2025-10-21"             # 第 10 个工作日
    assert b["deadline_local"] == "2025-10-21T18:00:00+08:00"
    assert b["deadline_utc"] == "2025-10-21T10:00:00+00:00"

    days = _by_date(b)
    # 国庆+中秋放假 8 天全部跳过，累计工作日保持 0
    for d in ("2025-10-01", "2025-10-02", "2025-10-08"):
        e = days[d]
        assert e["verdict"] == "休息" and e["skipped"] is True
        assert e["is_business_day"] is False
        assert e["declaration"]["kind"] == "holiday"
        assert e["declaration"]["name"]
        assert e["business_day_count"] == 0
        assert "放假" in e["reason"] and "跳过" in e["reason"]

    # 10/11 周六调休补班 => 计为第 3 个工作日
    makeup = days["2025-10-11"]
    assert makeup["verdict"] == "工作" and makeup["is_business_day"] is True
    assert makeup["declaration"]["kind"] == "workday"
    assert makeup["business_day_count"] == 3
    assert "补班" in makeup["reason"] and "计入" in makeup["reason"]

    # 普通周末按默认规则休息；普通工作日计入
    assert days["2025-10-12"]["verdict"] == "休息"
    assert "周六" in days["2025-10-12"]["weekend_default_rule"]
    assert days["2025-10-09"]["verdict"] == "工作"
    assert days["2025-10-09"]["business_day_count"] == 1
    assert days["2025-10-09"]["declaration"] is None
    assert "周一至周五" in days["2025-10-09"]["weekend_default_rule"]

    # 最后一个条目即第 N 个工作日，之后不再列出
    assert b["entries"][-1]["date"] == "2025-10-21"
    assert b["summary"] == {
        "business_days_counted": 10,
        "rest_days_seen": 11,
        "declared_holidays_seen": 8,
        "declared_makeup_workdays_seen": 1,
        "days_listed": 21,
    }
    assert b["read_only"] is True


def test_start_day_is_never_counted_even_if_makeup_workday(client):
    # 2025-01-26（周日，春节调休补班）作为起始日：当天不计入，次日 1/27(一)=1
    r = _diag(client, start_at="2025-01-26T09:00:00+08:00", business_days=1)
    b = r.json()
    assert b["count_start_date"] == "2025-01-27"
    assert b["entries"][0]["date"] == "2025-01-27"
    assert b["entries"][0]["business_day_count"] == 1


def test_holiday_on_weekday_skipped_and_makeup_weekend_counted(client):
    # 2025 春节：自 1/24（周五）次日起：1/25(六)休、1/26(日补班)=1、1/27(一)=2、
    # 1/28-2/4 放假跳过、2/5(三)=3、2/6=4、2/7(五)=5；2/8(六补班)在到达第 5 个后不再列出
    r = _diag(client, start_at="2025-01-24T20:00:00+08:00", business_days=5,
              cutoff="23:59:59")
    b = r.json()
    assert b["deadline_date"] == "2025-02-07"
    days = _by_date(b)
    assert days["2025-01-26"]["verdict"] == "工作"
    assert days["2025-01-26"]["business_day_count"] == 1
    assert days["2025-01-28"]["verdict"] == "休息"
    assert days["2025-01-28"]["declaration"]["kind"] == "holiday"
    assert days["2025-01-28"]["business_day_count"] == 2
    assert days["2025-02-04"]["verdict"] == "休息"
    assert days["2025-02-04"]["business_day_count"] == 2
    assert days["2025-02-05"]["business_day_count"] == 3
    assert "2025-02-08" not in days
    assert b["deadline_local"] == "2025-02-07T23:59:59+08:00"
    assert b["deadline_utc"] == "2025-02-07T15:59:59+00:00"


def test_cutoff_accepts_hhmm_and_hhmmss(client):
    r1 = _diag(client, business_days=1, cutoff="09:30")
    r2 = _diag(client, business_days=1, cutoff="09:30:00")
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["deadline_local"] == r2.json()["deadline_local"]


def test_diagnosis_is_timezone_aware(client):
    # 同一时刻 2025-09-30T02:00+08 == 2025-09-29T19:00+01（伦敦夏令时）
    r = _diag(client, timezone="Europe/London",
              start_at="2025-09-30T02:00:00+08:00",
              business_days=1, cutoff="09:30:00")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["start_at_local"] == "2025-09-29T19:00:00+01:00"
    assert b["count_start_date"] == "2025-09-30"
    assert b["deadline_local"] == "2025-09-30T09:30:00+01:00"
    assert b["deadline_utc"] == "2025-09-30T08:30:00+00:00"


def test_diagnose_is_read_only(client):
    before = client.get("/api/v1/calendars/CN").json()
    _diag(client)
    after = client.get("/api/v1/calendars/CN").json()
    assert before == after
    # 不产生订单/事件/凭证
    assert client.get("/api/v1/policies").status_code == 200


def test_engine_pure_function_matches_seed_calendar():
    cal = {
        "2025-10-01": ("holiday", "国庆节、中秋节"),
        "2025-10-11": ("workday", "国庆调休上班"),
    }
    out = diagnose_business_days(
        parse_dt("2025-10-08T10:00:00+08:00"), 1,
        parse_cutoff("18:00:00"), "Asia/Shanghai", cal)
    assert out["deadline_date"] == "2025-10-09"
    assert out["deadline_utc"] == "2025-10-09T10:00:00+00:00"
    assert out["entries"][0]["declaration"] is None


# ------------------------------------------------------------ 拒绝路径

def test_naive_start_at_rejected(client):
    r = _diag(client, start_at="2025-09-30T10:00:00")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert any(d["field"] == "start_at" for d in err["details"])


def test_missing_calendar_rejected(client):
    r = _diag(client, calendar_code="ZZ")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "CALENDAR_NOT_FOUND"


def test_unknown_timezone_rejected(client):
    r = _diag(client, timezone="Asia/NoCity")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "TIMEZONE_INVALID"


@pytest.mark.parametrize("n", [0, 61, -3])
def test_business_days_out_of_range_rejected(client, n):
    r = _diag(client, business_days=n)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("cutoff", ["24:00", "18:60", "18:00:60", "1800", "abc", "12"])
def test_invalid_cutoff_rejected(client, cutoff):
    r = _diag(client, cutoff=cutoff)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_missing_fields_rejected(client):
    r = client.post("/api/v1/calendars/diagnose", json={"calendar_code": "CN"})
    assert r.status_code == 422
    fields = {d["field"] for d in r.json()["error"]["details"]}
    assert {"timezone", "start_at", "business_days", "cutoff"} <= fields


def test_boundary_business_days_accepted(client):
    assert _diag(client, business_days=1).status_code == 200
    assert _diag(client, business_days=60,
                 start_at="2025-01-01T00:00:00+08:00").status_code == 200
