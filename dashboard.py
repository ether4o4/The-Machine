"""Local dashboard. Install: pip install flask pyyaml; run: python dashboard.py."""
import atexit
import copy
import hashlib
import json
import os
import threading
import time
from datetime import date
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request
from jinja2 import DictLoader

BASE = Path(__file__).resolve().parent
STATE = BASE / 'state'
STATE.mkdir(exist_ok=True, mode=0o700)
CONFIG = BASE / 'config.yaml'
config = yaml.safe_load(CONFIG.read_text()) if CONFIG.exists() else {}
config = config or {}
if not isinstance(config, dict) or not isinstance(config.get('keys', []), list):
    raise ValueError('config.yaml must be a mapping with a keys list')
lock = threading.RLock()
stop_event = threading.Event()
worker = None
app = Flask(__name__)


def load(name, default):
    path = STATE / (name + '.json')
    if not path.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise RuntimeError(f'Cannot read {path}; repair or back it up first') from exc


stats = load('stats', dict(day=str(date.today()), accounts_today=0,
    accounts_total=0, accounts_success=0, accounts_failed=0, plays_total=0,
    plays_by_source={}, captcha_solves=0, proxy_gb=0, spend_est=0))
key_state = load('keys', {})
keys = []
for secret in config.get('keys', []):
    if not isinstance(secret, str):
        raise ValueError('Each key must be a string')
    identity = hashlib.sha256(secret.encode()).hexdigest()
    row = key_state.setdefault(identity, dict(tag=secret[-4:], uses=0,
                                             cooldown_until=0, dead=False))
    keys.append(row)
status = load('status', dict(running=False, phase='idle', uptime_seconds=0))
status.update(running=False, phase='idle')  # A restarted process has no live worker.
started_at = None


def save():
    # Caller holds lock; atomic replacements avoid partially written JSON.
    for name, value in [('stats', stats), ('keys', key_state), ('status', status)]:
        path = STATE / (name + '.json')
        tmp = path.with_suffix('.tmp')
        with open(tmp, 'w') as handle:
            os.chmod(tmp, 0o600)
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)


def roll_day():
    if stats['day'] != str(date.today()):
        stats.update(day=str(date.today()), accounts_today=0)


def record_activity(success=None, source=None, captcha=0, proxy_gb=0):
    """Call from your worker after actual activity; never persist credentials."""
    with lock:
        roll_day()
        if success is not None:
            stats['accounts_success' if success else 'accounts_failed'] += 1
            if success:
                stats['accounts_today'] += 1
                stats['accounts_total'] += 1
        if source is not None:
            stats['plays_total'] += 1
            counts = stats['plays_by_source']
            counts[source] = counts.get(source, 0) + 1
        stats['captcha_solves'] += captcha
        stats['proxy_gb'] += proxy_gb
        rates = config.get('rates', {})
        stats['spend_est'] = (stats['captcha_solves'] * rates.get('captcha_solve', 0)
                              + stats['proxy_gb'] * rates.get('proxy_gb', 0))
        save()


def orchestrator():
    """Integration hook: add bounded, cancellable work using config and stop_event.

    Set status['phase'] under lock to creating/farming/idle; update key rows
    under the same lock. Read config['proxy'], ['target'], ['captcha_key'] here.
    The initial worker waits without generating synthetic activity.
    """
    try:
        while not stop_event.wait(1):
            with lock:
                roll_day()
                status['uptime_seconds'] = int(time.monotonic() - started_at)
                save()
    finally:
        with lock:
            status.update(running=False, phase='idle')
            save()


@app.before_request
def local_writes_only():
    # Reject cross-origin browser writes to the local control endpoints.
    if request.method == 'POST':
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'):
            return jsonify(error='Cross-origin control request rejected'), 403


# GET /: render the embedded index.html Jinja template.
@app.get('/')
def index():
    return render_template('index.html', target=config.get('target') or 'Not configured')


