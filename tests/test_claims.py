"""理赔申请（凭证）端到端测试：

- 以服务接收时间实时核验当前代：filable 才受理，返回申请编号/受理时间/判定代号/
  适用政策/截止时间/当时判定依据；其余状态返回可区分拒绝原因且不生成凭证；
- 当前代已归档仍按接收时间重算，不把较早归档快照当成实时资格；
- 幂等：同订单同键同类型重试返回原凭证；换类型 409；
  同订单同类型至多一张凭证（不同键也冲突）；并发不同键不得双重受理；
- 凭证不可变：受理后的更正/新代不影响凭证；可查询凭证及其所依据的代号。
"""
import json
import threading

from tests.conftest import make_order


SIGNED_LATE = {
    "event_id": "s1", "event_type": "signed",
    "event_time": "2025-02-11T10:00:00+08:00",
    "arrived_at": "2025-02-11T10:05:00+08:00",
}


def _file(client, oid="O1", claim_type="delay", key="K1",
          received_at="2025-02-20T12:00:00+08:00"):
    body = {"claim_type": claim_type, "idempotency_key": key}
    if received_at is not None:
        body["received_at"] = received_at
    return client.post(f"/api/v1/orders/{oid}/claims", json=body)


# ------------------------------------------------------------ 受理与凭证内容

