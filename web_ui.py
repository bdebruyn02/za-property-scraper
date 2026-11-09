#!/usr/bin/env python
"""Local web UI for the property scraper.

    python web_ui.py              # opens http://127.0.0.1:8765 in your browser
    python web_ui.py --port 9000 --no-open

Only listens on 127.0.0.1 (this machine). One scrape at a time, run in a background thread
with exactly the same polite settings as the CLI."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import re
import threading
import webbrowser
from collections import deque
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from scraper import onboarding
from scraper.analysis import DEFAULT_SCORING
from scraper.pipeline import HOW_TO_INTERPRET, load_config, rescore_run, run
from scraper.sites import SITES, adapter_for

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "searches.json"
OUTPUT = HERE / "output"
STATIC = HERE / "webui"
RUN_FILE = re.compile(r"^(listings|claude_export)_\d{8}_\d{6}\.(csv|json)$")

app = Flask(__name__, static_folder=None)
log = logging.getLogger("scraper")


def effective_config(overrides: dict | None) -> dict:
    """Saved config, with optional session-only overrides from the onboarding wizard."""
    cfg = load_config(CONFIG)
    if overrides:
        cfg["filters"] = {**cfg.get("filters", {}), **(overrides.get("filters") or {})}
        cfg["scoring"] = {**cfg.get("scoring", {}), **(overrides.get("scoring") or {})}
        if overrides.get("notes_for_claude"):
            cfg["notes_for_claude"] = overrides["notes_for_claude"]
        if overrides.get("searches"):
            cfg["searches"] = overrides["searches"]
    return cfg


# --------------------------------------------------------------------------- job runner
class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"            # idle | running | done | error | cancelled
        self.lines: deque[tuple[int, str]] = deque(maxlen=3000)
        self.seq = 0
        self.result: dict | None = None
        self.error: str | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None

    def add(self, text: str):
        with self.lock:
            self.seq += 1
            self.lines.append((self.seq, text))


JOB = Job()


class JobLogHandler(logging.Handler):
    def emit(self, record):
        JOB.add(self.format(record))


def _run_job(opts: dict):
    handler = JobLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        cfg = effective_config(opts.get("overrides"))
        if opts.get("overrides"):
            JOB.add("Using your session goal (not saved to settings).")
        searches = cfg["searches"]
        if opts.get("only"):
            wanted = set(opts["only"])
            searches = [dict(s, enabled=True) for s in searches if s["name"] in wanted]
        res = run(cfg, searches, OUTPUT, new_only=bool(opts.get("new_only")),
                  debug=bool(opts.get("debug")), max_pages=opts.get("max_pages") or None,
                  allow_browser=not opts.get("no_browser"), min_delay=3.0,
                  apply_filters=not opts.get("no_filters"), cancel=JOB.cancel)
        summ = res["export"]["summary"]
        JOB.result = {"stamp": res["stamp"], "matching": summ["matching"],
                      "scraped_total": summ["scraped_total"], "sites": res["site_stats"],
                      "requests": res["requests"]}
        blocked = [f"{k}: {v['blocked']}" for k, v in res["site_stats"].items() if v["blocked"]]
        for b in blocked:
            JOB.add(f"BLOCKED  {b}")
        JOB.add(f"Done: {summ['scraped_total']} scraped, {summ['matching']} match, "
                f"{res['requests']} requests. Run {res['stamp']}.")
        JOB.state = "cancelled" if res.get("cancelled") else "done"
    except Exception as e:                      # surface anything to the UI
        log.exception("Run failed")
        JOB.error = f"{type(e).__name__}: {e}"
        JOB.state = "error"
    finally:
        log.removeHandler(handler)
        JOB.finished_at = dt.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- API
@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC, name)


@app.get("/api/config")
def get_config():
    cfg = load_config(CONFIG)
    cfg.setdefault("scoring", {})
    for k in ("bonus_keywords", "red_flag_keywords"):
        cfg["scoring"].setdefault(k, DEFAULT_SCORING[k])
    return jsonify({"config": cfg, "supported_domains": [d for c in SITES for d in c.domains]})


def _validate(cfg: dict) -> list[str]:
    errors = []
    if not isinstance(cfg.get("searches"), list):
        return ["searches must be a list"]
    names = set()
    for i, s in enumerate(cfg["searches"], 1):
        if not s.get("name"):
            errors.append(f"search {i}: name is required")
        elif s["name"] in names:
            errors.append(f"search {i}: duplicate name '{s['name']}'")
        names.add(s.get("name"))
        try:
            adapter_for(s.get("url", ""))
        except ValueError as e:
            errors.append(f"search {i} ({s.get('name', '?')}): {e}")
        mp = s.get("max_pages")
        if mp is not None and (not isinstance(mp, int) or not 1 <= mp <= 20):
            errors.append(f"search {i}: max_pages must be 1-20")
    f = cfg.get("filters", {})
    for k in ("min_price", "max_price", "min_beds", "max_beds", "min_size_m2"):
        v = f.get(k)
        if v is not None and not isinstance(v, (int, float)):
            errors.append(f"filters.{k} must be a number or empty")
    if f.get("min_price") and f.get("max_price") and f["min_price"] > f["max_price"]:
        errors.append("min_price is above max_price")
    mp = cfg.get("max_pages")
    if mp is not None and (not isinstance(mp, int) or not 1 <= mp <= 20):
        errors.append("max_pages must be 1-20")
    return errors


@app.put("/api/config")
def put_config():
    cfg = request.get_json(force=True, silent=True)
    if not isinstance(cfg, dict):
        return jsonify({"errors": ["body must be a JSON object"]}), 400
    errors = _validate(cfg)
    if errors:
        return jsonify({"errors": errors}), 400
    if CONFIG.exists():
        (HERE / "searches.backup.json").write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return jsonify({"ok": True})


@app.post("/api/run")
def start_run():
    if JOB.state == "running":
        return jsonify({"error": "A scrape is already running"}), 409
    opts = request.get_json(force=True, silent=True) or {}
    JOB.state, JOB.result, JOB.error = "running", None, None
    JOB.lines.clear()
    JOB.cancel = threading.Event()
    JOB.started_at = dt.datetime.now().isoformat(timespec="seconds")
    JOB.finished_at = None
    JOB.add("Starting scrape (one request at a time, 3-6 s apart)...")
    JOB.thread = threading.Thread(target=_run_job, args=(opts,), daemon=True)
    JOB.thread.start()
    return jsonify({"ok": True})


@app.post("/api/stop")
def stop_run():
    if JOB.state == "running":
        JOB.cancel.set()
        JOB.add("Stop requested - finishing the current request and saving what was collected...")
    return jsonify({"ok": True})


@app.get("/api/status")
def status():
    since = request.args.get("since", default=0, type=int)
    with JOB.lock:
        lines = [{"n": n, "text": t} for n, t in JOB.lines if n > since]
    return jsonify({"state": JOB.state, "lines": lines, "next": JOB.seq, "result": JOB.result,
                    "error": JOB.error, "started_at": JOB.started_at, "finished_at": JOB.finished_at})


@app.get("/api/runs")
def runs():
    out = []
    for p in sorted(OUTPUT.glob("claude_export_*.json"), reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        stamp = p.stem.replace("claude_export_", "")
        s = d.get("summary", {})
        out.append({"stamp": stamp, "generated_at": d.get("generated_at"), "new_only": d.get("new_only"),
                    "matching": s.get("matching"), "scraped_total": s.get("scraped_total"),
                    "has_scores": "market" in d})
    return jsonify(out)


def _stamp_ok(stamp: str):
    if not re.fullmatch(r"\d{8}_\d{6}", stamp):
        abort(404)


@app.get("/api/runs/<stamp>")
def run_detail(stamp):
    _stamp_ok(stamp)
    p = OUTPUT / f"claude_export_{stamp}.json"
    if not p.exists():
        abort(404)
    return app.response_class(p.read_text(encoding="utf-8"), mimetype="application/json")


@app.get("/api/runs/<stamp>/all")
def run_all_rows(stamp):
    """Every scraped listing of a run (incl. ones the filters rejected), from its CSV."""
    _stamp_ok(stamp)
    p = OUTPUT / f"listings_{stamp}.csv"
    if not p.exists():
        abort(404)
    with p.open(encoding="utf-8-sig") as fh:
        return jsonify(list(csv.DictReader(fh)))


@app.post("/api/runs/<stamp>/rescore")
def run_rescore(stamp):
    """Re-apply current filters/scoring to a saved run without touching the network."""
    _stamp_ok(stamp)
    if JOB.state == "running":
        return jsonify({"error": "Wait for the running scrape to finish"}), 409
    if not (OUTPUT / f"listings_{stamp}.csv").exists():
        abort(404)
    body = request.get_json(force=True, silent=True) or {}
    res = rescore_run(effective_config(body.get("overrides")), stamp, OUTPUT)
    return jsonify({"stamp": res["stamp"], "matching": res["export"]["summary"]["matching"]})


@app.get("/api/onboarding")
def onboarding_state():
    cfg = load_config(CONFIG)
    ob = cfg.get("onboarding") or {}
    return jsonify({"catalog": onboarding.catalog(), "done": bool(ob.get("done")),
                    "answers": ob.get("answers")})


@app.post("/api/onboarding/preview")
def onboarding_preview():
    answers = request.get_json(force=True, silent=True) or {}
    errors = onboarding.validate(answers)
    if errors:
        return jsonify({"errors": errors}), 400
    return jsonify(onboarding.build(answers))


@app.post("/api/onboarding/apply")
def onboarding_apply():
    body = request.get_json(force=True, silent=True) or {}
    answers = body.get("answers") or {}
    errors = onboarding.validate(answers)
    if errors:
        return jsonify({"errors": errors}), 400
    built = onboarding.build(answers)
    if (body.get("notes") or "").strip():
        built["notes_for_claude"] = body["notes"].strip()
    replace = bool(body.get("replace_searches"))
    if body.get("save"):
        cfg = onboarding.apply_to_config(load_config(CONFIG), built, answers, replace)
        errs = _validate(cfg)
        if errs:
            return jsonify({"errors": errs}), 400
        if CONFIG.exists():
            (HERE / "searches.backup.json").write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return jsonify({"saved": True})
    overrides = {"filters": built["filters"], "scoring": built["scoring"],
                 "notes_for_claude": built["notes_for_claude"]}
    if replace:
        overrides["searches"] = built["searches"]
    return jsonify({"saved": False, "overrides": overrides})


@app.get("/api/interpretation")
def interpretation():
    return jsonify({"how_to_interpret": HOW_TO_INTERPRET})


@app.get("/files/<name>")
def files(name):
    if not RUN_FILE.match(name):
        abort(404)
    return send_from_directory(OUTPUT, name, as_attachment=True)


def main():
    ap = argparse.ArgumentParser(description="Property scraper web UI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()
    OUTPUT.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Property scraper UI: {url}   (Ctrl+C to quit)")
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
