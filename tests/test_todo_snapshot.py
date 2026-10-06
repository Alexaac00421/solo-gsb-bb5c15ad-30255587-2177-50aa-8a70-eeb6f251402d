"""跨订单理赔待办快照端到端测试：

- 首请求按带偏移核对时刻冻结完整结果：只纳入不晚于该时刻创建的订单、
  到达的扫描和受理的凭证（边界值取等号也纳入）；
- 按订单锁定政策/时区/日历重算三类资格，归档判定不替代该时点重算；
- 条目字段齐全；已有凭证的类型一律 can_apply_again=False，其余保持各自判定；
- 排序：订单号 → 理赔类型（delay→damage→loss）；游标翻页期间新订单/扫描/凭证
  不改变既有条目、总数与顺序；游标自快照创建起 30 分钟过期；
- 缺失偏移的时刻、越界页大小、无效或过期游标均明确报错。
"""
from datetime import datetime, timezone

from app import db
from tests.conftest import make_order

AS_OF = "2025-02-20T12:00:00+08:00"

SIGNED_LATE = {
    "event_id": "s1", "event_type": "signed",
    "event_time": "2025-02-11T10:00:00+08:00",
    "arrived_at": "2025-02-11T10:05:00+08:00",
}


def _create(client, as_of=AS_OF, page_size=10):
    return client.post("/api/v1/claims/todo-snapshots",
                       json={"as_of": as_of, "page_size": page_size})


def _page(client, cursor):
    return client.get("/api/v1/claims/todo-snapshots/page", params={"cursor": cursor})


def _pairs(body):
    return [(i["order_id"], i["claim_type"]) for i in body["items"]]


def _file_claim(client, oid, claim_type="delay", key="K1",
                received_at="2025-02-15T12:00:00+08:00"):
    r = client.post(f"/api/v1/orders/{oid}/claims",
                    json={"claim_type": claim_type, "idempotency_key": key,
                          "received_at": received_at})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------ 首请求：完整结果、排序、字段

