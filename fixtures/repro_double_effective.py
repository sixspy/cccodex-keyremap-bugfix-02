"""复现双生效假绿（本地夹具，SQLite）。

拷贝 data/app.db 到 fixtures/repro.db，落 fixtures/double_effective.sql
摆出双生效并存现场，再登录管理员走现有接口逐面取证：
映册列表 / 重映结果 / 正式实体 / 互证页 / 切版接口。

用法：.venv/bin/python fixtures/repro_double_effective.py
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCRATCH = ROOT / "fixtures" / "repro.db"


def main() -> None:
    if SCRATCH.exists():
        SCRATCH.unlink()
    shutil.copy(ROOT / "data" / "app.db", SCRATCH)
    appmod.DB_PATH = SCRATCH
    appmod.init_db()

    sql = (ROOT / "fixtures" / "double_effective.sql").read_text(encoding="utf-8")
    with appmod.connect() as c:
        c.executescript(sql)

    c = TestClient(appmod.app)
    tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    h = {"Authorization": "Bearer " + tok}

    print("== 现场：双生效并存数据已落库 ==")
    vers = c.get("/api/map-versions", headers=h).json()["items"]
    eff = [v["version"] for v in vers if v["status"] == "生效"]
    print(f"[映册列表] 生效版本数={len(eff)} -> {eff}")

    ms = c.get("/api/mappings", headers=h).json()["items"]
    lands = sorted({m["new_key"] for m in ms if m["old_key"] == "OLD-A1" and m["version"] in eff})
    print(f"[同旧键] OLD-A1 在生效版内可落新键 -> {lands}（{'两套新键' if len(lands) > 1 else '单套'}）")

    r = c.post("/api/remap", headers=h, json={"old_key": "OLD-A1"})
    print(f"[重映结果] POST /api/remap OLD-A1 -> {r.status_code} {r.json()}")

    f = c.get("/api/formal", headers=h).json()["items"]
    a1 = [(x["entity_key"], x["version"]) for x in f if x["mapped_from"] == "OLD-A1"]
    print(f"[正式实体] OLD-A1 已落正式区 -> {a1}")

    rec = c.get("/api/reconcile", headers=h).json()
    print(f"[互证页] single_effective={rec['single_effective']} consistent={rec['consistent']} effective={rec['effective']}")

    r = c.post("/api/cutover", headers=h, json={
        "version": "m-2026.13", "effective_from": "2026-11-20",
        "mappings": [{"old_key": "OLD-A1", "new_key": "NEW-102", "kind": "一对一"}]})
    print(f"[切版接口] POST /api/cutover m-2026.13 -> {r.status_code} {r.json()}")

    vers = c.get("/api/map-versions", headers=h).json()["items"]
    eff = [v["version"] for v in vers if v["status"] == "生效"]
    print(f"[映册列表] 切版后生效版本数={len(eff)} -> {eff}")

    rec = c.get("/api/reconcile", headers=h).json()
    print(f"[互证页] 切版后 single_effective={rec['single_effective']} consistent={rec['consistent']}")

    led = c.get("/api/cutovers", headers=h)
    print(f"[切版流水] GET /api/cutovers -> {led.status_code} {led.json() if led.status_code == 200 else led.text[:60]}")

    if len(eff) != 1:
        print(f"== 假绿复现：接口一路成功，生效版本却有 {len(eff)} 个，单生效互证失败 ==")
    else:
        print(f"== 仅一生效（{eff[0]}）：双生效假绿已切断 ==")


if __name__ == "__main__":
    main()
