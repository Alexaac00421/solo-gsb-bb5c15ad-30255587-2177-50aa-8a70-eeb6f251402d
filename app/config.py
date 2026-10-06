"""运行期配置，全部可通过环境变量覆盖。"""
import os
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("CLAIM_DATA_DIR", "./data"))


def db_path() -> Path:
    env = os.environ.get("CLAIM_DB_PATH")
    return Path(env) if env else data_dir() / "claims.db"


# 每个政策版本也可在自身配置中声明截止钟点；此值为兜底默认值。
DEFAULT_CUTOFF = os.environ.get("CLAIM_DEFAULT_CUTOFF", "23:59:59")

# 理赔待办快照游标有效期（秒）：自快照创建起算，默认 30 分钟。
SNAPSHOT_CURSOR_TTL_SECONDS = int(os.environ.get("CLAIM_SNAPSHOT_CURSOR_TTL_SECONDS", "1800"))
