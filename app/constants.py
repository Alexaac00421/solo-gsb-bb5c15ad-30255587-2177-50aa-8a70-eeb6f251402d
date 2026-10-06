"""领域常量：扫描事件类型、更正动作、理赔类型的中文标签。"""

# 基础扫描事件
CREATED = "created"
SIGNED = "signed"                 # 签收
DAMAGE_REPORTED = "damage_reported"
LOST_CONFIRMED = "lost_confirmed"

# 更正动作
AMEND = "amend"                   # 修正既有事件的时间
CANCEL = "cancel"                 # 撤销既有事件
INSERT = "insert"                 # 补录一个本应更早发生的事件

BASE_EVENT_TYPES = {CREATED, SIGNED, DAMAGE_REPORTED, LOST_CONFIRMED}
CORRECTION_ACTIONS = {AMEND, CANCEL, INSERT}
INSERTABLE_EVENT_TYPES = {SIGNED, DAMAGE_REPORTED, LOST_CONFIRMED}

# 理赔类型
DELAY = "delay"
DAMAGE = "damage"
LOSS = "loss"
ALL_CLAIMS = (DELAY, DAMAGE, LOSS)

EVENT_LABELS = {
    CREATED: "下单",
    SIGNED: "签收",
    DAMAGE_REPORTED: "破损申报",
    LOST_CONFIRMED: "丢件确认",
}

CLAIM_LABELS = {
    DELAY: "延迟送达",
    DAMAGE: "货物破损",
    LOSS: "货物丢失",
}

# 证据键 -> 展示名
EVIDENCE_LABELS = {
    "order": "有效订单",
    "promise": "承诺送达时限",
    "signed": "签收扫描",
    "damage_reported": "破损申报",
    "lost_confirmed": "丢件确认",
}
