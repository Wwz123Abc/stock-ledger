#!/usr/bin/env python3
"""美股持仓账本 — self-hosted server.

Standard library only (Python 3.10+). Serves the ledger pages to registered accounts, keeps each account's
records in SQLite as small JSON documents grouped in collections, and refreshes prices, daily closes and
exchange rates from Tencent Finance (quotes, daily bars) and Sina Finance (FX, gold). Market data (closes,
daily bars, gold, USD/CNY) is shared by everyone; trades, Treasuries and deposits, settings and reading
notes belong to one account and only it can read them.

  python3 app.py serve [--host 127.0.0.1] [--port 5090]
  python3 app.py users                       # list the accounts
  python3 app.py reset-password NAME         # forgotten password: a one-time reset code, valid 24 hours
  python3 app.py delete-user NAME            # delete an account and all of its records
  python3 app.py registration on|off         # allow or stop new sign-ups
  python3 app.py import NAME seed.json       # load {collection: {doc_id: data}} into an account
  python3 app.py refresh NAME                # fetch market data for one account's holdings
  python3 app.py backup                      # copy the database to data/backups/, keep 30
  python3 app.py vision-key                  # turn on screenshot import: asks for a DashScope API key
  python3 app.py vision-key off              # turn it off and forget the key
"""
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA, "ledger.db")
STATIC = os.path.join(ROOT, "static")
USER_COLLS = {"trades", "fixed", "config", "reading"}
SHARED_COLLS = {"history", "candles", "watch"}  # market data: written only by the server, uid 0
PAGES = {"/": "index.html", "/index.html": "index.html", "/dashboard": "dashboard.html", "/reading": "reading.html", "/account": "account.html"}
PUBLIC_PAGES = {"/privacy": "privacy.html"}
ASSETS = {"d3.min.js": "text/javascript; charset=utf-8"}
NAME_RE = re.compile(r"^[A-Za-z0-9_一-鿿]{2,20}$")
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
SESSION_DAYS = 30
MAX_BODY = 256 * 1024
MAX_IMAGE_BODY = 6 * 1024 * 1024
MAX_IMPORT = 1000        # records in one import
MAX_USERS = 2000
MAX_DOCS = 5000          # records per account
REFRESH_GAP = 30         # seconds between two refreshes of one account
RESET_HOURS = 24
HK, NY = ZoneInfo("Asia/Hong_Kong"), ZoneInfo("America/New_York")
write_lock = threading.Lock()
refresh_lock = threading.Lock()
fails = {}         # ip -> [count, first_time]: wrong passwords and reset codes
signups = {}       # ip -> [count, first_time]
last_refresh = {}  # uid -> time
VISION_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
VISION_MODEL = "qwen-vl-max"
VISION_PER_DAY = 20      # screenshots per account per day
VISION_SITE_DAY = 300    # screenshots for the whole site per day
vision_used = {}         # (date, uid) -> count; (date, 0) is the whole site
VISION_PROMPT = (
    "你是交易记录提取器。从券商或银行 App 的成交截图中提取股票成交记录，只输出一个 JSON 数组，不要任何解释或 Markdown。"
    "每条记录的格式：{\"date\": \"成交日期 YYYY-MM-DD\", \"side\": \"buy 或 sell\", "
    "\"symbol\": \"股票代码，美股如 AAPL、BRK.B，港股用 4 位数字如 0700；看不到代码就填股票名称\", "
    "\"shares\": 成交数量, \"price\": 成交单价, \"fees\": 佣金、平台费、印花税等费用合计（看不到就填 null）, "
    "\"currency\": \"USD 或 HKD\", \"note\": \"成交单编号或指示编号，没有就填空字符串\"}。"
    "规则：只提取已经成交的记录，忽略未成交、已撤销的委托；不要输出账户号码、证券账号、银行卡号、姓名等个人信息；"
    "数字不要带千分位和货币符号；截图里没有成交记录就输出 []。"
)


# ---------------------------------------------------------------- storage
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def columns(con, table):
    return [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]


def init_db():
    os.makedirs(DATA, exist_ok=True)
    with write_lock, db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, "
                    "pw TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, reset_hash TEXT, reset_expires REAL)")
        old = columns(con, "docs")
        if old and "uid" not in old:
            migrate_single_user(con)
        con.execute("CREATE TABLE IF NOT EXISTS docs (uid INTEGER NOT NULL, coll TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, "
                    "updated TEXT NOT NULL, PRIMARY KEY (uid, coll, id))")
        old = columns(con, "sessions")
        if old and "uid" not in old:
            con.execute("DROP TABLE sessions")
        con.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, uid INTEGER NOT NULL, expires REAL NOT NULL)")


