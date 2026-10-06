"""端到端调用示例（仅使用标准库 urllib）：

  python examples/call_api.py            # 默认 http://localhost:8000
  BASE_URL=http://host:port python examples/call_api.py

演示链路：
1. 创建 2025 版政策下的订单（承诺 2/10 18:00 送达）；
2. 提交"迟到签收"扫描事件 -> 查询判定（延迟可申请，截止按工作日跳过春节）；
3. 归档判定；
4. 重放同一事件 -> 不重复计入；倒序事件 -> 明确错误；
5. 无写入预演迟到的更正事件 -> 模拟新一代结果与判定差异，系统状态不变；
6. 正式提交该更正事件 -> 产生第 2 代新判定，与预演结果一致，第 1 代快照不变；
7. 查询判定历史；
8. 理赔申请：按接收时间核验受理并生成不可变凭证（含幂等重试/换类型冲突/
   同类型重复受理/非 filable 拒绝/凭证查询）；
9. 节假日表工作日诊断：只读逐日列出工作/休息判定与截止时刻（含非法参数拒绝）；
10. 跨订单理赔待办快照：首请求按核对时刻冻结完整结果并分页返回，
    游标翻页（30 分钟过期），含非法参数/无效游标拒绝示例。
"""
import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
ORDER_ID = "DEMO-20250205-001"


def call(method: str, path: str, payload=None, params=None):
    url = BASE + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            print(f"[{method} {path}] -> {resp.status}")
            return body
    except urllib.error.HTTPError as e:
        print(f"[{method} {path}] -> {e.code} 错误：")
        print(json.dumps(json.loads(e.read().decode()), ensure_ascii=False, indent=2))
        return None


def show(title, body, keys=("overall_status", "eligible_to_file", "generation_no",
                            "earliest_deadline_local", "missing_evidence")):
    print(f"\n===== {title} =====")
    if body:
        print(json.dumps({k: body.get(k) for k in keys if k in body},
                         ensure_ascii=False, indent=2))


