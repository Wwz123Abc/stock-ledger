#!/usr/bin/env python3
"""美股持仓账本 — self-hosted server.

Standard library only (Python 3.10+). Serves the ledger pages, keeps their data in SQLite as small JSON
documents grouped in collections, and refreshes prices,
daily closes and exchange rates from Tencent Finance (quotes, daily bars) and Sina Finance (FX).

  python3 app.py serve [--host 127.0.0.1] [--port 5090]
  python3 app.py import seed.json        # load {collection: {doc_id: data}}, replacing those docs
  python3 app.py backup                  # copy the database to data/backups/, keep 30
  python3 app.py refresh                 # fetch market data once from the command line
  python3 app.py reset-password          # forgot the password: sign everyone out, write a new setup code
"""
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.request
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA, "ledger.db")
STATIC = os.path.join(ROOT, "static")
COLLECTIONS = {"trades", "history", "fixed", "config", "candles", "reading", "watch"}
ASSETS = {"d3.min.js": "text/javascript; charset=utf-8"}
SESSION_DAYS = 30
MAX_BODY = 256 * 1024
HK, NY = ZoneInfo("Asia/Hong_Kong"), ZoneInfo("America/New_York")
write_lock = threading.Lock()
fails = {}  # ip -> [count, first_failure_time]


