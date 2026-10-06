"""跨订单理赔待办快照端到端测试：

- 首请求按带偏移的核对时刻冻结完整结果：只纳入不晚于该时刻创建的订单、
  到达的扫描和受理的凭证；按订单锁定政策/时区/日历重算（归档不替代重算）；
- 每条含订单号、理赔类型、资格状态、当地与 UTC 截止、缺失证据、
  已有凭证编号与能否再次申请（已有凭证的类型一律不可再次申请）；
- 按订单号、理赔类型（delay -> damage -> loss）排序，每页返回总数/条目/下一游标；
- 游标翻页期间新订单/扫描/凭证不改变既有条目、总数与顺序；
- 游标自快照创建起 30 分钟过期；缺失偏移的时刻、越界页大小、
  无效或过期游标均明确报错。
"""
from datetime import datetime, timedelta, timezone

from app import db
from tests.conftest import make_order

SIGNED_LATE = {
    "event_id": "s1", "event_type": "signed",
    "event_time": "2025-02-11T10:00:00+08:00",
    "arrived_at": "2025-02-11T10:05:00+08:00",
}

AS_OF = "2025-02-20T12:00:00+08:00"


def _create_snapshot(client, as_of=AS_OF, page_size=100):
    return client.post("/api/v1/claim-todos",
                       json={"as_of": as_of, "page_size": page_size})


def _page(client, cursor):
    return client.get("/api/v1/claim-todos", params={"cursor": cursor})


def _entry(payload, order_id, claim_type):
    return next(e for e in payload["entries"]
                if e["order_id"] == order_id and e["claim_type"] == claim_type)


# ------------------------------------------------------------ 首请求：内容、排序与分页

def test_snapshot_entries_sorted_and_paginated(client):
    make_order(client, "O-B")
    make_order(client, "O-A")
    r = _create_snapshot(client, page_size=2)
    assert r.status_code == 201, r.text
    p1 = r.json()
    assert p1["snapshot_id"].startswith("TS-")
    assert p1["as_of_utc"] == "2025-02-20T04:00:00+00:00"
    assert p1["total"] == 6                      # 2 个订单 × 3 类
    assert p1["page_size"] == 2
    assert p1["page"] == 1
    assert p1["page_count"] == 3
    assert p1["cursor_ttl_seconds"] == 1800
    assert p1["created_at_utc"] and p1["expires_at_utc"]
    assert p1["next_cursor"]

    seen, cursor, pages = list(p1["entries"]), p1["next_cursor"], 1
    while cursor:
        r = _page(client, cursor)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["total"] == 6             # 每页都返回同一总数
        assert payload["snapshot_id"] == p1["snapshot_id"]
        seen.extend(payload["entries"])
        cursor = payload["next_cursor"]
        pages += 1
    assert pages == 3
    # 按订单号、理赔类型（delay -> damage -> loss）排序
    assert [(e["order_id"], e["claim_type"]) for e in seen] == [
        ("O-A", "delay"), ("O-A", "damage"), ("O-A", "loss"),
        ("O-B", "delay"), ("O-B", "damage"), ("O-B", "loss"),
    ]


def test_snapshot_entry_fields(client):
    make_order(client)                           # O1，承诺 2/10 18:00
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    r = _create_snapshot(client, as_of="2025-02-14T12:00:00+08:00")
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["total"] == 3

    delay = _entry(payload, "O1", "delay")
    assert delay["claim_name"] == "延迟送达"
    assert delay["status"] == "filable"
    assert delay["deadline_local"] == "2025-02-25T23:59:59+08:00"
    assert delay["deadline_utc"] == "2025-02-25T15:59:59+00:00"
    assert delay["missing_evidence"] == []
    assert delay["voucher_nos"] == []
    assert delay["can_apply_again"] is True

    damage = _entry(payload, "O1", "damage")
    assert damage["status"] == "pending"
    assert damage["deadline_local"] is None and damage["deadline_utc"] is None
    assert damage["missing_evidence"] == ["damage_reported"]
    assert damage["can_apply_again"] is False