def migrate_single_user(con):
    """Version 1 kept one ledger behind one password; its records become the account "admin"."""
    r = con.execute("SELECT v FROM meta WHERE k='password'").fetchone()
    con.execute("INSERT INTO users(name, pw, created) VALUES('admin', ?, ?)", (r["v"] if r else "", now_iso()))
    uid = con.execute("SELECT id FROM users WHERE name='admin'").fetchone()["id"]
    con.execute("CREATE TABLE docs_v2 (uid INTEGER NOT NULL, coll TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, "
                "updated TEXT NOT NULL, PRIMARY KEY (uid, coll, id))")
    con.execute("INSERT INTO docs_v2 SELECT CASE WHEN coll IN ('history', 'candles', 'watch') THEN 0 ELSE ? END, coll, id, data, updated "
                "FROM docs", (uid,))
    con.execute("DROP TABLE docs")
    con.execute("ALTER TABLE docs_v2 RENAME TO docs")
    con.execute("DELETE FROM meta WHERE k IN ('password', 'version')")
    code = os.path.join(DATA, "setup-code.txt")
    if os.path.exists(code):
        os.remove(code)
    print('moved the single-user ledger into the account "admin"' + ("" if r else "; it has no password yet: run reset-password admin"), flush=True)


def get_meta(k):
    with db() as con:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def set_meta(k, v):
    with write_lock, db() as con:
        con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def bump(uid):
    k = "version:%d" % uid
    set_meta(k, str(int(get_meta(k) or 0) + 1))


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def list_docs(uid, coll):
    with db() as con:
        rows = con.execute("SELECT id, data FROM docs WHERE uid=? AND coll=? ORDER BY id", (uid, coll)).fetchall()
    return [{"id": r["id"], "data": json.loads(r["data"])} for r in rows]


def get_doc(uid, coll, id_):
    with db() as con:
        r = con.execute("SELECT data FROM docs WHERE uid=? AND coll=? AND id=?", (uid, coll, id_)).fetchone()
    return json.loads(r["data"]) if r else None


def put_doc(uid, coll, id_, data, merge=False):
    with write_lock, db() as con:
        if merge:
            r = con.execute("SELECT data FROM docs WHERE uid=? AND coll=? AND id=?", (uid, coll, id_)).fetchone()
            data = {**(json.loads(r["data"]) if r else {}), **data}
        con.execute("INSERT INTO docs(uid,coll,id,data,updated) VALUES(?,?,?,?,?) ON CONFLICT(uid,coll,id) DO UPDATE "
                    "SET data=excluded.data, updated=excluded.updated", (uid, coll, id_, json.dumps(data, ensure_ascii=False), now_iso()))
    bump(uid)
    return data


def del_doc(uid, coll, id_):
    with write_lock, db() as con:
        con.execute("DELETE FROM docs WHERE uid=? AND coll=? AND id=?", (uid, coll, id_))
    bump(uid)


def count_docs(uid):
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM docs WHERE uid=?", (uid,)).fetchone()[0]


def user_symbols(uid):
    """Symbols this account has traded: the only closes and daily bars it may read."""
    return {str(d["data"].get("symbol", "")).upper() for d in list_docs(uid, "trades")}


# ---------------------------------------------------------------- accounts
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 240000).hex()


def check_hash(secret, stored):
    if not stored:
        hash_pw(secret)  # same work as a real check, so a missing account does not answer faster
        return False
    return hmac.compare_digest(hash_pw(secret, stored.split("$", 1)[0]), stored)


def get_user(name):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()


def get_user_by_id(uid):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def user_count():
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def registration_open():
    return get_meta("registration") != "off"


def create_user(name, pw_hash):
    try:
        with write_lock, db() as con:
            return con.execute("INSERT INTO users(name, pw, created) VALUES(?,?,?)", (name, pw_hash, now_iso())).lastrowid
    except sqlite3.IntegrityError:
        return None


def set_password(uid, pw_hash):
    """New password; every device signed in to this account is signed out."""
    with write_lock, db() as con:
        con.execute("UPDATE users SET pw=?, reset_hash=NULL, reset_expires=NULL WHERE id=?", (pw_hash, uid))
        con.execute("DELETE FROM sessions WHERE uid=?", (uid,))


def set_reset_code(uid):
    code = secrets.token_hex(4)
    with write_lock, db() as con:
        con.execute("UPDATE users SET reset_hash=?, reset_expires=? WHERE id=?", (hash_pw(code), time.time() + RESET_HOURS * 3600, uid))
        con.execute("DELETE FROM sessions WHERE uid=?", (uid,))
    return code


