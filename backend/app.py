
import hashlib, secrets, sqlite3, threading
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mappings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          kind TEXT NOT NULL,
          UNIQUE(version, old_key)
        );
        CREATE TABLE IF NOT EXISTS formal_entities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cut_ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          target_version TEXT NOT NULL,
          actor TEXT NOT NULL,
          result TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)", ("m-2026.11","生效","2026-11-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11","OLD-A1","NEW-100","一对一"),
                ("m-2026.11","OLD-B1","NEW-200","多对一"),
                ("m-2026.11","OLD-B2","NEW-200","多对一"),
            ])
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                         ("NEW-100","OLD-A1","m-2026.11","2026-11-10T09:00:00"))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9","无映射","挂起"))

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}

@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}

class RemapIn(BaseModel):
    old_key: str

@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as c:
        effs = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC").fetchall()
        if not effs: raise HTTPException(404, "无生效映射版本")
        if len(effs) != 1: raise HTTPException(409, "双生效：生效版本不唯一，拒绝重映")
        ver = effs[0]
        m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?", (ver["version"], body.old_key)).fetchone()
        if not m:
            c.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", (body.old_key, "无映射", "挂起"))
            c.commit()
            raise HTTPException(409, "无映射，已进冲突队列")
        c.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                  (m["new_key"], body.old_key, ver["version"], now))
        return {"entity_key": m["new_key"], "version": ver["version"]}

# 并发双切同一目标版：排队占锁（每目标版一把锁，阻塞排队）
CUT_LOCKS: dict[str, threading.Lock] = {}
CUT_LOCKS_GUARD = threading.Lock()

def _cut_lock(version: str) -> threading.Lock:
    with CUT_LOCKS_GUARD:
        return CUT_LOCKS.setdefault(version, threading.Lock())

def _ledger(target: str, actor: str, result: str, detail: str) -> None:
    """切版流水独立落账（用于主事务之外的争用/拒写记录）。"""
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as c:
        c.execute("INSERT INTO cut_ledger(target_version,actor,result,detail,created_at) VALUES (?,?,?,?,?)",
                  (target, actor, result, detail, now))
        c.commit()

class CutMapIn(BaseModel):
    old_key: str
    new_key: str
    kind: str

class CutoverIn(BaseModel):
    version: str
    effective_from: str
    mappings: list[CutMapIn]

@app.post("/api/cutover")
def cutover(body: CutoverIn, authorization: str | None = Header(default=None)):
    """双生效修复卡（甲路径）：关旧生效与开新生效同库事务提交，失败两版回滚；
    单生效护栏偏离则拒写记「双生效偏了」；并发双切排队占锁，后到只记争用。"""
    actor = require_user(authorization)
    version = body.version.strip()
    if not version or not body.effective_from.strip() or not body.mappings:
        raise HTTPException(400, "切版参数不全：version/effective_from/mappings 必填且映射非空")
    for m in body.mappings:
        if not m.old_key.strip() or not m.new_key.strip() or not m.kind.strip():
            raise HTTPException(400, "切版参数不全：映射行 old_key/new_key/kind 必填")
    now = datetime.now().isoformat(timespec="seconds")
    with _cut_lock(version):  # 排队占锁
        conn = connect()
        conn.isolation_level = None  # 显式掌控事务边界
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM map_versions WHERE version=?", (version,)).fetchone():
                conn.execute("ROLLBACK")
                _ledger(version, actor, "争用", "目标版已存在，后到只记争用")
                raise HTTPException(409, "争用：目标版已存在，仅记争用")
            # 甲路径：同一事务内先关旧生效，再开新生效
            conn.execute("UPDATE map_versions SET status='停用' WHERE status='生效'")
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)",
                         (version, "生效", body.effective_from.strip()))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)",
                             [(version, m.old_key, m.new_key, m.kind) for m in body.mappings])
            # 单生效护栏：偏离修复卡则拒写并记「双生效偏了」
            eff = conn.execute("SELECT version FROM map_versions WHERE status='生效'").fetchall()
            if len(eff) != 1 or eff[0]["version"] != version:
                conn.execute("ROLLBACK")
                _ledger(version, actor, "双生效偏了", "单生效护栏触发，已拒写并两版回滚")
                raise HTTPException(500, "双生效偏了：已拒写并两版回滚")
            conn.execute("INSERT INTO cut_ledger(target_version,actor,result,detail,created_at) VALUES (?,?,?,?,?)",
                         (version, actor, "已切", "同事务关旧开新，单生效护栏通过", now))
            conn.execute("COMMIT")
        except HTTPException:
            raise
        except sqlite3.IntegrityError as e:
            conn.execute("ROLLBACK")  # 失败则两版回滚
            _ledger(version, actor, "失败", f"事务回滚：{e}")
            raise HTTPException(409, f"切版失败已两版回滚：{e}")
        finally:
            conn.close()
    return {"ok": True, "effective": version, "result": "已切"}

@app.get("/api/cutovers")
def cutovers(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM cut_ledger ORDER BY id DESC").fetchall()]}

@app.get("/api/reconcile")
def reconcile(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        vers = [dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]
        eff = [v["version"] for v in vers if v["status"] == "生效"]
        rows = []
        for v in vers:
            mc = c.execute("SELECT COUNT(*) c FROM mappings WHERE version=?", (v["version"],)).fetchone()["c"]
            fc = c.execute("SELECT COUNT(*) c FROM formal_entities WHERE version=?", (v["version"],)).fetchone()["c"]
            rows.append({"version": v["version"], "status": v["status"], "mappings": mc, "formal": fc})
        unknown = c.execute("SELECT COUNT(*) c FROM formal_entities WHERE version NOT IN (SELECT version FROM map_versions)").fetchone()["c"]
    return {"effective_versions": eff, "single_effective": len(eff) == 1,
            "effective": eff[0] if len(eff) == 1 else None, "versions": rows,
            "formal_unknown_version": unknown,
            "consistent": len(eff) == 1 and unknown == 0}

@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}

@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