def test_empty_database_returns_empty_first_page(client):
    r = _create(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["total"] == 0
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["page"] == 1 and body["total_pages"] == 1
    assert body["frozen"] is True


def test_first_page_sorted_by_order_then_claim_type_with_full_fields(client):
    # 创建顺序与订单号排序相反，验证按订单号排序而非创建先后
    make_order(client, "O-B", created_at="2025-02-06T09:00:00+08:00",
               promise_by="2025-02-12T18:00:00+08:00")
    make_order(client, "O-A", created_at="2025-02-05T09:00:00+08:00",
               promise_by="2025-02-10T18:00:00+08:00")
    r = _create(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["total"] == 6          # 2 个订单 × 3 类理赔
    assert body["page_size"] == 10
    assert _pairs(body) == [
        ("O-A", "delay"), ("O-A", "damage"), ("O-A", "loss"),
        ("O-B", "delay"), ("O-B", "damage"), ("O-B", "loss"),
    ]
    item = body["items"][0]
    for key in ("order_id", "claim_type", "claim_name", "status",
                "deadline_local", "deadline_utc", "missing_evidence",
                "voucher_claim_no", "can_apply_again"):
        assert key in item
    # O-A 承诺 2/10 已过且无签收 -> 延迟 filable，截止 10 个工作日后
    assert item["status"] == "filable"
    assert item["deadline_local"] == "2025-02-24T23:59:59+08:00"
    assert item["deadline_utc"] == "2025-02-24T15:59:59+00:00"
    assert item["missing_evidence"] == ["signed"]
    assert item["voucher_claim_no"] is None
    assert item["can_apply_again"] is True
    # 无触发事件的类型保持各自判定：pending 不可申请
    damage = body["items"][1]
    assert damage["status"] == "pending"
    assert damage["deadline_local"] is None and damage["deadline_utc"] is None
    assert damage["missing_evidence"] == ["damage_reported"]
    assert damage["can_apply_again"] is False


def test_response_meta_and_cursor_ttl(client):
    make_order(client)
    body = _create(client, page_size=2).json()
    assert body["as_of_utc"] == "2025-02-20T04:00:00+00:00"   # 核对时刻归一化为 UTC
    assert body["cursor_ttl_seconds"] == 1800
    created = datetime.fromisoformat(body["created_at_utc"])
    expires = datetime.fromisoformat(body["expires_at_utc"])
    assert (expires - created).total_seconds() == 1800        # 游标 30 分钟过期
    assert body["total"] == 3 and body["total_pages"] == 2
    assert body["next_cursor"]


def test_indeterminate_delay_when_promise_missing(client):
    make_order(client, promise_by=None)
    body = _create(client).json()
    delay = body["items"][0]
    assert delay["status"] == "indeterminate"
    assert delay["missing_evidence"] == ["promise", "signed"]
    assert delay["deadline_local"] is None
    assert delay["can_apply_again"] is False


# ------------------------------------------------------------ 纳入范围：不晚于核对时刻

def test_orders_created_after_as_of_excluded(client):
    make_order(client, "O1", created_at="2025-02-05T09:00:00+08:00")
    make_order(client, "O2", created_at="2025-03-01T09:00:00+08:00",
               promise_by="2025-03-10T18:00:00+08:00")
    body = _create(client).json()
    assert body["total"] == 3
    assert {i["order_id"] for i in body["items"]} == {"O1"}


def test_events_arrived_after_as_of_excluded(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "d1", "event_type": "damage_reported",
        "event_time": "2025-02-19T10:00:00+08:00",
        "arrived_at": "2025-02-21T09:00:00+08:00",   # 核对时刻之后到达
    })
    before = _create(client, as_of="2025-02-20T12:00:00+08:00").json()
    damage = next(i for i in before["items"] if i["claim_type"] == "damage")
    assert damage["status"] == "pending"           # 事件尚未到达，不计入

    after = _create(client, as_of="2025-02-22T12:00:00+08:00").json()
    damage = next(i for i in after["items"] if i["claim_type"] == "damage")
    assert damage["status"] == "filable"
    assert damage["deadline_local"] == "2025-02-24T23:59:59+08:00"
    assert damage["can_apply_again"] is True


def test_boundary_exactly_at_as_of_is_included(client):
    # 订单创建、扫描到达、凭证接收恰好等于核对时刻（不晚于）=> 纳入
    make_order(client, "O-EDGE", created_at=AS_OF,
               promise_by="2025-02-25T18:00:00+08:00")
    client.post("/api/v1/orders/O-EDGE/events", json={
        "event_id": "e1", "event_type": "damage_reported",
        "event_time": "2025-02-20T10:00:00+08:00", "arrived_at": AS_OF,
    })
    _file_claim(client, "O-EDGE", claim_type="damage", key="K-EDGE",
                received_at=AS_OF)
    body = _create(client, as_of=AS_OF).json()
    assert body["total"] == 3
    damage = next(i for i in body["items"] if i["claim_type"] == "damage")
    assert damage["status"] == "filable"                    # 到达事件已纳入
    assert damage["voucher_claim_no"] is not None           # 受理凭证已纳入
    assert damage["can_apply_again"] is False


# ------------------------------------------------------------ 凭证与能否再次申请

def test_existing_voucher_blocks_reapply_others_keep_own_status(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "d1", "event_type": "damage_reported",
        "event_time": "2025-02-18T10:00:00+08:00",
        "arrived_at": "2025-02-18T11:00:00+08:00",
    })
    voucher = _file_claim(client, "O1", claim_type="delay", key="K1",
                          received_at="2025-02-15T12:00:00+08:00")
    body = _create(client).json()
    by_type = {i["claim_type"]: i for i in body["items"]}

    delay = by_type["delay"]
    assert delay["status"] == "filable"                     # 资格仍按时点重算
    assert delay["deadline_local"] == "2025-02-25T23:59:59+08:00"
    assert delay["deadline_utc"] == "2025-02-25T15:59:59+00:00"
    assert delay["voucher_claim_no"] == voucher["claim_no"]
    assert delay["can_apply_again"] is False                # 已有凭证一律不可再申请

    damage = by_type["damage"]
    assert damage["status"] == "filable"
    assert damage["voucher_claim_no"] is None
    assert damage["can_apply_again"] is True                # 无凭证保持各自判定

    loss = by_type["loss"]
    assert loss["status"] == "pending"
    assert loss["can_apply_again"] is False


def test_voucher_received_after_as_of_not_counted(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    _file_claim(client, "O1", claim_type="delay", key="K1",
                received_at="2025-02-25T10:00:00+08:00")    # 核对时刻之后受理
    body = _create(client, as_of="2025-02-20T12:00:00+08:00").json()
    delay = next(i for i in body["items"] if i["claim_type"] == "delay")
    assert delay["voucher_claim_no"] is None
    assert delay["can_apply_again"] is True


def test_archived_determination_never_substitutes_recompute(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    # 归档第 1 代（filable），随后迟到更正开启第 2 代：签收提前、延迟不再成立
    client.post("/api/v1/orders/O1/determination/archive",
                params={"as_of": "2025-02-20T12:00:00+08:00"})
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-21T08:00:00+08:00",
        "correction_action": "amend", "target_event_id": "s1",
    })
    body = _create(client, as_of="2025-02-22T12:00:00+08:00").json()
    delay = next(i for i in body["items"] if i["claim_type"] == "delay")
    # 若采用归档快照会得到 filable；重算结果应为 not_applicable
    assert delay["status"] == "not_applicable"
    assert delay["can_apply_again"] is False


# ------------------------------------------------------------ 游标翻页与快照一致性

def test_pagination_covers_all_items_in_stable_order(client):
    for oid in ("O1", "O2", "O3"):
        make_order(client, oid)
    first = _create(client, page_size=4).json()
    assert first["total"] == 9 and first["total_pages"] == 3
    assert len(first["items"]) == 4 and first["next_cursor"]

    seen = list(_pairs(first))
    cursor = first["next_cursor"]
    pages = 1
    while cursor:
        body = _page(client, cursor).json()
        assert body["total"] == 9                           # 每页都返回总数
        assert body["snapshot_id"] == first["snapshot_id"]
        seen.extend(_pairs(body))
        cursor = body["next_cursor"]
        pages += 1
    assert pages == 3
    assert seen == [(o, t) for o in ("O1", "O2", "O3")
                    for t in ("delay", "damage", "loss")]