# GET /api/status: current worker state and elapsed runtime.
@app.get('/api/status')
def api_status():
    with lock:
        return jsonify(status)


# GET /api/keys: expose only suffixes and usage metadata.
@app.get('/api/keys')
def api_keys():
    with lock:
        return jsonify(keys)


# GET /api/stats: persistent counters plus detail for each dashboard panel.
@app.get('/api/stats')
def api_stats():
    with lock:
        roll_day()
        save()
        return jsonify(stats)


# POST /api/start: idempotently launch one background orchestrator.
@app.post('/api/start')
def api_start():
    global worker, started_at
    with lock:
        if worker and worker.is_alive():
            return jsonify(status), 409 if stop_event.is_set() else 200
        stop_event.clear()
        started_at = time.monotonic()
        status.update(running=True, phase='idle', uptime_seconds=0)
        save()
        worker = threading.Thread(target=orchestrator, daemon=True)
        worker.start()
        return jsonify(status)


# POST /api/stop: request cooperative shutdown without blocking Flask.
@app.post('/api/stop')
def api_stop():
    with lock:
        stop_event.set()
        return jsonify(status), 202 if worker and worker.is_alive() else 200


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>The Machine · Local control</title><style>
:root{color-scheme:dark;--bg:#0b0e13;--panel:#141923;--line:#293141;--muted:#9ba8ba;--green:#7aefbb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#eef3fa;font:15px system-ui,sans-serif}
main{max-width:1120px;margin:auto;padding:36px 20px}header{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:30px}
h1{font-size:27px;margin:0 0 8px;letter-spacing:-1px}p{color:var(--muted);margin:0}small{color:var(--muted)}
.badge{border:1px solid var(--line);padding:8px 12px;border-radius:20px;color:var(--green);white-space:nowrap}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:23px;min-width:0}
.status{grid-column:span 3}.keys{grid-column:span 3}h2{font-size:12px;letter-spacing:2px;color:var(--muted);margin:0 0 22px}
.controls{display:flex;align-items:center;gap:14px;flex-wrap:wrap}button{font:700 16px system-ui;padding:16px 34px;border:0;border-radius:9px;cursor:pointer;background:var(--green);color:#092019}
button.stop{background:#3a202a;color:#ffafbe;border:1px solid #714151}button:disabled{opacity:.4;cursor:default}
.metric{font-size:38px;font-weight:650;letter-spacing:-1px;margin-bottom:14px}.details{display:flex;justify-content:space-between;gap:16px;margin-top:13px;color:var(--muted)}.details b{color:#eef3fa;font-weight:500}
.scroll{overflow:auto}table{width:100%;border-collapse:collapse;text-align:left}th{color:var(--muted);font-size:12px;font-weight:500}td,th{padding:13px 10px;border-bottom:1px solid var(--line)}td{font-variant-numeric:tabular-nums}
#error{color:#ffafbe;min-height:24px;margin:16px 0}footer{color:var(--muted);font-size:12px;margin-top:22px}
@media(max-width:700px){.grid{grid-template-columns:1fr}.status,.keys{grid-column:auto}header{align-items:flex-start;flex-direction:column}.controls .badge{width:100%}}
</style></head><body><main>
<header><div><h1>THE MACHINE<span style="color:var(--green)">.</span></h1><p>Local automation control · {{ target }}</p></div><span class="badge" id="connection">Connecting…</span></header>
<div class="grid"><section class="panel status"><h2>01 / STATUS</h2><div class="controls">
<button id="start" disabled>START</button><button class="stop" id="stop" disabled>STOP</button><span class="badge" id="phase">—</span>
<span>Uptime <b id="uptime">—</b></span></div><div id="error" role="status" aria-live="polite"></div></section>
<section class="panel keys"><h2>02 / KEYS</h2><div class="scroll"><table><thead><tr><th>LAST 4</th><th>USES</th><th>COOLDOWN</th><th>DEAD</th></tr></thead><tbody id="keys"></tbody></table></div></section>
<section class="panel"><h2>03 / ACCOUNTS</h2><div class="metric" id="today">—</div><small>Created today</small><div class="details">Total created <b id="accounts">—</b></div><div class="details">Success / fail <b id="rate">—</b></div></section>
<section class="panel"><h2>04 / PLAYS</h2><div class="metric" id="plays">—</div><small>Total plays</small><div id="sources"></div></section>
<section class="panel"><h2>05 / SPEND</h2><div class="metric" id="spend">—</div><small>Running estimate · USD</small><div class="details">Captcha solves <b id="captcha">—</b></div><div class="details">Proxy usage <b id="proxy">—</b></div></section>
</div>
<footer>localhost:8080 · Refreshes every 3 seconds · Worker hook awaits integration</footer>
</main><script>
const $=id=>document.getElementById(id);let busy=false;
const number=n=>Number(n||0).toLocaleString();
async function json(url,options){const r=await fetch(url,{cache:'no-store',...options});if(!r.ok)throw Error(`Request failed (${r.status})`);return r.json()}
function text(id,value){$(id).textContent=value}
function row(parent,values,tag='td'){const tr=document.createElement('tr');values.forEach(v=>{const el=document.createElement(tag);el.textContent=v;tr.append(el)});parent.append(tr)}
async function refresh(){
 try{const [s,k,t]=await Promise.all(['/api/status','/api/keys','/api/stats'].map(u=>json(u)));
 text('connection','● Connected');text('phase',s.running?s.phase.toUpperCase():'STOPPED');
 const u=s.uptime_seconds;text('uptime',`${Math.floor(u/3600)}h ${Math.floor(u%3600/60)}m ${u%60}s`);
 $('start').disabled=busy||s.running;$('stop').disabled=busy||!s.running;
 text('today',number(t.accounts_today));text('accounts',number(t.accounts_total));text('plays',number(t.plays_total));
 const total=t.accounts_success+t.accounts_failed;text('rate',total?`${(100*t.accounts_success/total).toFixed(1)}% / ${(100*t.accounts_failed/total).toFixed(1)}%`:'No attempts');
 text('spend',Number(t.spend_est).toLocaleString(undefined,{style:'currency',currency:'USD',minimumFractionDigits:4}));text('captcha',number(t.captcha_solves));text('proxy',`${Number(t.proxy_gb).toFixed(3)} GB`);
 $('sources').replaceChildren();Object.entries(t.plays_by_source).forEach(([source,count])=>{const d=document.createElement('div');d.className='details';const label=document.createElement('span');label.textContent=source;const b=document.createElement('b');b.textContent=number(count);d.append(label,b);$('sources').append(d)});
 $('keys').replaceChildren();k.forEach(v=>row($('keys'),['••••'+v.tag,number(v.uses),v.cooldown_until>Date.now()/1000?`${Math.ceil(v.cooldown_until-Date.now()/1000)}s`:'Ready',v.dead?'DEAD':'—']));if(!k.length)row($('keys'),['No keys configured','—','—','—']);text('error','');
 }catch(e){text('connection','● Disconnected');text('error',e.message);$('start').disabled=true;$('stop').disabled=true}
}
async function control(action){busy=true;$('start').disabled=true;$('stop').disabled=true;try{await json('/api/'+action,{method:'POST'});busy=false;await refresh()}catch(e){busy=false;text('error',e.message)}}
$('start').onclick=()=>control('start');$('stop').onclick=()=>control('stop');
async function poll(){await refresh();setTimeout(poll,3000)}poll();
</script></body></html>'''
app.jinja_loader = DictLoader({'index.html': HTML})
with lock:
    roll_day()
    save()
atexit.register(stop_event.set)
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8080, threaded=True, use_reloader=False)
