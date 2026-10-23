# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn>=0.30", "python-multipart>=0.0.9"]
# ///
"""KotH 후보 업로드·리더보드. 실행: uv run app.py"""
import base64
import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect

ROOT = Path(__file__).resolve().parent


def load_env():
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if not os.environ.get(k):
            os.environ[k] = v


load_env()
TEAM_PW = os.environ.get("TEAM_PW", "")
LEAD_PW = os.environ.get("LEAD_PW", "")
SECRET = os.environ.get("SECRET", "").encode()
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
MAX_BYTES = int(os.environ.get("MAX_MB", "20480")) << 20
RESERVE_BYTES = int(os.environ.get("RESERVE_GB", "20")) << 30  # 디스크에 이만큼은 남겨둔다
CHUNK = 16 << 20  # 퀵 터널은 요청당 100MB 상한이라 조각으로 나눠 받는다
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
if not (TEAM_PW and LEAD_PW and SECRET):
    raise SystemExit(".env에 TEAM_PW, LEAD_PW, SECRET이 필요해요")

FILES, TMP = DATA / "files", DATA / "tmp"
for d in (FILES, TMP):
    d.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(TMP)  # 업로드 임시파일도 /data 쪽에
DB = DATA / "koth.db"
KST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS problems(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  higher_better INTEGER NOT NULL DEFAULT 1,
  score_key TEXT NOT NULL DEFAULT 'avg',
  verifier_version TEXT NOT NULL DEFAULT '',
  verifier_sha TEXT,
  verifier_name TEXT,
  notes TEXT NOT NULL DEFAULT '',
  webhook TEXT NOT NULL DEFAULT '',
  champion_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subs(
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL REFERENCES problems(id),
  nick TEXT NOT NULL,
  title TEXT NOT NULL,
  score REAL NOT NULL,
  rescore REAL,
  official REAL,
  report TEXT NOT NULL,
  parent_id INTEGER,
  sha256 TEXT NOT NULL,
  filename TEXT NOT NULL,
  size INTEGER NOT NULL,
  verifier_version TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  reviewer TEXT,
  reason TEXT NOT NULL DEFAULT '',
  thread_id TEXT,
  accepted_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(problem_id, sha256)
);
CREATE TABLE IF NOT EXISTS uploads(
  id TEXT PRIMARY KEY,
  nick TEXT NOT NULL,
  filename TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  sub_id INTEGER NOT NULL,
  nick TEXT NOT NULL,
  action TEXT NOT NULL,
  at TEXT NOT NULL
);
"""


@contextmanager
def tx():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


with tx() as c:
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)


# ---------- 공통 ----------

def e(x):
    return html.escape("" if x is None else str(x))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def kst(s):
    return datetime.fromisoformat(s).astimezone(KST).strftime("%m/%d %H:%M") if s else ""


def fmt(x):
    if x is None:
        return ""
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def size_str(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def num(s):
    try:
        v = float(str(s).strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def eff(s):
    return s["rescore"] if s["rescore"] is not None else s["score"]


def gain(p, a, b):
    """a가 b보다 얼마나 좋은지 (양수 = 좋음)"""
    return (a - b) if p["higher_better"] else (b - a)


def report_get(report, key):
    m = re.search(rf"(?im)^\W*{re.escape(key)}\b\s*[:=]?\s*(\S+)", report)
    return m.group(1) if m else None


def report_num(report, key):
    m = re.search(rf"(?i)\b{re.escape(key)}\b\s*[:=]?\s*(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)", report)
    return num(m.group(1)) if m else None


def base_url(request):
    return PUBLIC_URL or str(request.base_url).rstrip("/")


# ---------- 로그인 ----------

COOKIE = "koth"


def pw_fp(role):
    pw = LEAD_PW if role == "lead" else TEAM_PW
    return hashlib.sha256(SECRET + pw.encode()).hexdigest()[:12]


def sign(d):
    raw = base64.urlsafe_b64encode(json.dumps(d, ensure_ascii=False).encode()).decode()
    return raw + "." + hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()


def unsign(tok):
    try:
        raw, mac = tok.rsplit(".", 1)
        if not hmac.compare_digest(mac, hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()):
            return None
        d = json.loads(base64.urlsafe_b64decode(raw))
    except Exception:
        return None
    # 비번을 바꾸면 기존 로그인이 전부 풀린다
    if d.get("exp", 0) < time.time() or d.get("k") != pw_fp(d.get("role")):
        return None
    return d


def current_user(request):
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), LEAD_PW.encode()):
        return {"nick": "bot", "role": "lead"}
    return unsign(request.cookies.get(COOKIE, ""))


def is_lead(request):
    return request.state.user["role"] == "lead"


def need_lead(request):
    if not is_lead(request):
        raise HTTPException(403, "리드만 할 수 있어요")


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path != "/login":
        u = current_user(request)
        if not u:
            if path.startswith(("/api/", "/up")):
                return JSONResponse({"error": "다시 로그인해주세요"}, 401)
            return RedirectResponse("/login", 303)
        request.state.user = u
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    return resp


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith(("/api/", "/up")):
        return JSONResponse({"error": exc.detail}, exc.status_code)
    msg = exc.detail if exc.status_code != 404 or exc.detail != "Not Found" else "없는 페이지예요"
    return page(request, "오류", f'<p class="err">{e(msg)}</p><p><a href="/">처음으로</a></p>', exc.status_code)


# ---------- 화면 ----------

CSS = """
*{box-sizing:border-box}
html{background:#fff;color:#000}
body{margin:0 auto;max-width:1120px;padding:20px 20px 80px;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
a{color:#000;text-underline-offset:2px}
header{display:flex;justify-content:space-between;align-items:baseline;gap:16px;padding-bottom:10px;margin-bottom:28px;border-bottom:2px solid #000}
.brand{font-weight:800;font-size:17px;text-decoration:none;letter-spacing:-.01em}
nav{display:flex;gap:16px;font-size:14px}
h1{font-size:22px;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:15px;margin:36px 0 10px}
p{margin:6px 0}
.meta{font-size:14px}
.wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #e6e6e6;white-space:nowrap;vertical-align:top}
th{font-weight:700;border-bottom:2px solid #000}
td.t{white-space:normal;min-width:220px}
.n{text-align:right;font-variant-numeric:tabular-nums}
tr.top td{font-weight:700}
tr.x td{text-decoration:line-through}
tr.x td.t2{text-decoration:none}
.mono,pre,code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
pre{margin:0;padding:14px 16px;border:1px solid #000;white-space:pre-wrap;word-break:break-word;max-height:70vh;overflow:auto}
.box{border:2px solid #000;padding:14px 18px;margin:20px 0}
.box .k{font-size:12px;font-weight:700;letter-spacing:.06em}
.err{border:2px solid #000;padding:10px 14px;font-weight:700}
.ok{border:1px solid #000;padding:10px 14px}
form.f{max-width:720px}
label{display:block;margin:16px 0 6px;font-weight:700;font-size:14px}
small{font-size:12px}
input,select,textarea,button,.btn{font:inherit;color:#000;background:#fff;border:1px solid #000;border-radius:0;padding:7px 10px}
input[type=text],input[type=password],input[type=number],input:not([type]),textarea,select{width:100%}
input[type=file]{width:100%;padding:6px}
textarea{min-height:260px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
button,.btn{display:inline-block;cursor:pointer;font-weight:700;text-decoration:none;line-height:1.4}
button.p,.btn.p{background:#000;color:#fff}
button:hover,.btn:hover{background:#000;color:#fff}
button.p:hover,.btn.p:hover{background:#fff;color:#000}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}
.row input{width:auto;flex:1;min-width:160px}
.kv td:first-child{font-weight:700;width:120px}
.kv td{white-space:normal}
details{margin-top:28px}
summary{cursor:pointer;font-weight:700}
ul.log{list-style:none;padding:0;margin:0;font-size:14px}
ul.log li{padding:4px 0;border-bottom:1px solid #e6e6e6}
.empty{padding:28px 0}
"""


def page(request, title, body, status=200):
    u = getattr(request.state, "user", None)
    nav = ""
    if u:
        role = " · 리드" if u["role"] == "lead" else ""
        nav = f'<nav><a href="/">문제 목록</a><span>{e(u["nick"])}{role}</span><a href="/logout">로그아웃</a></nav>'
    return HTMLResponse(
        f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{e(title)} · KotH</title><style>{CSS}</style></head><body>"
        f'<header><a class="brand" href="/">KotH 보드</a>{nav}</header>{body}</body></html>',
        status,
    )


def get_problem(c, pid):
    p = c.execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "없는 문제예요")
    return p


def get_sub(c, sid):
    s = c.execute("SELECT * FROM subs WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(404, "없는 제출이에요")
    return s


def status_label(s, champ_id):
    if s["id"] == champ_id:
        return "챔피언"
    return {
        "pending": "대기",
        "review": f"검증중 · {e(s['reviewer'])}",
        "accepted": "채택(이전)",
        "rejected": "기각",
    }[s["status"]]


def direction(p):
    return "높을수록 좋음" if p["higher_better"] else "낮을수록 좋음"


def version_flag(p, s):
    if p["verifier_version"] and s["verifier_version"] != p["verifier_version"]:
        return " · 구버전" if s["verifier_version"] else " · 버전?"
    return ""


# ---------- 로그인 화면 ----------

@app.get("/login")
def login_form(request: Request, err: str = ""):
    msg = '<p class="err">비밀번호가 맞지 않아요</p>' if err else ""
    return page(request, "로그인", f"""
<h1>들어가기</h1>
<p class="meta">닉네임은 디스코드 닉네임 그대로 써주세요. 비밀번호는 디스코드 공지에 있어요.</p>
{msg}
<form class="f" method="post">
<label>닉네임</label><input name="nick" required maxlength="32" autofocus>
<label>비밀번호</label><input name="pw" type="password" required>
<p style="margin-top:20px"><button class="p">들어가기</button></p>
</form>""")


@app.post("/login")
def login(request: Request, nick: str = Form(...), pw: str = Form(...)):
    nick = nick.strip()[:32]
    if hmac.compare_digest(pw.encode(), LEAD_PW.encode()):
        role = "lead"
    elif hmac.compare_digest(pw.encode(), TEAM_PW.encode()):
        role = "member"
    else:
        time.sleep(1)
        return RedirectResponse("/login?err=1", 303)
    if not nick:
        return RedirectResponse("/login?err=1", 303)
    tok = sign({"nick": nick, "role": role, "k": pw_fp(role), "exp": time.time() + 4 * 86400})
    r = RedirectResponse("/", 303)
    r.set_cookie(COOKIE, tok, max_age=4 * 86400, httponly=True, samesite="lax",
                 secure=request.url.scheme == "https")
    return r


@app.get("/logout")
def logout():
    r = RedirectResponse("/login", 303)
    r.delete_cookie(COOKIE)
    return r


# ---------- 문제 목록 ----------

@app.get("/")
def home(request: Request):
    with tx() as c:
        probs = c.execute("SELECT * FROM problems ORDER BY id").fetchall()
        stats = {r["problem_id"]: r for r in c.execute(
            "SELECT problem_id, COUNT(*) n, SUM(status='pending') pend, SUM(status='review') rev,"
            " MAX(created_at) last FROM subs GROUP BY problem_id")}
        champs = {r["id"]: r for r in c.execute(
            "SELECT s.* FROM subs s JOIN problems p ON p.champion_id=s.id")}
        recent = c.execute(
            "SELECT s.*, p.name pname, p.champion_id cid FROM subs s JOIN problems p ON p.id=s.problem_id"
            " ORDER BY s.id DESC LIMIT 15").fetchall()

    add = ""
    if is_lead(request):
        add = (f'<p style="margin-top:14px"><a class="btn" href="/new">문제 추가</a></p>'
               f'<p class="meta">서버 디스크 여유 {size_str(shutil.disk_usage(DATA).free)}</p>')
    if not probs:
        return page(request, "문제 목록", f'<h1>문제 목록</h1><p class="empty">아직 문제가 없어요. 리드가 추가하면 여기에 떠요.</p>{add}')

    rows = []
    for p in probs:
        st = stats.get(p["id"])
        ch = champs.get(p["champion_id"])
        champ = f'{fmt(eff(ch))} · {e(ch["nick"])}' if ch else "없음"
        rows.append(
            f'<tr><td><a href="/p/{p["id"]}"><b>{e(p["name"])}</b></a></td><td>{direction(p)}</td>'
            f'<td class="n">{champ}</td><td class="n">{st["pend"] if st else 0}</td>'
            f'<td class="n">{st["rev"] if st else 0}</td><td class="n">{st["n"] if st else 0}</td>'
            f'<td>{kst(st["last"]) if st else ""}</td></tr>')
    rec = "".join(
        f'<tr><td><a href="/s/{s["id"]}">#{s["id"]}</a></td><td>{e(s["pname"])}</td>'
        f'<td class="n">{fmt(eff(s))}</td><td class="t">{e(s["title"])}</td><td>{e(s["nick"])}</td>'
        f'<td>{status_label(s, s["cid"])}</td><td>{kst(s["created_at"])}</td></tr>' for s in recent)
    return page(request, "문제 목록", f"""
<h1>문제 목록</h1>
<div class="wrap"><table>
<tr><th>문제</th><th>방향</th><th class="n">챔피언</th><th class="n">대기</th><th class="n">검증중</th><th class="n">전체</th><th>마지막 제출</th></tr>
{''.join(rows)}</table></div>{add}
<h2>최근 제출</h2>
<div class="wrap"><table>
<tr><th>#</th><th>문제</th><th class="n">점수</th><th>요약</th><th>제출자</th><th>상태</th><th>시각</th></tr>
{rec or '<tr><td colspan="7">아직 없어요</td></tr>'}</table></div>""")


# ---------- 문제 페이지 ----------

@app.get("/p/{pid}")
def problem(request: Request, pid: int):
    with tx() as c:
        p = get_problem(c, pid)
        subs = c.execute("SELECT * FROM subs WHERE problem_id=? ORDER BY id", (pid,)).fetchall()
    champ = next((s for s in subs if s["id"] == p["champion_id"]), None)
    alive = [s for s in subs if s["status"] != "rejected"]
    alive.sort(key=lambda s: (-eff(s) if p["higher_better"] else eff(s), s["id"]))
    rejected = [s for s in subs if s["status"] == "rejected"][::-1]

    ver = f' · 검증기 <b>{e(p["verifier_version"])}</b>' if p["verifier_version"] else ""
    if p["verifier_sha"]:
        ver += f' <a href="/p/{pid}/verifier">받기</a>'
    lead = f' <a class="btn" href="/p/{pid}/edit">설정</a>' if is_lead(request) else ""
    notes = f'<pre style="margin-top:14px">{e(p["notes"])}</pre>' if p["notes"].strip() else ""

    if champ:
        cbox = f"""<div class="box"><div class="k">현재 챔피언</div>
<p style="font-size:18px;margin:4px 0"><b>{fmt(eff(champ))}</b> · <a href="/s/{champ['id']}">#{champ['id']}</a> {e(champ['title'])} · {e(champ['nick'])}</p>
<p class="meta">로컬 {fmt(champ['score'])} · 재채점 {fmt(champ['rescore']) or '-'} · 공식 {fmt(champ['official']) or '-'} · <a href="/f/{champ['id']}">파일 받기</a></p></div>"""
    else:
        cbox = '<div class="box"><div class="k">현재 챔피언</div><p>아직 없어요. 리드가 첫 후보를 채택하면 생겨요.</p></div>'

    def row(s, rank):
        d = ""
        if champ and s["id"] != champ["id"]:
            g = gain(p, eff(s), eff(champ))
            d = ("+" if g > 0 else "") + fmt(g)
        cls = ' class="top"' if champ and s["id"] == champ["id"] else ""
        return (f'<tr{cls}><td class="n">{rank}</td><td><a href="/s/{s["id"]}">#{s["id"]}</a>{version_flag(p, s)}</td>'
                f'<td class="n">{fmt(eff(s))}</td><td class="n">{fmt(s["score"])}</td><td class="n">{fmt(s["rescore"])}</td>'
                f'<td class="n">{d}</td><td class="t">{e(s["title"])}</td><td>{e(s["nick"])}</td>'
                f'<td>{status_label(s, p["champion_id"])}</td><td>{kst(s["created_at"])}</td>'
                f'<td><a href="/f/{s["id"]}">받기</a></td></tr>')

    body = "".join(row(s, i + 1) for i, s in enumerate(alive)) or '<tr><td colspan="11">아직 후보가 없어요</td></tr>'
    rej = ""
    if rejected:
        rr = "".join(
            f'<tr class="x"><td><a href="/s/{s["id"]}">#{s["id"]}</a></td><td class="n">{fmt(eff(s))}</td>'
            f'<td class="t">{e(s["title"])}</td><td>{e(s["nick"])}</td><td class="t t2">{e(s["reason"])}</td>'
            f'<td>{e(s["reviewer"])}</td></tr>' for s in rejected)
        rej = f"""<details><summary>기각 {len(rejected)}건</summary><div class="wrap"><table>
<tr><th>#</th><th class="n">점수</th><th>요약</th><th>제출자</th><th>사유</th><th>리드</th></tr>{rr}</table></div></details>"""

    return page(request, p["name"], f"""
<h1>{e(p['name'])}</h1>
<p class="meta">{direction(p)} · 점수 기준 <b>{e(p['score_key'])}</b>{ver}</p>
{notes}
{cbox}
<p><a class="btn p" href="/p/{pid}/submit">후보 제출</a>{lead}</p>
<h2>후보 {len(alive)}건 · 점수순</h2>
<p class="meta">점수 = 재채점이 있으면 재채점, 없으면 제출자 로컬 점수. 챔피언 대비 +면 더 좋음.</p>
<div class="wrap"><table>
<tr><th class="n">순위</th><th>#</th><th class="n">점수</th><th class="n">로컬</th><th class="n">재채점</th><th class="n">챔피언 대비</th><th>요약</th><th>제출자</th><th>상태</th><th>시각</th><th>파일</th></tr>
{body}</table></div>
{rej}""")


@app.get("/p/{pid}/verifier")
def verifier_file(request: Request, pid: int):
    with tx() as c:
        p = get_problem(c, pid)
    if not p["verifier_sha"]:
        raise HTTPException(404, "검증기 파일이 없어요")
    return FileResponse(FILES / p["verifier_sha"], filename=p["verifier_name"], media_type="application/octet-stream")


# ---------- 제출 ----------

REPORT_TEMPLATE = """[REPORT]
verifier: {ver}
sha256:
min:
avg:
max:
[/REPORT]

이전 파일 대비 개선:
-

어떤 로직·방법으로:
-
"""


def size_label(n):
    return f"{n >> 30}GB" if n >= 1 << 30 else f"{n >> 20}MB"


def get_upload(c, uid, nick):
    u = c.execute("SELECT * FROM uploads WHERE id=?", (uid,)).fetchone()
    return u if u and u["nick"] == nick else None


def submit_form(request, p, err="", title="", score="", parent=None, report=None, upload=None):
    if report is None:
        report = REPORT_TEMPLATE.format(ver=p["verifier_version"] or "")
    if parent is None:
        parent = p["champion_id"] or ""
    msg = f'<p class="err">{e(err)}</p>' if err else ""
    up = ""
    if upload:
        up = (f'<p class="ok">올린 파일: <b>{e(upload["filename"])}</b> · {size_str(upload["size"])}. '
              f'다시 고르지 않아도 돼요. 다른 파일을 고르면 그걸로 바뀌어요.</p>')
    return page(request, f"{p['name']} 제출", f"""
<p class="meta"><a href="/p/{p['id']}">← {e(p['name'])}</a></p>
<h1>후보 제출</h1>
<p class="meta">{direction(p)} · 점수 기준 <b>{e(p['score_key'])}</b> · 파일 최대 {size_label(MAX_BYTES)}</p>
{msg}
<form class="f" method="post" enctype="multipart/form-data" data-chunked>
<input type="hidden" name="upload_id" value="{e(upload['id'] if upload else '')}">
<label>파일</label>{up}<input type="file" name="file">
<label>한 줄 요약</label><input name="title" required maxlength="80" value="{e(title)}" placeholder="예: 그리디 → 빔서치, seed별 타임아웃 처리">
<label>로컬 점수 <small>(비우면 REPORT의 {e(p['score_key'])} 값을 씀)</small></label><input name="score" inputmode="decimal" value="{e(score)}">
<label>출발한 제출 번호 <small>(챔피언에서 시작했으면 그대로)</small></label><input name="parent" type="number" value="{e(parent)}">
<label>REPORT + 변경점 <small>(검증기 출력과 프롬프트 결과를 그대로 붙여넣기)</small></label>
<textarea name="report" required>{e(report)}</textarea>
<p style="margin-top:20px"><button class="p">제출</button> <span id="up-status" class="mono"></span></p>
</form><script src="/up.js"></script>""")


@app.get("/p/{pid}/submit")
def submit_page(request: Request, pid: int):
    with tx() as c:
        p = get_problem(c, pid)
    return submit_form(request, p)


@app.post("/p/{pid}/submit")
def submit(request: Request, bg: BackgroundTasks, pid: int, title: str = Form(...), report: str = Form(...),
           score: str = Form(""), parent: str = Form(""), upload_id: str = Form(""),
           file: UploadFile | None = File(None)):
    nick = request.state.user["nick"]
    with tx() as c:
        p = get_problem(c, pid)
        upload = get_upload(c, upload_id, nick) if upload_id else None
    if upload and not upload["sha256"]:
        upload = None  # 아직 다 안 올라온 업로드
    title, report = title.strip()[:80], report.replace("\r\n", "\n").strip()

    def fail(msg, keep=True):
        return submit_form(request, p, msg, title, score, parent, report, upload if keep else None)

    v = num(score) if score.strip() else report_num(report, p["score_key"])
    if v is None:
        return fail(f"점수를 못 읽었어요. 점수 칸에 숫자를 넣거나 REPORT에 '{p['score_key']}: 숫자' 줄을 넣어주세요.")
    if not title:
        return fail("한 줄 요약을 적어주세요.")
    parent_id = int(parent) if parent.strip().isdigit() else None

    if file is not None and file.filename:  # 스크립트 없이 바로 올린 작은 파일
        tmp = TMP / secrets.token_hex(8)
        h, size = hashlib.sha256(), 0
        try:
            with open(tmp, "wb") as out:
                while chunk := file.file.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError
                    h.update(chunk)
                    out.write(chunk)
        except ValueError:
            tmp.unlink(missing_ok=True)
            return fail(f"파일이 {size_label(MAX_BYTES)}를 넘어요.")
        if size == 0:
            tmp.unlink(missing_ok=True)
            return fail("빈 파일이에요.")
        sha, name, upload = h.hexdigest(), Path(file.filename).name[:120] or "file", None
    elif upload:
        tmp, sha, size, name = up_paths(upload["id"])[0], upload["sha256"], upload["size"], upload["filename"]
    else:
        return fail("파일을 골라주세요.")

    def drop():
        tmp.unlink(missing_ok=True)
        if upload:
            with tx() as c:
                c.execute("DELETE FROM uploads WHERE id=?", (upload["id"],))

    claimed = re.search(r"(?i)sha256\s*[:=]?\s*([0-9a-f]{64})", report)
    if claimed and claimed.group(1).lower() != sha:
        if not upload:
            tmp.unlink(missing_ok=True)
        return fail(f"REPORT의 sha256과 올린 파일이 달라요. 올린 파일: {sha}")

    with tx() as c:
        dup = c.execute("SELECT id FROM subs WHERE problem_id=? AND sha256=?", (pid, sha)).fetchone()
        if dup:
            drop()
            return fail(f"같은 파일이 이미 #{dup['id']}로 올라와 있어요.", keep=False)
        if parent_id and not c.execute("SELECT 1 FROM subs WHERE id=? AND problem_id=?", (parent_id, pid)).fetchone():
            parent_id = None
        dest = FILES / sha
        if dest.exists():
            tmp.unlink()
        else:
            tmp.replace(dest)
        if upload:
            c.execute("DELETE FROM uploads WHERE id=?", (upload["id"],))
        ver = report_get(report, "verifier") or ""
        cur = c.execute(
            "INSERT INTO subs(problem_id,nick,title,score,report,parent_id,sha256,filename,size,verifier_version,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (pid, nick, title, v, report, parent_id, sha, name, size, ver, now()))
        sid = cur.lastrowid
        c.execute("INSERT INTO events(sub_id,nick,action,at) VALUES(?,?,?,?)", (sid, nick, f"제출 · 로컬 {fmt(v)}", now()))
    bg.add_task(notify_new, sid, base_url(request))
    return RedirectResponse(f"/s/{sid}?new=1", 303)


# ---------- 큰 파일 조각 업로드 ----------
# 브라우저가 파일을 CHUNK 크기로 잘라 PUT /up/{id}/{번호}로 병렬 전송 → 다 모이면 서버가 sha256 계산

UP_LOCK = threading.Lock()
UP_PARTS: dict[str, set[int]] = {}
HASHING: set[str] = set()


def up_paths(uid):
    return TMP / f"up-{uid}", TMP / f"up-{uid}.parts"


def up_count(u):
    return -(-u["size"] // CHUNK)


def up_parts(uid):
    with UP_LOCK:
        if uid not in UP_PARTS:
            pf = up_paths(uid)[1]
            UP_PARTS[uid] = {int(x) for x in pf.read_text().split()} if pf.exists() else set()
        return UP_PARTS[uid]


def hash_upload(uid):
    path, pf = up_paths(uid)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(8 << 20):
            h.update(b)
    with tx() as c:
        c.execute("UPDATE uploads SET sha256=? WHERE id=?", (h.hexdigest(), uid))
    pf.unlink(missing_ok=True)
    with UP_LOCK:
        HASHING.discard(uid)
        UP_PARTS.pop(uid, None)


def maybe_hash(u):
    if len(up_parts(u["id"])) < up_count(u):
        return
    with UP_LOCK:
        if u["id"] in HASHING:
            return
        HASHING.add(u["id"])
    threading.Thread(target=hash_upload, args=(u["id"],), daemon=True).start()


def purge_uploads():
    """하루 지난 미완성·미사용 업로드 정리"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    with tx() as c:
        old = [r["id"] for r in c.execute("SELECT id FROM uploads WHERE created_at<?", (cutoff,))]
        c.execute("DELETE FROM uploads WHERE created_at<?", (cutoff,))
    for uid in old:
        for f in up_paths(uid):
            f.unlink(missing_ok=True)
        UP_PARTS.pop(uid, None)


def up_owned(request, uid):
    with tx() as c:
        u = get_upload(c, uid, request.state.user["nick"])
    if not u:
        raise HTTPException(404, "없는 업로드예요")
    return u


@app.post("/up")
async def up_start(request: Request):
    try:
        d = await request.json()
        size = int(d["size"])
        name = Path(str(d.get("name") or "file")).name[:120] or "file"
    except Exception:
        raise HTTPException(400, "잘못된 요청이에요")
    if size <= 0:
        raise HTTPException(400, "빈 파일이에요")
    if size > MAX_BYTES:
        raise HTTPException(400, f"파일이 {size_label(MAX_BYTES)}를 넘어요")
    await run_in_threadpool(purge_uploads)
    if shutil.disk_usage(DATA).free - size < RESERVE_BYTES:
        raise HTTPException(507, "서버 디스크가 부족해요. 리드에게 알려주세요")
    uid = secrets.token_hex(12)
    with open(up_paths(uid)[0], "wb") as f:
        f.truncate(size)
    with tx() as c:
        c.execute("INSERT INTO uploads(id,nick,filename,size,created_at) VALUES(?,?,?,?,?)",
                  (uid, request.state.user["nick"], name, size, now()))
    return {"id": uid, "chunk": CHUNK, "count": -(-size // CHUNK)}


@app.put("/up/{uid}/{idx}")
async def up_chunk(request: Request, uid: str, idx: int):
    u = await run_in_threadpool(up_owned, request, uid)
    if u["sha256"]:
        return {"ok": True}
    if not 0 <= idx < up_count(u):
        raise HTTPException(400, "조각 번호가 이상해요")
    start = idx * CHUNK
    want = min(CHUNK, u["size"] - start)
    fd = os.open(up_paths(uid)[0], os.O_WRONLY)
    got, buf = 0, bytearray()
    try:
        async for piece in request.stream():
            if got + len(buf) + len(piece) > want:
                raise HTTPException(400, "조각 크기가 맞지 않아요")
            buf += piece
            if len(buf) >= 4 << 20:
                await run_in_threadpool(os.pwrite, fd, bytes(buf), start + got)
                got, buf = got + len(buf), bytearray()
        if buf:
            await run_in_threadpool(os.pwrite, fd, bytes(buf), start + got)
            got += len(buf)
    except ClientDisconnect:
        return Response(status_code=400)
    finally:
        os.close(fd)
    if got != want:
        raise HTTPException(400, "조각이 덜 왔어요")
    parts = up_parts(uid)
    with UP_LOCK:
        if idx not in parts:
            parts.add(idx)
            with open(up_paths(uid)[1], "a") as f:
                f.write(f"{idx}\n")
    maybe_hash(u)
    return {"ok": True}


@app.get("/up/{uid}")
def up_status(request: Request, uid: str):
    u = up_owned(request, uid)
    if u["sha256"]:
        return {"done": True, "sha256": u["sha256"]}
    parts = up_parts(uid)
    maybe_hash(u)  # 서버가 재시작돼서 계산이 끊긴 경우도 여기서 다시 시작
    return {"done": False, "missing": sorted(set(range(up_count(u))) - parts)[:64]}


UP_JS = r"""// 큰 파일은 조각으로 나눠 4개씩 병렬로 올린다 (퀵 터널은 요청당 100MB 상한)
const form = document.querySelector('form[data-chunked]');
const status = document.getElementById('up-status');
const human = n => n >= 2 ** 30 ? (n / 2 ** 30).toFixed(2) + 'GB' : (n / 2 ** 20).toFixed(1) + 'MB';
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function json(res) {
  let j = {};
  try { j = await res.json(); } catch {}
  if (!res.ok) throw new Error(j.error || 'HTTP ' + res.status);
  return j;
}

async function upload(file) {
  const { id, chunk, count } = await json(await fetch('/up', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: file.name, size: file.size }),
  }));
  const t0 = Date.now();
  let sent = 0;
  const show = () => {
    const s = Math.max((Date.now() - t0) / 1000, 1), n = Math.min(sent, file.size);
    status.textContent = `업로드 ${Math.floor(n / file.size * 100)}% · ${human(n)} / ${human(file.size)} · ${human(sent / s)}/s`;
  };
  const put = async i => {
    const part = file.slice(i * chunk, Math.min(file.size, (i + 1) * chunk));
    for (let tries = 1; ; tries++) {
      try {
        await json(await fetch(`/up/${id}/${i}`, { method: 'PUT', body: part }));
        sent += part.size; show(); return;
      } catch (err) {
        if (tries >= 6) throw err;
        await sleep(1500 * tries);
      }
    }
  };
  show();
  let todo = [...Array(count).keys()];
  for (let round = 0; round < 10; round++) {
    const q = [...todo];
    await Promise.all(Array.from({ length: Math.min(4, q.length) }, async () => { while (q.length) await put(q.shift()); }));
    // 서버가 다 받았는지 확인하고, 빠진 조각이 있으면 다시 보낸다
    for (;;) {
      const s = await json(await fetch(`/up/${id}`));
      if (s.done) return id;
      if (s.missing.length) { todo = s.missing; break; }
      status.textContent = '서버에서 파일 확인 중…';
      await sleep(1000);
    }
  }
  throw new Error('조각이 계속 빠져요');
}

form?.addEventListener('submit', async ev => {
  const input = form.querySelector('input[type=file]');
  const file = input.files[0];
  if (!file) return;  // 이미 올린 파일을 그대로 쓰는 경우
  ev.preventDefault();
  const btn = form.querySelector('button.p');
  btn.disabled = true;
  try {
    form.upload_id.value = await upload(file);
    input.disabled = true;  // 파일 본문은 다시 안 보낸다
    status.textContent = '제출 중…';
    form.submit();
  } catch (err) {
    status.textContent = '업로드 실패: ' + err.message + ' · 다시 눌러주세요';
    btn.disabled = false;
  }
});
"""


@app.get("/up.js")
def up_js():
    return Response(UP_JS, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


# ---------- 제출 상세 ----------

@app.get("/s/{sid}")
def sub_page(request: Request, sid: int, new: str = ""):
    with tx() as c:
        s = get_sub(c, sid)
        p = get_problem(c, s["problem_id"])
        champ = c.execute("SELECT * FROM subs WHERE id=?", (p["champion_id"],)).fetchone() if p["champion_id"] else None
        events = c.execute("SELECT * FROM events WHERE sub_id=? ORDER BY id", (sid,)).fetchall()

    vs = ""
    if champ and champ["id"] != sid:
        g = gain(p, eff(s), eff(champ))
        vs = f'{"+" if g > 0 else ""}{fmt(g)} (챔피언 #{champ["id"]} {fmt(eff(champ))})'
    parent = f'<a href="/s/{s["parent_id"]}">#{s["parent_id"]}</a>' if s["parent_id"] else "-"
    reason = f'<tr><td>기각 사유</td><td>{e(s["reason"])}</td></tr>' if s["status"] == "rejected" else ""
    done = '<p class="ok">제출됐어요. 리드가 재채점하고 결과를 디스코드 스레드에 달아줄 거예요.</p>' if new else ""
    log = "".join(f'<li>{kst(ev["at"])} · {e(ev["nick"])} · {e(ev["action"])}</li>' for ev in events)

    lead = ""
    if is_lead(request):
        me = request.state.user["nick"]
        warn = ""
        if s["status"] == "review" and s["reviewer"] != me:
            warn = f'<p class="err">{e(s["reviewer"])} 님이 검증 중이에요.</p>'
        claim = ""
        if s["status"] == "pending":
            claim = '<button name="action" value="claim">검증 시작 (내가 볼게요)</button>'
        elif s["status"] == "review":
            claim = '<button name="action" value="unclaim">검증 해제</button>'
        accept = ("" if s["id"] == p["champion_id"]
                  else '<button class="p" name="action" value="accept">채택 · 챔피언으로 (업로드했으면)</button>')
        reopen = ('<form method="post" action="/s/{0}/act"><div class="row"><button name="action" value="reopen">대기로 되돌리기</button></div></form>'
                  .format(sid) if s["status"] == "rejected" else "")
        lead = f"""<h2>리드</h2>{warn}
<form method="post" action="/s/{sid}/act"><div class="row">{claim}{accept}</div></form>
<form method="post" action="/s/{sid}/act"><div class="row"><input name="value" inputmode="decimal" placeholder="재채점 점수" value="{fmt(s['rescore'])}"><button name="action" value="rescore">재채점 저장</button></div></form>
<form method="post" action="/s/{sid}/act"><div class="row"><input name="value" inputmode="decimal" placeholder="공식 점수 (대회 플랫폼)" value="{fmt(s['official'])}"><button name="action" value="official">공식 점수 저장</button></div></form>
<form method="post" action="/s/{sid}/act"><div class="row"><input name="value" placeholder="기각 사유 (예: seed 37 타임아웃, +0.3%는 노이즈)" required><button name="action" value="reject">기각</button></div></form>
{reopen}"""

    return page(request, f"#{sid} {s['title']}", f"""
<p class="meta"><a href="/p/{p['id']}">← {e(p['name'])}</a></p>
<h1>#{sid} {e(s['title'])}</h1>
{done}
<div class="wrap"><table class="kv">
<tr><td>상태</td><td><b>{status_label(s, p['champion_id'])}</b></td></tr>{reason}
<tr><td>제출자</td><td>{e(s['nick'])} · {kst(s['created_at'])}</td></tr>
<tr><td>로컬 점수</td><td class="mono">{fmt(s['score'])}</td></tr>
<tr><td>재채점</td><td class="mono">{fmt(s['rescore']) or '-'}</td></tr>
<tr><td>공식 점수</td><td class="mono">{fmt(s['official']) or '-'}</td></tr>
<tr><td>챔피언 대비</td><td class="mono">{vs or '-'}</td></tr>
<tr><td>출발</td><td>{parent}</td></tr>
<tr><td>파일</td><td><a href="/f/{sid}">{e(s['filename'])}</a> · {size_str(s['size'])}<br><span class="mono">sha256 {s['sha256']}</span></td></tr>
<tr><td>검증기</td><td>{e(s['verifier_version']) or '-'}{version_flag(p, s)}</td></tr>
</table></div>
<h2>REPORT · 변경점</h2>
<pre>{e(s['report'])}</pre>
{lead}
<h2>기록</h2><ul class="log">{log}</ul>""")


@app.get("/f/{sid}")
def download(request: Request, sid: int):
    with tx() as c:
        s = get_sub(c, sid)
    return FileResponse(FILES / s["sha256"], filename=s["filename"], media_type="application/octet-stream")


@app.post("/s/{sid}/act")
def act(request: Request, bg: BackgroundTasks, sid: int, action: str = Form(...), value: str = Form("")):
    need_lead(request)
    me = request.state.user["nick"]
    value = value.strip()
    with tx() as c:
        s = get_sub(c, sid)
        p = get_problem(c, s["problem_id"])
        msg = None  # 디스코드 스레드에 남길 말
        if action == "claim":
            c.execute("UPDATE subs SET status='review', reviewer=? WHERE id=?", (me, sid))
            log, msg = "검증 시작", f"🔵 {me} 님이 검증 시작"
        elif action == "unclaim":
            c.execute("UPDATE subs SET status='pending', reviewer=NULL WHERE id=?", (sid,))
            log = "검증 해제"
        elif action in ("rescore", "official"):
            v = num(value) if value else None
            if value and v is None:
                raise HTTPException(400, "숫자를 넣어주세요")
            c.execute(f"UPDATE subs SET {action}=? WHERE id=?", (v, sid))
            label = "재채점" if action == "rescore" else "공식 점수"
            log = f"{label} {fmt(v) or '지움'}"
            msg = f"{'🧪' if action == 'rescore' else '🏁'} {label} {fmt(v)} ({me})" if v is not None else None
        elif action == "accept":
            c.execute("UPDATE subs SET status='accepted', reviewer=?, reason='', accepted_at=? WHERE id=?", (me, now(), sid))
            c.execute("UPDATE problems SET champion_id=? WHERE id=?", (sid, p["id"]))
            sc = fmt(s["rescore"]) if s["rescore"] is not None else f"로컬 {fmt(s['score'])}"
            log, msg = "채택 · 챔피언", f"✅ 채택 — 새 챔피언 ({me}) · {sc}"
        elif action == "reject":
            if not value:
                raise HTTPException(400, "기각 사유를 적어주세요")
            c.execute("UPDATE subs SET status='rejected', reviewer=?, reason=? WHERE id=?", (me, value[:300], sid))
            log, msg = f"기각 · {value[:300]}", f"❌ 기각 ({me}): {value[:300]}"
            if p["champion_id"] == sid:  # 챔피언을 기각하면 직전 챔피언으로 되돌림
                prev = c.execute(
                    "SELECT id FROM subs WHERE problem_id=? AND status='accepted' AND id!=? ORDER BY accepted_at DESC LIMIT 1",
                    (p["id"], sid)).fetchone()
                c.execute("UPDATE problems SET champion_id=? WHERE id=?", (prev["id"] if prev else None, p["id"]))
                log += f" · 챔피언 → {'#' + str(prev['id']) if prev else '없음'}"
        elif action == "reopen":
            c.execute("UPDATE subs SET status='pending', reviewer=NULL, reason='' WHERE id=?", (sid,))
            log, msg = "대기로 되돌림", f"↩️ 다시 대기 ({me})"
        else:
            raise HTTPException(400, "모르는 동작이에요")
        c.execute("INSERT INTO events(sub_id,nick,action,at) VALUES(?,?,?,?)", (sid, me, log, now()))
    if msg:
        bg.add_task(notify_thread, sid, msg)
    if request.headers.get("authorization"):
        return JSONResponse({"ok": True})
    return RedirectResponse(f"/s/{sid}", 303)


# ---------- 문제 추가·설정 (리드) ----------

def problem_form(request, p=None, err=""):
    p = p or {"id": None, "name": "", "higher_better": 1, "score_key": "avg", "verifier_version": "v1",
              "verifier_name": None, "notes": "", "webhook": ""}
    action = f"/p/{p['id']}/edit" if p["id"] else "/new"
    vf = f' <small>현재: {e(p["verifier_name"])}</small>' if p["verifier_name"] else ""
    msg = f'<p class="err">{e(err)}</p>' if err else ""
    hb, lb = ("selected", "") if p["higher_better"] else ("", "selected")
    return page(request, "문제 설정", f"""
<h1>{'문제 설정' if p['id'] else '문제 추가'}</h1>{msg}
<form class="f" method="post" action="{action}" enctype="multipart/form-data">
<label>문제 이름</label><input name="name" required maxlength="60" value="{e(p['name'])}">
<label>점수 방향</label><select name="higher_better"><option value="1" {hb}>높을수록 좋음</option><option value="0" {lb}>낮을수록 좋음</option></select>
<label>점수 기준 키 <small>(REPORT에서 읽을 이름. 보통 avg)</small></label><input name="score_key" required maxlength="20" value="{e(p['score_key'])}">
<label>검증기 버전 <small>(바꾸면 이전 버전 제출에 '구버전' 표시)</small></label><input name="verifier_version" maxlength="20" value="{e(p['verifier_version'])}">
<label>검증기 파일{vf}</label><input type="file" name="verifier">
<label>안내 <small>(규칙, 실행법, 주의점)</small></label><textarea name="notes" style="min-height:140px">{e(p['notes'])}</textarea>
<label>디스코드 웹훅 <small>(이 문제 포럼 채널 웹훅. 비우면 기본값)</small></label><input name="webhook" value="{e(p['webhook'])}" placeholder="https://discord.com/api/webhooks/...">
<p style="margin-top:20px"><button class="p">저장</button></p>
</form>""")


def save_problem(request, pid, name, higher_better, score_key, verifier_version, notes, webhook, verifier):
    need_lead(request)
    name = name.strip()
    if not name:
        raise HTTPException(400, "문제 이름을 적어주세요")
    if webhook.strip() and not webhook.strip().startswith("https://"):
        raise HTTPException(400, "웹훅 주소는 https://로 시작해야 해요")
    vsha = vname = None
    if verifier is not None and verifier.filename:
        data = verifier.file.read()
        if data:
            vsha = hashlib.sha256(data).hexdigest()
            (FILES / vsha).write_bytes(data)
            vname = Path(verifier.filename).name[:120]
    vals = (name, 1 if higher_better == "1" else 0, score_key.strip() or "avg", verifier_version.strip(),
            notes, webhook.strip())
    with tx() as c:
        try:
            if pid:
                get_problem(c, pid)
                c.execute("UPDATE problems SET name=?,higher_better=?,score_key=?,verifier_version=?,notes=?,webhook=? WHERE id=?",
                          (*vals, pid))
            else:
                pid = c.execute(
                    "INSERT INTO problems(name,higher_better,score_key,verifier_version,notes,webhook,created_at)"
                    " VALUES(?,?,?,?,?,?,?)", (*vals, now())).lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(400, "같은 이름의 문제가 이미 있어요")
        if vsha:
            c.execute("UPDATE problems SET verifier_sha=?, verifier_name=? WHERE id=?", (vsha, vname, pid))
    return RedirectResponse(f"/p/{pid}", 303)


@app.get("/new")
def new_problem_page(request: Request):
    need_lead(request)
    return problem_form(request)


@app.post("/new")
def new_problem(request: Request, name: str = Form(...), higher_better: str = Form("1"), score_key: str = Form("avg"),
                verifier_version: str = Form(""), notes: str = Form(""), webhook: str = Form(""),
                verifier: UploadFile | None = File(None)):
    return save_problem(request, None, name, higher_better, score_key, verifier_version, notes, webhook, verifier)


@app.get("/p/{pid}/edit")
def edit_problem_page(request: Request, pid: int):
    need_lead(request)
    with tx() as c:
        p = get_problem(c, pid)
    return problem_form(request, dict(p))


@app.post("/p/{pid}/edit")
def edit_problem(request: Request, pid: int, name: str = Form(...), higher_better: str = Form("1"),
                 score_key: str = Form("avg"), verifier_version: str = Form(""), notes: str = Form(""),
                 webhook: str = Form(""), verifier: UploadFile | None = File(None)):
    return save_problem(request, pid, name, higher_better, score_key, verifier_version, notes, webhook, verifier)


# ---------- 자동 재채점용 API (리드 비번을 Bearer로) ----------

@app.get("/api/p/{pid}/subs")
def api_subs(request: Request, pid: int, status: str = ""):
    need_lead(request)
    with tx() as c:
        get_problem(c, pid)
        q, args = "SELECT * FROM subs WHERE problem_id=?", [pid]
        if status:
            q, args = q + " AND status=?", args + [status]
        rows = c.execute(q + " ORDER BY id", args).fetchall()
    return [{k: r[k] for k in r.keys() if k != "thread_id"} for r in rows]


# ---------- 디스코드 ----------

def discord_post(url, payload, query):
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        url + sep + query, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "DiscordBot (koth-board, 1.0)"})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
    return json.loads(body) if body else None


def notify_new(sid, base):
    with tx() as c:
        s = get_sub(c, sid)
        p = get_problem(c, s["problem_id"])
    url = p["webhook"] or WEBHOOK
    if not url:
        return
    rep = s["report"].replace("```", "'''")
    head = (f"**#{sid}** {s['nick']} · 로컬 **{fmt(s['score'])}** ({p['score_key']})"
            f"{version_flag(p, s)}\n{s['title']}\n{base}/s/{sid}\n")
    content = head + "```\n" + rep[: 1900 - len(head)] + "\n```"
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    thread = None
    try:
        m = discord_post(url, {**payload, "thread_name": f"[{fmt(s['score'])}] {s['nick']} - {s['title']}"[:100]}, "wait=true")
        thread = m["channel_id"]
    except urllib.error.HTTPError as err:
        if err.code != 400:
            print(f"discord #{sid}: {err.code} {err.read()[:200]!r}", flush=True)
            return
        try:  # 포럼이 아닌 일반 채널 웹훅
            discord_post(url, payload, "wait=true")
        except Exception as err2:
            print(f"discord #{sid}: {err2}", flush=True)
            return
    except Exception as err:
        print(f"discord #{sid}: {err}", flush=True)
        return
    if thread:
        with tx() as c:
            c.execute("UPDATE subs SET thread_id=? WHERE id=?", (thread, sid))


def notify_thread(sid, text):
    with tx() as c:
        s = get_sub(c, sid)
        p = get_problem(c, s["problem_id"])
    url = p["webhook"] or WEBHOOK
    if not url:
        return
    try:
        query = f"thread_id={s['thread_id']}" if s["thread_id"] else ""
        discord_post(url, {"content": f"#{sid} · {text}", "allowed_mentions": {"parse": []}}, query)
    except Exception as err:
        print(f"discord #{sid}: {err}", flush=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, proxy_headers=True, forwarded_allow_ips="*")
