"""测试夹具：每个用例使用独立临时数据库并播种。"""
import os
import tempfile

import pytest

_tmp = tempfile.TemporaryDirectory()
os.environ["CLAIM_DATA_DIR"] = _tmp.name
os.environ["CLAIM_DB_PATH"] = os.path.join(_tmp.name, "test.db")

from app import db  # noqa: E402
from app.db import connect  # noqa: E402
from app.seed_data import seed  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    if os.path.exists(os.environ["CLAIM_DB_PATH"]):
        os.remove(os.environ["CLAIM_DB_PATH"])
    db.reset_for_tests()
    connect()
    seed()
    yield


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_order(client, order_id="O1", created_at="2025-02-05T09:00:00+08:00",
               promise_by="2025-02-10T18:00:00+08:00", calendar_code="CN",
               timezone="Asia/Shanghai"):
    payload = {"order_id": order_id, "created_at": created_at, "timezone": timezone,
               "calendar_code": calendar_code, "promise_by": promise_by, "weight_kg": 2.5}
    r = client.post("/api/v1/orders", json=payload)
    assert r.status_code == 201, r.text
    return r.json()
