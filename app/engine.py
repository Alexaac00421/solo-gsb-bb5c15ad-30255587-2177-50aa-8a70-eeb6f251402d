"""核心判定引擎（无状态、纯函数，便于单测）。

职责：
1. 按订单创建时间选取生效政策版本；
2. 在订单时区/节假日表上做"工作日截止"推算；
3. 把原始扫描事件 + 更正事件物化为"有效事件"；
4. 输出各理赔类型的可申请状态、截止时间、缺失证据、判定依据。
"""
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import constants as C
from .db import q1

UTC = timezone.utc


# ---------------------------------------------------------------- 基础工具

def parse_dt(value) -> datetime:
    """入参必须是带时区偏移的时间，统一转 aware UTC。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"无法解析时间: {value!r}")
    if dt.tzinfo is None:
        raise ValueError(f"时间缺少时区信息: {value!r}")
    return dt.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_local(dt_utc: datetime, tzname: str) -> datetime:
    return dt_utc.astimezone(ZoneInfo(tzname))


def parse_cutoff(text: str) -> time:
    h, m, *rest = [int(x) for x in text.split(":")]
    s = rest[0] if rest else 0
    return time(h, m, s)


# ---------------------------------------------------------------- 政策版本

def load_policy(version: str) -> dict:
    row = q1("SELECT body_json FROM policies WHERE version = ?", (version,))
    if row is None:
        raise LookupError(f"政策版本不存在: {version}")
    return json.loads(row["body_json"])


def resolve_policy(order_created_utc: datetime) -> dict:
    """选取订单创建时刻生效（valid_from <= 创建 < valid_to）的最新版本。"""
    iso = order_created_utc.isoformat()
    row = q1(
        """SELECT body_json FROM policies
           WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)
           ORDER BY valid_from DESC LIMIT 1""",
        (iso, iso),
    )
    if row is None:
        raise LookupError("订单创建时间没有任何生效政策版本")
    return json.loads(row["body_json"])


# ---------------------------------------------------------------- 节假日日历

def _calendar_map(calendar_code: str) -> dict[str, str]:
    from .db import q
    rows = q("SELECT date, kind FROM calendars WHERE calendar_code = ?", (calendar_code,))
    return {r["date"]: r["kind"] for r in rows}


def is_business_day(d: date, calendar_code: str) -> bool:
    """节假日表显式声明优先（holiday=休 / workday=补班）；否则按周一至周五。"""
    cal = _calendar_map(calendar_code)
    declared = cal.get(d.isoformat())
    if declared == "holiday":
        return False
    if declared == "workday":
        return True
    return d.weekday() < 5


def add_business_days(start: date, n: int, calendar_code: str) -> date:
    """从 start（含）向后数 n 个工作日，返回第 n 个工作日；n=0 即 start 当天（若非工作日则前移规则见调用方）。

    这里采用"触发日当天不计入，次日起数 n 个工作日，截止落在第 n 个工作日"的口径，
    与国内监管常见的"次日起算"一致。
    """
    cur = start
    remaining = n
    while remaining > 0:
        cur += timedelta(days=1)
        if is_business_day(cur, calendar_code):
            remaining -= 1
    return cur


WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def diagnose_business_days(
    start_utc: datetime,
    n: int,
    cutoff: time,
    tzname: str,
    cal: dict[str, tuple[str, str]],
) -> dict:
    """节假日表工作日诊断（只读、纯函数，便于单测）。

    从起始时刻（转换到 tzname 当地日期）的**次日**起逐日判定，直到数出第 n 个工作日：
    - 日历显式声明优先：holiday=放假休息 / workday=调休补班（即使落在周末也计为工作日）；
    - 无声明时按默认规则：周一至周五工作日、周六周日休息。
    逐日记录判定、声明、默认规则、累计工作日数与（跳过）原因，
    并返回第 n 个工作日在当地截止钟点的本地/UTC 时刻。

    cal: {YYYY-MM-DD: (kind, name)}，由调用方从 calendars 表读取后注入（本函数不碰数据库）。
    """
    tz = ZoneInfo(tzname)
    start_local = start_utc.astimezone(tz)
    cur = start_local.date()

    entries: list[dict] = []
    counted = 0
    rest_seen = 0
    holidays_seen = 0
    makeup_seen = 0
    deadline_date: date | None = None

    while counted < n:
        cur += timedelta(days=1)
        weekday = cur.weekday()
        is_weekend = weekday >= 5
        declaration = cal.get(cur.isoformat())
        if declaration is not None:
            kind, name = declaration
            declared = {"kind": kind, "name": name}
            if kind == "holiday":
                is_workday = False
                holidays_seen += 1
            else:  # workday：调休补班，落在周末也计为工作日
                is_workday = True
                makeup_seen += 1
        else:
            kind = None
            declared = None
            is_workday = not is_weekend

        if is_workday:
            counted += 1
            deadline_date = cur
            if kind == "workday":
                if is_weekend:
                    reason = (f"日历声明补班上班（{name}），虽落在周末也计为工作日，计入")
                else:
                    reason = f"日历声明补班上班（{name}），计为工作日，计入"
            else:
                reason = "无日历声明，按默认规则周一至周五为工作日，计入"
        else:
            rest_seen += 1
            if kind == "holiday":
                suffix = "（默认规则下当日原为周末休息日）" if is_weekend else ""
                reason = f"日历声明放假（{name}），跳过{suffix}"
            else:
                reason = "无日历声明，按默认规则周六、周日为休息日，跳过"

        if is_weekend:
            default_rule = "默认规则：周六、周日为休息日"
        else:
            default_rule = "默认规则：周一至周五为工作日"

        entries.append({
            "date": cur.isoformat(),
            "weekday": WEEKDAY_LABELS[weekday],
            "verdict": "工作" if is_workday else "休息",
            "is_business_day": is_workday,
            "declaration": declared,
            "weekend_default_rule": default_rule,
            "business_day_count": counted,
            "skipped": not is_workday,
            "reason": reason,
        })

    deadline_local = datetime.combine(deadline_date, cutoff, tzinfo=tz)
    deadline_utc = deadline_local.astimezone(UTC)
    return {
        "timezone": tzname,
        "start_at_local": start_local.isoformat(),
        "start_at_utc": start_utc.isoformat(),
        "business_days_requested": n,
        "cutoff_local": cutoff.strftime("%H:%M:%S"),
        "count_basis": "起始日当天不计入，自次日起逐日计数，截止落在第 N 个工作日",
        "count_start_date": (start_local.date() + timedelta(days=1)).isoformat(),
        "deadline_date": deadline_date.isoformat(),
        "deadline_local": deadline_local.isoformat(),
        "deadline_utc": deadline_utc.isoformat(),
        "summary": {
            "business_days_counted": counted,
            "rest_days_seen": rest_seen,
            "declared_holidays_seen": holidays_seen,
            "declared_makeup_workdays_seen": makeup_seen,
            "days_listed": len(entries),
        },
        "entries": entries,
        "read_only": True,
        "note": "只读诊断：未修改日历、政策、订单或凭证",
    }


def compute_deadline(
    anchor_utc: datetime,
    window: dict,
    tzname: str,
    calendar_code: str,
    default_cutoff: str,
) -> datetime:
    """根据时限窗口计算截止时刻（返回 UTC datetime）。

    window: {"days": n, "day_basis": "business"|"calendar", "cutoff": "23:59:59"?}
    """
    local_anchor = to_local(anchor_utc, tzname)
    cutoff = parse_cutoff(window.get("cutoff") or default_cutoff)
    days = int(window["days"])
    if window.get("day_basis") == "business":
        due_date = add_business_days(local_anchor.date(), days, calendar_code)
    else:
        due_date = local_anchor.date() + timedelta(days=days)
    due_local = datetime.combine(due_date, cutoff, tzinfo=ZoneInfo(tzname))
    return due_local.astimezone(UTC)


# ---------------------------------------------------------------- 事件物化

@dataclass
class BaseEvent:
    event_id: str
    event_type: str
    event_time: datetime      # UTC
    arrived_at: datetime      # UTC
    cancelled: bool = False
    amended_time: datetime | None = None
    note: str = ""

    @property
    def effective_time(self) -> datetime:
        return self.amended_time or self.event_time


def _row_get(r, key):
    """同时兼容 sqlite3.Row（下标）与对象属性两种行表示。"""
    if isinstance(r, dict):
        return r[key]
    try:
        return r[key]
    except (TypeError, KeyError, IndexError):
        return getattr(r, key)


def materialize(rows) -> tuple[dict[str, BaseEvent], list[str]]:
    """把按到达顺序入库的事件流物化为 {type: BaseEvent}（每类取有效时间最早的一条）。

    更正事件（amend/cancel/insert）只影响物化结果，原始行永不修改、不删除。
    返回 (事件映射, 更正说明列表)。
    """
    bases: dict[str, BaseEvent] = {}
    amendments: list[str] = []

    for r in rows:
        etype = _row_get(r, "event_type")
        if not _row_get(r, "is_correction"):
            bases[_row_get(r, "event_id")] = BaseEvent(
                event_id=_row_get(r, "event_id"),
                event_type=etype,
                event_time=parse_dt(_row_get(r, "event_time")),
                arrived_at=parse_dt(_row_get(r, "arrived_at")),
                note=_row_get(r, "note") or "",
            )
            continue

        action = _row_get(r, "correction_action")
        target_id = _row_get(r, "target_event_id")
        label = C.EVENT_LABELS.get(etype, etype)

        if action in (C.AMEND, C.CANCEL):
            target = bases.get(target_id)
            if target is None:
                # 目标事件不存在：更正无效（正常路径会在入库前拦截，此处兜底忽略）
                continue
            if action == C.AMEND:
                new_time = parse_dt(_row_get(r, "event_time"))
                old_local = target.effective_time.isoformat()
                target.amended_time = new_time
                amendments.append(
                    f"更正事件 {_row_get(r, 'event_id')} 将{label}时间由 {old_local} 修正为 "
                    f"{new_time.isoformat()}"
                )
            else:
                target.cancelled = True
                amendments.append(
                    f"更正事件 {_row_get(r, 'event_id')} 撤销了{label}事件 {target_id}"
                )

        elif action == C.INSERT:
            # 补录：合成一条基础事件参与判定，ID 用更正事件自身避免与原流混淆
            bases[_row_get(r, "event_id")] = BaseEvent(
                event_id=_row_get(r, "event_id"),
                event_type=etype,
                event_time=parse_dt(_row_get(r, "event_time")),
                arrived_at=parse_dt(_row_get(r, "arrived_at")),
                note=f"补录事件（{_row_get(r, 'note')}）" if _row_get(r, "note") else "补录事件",
            )
            amendments.append(f"更正事件 {_row_get(r, 'event_id')} 补录了一条{label}事件")

    effective: dict[str, BaseEvent] = {}
    for ev in bases.values():
        if ev.cancelled:
            continue
        existing = effective.get(ev.event_type)
        if existing is None or ev.effective_time < existing.effective_time:
            effective[ev.event_type] = ev
    return effective, amendments


# ---------------------------------------------------------------- 判定

def _evidence_item(key: str, present: bool, detail: str = "") -> dict:
    return {"key": key, "name": C.EVIDENCE_LABELS[key], "present": present, "detail": detail}


def _claim_result(
    claim: str,
    window: dict,
    anchor_utc: datetime | None,
    status: str,
    tzname: str,
    calendar_code: str,
    default_cutoff: str,
    evidence: list[dict],
    reasons: list[str],
    basis_extra: list[str] | None = None,
) -> dict:
    deadline = None
    if anchor_utc is not None:
        deadline = compute_deadline(anchor_utc, window, tzname, calendar_code, default_cutoff)
    basis = list(reasons)
    if deadline is not None:
        basis.append(
            f"时限 {window['days']} 个"
            f"{'工作日（已跳过当地节假日）' if window.get('day_basis') == 'business' else '自然日'}，"
            f"截止 {to_local(deadline, tzname).strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
    if basis_extra:
        basis.extend(basis_extra)
    return {
        "claim_type": claim,
        "claim_name": C.CLAIM_LABELS[claim],
        "status": status,  # filable | pending | not_filable | not_applicable | indeterminate
        "anchor_time": anchor_utc.isoformat() if anchor_utc else None,
        "deadline_local": to_local(deadline, tzname).isoformat() if deadline else None,
        "deadline_utc": deadline.isoformat() if deadline else None,
        "missing_evidence": [e["key"] for e in evidence if not e["present"]],
        "evidence": evidence,
        "basis": basis,
    }


def _eval_delay(events, promise, window, tzname, calendar_code, cutoff, as_of) -> dict:
    ev_order = _evidence_item("order", True)
    ev_promise = _evidence_item(
        "promise", promise is not None,
        "" if promise is not None else "订单未提供承诺送达时限",
    )
    signed = events.get(C.SIGNED)
    ev_signed = _evidence_item(
        "signed", signed is not None,
        to_local(signed.effective_time, tzname).isoformat() if signed else "",
    )
    evidence = [ev_order, ev_promise, ev_signed]

    if promise is None:
        return _claim_result(C.DELAY, window, None, "indeterminate", tzname,
                             calendar_code, cutoff, evidence,
                             ["缺少承诺送达时限，延迟类理赔暂不可判定"])

    promise_local = to_local(promise, tzname)
    if signed is not None:
        signed_local = to_local(signed.effective_time, tzname)
        if signed.effective_time > promise:
            cr = _claim_result(
                C.DELAY, window, signed.effective_time, "filable", tzname,
                calendar_code, cutoff, evidence,
                [f"实际签收 {signed_local.strftime('%Y-%m-%d %H:%M:%S %Z')} 晚于承诺时限 "
                 f"{promise_local.strftime('%Y-%m-%d %H:%M:%S %Z')}，构成延迟送达"],
            )
        else:
            cr = _claim_result(
                C.DELAY, window, None, "not_applicable", tzname,
                calendar_code, cutoff, evidence,
                [f"实际签收 {signed_local.strftime('%Y-%m-%d %H:%M:%S %Z')} 未晚于承诺时限 "
                 f"{promise_local.strftime('%Y-%m-%d %H:%M:%S %Z')}，不构成延迟送达"],
            )
    elif as_of < promise:
        cr = _claim_result(
            C.DELAY, window, None, "pending", tzname,
            calendar_code, cutoff, evidence,
            [f"截至 {to_local(as_of, tzname).strftime('%Y-%m-%d %H:%M:%S %Z')} 尚未签收，"
             f"承诺时限 {promise_local.strftime('%Y-%m-%d %H:%M:%S %Z')} 未到，需等待结果"],
        )
    else:
        # 承诺时限已过且未签收 => 构成延迟；以承诺时限为起算锚点
        cr = _claim_result(
            C.DELAY, window, promise, "filable", tzname,
            calendar_code, cutoff, evidence,
            [f"承诺时限 {promise_local.strftime('%Y-%m-%d %H:%M:%S %Z')} 已过且仍无签收扫描，"
             "构成延迟送达"],
        )

    # 统一做截止状态收敛
    return _expire_if_needed(cr, as_of, tzname, calendar_code, cutoff)


def _eval_simple(claim, trigger_type, events, window, tzname, calendar_code,
                 cutoff, as_of, extra_reason) -> dict:
    evidence = [_evidence_item("order", True)]
    trigger = events.get(trigger_type)
    evidence.append(_evidence_item(
        trigger_type, trigger is not None,
        to_local(trigger.effective_time, tzname).isoformat() if trigger else "",
    ))
    if trigger is None:
        return _claim_result(claim, window, None, "pending", tzname, calendar_code,
                             cutoff, evidence, [extra_reason["no_trigger"]])
    cr = _claim_result(
        claim, window, trigger.effective_time, "filable", tzname, calendar_code,
        cutoff, evidence, [extra_reason["triggered"]],
    )
    return _expire_if_needed(cr, as_of, tzname, calendar_code, cutoff)


def _expire_if_needed(cr, as_of, tzname, calendar_code, cutoff) -> dict:
    """已构成/已触发的理赔，过截止即不可申请。"""
    if cr["status"] != "filable" or not cr["deadline_utc"]:
        return cr
    deadline = parse_dt(cr["deadline_utc"])
    if as_of > deadline:
        local_dl = to_local(deadline, tzname)
        cr["status"] = "not_filable"
        cr["basis"].append(
            f"当前时间已晚于截止时间 {local_dl.strftime('%Y-%m-%d %H:%M:%S %Z')}，不可再申请"
        )
    else:
        cr["basis"].append("当前时间在申请时限内，可申请")
    return cr


def evaluate(
    order: dict,
    event_rows,
    policy: dict,
    as_of: datetime | None = None,
    generation_info: dict | None = None,
) -> dict:
    """执行完整判定。

    order: orders 行（dict 化）
    event_rows: 截至当前代（含）的事件行列表，按 arrived_at 排序
    policy: 生效政策 body
    as_of: 判定基准时刻（UTC），默认当前时间
    """
    as_of = (as_of or utc_now()).astimezone(UTC)
    tzname = order["timezone"]
    calendar_code = order["calendar_code"]
    cutoff = policy.get("default_cutoff", "23:59:59")
    promise = parse_dt(order["promise_by"]) if order["promise_by"] else None

    events, amendments = materialize(event_rows)

    rules = {r["claim_type"]: r for r in policy["claims"]}
    claims = [
        _eval_delay(events, promise, rules[C.DELAY]["window"], tzname, calendar_code,
                    cutoff, as_of),
        _eval_simple(C.DAMAGE, C.DAMAGE_REPORTED, events, rules[C.DAMAGE]["window"],
                     tzname, calendar_code, cutoff, as_of,
                     {"no_trigger": "尚无破损申报扫描，破损类理赔暂不可判定",
                      "triggered": "存在破损申报扫描，构成破损理赔事由"}),
        _eval_simple(C.LOSS, C.LOST_CONFIRMED, events, rules[C.LOSS]["window"],
                     tzname, calendar_code, cutoff, as_of,
                     {"no_trigger": "尚无丢件确认扫描，丢失类理赔暂不可判定",
                      "triggered": "存在丢件确认扫描，构成丢失理赔事由"}),
    ]
    statuses = {c["status"] for c in claims}
    if "filable" in statuses:
        overall = "filable"
    elif statuses <= {"not_applicable"}:
        overall = "closed"
    elif "pending" in statuses or "indeterminate" in statuses:
        overall = "pending"
    else:
        overall = "not_filable"

    filable = [c for c in claims if c["status"] == "filable"]
    deadlines = [parse_dt(c["deadline_utc"]) for c in filable if c["deadline_utc"]]
    earliest = min(deadlines) if deadlines else None

    basis = [
        f"适用政策版本：{policy['version']}（订单于 {to_local(parse_dt(order['created_at']), tzname).isoformat()} 创建）",
        f"判定基准时刻：{to_local(as_of, tzname).isoformat()}",
    ]
    if amendments:
        basis.append("本次判定已纳入迟到更正事件：")
        basis.extend(f"  - {a}" for a in amendments)
    gen = generation_info or {}
    if gen.get("gen_no", 1) > 1:
        basis.append(f"本判定为第 {gen['gen_no']} 代判定，归档于第 {gen.get('supersedes_gen')} 代的结果不被改写")

    return {
        "order_id": order["order_id"],
        "policy_version": policy["version"],
        "timezone": tzname,
        "calendar_code": calendar_code,
        "as_of_utc": as_of.isoformat(),
        "generation_no": gen.get("gen_no", 1),
        "overall_status": overall,
        "eligible_to_file": overall == "filable",
        "earliest_deadline_local": to_local(earliest, tzname).isoformat() if earliest else None,
        "earliest_deadline_utc": earliest.isoformat() if earliest else None,
        "missing_evidence": sorted({k for c in claims if c["status"] == "filable"
                                    for k in c["missing_evidence"]}),
        "claims": claims,
        "basis": basis,
    }
