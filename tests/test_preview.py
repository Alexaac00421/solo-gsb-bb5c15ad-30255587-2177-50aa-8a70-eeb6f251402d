"""无写入预演（dry-run）端到端测试：

- 预演成功：逐项校验结果、模拟判定、判定依据差异，且零写入；
- 预演失败：到达顺序 / 更正目标 / 类型约束违反 => 422 + 首个失败位置，保留现状；
- 已归档订单：预演明确按新一代结果计算，归档快照不变；
- 与正式摄取的一致性：预演说有效的批次，实际摄取后判定相同。
"""
from tests.conftest import make_order


def _signed(event_id="s1", event_time="2025-02-11T10:00:00+08:00",
            arrived_at="2025-02-11T10:05:00+08:00"):
    return {"event_id": event_id, "event_type": "signed",
            "event_time": event_time, "arrived_at": arrived_at}


def _amend_signed(event_id="c1", target="s1",
                  event_time="2025-02-09T09:00:00+08:00",
                  arrived_at="2025-02-21T08:00:00+08:00"):
    return {"event_id": event_id, "event_type": "signed",
            "event_time": event_time, "arrived_at": arrived_at,
            "correction_action": "amend", "target_event_id": target}


def test_preview_success_returns_checks_simulation_and_diff(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    events_before = client.get("/api/v1/orders/O1/events").json()["events"]

    r = client.post("/api/v1/orders/O1/events/preview", json={
        "as_of": "2025-02-20T12:00:00+08:00",
        "events": [_amend_signed()],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True and body["valid"] is True
    assert body["first_failure_index"] is None
    assert body["accepted"] == 1 and body["duplicates"] == []
    assert body["no_writes"]

    # 逐项校验结果
    item = body["items"][0]
    assert item["index"] == 0 and item["event_id"] == "c1"
    assert item["valid"] is True and item["duplicated"] is False
    check_names = [c["check"] for c in item["checks"]]
    assert check_names == ["shape", "duplicate", "arrival_order", "correction_target"]
    assert all(c["passed"] for c in item["checks"])

    # 模拟判定：签收被修正到承诺时限前 -> 延迟不成立
    sim = body["simulation"]
    assert sim["based_on_generation_no"] == 1
    assert sim["current_generation_archived"] is False
    assert sim["would_reopen"] is False
    assert sim["generation_no"] == 1
    det = sim["determination"]
    assert det["claims"][0]["status"] == "not_applicable"
    assert det["overall_status"] == "pending"  # damage/loss 仍待触发
    assert det["eligible_to_file"] is False

    # 判定差异：delay 从 filable 变为 not_applicable，依据有增删
    diff = body["diff"]
    delay_diff = [c for c in diff["claims"] if c["claim_type"] == "delay"][0]
    assert delay_diff["status_before"] == "filable"
    assert delay_diff["status_after"] == "not_applicable"
    assert delay_diff["deadline_local_before"].startswith("2025-02-25T23:59:59")
    assert delay_diff["deadline_local_after"] is None
    assert delay_diff["changed"] is True
    assert diff["overall_status"]["before"] == "filable"
    assert diff["overall_status"]["after"] == "pending"
    # 判定依据差异：顶层新增更正记录；延迟理赔依据有增（不成立）有删（成立+截止）
    assert any("更正事件 c1" in x for x in diff["basis_added"])
    assert any("不构成延迟送达" in x for x in delay_diff["basis_added"])
    assert any("构成延迟送达" in x for x in delay_diff["basis_removed"])
    assert any("截止" in x for x in delay_diff["basis_removed"])

    # 零写入：事件流与代际均未变化
    assert client.get("/api/v1/orders/O1/events").json()["events"] == events_before
    hist = client.get("/api/v1/orders/O1/determinations").json()
    assert len(hist["generations"]) == 1
    assert hist["generations"][0]["archived"] is False


def test_preview_out_of_order_fails_with_first_failure_index(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    r = client.post("/api/v1/orders/O1/events/preview", json={"events": [
        {"event_id": "e2", "event_type": "damage_reported",
         "event_time": "2025-02-12T10:00:00+08:00",
         "arrived_at": "2025-02-12T10:05:00+08:00"},
        {"event_id": "e3", "event_type": "lost_confirmed",
         "event_time": "2025-02-11T10:00:00+08:00",
         "arrived_at": "2025-02-11T08:00:00+08:00"},   # 早于上一已接受事件
    ]})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "EVENT_ORDER_INVALID"
    details = err["details"]
    assert details["dry_run"] is True
    assert details["writes_performed"] is False
    assert details["first_failure_index"] == 1
    items = details["items"]
    assert items[0]["valid"] is True
    assert items[1]["valid"] is False
    assert items[1]["checks"][-1]["check"] == "arrival_order"
    assert items[1]["checks"][-1]["passed"] is False
    # 保留现状：只有最初一条事件
    assert len(client.get("/api/v1/orders/O1/events").json()["events"]) == 1


def test_preview_correction_target_missing(client):
    make_order(client)
    r = client.post("/api/v1/orders/O1/events/preview",
                    json={"events": [_amend_signed(target="nope",
                                                   arrived_at="2025-02-12T10:00:00+08:00")]})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "CORRECTION_TARGET_NOT_FOUND"
    assert err["details"]["first_failure_index"] == 0
    assert err["details"]["items"][0]["checks"][-1]["check"] == "correction_target"


def test_preview_correction_type_mismatch(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    r = client.post("/api/v1/orders/O1/events/preview", json={"events": [{
        "event_id": "c1", "event_type": "damage_reported",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-12T10:00:00+08:00",
        "correction_action": "amend", "target_event_id": "s1"}]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "CORRECTION_TYPE_MISMATCH"


def test_preview_shape_violation_missing_target(client):
    make_order(client)
    r = client.post("/api/v1/orders/O1/events/preview", json={"events": [{
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-12T10:00:00+08:00",
        "correction_action": "amend"}]})  # 缺 target_event_id
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "CORRECTION_TARGET_REQUIRED"
    assert err["details"]["first_failure_index"] == 0
    assert err["details"]["items"][0]["checks"][0]["check"] == "shape"


def test_preview_unknown_event_type_rejected(client):
    make_order(client)
    r = client.post("/api/v1/orders/O1/events/preview", json={"events": [{
        "event_id": "x", "event_type": "teleport",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"}]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "EVENT_TYPE_INVALID"


def test_preview_archived_order_computes_next_generation(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    client.post("/api/v1/orders/O1/determination/archive",
                params={"as_of": "2025-02-12T10:00:00+08:00"})
    hist_before = client.get("/api/v1/orders/O1/determinations").json()

    r = client.post("/api/v1/orders/O1/events/preview", json={
        "as_of": "2025-02-21T12:00:00+08:00",
        "events": [_amend_signed()],
    })
    assert r.status_code == 200
    sim = r.json()["simulation"]
    # 已归档订单：明确以新一代结果计算
    assert sim["current_generation_archived"] is True
    assert sim["would_reopen"] is True
    assert sim["generation_no"] == 2
    assert sim["supersedes_generation"] == 1
    det = sim["determination"]
    assert det["generation_no"] == 2
    assert det["claims"][0]["status"] == "not_applicable"
    assert any("第 2 代判定" in b for b in det["basis"])

    # 归档快照原样保留、未开启新代
    hist_after = client.get("/api/v1/orders/O1/determinations").json()
    assert hist_after == hist_before
    # 当前判定仍是归档快照（filable），不受预演影响
    live = client.get("/api/v1/orders/O1/determination").json()
    assert live["archived"] is True
    assert live["claims"][0]["status"] == "filable"


def test_preview_duplicate_event_marked_and_no_change(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    r = client.post("/api/v1/orders/O1/events/preview",
                    json={"as_of": "2025-02-20T12:00:00+08:00",
                          "events": [_signed()]})
    body = r.json()
    assert body["accepted"] == 0
    assert body["duplicates"] == ["s1"]
    assert body["items"][0]["duplicated"] is True
    # 视图未变：所有理赔均无差异
    assert all(not c["changed"] for c in body["diff"]["claims"])
    assert body["diff"]["basis_added"] == []
    assert body["diff"]["basis_removed"] == []


def test_preview_insert_correction_simulates_claim(client):
    make_order(client)
    r = client.post("/api/v1/orders/O1/events/preview", json={
        "as_of": "2025-02-12T09:00:00+08:00",
        "events": [{
            "event_id": "c1", "event_type": "damage_reported",
            "event_time": "2025-02-11T11:00:00+08:00",
            "arrived_at": "2025-02-12T08:00:00+08:00",
            "correction_action": "insert", "note": "网点补录破损申报"}]})
    assert r.status_code == 200
    damage = r.json()["simulation"]["determination"]["claims"][1]
    assert damage["status"] == "filable"
    assert damage["deadline_local"].startswith("2025-02-14T23:59:59")
    damage_diff = [c for c in r.json()["diff"]["claims"]
                   if c["claim_type"] == "damage"][0]
    assert damage_diff["status_before"] == "pending"
    assert damage_diff["changed"] is True


def test_preview_correction_can_target_event_within_same_batch(client):
    # 批内前序基础事件可作为后续更正的目标（与正式摄取行为一致）
    make_order(client)
    r = client.post("/api/v1/orders/O1/events/preview", json={
        "as_of": "2025-02-20T12:00:00+08:00",
        "events": [
            _signed(),
            _amend_signed(arrived_at="2025-02-12T08:00:00+08:00"),
        ]})
    assert r.status_code == 200
    assert r.json()["accepted"] == 2
    det = r.json()["simulation"]["determination"]
    assert det["claims"][0]["status"] == "not_applicable"


def test_preview_then_real_ingest_produces_same_determination(client):
    # 预演判定有效的批次，正式摄取应成功且判定一致
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    payload = {"as_of": "2025-02-20T12:00:00+08:00", "events": [_amend_signed()]}
    preview = client.post("/api/v1/orders/O1/events/preview", json=payload).json()
    ing = client.post("/api/v1/orders/O1/events/batch",
                      json={"events": payload["events"]})
    assert ing.status_code == 200 and ing.json()["accepted"] == 1
    live = client.get("/api/v1/orders/O1/determination",
                      params={"as_of": "2025-02-20T12:00:00+08:00"}).json()
    assert live["claims"] == preview["simulation"]["determination"]["claims"]
    assert live["basis"] == preview["simulation"]["determination"]["basis"]


def test_preview_archived_then_real_ingest_matches_simulated_generation(client):
    # 已归档订单：预演的新一代结果与实际摄取后的新一代判定一致
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    client.post("/api/v1/orders/O1/determination/archive",
                params={"as_of": "2025-02-12T10:00:00+08:00"})
    payload = {"as_of": "2025-02-21T12:00:00+08:00", "events": [_amend_signed()]}
    preview = client.post("/api/v1/orders/O1/events/preview", json=payload).json()
    client.post("/api/v1/orders/O1/events/batch", json={"events": payload["events"]})
    live = client.get("/api/v1/orders/O1/determination",
                      params={"as_of": "2025-02-21T12:00:00+08:00"}).json()
    assert live["generation_no"] == 2
    assert live["claims"] == preview["simulation"]["determination"]["claims"]


def test_preview_unknown_order_404(client):
    r = client.post("/api/v1/orders/NOPE/events/preview",
                    json={"events": [_signed()]})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ORDER_NOT_FOUND"


def test_preview_naive_as_of_rejected(client):
    make_order(client)
    r = client.post("/api/v1/orders/O1/events/preview", json={
        "as_of": "2025-02-20T12:00:00", "events": [_signed()]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_preview_does_not_block_subsequent_writes(client):
    # 预演失败后，相同订单的正式摄取不受影响（预演未留下任何痕迹）
    make_order(client)
    bad = client.post("/api/v1/orders/O1/events/preview", json={"events": [
        _signed("s1", arrived_at="2025-02-11T10:05:00+08:00"),
        _signed("s0", arrived_at="2025-02-11T09:00:00+08:00"),
    ]})
    assert bad.status_code == 422
    ok = client.post("/api/v1/orders/O1/events", json=_signed("s1"))
    assert ok.status_code == 200 and ok.json()["accepted"] == 1
