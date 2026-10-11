# The Machine — local dashboard

Single-file Flask + embedded Jinja HTML + vanilla JavaScript. No frontend build.

```sh
# Termux: pkg install python
pip install -r requirements.txt
cp config.example.yaml config.yaml
# Edit config.yaml with your settings and provider prices.
python dashboard.py
```

Open http://localhost:8080. State is stored in `state/*.json` beside the app.
Keep one app process running; JSON persistence is designed for one writer.
All six requested routes are implemented and commented. The UI polls every
three seconds and adapts to mobile screens. Cooldown timestamps are Unix seconds.
Daily counts reset at local midnight; success/fail percentages are lifetime.

The repository has no automation implementation. START launches a cancellable
background worker, which waits in the idle phase until integrated. No account
creation, plays, or spend is simulated. Add your work inside `orchestrator()`,
check `stop_event` regularly, and give external calls finite timeouts. Set phases
and key metadata under `lock`; call `record_activity()` for actual results.
Config includes keys, proxy, target, captcha key, and per-unit USD rates.

STOP requests cooperative cancellation. Counters survive restarts; after a
restart the worker remains stopped until START is pressed. A stop in progress
prevents another worker from launching. JSON writes use atomic replacements;
unreadable state fails visibly instead of silently discarding counters.
Only key suffixes and hashed identities are persisted, never raw credentials.