def delete_user(uid):
    with write_lock, db() as con:
        con.execute("DELETE FROM docs WHERE uid=?", (uid,))
        con.execute("DELETE FROM sessions WHERE uid=?", (uid,))
        con.execute("DELETE FROM users WHERE id=?", (uid,))
        con.execute("DELETE FROM meta WHERE k=?", ("version:%d" % uid,))


def new_session(uid):
    token = secrets.token_urlsafe(32)
    with write_lock, db() as con:
        con.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        con.execute("INSERT INTO sessions(token, uid, expires) VALUES(?,?,?)", (token, uid, time.time() + SESSION_DAYS * 86400))
    return token


def session_uid(token):
    if not token:
        return None
    with db() as con:
        r = con.execute("SELECT uid, expires FROM sessions WHERE token=?", (token,)).fetchone()
    return r["uid"] if r and r["expires"] > time.time() else None


def drop_session(token):
    with write_lock, db() as con:
        con.execute("DELETE FROM sessions WHERE token=?", (token,))


def too_many(book, ip, limit, window):
    c = book.get(ip)
    if c and time.time() - c[1] > window:
        book.pop(ip, None)
        return False
    return bool(c and c[0] >= limit)


def note(book, ip):
    c = book.setdefault(ip, [0, time.time()])
    c[0] += 1


def locked_out(ip):
    return too_many(fails, ip, 5, 900)


# ---------------------------------------------------------------- market data
def fetch(url, referer="https://gu.qq.com/"):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("gbk", errors="replace")


def is_hk(sym):
    return sym.isdigit()


def tq_code(sym):
    return "hk" + sym.zfill(5) if is_hk(sym) else "us" + sym


def in_session(sym, now=None):
    """Whether the stock's regular session is under way right now."""
    if is_hk(sym):
        t = (now or dt.datetime.now(HK)).astimezone(HK)
        m = t.hour * 60 + t.minute
        return t.weekday() < 5 and (570 <= m < 720 or 780 <= m < 960)
    t = (now or dt.datetime.now(NY)).astimezone(NY)
    m = t.hour * 60 + t.minute
    return t.weekday() < 5 and 570 <= m < 960


def quotes(symbols):
    """Latest price per symbol from Tencent: {sym: {price, date, kind, currency, name, code}}."""
    out, errors = {}, {}
    if not symbols:
        return out, errors
    text = fetch("https://qt.gtimg.cn/q=" + ",".join(tq_code(s) for s in symbols))
    by_code = {}
    for line in text.split(";"):
        if '="' not in line:
            continue
        key, val = line.strip().split('="', 1)
        by_code[key.replace("v_", "", 1)] = val.rstrip('"').split("~")
    for s in symbols:
        f = by_code.get(tq_code(s))
        try:
            price = float(f[3])
            if not price > 0:
                raise ValueError("no price")
            stamp = f[30].replace("/", "-")[:10]
            hk = is_hk(s)
            out[s] = {"price": price, "date": stamp, "currency": "HKD" if hk else (f[35] or "USD"), "name": f[1],
                      "code": f[2], "kind": "live" if in_session(s) else "close"}
        except Exception:
            errors[s] = "腾讯财经没有返回这只股票的价格"
    return out, errors


def daily_bars(sym, code, days=320):
    """Unadjusted daily bars from Tencent as [date, open, high, low, close, volume]."""
    param = ("hk" + sym.zfill(5)) if is_hk(sym) else ("us" + code)
    j = json.loads(fetch("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d," % (param, days)))
    rows = next(iter(j["data"].values())).get("day") or []
    out = []
    for r in rows:
        o, c, hi, lo, v = (float(x) for x in r[1:6])  # Tencent order: date, open, close, high, low, volume
        if min(o, c, hi, lo) > 0:
            out.append([r[0], round(o, 4), round(hi, 4), round(lo, 4), round(c, 4), int(v)])
    return out


def gold_bars():
    """COMEX gold futures (front month) daily bars from Sina since 2016, as [date, open, high, low, close]."""
    text = fetch("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_GC=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=GC",
                 referer="https://finance.sina.com.cn/")
    rows = json.loads(text[text.index("(") + 1:text.rindex(")")])
    out = []
    for r in rows:
        o, hi, lo, c = (float(r[k]) for k in ("open", "high", "low", "close"))
        if min(o, hi, lo, c) > 0:
            out.append([r["date"], o, hi, lo, c])
    return out


def usdcny_bars(since="2005-07-21"):
    """Onshore USD/CNY daily bars from Sina since the 2005 exchange-rate reform, as [date, open, high, low, close]."""
    text = fetch("https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20_fx=/NewForexService.getDayKLine?symbol=fx_susdcny",
                 referer="https://finance.sina.com.cn/")
    body = text[text.index('("') + 2:text.rindex('")')]
    out = []
    for row in body.split("|"):
        f = row.split(",")
        if len(f) < 5 or f[0] < since:
            continue
        o, lo, hi, c = (float(x) for x in f[1:5])  # Sina order: date, open, low, high, close
        if min(o, lo, hi, c) > 0:
            out.append([f[0], o, hi, lo, c])
    return out