# ---------------------------------------------------------------- storage
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(DATA, exist_ok=True)
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS docs (coll TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                updated TEXT NOT NULL, PRIMARY KEY (coll, id));
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, expires REAL NOT NULL);
        """)
        con.execute("PRAGMA journal_mode=WAL")
    if not get_meta("password"):
        path = os.path.join(DATA, "setup-code.txt")
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(4) + "\n")


def get_meta(k):
    with db() as con:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def set_meta(k, v):
    with write_lock, db() as con:
        con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def bump():
    set_meta("version", str(int(get_meta("version") or 0) + 1))


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def list_docs(coll):
    with db() as con:
        rows = con.execute("SELECT id, data FROM docs WHERE coll=? ORDER BY id", (coll,)).fetchall()
    return [{"id": r["id"], "data": json.loads(r["data"])} for r in rows]


def get_doc(coll, id_):
    with db() as con:
        r = con.execute("SELECT data FROM docs WHERE coll=? AND id=?", (coll, id_)).fetchone()
    return json.loads(r["data"]) if r else None


def put_doc(coll, id_, data, merge=False):
    with write_lock, db() as con:
        if merge:
            r = con.execute("SELECT data FROM docs WHERE coll=? AND id=?", (coll, id_)).fetchone()
            data = {**(json.loads(r["data"]) if r else {}), **data}
        con.execute("INSERT INTO docs(coll,id,data,updated) VALUES(?,?,?,?) ON CONFLICT(coll,id) DO UPDATE "
                    "SET data=excluded.data, updated=excluded.updated", (coll, id_, json.dumps(data, ensure_ascii=False), now_iso()))
    bump()
    return data


def del_doc(coll, id_):
    with write_lock, db() as con:
        con.execute("DELETE FROM docs WHERE coll=? AND id=?", (coll, id_))
    bump()


# ---------------------------------------------------------------- auth
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 240000).hex()


def check_pw(pw):
    stored = get_meta("password")
    if not stored:
        return False
    salt = stored.split("$", 1)[0]
    return hmac.compare_digest(hash_pw(pw, salt), stored)


def new_session():
    token = secrets.token_urlsafe(32)
    with write_lock, db() as con:
        con.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        con.execute("INSERT INTO sessions(token, expires) VALUES(?,?)", (token, time.time() + SESSION_DAYS * 86400))
    return token


def valid_session(token):
    if not token:
        return False
    with db() as con:
        r = con.execute("SELECT expires FROM sessions WHERE token=?", (token,)).fetchone()
    return bool(r and r["expires"] > time.time())


def drop_session(token):
    with write_lock, db() as con:
        con.execute("DELETE FROM sessions WHERE token=?", (token,))


def locked_out(ip):
    c = fails.get(ip)
    if c and time.time() - c[1] > 900:
        fails.pop(ip, None)
        return False
    return bool(c and c[0] >= 5)


def note_fail(ip):
    c = fails.setdefault(ip, [0, time.time()])
    c[0] += 1


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


def refresh_market():
    """Update config/main prices, quotes and rates, and history closes for traded symbols."""
    trades = [d["data"] for d in list_docs("trades")]
    cfg = get_doc("config", "main") or {}
    traded = sorted({str(t.get("symbol", "")).upper() for t in trades if t.get("symbol")})
    symbols = sorted(set(traded) | set((cfg.get("prices") or {}).keys()))
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
    put_doc("config", "main", patch, merge=True)
    try:
        g = gold_bars()
        if g:
            put_doc("watch", "GC", {"name": "纽约黄金期货（COMEX 近月）", "unit": "美元/盎司", "bars": g, "updatedAt": now_iso()})
            report["gold"] = {"price": g[-1][4], "date": g[-1][0]}
    except Exception as e:
        report["errors"]["金价"] = str(e)
    try:
        fxb = usdcny_bars()
        if fxb:
            put_doc("watch", "USDCNY", {"name": "美元兑人民币（在岸）", "unit": "人民币/美元", "bars": fxb, "updatedAt": now_iso()})
            report["usdcny"] = {"rate": fxb[-1][4], "date": fxb[-1][0]}
    except Exception as e:
        report["errors"]["人民币汇率"] = str(e)
    for s in traded:
        try:
            code = (q.get(s) or {}).get("code") or s
            bars = daily_bars(s, code)
            if bars:
                ccy = "HKD" if is_hk(s) else "USD"
                put_doc("history", s, {"symbol": s, "currency": ccy, "closes": {b[0]: b[4] for b in bars}, "updatedAt": now_iso()})
                put_doc("candles", s, {"symbol": s, "currency": ccy, "name": (q.get(s) or {}).get("name") or quotes_meta.get(s, {}).get("name", ""),
                                       "bars": bars, "updatedAt": now_iso()})
                report["history"][s] = len(bars)
        except Exception as e:
            report["errors"][s + " 历史"] = str(e)
    return report


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

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("too large")
        raw = self.rfile.read(n) if n else b"{}"
        obj = json.loads(raw or b"{}")
        if not isinstance(obj, dict):
            raise ValueError("expected an object")
        return obj

    def route(self, method):
        path = self.path.split("?", 1)[0]
        authed = valid_session(self.token())
        if method == "GET" and path in ("/", "/index.html"):
            return self.send_file("index.html" if authed else "login.html")
        if method == "GET" and path in ("/dashboard", "/reading"):
            return self.send_file(path[1:] + ".html" if authed else "login.html")
        if method == "GET" and path.startswith("/assets/") and path[8:] in ASSETS:
            return self.send_file(path[8:], ASSETS[path[8:]], "public, max-age=31536000, immutable")
        if not path.startswith("/api/"):
            return self.send_json(404, {"error": "not found"})
        if method != "GET" and self.headers.get("X-Requested-With") != "ledger":
            return self.send_json(403, {"error": "forbidden"})
        parts = path[5:].strip("/").split("/")

        if parts == ["auth"] and method == "GET":
            return self.send_json(200, {"authed": authed, "setup": not get_meta("password")})
        if parts == ["login"] and method == "POST":
            ip = self.client_ip()
            if locked_out(ip):
                return self.send_json(429, {"error": "密码错误次数太多，15 分钟后再试。"})
            if check_pw(str(self.body().get("password", ""))):
                fails.pop(ip, None)
                return self.send_json(200, {"ok": True}, {"Set-Cookie": self.cookie(new_session())})
            note_fail(ip)
            return self.send_json(401, {"error": "密码不对。"})
        if parts == ["setup"] and method == "POST":
            b = self.body()
            path_code = os.path.join(DATA, "setup-code.txt")
            if get_meta("password") or not os.path.exists(path_code):
                return self.send_json(403, {"error": "已经设置过密码。"})
            ip = self.client_ip()
            if locked_out(ip):
                return self.send_json(429, {"error": "错误次数太多，15 分钟后再试。"})
            with open(path_code) as f:
                code = f.read().strip()
            pw = str(b.get("password", ""))
            if not hmac.compare_digest(str(b.get("code", "")).strip(), code):
                note_fail(ip)
                return self.send_json(401, {"error": "设置码不对。"})
            if len(pw) < 8:
                return self.send_json(400, {"error": "密码至少 8 位。"})
            set_meta("password", hash_pw(pw))
            os.remove(path_code)
            return self.send_json(200, {"ok": True}, {"Set-Cookie": self.cookie(new_session())})

        if not authed:
            return self.send_json(401, {"error": "请先登录。"})
        if parts == ["logout"] and method == "POST":
            drop_session(self.token())
            return self.send_json(200, {"ok": True}, {"Set-Cookie": "sid=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})
        if parts == ["version"] and method == "GET":
            return self.send_json(200, {"version": get_meta("version") or "0"})
        if parts == ["refresh"] and method == "POST":
            try:
                return self.send_json(200, refresh_market())
            except Exception as e:
                return self.send_json(502, {"error": "取行情失败：%s" % e})

        if len(parts) >= 2 and parts[0] in ("c", "d") and parts[1] in COLLECTIONS:
            coll = parts[1]
            if parts[0] == "c" and len(parts) == 2:
                if method == "GET":
                    return self.send_json(200, {"docs": list_docs(coll)})
                if method == "POST":
                    id_ = "n" + dt.datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(3)
                    put_doc(coll, id_, self.body())
                    return self.send_json(200, {"id": id_})
            if parts[0] == "d" and len(parts) == 3 and parts[2] and len(parts[2]) <= 120:
                id_ = parts[2]
                if method == "GET":
                    d = get_doc(coll, id_)
                    return self.send_json(200, {"exists": d is not None, "data": d})
                if method == "PUT":
                    return self.send_json(200, {"data": put_doc(coll, id_, self.body())})
                if method == "PATCH":
                    return self.send_json(200, {"data": put_doc(coll, id_, self.body(), merge=True)})
                if method == "DELETE":
                    del_doc(coll, id_)
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


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    init_db()
    if cmd == "serve":
        host = sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv else "127.0.0.1"
        port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 5090
        if not get_meta("password"):
            print("first run: open the page and enter the setup code from %s" % os.path.join(DATA, "setup-code.txt"), flush=True)
        print("listening on %s:%d" % (host, port), flush=True)
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    elif cmd == "import":
        with open(sys.argv[2], encoding="utf-8") as f:
            seed = json.load(f)
        for coll, docs in seed.items():
            if coll not in COLLECTIONS:
                raise SystemExit("unknown collection: " + coll)
            for id_, data in docs.items():
                put_doc(coll, id_, data)
            print(coll, len(docs))
    elif cmd == "backup":
        backup()
    elif cmd == "refresh":
        print(json.dumps(refresh_market(), ensure_ascii=False, indent=1))
    elif cmd == "reset-password":
        with write_lock, db() as con:
            con.execute("DELETE FROM meta WHERE k='password'")
            con.execute("DELETE FROM sessions")
        init_db()
        print("password cleared; open the page and set a new one with the setup code in %s" % os.path.join(DATA, "setup-code.txt"))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
