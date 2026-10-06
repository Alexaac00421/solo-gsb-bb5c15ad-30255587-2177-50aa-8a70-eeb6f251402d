"""FastAPI 入口：快递理赔时限判定 HTTP API。"""
import json
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db
from .errors import AppError
from .schemas import (CalendarDiagnoseRequest, ClaimSubmitRequest,
                      CreateOrderRequest, EventBatchRequest, EventIn,
                      EventPreviewRequest)
from .services import (archive_determination, create_order,
                       determination_history, diagnose_calendar, get_voucher,
                       ingest_events, list_events, list_vouchers,
                       live_determination, preview_events, submit_claim)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.connect()
    yield


app = FastAPI(
    title="快递理赔时限判定服务",
    version="1.2.0",
    description="按订单创建时生效政策版本判定理赔申请时限；支持时区/节假日、事件去重、"
                "迟到更正与幂等理赔申请凭证；并提供只读的节假日表工作日诊断。",
    lifespan=lifespan,
)


# ------------------------------------------------------------ 统一错误格式

@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    details = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"] if x not in ("body",))
        details.append({
            "field": loc or "body",
            "type": err["type"],
            "message": err["msg"],
        })
    return JSONResponse(
        status_code=422,
        content={"error": {
            "code": "INVALID_REQUEST",
            "message": "请求参数校验失败（字段缺失或时间缺少时区等）",
            "details": details,
        }},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------ 订单

@app.post("/api/v1/orders", status_code=201)
def api_create_order(req: CreateOrderRequest):
    return create_order(req)


@app.get("/api/v1/orders/{order_id}")
def api_get_order(order_id: str):
    from .services import require_order
    return require_order(order_id)


# ------------------------------------------------------------ 事件

@app.post("/api/v1/orders/{order_id}/events")
def api_ingest_event(order_id: str, req: EventIn):
    return ingest_events(order_id, [req])


@app.post("/api/v1/orders/{order_id}/events/batch")
def api_ingest_batch(order_id: str, req: EventBatchRequest):
    return ingest_events(order_id, req.events)


@app.post("/api/v1/orders/{order_id}/events/preview")
def api_preview_events(order_id: str, req: EventPreviewRequest):
    """无写入预演：在当前最新代事件视图上模拟整批摄取（含更正规则），
    返回逐项校验结果、首个失败位置、模拟判定与判定依据差异；
    不写事件、不开新代、不改归档快照。已归档订单按新一代结果计算。"""
    return preview_events(order_id, req.events, as_of=req.as_of)


@app.get("/api/v1/orders/{order_id}/events")
def api_list_events(order_id: str):
    return list_events(order_id)


# ------------------------------------------------------------ 判定

def _parse_as_of(v: str | None) -> datetime | None:
    if not v:
        return None
    from .engine import parse_dt
    try:
        return parse_dt(v)
    except Exception:
        raise AppError("INVALID_AS_OF", "as_of 必须是带时区偏移的 ISO8601 时间", 422)


@app.get("/api/v1/orders/{order_id}/determination")
def api_live(order_id: str, as_of: str | None = None):
    """实时判定（最新一代未归档则现算；已归档则返回不可变快照）。"""
    return live_determination(order_id, as_of=_parse_as_of(as_of))


@app.post("/api/v1/orders/{order_id}/determination/archive")
def api_archive(order_id: str, as_of: str | None = None):
    """归档当前代判定。归档后迟到事件只会另开新一代，不改写本快照。"""
    return archive_determination(order_id, as_of=_parse_as_of(as_of))


@app.get("/api/v1/orders/{order_id}/determinations")
def api_history(order_id: str):
    return determination_history(order_id)


# ------------------------------------------------------------ 理赔申请（凭证）

@app.post("/api/v1/orders/{order_id}/claims", status_code=201)
def api_submit_claim(
    order_id: str,
    req: ClaimSubmitRequest,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key",
        description="幂等键，也可在请求体 idempotency_key 中提供；两处不一致返回 422"),
):
    """按订单和理赔类型提交申请。

    以服务实际接收时间（body.received_at，缺省当前 UTC）实时核验当前代
    （当前代已归档也重算，不把归档快照当实时资格）；仅该类型 filable 时受理，
    返回申请编号、受理时间、判定代号、适用政策、截止时间与当时判定依据。
    同订单同键同类型重试返回原凭证（200, replayed=true）；换类型 409；
    同订单同类型已有凭证 409；其余非 filable 状态返回可区分拒绝，不生成凭证。
    """
    key = req.idempotency_key
    if idempotency_key is not None and idempotency_key != key:
        raise AppError(
            "IDEMPOTENCY_KEY_MISMATCH",
            "请求头 Idempotency-Key 与请求体 idempotency_key 不一致",
            422,
            {"header_idempotency_key": idempotency_key,
             "body_idempotency_key": key},
        )
    voucher, replayed = submit_claim(order_id, req.claim_type, key,
                                     received_at=req.received_at)
    return JSONResponse(status_code=200 if replayed else 201, content=voucher)


@app.get("/api/v1/orders/{order_id}/claims")
def api_list_claims(order_id: str):
    """列出该订单全部已受理凭证（凭证及其判定依据不可变）。"""
    return list_vouchers(order_id)


@app.get("/api/v1/claims/{claim_no}")
def api_get_claim(claim_no: str):
    """按申请编号查询凭证及其所依据的判定代号与冻结判定。"""
    return get_voucher(claim_no)


# ------------------------------------------------------------ 基础数据

@app.get("/api/v1/policies")
def api_policies():
    rows = db.q("SELECT version, valid_from, valid_to, body_json FROM policies ORDER BY valid_from")
    return {"policies": [
        {"version": r["version"], "valid_from": r["valid_from"],
         "valid_to": r["valid_to"], "body": json.loads(r["body_json"])}
        for r in rows
    ]}


@app.get("/api/v1/calendars/{calendar_code}")
def api_calendar(calendar_code: str):
    rows = db.q(
        "SELECT date, kind, name FROM calendars WHERE calendar_code = ? ORDER BY date",
        (calendar_code,),
    )
    if not rows:
        raise AppError("CALENDAR_NOT_FOUND", f"节假日表不存在: {calendar_code}", 404)
    return {"calendar_code": calendar_code, "days": [dict(r) for r in rows]}


@app.post("/api/v1/calendars/diagnose")
def api_diagnose_calendar(req: CalendarDiagnoseRequest):
    """节假日表工作日诊断（只读）：自起始时刻当地日期次日起逐日列出工作/休息
    判定、补班/放假声明、周末默认规则、累计工作日数与跳过原因，直到第 N 个
    工作日，返回当地与 UTC 截止时刻。不修改日历、政策、订单或凭证。

    - 起始时间必须带时区偏移，否则 422；
    - 日历不存在 404，时区无法识别 422；
    - 工作日数须为 1～60，截止钟点须为合法 HH:MM(:SS)，否则 422。
    """
    return diagnose_calendar(req)
