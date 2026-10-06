"""端到端 API 测试：政策版本、时限推算、去重、倒序报错、归档与迟到更正。"""
from tests.conftest import make_order


def test_missing_field_returns_structured_error(client):
    r = client.post("/api/v1/orders", json={"order_id": "X"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert any(d["field"] == "created_at" for d in err["details"])


def test_naive_datetime_rejected(client):
    r = client.post("/api/v1/orders", json={
        "order_id": "X", "created_at": "2025-02-05T09:00:00",
        "timezone": "Asia/Shanghai", "calendar_code": "CN",
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_policy_version_selected_by_order_creation_time(client):
    make_order(client, "OLD", created_at="2024-06-01T09:00:00+08:00",
               promise_by="2024-06-10T18:00:00+08:00")
    make_order(client, "NEW", created_at="2025-02-05T09:00:00+08:00",
               promise_by="2025-02-10T18:00:00+08:00")
    d_old = client.get("/api/v1/orders/OLD/determination").json()
    d_new = client.get("/api/v1/orders/NEW/determination").json()
    assert d_old["policy_version"] == "v1"
    assert d_new["policy_version"] == "v2"
    assert client.get("/api/v1/orders/OLD/determination").json()["claims"][0]


def test_order_before_any_policy_rejected(client):
    r = client.post("/api/v1/orders", json={
        "order_id": "TOO_EARLY", "created_at": "2022-01-01T00:00:00+08:00",
        "timezone": "Asia/Shanghai", "calendar_code": "CN",
    })
    assert r.status_code == 422 and r.json()["error"]["code"] == "POLICY_NOT_FOUND"


def test_replay_same_event_not_counted_twice(client):
    make_order(client)
    payload = {"event_id": "s1", "event_type": "signed",
               "event_time": "2025-02-11T10:00:00+08:00",
               "arrived_at": "2025-02-11T10:05:00+08:00"}
    assert client.post("/api/v1/orders/O1/events", json=payload).json()["accepted"] == 1
    again = client.post("/api/v1/orders/O1/events", json=payload).json()
    assert again["accepted"] == 0 and again["duplicates"] == ["s1"]
    evs = client.get("/api/v1/orders/O1/events").json()["events"]
    assert len(evs) == 1


def test_out_of_order_events_rejected_and_batch_is_atomic(client):
    make_order(client)
    batch = {"events": [
        {"event_id": "s1", "event_type": "signed",
         "event_time": "2025-02-11T10:00:00+08:00",
         "arrived_at": "2025-02-11T10:05:00+08:00"},
        {"event_id": "s0", "event_type": "signed",
         "event_time": "2025-02-10T09:00:00+08:00",
         "arrived_at": "2025-02-11T09:00:00+08:00"},
    ]}
    r = client.post("/api/v1/orders/O1/events/batch", json=batch)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "EVENT_ORDER_INVALID"
    assert err["details"]["index"] == 1
    # 整批回滚：没有任何事件落库
    assert client.get("/api/v1/orders/O1/events").json()["events"] == []


def test_delay_filable_and_business_day_deadline(client):
    # 承诺 2/10 18:00；签收 2/11 10:00 迟到。锚点 2/11，10 个工作日 => 2/25
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    d = client.get("/api/v1/orders/O1/determination",
                   params={"as_of": "2025-02-20T12:00:00+08:00"}).json()
    delay = d["claims"][0]
    assert d["eligible_to_file"] is True
    assert delay["status"] == "filable"
    assert delay["deadline_local"].startswith("2025-02-25T23:59:59")
    assert d["missing_evidence"] == []
    assert any("春节" in b or "工作日" in b for b in delay["basis"])


def test_delay_not_applicable_when_on_time(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-10T09:00:00+08:00",
        "arrived_at": "2025-02-10T09:05:00+08:00"})
    d = client.get("/api/v1/orders/O1/determination",
                   params={"as_of": "2025-02-10T12:00:00+08:00"}).json()
    assert d["claims"][0]["status"] == "not_applicable"


def test_delay_pending_before_promise_due(client):
    make_order(client)
    d = client.get("/api/v1/orders/O1/determination",
                   params={"as_of": "2025-02-08T12:00:00+08:00"}).json()
    assert d["overall_status"] == "pending"
    assert d["claims"][0]["status"] == "pending"


def test_after_deadline_not_filable(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    d = client.get("/api/v1/orders/O1/determination",
                   params={"as_of": "2025-02-26T08:00:00+08:00"}).json()
    assert d["claims"][0]["status"] == "not_filable"
    assert d["eligible_to_file"] is False


def test_damage_claim_business_day_window_v2(client):
    # 破损申报 2/11，v2 窗口 3 个工作日：2/12,2/13,2/14 => 截止 2/14
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "g1", "event_type": "damage_reported",
        "event_time": "2025-02-11T11:00:00+08:00",
        "arrived_at": "2025-02-11T11:10:00+08:00"})
    d = client.get("/api/v1/orders/O1/determination",
                   params={"as_of": "2025-02-11T15:00:00+08:00"}).json()
    damage = d["claims"][1]
    assert damage["status"] == "filable"
    assert damage["deadline_local"].startswith("2025-02-14T23:59:59")
    assert damage["missing_evidence"] == []


def test_missing_promise_makes_delay_indeterminate(client):
    make_order(client, promise_by=None)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-20T10:00:00+08:00",
        "arrived_at": "2025-02-20T10:05:00+08:00"})
    d = client.get("/api/v1/orders/O1/determination").json()
    delay = d["claims"][0]
    assert delay["status"] == "indeterminate"
    assert "promise" in delay["missing_evidence"]