def main():
    print(f"目标服务：{BASE}")

    # 幂等：演示订单已存在则删除无接口，直接换 ID
    order_payload = {
        "order_id": ORDER_ID,
        "created_at": "2025-02-05T09:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "calendar_code": "CN",
        "promise_by": "2025-02-10T18:00:00+08:00",
        "weight_kg": 2.5,
    }
    r = call("POST", "/api/v1/orders", order_payload)
    if r is None:
        print("订单创建失败，请确认服务已启动且已初始化（docker compose up 会自动初始化）")
        return
    print("适用政策版本：", r["created_policy"])

    signed_event = {
        "event_id": "EV-SIGNED-01",
        "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",    # 扫描发生时间（晚于承诺时限）
        "arrived_at": "2025-02-11T10:05:00+08:00",    # 到达服务时间（顺序以此为准）
    }
    call("POST", f"/api/v1/orders/{ORDER_ID}/events", signed_event)

    det = call("GET", f"/api/v1/orders/{ORDER_ID}/determination",
               params={"as_of": "2025-02-20T12:00:00+08:00"})
    show("实时判定：延迟送达，10 个工作日截止（2/11 起算，跳过春节后为 2/25 23:59:59）", det)
    if det:
        print("\n延迟类判定依据：")
        for line in det["claims"][0]["basis"]:
            print("  -", line)

    archived = call("POST", f"/api/v1/orders/{ORDER_ID}/determination/archive",
                    params={"as_of": "2025-02-20T12:00:00+08:00"})
    show("归档第 1 代判定", archived)

    dup = call("POST", f"/api/v1/orders/{ORDER_ID}/events", signed_event)
    show("重放同一事件：accepted=0、duplicates 记录", dup,
         keys=("accepted", "duplicates", "generation_no"))

    bad = {"events": [
        {"event_id": "EV-LATE", "event_type": "lost_confirmed",
         "event_time": "2025-02-15T09:00:00+08:00",
         "arrived_at": "2025-02-09T08:00:00+08:00"},  # 早于已接受的 2/11
    ]}
    print("\n===== 倒序事件提交：期望 422 EVENT_ORDER_INVALID =====")
    call("POST", f"/api/v1/orders/{ORDER_ID}/events/batch", bad)

    correction = {
        "event_id": "EV-CORR-01",
        "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",    # 修正后的签收时间（未迟到）
        "arrived_at": "2025-02-21T08:00:00+08:00",    # 晚于归档 -> 迟到更正
        "correction_action": "amend",
        "target_event_id": "EV-SIGNED-01",
        "note": "网点补传准确签收时间",
    }

    # 无写入预演：正式提交前，先模拟这批更正事件的影响（不落库、不开新代）
    preview = call("POST", f"/api/v1/orders/{ORDER_ID}/events/preview",
                   {"as_of": "2025-02-21T12:00:00+08:00", "events": [correction]})
    print("\n===== 无写入预演：模拟新一代结果与判定差异 =====")
    if preview:
        sim = preview["simulation"]
        print(f"  校验通过={preview['valid']}，首个失败位置={preview['first_failure_index']}")
        print(f"  模拟代=第 {sim['generation_no']} 代（would_reopen={sim['would_reopen']}，"
              f"归档快照不受影响）")
        for c in preview["diff"]["claims"]:
            if c["changed"]:
                print(f"  {c['claim_type']}: {c['status_before']} -> {c['status_after']}")
        print("  判定依据新增：")
        for line in preview["diff"]["basis_added"]:
            print("   +", line)

    reopened = call("POST", f"/api/v1/orders/{ORDER_ID}/events", correction)
    show("正式提交迟到更正：自动开启第 2 代", reopened,
         keys=("accepted", "reopened", "generation_no", "note"))

    det2 = call("GET", f"/api/v1/orders/{ORDER_ID}/determination")
    show("第 2 代判定：延迟不再成立（not_applicable），与预演一致，旧归档未被改写", det2)

    hist = call("GET", f"/api/v1/orders/{ORDER_ID}/determinations")
    print("\n===== 判定历史 =====")
    if hist:
        for g in hist["generations"]:
            status = g.get("overall_status", "（未归档）")
            print(f"  第 {g['generation_no']} 代 | archived={g['archived']} | "
                  f"状态={status} | supersedes={g['supersedes_generation']}")

    claim_demo()
    calendar_diagnose_demo()
    todo_snapshot_demo()

    print("\n示例完成。交互文档：" + BASE + "/docs")


def todo_snapshot_demo():
    """跨订单理赔待办快照：首请求冻结完整结果，游标翻页（30 分钟过期）。"""
    print("\n\n########## 理赔待办快照演示 ##########")
    as_of = "2025-02-20T12:00:00+08:00"          # 必须带时区偏移
    r = call("POST", "/api/v1/claim-todos", {"as_of": as_of, "page_size": 5})
    if r is None:
        return
    print(f"快照 {r['snapshot_id']} | 核对时刻(UTC) {r['as_of_utc']} | "
          f"总数 {r['total']} | 每页 {r['page_size']} | 共 {r['page_count']} 页 | "
          f"游标 {r['cursor_ttl_seconds'] // 60} 分钟内有效")

    def show_page(p):
        for e in p["entries"]:
            print(f"  {e['order_id']} | {e['claim_type']}({e['claim_name']}) | "
                  f"{e['status']} | 截止 {e['deadline_local']} | "
                  f"缺失证据 {e['missing_evidence']} | 凭证 {e['voucher_nos']} | "
                  f"可再次申请 {e['can_apply_again']}")

    print("-- 第 1 页 --")
    show_page(r)
    cursor = r["next_cursor"]
    while cursor:                                # 翻页期间结果固定不变
        p = call("GET", "/api/v1/claim-todos", params={"cursor": cursor})
        if p is None:
            break
        print(f"-- 第 {p['page']} 页 --")
        show_page(p)
        cursor = p["next_cursor"]

    # 非法参数示例：时刻无偏移 / 页大小越界 / 无效游标，均被明确拒绝
    print("\n--- 非法参数拒绝示例 ---")
    call("POST", "/api/v1/claim-todos",
         {"as_of": "2025-02-20T12:00:00", "page_size": 5})      # 缺偏移 -> 422
    call("POST", "/api/v1/claim-todos",
         {"as_of": as_of, "page_size": 101})                    # 越界 -> 422
    call("GET", "/api/v1/claim-todos", params={"cursor": "bad-cursor"})  # -> 422


