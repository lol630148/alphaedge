#!/usr/bin/env python3
"""
AI Quant Research Terminal — Flask Backend
Real market data via yfinance (Yahoo Finance, brezplacno)
Zageni: python app.py   -->  odpri http://localhost:5001
"""

from flask import Flask, jsonify, render_template, request, session, redirect, url_for
import yfinance as yf
import pandas as pd
import numpy as np
import time, traceback, datetime, sqlite3, os, secrets, smtplib, threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# ── USER DATABASE ────────────────────────────────────────────
# Railway persistent volume is mounted at /data, fallback to local for dev
_data_dir = '/data' if os.path.isdir('/data') else os.path.dirname(__file__)
DB_PATH = os.path.join(_data_dir, 'users.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email    TEXT,
        password_hash TEXT NOT NULL,
        role          TEXT DEFAULT 'user',
        is_active     INTEGER DEFAULT 1,
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Add role column if upgrading from old DB without it
    try:
        c.execute('ALTER TABLE users ADD COLUMN role TEXT DEFAULT "user"')
    except Exception:
        pass
    c.execute('''CREATE TABLE IF NOT EXISTS alert_config (
        id         INTEGER PRIMARY KEY DEFAULT 1,
        enabled    INTEGER DEFAULT 0,
        smtp_host  TEXT DEFAULT 'smtp.gmail.com',
        smtp_port  INTEGER DEFAULT 587,
        smtp_user  TEXT DEFAULT '',
        smtp_pass  TEXT DEFAULT '',
        recipients TEXT DEFAULT '',
        send_time  TEXT DEFAULT '07:00',
        min_signal TEXT DEFAULT 'HIGH'
    )''')
    c.execute('INSERT OR IGNORE INTO alert_config (id) VALUES (1)')

    # Watchlist per user
    c.execute('''CREATE TABLE IF NOT EXISTS watchlist (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        ticker     TEXT NOT NULL,
        added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, ticker)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS portfolio (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        ticker      TEXT NOT NULL,
        direction   TEXT NOT NULL DEFAULT 'LONG',
        qty         REAL NOT NULL,
        entry_price REAL NOT NULL,
        stop_pct    REAL NOT NULL DEFAULT 5.0,
        sector      TEXT DEFAULT 'Unknown',
        added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Create default admin on first run
    c.execute('SELECT COUNT(*) FROM users')
    if c.fetchone()[0] == 0:
        c.execute('INSERT INTO users (username, email, password_hash, role) VALUES (?,?,?,?)',
                  ('admin', '', generate_password_hash('admin123'), 'admin'))
        print('  ✔ Default user created: admin / admin123')
        print('  ⚠  SPREMENI geslo po prvem vpisu!')
    conn.commit()
    conn.close()

def get_user(username):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    u = conn.execute('SELECT * FROM users WHERE username=? AND is_active=1',
                     (username,)).fetchone()
    conn.close()
    return u

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT id,username,email,role,is_active,created_at FROM users ORDER BY id').fetchall()
    conn.close()
    return [dict(r) for r in rows]

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized', 'redirect': '/login'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        if session.get('role') != 'admin':
            return jsonify({'error': 'Samo admin ima dostop do tega'}), 403
        return f(*args, **kwargs)
    return decorated


# ── ALERT CONFIG ─────────────────────────────────────────────
def get_alert_config():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM alert_config WHERE id=1').fetchone()
    conn.close()
    return dict(row) if row else {}

def save_alert_config(data):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE alert_config SET enabled=?,smtp_host=?,smtp_port=?,smtp_user=?,"
        "smtp_pass=?,recipients=?,send_time=?,min_signal=? WHERE id=1",
        (1 if data.get('enabled') else 0,
         data.get('smtp_host','smtp.gmail.com'),
         int(data.get('smtp_port',587)),
         data.get('smtp_user',''),
         data.get('smtp_pass',''),
         data.get('recipients',''),
         data.get('send_time','07:00'),
         data.get('min_signal','HIGH')))
    conn.commit(); conn.close()

def build_alert_email_html(filtered, min_sig):
    sig_colors = {'CRITICAL':'#cc0022','HIGH':'#cc5500','MEDIUM':'#997700','LOW':'#555'}
    rows_html = ''
    for r in filtered[:20]:
        c = sig_colors.get(r['signal'],'#555')
        price = '$' + str(r['price'])
        days  = str(r['days_to_earn']) + 'd'
        cd    = ('+' if r['corr_delta']>=0 else '') + str(round(r['corr_delta'],3))
        vr    = str(round(r['vol_ratio'],2)) + 'x'
        md    = ('+' if r['mom_div']>=0 else '') + str(round(r['mom_div'],2)) + '%'
        sc    = str(round(r['anomaly_score'],1))
        rows_html += (
            '<tr>'
            '<td style="padding:5px 8px;border-bottom:1px solid #eee;font-weight:bold">' + r['ticker'] + '</td>'
            '<td style="padding:5px 8px;border-bottom:1px solid #eee;color:#555">' + r.get('sector','') + '</td>'
            '<td style="padding:5px 8px;border-bottom:1px solid #eee">' + price + '</td>'
            '<td style="padding:5px 8px;border-bottom:1px solid #eee;font-weight:bold">' + days + '</td>'
            '<td style="padding:5px 8px;border-bottom:1px solid #eee">' + cd + '</td>'
            '<td style="padding:5px 8px;border-bottom:1px solid #eee">' + vr + '</td>'
            '<td style="padding:5px 8px;border-bottom:1px solid #eee">' + md + '</td>'
            '<td style="padding:5px 8px;border-bottom:1px solid #eee;font-weight:bold">' + sc + '</td>'
            '<td style="padding:5px 8px;border-bottom:1px solid #eee">'
            '<span style="background:' + c + '20;color:' + c + ';padding:2px 8px;border-radius:3px;font-weight:bold">' + r['signal'] + '</span>'
            '</td></tr>'
        )
    dt = datetime.datetime.now().strftime('%d.%m.%Y %H:%M')
    n = len(filtered)
    return (
        '<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:20px">'
        '<div style="max-width:720px;margin:0 auto;background:#fff;border-radius:4px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)">'
        '<div style="background:#05080f;padding:20px 24px;border-bottom:3px solid #00d4ff">'
        '<div style="color:#00d4ff;font-size:20px;font-weight:bold;letter-spacing:3px;font-family:monospace">QUANT TERMINAL</div>'
        '<div style="color:#7090b0;font-size:11px;margin-top:4px">PRE-EARNINGS ANOMALY ALERT — ' + dt + '</div>'
        '</div>'
        '<div style="padding:20px 24px">'
        '<p style="color:#333;margin-bottom:16px">Najdenih <b>' + str(n) + '</b> anomalij pred earnings (signal &ge; ' + min_sig + '):</p>'
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead><tr style="background:#f8f8f8">'
        '<th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ddd">Ticker</th>'
        '<th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ddd">Sektor</th>'
        '<th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ddd">Cena</th>'
        '<th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ddd">Dni</th>'
        '<th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ddd">Corr D</th>'
        '<th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ddd">Vol</th>'
        '<th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ddd">Mom</th>'
        '<th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ddd">Score</th>'
        '<th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ddd">Signal</th>'
        '</tr></thead>'
        '<tbody>' + rows_html + '</tbody></table>'
        '<p style="color:#999;font-size:11px;margin-top:16px;border-top:1px solid #eee;padding-top:12px">'
        'NOT FINANCIAL ADVICE - FOR RESEARCH ONLY | Quant Terminal | ' + dt + '</p>'
        '</div></div></body></html>'
    )

def send_alert_email(cfg, results):
    if not cfg.get('smtp_user') or not cfg.get('recipients'):
        return False, 'Manjka SMTP user ali recipients'
    sig_order = ['CRITICAL','HIGH','MEDIUM','LOW']
    min_sig = cfg.get('min_signal','HIGH')
    min_idx = sig_order.index(min_sig) if min_sig in sig_order else 1
    filtered = [r for r in results if sig_order.index(r['signal']) <= min_idx]
    if not filtered:
        return False, 'Ni anomalij nad minimalno stopnjo — email ni poslan'
    try:
        html_body = build_alert_email_html(filtered, min_sig)
        msg = MIMEMultipart('alternative')
        today_str = datetime.date.today().strftime('%d.%m.%Y')
        msg['Subject'] = '[Quant Terminal] ' + str(len(filtered)) + ' anomalij pred earnings — ' + today_str
        msg['From'] = cfg['smtp_user']
        msg['To'] = cfg['recipients']
        msg.attach(MIMEText(html_body, 'html'))
        with smtplib.SMTP(cfg['smtp_host'], int(cfg.get('smtp_port',587))) as srv:
            srv.starttls()
            srv.login(cfg['smtp_user'], cfg['smtp_pass'])
            srv.send_message(msg)
        return True, 'Email poslan na ' + cfg['recipients']
    except Exception as e:
        return False, str(e)

_last_alert_date = None

def alert_scheduler():
    global _last_alert_date
    while True:
        time.sleep(60)
        try:
            cfg = get_alert_config()
            if not cfg.get('enabled'):
                continue
            now = datetime.datetime.now()
            send_time = cfg.get('send_time','07:00')
            h, m = map(int, send_time.split(':'))
            today = now.date()
            if now.hour == h and now.minute == m and _last_alert_date != today:
                _last_alert_date = today
                scan_data = cached('anomaly_scan', lambda: None, ttl=0)
                if scan_data and scan_data.get('results'):
                    ok, msg_txt = send_alert_email(cfg, scan_data['results'])
                    print('  [ALERT] ' + now.strftime('%H:%M') + ' — ' + msg_txt)
        except Exception as e:
            print('  [ALERT ERROR] ' + str(e))

# ── SIMPLE IN-MEMORY CACHE ──────────────────────────────────
_cache, _cache_ts = {}, {}

def cached(key, fn, ttl=180):
    now = time.time()
    if key in _cache and (now - _cache_ts.get(key, 0)) < ttl:
        return _cache[key]
    result = fn()
    _cache[key] = result
    _cache_ts[key] = now
    return result

# ── UNIVERSE ────────────────────────────────────────────────
UNIVERSE = [
    {'t':'AAPL', 'n':'Apple Inc',        's':'Technology', 'b':1.20},
    {'t':'MSFT', 'n':'Microsoft Corp',   's':'Technology', 'b':0.90},
    {'t':'NVDA', 'n':'NVIDIA Corp',      's':'Technology', 'b':1.80},
    {'t':'GOOGL','n':'Alphabet Inc',     's':'Technology', 'b':1.05},
    {'t':'META', 'n':'Meta Platforms',   's':'Technology', 'b':1.30},
    {'t':'AMZN', 'n':'Amazon.com',       's':'Consumer',   'b':1.15},
    {'t':'TSLA', 'n':'Tesla Inc',        's':'Consumer',   'b':2.10},
    {'t':'JPM',  'n':'JPMorgan Chase',   's':'Financials', 'b':1.10},
    {'t':'V',    'n':'Visa Inc',         's':'Financials', 'b':0.95},
    {'t':'MA',   'n':'Mastercard',       's':'Financials', 'b':1.00},
    {'t':'JNJ',  'n':'Johnson & Johnson','s':'Healthcare', 'b':0.65},
    {'t':'UNH',  'n':'UnitedHealth',     's':'Healthcare', 'b':0.75},
    {'t':'LLY',  'n':'Eli Lilly',        's':'Healthcare', 'b':0.85},
    {'t':'XOM',  'n':'Exxon Mobil',      's':'Energy',     'b':0.80},
    {'t':'CVX',  'n':'Chevron Corp',     's':'Energy',     'b':0.85},
    {'t':'HD',   'n':'Home Depot',       's':'Consumer',   'b':1.05},
    {'t':'AVGO', 'n':'Broadcom Inc',     's':'Technology', 'b':1.40},
    {'t':'PG',   'n':'Procter & Gamble', 's':'Consumer',   'b':0.55},
    {'t':'MRK',  'n':'Merck & Co',       's':'Healthcare', 'b':0.70},
    {'t':'ABBV', 'n':'AbbVie Inc',       's':'Healthcare', 'b':0.75},
]
UNI_MAP  = {u['t']: u for u in UNIVERSE}
UNI_TICK = [u['t'] for u in UNIVERSE]

SECTOR_ETFS = {
    'XLK':'Technology','XLF':'Financials','XLV':'Healthcare',
    'XLE':'Energy','XLY':'Cons. Disc.','XLI':'Industrials',
    'XLB':'Materials','XLU':'Utilities','XLRE':'Real Estate','XLP':'Staples',
}

# ── LIVE BETA CACHE ─────────────────────────────────────────
def get_live_beta(ticker):
    """Fetch real beta from Yahoo Finance. Falls back to hardcoded value."""
    fallback = {'AAPL':1.20,'MSFT':0.90,'NVDA':1.80,'GOOGL':1.05,'META':1.30,
                'AMZN':1.15,'TSLA':2.10,'JPM':1.10,'V':0.95,'MA':1.00,
                'JNJ':0.65,'UNH':0.75,'LLY':0.85,'XOM':0.80,'CVX':0.85,
                'HD':1.05,'AVGO':1.40,'PG':0.55,'MRK':0.70,'ABBV':0.75}
    def fetch():
        try:
            info = yf.Ticker(ticker).info
            b = info.get('beta') or info.get('beta3Year')
            return round(float(b), 2) if b else fallback.get(ticker, 1.0)
        except:
            return fallback.get(ticker, 1.0)
    return cached(f'beta_{ticker}', fetch, ttl=86400)  # cache 24h

# ── SIGNAL ENGINE ───────────────────────────────────────────
def calc_rsi(closes, period=14):
    deltas = np.diff(closes[-period-1:])
    gains  = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    ag = gains.mean(); al = losses.mean()
    if al == 0: return 100.0
    return round(100 - 100 / (1 + ag/al), 1)

def compute_signals(hist_df):
    closes = hist_df['Close'].values.astype(float)
    dates  = hist_df.index.strftime('%Y-%m-%d').tolist()
    out = []
    for i in range(20, len(closes)):
        mom = (closes[i] - closes[i-20]) / closes[i-20] * 100
        rsi = calc_rsi(closes[:i+1])
        sig = ('BUY'     if rsi < 30 or mom >  6 else
               'SELL'    if rsi > 70 or mom < -6 else 'NEUTRAL')
        out.append({'date':dates[i], 'price':round(closes[i],2),
                    'rsi':rsi, 'mom':round(float(mom),2), 'sig':sig})
    return out

# ── FLASK ROUTES ────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    from flask import make_response
    resp = make_response(render_template('terminal.html',
                           username=session.get('username',''),
                           is_admin=(session.get('role') == 'admin')))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@app.route('/landing')
def landing():
    """Marketing landing page — public."""
    return render_template('landing.html')

@app.route('/login', methods=['GET','POST'])
def login():
    err = ''
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        user = get_user(username)
        if user and check_password_hash(user['password_hash'], password):
            session.permanent = True
            session['user_id']  = user['id']
            session['username'] = user['username']
            session['role']     = user['role'] or 'user'
            return redirect('/')
        err = 'Napačno uporabniško ime ali geslo.'
    return render_template('login.html', error=err)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/admin/users')
@admin_required
def admin_users():
    """User management — JSON API."""
    return jsonify({'users': get_all_users()})

@app.route('/admin/users/add', methods=['POST'])
@admin_required
def admin_add_user():
    data = request.get_json() or {}
    username = data.get('username','').strip()
    password = data.get('password','')
    email    = data.get('email','').strip()
    if not username or not password:
        return jsonify({'error': 'Manjka username ali password'}), 400
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('INSERT INTO users (username,email,password_hash) VALUES (?,?,?)',
                     (username, email, generate_password_hash(password)))
        conn.commit(); conn.close()
        return jsonify({'ok': True, 'username': username})
    except sqlite3.IntegrityError:
        return jsonify({'error': f'Uporabnik {username} že obstaja'}), 409

@app.route('/admin/users/delete', methods=['POST'])
@admin_required
def admin_delete_user():
    data = request.get_json() or {}
    uid = data.get('id')
    if uid == session.get('user_id'):
        return jsonify({'error': 'Ne moreš izbrisati samega sebe'}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.execute('UPDATE users SET is_active=0 WHERE id=?', (uid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/admin/change-password', methods=['POST'])
@login_required
def admin_change_password():
    data = request.get_json() or {}
    old_pw  = data.get('old_password','')
    new_pw  = data.get('new_password','')
    if len(new_pw) < 6:
        return jsonify({'error': 'Geslo mora biti vsaj 6 znakov'}), 400
    user = get_user(session['username'])
    if not user or not check_password_hash(user['password_hash'], old_pw):
        return jsonify({'error': 'Staro geslo je napačno'}), 403
    conn = sqlite3.connect(DB_PATH)
    conn.execute('UPDATE users SET password_hash=? WHERE id=?',
                 (generate_password_hash(new_pw), session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'ok': True})



@app.route('/api/header-prices')
@login_required
def api_header_prices():
    """Lightweight endpoint: just price + % change for header tickers."""
    tickers = {
        'sp500': '^GSPC',
        'nasdaq': 'QQQ',
        'vix': '^VIX',
        'spy': 'SPY',
    }
    result = {}
    for key, sym in tickers.items():
        try:
            h = yf.Ticker(sym).history(period='5d', auto_adjust=True)
            if h.empty:
                result[key] = None
                continue
            last = float(h['Close'].iloc[-1])
            prev = float(h['Close'].iloc[-2]) if len(h) > 1 else last
            chg  = (last - prev) / prev * 100
            result[key] = {'price': round(last, 2), 'chg': round(chg, 2)}
        except Exception:
            result[key] = None
    return jsonify(result)

@app.route('/api/movers')
@login_required
def api_movers():
    """Real top movers for the whole universe (batch download 2d)."""
    def fetch():
        try:
            raw = yf.download(UNI_TICK, period='5d', progress=False,
                              auto_adjust=True, threads=True)
            closes  = raw['Close']
            volumes = raw['Volume']
            results = []
            for t in UNI_TICK:
                col = t if t in closes.columns else None
                if col is None: continue
                c = closes[col].dropna()
                v = volumes[col].dropna()
                if len(c) < 2: continue
                last, prev = float(c.iloc[-1]), float(c.iloc[-2])
                chg  = (last - prev) / prev * 100
                vol  = int(v.iloc[-1])
                avg_v= float(v.iloc[-min(20,len(v)):].mean())
                vr   = vol / avg_v if avg_v > 0 else 1.0
                info = UNI_MAP.get(t, {})
                results.append({'t':t,'n':info.get('n',t),'s':info.get('s',''),
                                 'b':get_live_beta(t),'price':round(last,2),
                                 'chg':round(chg,2),'volume':vol,
                                 'avgVolume':int(avg_v),'volRatio':round(vr,2)})
            return results
        except Exception as e:
            print(f"[movers] {e}"); traceback.print_exc(); return []

    return jsonify({'stocks': cached('movers', fetch, ttl=120)})


@app.route('/api/stock/<ticker>')
@login_required
def api_stock(ticker):
    """Historical OHLCV + computed signals for a ticker."""
    ticker = ticker.upper()
    period = request.args.get('period', '6mo')
    key    = f'stock_{ticker}_{period}'

    def fetch():
        try:
            hist = yf.Ticker(ticker).history(period=period, auto_adjust=True)
            if hist.empty: return None
            history = [{'date': d.strftime('%Y-%m-%d'),
                        'open':  round(float(r['Open']),  2),
                        'high':  round(float(r['High']),  2),
                        'low':   round(float(r['Low']),   2),
                        'close': round(float(r['Close']), 2),
                        'volume':int(r['Volume'])}
                       for d, r in hist.iterrows()]
            signals  = compute_signals(hist)
            last     = float(hist['Close'].iloc[-1])
            prev     = float(hist['Close'].iloc[-2]) if len(hist) > 1 else last
            chg      = (last - prev) / prev * 100
            avg_v    = float(hist['Volume'].iloc[-20:].mean())
            today_v  = float(hist['Volume'].iloc[-1])
            vr       = today_v / avg_v if avg_v > 0 else 1.0
            info     = UNI_MAP.get(ticker, {'n':ticker,'s':'Unknown','b':1.0})
            live_beta = get_live_beta(ticker)
            return {'ticker':ticker, 'name':info['n'], 'sector':info['s'],
                    'beta':live_beta, 'price':round(last,2), 'chg':round(chg,2),
                    'volume':int(today_v), 'avgVolume':int(avg_v),
                    'volRatio':round(vr,2), 'history':history, 'signals':signals}
        except Exception as e:
            print(f"[stock {ticker}] {e}"); traceback.print_exc(); return None

    data = cached(key, fetch, ttl=300)
    if data is None:
        return jsonify({'error': f'Cannot fetch {ticker}'}), 404
    return jsonify(data)


@app.route('/api/beta/<ticker>')
@login_required
def api_beta(ticker):
    """Live beta from Yahoo Finance."""
    ticker = ticker.upper()
    beta = get_live_beta(ticker)
    return jsonify({'ticker': ticker, 'beta': beta})


@app.route('/api/sectors')
@login_required
def api_sectors():
    """Real 1-day returns for sector ETFs."""
    def fetch():
        try:
            ticks = list(SECTOR_ETFS.keys())
            raw   = yf.download(ticks, period='5d', progress=False, auto_adjust=True)
            closes= raw['Close']
            out   = []
            for etf, name in SECTOR_ETFS.items():
                col = etf if etf in closes.columns else None
                if col is None: continue
                c = closes[col].dropna()
                if len(c) < 2: continue
                chg = (float(c.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100
                out.append({'etf':etf, 'name':name, 'ret':round(chg,2)})
            return out
        except Exception as e:
            print(f"[sectors] {e}"); return []

    return jsonify({'sectors': cached('sectors', fetch, ttl=120)})


@app.route('/api/backtest')
@login_required
def api_backtest():
    """Real backtest using Yahoo Finance historical data."""
    strategy = request.args.get('strategy', 'mom')
    period   = request.args.get('period',   '1y')
    holding  = int(request.args.get('holding', '5'))
    universe = request.args.get('universe', 'sp')

    tickers = ({'tech': ['AAPL','MSFT','NVDA','GOOGL','META','AVGO'],
                'etf':  list(SECTOR_ETFS.keys()),
                'sp':   UNI_TICK}.get(universe, UNI_TICK))

    key = f'bt_{strategy}_{period}_{holding}_{universe}'

    def run():
        try:
            all_t = ['SPY'] + tickers
            raw   = yf.download(all_t, period=period, progress=False,
                                auto_adjust=True, threads=True)
            closes = raw['Close'].dropna(how='all')
            if 'SPY' not in closes.columns: return None

            spy_c = closes['SPY'].dropna().values.astype(float)
            dates = closes['SPY'].dropna().index

            equity, bm  = [1.0], [1.0]
            trades = []
            wins   = 0
            N      = len(spy_c)

            for i in range(1, N):
                bm_ret = (spy_c[i] - spy_c[i-1]) / spy_c[i-1]
                bm.append(bm[-1] * (1 + bm_ret))

                if i % holding == 0 and i >= 21:
                    scored = []
                    for t in tickers:
                        if t not in closes.columns: continue
                        tc = closes[t].dropna().values.astype(float)
                        if len(tc) < i + 1: continue
                        sl = tc[:i]

                        if strategy == 'mom':
                            score = (sl[-1] - sl[-20]) / sl[-20] if len(sl) >= 20 else 0
                        elif strategy == 'rsi':
                            rsi = calc_rsi(sl)
                            score = (30 - rsi) / 30  # oversold = high score
                        else:  # pairs / relative value
                            spy_sl = spy_c[:i]
                            if len(spy_sl) < 20: score = 0
                            else:
                                score = (sl[-1]/sl[-20] if len(sl)>=20 else 1) - (spy_sl[-1]/spy_sl[-20])
                        scored.append((t, float(score)))

                    scored.sort(key=lambda x: x[1], reverse=True)
                    top = scored[:3]
                    if not top:
                        equity.append(equity[-1] * (1 + bm_ret * 0.95))
                        continue

                    port_ret = 0.0
                    for t, _ in top:
                        tc = closes[t].dropna().values.astype(float)
                        if len(tc) < i + 1: continue
                        start_i = max(0, i - holding)
                        ret     = (tc[i] - tc[start_i]) / tc[start_i]
                        port_ret += ret
                        ep  = round(tc[start_i], 2)
                        ex  = round(tc[i], 2)
                        pnl = ret * 100
                        if pnl > 0: wins += 1
                        trades.append({'n':len(trades)+1,'t':t,
                            'ed':dates[start_i].strftime('%Y-%m-%d'),
                            'xd':dates[i].strftime('%Y-%m-%d'),
                            'dir':'LONG','ep':ep,'ex':ex,
                            'pnl':round(pnl,2),
                            'why':{'mom':'Momentum breakout','rsi':'RSI oversold',
                                   'pairs':'Relative value'}[strategy]})

                    port_ret = port_ret / len(top) - 0.0005  # costs
                    equity.append(equity[-1] * (1 + port_ret))
                else:
                    # Hold existing
                    equity.append(equity[-1] * (1 + bm_ret * 0.95))

            # ── Stats ──
            ret_total = (equity[-1] - 1) * 100
            rets = np.array([(equity[i]-equity[i-1])/equity[i-1] for i in range(1,len(equity))])
            sharpe = (rets.mean()*252) / (rets.std()*np.sqrt(252)) if rets.std() > 0 else 0

            peak, mdd = 1.0, 0.0
            dd_series = []
            for v in equity:
                peak = max(peak, v)
                dd   = (v - peak) / peak * 100
                mdd  = min(mdd, dd)
                dd_series.append(round(dd, 2))

            trade_n  = len(trades)
            win_rate = wins / trade_n * 100 if trade_n > 0 else 0
            calmar   = ret_total / abs(mdd) if mdd != 0 else 0

            return {
                'equity':    [round((v-1)*100, 2) for v in equity],
                'benchmark': [round((v-1)*100, 2) for v in bm],
                'drawdown':  dd_series,
                'labels':    [d.strftime('%Y-%m-%d') for d in dates],
                'trades':    trades[-50:],
                'stats': {
                    'return':  round(ret_total, 2),
                    'sharpe':  round(float(sharpe), 2),
                    'maxDD':   round(float(mdd), 2),
                    'winRate': round(win_rate, 1),
                    'trades':  trade_n,
                    'calmar':  round(float(calmar), 2),
                }
            }
        except Exception as e:
            print(f"[backtest] {e}"); traceback.print_exc(); return None

    data = cached(key, run, ttl=600)
    if data is None:
        return jsonify({'error': 'Backtest failed — check logs'}), 500
    return jsonify(data)


# ── NEWS & EARNINGS ─────────────────────────────────────────

# Keyword sentiment classifier
_POS = {'beat','beats','surge','surges','soar','soars','growth','bullish','upgrade',
        'strong','record','profit','rise','gain','rally','buy','outperform','boost',
        'exceed','exceeds','above','positive','win','wins'}
_NEG = {'miss','misses','decline','declines','cut','bearish','downgrade','weak',
        'loss','warning','fall','falls','drop','drops','sell','underperform',
        'below','concern','concerns','disappoints','disappoint','slump','slumps'}

def _sentiment(title):
    words = set(title.lower().split())
    pos = len(words & _POS); neg = len(words & _NEG)
    return 'BULLISH' if pos > neg else 'BEARISH' if neg > pos else 'NEUTRAL'

def _rel_time(ts):
    diff = int(time.time()) - int(ts)
    if diff < 3600:   return f"{diff//60}m ago"
    if diff < 86400:  return f"{diff//3600}h ago"
    return f"{diff//86400}d ago"


@app.route('/api/news/<ticker>')
@login_required
def api_news(ticker):
    ticker = ticker.upper()
    def fetch():
        try:
            raw = yf.Ticker(ticker).news or []
            out = []
            for n in raw[:20]:
                # Support both old and new yfinance news structure
                content = n.get('content', {})
                if content:
                    # New yfinance format (0.2.50+)
                    title     = content.get('title', '')
                    publisher = (content.get('provider') or {}).get('displayName', '')
                    link      = (content.get('canonicalUrl') or {}).get('url', '') or (content.get('clickThroughUrl') or {}).get('url', '')
                    pub_raw   = content.get('pubDate', '')
                    try:
                        import dateutil.parser as dp
                        ts = int(dp.parse(pub_raw).timestamp()) if pub_raw else int(time.time())
                    except Exception:
                        ts = int(time.time())
                    tickers = [t.get('symbol','') for t in (content.get('finance',{}).get('stockTickers') or []) if t.get('symbol')][:4]
                else:
                    # Old yfinance format
                    title     = n.get('title', '')
                    publisher = n.get('publisher', '')
                    link      = n.get('link', '')
                    ts        = n.get('providerPublishTime', int(time.time()))
                    tickers   = n.get('relatedTickers', [])[:4]
                if not title:
                    continue
                out.append({
                    'title':     title,
                    'publisher': publisher,
                    'link':      link,
                    'time':      ts,
                    'relTime':   _rel_time(ts),
                    'sentiment': _sentiment(title),
                    'tickers':   tickers,
                })
            return out
        except Exception as e:
            print(f"[news {ticker}] {e}"); return []
    return jsonify({'news': cached(f'news_{ticker}', fetch, ttl=600)})


@app.route('/api/earnings/<ticker>')
@login_required
def api_earnings(ticker):
    ticker = ticker.upper()
    def fetch():
        try:
            t   = yf.Ticker(ticker)
            cal = {}
            try: cal = t.calendar or {}
            except: pass

            # Next earnings date + estimates
            next_date, eps_est, rev_est, eps_low, eps_high = None, None, None, None, None
            if cal:
                try:
                    dates = cal.get('Earnings Date', [])
                    if isinstance(dates, list) and dates:
                        next_date = str(dates[0])[:10]
                    eps_est  = cal.get('Earnings Average') or cal.get('EPS Estimate')
                    eps_low  = cal.get('Earnings Low')
                    eps_high = cal.get('Earnings High')
                    rev_est  = cal.get('Revenue Average') or cal.get('Revenue Estimate')
                except: pass

            # EPS history (last 8 quarters)
            history = []
            try:
                eh = t.earnings_history
                if eh is not None and not eh.empty:
                    for date, row in eh.iterrows():
                        actual   = row.get('epsActual')
                        estimate = row.get('epsEstimate')
                        surprise = row.get('surprisePercent')
                        history.append({
                            'date':        str(date)[:10],
                            'epsActual':   round(float(actual),   2) if actual   is not None else None,
                            'epsEstimate': round(float(estimate), 2) if estimate is not None else None,
                            'surprise':    round(float(surprise), 2) if surprise is not None else None,
                        })
            except Exception as e:
                print(f"[earnings hist {ticker}] {e}")

            return {
                'nextDate':        next_date,
                'epsEstimate':     float(eps_est)  if eps_est  else None,
                'epsLow':          float(eps_low)  if eps_low  else None,
                'epsHigh':         float(eps_high) if eps_high else None,
                'revenueEstimate': int(rev_est)    if rev_est  else None,
                'history':         history[-8:],
            }
        except Exception as e:
            print(f"[earnings {ticker}] {e}"); return {'nextDate':None,'history':[]}
    return jsonify(cached(f'earn_{ticker}', fetch, ttl=3600))


@app.route('/api/correlations')
@login_required
def api_correlations():
    """Real 90-day rolling correlation matrix from actual price history."""
    tickers_raw = request.args.get('tickers', '')
    if not tickers_raw:
        return jsonify({'error': 'No tickers provided'}), 400

    tickers = [t.strip().upper() for t in tickers_raw.split(',') if t.strip()][:8]
    key = 'corr_' + '_'.join(sorted(tickers))

    def fetch():
        try:
            raw  = yf.download(tickers, period='90d', progress=False, auto_adjust=True, threads=True)
            if len(tickers) == 1:
                closes = raw[['Close']].copy()
                closes.columns = tickers
            else:
                closes = raw['Close']

            closes  = closes.dropna(how='all')
            returns = closes.pct_change().dropna()

            matrix = {}
            for t1 in tickers:
                matrix[t1] = {}
                for t2 in tickers:
                    if t1 not in returns.columns or t2 not in returns.columns:
                        matrix[t1][t2] = None
                    elif t1 == t2:
                        matrix[t1][t2] = 1.0
                    else:
                        c = returns[t1].corr(returns[t2])
                        matrix[t1][t2] = round(float(c), 3) if not np.isnan(c) else None

            return {'tickers': tickers, 'matrix': matrix, 'days': len(returns)}
        except Exception as e:
            print(f"[correlations] {e}")
            traceback.print_exc()
            return None

    data = cached(key, fetch, ttl=1800)
    if data is None:
        return jsonify({'error': 'Could not compute correlations'}), 500
    return jsonify(data)


@app.route('/api/earnings-calendar')
@login_required
def api_earnings_calendar():
    """Upcoming earnings dates for the full universe."""
    def fetch():
        out = []
        for u in UNIVERSE:
            try:
                cal = yf.Ticker(u['t']).calendar or {}
                dates = cal.get('Earnings Date', [])
                if isinstance(dates, list) and dates:
                    nd      = str(dates[0])[:10]
                    eps_est = cal.get('Earnings Average')
                    out.append({'t': u['t'], 'n': u['n'], 's': u['s'],
                                'nextEarnings': nd,
                                'epsEstimate': round(float(eps_est),2) if eps_est else None})
                time.sleep(0.15)
            except:
                pass
        out.sort(key=lambda x: x['nextEarnings'] or '9999')
        return out
    return jsonify({'calendar': cached('earn_cal', fetch, ttl=3600)})


# ── ANOMALY SCANNER ─────────────────────────────────────────

SP150 = [
    # Technology (25)
    'AAPL','MSFT','NVDA','AMD','INTC','QCOM','AVGO','TXN','MU','AMAT',
    'LRCX','ADBE','CRM','NOW','ORCL','CSCO','PANW','FTNT','SNPS','CDNS',
    'IBM','DELL','MRVL','NET','ZS',
    # Financials (15)
    'JPM','BAC','WFC','GS','MS','C','BLK','AXP','V','MA',
    'COF','USB','PNC','TFC','SCHW',
    # Healthcare (15)
    'JNJ','UNH','PFE','ABBV','MRK','LLY','BMY','AMGN','GILD','REGN',
    'VRTX','ISRG','SYK','MDT','ABT',
    # Energy (10)
    'XOM','CVX','COP','EOG','SLB','PSX','VLO','MPC','OXY','HAL',
    # Consumer Discretionary (15)
    'AMZN','TSLA','HD','MCD','NKE','SBUX','TGT','LOW','BKNG','MAR',
    'HLT','ROST','TJX','DG','EBAY',
    # Consumer Staples (10)
    'WMT','PG','KO','PEP','COST','PM','MO','CL','KMB','GIS',
    # Industrials (15)
    'CAT','HON','UPS','FDX','RTX','LMT','NOC','GD','BA','DE',
    'MMM','EMR','ETN','PH','ROK',
    # Materials (8)
    'LIN','APD','ECL','SHW','NEM','FCX','NUE','CF',
    # Utilities (8)
    'NEE','DUK','SO','D','AEP','EXC','XEL','WEC',
    # Communication Services (10)
    'NFLX','DIS','CMCSA','T','VZ','TMUS','CHTR','EA','SNAP','TTWO',
    # Real Estate (9)
    'AMT','PLD','CCI','EQIX','PSA','SPG','O','WELL','DLR',
    # Alphabet & Meta (separate so both get XLC)
    'GOOGL','META',
]
SP150 = list(dict.fromkeys(SP150))  # deduplicate, preserve order

STOCK_SECTOR = {
    # Technology → XLK
    'AAPL':'Technology','MSFT':'Technology','NVDA':'Technology',
    'AMD':'Technology','INTC':'Technology','QCOM':'Technology',
    'AVGO':'Technology','TXN':'Technology','MU':'Technology',
    'AMAT':'Technology','LRCX':'Technology','ADBE':'Technology',
    'CRM':'Technology','NOW':'Technology','ORCL':'Technology',
    'CSCO':'Technology','PANW':'Technology','FTNT':'Technology',
    'SNPS':'Technology','CDNS':'Technology','IBM':'Technology',
    'DELL':'Technology','MRVL':'Technology','NET':'Technology','ZS':'Technology',
    # Financials → XLF
    'JPM':'Financials','BAC':'Financials','WFC':'Financials',
    'GS':'Financials','MS':'Financials','C':'Financials',
    'BLK':'Financials','AXP':'Financials','V':'Financials','MA':'Financials',
    'COF':'Financials','USB':'Financials','PNC':'Financials',
    'TFC':'Financials','SCHW':'Financials',
    # Healthcare → XLV
    'JNJ':'Healthcare','UNH':'Healthcare','PFE':'Healthcare',
    'ABBV':'Healthcare','MRK':'Healthcare','LLY':'Healthcare',
    'BMY':'Healthcare','AMGN':'Healthcare','GILD':'Healthcare',
    'REGN':'Healthcare','VRTX':'Healthcare','ISRG':'Healthcare',
    'SYK':'Healthcare','MDT':'Healthcare','ABT':'Healthcare',
    # Energy → XLE
    'XOM':'Energy','CVX':'Energy','COP':'Energy','EOG':'Energy',
    'SLB':'Energy','PSX':'Energy','VLO':'Energy','MPC':'Energy',
    'OXY':'Energy','HAL':'Energy',
    # Consumer Discretionary → XLY
    'AMZN':'Consumer Disc','TSLA':'Consumer Disc','HD':'Consumer Disc',
    'MCD':'Consumer Disc','NKE':'Consumer Disc','SBUX':'Consumer Disc',
    'TGT':'Consumer Disc','LOW':'Consumer Disc','BKNG':'Consumer Disc',
    'MAR':'Consumer Disc','HLT':'Consumer Disc','ROST':'Consumer Disc',
    'TJX':'Consumer Disc','DG':'Consumer Disc','EBAY':'Consumer Disc',
    # Consumer Staples → XLP
    'WMT':'Consumer Staples','PG':'Consumer Staples','KO':'Consumer Staples',
    'PEP':'Consumer Staples','COST':'Consumer Staples','PM':'Consumer Staples',
    'MO':'Consumer Staples','CL':'Consumer Staples',
    'KMB':'Consumer Staples','GIS':'Consumer Staples',
    # Industrials → XLI
    'CAT':'Industrials','HON':'Industrials','UPS':'Industrials',
    'FDX':'Industrials','RTX':'Industrials','LMT':'Industrials',
    'NOC':'Industrials','GD':'Industrials','BA':'Industrials',
    'DE':'Industrials','MMM':'Industrials','EMR':'Industrials',
    'ETN':'Industrials','PH':'Industrials','ROK':'Industrials',
    # Materials → XLB
    'LIN':'Materials','APD':'Materials','ECL':'Materials','SHW':'Materials',
    'NEM':'Materials','FCX':'Materials','NUE':'Materials','CF':'Materials',
    # Utilities → XLU
    'NEE':'Utilities','DUK':'Utilities','SO':'Utilities','D':'Utilities',
    'AEP':'Utilities','EXC':'Utilities','XEL':'Utilities','WEC':'Utilities',
    # Communication Services → XLC
    'NFLX':'Comm Services','DIS':'Comm Services','CMCSA':'Comm Services',
    'T':'Comm Services','VZ':'Comm Services','TMUS':'Comm Services',
    'CHTR':'Comm Services','EA':'Comm Services','SNAP':'Comm Services',
    'TTWO':'Comm Services','GOOGL':'Comm Services','META':'Comm Services',
    # Real Estate → XLRE
    'AMT':'Real Estate','PLD':'Real Estate','CCI':'Real Estate',
    'EQIX':'Real Estate','PSA':'Real Estate','SPG':'Real Estate',
    'O':'Real Estate','WELL':'Real Estate','DLR':'Real Estate',
}

SECTOR_ETF = {
    'Technology':'XLK', 'Financials':'XLF', 'Healthcare':'XLV',
    'Energy':'XLE', 'Consumer Disc':'XLY', 'Consumer Staples':'XLP',
    'Industrials':'XLI', 'Materials':'XLB', 'Utilities':'XLU',
    'Comm Services':'XLC', 'Real Estate':'XLRE',
}


def _get_earn_date(ticker):
    """Return (ticker, pd.Timestamp|None) — safe, no exceptions."""
    try:
        cal = yf.Ticker(ticker).calendar
        if cal is None:
            return ticker, None
        if isinstance(cal, dict):
            dates = cal.get('Earnings Date', [])
            if isinstance(dates, list) and dates:
                return ticker, pd.Timestamp(str(dates[0])[:10])
            if dates:
                return ticker, pd.Timestamp(str(dates)[:10])
        elif hasattr(cal, 'index') and 'Earnings Date' in cal.index:
            raw = cal.loc['Earnings Date']
            val = raw.iloc[0] if hasattr(raw, 'iloc') else raw
            return ticker, pd.Timestamp(str(val)[:10])
    except Exception:
        pass
    return ticker, None


@app.route('/api/anomaly-scan')
@login_required
def api_anomaly_scan():
    """Pre-earnings correlation anomaly scanner — ~150 S&P 500 stocks."""
    def fetch():
        t0 = time.time()
        try:
            # ── 1. Batch download 95 days of Close + Volume ──────────
            etfs = list(SECTOR_ETF.values())
            all_tk = list(dict.fromkeys(SP150 + etfs))
            raw = yf.download(all_tk, period='95d', auto_adjust=True,
                              progress=False, threads=True)
            if raw.empty:
                return {'error': 'yfinance download returned empty', 'results': []}

            # Normalise multi-level columns
            if isinstance(raw.columns, pd.MultiIndex):
                closes  = raw['Close']
                volumes = raw['Volume']
            else:
                closes  = raw[['Close']]
                volumes = raw[['Volume']]

            # ── 2. Fetch earnings dates concurrently ─────────────────
            earn_map = {}
            with ThreadPoolExecutor(max_workers=20) as ex:
                futs = {ex.submit(_get_earn_date, t): t for t in SP150}
                for f in as_completed(futs, timeout=90):
                    try:
                        tk, dt = f.result(timeout=5)
                        if dt is not None:
                            earn_map[tk] = dt
                    except Exception:
                        pass

            # ── 3. Score each ticker with upcoming earnings ───────────
            today = pd.Timestamp(datetime.date.today())
            results, candidates = [], 0

            for ticker in SP150:
                if ticker not in earn_map:
                    continue
                edate = earn_map[ticker]
                # strip timezone if present
                if hasattr(edate, 'tzinfo') and edate.tzinfo is not None:
                    edate = edate.tz_localize(None)
                days_to = (edate - today).days
                if not (0 <= days_to <= 14):
                    continue
                candidates += 1

                sector = STOCK_SECTOR.get(ticker, 'Technology')
                etf    = SECTOR_ETF.get(sector, 'XLK')

                try:
                    sc = closes[ticker].dropna()
                    ec = closes[etf].dropna()
                    sv = volumes[ticker].dropna()
                except (KeyError, TypeError):
                    continue

                if len(sc) < 30 or len(ec) < 30:
                    continue

                common = sc.index.intersection(ec.index)
                if len(common) < 30:
                    continue

                sp_s = sc[common];  ep_s = ec[common]
                sr   = sp_s.pct_change().dropna()
                er   = ep_s.pct_change().dropna()

                # Correlation: 90-day baseline vs 15-day recent
                corr90 = float(sr.corr(er))
                corr15 = float(sr.tail(15).corr(er.tail(15)))
                if pd.isna(corr90) or pd.isna(corr15):
                    continue
                # Positive delta = stock decorrelating from sector (unusual)
                corr_delta = corr90 - corr15

                # Volume: recent 10-day avg vs 90-day avg
                sv_common = sv[sv.index.isin(common)]
                vol90 = float(sv_common.mean()) if len(sv_common) else 1.0
                vol10 = float(sv_common.tail(10).mean()) if len(sv_common) >= 10 else vol90
                vol_ratio = vol10 / vol90 if vol90 > 0 else 1.0

                # Momentum divergence: stock 20d return vs ETF 20d return
                if len(sp_s) >= 20:
                    mom_s   = (float(sp_s.iloc[-1]) / float(sp_s.iloc[-20]) - 1) * 100
                    mom_e   = (float(ep_s.iloc[-1]) / float(ep_s.iloc[-20]) - 1) * 100
                    mom_div = mom_s - mom_e
                else:
                    mom_s = mom_e = mom_div = 0.0

                # Day change
                cur  = float(sp_s.iloc[-1])
                prev = float(sp_s.iloc[-2]) if len(sp_s) > 1 else cur
                day_chg = (cur / prev - 1) * 100

                # ── Anomaly score (0-100) ────────────────────────────
                # Correlation component (0-40): peaks at corr_delta ≥ 0.3
                c_score = min(40.0, max(0.0, corr_delta * 133.0))
                # Volume component (0-30): peaks at vol_ratio ≥ 1.5x
                v_score = min(30.0, max(0.0, (vol_ratio - 1.0) * 60.0))
                # Momentum divergence (0-30): peaks at |div| ≥ 10%
                m_score = min(30.0, max(0.0, abs(mom_div) * 3.0))
                total   = c_score + v_score + m_score

                if   total >= 65: signal = 'CRITICAL'
                elif total >= 45: signal = 'HIGH'
                elif total >= 25: signal = 'MEDIUM'
                else:             signal = 'LOW'

                results.append({
                    'ticker':        ticker,
                    'sector':        sector,
                    'price':         round(cur, 2),
                    'day_chg':       round(day_chg, 2),
                    'days_to_earn':  int(days_to),
                    'earn_date':     edate.strftime('%Y-%m-%d'),
                    'corr_90d':      round(corr90, 3),
                    'corr_15d':      round(corr15, 3),
                    'corr_delta':    round(corr_delta, 3),
                    'vol_ratio':     round(vol_ratio, 2),
                    'mom_stock':     round(mom_s, 2),
                    'mom_etf':       round(mom_e, 2),
                    'mom_div':       round(mom_div, 2),
                    'corr_score':    round(c_score, 1),
                    'vol_score':     round(v_score, 1),
                    'mom_score':     round(m_score, 1),
                    'anomaly_score': round(total, 1),
                    'signal':        signal,
                })

            results.sort(key=lambda x: x['anomaly_score'], reverse=True)
            elapsed = round(time.time() - t0, 1)

            return {
                'scanned':    len(SP150),
                'with_earn':  len(earn_map),
                'candidates': candidates,
                'elapsed':    elapsed,
                'results':    results,
                'ts':         datetime.datetime.now().strftime('%H:%M:%S'),
            }

        except Exception as e:
            traceback.print_exc()
            return {'error': str(e), 'results': []}

    data = cached('anomaly_scan', fetch, ttl=600)
    return jsonify(data)



@app.route('/api/alerts/config', methods=['GET'])
@login_required
def api_alerts_config_get():
    cfg = get_alert_config()
    cfg.pop('smtp_pass', None)  # never send password to browser
    return jsonify(cfg)

@app.route('/api/alerts/config', methods=['POST'])
@admin_required
def api_alerts_config_post():
    data = request.get_json() or {}
    existing = get_alert_config()
    if 'smtp_pass' not in data or not data.get('smtp_pass'):
        data['smtp_pass'] = existing.get('smtp_pass','')
    save_alert_config(data)
    return jsonify({'ok': True})

@app.route('/api/alerts/test', methods=['POST'])
@admin_required
def api_alerts_test():
    cfg = get_alert_config()
    data = request.get_json() or {}
    if data.get('smtp_pass'):
        cfg['smtp_pass'] = data['smtp_pass']
    cfg.update({k:v for k,v in data.items() if k != 'smtp_pass' or data.get('smtp_pass')})
    test_results = [{'ticker':'TEST','sector':'Technology','price':150.0,'days_to_earn':5,
        'corr_delta':0.45,'vol_ratio':1.6,'mom_div':8.2,'anomaly_score':72.5,'signal':'CRITICAL'}]
    ok, msg = send_alert_email(cfg, test_results)
    return jsonify({'ok': ok, 'msg': msg})

@app.route('/api/alerts/run-now', methods=['POST'])
@admin_required
def api_alerts_run_now():
    scan = _cache.get('anomaly_scan')
    if not scan or not scan.get('results'):
        return jsonify({'ok': False, 'msg': 'Najprej pozeni Anomaly Scan'})
    cfg = get_alert_config()
    ok, msg = send_alert_email(cfg, scan['results'])
    return jsonify({'ok': ok, 'msg': msg})

# ── WATCHLIST ────────────────────────────────────────────────
@app.route('/api/watchlist', methods=['GET'])
@login_required
def api_watchlist_get():
    uid = session['user_id']
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT ticker, added_at FROM watchlist WHERE user_id=? ORDER BY added_at DESC', (uid,)).fetchall()
    conn.close()
    return jsonify({'tickers': [dict(r) for r in rows]})

@app.route('/api/watchlist/add', methods=['POST'])
@login_required
def api_watchlist_add():
    uid = session['user_id']
    data = request.get_json() or {}
    ticker = data.get('ticker','').strip().upper()
    if not ticker or len(ticker) > 10:
        return jsonify({'error': 'Invalid ticker'}), 400
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('INSERT OR IGNORE INTO watchlist (user_id, ticker) VALUES (?,?)', (uid, ticker))
        conn.commit(); conn.close()
        return jsonify({'ok': True, 'ticker': ticker})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/watchlist/remove', methods=['POST'])
@login_required
def api_watchlist_remove():
    uid = session['user_id']
    data = request.get_json() or {}
    ticker = data.get('ticker','').strip().upper()
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DELETE FROM watchlist WHERE user_id=? AND ticker=?', (uid, ticker))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/watchlist/scan', methods=['GET'])
@login_required
def api_watchlist_scan():
    """Scan all watchlist tickers — price, signal, RSI, days to earnings."""
    uid = session['user_id']
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('SELECT ticker FROM watchlist WHERE user_id=? ORDER BY added_at DESC', (uid,)).fetchall()
    conn.close()
    tickers = [r[0] for r in rows]
    if not tickers:
        return jsonify({'results': []})

    def scan_one(ticker):
        result = {'ticker': ticker, 'name': ticker, 'sector': '—',
                  'price': 0, 'chg': 0, 'rsi': 50, 'signal': 'NEUTRAL',
                  'vol_ratio': 1.0, 'days_to_earn': None}
        try:
            t_obj = yf.Ticker(ticker)
            hist = t_obj.history(period='3mo', auto_adjust=True)
            if hist.empty: return None
            last  = float(hist['Close'].iloc[-1])
            prev  = float(hist['Close'].iloc[-2]) if len(hist)>1 else last
            chg   = (last-prev)/prev*100
            avg_v = float(hist['Volume'].iloc[-20:].mean())
            today_v = float(hist['Volume'].iloc[-1])
            vol_ratio = today_v/avg_v if avg_v>0 else 1.0
            result['price'] = round(last,2)
            result['chg']   = round(chg,2)
            result['vol_ratio'] = round(vol_ratio,2)
        except Exception:
            return None
        try:
            sigs = compute_signals(hist)
            result['rsi']    = round(sigs[-1]['rsi'],1) if sigs else 50
            result['signal'] = sigs[-1]['sig'] if sigs else 'NEUTRAL'
        except Exception:
            pass
        try:
            info = UNI_MAP.get(ticker, {'n': ticker, 's': 'Unknown', 'b': 1.0})
            result['name']   = info.get('n', ticker)
            result['sector'] = info.get('s', '—')
        except Exception:
            pass
        try:
            _, earn_date = _get_earn_date(ticker)
            if earn_date:
                delta = (earn_date.date() - datetime.date.today()).days
                if -5 <= delta <= 90:
                    result['days_to_earn'] = delta
        except Exception:
            pass
        return result

    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(scan_one, t): t for t in tickers}
        for f in as_completed(futures):
            r = f.result()
            if r: results.append(r)

    results.sort(key=lambda x: tickers.index(x['ticker']) if x['ticker'] in tickers else 999)
    return jsonify({'results': results})



# ── PORTFOLIO ─────────────────────────────────────────────────
@app.route('/api/portfolio', methods=['GET'])
@login_required
def api_portfolio_get():
    """Get all positions for current user with live prices."""
    user_id = session['user_id']
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        'SELECT id, ticker, direction, qty, entry_price, stop_pct, sector FROM portfolio WHERE user_id=? ORDER BY added_at',
        (user_id,)
    ).fetchall()
    conn.close()

    positions = []
    for row in rows:
        pid, ticker, direction, qty, entry_price, stop_pct, sector = row
        try:
            hist = yf.Ticker(ticker).history(period='2d', auto_adjust=True)
            if len(hist) >= 1:
                cur = float(hist['Close'].iloc[-1])
            else:
                cur = entry_price
        except Exception:
            cur = entry_price
        pnl = (cur - entry_price) * qty if direction == 'LONG' else (entry_price - cur) * qty
        pnl_pct = (cur - entry_price) / entry_price * 100 if direction == 'LONG' else (entry_price - cur) / entry_price * 100
        stop_price = cur * (1 - stop_pct / 100) if direction == 'LONG' else cur * (1 + stop_pct / 100)
        max_loss = qty * abs(cur - stop_price)
        positions.append({
            'id': pid, 'ticker': ticker, 'direction': direction,
            'qty': qty, 'entry': round(entry_price, 2), 'cur': round(cur, 2),
            'sector': sector, 'stop_pct': stop_pct,
            'stop_price': round(stop_price, 2),
            'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 2),
            'max_loss': round(max_loss, 2),
            'exposure': round(qty * cur, 2)
        })
    return jsonify({'positions': positions})

@app.route('/api/portfolio/add', methods=['POST'])
@login_required
def api_portfolio_add():
    user_id = session['user_id']
    d = request.get_json() or {}
    ticker = d.get('ticker', '').upper().strip()
    if not ticker:
        return jsonify({'error': 'Ticker required'}), 400
    # Auto-detect sector from UNI_MAP
    sector = UNI_MAP.get(ticker, {}).get('s', d.get('sector', 'Unknown'))
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'INSERT INTO portfolio (user_id, ticker, direction, qty, entry_price, stop_pct, sector) VALUES (?,?,?,?,?,?,?)',
        (user_id, ticker, d.get('direction', 'LONG'), float(d.get('qty', 100)),
         float(d.get('entry_price', 0)), float(d.get('stop_pct', 5)), sector)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/portfolio/remove', methods=['POST'])
@login_required
def api_portfolio_remove():
    user_id = session['user_id']
    pid = (request.get_json() or {}).get('id')
    if not pid:
        return jsonify({'error': 'ID required'}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DELETE FROM portfolio WHERE id=? AND user_id=?', (pid, user_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── ANOMALY ACCURACY BACKTEST ─────────────────────────────────
@app.route('/api/anomaly-accuracy')
@login_required
def api_anomaly_accuracy():
    """Backtest anomaly signals on 6 months of historical data."""
    cache_key = 'anomaly_accuracy'
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached['ts']) < 3600:
        return jsonify(cached['data'])

    # Use 30 liquid large-caps for speed
    TEST_STOCKS = [
        'AAPL','MSFT','NVDA','AMZN','META','GOOGL','TSLA','JPM','V','UNH',
        'XOM','JNJ','WMT','PG','MA','HD','AVGO','LLY','CVX','MRK',
        'ABBV','BAC','KO','PEP','TMO','COST','ADBE','CRM','AMD','NFLX'
    ]

    SECTOR_ETF_MAP = {
        'AAPL':'XLK','MSFT':'XLK','NVDA':'XLK','AVGO':'XLK','ADBE':'XLK','CRM':'XLK','AMD':'XLK',
        'AMZN':'XLY','TSLA':'XLY','HD':'XLY','COST':'XLY',
        'META':'XLC','GOOGL':'XLC','NFLX':'XLC',
        'JPM':'XLF','BAC':'XLF','V':'XLF','MA':'XLF',
        'UNH':'XLV','JNJ':'XLV','LLY':'XLV','MRK':'XLV','ABBV':'XLV','TMO':'XLV',
        'XOM':'XLE','CVX':'XLE',
        'WMT':'XLP','PG':'XLP','KO':'XLP','PEP':'XLP',
    }

    # Download 9 months so we have 6m of signals + 3m of outcomes
    all_tickers = list(set(TEST_STOCKS + list(set(SECTOR_ETF_MAP.values()))))
    try:
        raw = yf.download(all_tickers, period='9mo', auto_adjust=True, progress=False, threads=True)
        closes = raw['Close'] if 'Close' in raw.columns.get_level_values(0) else raw
    except Exception as e:
        return jsonify({'error': str(e), 'signals': [], 'stats': {}})

    signals_found = []
    today = pd.Timestamp.today().normalize()

    for ticker in TEST_STOCKS:
        etf = SECTOR_ETF_MAP.get(ticker, 'SPY')
        if ticker not in closes.columns or etf not in closes.columns:
            continue
        stock_prices = closes[ticker].dropna()
        etf_prices   = closes[etf].dropna()
        if len(stock_prices) < 60:
            continue

        # Align
        common_idx = stock_prices.index.intersection(etf_prices.index)
        sp = stock_prices.loc[common_idx]
        ep = etf_prices.loc[common_idx]

        # Scan weekly over past 6 months (only past dates, not future)
        scan_start = today - pd.Timedelta(days=180)
        scan_dates = [d for d in sp.index if scan_start <= d <= today - pd.Timedelta(days=25)]

        for i, date in enumerate(scan_dates):
            idx = sp.index.get_loc(date)
            if idx < 90:
                continue
            # Correlation anomaly: 90d vs 15d
            sp_90 = sp.iloc[idx-89:idx+1]
            ep_90 = ep.iloc[idx-89:idx+1]
            sp_15 = sp.iloc[idx-14:idx+1]
            ep_15 = ep.iloc[idx-14:idx+1]
            if len(sp_90) < 30 or len(sp_15) < 10:
                continue
            try:
                corr_90 = float(sp_90.corr(ep_90))
                corr_15 = float(sp_15.corr(ep_15))
                corr_delta = corr_90 - corr_15
            except Exception:
                continue

            # Corr score (0-40)
            corr_score = min(40, max(0, corr_delta * 50)) if corr_delta > 0.1 else 0

            # Volume ratio (0-30) — skip if no volume data
            vol_score = 0
            if 'Volume' in raw.columns.get_level_values(0):
                try:
                    vols = raw['Volume'][ticker].dropna()
                    if idx < len(vols):
                        avg_vol = float(vols.iloc[max(0,idx-59):idx].mean())
                        cur_vol = float(vols.iloc[idx])
                        if avg_vol > 0:
                            vol_ratio = cur_vol / avg_vol
                            vol_score = min(30, max(0, (vol_ratio - 1.0) * 20))
                except Exception:
                    pass

            # Momentum divergence (0-30)
            try:
                mom_stock = (float(sp.iloc[idx]) - float(sp.iloc[idx-19])) / float(sp.iloc[idx-19]) * 100
                mom_etf   = (float(ep.iloc[idx]) - float(ep.iloc[idx-19])) / float(ep.iloc[idx-19]) * 100
                mom_div   = abs(mom_stock - mom_etf)
                mom_score = min(30, mom_div * 2)
            except Exception:
                mom_score = 0

            total_score = corr_score + vol_score + mom_score
            if total_score < 45:
                continue  # Only HIGH and above

            sig_level = 'CRITICAL' if total_score >= 75 else 'HIGH'

            # Forward returns: 5d, 10d, 20d
            future_idx_5  = idx + 5
            future_idx_10 = idx + 10
            future_idx_20 = idx + 20
            sp_arr = sp.values
            cur_price = float(sp_arr[idx])
            ret5  = round((float(sp_arr[future_idx_5])  - cur_price) / cur_price * 100, 2) if future_idx_5  < len(sp_arr) else None
            ret10 = round((float(sp_arr[future_idx_10]) - cur_price) / cur_price * 100, 2) if future_idx_10 < len(sp_arr) else None
            ret20 = round((float(sp_arr[future_idx_20]) - cur_price) / cur_price * 100, 2) if future_idx_20 < len(sp_arr) else None

            signals_found.append({
                'ticker': ticker,
                'date': date.strftime('%Y-%m-%d'),
                'score': round(total_score, 1),
                'signal': sig_level,
                'price': round(cur_price, 2),
                'ret5': ret5, 'ret10': ret10, 'ret20': ret20,
                'sector': SECTOR_ETF_MAP.get(ticker, '—')
            })

    # Stats
    with_ret10 = [s for s in signals_found if s['ret10'] is not None]
    wins = [s for s in with_ret10 if s['ret10'] > 0]
    avg_ret = round(sum(s['ret10'] for s in with_ret10) / len(with_ret10), 2) if with_ret10 else 0
    win_rate = round(len(wins) / len(with_ret10) * 100, 1) if with_ret10 else 0
    critical_sigs = [s for s in signals_found if s['signal'] == 'CRITICAL']
    crit_wins = [s for s in critical_sigs if s.get('ret10') is not None and s['ret10'] > 0]
    crit_wr = round(len(crit_wins)/len([s for s in critical_sigs if s.get('ret10') is not None])*100,1) if critical_sigs else 0

    # Sort by date desc
    signals_found.sort(key=lambda x: x['date'], reverse=True)

    result = {
        'signals': signals_found[:80],
        'stats': {
            'total': len(signals_found),
            'win_rate': win_rate,
            'avg_ret10': avg_ret,
            'critical_count': len(critical_sigs),
            'critical_win_rate': crit_wr,
            'stocks_tested': len(TEST_STOCKS)
        }
    }
    _cache[cache_key] = {'ts': time.time(), 'data': result}
    return jsonify(result)


# ── STARTUP (runs for both gunicorn and direct python) ────────
init_db()
_scheduler_thread = threading.Thread(target=alert_scheduler, daemon=True)
_scheduler_thread.start()

# ── START (direct python only) ────────────────────────────────
if __name__ == '__main__':
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    try:
        print()
        print('=' * 58)
        print('  AI QUANT RESEARCH TERMINAL')
        print('  Backend  : Flask + yfinance (Yahoo Finance)')
        print('  Login    : http://localhost:5001/login')
        print('  Terminal : http://localhost:5001')
        print('=' * 58)
        print()
        port = int(os.environ.get('PORT', 5001))
        host = '127.0.0.1'
        if os.environ.get('PORT'):
            host = '0.0.0.0'
        print('  [ALERT] Scheduler zagnan')
        app.run(debug=False, port=port, host=host, use_reloader=False)
    except Exception as e:
        import sys
        sys.stderr.write('NAPAKA: ' + str(e) + '\n')
        traceback.print_exc()
        input('Pritisni Enter...')