def fx_rates():
    text = fetch("https://hq.sinajs.cn/list=fx_susdhkd", referer="https://finance.sina.com.cn/")
    rates = {}
    for line in text.split(";"):
        if "hq_str_fx_s" in line and '="' in line:
            name = line.split("hq_str_fx_s", 1)[1].split("=", 1)[0]
            f = line.split('="', 1)[1].rstrip('"').split(",")
            rates[name.upper()] = float(f[1])
    return rates


def refresh_market(uid):
    """Update one account's prices, quotes and rates, and the shared closes, daily bars, gold and USD/CNY."""
    trades = [d["data"] for d in list_docs(uid, "trades")]
    cfg = get_doc(uid, "config", "main") or {}
    traded = sorted({s for s in (str(t.get("symbol", "")).upper() for t in trades) if SYMBOL_RE.match(s)})
    symbols = sorted(set(traded) | {s for s in (cfg.get("prices") or {}) if SYMBOL_RE.match(s)})
    report = {"updated": {}, "kept_manual": [], "errors": {}, "history": {}}
    q, errs = quotes(symbols)
    report["errors"].update(errs)
    prices, quotes_meta = dict(cfg.get("prices") or {}), dict(cfg.get("quotes") or {})
    for s, x in q.items():
        old = quotes_meta.get(s) or {}
        if old.get("source") == "手动" and str(old.get("date", "")) > x["date"]:
            report["kept_manual"].append(s)
            continue
        prices[s] = x["price"]
        quotes_meta[s] = {"date": x["date"], "kind": x["kind"], "source": "腾讯财经", "currency": x["currency"], "name": x["name"]}
        report["updated"][s] = {"price": x["price"], "date": x["date"], "kind": x["kind"]}
    patch = {"prices": prices, "quotes": quotes_meta, "updatedAt": now_iso(), "source": "腾讯财经、新浪财经"}
    try:
        r = fx_rates()
        if 7.7 <= r.get("USDHKD", 0) <= 7.9:
            patch["fx"] = round(r["USDHKD"], 4)
        report["fx"] = {"USDHKD": patch.get("fx")}
    except Exception as e:
        report["errors"]["fx"] = "汇率取不到：%s" % e
    put_doc(uid, "config", "main", patch, merge=True)
    try:
        g = gold_bars()
        if g:
            put_doc(0, "watch", "GC", {"name": "纽约黄金期货（COMEX 近月）", "unit": "美元/盎司", "bars": g, "updatedAt": now_iso()})
            report["gold"] = {"price": g[-1][4], "date": g[-1][0]}
    except Exception as e:
        report["errors"]["金价"] = str(e)
    try:
        fxb = usdcny_bars()
        if fxb:
            put_doc(0, "watch", "USDCNY", {"name": "美元兑人民币（在岸）", "unit": "人民币/美元", "bars": fxb, "updatedAt": now_iso()})
            report["usdcny"] = {"rate": fxb[-1][4], "date": fxb[-1][0]}
    except Exception as e:
        report["errors"]["人民币汇率"] = str(e)
    for s in traded:
        try:
            code = (q.get(s) or {}).get("code") or s
            bars = daily_bars(s, code)
            if bars:
                ccy = "HKD" if is_hk(s) else "USD"
                put_doc(0, "history", s, {"symbol": s, "currency": ccy, "closes": {b[0]: b[4] for b in bars}, "updatedAt": now_iso()})
                put_doc(0, "candles", s, {"symbol": s, "currency": ccy, "name": (q.get(s) or {}).get("name") or quotes_meta.get(s, {}).get("name", ""),
                                          "bars": bars, "updatedAt": now_iso()})
                report["history"][s] = len(bars)
        except Exception as e:
            report["errors"][s + " 历史"] = str(e)
    return report


# ---------------------------------------------------------------- import and screenshot recognition
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def clean_trade(t):
    """A trade from an import, checked and reduced to the fields the ledger uses; None when it is not usable."""
    if not isinstance(t, dict):
        return None
    sym = str(t.get("symbol", "")).strip().upper()
    try:
        shares, price, fees = float(t.get("shares")), float(t.get("price")), float(t.get("fees") or 0)
    except (TypeError, ValueError):
        return None
    if not (SYMBOL_RE.match(sym) and DATE_RE.match(str(t.get("date", ""))) and t.get("side") in ("buy", "sell")
            and shares > 0 and price > 0 and fees >= 0):
        return None
    hk = sym.isdigit()
    return {"date": t["date"], "side": t["side"], "symbol": sym, "market": "HK" if hk else "US", "currency": "HKD" if hk else "USD",
            "shares": shares, "price": price, "fees": fees, "note": str(t.get("note") or "")[:120], "createdAt": now_iso()}