def calendar_diagnose_demo():
    """节假日表工作日诊断（只读）：独立于订单，逐日解释第 N 个工作日如何得出。"""
    print("\n\n########## 节假日表工作日诊断演示 ##########")
    payload = {
        "calendar_code": "CN",
        "timezone": "Asia/Shanghai",
        "start_at": "2025-09-30T10:00:00+08:00",   # 必须带偏移；自当地日期次日起数
        "business_days": 10,                        # 1～60
        "cutoff": "18:00",                          # HH:MM 或 HH:MM:SS
    }
    r = call("POST", "/api/v1/calendars/diagnose", payload)
    if r is None:
        return
    print(f"日历 {r['calendar_code']}（声明日期 {r['calendar_declared_days']} 个）| "
          f"时区 {r['timezone']} | {r['count_basis']}")
    print(f"起始当地时刻 {r['start_at_local']}（UTC {r['start_at_utc']}），"
          f"自 {r['count_start_date']} 起逐日列出：")
    for e in r["entries"]:
        decl = f"[{e['declaration']['name']}]" if e["declaration"] else ""
        print(f"  {e['date']} {e['weekday']} {e['verdict']} "
              f"累计第 {e['business_day_count']} 个工作日 {decl} - {e['reason']}")
    s = r["summary"]
    print(f"汇总：列出 {s['days_listed']} 天，其中工作日 {s['business_days_counted']}、"
          f"休息 {s['rest_days_seen']}（放假 {s['declared_holidays_seen']}、"
          f"补班上班 {s['declared_makeup_workdays_seen']}）")
    print(f"第 10 个工作日截止：当地 {r['deadline_local']} | UTC {r['deadline_utc']}")
    print(f"只读：{r['note']}")

    # 非法参数示例：时间无偏移 / 日历不存在 / 钟点非法，均被明确拒绝
    print("\n--- 非法参数拒绝示例 ---")
    call("POST", "/api/v1/calendars/diagnose", {**payload, "start_at": "2025-09-30T10:00:00"})
    call("POST", "/api/v1/calendars/diagnose", {**payload, "calendar_code": "ZZ"})
    call("POST", "/api/v1/calendars/diagnose", {**payload, "business_days": 61})
    call("POST", "/api/v1/calendars/diagnose", {**payload, "cutoff": "18:60"})