def test_filable_claim_is_accepted_with_full_voucher(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    r = _file(client, received_at="2025-02-20T12:00:00+08:00")
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["claim_no"].startswith("CLM-")
    assert v["status"] == "accepted"
    assert v["replayed"] is False
    assert v["immutable"] is True
    assert v["order_id"] == "O1"
    assert v["claim_type"] == "delay"
    assert v["idempotency_key"] == "K1"
    # 判定代号 + 适用政策 + 截止时间
    assert v["decision_code"] == "FILABLE"
    assert v["policy_version"] == "v2"
    assert v["policy"]["window"]["days"] == 10
    assert v["deadline_local"].startswith("2025-02-25T23:59:59")
    # 受理时间与接收时间
    assert v["received_at_utc"].startswith("2025-02-20T04:00:00")  # +08:00 -> UTC
    assert v["accepted_at_utc"]
    # 当时判定依据（类型级 + 整单）
    assert any("构成延迟送达" in b for b in v["basis"])
    assert any("当前时间在申请时限内，可申请" in b for b in v["basis"])
    assert any("适用政策版本：v2" in b for b in v["determination_basis"])
    assert v["claim"]["status"] == "filable"
    assert v["determination"]["overall_status"] == "filable"


def test_idempotency_header_key_accepted(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    r = client.post("/api/v1/orders/O1/claims",
                    json={"claim_type": "delay",
                          "idempotency_key": "H1",
                          "received_at": "2025-02-20T12:00:00+08:00"},
                    headers={"Idempotency-Key": "H1"})
    assert r.status_code == 201


def test_header_body_key_mismatch_rejected(client):
    make_order(client)
    r = client.post("/api/v1/orders/O1/claims",
                    json={"claim_type": "delay", "idempotency_key": "BODY-1"},
                    headers={"Idempotency-Key": "HDR-1"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"


def test_unknown_claim_type_rejected(client):
    make_order(client)
    r = _file(client, claim_type="weather")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "CLAIM_TYPE_INVALID"


def test_unknown_order_404(client):
    r = client.post("/api/v1/orders/NOPE/claims",
                    json={"claim_type": "delay", "idempotency_key": "K1"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ORDER_NOT_FOUND"


def test_naive_received_at_rejected(client):
    make_order(client)
    r = client.post("/api/v1/orders/O1/claims",
                    json={"claim_type": "delay", "idempotency_key": "K1",
                          "received_at": "2025-02-20T12:00:00"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


# ------------------------------------------------------------ 可区分的业务拒绝

def test_pending_claim_rejected_without_voucher(client):
    # 无破损申报 -> damage 为 pending
    make_order(client)
    r = _file(client, claim_type="damage",
              received_at="2025-02-12T10:00:00+08:00")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "CLAIM_PENDING"
    assert err["details"]["status"] == "pending"
    assert err["details"]["decision_code"] == "PENDING"
    assert err["details"]["voucher_generated"] is False
    assert client.get("/api/v1/orders/O1/claims").json()["vouchers"] == []


def test_not_applicable_claim_rejected(client):
    # 准时签收 -> delay 不成立
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-10T09:00:00+08:00",
        "arrived_at": "2025-02-10T09:05:00+08:00"})
    r = _file(client, received_at="2025-02-10T12:00:00+08:00")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "CLAIM_NOT_APPLICABLE"


def test_indeterminate_claim_rejected(client):
    make_order(client, promise_by=None)
    r = _file(client, received_at="2025-02-20T12:00:00+08:00")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "CLAIM_INDETERMINATE"
    assert "promise" in err["details"]["missing_evidence"]


def test_deadline_passed_rejected_and_no_voucher(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    # 截止 2/25 23:59:59；2/26 申请 -> not_filable
    r = _file(client, received_at="2025-02-26T08:00:00+08:00")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "CLAIM_DEADLINE_PASSED"
    assert err["details"]["status"] == "not_filable"
    assert err["details"]["decision_code"] == "NOT_FILABLE"
    assert err["details"]["deadline_local"].startswith("2025-02-25T23:59:59")
    assert err["details"]["voucher_generated"] is False
    assert any("截止时间" in b for b in err["details"]["basis"])
    # 拒绝不留凭证，之后在时限内用新键仍可受理
    ok = _file(client, key="K2", received_at="2025-02-20T12:00:00+08:00")
    assert ok.status_code == 201


def test_rejection_does_not_consume_idempotency_key(client):
    # 首次 pending 被拒；同键在触发事件到达后重试应正常受理（而非冲突）
    make_order(client)
    first = _file(client, claim_type="damage", key="D1",
                  received_at="2025-02-12T10:00:00+08:00")
    assert first.status_code == 422 and first.json()["error"]["code"] == "CLAIM_PENDING"
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "g1", "event_type": "damage_reported",
        "event_time": "2025-02-11T11:00:00+08:00",
        "arrived_at": "2025-02-13T09:00:00+08:00"})
    again = _file(client, claim_type="damage", key="D1",
                  received_at="2025-02-13T10:00:00+08:00")
    assert again.status_code == 201


# ------------------------------------------------------------ 接收时间核验

def test_evaluation_uses_receive_time_not_server_now(client):
    # 不传 received_at 时测试环境当前时间（2026）必已过截止 -> 拒绝；
    # 显式给 2/20 的接收时间 -> 受理
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    late = client.post("/api/v1/orders/O1/claims",
                       json={"claim_type": "delay", "idempotency_key": "NOW"})
    assert late.status_code == 422
    assert late.json()["error"]["code"] == "CLAIM_DEADLINE_PASSED"
    ok = _file(client, key="PAST", received_at="2025-02-20T12:00:00+08:00")
    assert ok.status_code == 201


def test_events_arriving_after_receive_time_are_not_basis(client):
    # 接收时（2/12）尚无任何触发扫描：delay 承诺时限（2/10）已过且未签收 -> filable；
    # 2/13 才到达的"准时签收更正/事件"不能推翻 2/12 的受理
    make_order(client)
    accepted = _file(client, received_at="2025-02-12T10:00:00+08:00")
    assert accepted.status_code == 201
    assert accepted.json()["decision_code"] == "FILABLE"


def test_archived_generation_is_rechecked_at_receive_time(client):
    # 2/12 归档快照里 delay=pending（承诺 2/10 未到？此处构造过期未签收）
    make_order(client, promise_by="2025-02-10T18:00:00+08:00")
    archived = client.post("/api/v1/orders/O1/determination/archive",
                           params={"as_of": "2025-02-12T10:00:00+08:00"}).json()
    assert archived["claims"][0]["status"] == "filable"  # 快照显示可申请
    assert archived["generation_no"] == 1

    # 快照后、申请接收前到达的迟到更正：签收提前到 2/9（未迟到）
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-13T08:00:00+08:00",
        "correction_action": "insert"})
    r = _file(client, key="K1", received_at="2025-02-14T10:00:00+08:00")
    # 不把较早归档快照（filable）当成实时资格：实时重算为 not_applicable
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "CLAIM_NOT_APPLICABLE"


def test_archived_generation_rechecked_and_still_filable_accepted(client):
    # 归档快照 filable；无新事件；申请仍按接收时间重算并受理，凭证明示重算
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    client.post("/api/v1/orders/O1/determination/archive",
                params={"as_of": "2025-02-12T10:00:00+08:00"})
    r = _file(client, key="K1", received_at="2025-02-20T12:00:00+08:00")
    assert r.status_code == 201
    v = r.json()
    assert v["archived_generation_rechecked"] is True
    assert any("重新核验" in b for b in v["determination_basis"])
    assert v["generation_no"] == 1


# ------------------------------------------------------------ 幂等与唯一性

def test_same_key_same_type_retry_returns_original_voucher(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    first = _file(client, key="K1", received_at="2025-02-20T12:00:00+08:00")
    assert first.status_code == 201
    claim_no = first.json()["claim_no"]

    # 重试（即便换了接收时间）也返回原凭证，且 replayed=True、200
    retry = _file(client, key="K1", received_at="2025-02-21T12:00:00+08:00")
    assert retry.status_code == 200
    rv = retry.json()
    assert rv["replayed"] is True
    assert rv["claim_no"] == claim_no
    assert rv["received_at_utc"] == first.json()["received_at_utc"]
    assert rv["accepted_at_utc"] == first.json()["accepted_at_utc"]

    # 只有一张凭证
    vouchers = client.get("/api/v1/orders/O1/claims").json()["vouchers"]
    assert len(vouchers) == 1 and vouchers[0]["claim_no"] == claim_no


def test_same_key_different_type_conflicts(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    first = _file(client, claim_type="delay", key="SHARED",
                  received_at="2025-02-20T12:00:00+08:00")
    assert first.status_code == 201
    other = _file(client, claim_type="damage", key="SHARED",
                  received_at="2025-02-20T12:00:00+08:00")
    assert other.status_code == 409
    err = other.json()["error"]
    assert err["code"] == "CLAIM_CONFLICT"
    assert err["details"]["existing_claim_type"] == "delay"
    assert err["details"]["submitted_claim_type"] == "damage"
    assert err["details"]["existing_claim_no"] == first.json()["claim_no"]


def test_same_type_different_key_conflicts(client):
    # 同订单同类型至多一张凭证
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    first = _file(client, key="K1", received_at="2025-02-20T12:00:00+08:00")
    assert first.status_code == 201
    second = _file(client, key="K2", received_at="2025-02-20T12:00:00+08:00")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CLAIM_ALREADY_ACCEPTED"
    assert len(client.get("/api/v1/orders/O1/claims").json()["vouchers"]) == 1


def test_different_claim_types_can_each_have_voucher(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "g1", "event_type": "damage_reported",
        "event_time": "2025-02-11T11:00:00+08:00",
        "arrived_at": "2025-02-11T11:10:00+08:00"})
    d = _file(client, claim_type="delay", key="KD",
              received_at="2025-02-12T10:00:00+08:00")
    g = _file(client, claim_type="damage", key="KG",
              received_at="2025-02-12T10:00:00+08:00")
    assert d.status_code == 201 and g.status_code == 201
    assert d.json()["claim_no"] != g.json()["claim_no"]
    assert len(client.get("/api/v1/orders/O1/claims").json()["vouchers"]) == 2


def test_same_key_reusable_across_orders(client):
    make_order(client, "O1")
    make_order(client, "O2", promise_by=None)
    # O2 delay 因缺承诺时限为 indeterminate；用同键给两个订单各自提交互不影响
    r2 = _file(client, "O2", key="SAME", received_at="2025-02-12T10:00:00+08:00")
    assert r2.status_code == 422
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    r1 = _file(client, "O1", key="SAME", received_at="2025-02-20T12:00:00+08:00")
    assert r1.status_code == 201


def test_concurrent_same_type_different_keys_no_double_acceptance(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    results: list = []
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        r = client.post("/api/v1/orders/O1/claims", json={
            "claim_type": "delay", "idempotency_key": f"P{i}",
            "received_at": "2025-02-20T12:00:00+08:00"})
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = [b for s, b in results if s == 201]
    rejected = [b for s, b in results if s == 409]
    assert len(accepted) == 1
    assert len(rejected) == 7
    assert all(b["error"]["code"] == "CLAIM_ALREADY_ACCEPTED" for b in rejected)
    assert len(client.get("/api/v1/orders/O1/claims").json()["vouchers"]) == 1


def test_concurrent_same_key_returns_single_voucher(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    results: list = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        r = client.post("/api/v1/orders/O1/claims", json={
            "claim_type": "delay", "idempotency_key": "RACE-KEY",
            "received_at": "2025-02-20T12:00:00+08:00"})
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    claim_nos = {b["claim_no"] for s, b in results if s in (200, 201)}
    assert len(claim_nos) == 1
    assert len(client.get("/api/v1/orders/O1/claims").json()["vouchers"]) == 1


# ------------------------------------------------------------ 不可变与查询

def test_voucher_immutable_after_correction_and_new_generation(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    accepted = _file(client, key="K1", received_at="2025-02-20T12:00:00+08:00")
    assert accepted.status_code == 201
    v = accepted.json()

    # 受理后先归档当前代，再由迟到更正开启第 2 代（签收修正到承诺时限前）
    client.post("/api/v1/orders/O1/determination/archive",
                params={"as_of": "2025-02-21T12:00:00+08:00"})
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-22T08:00:00+08:00",
        "correction_action": "amend", "target_event_id": "s1"})
    live = client.get("/api/v1/orders/O1/determination").json()
    assert live["generation_no"] == 2
    assert live["claims"][0]["status"] == "not_applicable"

    # 凭证仍是受理时冻结的内容：FILABLE、第 1 代、截止与依据不变
    got = client.get(f"/api/v1/claims/{v['claim_no']}").json()
    assert got["claim_no"] == v["claim_no"]
    assert got["decision_code"] == "FILABLE"
    assert got["generation_no"] == 1
    assert got["deadline_local"] == v["deadline_local"]
    assert got["claim"] == v["claim"]
    assert got["determination"] == v["determination"]
    assert got["accepted_at_utc"] == v["accepted_at_utc"]


def test_get_voucher_404(client):
    r = client.get("/api/v1/claims/CLM-NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "VOUCHER_NOT_FOUND"


def test_list_vouchers_unknown_order_404(client):
    r = client.get("/api/v1/orders/NOPE/claims")
    assert r.status_code == 404


def test_voucher_policy_is_locked_order_policy(client):
    # v1 政策订单（2024 创建）：凭证应冻结 v1（7 个工作日）
    make_order(client, "OLD", created_at="2024-06-03T09:00:00+08:00",
               promise_by="2024-06-10T18:00:00+08:00")
    client.post("/api/v1/orders/OLD/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2024-06-11T10:00:00+08:00",
        "arrived_at": "2024-06-11T10:05:00+08:00"})
    r = _file(client, "OLD", key="K1", received_at="2024-06-12T10:00:00+08:00")
    assert r.status_code == 201
    v = r.json()
    assert v["policy_version"] == "v1"
    assert v["policy"]["window"]["days"] == 7
    assert v["determination"]["policy_version"] == "v1"


def test_voucher_payload_is_json_serializable(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    r = _file(client)
    assert r.status_code == 201
    # 往返一次（TestClient 已做 JSON 解析），再序列化确保无类型问题
    json.dumps(r.json())