def import_records(uid, trades, fixed):
    rows = [("trades", t) for t in trades] + [("fixed", f) for f in fixed]
    with write_lock, db() as con:
        for coll, data in rows:
            id_ = "i" + dt.datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(4)
            con.execute("INSERT INTO docs(uid,coll,id,data,updated) VALUES(?,?,?,?,?)", (uid, coll, id_, json.dumps(data, ensure_ascii=False), now_iso()))
    bump(uid)


def vision_key():
    return get_meta("vision_key") or os.environ.get("DASHSCOPE_API_KEY", "")


def recognize(image_url):
    """Trades read from one screenshot by the vision model, as a list of dicts (not yet checked)."""
    body = {"model": get_meta("vision_model") or VISION_MODEL, "temperature": 0, "messages": [
        {"role": "system", "content": VISION_PROMPT},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": image_url}}, {"type": "text", "text": "提取这张截图里的成交记录。"}]},
    ]}
    req = urllib.request.Request(VISION_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + vision_key()})
    with urllib.request.urlopen(req, timeout=90) as r:
        text = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
    if isinstance(text, list):
        text = "".join(str(x.get("text", "")) for x in text if isinstance(x, dict))
    a, b = text.find("["), text.rfind("]")
    rows = json.loads(text[a:b + 1]) if a >= 0 and b > a else []
    return [r for r in rows if isinstance(r, dict)][:200]


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "ledger"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.client_ip(), fmt % args))

    def client_ip(self):
        # X-Real-IP is set by the reverse proxy; trust it only from a local or private peer, so a client that
        # reaches the server directly cannot dodge the login lockout by sending its own header.
        peer = self.client_address[0]
        try:
            proxied = ipaddress.ip_address(peer).is_private
        except ValueError:
            proxied = False
        return (self.headers.get("X-Real-IP") or peer) if proxied else peer

    def token(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        return c["sid"].value if "sid" in c else None

    def send_json(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, name, ctype="text/html; charset=utf-8", cache="no-cache"):
        path = os.path.join(STATIC, name)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit=MAX_BODY):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too large")
        raw = self.rfile.read(n) if n else b"{}"
        obj = json.loads(raw or b"{}")
        if not isinstance(obj, dict):
            raise ValueError("expected an object")
        return obj

    def signed_in(self, uid):
        return self.send_json(200, {"ok": True}, {"Set-Cookie": self.cookie(new_session(uid))})

    def route(self, method):
        path = self.path.split("?", 1)[0]
        uid = session_uid(self.token())
        if method == "GET" and path in PAGES:
            return self.send_file(PAGES[path] if uid else "login.html")
        if method == "GET" and path in PUBLIC_PAGES:
            return self.send_file(PUBLIC_PAGES[path])
        if method == "GET" and path.startswith("/assets/") and path[8:] in ASSETS:
            return self.send_file(path[8:], ASSETS[path[8:]], "public, max-age=31536000, immutable")
        if not path.startswith("/api/"):
            return self.send_json(404, {"error": "not found"})
        if method != "GET" and self.headers.get("X-Requested-With") != "ledger":
            return self.send_json(403, {"error": "forbidden"})
        parts = path[5:].strip("/").split("/")
        ip = self.client_ip()

        # ---- signed out: sign in, sign up, reset a forgotten password
        if parts == ["auth"] and method == "GET":
            u = get_user_by_id(uid) if uid else None
            return self.send_json(200, {"authed": bool(u), "name": u["name"] if u else None, "registration": registration_open()})
        if parts == ["login"] and method == "POST":
            if locked_out(ip):
                return self.send_json(429, {"error": "错误次数太多，15 分钟后再试。"})
            b = self.body()
            u = get_user(str(b.get("name", "")).strip())
            if check_hash(str(b.get("password", "")), u["pw"] if u else "") and u:
                fails.pop(ip, None)
                return self.signed_in(u["id"])
            note(fails, ip)
            return self.send_json(401, {"error": "用户名或密码不对。"})
        if parts == ["register"] and method == "POST":
            if not registration_open():
                return self.send_json(403, {"error": "现在不开放注册。"})
            if too_many(signups, ip, 3, 3600):
                return self.send_json(429, {"error": "注册太频繁，一小时后再试。"})
            b = self.body()
            name, pw = str(b.get("name", "")).strip(), str(b.get("password", ""))
            if b.get("agree") is not True:
                return self.send_json(400, {"error": "请先阅读并同意隐私说明。"})
            if not NAME_RE.match(name):
                return self.send_json(400, {"error": "用户名要 2–20 个字，只能用字母、数字、下划线或汉字。"})
            if not 8 <= len(pw) <= 128:
                return self.send_json(400, {"error": "密码要 8–128 位。"})
            if user_count() >= MAX_USERS:
                return self.send_json(403, {"error": "注册人数已满。"})
            new = create_user(name, hash_pw(pw))
            if not new:
                return self.send_json(409, {"error": "这个用户名已经有人用了，换一个吧。"})
            note(signups, ip)
            return self.signed_in(new)
        if parts == ["reset"] and method == "POST":
            if locked_out(ip):
                return self.send_json(429, {"error": "错误次数太多，15 分钟后再试。"})
            b = self.body()
            u, pw = get_user(str(b.get("name", "")).strip()), str(b.get("password", ""))
            if not 8 <= len(pw) <= 128:
                return self.send_json(400, {"error": "新密码要 8–128 位。"})
            live = bool(u and u["reset_hash"] and (u["reset_expires"] or 0) > time.time())
            if not (check_hash(str(b.get("code", "")).strip(), u["reset_hash"] if live else "") and live):
                note(fails, ip)
                return self.send_json(401, {"error": "用户名或重置码不对，或者重置码已过期。"})
            fails.pop(ip, None)
            set_password(u["id"], hash_pw(pw))
            return self.signed_in(u["id"])

        if not uid:
            return self.send_json(401, {"error": "请先登录。"})

        # ---- signed in: the account itself
        if parts == ["logout"] and method == "POST":
            drop_session(self.token())
            return self.send_json(200, {"ok": True}, {"Set-Cookie": "sid=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})
        if parts == ["password"] and method == "POST":
            if locked_out(ip):
                return self.send_json(429, {"error": "错误次数太多，15 分钟后再试。"})
            b = self.body()
            pw = str(b.get("password", ""))
            if not check_hash(str(b.get("old", "")), get_user_by_id(uid)["pw"]):
                note(fails, ip)
                return self.send_json(401, {"error": "原密码不对。"})
            if not 8 <= len(pw) <= 128:
                return self.send_json(400, {"error": "新密码要 8–128 位。"})
            set_password(uid, hash_pw(pw))
            return self.signed_in(uid)
        if parts == ["export"] and method == "GET":
            return self.send_json(200, {c: {d["id"]: d["data"] for d in list_docs(uid, c)} for c in sorted(USER_COLLS)})
        if parts == ["account", "delete"] and method == "POST":
            if locked_out(ip):
                return self.send_json(429, {"error": "错误次数太多，15 分钟后再试。"})
            if not check_hash(str(self.body().get("password", "")), get_user_by_id(uid)["pw"]):
                note(fails, ip)
                return self.send_json(401, {"error": "密码不对。"})
            delete_user(uid)
            return self.send_json(200, {"ok": True}, {"Set-Cookie": "sid=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})
        if parts == ["version"] and method == "GET":
            return self.send_json(200, {"version": "%s-%s" % (get_meta("version:0") or "0", get_meta("version:%d" % uid) or "0")})
        if parts == ["refresh"] and method == "POST":
            wait = REFRESH_GAP - (time.time() - last_refresh.get(uid, 0))
            if wait > 0:
                return self.send_json(429, {"error": "刚刷新过，%d 秒后再试。" % math.ceil(wait)})
            if not refresh_lock.acquire(timeout=60):
                return self.send_json(503, {"error": "服务器正忙，稍后再试。"})
            try:
                last_refresh[uid] = time.time()
                return self.send_json(200, refresh_market(uid))
            except Exception as e:
                return self.send_json(502, {"error": "取行情失败：%s" % e})
            finally:
                refresh_lock.release()

        if parts == ["import"] and method == "POST":
            b = self.body()
            trades, fixed = b.get("trades") or [], b.get("fixed") or []
            if not isinstance(trades, list) or not isinstance(fixed, list) or len(trades) + len(fixed) > MAX_IMPORT:
                return self.send_json(400, {"error": "一次最多导入 %d 条。" % MAX_IMPORT})
            good = [clean_trade(t) for t in trades]
            if None in good:
                return self.send_json(400, {"error": "第 %d 笔交易的内容不完整或格式不对。" % (good.index(None) + 1)})
            fx = [f for f in fixed if isinstance(f, dict) and f.get("kind") in ("bond", "deposit")]
            if len(fx) != len(fixed):
                return self.send_json(400, {"error": "国债或定存记录的格式不对。"})
            if count_docs(uid) + len(good) + len(fx) > MAX_DOCS:
                return self.send_json(507, {"error": "记录太多了，每个账号最多 %d 条。" % MAX_DOCS})
            import_records(uid, good, fx)
            return self.send_json(200, {"trades": len(good), "fixed": len(fx)})
        if parts == ["recognize"] and method == "GET":
            today = dt.date.today().isoformat()
            return self.send_json(200, {"enabled": bool(vision_key()), "left": max(0, VISION_PER_DAY - vision_used.get((today, uid), 0))})
        if parts == ["recognize"] and method == "POST":
            if not vision_key():
                return self.send_json(503, {"error": "管理员还没有开通截图识别，可以先用粘贴表格导入。"})
            today = dt.date.today().isoformat()
            if vision_used.get((today, uid), 0) >= VISION_PER_DAY:
                return self.send_json(429, {"error": "今天的截图识别次数（%d 张）已用完，明天再来。" % VISION_PER_DAY})
            if vision_used.get((today, 0), 0) >= VISION_SITE_DAY:
                return self.send_json(429, {"error": "今天全站的截图识别次数已用完，明天再来。"})
            image = str(self.body(MAX_IMAGE_BODY).get("image", ""))
            if not re.match(r"^data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$", image):
                return self.send_json(400, {"error": "图片格式不对，请用 JPG 或 PNG 截图。"})
            used = ((today, uid), (today, 0))
            for k in used:
                vision_used[k] = vision_used.get(k, 0) + 1
            try:
                rows = recognize(image)
            except Exception as e:
                for k in used:  # a failed read does not count against the daily allowance
                    vision_used[k] -= 1
                if isinstance(e, urllib.error.HTTPError):
                    sys.stderr.write("vision: HTTP %s %s\n" % (e.code, e.read()[:300]))
                    return self.send_json(502, {"error": "识别服务出错（%s），稍后再试。" % e.code})
                sys.stderr.write("vision: %r\n" % e)
                return self.send_json(502, {"error": "识别没成功，换一张清楚点的截图再试。"})
            return self.send_json(200, {"trades": rows, "left": max(0, VISION_PER_DAY - vision_used[(today, uid)])})

        # ---- records
        if len(parts) >= 2 and parts[0] in ("c", "d") and parts[1] in SHARED_COLLS:
            coll = parts[1]
            if method != "GET":
                return self.send_json(403, {"error": "行情数据只能由服务器更新。"})
            mine = None if coll == "watch" else user_symbols(uid)  # other accounts' holdings stay private
            if parts[0] == "c" and len(parts) == 2:
                return self.send_json(200, {"docs": [d for d in list_docs(0, coll) if mine is None or d["id"] in mine]})
            if parts[0] == "d" and len(parts) == 3 and parts[2]:
                d = get_doc(0, coll, parts[2]) if mine is None or parts[2] in mine else None
                return self.send_json(200, {"exists": d is not None, "data": d})
        if len(parts) >= 2 and parts[0] in ("c", "d") and parts[1] in USER_COLLS:
            coll = parts[1]
            full = {"error": "记录太多了，每个账号最多 %d 条。" % MAX_DOCS}
            if parts[0] == "c" and len(parts) == 2:
                if method == "GET":
                    return self.send_json(200, {"docs": list_docs(uid, coll)})
                if method == "POST":
                    if count_docs(uid) >= MAX_DOCS:
                        return self.send_json(507, full)
                    id_ = "n" + dt.datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(3)
                    put_doc(uid, coll, id_, self.body())
                    return self.send_json(200, {"id": id_})
            if parts[0] == "d" and len(parts) == 3 and parts[2] and len(parts[2]) <= 120:
                id_ = parts[2]
                if method == "GET":
                    d = get_doc(uid, coll, id_)
                    return self.send_json(200, {"exists": d is not None, "data": d})
                if method in ("PUT", "PATCH"):
                    if get_doc(uid, coll, id_) is None and count_docs(uid) >= MAX_DOCS:
                        return self.send_json(507, full)
                    return self.send_json(200, {"data": put_doc(uid, coll, id_, self.body(), merge=method == "PATCH")})
                if method == "DELETE":
                    del_doc(uid, coll, id_)
                    return self.send_json(200, {"ok": True})
        return self.send_json(404, {"error": "not found"})

    def cookie(self, token):
        return "sid=%s; Path=/; Max-Age=%d; HttpOnly; Secure; SameSite=Strict" % (token, SESSION_DAYS * 86400)

    def handle_method(self, method):
        try:
            self.route(method)
        except (ValueError, json.JSONDecodeError) as e:
            self.send_json(400, {"error": "请求格式不对：%s" % e})
        except Exception as e:
            sys.stderr.write("error: %r\n" % e)
            self.send_json(500, {"error": "服务器出错了。"})

    def do_GET(self):
        self.handle_method("GET")

    def do_POST(self):
        self.handle_method("POST")

    def do_PUT(self):
        self.handle_method("PUT")

    def do_PATCH(self):
        self.handle_method("PATCH")

    def do_DELETE(self):
        self.handle_method("DELETE")


# ---------------------------------------------------------------- cli
def backup():
    d = os.path.join(DATA, "backups")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "ledger-%s.db" % dt.datetime.now(HK).strftime("%Y%m%d-%H%M"))
    src, out = sqlite3.connect(DB_PATH), sqlite3.connect(dst)
    with out:
        src.backup(out)
    src.close()
    out.close()
    for old in sorted(f for f in os.listdir(d) if f.startswith("ledger-"))[:-30]:
        os.remove(os.path.join(d, old))
    print(dst)


def arg(i, usage):
    if len(sys.argv) <= i:
        raise SystemExit("usage: python3 app.py " + usage)
    return sys.argv[i]


def account(name):
    u = get_user(name)
    if not u:
        raise SystemExit("no such account: " + name)
    return u


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    init_db()
    if cmd == "serve":
        host = sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv else "127.0.0.1"
        port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 5090
        print("%d accounts, registration %s; listening on %s:%d" % (user_count(), "open" if registration_open() else "closed", host, port), flush=True)
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    elif cmd == "users":
        with db() as con:
            rows = con.execute("SELECT u.name, u.created, COUNT(d.id) AS n FROM users u LEFT JOIN docs d ON d.uid = u.id "
                               "GROUP BY u.id ORDER BY u.id").fetchall()
        for r in rows:
            print("%-20s created %s  %d records" % (r["name"], r["created"], r["n"]))
        print("%d accounts, registration %s" % (len(rows), "open" if registration_open() else "closed"))
    elif cmd == "reset-password":
        u = account(arg(2, "reset-password NAME"))
        code = set_reset_code(u["id"])
        print("reset code for %s: %s  (valid %d hours; on the sign-in page choose 忘记密码 and enter it with a new password)"
              % (u["name"], code, RESET_HOURS))
    elif cmd == "delete-user":
        u = account(arg(2, "delete-user NAME"))
        delete_user(u["id"])
        print("deleted " + u["name"])
    elif cmd == "registration":
        v = arg(2, "registration on|off")
        if v not in ("on", "off"):
            raise SystemExit("usage: python3 app.py registration on|off")
        set_meta("registration", v)
        print("registration " + ("open" if v == "on" else "closed"))
    elif cmd == "import":
        u = account(arg(2, "import NAME seed.json"))
        with open(arg(3, "import NAME seed.json"), encoding="utf-8") as f:
            seed = json.load(f)
        for coll, docs in seed.items():
            if coll not in USER_COLLS:
                raise SystemExit("unknown collection: " + coll)
            for id_, data in docs.items():
                put_doc(u["id"], coll, id_, data)
            print(coll, len(docs))
    elif cmd == "backup":
        backup()
    elif cmd == "vision-key":
        if sys.argv[2:3] == ["off"]:
            with write_lock, db() as con:
                con.execute("DELETE FROM meta WHERE k='vision_key'")
            print("screenshot import off")
        else:
            import getpass
            key = getpass.getpass("DashScope API key (input hidden, paste once): ").strip()
            if not key.startswith("sk-"):
                raise SystemExit("that does not look like a DashScope key (it starts with sk-)")
            if key.count("sk-") > 1:
                raise SystemExit("the key looks pasted more than once; run the command again and paste it a single time")
            # Ask DashScope before saving, so a wrong key is caught now rather than on the first screenshot.
            check = urllib.request.Request(VISION_URL.replace("/chat/completions", "/models"), headers={"Authorization": "Bearer " + key})
            try:
                urllib.request.urlopen(check, timeout=20).close()
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise SystemExit("DashScope rejected this key (HTTP %d). Create one in the Model Studio (百炼) console for the "
                                     "China (Beijing) region and copy it in full; nothing was saved." % e.code)
                print("warning: could not check the key (HTTP %d); saving it anyway" % e.code)
            except Exception as e:
                print("warning: could not reach DashScope to check the key (%s); saving it anyway" % e)
            set_meta("vision_key", key)
            print("screenshot import on, model %s; restart not needed" % (get_meta("vision_model") or VISION_MODEL))
    elif cmd == "refresh":
        print(json.dumps(refresh_market(account(arg(2, "refresh NAME"))["id"]), ensure_ascii=False, indent=1))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
