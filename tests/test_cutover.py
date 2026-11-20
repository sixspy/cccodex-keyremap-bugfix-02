
import threading

import pytest
from fastapi.testclient import TestClient

from app import app, connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _admin(c):
    r = c.post("/api/login", json={"username": "admin", "password": "123456"})
    return {"Authorization": "Bearer " + r.json()["token"]}


V12 = {
    "version": "m-2026.12",
    "effective_from": "2026-11-20",
    "mappings": [
        {"old_key": "OLD-A1", "new_key": "NEW-101", "kind": "一对一"},
        {"old_key": "OLD-B1", "new_key": "NEW-201", "kind": "多对一"},
    ],
}


def _effective(c, h):
    vers = c.get("/api/map-versions", headers=h).json()["items"]
    return [v["version"] for v in vers if v["status"] == "生效"]


def test_cutover_single_effective_five_surfaces_agree():
    """双生效失败测：切版后五处同态，仅一生效。"""
    c = TestClient(app)
    h = _admin(c)
    r = c.post("/api/cutover", headers=h, json=V12)
    assert r.status_code == 200, r.text

    # 映册列表：仅一生效且为新版，旧版已关闭
    assert _effective(c, h) == ["m-2026.12"]

    # 重映结果：落新版键
    rr = c.post("/api/remap", headers=h, json={"old_key": "OLD-A1"})
    assert rr.status_code == 200
    assert rr.json()["entity_key"] == "NEW-101"
    assert rr.json()["version"] == "m-2026.12"

    # 正式实体：带新版
    f = c.get("/api/formal", headers=h).json()["items"]
    assert f[0]["entity_key"] == "NEW-101" and f[0]["version"] == "m-2026.12"

    # 互证页：单生效且一致
    rec = c.get("/api/reconcile", headers=h).json()
    assert rec["single_effective"] is True
    assert rec["effective"] == "m-2026.12"
    assert rec["consistent"] is True

    # 切版流水：已切
    led = c.get("/api/cutovers", headers=h).json()["items"]
    assert any(x["target_version"] == "m-2026.12" and x["result"] == "已切" for x in led)


def test_cutover_heals_seeded_double_effective():
    """双生效失败测（复现数据）：双生效并存现场下切版，切完仅一生效。"""
    with connect() as c:
        c.executescript("""
        INSERT INTO map_versions(version,status,effective_from) VALUES ('m-2026.12','生效','2026-11-15');
        INSERT INTO mappings(version,old_key,new_key,kind) VALUES ('m-2026.12','OLD-A1','NEW-101','一对一');
        """)
        c.commit()
    c = TestClient(app)
    h = _admin(c)
    assert c.get("/api/reconcile", headers=h).json()["single_effective"] is False  # 假绿现场

    r = c.post("/api/cutover", headers=h, json={
        "version": "m-2026.13", "effective_from": "2026-11-20",
        "mappings": [{"old_key": "OLD-A1", "new_key": "NEW-102", "kind": "一对一"}]})
    assert r.status_code == 200, r.text
    assert _effective(c, h) == ["m-2026.13"]
    rec = c.get("/api/reconcile", headers=h).json()
    assert rec["single_effective"] is True and rec["consistent"] is True


def test_cutover_failure_rolls_back_both_versions():
    """甲路径：事务中途失败则两版回滚——旧版仍生效，新版与映射不留痕。"""
    c = TestClient(app)
    h = _admin(c)
    bad = {
        "version": "m-2026.12",
        "effective_from": "2026-11-20",
        "mappings": [
            {"old_key": "OLD-D1", "new_key": "NEW-300", "kind": "一对一"},
            {"old_key": "OLD-D1", "new_key": "NEW-301", "kind": "一对一"},  # 同版旧键重复，事务中途炸
        ],
    }
    r = c.post("/api/cutover", headers=h, json=bad)
    assert r.status_code in (400, 409), r.text
    assert _effective(c, h) == ["m-2026.11"]  # 旧版回滚仍生效
    vers = c.get("/api/map-versions", headers=h).json()["items"]
    assert all(v["version"] != "m-2026.12" for v in vers)  # 新版回滚不留痕
    ms = c.get("/api/mappings", headers=h).json()["items"]
    assert all(m["new_key"] not in ("NEW-300", "NEW-301") for m in ms)  # 映射回滚


def test_concurrent_cutover_same_target_records_contention():
    """抢切争用失败测：并发双切同一目标版，排队占锁，后到只记争用。"""
    h = _admin(TestClient(app))
    barrier = threading.Barrier(2)
    results = {}

    def hit(tag):
        cli = TestClient(app)
        barrier.wait(timeout=10)
        results[tag] = cli.post("/api/cutover", headers=h, json=V12).status_code

    ts = [threading.Thread(target=hit, args=(i,)) for i in (1, 2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)

    assert sorted(results.values()) == [200, 409]  # 一切一争用
    c = TestClient(app)
    assert _effective(c, h) == ["m-2026.12"]  # 仅一生效
    led = c.get("/api/cutovers", headers=h).json()["items"]
    assert sum(1 for x in led if x["target_version"] == "m-2026.12" and x["result"] == "已切") == 1
    assert sum(1 for x in led if x["target_version"] == "m-2026.12" and x["result"] == "争用") == 1