def test_snapshot_as_of_filters_orders_and_events(client):
    make_order(client, "O1")                                   # 创建于 2/5
    make_order(client, "O2", created_at="2025-02-15T09:00:00+08:00")
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)  # 2/11 10:05 到达

    # 核对时刻早于 O2 创建、也早于签收扫描到达：只纳入 O1，签收事件不可见
    p1 = _create_snapshot(client, as_of="2025-02-11T10:00:00+08:00").json()
    assert p1["total"] == 3
    delay = _entry(p1, "O1", "delay")
    assert delay["status"] == "filable"
    # 承诺时限 2/10 已过且无签收 -> 以承诺时限为锚点，截止 2/24
    assert delay["deadline_local"].startswith("2025-02-24T23:59:59")

    # 签收扫描到达后：锚点变为实际签收 2/11，截止 2/25
    p2 = _create_snapshot(client, as_of="2025-02-12T00:00:00+08:00").json()
    assert _entry(p2, "O1", "delay")["deadline_local"].startswith("2025-02-25")

    # O2 创建之后：两单共 6 条
    p3 = _create_snapshot(client, as_of="2025-02-16T00:00:00+08:00").json()
    assert p3["total"] == 6


def test_snapshot_recomputes_ignoring_archived_snapshot(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    # 归档第 1 代（此时 delay=filable）
    r = client.post("/api/v1/orders/O1/determination/archive",
                    params={"as_of": AS_OF})
    assert r.status_code == 200, r.text
    # 迟到更正：签收时间修正为 2/9（未迟到）-> 第 2 代 delay=not_applicable
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-21T08:00:00+08:00",
        "correction_action": "amend", "target_event_id": "s1",
    })
    payload = _create_snapshot(client, as_of="2025-02-22T12:00:00+08:00").json()
    # 归档判定（filable）不能替代该时点重算：快照必须反映 not_applicable
    assert _entry(payload, "O1", "delay")["status"] == "not_applicable"


# ------------------------------------------------------------ 已有凭证与再次申请