def test_archived_result_is_immutable_late_correction_makes_new_generation(client):
    make_order(client)
    # 2/11 签收迟到，2/12 归档：延迟可申请
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    archived = client.post("/api/v1/orders/O1/determination/archive",
                           params={"as_of": "2025-02-12T10:00:00+08:00"}).json()
    assert archived["archived"] is True
    assert archived["generation_no"] == 1
    assert archived["claims"][0]["status"] == "filable"

    # 再次归档被拒绝
    again = client.post("/api/v1/orders/O1/determination/archive")
    assert again.status_code == 409 and again.json()["error"]["code"] == "ALREADY_ARCHIVED"

    # 迟到的更正事件：把签收修正到承诺时限之前（2/9 09:00）
    corr = client.post("/api/v1/orders/O1/events", json={
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-20T08:00:00+08:00",
        "correction_action": "amend", "target_event_id": "s1"})
    body = corr.json()
    assert body["reopened"] is True and body["generation_no"] == 2

    # 新判定：延迟不再成立
    live = client.get("/api/v1/orders/O1/determination").json()
    assert live["generation_no"] == 2
    assert live["claims"][0]["status"] == "not_applicable"
    assert any("第 2 代判定" in b for b in live["basis"])

    # 历史中第 1 代快照原样保留，仍是 filable
    hist = client.get("/api/v1/orders/O1/determinations").json()
    g1 = hist["generations"][0]["snapshot"]
    assert g1["generation_no"] == 1
    assert g1["claims"][0]["status"] == "filable"
    assert hist["generations"][1]["supersedes_generation"] == 1
    assert hist["generations"][0]["archived"] is True


def test_late_plain_event_after_archive_also_reopens(client):
    make_order(client, promise_by="2025-02-10T18:00:00+08:00")
    client.post("/api/v1/orders/O1/determination/archive",
                params={"as_of": "2025-02-12T10:00:00+08:00"})
    r = client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-09T10:00:00+08:00",
        "arrived_at": "2025-02-20T10:00:00+08:00"})
    assert r.json()["generation_no"] == 2


def test_correction_without_target_rejected(client):
    make_order(client)
    r = client.post("/api/v1/orders/O1/events", json={
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-12T10:00:00+08:00",
        "correction_action": "amend"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "CORRECTION_TARGET_REQUIRED"


def test_correction_unknown_target_rejected(client):
    make_order(client)
    r = client.post("/api/v1/orders/O1/events", json={
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-12T10:00:00+08:00",
        "correction_action": "amend", "target_event_id": "nope"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "CORRECTION_TARGET_NOT_FOUND"


def test_unknown_event_type_rejected(client):
    make_order(client)
    r = client.post("/api/v1/orders/O1/events", json={
        "event_id": "x", "event_type": "teleport",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "EVENT_TYPE_INVALID"


def test_national_day_window_skips_golden_week(client):
    # 签收迟到锚点 2025-10-01，10 个工作日 => 跳过国庆+中秋 8 天和两个周末
    make_order(client, promise_by="2025-09-30T18:00:00+08:00")
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-10-01T10:00:00+08:00",
        "arrived_at": "2025-10-01T10:05:00+08:00"})
    d = client.get("/api/v1/orders/O1/determination").json()
    assert d["claims"][0]["deadline_local"].startswith("2025-10-21T23:59:59")