def test_new_data_during_pagination_does_not_change_snapshot(client):
    for oid in ("O1", "O2", "O3"):
        make_order(client, oid)
    first = _create(client, page_size=4).json()
    frozen_page1 = first["items"]

    # 快照后：新订单（按订单号会排到最前）、新扫描、新凭证
    make_order(client, "O0", created_at="2025-02-04T09:00:00+08:00")
    client.post("/api/v1/orders/O2/events", json=SIGNED_LATE)
    _file_claim(client, "O1", claim_type="delay", key="K1",
                received_at="2025-02-15T12:00:00+08:00")

    second = _page(client, first["next_cursor"]).json()
    assert second["total"] == 9                             # 总数不变
    assert _pairs(second) == [("O2", "damage"), ("O2", "loss"),
                              ("O3", "delay"), ("O3", "damage")]
    assert all(i["order_id"] != "O0" for i in second["items"])  # 新订单不出现
    third = _page(client, second["next_cursor"]).json()
    assert _pairs(third) == [("O3", "loss")]
    assert third["next_cursor"] is None

    # 重新发起首请求则看到最新数据（含 O0、O2 的延迟构成、O1 的凭证）
    fresh = _create(client, page_size=100).json()
    assert fresh["total"] == 12
    assert _pairs(fresh)[0] == ("O0", "delay")
    o2_delay = next(i for i in fresh["items"]
                    if i["order_id"] == "O2" and i["claim_type"] == "delay")
    assert o2_delay["status"] == "filable"
    o1_delay = next(i for i in fresh["items"]
                    if i["order_id"] == "O1" and i["claim_type"] == "delay")
    assert o1_delay["voucher_claim_no"] is not None
    assert o1_delay["can_apply_again"] is False
    # 首页内容确实被后续数据改变，证明上一快照的稳定性来自冻结而非巧合
    assert fresh["items"][:4] != frozen_page1 or fresh["total"] != 9


# ------------------------------------------------------------ 参数与游标错误

def test_naive_as_of_rejected(client):
    r = _create(client, as_of="2025-02-20T12:00:00")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_missing_as_of_or_page_size_rejected(client):
    for payload in ({}, {"page_size": 10}, {"as_of": AS_OF}):
        r = client.post("/api/v1/claims/todo-snapshots", json=payload)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_page_size_out_of_range_rejected(client):
    for size in (0, -1, 101, 1000):
        r = _create(client, page_size=size)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_REQUEST"
    assert _create(client, page_size=1).status_code == 201
    assert _create(client, page_size=100).status_code == 201


def test_missing_or_malformed_cursor_rejected(client):
    r = client.get("/api/v1/claims/todo-snapshots/page")     # 缺 cursor 参数
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"

    for bad in ("", "garbage", "aaa.bbb.ccc", "####.####"):
        r = _page(client, bad)
        assert r.status_code == 422, bad
        assert r.json()["error"]["code"] == "SNAPSHOT_CURSOR_INVALID"


def test_tampered_cursor_rejected(client):
    make_order(client)
    first = _create(client, page_size=1).json()
    cursor = first["next_cursor"]
    payload, sig = cursor.rsplit(".", 1)
    tampered_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    r = _page(client, f"{payload}.{tampered_sig}")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SNAPSHOT_CURSOR_INVALID"


def test_cursor_for_deleted_snapshot_rejected(client):
    make_order(client)
    first = _create(client, page_size=1).json()
    db.execute("DELETE FROM todo_snapshots WHERE snapshot_id = ?",
               (first["snapshot_id"],))
    r = _page(client, first["next_cursor"])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SNAPSHOT_CURSOR_INVALID"


def test_expired_cursor_rejected(client):
    make_order(client)
    first = _create(client, page_size=1).json()
    assert first["next_cursor"]
    # 把快照创建时间拨到 31 分钟前（游标 30 分钟过期）
    db.execute(
        "UPDATE todo_snapshots SET created_at = ?, expires_at = ? WHERE snapshot_id = ?",
        ("2025-01-01T00:00:00+00:00", "2025-01-01T00:30:00+00:00",
         first["snapshot_id"]),
    )
    r = _page(client, first["next_cursor"])
    assert r.status_code == 410
    err = r.json()["error"]
    assert err["code"] == "SNAPSHOT_CURSOR_EXPIRED"
    assert err["details"]["snapshot_id"] == first["snapshot_id"]
    assert err["details"]["cursor_ttl_seconds"] == 1800


def test_cursor_replay_same_page_is_consistent(client):
    make_order(client)
    first = _create(client, page_size=1).json()
    again = _page(client, first["next_cursor"]).json()
    again2 = _page(client, first["next_cursor"]).json()
    assert again == again2                                   # 游标可重复取用
    assert again["page"] == 2 and again["total"] == 3