def test_snapshot_voucher_visibility_and_reapply_rule(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    r = client.post("/api/v1/orders/O1/claims", json={
        "claim_type": "delay", "idempotency_key": "K1",
        "received_at": "2025-02-13T10:00:00+08:00",
    })
    assert r.status_code == 201, r.text
    claim_no = r.json()["claim_no"]              # accepted_at 为服务当前真实时间

    # 核对时刻早于凭证受理时间：凭证不纳入
    before = _create_snapshot(client, as_of="2025-02-20T12:00:00+08:00").json()
    delay = _entry(before, "O1", "delay")
    assert delay["voucher_nos"] == []
    assert delay["can_apply_again"] is True      # filable 且无凭证 -> 可申请

    # 核对时刻晚于凭证受理时间：已有凭证的类型一律不可再次申请
    after = _create_snapshot(client, as_of="2027-01-01T00:00:00+08:00").json()
    delay = _entry(after, "O1", "delay")
    assert delay["voucher_nos"] == [claim_no]
    assert delay["can_apply_again"] is False
    # 其他类型保持各自判定：无触发事件仍 pending，不可申请
    assert _entry(after, "O1", "damage")["status"] == "pending"
    assert _entry(after, "O1", "damage")["can_apply_again"] is False


def test_reapply_rule_and_paging_freeze_with_live_data(client):
    """以当前真实时间构造：翻页期间新订单/扫描/凭证均不改变既有快照。"""
    now = datetime.now(timezone.utc)
    created = (now - timedelta(days=3)).isoformat()
    promise = (now - timedelta(days=2)).isoformat()
    signed = {
        "event_id": "dyn-s1", "event_type": "signed",
        "event_time": (now - timedelta(days=1)).isoformat(),
        "arrived_at": (now - timedelta(days=1)).isoformat(),
    }
    make_order(client, "DYN-1", created_at=created, promise_by=promise)
    client.post("/api/v1/orders/DYN-1/events", json=signed)
    as_of = (now + timedelta(hours=1)).isoformat()

    r = _create_snapshot(client, as_of=as_of, page_size=1)
    assert r.status_code == 201, r.text
    p1 = r.json()
    assert p1["total"] == 3 and p1["page_count"] == 3
    delay = p1["entries"][0]
    assert (delay["order_id"], delay["claim_type"]) == ("DYN-1", "delay")
    assert delay["status"] == "filable" and delay["can_apply_again"] is True

    # 快照之后到达新数据（均不晚于核对时刻）：新订单、新扫描、新凭证
    make_order(client, "DYN-2", created_at=now.isoformat(), promise_by=promise)
    client.post("/api/v1/orders/DYN-1/events", json={
        "event_id": "dyn-d1", "event_type": "damage_reported",
        "event_time": now.isoformat(), "arrived_at": now.isoformat(),
    })
    r = client.post("/api/v1/orders/DYN-1/claims",
                    json={"claim_type": "delay", "idempotency_key": "DYN-K1"})
    assert r.status_code == 201, r.text
    claim_no = r.json()["claim_no"]

    # 既有页不变：总数、条目、顺序与首请求冻结时一致
    p2 = _page(client, p1["next_cursor"]).json()
    assert p2["total"] == 3
    assert [(e["order_id"], e["claim_type"]) for e in p2["entries"]] == [
        ("DYN-1", "damage")]
    assert p2["entries"][0]["status"] == "pending"   # 新到的破损扫描不影响
    p3 = _page(client, p2["next_cursor"]).json()
    assert [(e["order_id"], e["claim_type"]) for e in p3["entries"]] == [
        ("DYN-1", "loss")]
    assert p3["next_cursor"] is None
    # 第 1 页重取也不变（含未纳入新受理的凭证）
    assert _page(client, f"{p1['snapshot_id']}:1").json()["entries"] == p1["entries"]

    # 新快照才反映新数据：凭证冻结再次申请、破损变为可申请、新订单纳入
    fresh = _create_snapshot(client, as_of=as_of).json()
    assert fresh["total"] == 6
    delay = _entry(fresh, "DYN-1", "delay")
    assert delay["status"] == "filable"              # 各自判定仍在时限内
    assert delay["voucher_nos"] == [claim_no]
    assert delay["can_apply_again"] is False         # 已有凭证一律不可再次申请
    damage = _entry(fresh, "DYN-1", "damage")
    assert damage["status"] == "filable" and damage["can_apply_again"] is True


# ------------------------------------------------------------ 游标：过期与无效

def test_cursor_expires_30_minutes_after_snapshot_creation(client):
    make_order(client)
    p1 = _create_snapshot(client, page_size=1).json()
    assert p1["next_cursor"]
    # 将快照过期时间拨到过去，模拟创建 30 分钟之后
    db.execute("UPDATE todo_snapshots SET expires_at = ? WHERE snapshot_id = ?",
               ("2000-01-01T00:00:00+00:00", p1["snapshot_id"]))
    r = _page(client, p1["next_cursor"])
    assert r.status_code == 410, r.text
    err = r.json()["error"]
    assert err["code"] == "CURSOR_EXPIRED"
    assert err["details"]["snapshot_id"] == p1["snapshot_id"]
    # 第 1 页同样过期
    assert _page(client, f"{p1['snapshot_id']}:1").status_code == 410


def test_invalid_cursors_are_rejected(client):
    make_order(client)
    p1 = _create_snapshot(client, page_size=1).json()
    sid = p1["snapshot_id"]
    for bad in ("not-a-cursor", f"{sid}:0", f"{sid}:-1", f"{sid}:abc",
                f"{sid}:99", "TS-20000101000000-DEADBEEF:1", ":"):
        r = _page(client, bad)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "CURSOR_INVALID", bad
    # 缺 cursor 参数 -> 统一参数校验错误
    r = client.get("/api/v1/claim-todos")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


# ------------------------------------------------------------ 首请求参数校验

def test_first_request_validation(client):
    # 缺 as_of / page_size
    r = client.post("/api/v1/claim-todos", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"
    # 核对时刻缺少时区偏移
    r = _create_snapshot(client, as_of="2025-02-20T12:00:00")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"
    # 页大小越界（1～100）
    for size in (0, 101, -1):
        r = _create_snapshot(client, page_size=size)
        assert r.status_code == 422, size
        assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_empty_snapshot(client):
    r = _create_snapshot(client, as_of="2025-02-20T12:00:00+08:00", page_size=10)
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["total"] == 0
    assert payload["entries"] == []
    assert payload["page_count"] == 1
    assert payload["next_cursor"] is None
