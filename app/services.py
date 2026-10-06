"""业务服务层：订单、事件摄取、判定装配、归档与理赔申请。"""
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config
from . import constants as C
from . import db
from .db import q, q1
from .engine import (diagnose_business_days, evaluate, parse_cutoff, parse_dt,
                     resolve_policy, to_local, utc_now)
from .errors import AppError
from .schemas import CreateOrderRequest, EventIn

WINDOW_RULE_NOTE = "迟到更正事件仅能产生新一代判定，不修改已归档结果"


# ---------------------------------------------------------------- 订单

def get_order(order_id: str) -> dict | None:
    row = q1("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    return dict(row) if row else None


def require_order(order_id: str) -> dict:
    order = get_order(order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", f"订单不存在: {order_id}", 404)
    return order


def create_order(req: CreateOrderRequest) -> dict:
    created_utc = parse_dt(req.created_at)
    try:
        ZoneInfo(req.timezone)
    except ZoneInfoNotFoundError:
        raise AppError("TIMEZONE_INVALID", f"无法识别的时区: {req.timezone}", 422)

    cal = q1("SELECT 1 FROM calendars WHERE calendar_code = ? LIMIT 1", (req.calendar_code,))
    if cal is None:
        raise AppError(
            "CALENDAR_NOT_FOUND",
            f"节假日表不存在: {req.calendar_code}，请先初始化或使用已有日历代码",
            422,
        )

    try:
        policy = resolve_policy(created_utc)
    except LookupError:
        raise AppError(
            "POLICY_NOT_FOUND",
            f"订单创建时间 {created_utc.isoformat()} 没有任何生效政策版本",
            422,
        )

    promise_iso = parse_dt(req.promise_by).isoformat() if req.promise_by else None
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO orders(order_id, created_at, timezone, calendar_code,
                                  promise_by, weight_kg, created_policy)
               VALUES (?,?,?,?,?,?,?)""",
            (req.order_id, created_utc.isoformat(), req.timezone, req.calendar_code,
             promise_iso, req.weight_kg, policy["version"]),
        )
        conn.execute(
            """INSERT INTO generations(order_id, gen_no, opened_at, archived)
               VALUES (?,1,?,0)""",
            (req.order_id, created_utc.isoformat()),
        )
    order = require_order(req.order_id)
    order["policy_version"] = policy["version"]
    return order


# ---------------------------------------------------------------- 事件摄取

def _validate_event_shape(ev: EventIn) -> None:
    is_corr = ev.correction_action is not None
    if is_corr:
        if ev.correction_action in (C.AMEND, C.CANCEL):
            if not ev.target_event_id:
                raise AppError(
                    "CORRECTION_TARGET_REQUIRED",
                    f"更正事件 {ev.event_id}（{ev.correction_action}）缺少 target_event_id",
                )
            if ev.event_type not in C.BASE_EVENT_TYPES:
                raise AppError(
                    "EVENT_TYPE_INVALID",
                    f"更正事件类型必须是基础扫描类型之一: {sorted(C.BASE_EVENT_TYPES)}",
                )
        elif ev.correction_action == C.INSERT:
            if ev.event_type not in C.INSERTABLE_EVENT_TYPES:
                raise AppError(
                    "EVENT_TYPE_INVALID",
                    f"补录事件类型仅允许: {sorted(C.INSERTABLE_EVENT_TYPES)}",
                )
    else:
        if ev.event_type not in C.BASE_EVENT_TYPES:
            raise AppError(
                "EVENT_TYPE_INVALID",
                f"未知扫描事件类型: {ev.event_type}，允许: {sorted(C.BASE_EVENT_TYPES)}",
            )


def _latest_gen(conn, order_id: str):
    return conn.execute(
        "SELECT * FROM generations WHERE order_id = ? ORDER BY gen_no DESC LIMIT 1",
        (order_id,),
    ).fetchone()


def ingest_events(order_id: str, events: list[EventIn]) -> dict:
    """按到达顺序原子写入整批事件。

    - 重放（event_id 已存在，按订单维度）跳过且不计入顺序；
    - 到达时间必须相对已接受流单调不减，否则整批回滚并返回 EVENT_ORDER_INVALID；
    - 最新一代已归档时，任一新事件开启新一代（迟到事件只产生新判定）。
    """
    require_order(order_id)
    for ev in events:
        _validate_event_shape(ev)

    items = []
    duplicates = []
    accepted = 0
    reopened = False
    current_gen_no = 1

    with db.tx() as conn:
        prev_row = conn.execute(
            "SELECT arrived_at FROM events WHERE order_id = ? ORDER BY arrived_at DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        prev_arrived = parse_dt(prev_row["arrived_at"]) if prev_row else None

        for idx, ev in enumerate(events):
            dup = conn.execute(
                "SELECT 1 FROM events WHERE order_id = ? AND event_id = ?",
                (order_id, ev.event_id),
            ).fetchone()
            if dup:
                duplicates.append(ev.event_id)
                items.append({"index": idx, "event_id": ev.event_id, "duplicated": True})
                continue

            arrived = parse_dt(ev.arrived_at)
            if prev_arrived is not None and arrived < prev_arrived:
                raise AppError(
                    "EVENT_ORDER_INVALID",
                    f"第 {idx} 个事件 {ev.event_id} 到达时间 {arrived.isoformat()} "
                    f"早于上一已接受事件 {prev_arrived.isoformat()}，事件必须按到达时间顺序提交",
                    422,
                    {"index": idx, "event_id": ev.event_id,
                     "arrived_at": arrived.isoformat(),
                     "previous_arrived_at": prev_arrived.isoformat()},
                )

            gen = _latest_gen(conn, order_id)
            if gen["archived"]:
                # 迟到事件：开启新一代，归档结果保持不变
                conn.execute(
                    """INSERT INTO generations(order_id, gen_no, opened_at, archived, supersedes_gen)
                       VALUES (?,?,?,0,?)""",
                    (order_id, gen["gen_no"] + 1, arrived.isoformat(), gen["gen_no"]),
                )
                gen = _latest_gen(conn, order_id)
                reopened = True
            current_gen_no = gen["gen_no"]

            if ev.correction_action in (C.AMEND, C.CANCEL):
                target = conn.execute(
                    "SELECT event_type FROM events WHERE order_id = ? AND event_id = ? AND is_correction = 0",
                    (order_id, ev.target_event_id),
                ).fetchone()
                if target is None:
                    raise AppError(
                        "CORRECTION_TARGET_NOT_FOUND",
                        f"更正事件 {ev.event_id} 的目标事件 {ev.target_event_id} 不存在或不是基础扫描事件",
                        422,
                        {"event_id": ev.event_id, "target_event_id": ev.target_event_id},
                    )
                if ev.correction_action == C.AMEND and target["event_type"] != ev.event_type:
                    raise AppError(
                        "CORRECTION_TYPE_MISMATCH",
                        f"更正事件类型 {ev.event_type} 与目标事件类型 {target['event_type']} 不一致",
                        422,
                    )

            conn.execute(
                """INSERT INTO events(event_id, order_id, gen_id, event_type, event_time,
                                      arrived_at, is_correction, correction_action,
                                      target_event_id, note, prev_arrived_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (ev.event_id, order_id, gen["id"], ev.event_type,
                 parse_dt(ev.event_time).isoformat(), arrived.isoformat(),
                 int(ev.correction_action is not None), ev.correction_action,
                 ev.target_event_id, ev.note,
                 prev_arrived.isoformat() if prev_arrived else None),
            )
            prev_arrived = arrived
            accepted += 1
            items.append({
                "index": idx, "event_id": ev.event_id, "duplicated": False,
                "generation_no": current_gen_no,
            })

    return {
        "order_id": order_id,
        "accepted": accepted,
        "duplicates": duplicates,
        "reopened": reopened,
        "generation_no": current_gen_no,
        "items": items,
        "note": WINDOW_RULE_NOTE if reopened else "",
    }


# ---------------------------------------------------------------- 无写入预演

PREVIEW_NOTE = "本次为无写入预演：未写入任何事件、未开启新代、归档快照未受影响"


def _preview_fail(code: str, message: str, items: list[dict], index: int,
                  extra: dict | None = None) -> AppError:
    """预演失败：错误码与正式摄取一致，details 携带逐项校验结果与首个失败位置。"""
    details = {
        "dry_run": True,
        "first_failure_index": index,
        "items": items,
        "writes_performed": False,
    }
    if extra:
        details.update(extra)
    return AppError(code, f"预演失败（{PREVIEW_NOTE}）：{message}", 422, details)


def preview_events(order_id: str, events: list[EventIn],
                   as_of: datetime | None = None) -> dict:
    """无写入预演：在当前最新代事件视图上模拟整批摄取（含 amend/cancel/insert 规则）。

    - 校验规则与正式摄取一致（形状 / 去重 / 到达顺序 / 更正目标 / 类型一致）；
    - 任一事件违反约束 => 整批预演失败（422），系统状态保持现状；
    - 最新代已归档时，模拟结果明确按新一代（gen_no+1）计算，归档快照不被改写；
    - 全程只读：不写事件、不开新代、不改归档快照。
    """
    order, policy = _load_eval_inputs(order_id)
    # before/after 共用同一基准时刻，保证差异只归因于被预演的事件
    as_of_utc = parse_dt(as_of) if as_of is not None else utc_now()

    with db.tx() as conn:
        gen = _latest_gen(conn, order_id)
        rows = _events_until_gen(conn, order_id, gen["id"])
    existing_rows = [dict(r) for r in rows]
    existing_ids = {r["event_id"] for r in existing_rows}
    # amend/cancel 目标解析范围：已接受的基础事件（与正式摄取 is_correction = 0 过滤一致）
    target_types = {r["event_id"]: r["event_type"]
                    for r in existing_rows if not r["is_correction"]}
    prev_arrived = parse_dt(existing_rows[-1]["arrived_at"]) if existing_rows else None

    items: list[dict] = []
    duplicates: list[str] = []
    simulated_rows: list[dict] = []
    accepted_ids: set[str] = set()

    for idx, ev in enumerate(events):
        checks: list[dict] = []
        item = {"index": idx, "event_id": ev.event_id, "valid": True,
                "duplicated": False, "checks": checks}

        # 1) 形状：事件类型 / 更正动作约束
        try:
            _validate_event_shape(ev)
        except AppError as exc:
            checks.append({"check": "shape", "passed": False, "detail": exc.message})
            item["valid"] = False
            items.append(item)
            raise _preview_fail(exc.code, f"第 {idx} 个事件 {ev.event_id}：{exc.message}",
                                items, idx)
        checks.append({"check": "shape", "passed": True,
                       "detail": "事件类型与更正动作约束满足"})

        # 2) 去重：重放事件跳过且不参与顺序校验（与正式摄取一致）
        if ev.event_id in existing_ids or ev.event_id in accepted_ids:
            item["duplicated"] = True
            checks.append({"check": "duplicate", "passed": True,
                           "detail": "event_id 已存在，重放将跳过、不计入判定"})
            items.append(item)
            duplicates.append(ev.event_id)
            continue
        checks.append({"check": "duplicate", "passed": True,
                       "detail": "event_id 未见重复"})

        # 3) 到达顺序：相对已接受流（含批内已接受事件）单调不减
        arrived = parse_dt(ev.arrived_at)
        if prev_arrived is not None and arrived < prev_arrived:
            detail = (f"到达时间 {arrived.isoformat()} 早于上一已接受事件 "
                      f"{prev_arrived.isoformat()}")
            checks.append({"check": "arrival_order", "passed": False, "detail": detail})
            item["valid"] = False
            items.append(item)
            raise _preview_fail(
                "EVENT_ORDER_INVALID",
                f"第 {idx} 个事件 {ev.event_id} {detail}，事件必须按到达时间顺序提交",
                items, idx,
                {"index": idx, "event_id": ev.event_id,
                 "arrived_at": arrived.isoformat(),
                 "previous_arrived_at": prev_arrived.isoformat()},
            )
        checks.append({"check": "arrival_order", "passed": True,
                       "detail": "到达时间不早于上一已接受事件"})

        # 4) 更正目标：存在且为基础扫描事件；amend 要求类型一致
        if ev.correction_action in (C.AMEND, C.CANCEL):
            target_type = target_types.get(ev.target_event_id)
            if target_type is None:
                detail = f"目标事件 {ev.target_event_id} 不存在或不是基础扫描事件"
                checks.append({"check": "correction_target", "passed": False,
                               "detail": detail})
                item["valid"] = False
                items.append(item)
                raise _preview_fail(
                    "CORRECTION_TARGET_NOT_FOUND",
                    f"第 {idx} 个更正事件 {ev.event_id} 的{detail}",
                    items, idx,
                    {"event_id": ev.event_id, "target_event_id": ev.target_event_id},
                )
            if ev.correction_action == C.AMEND and target_type != ev.event_type:
                detail = (f"更正事件类型 {ev.event_type} 与目标事件类型 "
                          f"{target_type} 不一致")
                checks.append({"check": "correction_target", "passed": False,
                               "detail": detail})
                item["valid"] = False
                items.append(item)
                raise _preview_fail("CORRECTION_TYPE_MISMATCH",
                                    f"第 {idx} 个事件 {ev.event_id}：{detail}",
                                    items, idx)
            checks.append({"check": "correction_target", "passed": True,
                           "detail": f"目标事件 {ev.target_event_id} 校验通过"})

        # 通过：合成为内存中的模拟事件行（绝不落库）
        accepted_ids.add(ev.event_id)
        if ev.correction_action is None:
            target_types[ev.event_id] = ev.event_type
        simulated_rows.append({
            "event_id": ev.event_id,
            "event_type": ev.event_type,
            "event_time": parse_dt(ev.event_time).isoformat(),
            "arrived_at": arrived.isoformat(),
            "is_correction": int(ev.correction_action is not None),
            "correction_action": ev.correction_action,
            "target_event_id": ev.target_event_id,
            "note": ev.note or "",
        })
        prev_arrived = arrived
        items.append(item)

    accepted = len(simulated_rows)
    archived = bool(gen["archived"])
    # 与正式摄取一致：仅当存在新事件且最新代已归档时才会开启新一代
    would_reopen = archived and accepted > 0
    if would_reopen:
        gen_info = {"gen_no": gen["gen_no"] + 1, "supersedes_gen": gen["gen_no"]}
    else:
        gen_info = {"gen_no": gen["gen_no"], "supersedes_gen": gen["supersedes_gen"]}

    before = evaluate(order, existing_rows, policy, as_of=as_of_utc,
                      generation_info={"gen_no": gen["gen_no"],
                                       "supersedes_gen": gen["supersedes_gen"]})
    after = evaluate(order, existing_rows + simulated_rows, policy, as_of=as_of_utc,
                     generation_info=gen_info)

    if archived:
        if would_reopen:
            note = (f"当前第 {gen['gen_no']} 代判定已归档；若实际摄取，本批事件将开启第 "
                    f"{gen['gen_no'] + 1} 代。模拟结果按新一代计算，归档快照不会被改写。")
        else:
            note = (f"当前第 {gen['gen_no']} 代判定已归档；本批均为重放事件，"
                    "实际摄取不会开启新代，判定保持不变。")
    else:
        note = (f"在当前第 {gen['gen_no']} 代（未归档）事件视图上模拟；"
                "若实际摄取，本批事件将并入该代。")

    return {
        "order_id": order_id,
        "dry_run": True,
        "valid": True,
        "first_failure_index": None,
        "as_of_utc": as_of_utc.isoformat(),
        "accepted": accepted,
        "duplicates": duplicates,
        "items": items,
        "simulation": {
            "based_on_generation_no": gen["gen_no"],
            "current_generation_archived": archived,
            "would_reopen": would_reopen,
            "generation_no": gen_info["gen_no"],
            "supersedes_generation": gen_info["supersedes_gen"],
            "note": note,
            "determination": after,
        },
        "diff": _diff_determinations(before, after),
        "no_writes": PREVIEW_NOTE,
    }


def _diff_determinations(before: dict, after: dict) -> dict:
    """对照两次判定：各理赔状态/截止时间/缺失证据变化与判定依据差异。"""
    claims_before = {c["claim_type"]: c for c in before["claims"]}
    claims_after = {c["claim_type"]: c for c in after["claims"]}
    claim_diffs = []
    for claim_type in C.ALL_CLAIMS:
        b = claims_before[claim_type]
        a = claims_after[claim_type]
        claim_diffs.append({
            "claim_type": claim_type,
            "claim_name": a["claim_name"],
            "status_before": b["status"],
            "status_after": a["status"],
            "deadline_local_before": b["deadline_local"],
            "deadline_local_after": a["deadline_local"],
            "deadline_utc_before": b["deadline_utc"],
            "deadline_utc_after": a["deadline_utc"],
            "missing_evidence_before": b["missing_evidence"],
            "missing_evidence_after": a["missing_evidence"],
            "basis_added": [x for x in a["basis"] if x not in b["basis"]],
            "basis_removed": [x for x in b["basis"] if x not in a["basis"]],
            "changed": (b["status"] != a["status"]
                        or b["deadline_utc"] != a["deadline_utc"]
                        or b["missing_evidence"] != a["missing_evidence"]),
        })

    def _field(key):
        return {"before": before[key], "after": after[key],
                "changed": before[key] != after[key]}

    me_before = set(before["missing_evidence"])
    me_after = set(after["missing_evidence"])
    basis_before = before["basis"]
    basis_after = after["basis"]
    return {
        "overall_status": _field("overall_status"),
        "eligible_to_file": _field("eligible_to_file"),
        "earliest_deadline_local": _field("earliest_deadline_local"),
        "earliest_deadline_utc": _field("earliest_deadline_utc"),
        "missing_evidence": {
            "before": before["missing_evidence"],
            "after": after["missing_evidence"],
            "added": sorted(me_after - me_before),
            "resolved": sorted(me_before - me_after),
        },
        "claims": claim_diffs,
        "basis_added": [x for x in basis_after if x not in basis_before],
        "basis_removed": [x for x in basis_before if x not in basis_after],
    }


# ---------------------------------------------------------------- 判定

def _load_eval_inputs(order_id: str):
    order = require_order(order_id)
    from .engine import load_policy
    policy = load_policy(order["created_policy"])
    return order, policy


def _events_until_gen(conn, order_id: str, gen_id: int):
    rows = conn.execute(
        """SELECT e.* FROM events e JOIN generations g ON e.gen_id = g.id
           WHERE e.order_id = ? AND g.id <= ? ORDER BY e.arrived_at""",
        (order_id, gen_id),
    ).fetchall()
    return [dict(r) for r in rows]


def live_determination(order_id: str, as_of: datetime | None = None) -> dict:
    order, policy = _load_eval_inputs(order_id)
    with db.tx() as conn:
        gen = _latest_gen(conn, order_id)
        gen_info = {"gen_no": gen["gen_no"], "supersedes_gen": gen["supersedes_gen"]}
        if gen["archived"]:
            # 最新一代已归档：当前判定即不可变快照，不重新计算
            return _archived_snapshot(gen)
        rows = _events_until_gen(conn, order_id, gen["id"])
    return evaluate(order, rows, policy, as_of=as_of, generation_info=gen_info)


def _archived_snapshot(gen_row) -> dict:
    snap = json.loads(gen_row["determination_json"])
    snap["archived"] = True
    snap["archived_at_utc"] = gen_row["archived_at"]
    return snap


def archive_determination(order_id: str, as_of: datetime | None = None) -> dict:
    order, policy = _load_eval_inputs(order_id)
    with db.tx() as conn:
        gen = _latest_gen(conn, order_id)
        if gen["archived"]:
            raise AppError(
                "ALREADY_ARCHIVED",
                f"订单 {order_id} 第 {gen['gen_no']} 代判定已归档；"
                "如需反映新到事件，请先摄取事件生成新一代判定",
                409,
            )
        gen_info = {"gen_no": gen["gen_no"], "supersedes_gen": gen["supersedes_gen"]}
        rows = _events_until_gen(conn, order_id, gen["id"])
        result = evaluate(order, rows, policy, as_of=as_of, generation_info=gen_info)
        result["archived"] = True
        stamp = (as_of or utc_now()).isoformat()
        result["archived_at_utc"] = stamp
        conn.execute(
            "UPDATE generations SET archived = 1, archived_at = ?, determination_json = ? WHERE id = ?",
            (stamp, json.dumps(result, ensure_ascii=False), gen["id"]),
        )
    return result


def determination_history(order_id: str) -> dict:
    require_order(order_id)
    rows = q(
        "SELECT gen_no, opened_at, archived, archived_at, supersedes_gen, determination_json "
        "FROM generations WHERE order_id = ? ORDER BY gen_no",
        (order_id,),
    )
    gens = []
    for r in rows:
        item = {
            "generation_no": r["gen_no"],
            "opened_at_utc": r["opened_at"],
            "archived": bool(r["archived"]),
            "archived_at_utc": r["archived_at"],
            "supersedes_generation": r["supersedes_gen"],
        }
        if r["determination_json"]:
            snap = json.loads(r["determination_json"])
            item["overall_status"] = snap["overall_status"]
            item["snapshot"] = snap
        gens.append(item)
    return {"order_id": order_id, "generations": gens}


def list_events(order_id: str) -> dict:
    require_order(order_id)
    rows = q(
        """SELECT e.event_id, e.event_type, e.event_time, e.arrived_at,
                  e.is_correction, e.correction_action, e.target_event_id, e.note, g.gen_no
           FROM events e JOIN generations g ON e.gen_id = g.id
           WHERE e.order_id = ? ORDER BY e.arrived_at""",
        (order_id,),
    )
    return {"order_id": order_id, "events": [dict(r) for r in rows]}


# ---------------------------------------------------------------- 理赔申请（凭证）

# 非 filable 状态 -> 可区分的业务拒绝代号（不生成凭证）
_REJECTION_BY_STATUS = {
    "pending": ("CLAIM_PENDING", "理赔事由的触发事件尚未出现，暂不受理"),
    "not_filable": ("CLAIM_DEADLINE_PASSED", "已过当地申请截止时刻，不再受理"),
    "not_applicable": ("CLAIM_NOT_APPLICABLE", "按现有扫描事件该理赔事由不成立，不予受理"),
    "indeterminate": ("CLAIM_INDETERMINATE", "证据不足、该类型不可判定，不予受理"),
}


def _new_claim_no(accepted_utc: datetime) -> str:
    return f"CLM-{accepted_utc.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4).upper()}"


def _recheck_at_receive_time(conn, order: dict, policy: dict, received_utc: datetime) -> dict:
    """以服务实际接收时间核验当前代资格——即使当前代已归档也实时重算。

    - 不读取 generations.determination_json 归档快照，只以当前最新代的事件视图重算；
    - 接收时刻之后到达的事件/更正不构成本次核验依据（arrived_at > received_at 剔除）；
    - 订单仍按下单时锁定的政策版本（order.created_policy）判定。
    """
    gen = _latest_gen(conn, order["order_id"])
    rows = _events_until_gen(conn, order["order_id"], gen["id"])
    visible = [r for r in rows if parse_dt(r["arrived_at"]) <= received_utc]
    gen_info = {"gen_no": gen["gen_no"], "supersedes_gen": gen["supersedes_gen"]}
    result = evaluate(order, visible, policy, as_of=received_utc, generation_info=gen_info)

    if gen["archived"]:
        # 明确记录：资格是按接收时间重算的，未把较早归档快照当作实时资格
        result["basis"].append(
            f"第 {gen['gen_no']} 代判定虽已归档，本申请仍按服务接收时间 "
            f"{to_local(received_utc, order['timezone']).isoformat()} 重新核验，"
            "未采用较早的归档快照作为实时资格"
        )
        result["archived_generation_rechecked"] = True
    else:
        result["archived_generation_rechecked"] = False
    return result


def _voucher_row(conn, claim_no: str | None = None, *, order_id: str | None = None,
                 idem_key: str | None = None):
    if claim_no is not None:
        return conn.execute(
            "SELECT * FROM claim_vouchers WHERE claim_no = ?", (claim_no,)
        ).fetchone()
    return conn.execute(
        "SELECT * FROM claim_vouchers WHERE order_id = ? AND idempotency_key = ?",
        (order_id, idem_key),
    ).fetchone()


def _voucher_payload(row, replayed: bool = False) -> dict:
    """凭证一经受理不可变：所有字段均来自受理时冻结的快照行。"""
    claim = json.loads(row["claim_json"])
    determination = json.loads(row["determination_json"])
    policy = json.loads(row["policy_json"])
    window = next((r["window"] for r in policy["claims"]
                   if r["claim_type"] == row["claim_type"]), None)
    return {
        "claim_no": row["claim_no"],
        "order_id": row["order_id"],
        "claim_type": row["claim_type"],
        "claim_name": C.CLAIM_LABELS[row["claim_type"]],
        "idempotency_key": row["idempotency_key"],
        "received_at_utc": row["received_at"],
        "accepted_at_utc": row["accepted_at"],
        "decision_code": row["decision_code"],          # 受理时恒为 FILABLE
        "status": "accepted",
        "replayed": replayed,
        "immutable": True,
        "policy_version": row["policy_version"],
        "policy": {
            "version": policy.get("version"),
            "display_name": policy.get("display_name"),
            "window": window,
            "default_cutoff": policy.get("default_cutoff"),
        },
        "anchor_time_utc": claim.get("anchor_time"),
        "deadline_local": claim.get("deadline_local"),
        "deadline_utc": claim.get("deadline_utc"),
        "missing_evidence": claim.get("missing_evidence", []),
        "basis": claim.get("basis", []),                 # 该类型当时的判定依据
        "generation_no": determination.get("generation_no"),
        "archived_generation_rechecked": determination.get(
            "archived_generation_rechecked", False),
        "determination_basis": determination.get("basis", []),
        "claim": claim,                                  # 冻结的该类型判定
        "determination": determination,                  # 冻结的整单判定
    }


def submit_claim(order_id: str, claim_type: str, idem_key: str,
                 received_at: datetime | None = None) -> tuple[dict, bool]:
    """受理一笔理赔申请。返回 (凭证, 是否幂等重放)。

    - 以服务实际接收时间（received_at，缺省=当前 UTC）实时核验当前代，
      当前代已归档也不使用归档快照；
    - 仅当该类型状态为 filable 时受理并冻结凭证；否则按状态给可区分拒绝，不生成凭证；
    - 同订单同幂等键同类型重试 => 原凭证（replayed=True）；同键换类型 => 409；
    - 同订单同类型至多一张凭证，并发不同键由进程锁 + UNIQUE 双重兜底。
    """
    order = require_order(order_id)
    if claim_type not in C.ALL_CLAIMS:
        raise AppError(
            "CLAIM_TYPE_INVALID",
            f"未知理赔类型: {claim_type}，允许: {list(C.ALL_CLAIMS)}",
            422,
        )
    received_utc = parse_dt(received_at) if received_at is not None else utc_now()
    from .engine import load_policy
    policy = load_policy(order["created_policy"])

    with db.tx() as conn:
        # 1) 幂等键：同订单同键只指向一张凭证
        existing = _voucher_row(conn, order_id=order_id, idem_key=idem_key)
        if existing is not None:
            if existing["claim_type"] == claim_type:
                return _voucher_payload(existing, replayed=True), True
            raise AppError(
                "CLAIM_CONFLICT",
                f"幂等键 {idem_key} 已用于订单 {order_id} 的 "
                f"{existing['claim_type']} 类申请，不能再用于 {claim_type} 类",
                409,
                {"idempotency_key": idem_key,
                 "submitted_claim_type": claim_type,
                 "existing_claim_type": existing["claim_type"],
                 "existing_claim_no": existing["claim_no"]},
            )

        # 2) 同订单同类型至多一张凭证（不同键也不允许）
        same_type = conn.execute(
            "SELECT * FROM claim_vouchers WHERE order_id = ? AND claim_type = ?",
            (order_id, claim_type),
        ).fetchone()
        if same_type is not None:
            raise AppError(
                "CLAIM_ALREADY_ACCEPTED",
                f"订单 {order_id} 的 {claim_type} 类理赔已受理（凭证 {same_type['claim_no']}），"
                "同一订单同一类型至多一张凭证",
                409,
                {"claim_type": claim_type,
                 "existing_claim_no": same_type["claim_no"],
                 "existing_idempotency_key": same_type["idempotency_key"]},
            )

        # 3) 以接收时间实时核验（即使当前代已归档，也重算而非读快照）
        determination = _recheck_at_receive_time(conn, order, policy, received_utc)
        claim_result = next(c for c in determination["claims"]
                            if c["claim_type"] == claim_type)
        status = claim_result["status"]
        if status != "filable":
            code, reason = _REJECTION_BY_STATUS[status]
            raise AppError(
                code,
                f"{C.CLAIM_LABELS[claim_type]}理赔申请不予受理：{reason}",
                422,
                {"order_id": order_id,
                 "claim_type": claim_type,
                 "status": status,
                 "decision_code": status.upper(),
                 "received_at_utc": received_utc.isoformat(),
                 "policy_version": policy["version"],
                 "generation_no": determination["generation_no"],
                 "deadline_local": claim_result.get("deadline_local"),
                 "deadline_utc": claim_result.get("deadline_utc"),
                 "missing_evidence": claim_result.get("missing_evidence", []),
                 "basis": claim_result.get("basis", []),
                 "voucher_generated": False},
            )

        # 4) 受理：冻结凭证（之后更正/新代只影响当前判定，不影响本凭证）
        accepted_utc = utc_now()
        row = None
        for _ in range(3):
            claim_no = _new_claim_no(accepted_utc)
            try:
                conn.execute(
                    """INSERT INTO claim_vouchers(
                           claim_no, order_id, claim_type, idempotency_key,
                           received_at, accepted_at, decision_code,
                           policy_version, policy_json, claim_json, determination_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (claim_no, order_id, claim_type, idem_key,
                     received_utc.isoformat(), accepted_utc.isoformat(), "FILABLE",
                     policy["version"], json.dumps(policy, ensure_ascii=False),
                     json.dumps(claim_result, ensure_ascii=False),
                     json.dumps(determination, ensure_ascii=False)),
                )
                row = _voucher_row(conn, claim_no=claim_no)
                break
            except sqlite3.IntegrityError as exc:
                # 并发兜底：UNIQUE 命中则重新读取既有凭证并按幂等/冲突处理
                conflicted = _voucher_row(conn, order_id=order_id, idem_key=idem_key)
                if conflicted is not None:
                    if conflicted["claim_type"] == claim_type:
                        return _voucher_payload(conflicted, replayed=True), True
                    raise AppError(
                        "CLAIM_CONFLICT",
                        f"幂等键 {idem_key} 已用于 {conflicted['claim_type']} 类申请",
                        409,
                        {"idempotency_key": idem_key,
                         "submitted_claim_type": claim_type,
                         "existing_claim_type": conflicted["claim_type"],
                         "existing_claim_no": conflicted["claim_no"]},
                    )
                same = conn.execute(
                    "SELECT * FROM claim_vouchers WHERE order_id = ? AND claim_type = ?",
                    (order_id, claim_type),
                ).fetchone()
                if same is not None:
                    raise AppError(
                        "CLAIM_ALREADY_ACCEPTED",
                        f"订单 {order_id} 的 {claim_type} 类理赔已受理（凭证 {same['claim_no']}）",
                        409,
                        {"claim_type": claim_type,
                         "existing_claim_no": same["claim_no"],
                         "existing_idempotency_key": same["idempotency_key"]},
                    )
                # 仅申请编号撞号（极小概率）：换新编号重试；其余异常抛出
                if "claim_vouchers.UNIQUE" not in str(exc) and "claim_no" not in str(exc):
                    raise
                accepted_utc = utc_now()
        if row is None:
            raise AppError("CLAIM_ACCEPT_FAILED", "凭证生成失败，请重试", 500)

    return _voucher_payload(row, replayed=False), False


def get_voucher(claim_no: str) -> dict:
    row = q1("SELECT * FROM claim_vouchers WHERE claim_no = ?", (claim_no,))
    if row is None:
        raise AppError("VOUCHER_NOT_FOUND", f"凭证不存在: {claim_no}", 404)
    return _voucher_payload(row)


def list_vouchers(order_id: str) -> dict:
    require_order(order_id)
    rows = q(
        "SELECT * FROM claim_vouchers WHERE order_id = ? "
        "ORDER BY accepted_at, claim_no",
        (order_id,),
    )
    return {"order_id": order_id, "vouchers": [_voucher_payload(r) for r in rows]}


# ---------------------------------------------------------------- 节假日表诊断（只读）

def diagnose_calendar(req) -> dict:
    """节假日表工作日诊断：只读现有日历并逐日给出判定，不写任何数据。

    - 日历不存在 -> 404 CALENDAR_NOT_FOUND；时区不存在 -> 422 TIMEZONE_INVALID；
    - 时间缺少偏移、天数/钟点非法在请求模型层 422 拒绝；
    - 全程只读，不修改日历、政策、订单或凭证。
    """
    cal_row = q1(
        "SELECT 1 FROM calendars WHERE calendar_code = ? LIMIT 1",
        (req.calendar_code,),
    )
    if cal_row is None:
        raise AppError(
            "CALENDAR_NOT_FOUND",
            f"节假日表不存在: {req.calendar_code}，请先初始化或使用已有日历代码",
            404,
        )
    try:
        ZoneInfo(req.timezone)
    except ZoneInfoNotFoundError:
        raise AppError("TIMEZONE_INVALID", f"无法识别的时区: {req.timezone}", 422)

    rows = q(
        "SELECT date, kind, name FROM calendars WHERE calendar_code = ?",
        (req.calendar_code,),
    )
    cal = {r["date"]: (r["kind"], r["name"]) for r in rows}

    result = diagnose_business_days(
        start_utc=parse_dt(req.start_at),
        n=req.business_days,
        cutoff=parse_cutoff(req.cutoff),
        tzname=req.timezone,
        cal=cal,
    )
    return {
        "calendar_code": req.calendar_code,
        "calendar_declared_days": len(cal),
        **result,
    }


# ---------------------------------------------------------------- 理赔待办快照（跨订单，游标分页）

SNAPSHOT_SORT_NOTE = "order_id ASC, claim_type（delay → damage → loss 固定次序）"
SNAPSHOT_NOTE = ("快照在创建时冻结完整结果；游标翻页期间新订单/扫描/凭证不影响"
                 "既有条目、总数与顺序。各类型资格按核对时刻重算，未采用任何归档判定。")


def _new_snapshot_id(created_utc: datetime) -> str:
    return f"TS-{created_utc.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4).upper()}"


def _cursor_secret(conn) -> str:
    """游标签名密钥：随库持久化（app_meta），首次使用时生成，重启后既有游标仍有效。"""
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'todo_cursor_secret'"
    ).fetchone()
    if row:
        return row["value"]
    secret = secrets.token_hex(32)
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES ('todo_cursor_secret', ?)", (secret,)
    )
    return secret


def _encode_cursor(secret: str, snapshot_id: str, page: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"sid": snapshot_id, "page": page}, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}.{sig}"


def _decode_cursor(secret: str, cursor: str) -> tuple[str, int]:
    try:
        payload, sig = cursor.rsplit(".", 1)
    except ValueError:
        raise AppError("SNAPSHOT_CURSOR_INVALID", "游标格式无法识别", 422)
    expect = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(sig, expect):
        raise AppError("SNAPSHOT_CURSOR_INVALID", "游标校验失败（无效或已被篡改）", 422)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return data["sid"], int(data["page"])
    except Exception:
        raise AppError("SNAPSHOT_CURSOR_INVALID", "游标内容无法解析", 422)


def _snapshot_page_payload(row, items: list[dict], page: int, secret: str) -> dict:
    page_size = row["page_size"]
    total = row["total"]
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page - 1) * page_size
    next_cursor = (_encode_cursor(secret, row["snapshot_id"], page + 1)
                   if page < total_pages else None)
    return {
        "snapshot_id": row["snapshot_id"],
        "as_of_utc": row["as_of"],
        "created_at_utc": row["created_at"],
        "expires_at_utc": row["expires_at"],
        "cursor_ttl_seconds": config.SNAPSHOT_CURSOR_TTL_SECONDS,
        "page_size": page_size,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "items": items[start:start + page_size],
        "next_cursor": next_cursor,
        "sorted_by": SNAPSHOT_SORT_NOTE,
        "frozen": True,
        "note": SNAPSHOT_NOTE,
    }


def create_todo_snapshot(as_of: datetime, page_size: int) -> dict:
    """生成跨订单理赔待办快照（首请求），返回第一页。

    - 只纳入 created_at <= 核对时刻的订单、arrived_at <= 核对时刻的扫描事件、
      received_at <= 核对时刻的已受理凭证（received_at 即服务实际接收申请时间，
      与理赔核验基准一致）；
    - 每个订单按下单锁定的政策版本、订单时区与节假日表重算三类理赔资格，
      不读取任何归档判定快照；
    - 完整结果按（订单号, 理赔类型 delay→damage→loss）排序后冻结入库，
      之后翻页不受新订单/扫描/凭证影响；游标自创建起 30 分钟过期。
    """
    from .engine import load_policy
    as_of_utc = parse_dt(as_of)
    as_of_iso = as_of_utc.isoformat()

    with db.tx() as conn:
        secret = _cursor_secret(conn)
        order_rows = conn.execute(
            "SELECT * FROM orders WHERE created_at <= ? ORDER BY order_id",
            (as_of_iso,),
        ).fetchall()

        items: list[dict] = []
        for row in order_rows:
            order = dict(row)
            policy = load_policy(order["created_policy"])
            ev_rows = conn.execute(
                "SELECT * FROM events WHERE order_id = ? ORDER BY arrived_at",
                (order["order_id"],),
            ).fetchall()
            # 核对时刻之后到达的事件/更正不构成本次判定依据
            visible = [dict(r) for r in ev_rows
                       if parse_dt(r["arrived_at"]) <= as_of_utc]
            det = evaluate(order, visible, policy, as_of=as_of_utc)
            voucher_rows = conn.execute(
                "SELECT claim_type, claim_no FROM claim_vouchers "
                "WHERE order_id = ? AND received_at <= ?",
                (order["order_id"], as_of_iso),
            ).fetchall()
            vouchers = {v["claim_type"]: v["claim_no"] for v in voucher_rows}
            for c in det["claims"]:
                claim_no = vouchers.get(c["claim_type"])
                items.append({
                    "order_id": order["order_id"],
                    "claim_type": c["claim_type"],
                    "claim_name": c["claim_name"],
                    "status": c["status"],
                    "deadline_local": c["deadline_local"],
                    "deadline_utc": c["deadline_utc"],
                    "missing_evidence": c["missing_evidence"],
                    "voucher_claim_no": claim_no,
                    # 已有凭证的类型一律不可再次申请；其余按各自重算判定
                    "can_apply_again": claim_no is None and c["status"] == "filable",
                })

        created = utc_now()
        ttl = config.SNAPSHOT_CURSOR_TTL_SECONDS
        expires = created + timedelta(seconds=ttl)
        snapshot_id = None
        for _ in range(3):
            candidate = _new_snapshot_id(created)
            try:
                conn.execute(
                    """INSERT INTO todo_snapshots(
                           snapshot_id, created_at, as_of, page_size, total,
                           expires_at, items_json)
                       VALUES (?,?,?,?,?,?,?)""",
                    (candidate, created.isoformat(), as_of_iso, page_size,
                     len(items), expires.isoformat(),
                     json.dumps(items, ensure_ascii=False)),
                )
                snapshot_id = candidate
                break
            except sqlite3.IntegrityError:
                continue  # 快照编号撞号（极小概率）：换新编号重试
        if snapshot_id is None:
            raise AppError("SNAPSHOT_CREATE_FAILED", "快照创建失败，请重试", 500)
        row = conn.execute(
            "SELECT * FROM todo_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()

    return _snapshot_page_payload(row, items, 1, secret)


def get_todo_snapshot_page(cursor: str) -> dict:
    """按游标取快照后续页。

    - 游标无效（无法解析/校验失败/快照不存在/页码越界）→ 422 SNAPSHOT_CURSOR_INVALID；
    - 游标自快照创建起 30 分钟过期 → 410 SNAPSHOT_CURSOR_EXPIRED；
    - 命中后返回冻结条目中的对应页：条目、总数与顺序与首请求完全一致。
    """
    if not cursor or not isinstance(cursor, str):
        raise AppError("SNAPSHOT_CURSOR_INVALID", "缺少游标 cursor", 422)
    with db.tx() as conn:
        secret = _cursor_secret(conn)
        snapshot_id, page = _decode_cursor(secret, cursor)
        row = conn.execute(
            "SELECT * FROM todo_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise AppError("SNAPSHOT_CURSOR_INVALID",
                           "游标指向的快照不存在或已被清理", 422)
        if utc_now() > parse_dt(row["expires_at"]):
            raise AppError(
                "SNAPSHOT_CURSOR_EXPIRED",
                f"游标已过期：快照 {snapshot_id} 创建于 {row['created_at']}，"
                f"游标有效期 {config.SNAPSHOT_CURSOR_TTL_SECONDS} 秒，"
                f"已于 {row['expires_at']} 过期，请重新发起首请求",
                410,
                {"snapshot_id": snapshot_id,
                 "created_at_utc": row["created_at"],
                 "expires_at_utc": row["expires_at"],
                 "cursor_ttl_seconds": config.SNAPSHOT_CURSOR_TTL_SECONDS},
            )
        total_pages = max(1, (row["total"] + row["page_size"] - 1) // row["page_size"])
        if not 1 <= page <= total_pages:
            raise AppError("SNAPSHOT_CURSOR_INVALID",
                           f"游标页码越界：第 {page} 页（共 {total_pages} 页）", 422)
        items = json.loads(row["items_json"])
    return _snapshot_page_payload(row, items, page, secret)
