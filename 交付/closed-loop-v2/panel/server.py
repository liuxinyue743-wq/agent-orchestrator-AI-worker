#!/usr/bin/env python
"""closed-loop-v2 web panel — zero-dependency local UI for the closed loop.

Double-click 启动面板.bat (or run this file with the venv python) and the
browser opens on the panel. The panel drives the EXACT runner code path the
CLI uses (run_mission.build_runtime); all state is read from the mission's
SQLite store, so the panel never modifies kernel behavior.

Bind: 127.0.0.1 only. Port: 7100 (override with PANEL_PORT).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PANEL_DIR = Path(__file__).resolve().parent
ROOT = PANEL_DIR.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import run_mission  # noqa: E402
from loopcore.envelope import Envelope, MessageKind  # noqa: E402
from loopcore.mission import MISSION_TERMINAL  # noqa: E402
from loopcore.event_normalizer import now_iso  # noqa: E402

PORT = int(os.environ.get("PANEL_PORT", "7100"))


# ------------------------------------------------------------------ state
class PanelState:
    """Owns the active mission runtime (if any) and the runner thread."""

    def __init__(self):
        self.lock = threading.RLock()
        self.rt = None                 # run_mission.MissionRuntime | None
        self.thread = None
        self.stop_flag = threading.Event()
        self.started_mono = None
        self.last_summary = None
        self.live = {                   # live-tunable time knobs (seconds)
            "poll_seconds": 5,
            "idle_audit_cooldown_seconds": 300,
            "blocked_escalation_seconds": 600,
            "l0_nudge_grace_seconds": 300,
        }
        self.errors = []
        # 风险边界开关：MISSION_DONE 后把目标仓库 master 快进到已验证的
        # 集成头并推 origin（AO 的 worker 基线取自 origin/master，只合本地
        # 会让下个任务的 worker 拿到旧基线）。默认关。
        self.auto_ff_master = False

    # ---- mission lifecycle
    def start_mission(self, mission_dict: dict) -> str:
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError("已有任务在运行，先停止或等待完成")
            run_mission.setup_environment()
            cfg = run_mission.load_config()
            cfg.setdefault("observer", {})
            for k in ("idle_audit_cooldown_seconds",
                      "blocked_escalation_seconds", "l0_nudge_grace_seconds"):
                cfg["observer"][k] = self.live[k]
            self.rt = run_mission.build_runtime(mission_dict, cfg)
            self.stop_flag.clear()
            self.started_mono = time.monotonic()
            self.last_summary = None
            self.thread = threading.Thread(target=self._run, daemon=True,
                                           name="mission-runner")
            self.thread.start()
            return mission_dict["mission_id"]

    def _run(self):
        rt = self.rt
        try:
            started = time.monotonic()
            while True:
                rt.controller.step()
                rt.projector.project_once()
                state = rt.controller.state
                if state in MISSION_TERMINAL or self.stop_flag.is_set():
                    break
                if time.monotonic() - started >= 7200:      # 2h hard cap
                    break
                time.sleep(max(1.0, float(self.live["poll_seconds"])))
            rt.projector.project_once()
            self.last_summary = {
                "mission_id": rt.mission.mission_id,
                "final_state": rt.controller.state,
                "stopped_by_user": self.stop_flag.is_set(),
            }
            if (rt.controller.state == "MISSION_DONE" and self.auto_ff_master
                    and not self.stop_flag.is_set()):
                try:
                    self.last_summary["auto_ff"] = ff_master_to_integration(
                        rt.mission.mission_id, rt.mission.project_id)
                except Exception as e:                       # never die mute
                    self.last_summary["auto_ff"] = {"error": str(e)}
                    self.errors.append("%s: auto-ff: %s" % (now_iso(), e))
        except Exception as e:                               # never die mute
            self.errors.append("%s: runner: %s" % (now_iso(), e))

    def stop(self):
        self.stop_flag.set()

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    # ---- directive channel
    def post_directive(self, target: str, text: str) -> dict:
        target, text = (target or "").strip(), (text or "").strip()
        if not target or not text:
            raise RuntimeError("target 和 text 都不能为空")
        with self.lock:
            if not self.rt:
                raise RuntimeError("没有已加载的任务")
            d = self.rt.controller.directives.post(target, text)
            # record on the bus (traffic + planner mirror semantics live in
            # the envelope layer) so bus_traffic.jsonl shows the exchange.
            try:
                env = Envelope(sender="user", receiver=target,
                               kind=MessageKind.USER_DIRECTIVE,
                               thread_id="U-%d" % int(time.time() * 1000),
                               payload={"directive": text})
                self.rt.bus.submit(env)
                log = self.rt.runtime / "bus_traffic.jsonl"
                with open(log, "a", encoding="utf-8") as f:
                    f.write(json.dumps(
                        {"at": now_iso(), "kind": env.kind.value,
                         "sender": "user", "receiver": target,
                         "payload": {"directive": text}},
                        ensure_ascii=False) + "\n")
            except Exception as e:
                self.errors.append("%s: bus: %s" % (now_iso(), e))
            return {"target": d.target, "text": d.text, "at": d.at,
                    "mirrored_to_planner": target != "planner"}

    # ---- live config
    def set_config(self, updates: dict) -> dict:
        with self.lock:
            for k in self.live:
                if k in updates:
                    self.live[k] = max(1, int(updates[k]))
            if "auto_ff_master" in updates:
                self.auto_ff_master = bool(updates["auto_ff_master"])
            if self.rt:      # controller reads these per call -> instant
                obs = self.rt.controller.cfg.setdefault("observer", {})
                for k in ("idle_audit_cooldown_seconds",
                          "blocked_escalation_seconds",
                          "l0_nudge_grace_seconds"):
                    obs[k] = self.live[k]
            out = dict(self.live)
            out["auto_ff_master"] = self.auto_ff_master
            return out


PANEL = PanelState()


# --------------------------------------------------------------- snapshot
def _ro_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.as_posix(), uri=True,
                           timeout=3)
    return conn


def _rows(conn, sql, args=(), retries=3):
    for i in range(retries):
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            if i == retries - 1:
                return []
            time.sleep(0.15)
    return []


def ff_master_to_integration(mission_id: str, project_id: str) -> dict:
    """Fast-forward the target repo's master to the mission's VERIFIED
    integration head, then push to origin when the project has one.

    Pushing is NOT optional under AO: workers branch from
    refs/remotes/origin/master (ao.db sessions.diff_base_ref), so a
    local-only merge hands the NEXT mission a stale base -> integration
    conflict -> HUMAN (observed on MISSION-PANEL-20260830-203226).

    Safety rails: refuse when the integration worktree is missing, when the
    main checkout is not on master/main, or when tracked files are dirty.
    Only ever fires after final gate + mission Verifier PASS (MISSION_DONE).
    """
    wt = Path(run_mission.AO_DATA_DIR) / "worktrees" / project_id / \
        ("integration-" + mission_id)
    if not wt.exists():
        raise RuntimeError("集成 worktree 不存在: %s" % wt.name)

    def git(*args, cwd):
        p = subprocess.run(["git"] + list(args), cwd=cwd,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        if p.returncode != 0:
            raise RuntimeError("git %s: %s"
                               % (args[0], (p.stderr or p.stdout or "")[:200]))
        return (p.stdout or "").strip()

    common = git("rev-parse", "--path-format=absolute", "--git-common-dir",
                 cwd=str(wt))
    main_repo = str(Path(common).parent)
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=main_repo)
    if branch not in ("master", "main"):
        raise RuntimeError("主仓库当前在 %s 分支，拒绝自动合并" % branch)
    dirty = [ln for ln in git("status", "--porcelain", cwd=main_repo)
             .splitlines() if ln and not ln.startswith("??")]
    if dirty:
        raise RuntimeError("master 有未提交的 tracked 改动，拒绝自动合并: %s"
                           % dirty[0])
    head = git("rev-parse", "integration-" + mission_id, cwd=main_repo)
    before = git("rev-parse", "HEAD", cwd=main_repo)
    if before != head:
        git("merge", "--ff-only", head, cwd=main_repo)
    remotes = git("remote", cwd=main_repo).split()
    pushed = False
    if "origin" in remotes:
        git("push", "origin", "HEAD:%s" % branch, cwd=main_repo)
        pushed = True
    note = ("master %s -> %s" % (before[:8], head[:8])) \
        if before != head else "master 已是最新"
    return {"repo": main_repo, "master": head[:8], "pushed": pushed,
            "note": note + ("，已推 origin" if pushed else "")}


def list_missions() -> list:
    out = []
    base = ROOT / "runtime"
    if not base.exists():
        return out
    for d in sorted(base.iterdir(), reverse=True):
        db = d / "state.db"
        if not db.exists():
            continue
        try:
            conn = _ro_conn(db)
            r = _rows(conn, "SELECT payload_json FROM missions LIMIT 1")
            state, objective = "?", ""
            if r:
                payload = json.loads(r[0][0])
                state = payload.get("state", "?")
                objective = (payload.get("mission") or {}
                             ).get("objective", "")[:80]
            out.append({"mission_id": d.name, "state": state,
                        "objective": objective})
            conn.close()
        except Exception:
            continue
    return out


def snapshot() -> dict:
    with PANEL.lock:
        rt = PANEL.rt
        running = PANEL.running()
        live = dict(PANEL.live)
        live["auto_ff_master"] = PANEL.auto_ff_master
        errs = PANEL.errors[-10:]
        summary = PANEL.last_summary
    snap = {"ok": True, "running": running, "config": live,
            "panel_errors": errs, "last_summary": summary,
            "missions": list_missions(), "elapsed": None}
    if not rt:
        snap["mission"] = None
        return snap
    if PANEL.started_mono and running:
        snap["elapsed"] = round(time.monotonic() - PANEL.started_mono, 1)
    db_path = rt.runtime / "state.db"
    try:
        conn = _ro_conn(db_path)
    except Exception as e:
        snap["mission"] = {"id": rt.mission.mission_id, "state": "?",
                           "error": str(e)}
        return snap

    def _payloads(table, limit=50):
        # rowid works on every table (several store tables have no `id`
        # column — ORDER BY id silently returned nothing via the retry
        # swallow, e.g. verifications showed 0 rows).
        rows = _rows(conn, "SELECT payload_json FROM %s ORDER BY rowid DESC "
                           "LIMIT ?" % table, (limit,))
        out = []
        for (p,) in rows:
            try:
                out.append(json.loads(p))
            except Exception:
                pass
        return out

    mission_row = _rows(conn, "SELECT payload_json FROM missions LIMIT 1")
    mission_payload = json.loads(mission_row[0][0]) if mission_row else {}
    mstate = mission_payload.get("state") or \
        ("MISSION_READY" if running else "?")
    counters = {n: v for n, v in _rows(conn,
                                       "SELECT name, value FROM counters")}
    transitions = _rows(conn,
        "SELECT task_id, to_state, actor, reason, timestamp FROM "
        "state_transitions ORDER BY id DESC LIMIT 40")
    latest_state = {}
    for task_id, to_state, actor, reason, ts in transitions:
        latest_state.setdefault(task_id, (to_state, actor, ts))
    tasks = []
    for (spec,) in [(r[0],) for r in _rows(conn,
                    "SELECT spec_json FROM tasks")]:
        try:
            t = json.loads(spec)
        except Exception:
            continue
        tid = t.get("task_id", "?")
        st = latest_state.get(tid, ("TASK_READY", "", ""))
        tasks.append({
            "task_id": tid, "objective": (t.get("objective") or "")[:120],
            "state": st[0], "actor": st[1], "at": st[2],
            "worker_session_id": t.get("worker_session_id"),
            "local_fixes": counters.get("local_fixes:" + tid, 0),
            "replans": counters.get("replans:" + tid, 0),
            "max_local_fixes": (t.get("budgets") or {}).get(
                "max_local_fixes", "?"),
            "max_replans": (t.get("budgets") or {}).get("max_replans", "?"),
        })
    snap.update({
        "mission": {
            "id": rt.mission.mission_id,
            "state": mstate,
            "reason": mission_payload.get("reason", ""),
            "objective": rt.mission_dict.get("objective", "")[:200],
        },
        "subtasks": sorted(tasks, key=lambda t: t["task_id"]),
        "transitions": [{"task": t[0], "to": t[1], "actor": t[2],
                         "reason": (t[3] or "")[:100], "at": t[4]}
                        for t in transitions[:25]],
        "gate_runs": _payloads("gate_runs", 6),
        "audits": _payloads("audits", 4),
        "verifications": _payloads("verifications", 4),
        "alerts": _payloads("alerts", 10),
        "counters": counters,
        "directives_pending": rt.controller.directives.pending_count(),
    })
    conn.close()
    # traffic tail
    log = rt.runtime / "bus_traffic.jsonl"
    if log.exists():
        try:
            lines = log.read_text(encoding="utf-8",
                                  errors="replace").splitlines()[-30:]
            snap["traffic"] = [json.loads(x) for x in lines if x.strip()]
        except Exception:
            snap["traffic"] = []
    else:
        snap["traffic"] = []
    return snap


def read_file(rt, name: str) -> str:
    p = rt.runtime / name
    if p.exists():
        return p.read_text(encoding="utf-8", errors="replace")
    return "(尚未生成)"


# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "ClosedLoopPanel/1.0"

    def log_message(self, *a):           # quiet
        pass

    # -- helpers
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- routing
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path == "/index.html":
            html = (PANEL_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if path == "/api/state":
            self._json(snapshot())
            return
        if path == "/api/stream":
            self._sse()
            return
        if path.startswith("/api/file"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            if name not in ("memory.md", "project.md"):
                self._json({"ok": False, "error": "bad name"}, 400)
                return
            with PANEL.lock:
                rt = PANEL.rt
            if not rt:
                self._json({"ok": False, "error": "no mission"}, 400)
                return
            self._json({"ok": True, "name": name,
                        "content": read_file(rt, name)})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/mission":
                self._json(self._start_mission(body))
                return
            if path == "/api/resume":
                self._json(self._resume(body))
                return
            if path == "/api/attach":
                self._json(self._attach(body))
                return
            if path == "/api/stop":
                PANEL.stop()
                self._json({"ok": True})
                return
            if path == "/api/directive":
                d = PANEL.post_directive(str(body.get("target") or ""),
                                         str(body.get("text") or ""))
                self._json({"ok": True, "directive": d})
                return
            if path == "/api/config":
                self._json({"ok": True,
                            "config": PANEL.set_config(body)})
                return
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    # -- mission builders
    def _start_mission(self, body: dict) -> dict:
        objective = (body.get("objective") or "").strip()
        if not objective:
            raise RuntimeError("objective 不能为空")
        mid = "MISSION-PANEL-%s" % time.strftime("%Y%m%d-%H%M%S")
        allowed = [p.strip() for p in (body.get("allowed_paths") or "")
                   .splitlines() if p.strip()] or ["app.py", "math2.py",
                                                   "tests/**"]
        acs = []
        for i, line in enumerate((body.get("acceptance_criteria") or "")
                                 .splitlines()):
            line = line.strip()
            if line:
                acs.append({"id": "AC-%02d" % (i + 1), "description": line})
        if not acs:
            raise RuntimeError("至少一条验收条件")
        gates = [g.strip() for g in (body.get("gate_commands") or "")
                 .splitlines() if g.strip()] or ["python -m pytest -q"]
        mission = {
            "mission_id": mid,
            "project_id": body.get("project_id") or "closed-loop-demo",
            "objective": objective,
            "allowed_paths": allowed,
            "forbidden_paths": [".git/**"],
            "acceptance_criteria": acs,
            "gate_commands": gates,
            "user_instruction": body.get("user_instruction") or "",
            "worker_harness": "claude-code",
            "budgets": {"max_subtasks": int(body.get("max_subtasks") or 2),
                        "max_total_replans": 2,
                        "max_runtime_seconds": 3600,
                        "subtask_budgets": {
                            "max_local_fixes": 2, "max_replans": 1,
                            "max_same_alerts": 2,
                            "max_runtime_seconds": 1800}},
        }
        # persist for resume/reference
        tasks_dir = ROOT / "tasks"
        tasks_dir.mkdir(exist_ok=True)
        (tasks_dir / ("%s.json" % mid.lower())).write_text(
            json.dumps(mission, ensure_ascii=False, indent=2), "utf-8")
        PANEL.start_mission(mission)
        return {"ok": True, "mission_id": mid}

    def _resume(self, body: dict) -> dict:
        mid = (body.get("mission_id") or "").strip()
        db = ROOT / "runtime" / mid / "state.db"
        if not db.exists():
            raise RuntimeError("找不到该任务的运行存档: " + mid)
        conn = _ro_conn(db)
        r = _rows(conn, "SELECT payload_json FROM missions LIMIT 1")
        conn.close()
        if not r:
            raise RuntimeError("存档中没有 mission 定义")
        mission = json.loads(r[0][0]).get("mission")
        if not mission:
            raise RuntimeError("存档 mission 定义损坏")
        PANEL.start_mission(mission)          # store resumes in place
        return {"ok": True, "mission_id": mid, "resumed": True}

    def _attach(self, body: dict) -> dict:
        """Load a stored mission READ-ONLY for inspection (no runner thread,
        no provider calls — the kernel is never stepped)."""
        mid = (body.get("mission_id") or "").strip()
        db = ROOT / "runtime" / mid / "state.db"
        if not db.exists():
            raise RuntimeError("找不到该任务的运行存档: " + mid)
        conn = _ro_conn(db)
        r = _rows(conn, "SELECT payload_json FROM missions LIMIT 1")
        conn.close()
        if not r:
            raise RuntimeError("存档中没有 mission 定义")
        mission = json.loads(r[0][0]).get("mission")
        if not mission:
            raise RuntimeError("存档 mission 定义损坏")
        with PANEL.lock:
            if PANEL.running():
                raise RuntimeError("任务运行中，先停止再查看其它存档")
            run_mission.setup_environment()
            cfg = run_mission.load_config()
            PANEL.rt = run_mission.build_runtime(mission, cfg, dry_run=True)
            PANEL.last_summary = None
            PANEL.started_mono = None
        return {"ok": True, "mission_id": mid, "attached": True}

    # -- SSE
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        deadline = time.monotonic() + 300     # client reconnects
        while time.monotonic() < deadline:
            try:
                payload = json.dumps(snapshot(), ensure_ascii=False)
                self.wfile.write(("data: %s\n\n" % payload).encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            time.sleep(2)


def main() -> int:
    run_mission.setup_environment()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = "http://127.0.0.1:%d/" % PORT
    print("[panel] %s  (Ctrl+C 停止)" % url, flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    PANEL.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