def claim_demo():
    """理赔申请（凭证）全链路：独立订单，互不干扰。"""
    oid = "DEMO-CLAIM-20250205-001"
    print("\n\n########## 理赔申请（凭证）演示 ##########")
    r = call("POST", "/api/v1/orders", {
        "order_id": oid,
        "created_at": "2025-02-05T09:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "calendar_code": "CN",
        "promise_by": "2025-02-10T18:00:00+08:00",
        "weight_kg": 2.5,
    })
    if r is None:
        print("理赔演示订单创建失败（可能已存在于持久化数据中），跳过本段演示；"
              "重跑可换新 ORDER_ID")
        return
    call("POST", f"/api/v1/orders/{oid}/events", {
        "event_id": "EV-CLAIM-SIGNED-01",
        "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00",
    })

    # 1) 提交申请：以服务接收时间 2/20 实时核验 -> delay=filable，受理并出凭证
    payload = {
        "claim_type": "delay",
        "idempotency_key": "IDEMP-20250220-001",
        "received_at": "2025-02-20T12:00:00+08:00",   # 缺省则取服务当前 UTC
    }
    v = call("POST", f"/api/v1/orders/{oid}/claims", payload)
    print("\n--- 1) 首次申请（201，受理） ---")
    if v:
        print(f"  申请编号 {v['claim_no']}")
        print(f"  受理时间(UTC) {v['accepted_at_utc']} | 接收时间(UTC) {v['received_at_utc']}")
        print(f"  判定代号 {v['decision_code']} | 适用政策 {v['policy_version']}")
        print(f"  截止时间 {v['deadline_local']}（UTC {v['deadline_utc']}）")
        print("  当时判定依据：")
        for line in v["basis"]:
            print("   -", line)
        claim_no = v["claim_no"]
    else:
        claim_no = None

    # 2) 同订单同键同类型重试 -> 返回原凭证（200, replayed=true），不重新受理
    retry = call("POST", f"/api/v1/orders/{oid}/claims", payload)
    print("\n--- 2) 同键同类型重试（幂等，返回原凭证） ---")
    if retry:
        print(f"  replayed={retry['replayed']} claim_no={retry['claim_no']} "
              f"（与首次相同：{retry['claim_no'] == claim_no}）")

    # 3) 同订单同键换类型 -> 409 CLAIM_CONFLICT
    call("POST", f"/api/v1/orders/{oid}/claims",
         {**payload, "claim_type": "damage"})
    print("--- 3) 同键换类型：期望 409 CLAIM_CONFLICT（见上） ---")

    # 4) 同订单同类型换键 -> 409 CLAIM_ALREADY_ACCEPTED
    call("POST", f"/api/v1/orders/{oid}/claims",
         {**payload, "idempotency_key": "IDEMP-OTHER-KEY"})
    print("--- 4) 同类型另一键：期望 409 CLAIM_ALREADY_ACCEPTED（见上） ---")

    # 5) 非 filable 状态：damage 无触发事件 -> 422 CLAIM_PENDING，不生成凭证
    call("POST", f"/api/v1/orders/{oid}/claims",
         {"claim_type": "damage", "idempotency_key": "IDEMP-DAMAGE-001",
          "received_at": "2025-02-12T10:00:00+08:00"})
    print("--- 5) 破损类尚无触发事件：期望 422 CLAIM_PENDING，不生成凭证（见上） ---")

    # 6) 已过截止：接收时间 2/26（截止 2/25 23:59:59）-> 422 CLAIM_DEADLINE_PASSED
    call("POST", "/api/v1/orders", {
        "order_id": oid + "-LATE",
        "created_at": "2025-02-05T09:00:00+08:00",
        "timezone": "Asia/Shanghai", "calendar_code": "CN",
        "promise_by": "2025-02-10T18:00:00+08:00"})
    call("POST", f"/api/v1/orders/{oid}-LATE/events", {
        "event_id": "EV-CLAIM-LATE-SIGNED",
        "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    call("POST", f"/api/v1/orders/{oid}-LATE/claims",
         {"claim_type": "delay", "idempotency_key": "IDEMP-LATE-001",
          "received_at": "2025-02-26T08:00:00+08:00"})
    print("--- 6) 过截止后申请：期望 422 CLAIM_DEADLINE_PASSED（见上） ---")

    # 7) 按申请编号查询凭证（含冻结的判定代号与当时判定依据）
    if claim_no:
        got = call("GET", f"/api/v1/claims/{claim_no}")
        print("\n--- 7) 查询凭证 ---")
        if got:
            print(f"  {got['claim_no']} | {got['claim_type']} | "
                  f"decision_code={got['decision_code']} | immutable={got['immutable']}")
    lst = call("GET", f"/api/v1/orders/{oid}/claims")
    if lst:
        print(f"  订单 {oid} 已有凭证 {len(lst['vouchers'])} 张（同类型至多一张）")


if __name__ == "__main__":
    main()
