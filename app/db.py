"""SQLite 存储层（标准库 sqlite3，无 ORM）。

单连接 + 进程内互斥锁，足以承载单容器服务；更换 Postgres 时只需重写本模块。
时间戳统一以 UTC ISO8601 字符串入库。
"""
import sqlite3
import threading
from contextlib import contextmanager

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendars (
    calendar_code TEXT NOT NULL,
    date          TEXT NOT NULL,            -- YYYY-MM-DD（当地日历日期）
    kind          TEXT NOT NULL,            -- holiday | workday
    name          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (calendar_code, date)
);

CREATE TABLE IF NOT EXISTS policies (
    version     TEXT PRIMARY KEY,
    valid_from TEXT NOT NULL,               -- UTC ISO8601，含
    valid_to   TEXT,                        -- UTC ISO8601，不含；NULL 表示至今有效
    body_json  TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,          -- UTC ISO8601，决定适用政策版本
    timezone        TEXT NOT NULL,
    calendar_code   TEXT NOT NULL,
    promise_by      TEXT,                   -- UTC ISO8601，可空（缺失时延迟类不可判定）
    weight_kg       REAL,
    created_policy  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT NOT NULL REFERENCES orders(order_id),
    gen_no        INTEGER NOT NULL,
    opened_at     TEXT NOT NULL,            -- UTC，第一代=订单创建；后续=迟到事件到达
    archived      INTEGER NOT NULL DEFAULT 0,
    archived_at   TEXT,
    supersedes_gen INTEGER,                 -- 新一代开启时，被它替代的已归档代号
    determination_json TEXT,                -- 归档时固化的判定快照（不可变）
    UNIQUE (order_id, gen_no)
);

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES orders(order_id),
    gen_id       INTEGER NOT NULL REFERENCES generations(id),
    event_type   TEXT NOT NULL,
    event_time   TEXT NOT NULL,             -- UTC ISO8601，扫描发生时间
    arrived_at   TEXT NOT NULL,             -- UTC ISO8601，服务收到时间（顺序以此为准）
    is_correction INTEGER NOT NULL DEFAULT 0,
    correction_action TEXT,                 -- amend | cancel | insert
    target_event_id TEXT,                   -- 被更正的原事件
    note         TEXT NOT NULL DEFAULT '',
    prev_arrived_at TEXT                    -- 批次内上一事件到达时间，用于报错定位
);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id, arrived_at);

-- 理赔申请凭证：一经受理不可变（只插入，不更新、不删除）。
-- UNIQUE(order_id, idempotency_key)：同订单同幂等键重试 => 原凭证；换类型 => 冲突。
-- UNIQUE(order_id, claim_type)：同订单同类型至多一张凭证（并发不同键也受数据库层兜底）。
CREATE TABLE IF NOT EXISTS claim_vouchers (
    claim_no        TEXT PRIMARY KEY,           -- 申请编号（受理时生成，业务主键）
    order_id        TEXT NOT NULL REFERENCES orders(order_id),
    claim_type      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    received_at     TEXT NOT NULL,              -- UTC ISO8601，服务实际接收申请时间（判定基准）
    accepted_at     TEXT NOT NULL,              -- UTC ISO8601，受理时间
    decision_code   TEXT NOT NULL,              -- 判定代号（受理时恒为 FILABLE）
    policy_version  TEXT NOT NULL,              -- 下单锁定的政策版本号
    policy_json     TEXT NOT NULL,              -- 受理时适用政策全文快照（不可变）
    claim_json      TEXT NOT NULL,              -- 该类型判定快照：状态/锚点/截止/证据/依据
    determination_json TEXT NOT NULL,           -- 受理时整单判定快照（当时判定依据）
    UNIQUE (order_id, idempotency_key),
    UNIQUE (order_id, claim_type)
);
CREATE INDEX IF NOT EXISTS idx_vouchers_order ON claim_vouchers(order_id);

-- 跨订单理赔待办快照：首请求冻结完整结果（按订单号、理赔类型排序），
-- 游标翻页期间条目/总数/顺序不变；游标自快照创建起 30 分钟过期。
CREATE TABLE IF NOT EXISTS todo_snapshots (
    snapshot_id  TEXT PRIMARY KEY,          -- 快照编号（游标的组成部分）
    as_of        TEXT NOT NULL,             -- UTC ISO8601，核对时刻
    page_size    INTEGER NOT NULL,          -- 每页条数（1～100）
    total        INTEGER NOT NULL,          -- 条目总数（纳入订单数 × 3）
    entries_json TEXT NOT NULL,             -- 冻结的完整条目（不可变）
    created_at   TEXT NOT NULL,             -- UTC 快照创建时刻（游标过期起算点）
    expires_at   TEXT NOT NULL              -- created_at + 30 分钟
);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = config.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        _conn = conn
    return _conn


@contextmanager
def tx():
    """串行化所有写事务，保证事件到达顺序校验与插入的原子性。"""
    with _lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def q(sql: str, params=()):
    with _lock:
        return connect().execute(sql, params).fetchall()


def q1(sql: str, params=()):
    with _lock:
        return connect().execute(sql, params).fetchone()


def execute(sql: str, params=()):
    with tx() as conn:
        cur = conn.execute(sql, params)
        return cur


def reset_for_tests():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
