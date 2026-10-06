"""统一的业务错误类型与错误码。"""


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


ERR_ORDER_NOT_FOUND = ("ORDER_NOT_FOUND", 404)
ERR_EVENT_ORDER_INVALID = ("EVENT_ORDER_INVALID", 422)
ERR_CORRECTION_TARGET_NOT_FOUND = ("CORRECTION_TARGET_NOT_FOUND", 422)
ERR_POLICY_NOT_FOUND = ("POLICY_NOT_FOUND", 422)
ERR_CALENDAR_NOT_FOUND = ("CALENDAR_NOT_FOUND", 422)
ERR_ALREADY_ARCHIVED = ("ALREADY_ARCHIVED", 409)

# 理赔申请
ERR_VOUCHER_NOT_FOUND = ("VOUCHER_NOT_FOUND", 404)
ERR_CLAIM_TYPE_INVALID = ("CLAIM_TYPE_INVALID", 422)
ERR_IDEMPOTENCY_KEY_REQUIRED = ("IDEMPOTENCY_KEY_REQUIRED", 422)
# 按申请受理时状态区分的业务拒绝原因（不生成凭证）
ERR_CLAIM_PENDING = ("CLAIM_PENDING", 422)               # 触发事件尚未出现
ERR_CLAIM_DEADLINE_PASSED = ("CLAIM_DEADLINE_PASSED", 422)  # 已过当地截止时刻
ERR_CLAIM_NOT_APPLICABLE = ("CLAIM_NOT_APPLICABLE", 422)    # 理赔事由不成立
ERR_CLAIM_INDETERMINATE = ("CLAIM_INDETERMINATE", 422)      # 证据不足不可判定
# 幂等/唯一性冲突
ERR_CLAIM_CONFLICT = ("CLAIM_CONFLICT", 409)                 # 同订单同键换类型
ERR_CLAIM_ALREADY_ACCEPTED = ("CLAIM_ALREADY_ACCEPTED", 409)  # 同订单同类型已有凭证

# 理赔待办快照（跨订单）
ERR_CURSOR_INVALID = ("CURSOR_INVALID", 422)      # 游标格式错误/指向不存在快照/页码越界
ERR_CURSOR_EXPIRED = ("CURSOR_EXPIRED", 410)      # 游标自快照创建起 30 分钟过期
