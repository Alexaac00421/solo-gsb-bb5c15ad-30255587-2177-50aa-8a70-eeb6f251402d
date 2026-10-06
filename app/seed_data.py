"""种子数据：政策版本与节假日表（幂等 upsert）。

政策版本说明（演示用，规则集中在 body_json，新增版本无需改代码）：
- v1（2023-01-01 ~ 2025-01-01 不含）：延迟 7 个工作日、破损 5 自然日、丢失 30 自然日
- v2（2025-01-01 起）：延迟 10 个工作日、破损 3 个工作日、丢失 15 个工作日
"""
import json

from .db import tx

POLICIES = [
    {
        "version": "v1",
        "valid_from": "2023-01-01T00:00:00+00:00",
        "valid_to": "2025-01-01T00:00:00+00:00",
        "body": {
            "version": "v1",
            "display_name": "快递理赔政策（2023 版）",
            "default_cutoff": "23:59:59",
            "claims": [
                {"claim_type": "delay", "window": {"days": 7, "day_basis": "business", "cutoff": "23:59:59"}},
                {"claim_type": "damage", "window": {"days": 5, "day_basis": "calendar", "cutoff": "23:59:59"}},
                {"claim_type": "loss", "window": {"days": 30, "day_basis": "calendar", "cutoff": "23:59:59"}},
            ],
        },
    },
    {
        "version": "v2",
        "valid_from": "2025-01-01T00:00:00+00:00",
        "valid_to": None,
        "body": {
            "version": "v2",
            "display_name": "快递理赔政策（2025 版）",
            "default_cutoff": "23:59:59",
            "claims": [
                {"claim_type": "delay", "window": {"days": 10, "day_basis": "business", "cutoff": "23:59:59"}},
                {"claim_type": "damage", "window": {"days": 3, "day_basis": "business", "cutoff": "23:59:59"}},
                {"claim_type": "loss", "window": {"days": 15, "day_basis": "business", "cutoff": "23:59:59"}},
            ],
        },
    },
]

# 国务院办公厅公布的 2024、2025 年节假日安排（holiday=放假，workday=调休上班）
CN_CALENDAR: dict[str, list[tuple[str, str, str]]] = {
    "2024": [
        ("2024-01-01", "holiday", "元旦"),
        ("2024-02-04", "workday", "春节调休上班"),
        ("2024-02-09", "holiday", "春节"),
        ("2024-02-10", "holiday", "春节"),
        ("2024-02-11", "holiday", "春节"),
        ("2024-02-12", "holiday", "春节"),
        ("2024-02-13", "holiday", "春节"),
        ("2024-02-14", "holiday", "春节"),
        ("2024-02-15", "holiday", "春节"),
        ("2024-02-16", "holiday", "春节"),
        ("2024-02-17", "holiday", "春节"),
        ("2024-02-18", "workday", "春节调休上班"),
        ("2024-04-04", "holiday", "清明节"),
        ("2024-04-05", "holiday", "清明节"),
        ("2024-04-06", "holiday", "清明节"),
        ("2024-04-07", "workday", "清明调休上班"),
        ("2024-05-01", "holiday", "劳动节"),
        ("2024-05-02", "holiday", "劳动节"),
        ("2024-05-03", "holiday", "劳动节"),
        ("2024-05-04", "holiday", "劳动节"),
        ("2024-05-05", "holiday", "劳动节"),
        ("2024-04-28", "workday", "劳动节调休上班"),
        ("2024-05-11", "workday", "劳动节调休上班"),
        ("2024-06-08", "holiday", "端午节"),
        ("2024-06-09", "holiday", "端午节"),
        ("2024-06-10", "holiday", "端午节"),
        ("2024-09-15", "holiday", "中秋节"),
        ("2024-09-16", "holiday", "中秋节"),
        ("2024-09-17", "holiday", "中秋节"),
        ("2024-09-14", "workday", "中秋调休上班"),
        ("2024-10-01", "holiday", "国庆节"),
        ("2024-10-02", "holiday", "国庆节"),
        ("2024-10-03", "holiday", "国庆节"),
        ("2024-10-04", "holiday", "国庆节"),
        ("2024-10-05", "holiday", "国庆节"),
        ("2024-10-06", "holiday", "国庆节"),
        ("2024-10-07", "holiday", "国庆节"),
        ("2024-09-29", "workday", "国庆调休上班"),
        ("2024-10-12", "workday", "国庆调休上班"),
    ],
    "2025": [
        ("2025-01-01", "holiday", "元旦"),
        ("2025-01-26", "workday", "春节调休上班"),
        ("2025-01-28", "holiday", "春节"),
        ("2025-01-29", "holiday", "春节"),
        ("2025-01-30", "holiday", "春节"),
        ("2025-01-31", "holiday", "春节"),
        ("2025-02-01", "holiday", "春节"),
        ("2025-02-02", "holiday", "春节"),
        ("2025-02-03", "holiday", "春节"),
        ("2025-02-04", "holiday", "春节"),
        ("2025-02-08", "workday", "春节调休上班"),
        ("2025-04-04", "holiday", "清明节"),
        ("2025-04-05", "holiday", "清明节"),
        ("2025-04-06", "holiday", "清明节"),
        ("2025-05-01", "holiday", "劳动节"),
        ("2025-05-02", "holiday", "劳动节"),
        ("2025-05-03", "holiday", "劳动节"),
        ("2025-05-04", "holiday", "劳动节"),
        ("2025-05-05", "holiday", "劳动节"),
        ("2025-04-27", "workday", "劳动节调休上班"),
        ("2025-05-31", "holiday", "端午节"),
        ("2025-06-01", "holiday", "端午节"),
        ("2025-06-02", "holiday", "端午节"),
        ("2025-10-01", "holiday", "国庆节、中秋节"),
        ("2025-10-02", "holiday", "国庆节、中秋节"),
        ("2025-10-03", "holiday", "国庆节、中秋节"),
        ("2025-10-04", "holiday", "国庆节、中秋节"),
        ("2025-10-05", "holiday", "国庆节、中秋节"),
        ("2025-10-06", "holiday", "国庆节、中秋节"),
        ("2025-10-07", "holiday", "国庆节、中秋节"),
        ("2025-10-08", "holiday", "国庆节、中秋节"),
        ("2025-09-28", "workday", "国庆调休上班"),
        ("2025-10-11", "workday", "国庆调休上班"),
    ],
}


def seed():
    with tx() as conn:
        for p in POLICIES:
            conn.execute(
                """INSERT INTO policies(version, valid_from, valid_to, body_json, created_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(version) DO UPDATE SET
                     valid_from=excluded.valid_from,
                     valid_to=excluded.valid_to,
                     body_json=excluded.body_json""",
                (p["version"], p["valid_from"], p["valid_to"],
                 json.dumps(p["body"], ensure_ascii=False), "2024-01-01T00:00:00+00:00"),
            )

        for _, days in CN_CALENDAR.items():
            for d, kind, name in days:
                conn.execute(
                    """INSERT INTO calendars(calendar_code, date, kind, name)
                       VALUES ('CN', ?, ?, ?)
                       ON CONFLICT(calendar_code, date) DO UPDATE SET
                         kind=excluded.kind, name=excluded.name""",
                    (d, kind, name),
                )
    return len(POLICIES), sum(len(v) for v in CN_CALENDAR.values())


if __name__ == "__main__":
    from .db import connect
    connect()
    pn, cn = seed()
    print(f"初始化完成：{pn} 个政策版本，CN 日历 {cn} 个特殊日期（节假日/补班日）")
