#!/usr/bin/env python3
"""Deploy tu repo + quan ly app bang pm2.  Chay qua alias:  git up [lenh] [tuy chon]

Lenh:
  (mac dinh) deploy  keo code moi -> cai thu vien neu can -> build neu can
                     -> restart CHI app co code/thu vien/cau hinh thay doi
  status             trang thai app: pm2, commit dang chay, co can restart
  doctor             kiem tra moi truong (chi doc, khong thay doi gi)
  setup              cai alias git up, pm2-logrotate, chuyen app tu systemd
                     sang pm2, pm2 save + pm2 startup

Tuy chon: --yes (dong y restart app tien that), --dry-run, --only a,b,
          --no-restart, --restart (ep restart app chon), --force-deps

Nguyen tac an toan:
  * Chi fast-forward (khong merge/rebase/reset); working tree co sua doi ->
    dung. Local co commit chua push / lech nhanh -> dung.
  * Biet app dang chay commit nao: thoi diem process start (pm2) doi chieu
    git reflog -> chi restart khi file Python app import (tinh theo AST),
    requirements, cau hinh pm2 cua app, hoac thu vien da cai thay doi. Nho
    vay ca truong hop pull tay ma quen restart cung duoc phat hien.
  * App tien that (confirm=true trong apps.json) phai xac nhan y/N hoac --yes.
  * Kiem tra cu phap Python truoc khi restart; health check sau restart.
"""
from __future__ import print_function

import argparse
import ast
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime

DEPLOY_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DEPLOY_DIR)
STATE_DIR = os.path.join(ROOT, ".deploy")
ECOSYSTEM = os.path.join(DEPLOY_DIR, "ecosystem.config.js")
APPS_JSON = os.path.join(DEPLOY_DIR, "apps.json")
SELF_FILES = ("deploy/", "deploy.sh")
# Bien systemd tu dat, khong can co trong ENV_FILE khi chuyen sang pm2.
SYSTEM_ENV = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
              "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "TZ", "TERM",
              "VIRTUAL_ENV", "PYTHONPATH", "PYTHONIOENCODING"}

TTY = sys.stdout.isatty()


def _c(code, s):
    return "\033[%sm%s\033[0m" % (code, s) if TTY else s


def info(msg=""):
    print(msg, flush=True)


def step(msg):
    info(_c("1;36", "==> " + msg))


def ok(msg):
    info("  " + _c("32", "OK ") + msg)


def skip(msg):
    info("  " + _c("2", "-- " + msg))


def warn(msg):
    info("  " + _c("33", "!! " + msg))


def bad(msg):
    info("  " + _c("31", "XX " + msg))


class DeployError(Exception):
    pass


# --------------------------------------------------------------- shell/git
def run(cmd, check=True, cwd=None, timeout=None, capture=True, env=None):
    try:
        res = subprocess.run(cmd, cwd=cwd or ROOT, timeout=timeout, env=env,
                             stdout=subprocess.PIPE if capture else None,
                             stderr=subprocess.PIPE if capture else None,
                             universal_newlines=True)
    except FileNotFoundError:
        raise DeployError("khong tim thay lenh: %s" % cmd[0])
    except subprocess.TimeoutExpired:
        raise DeployError("qua thoi gian (%ss): %s" % (timeout, " ".join(cmd)))
    if check and res.returncode != 0:
        err = ((res.stderr or "") + (res.stdout or "")).strip()
        raise DeployError("%s -> ma %s\n%s" % (" ".join(cmd), res.returncode,
                                               err[-2000:]))
    return res


def git(*args, **kw):
    return run(["git"] + list(args), **kw).stdout.strip()


def git_ok(*args):
    return run(["git"] + list(args), check=False).returncode == 0


# ------------------------------------------------------------------ config
def parse_env_file(path):
    """KEY=VALUE; '#' comment; bo 'export ' va nhay bao quanh. Khong eval."""
    out = {}
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except (IOError, OSError):
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = re.sub(r"^export\s+", "", key.strip())
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            out[key] = val
    return out


def load_config():
    cfg = parse_env_file(os.path.join(DEPLOY_DIR, "deploy.env"))
    cfg.update(parse_env_file(os.path.join(DEPLOY_DIR, "deploy.local.env")))
    return cfg


def app_key(name):
    return re.sub(r"[^A-Z0-9]", "_", name.upper())


def cfg_for(cfg, key, name):
    own = cfg.get("%s_%s" % (key, app_key(name)))
    return own if own else cfg.get(key, "")


def abspath(p):
    return os.path.normpath(os.path.join(ROOT, p)) if p else p


def load_apps(cfg, only=None):
    with open(APPS_JSON) as f:
        spec = json.load(f)["apps"]
    names = (cfg.get("APPS") or " ".join(spec)).split()
    unknown = [n for n in names if n not in spec]
    if unknown:
        raise DeployError("APPS co app khong co trong apps.json: %s"
                          % " ".join(unknown))
    if only:
        bad_only = [n for n in only if n not in names]
        if bad_only:
            raise DeployError("--only: app khong nam trong APPS: %s"
                              % " ".join(bad_only))
        names = [n for n in names if n in only]
    apps = []
    for n in names:
        s = dict(spec[n])
        s["name"] = n
        s["python"] = abspath(cfg_for(cfg, "PYTHON", n))
        env_file = cfg_for(cfg, "ENV_FILE", n)
        s["env_file"] = abspath(env_file) if env_file else ""
        apps.append(s)
    return apps


# -------------------------------------------------------- import closure
class TreeReader(object):
    """Doc file tracked tai 1 commit (khong phu thuoc working tree)."""

    def __init__(self, rev):
        self.rev = rev
        out = git("ls-tree", "-r", "--name-only", rev)
        self.files = set(out.splitlines())
        self._cache = {}

    def exists(self, rel):
        return rel in self.files

    def read(self, rel):
        if rel not in self._cache:
            self._cache[rel] = run(["git", "show", "%s:%s" % (self.rev, rel)],
                                   check=False).stdout or ""
        return self._cache[rel]


def _module_candidates(dotted):
    parts = dotted.split(".")
    out = []
    for i in range(1, len(parts) + 1):
        base = "/".join(parts[:i])
        out.append((i == len(parts), [base + ".py", base + "/__init__.py"]))
    return out


def _resolve(dotted, dirs, tree):
    """Danh sach file repo cua module `dotted` tim trong `dirs`."""
    for d in dirs:
        found = []
        for _last, cands in _module_candidates(dotted):
            hit = None
            for c in cands:
                rel = os.path.normpath(os.path.join(d, c)) if d else c
                if tree.exists(rel):
                    hit = rel
                    break
            if hit is None:
                break
            found.append(hit)
        else:
            return found
        if found:
            # tim duoc package cha nhung khong co module con: van tinh cha
            return found
    return []


def import_closure(entry_rel, search_path, tree):
    """Tap file .py (tuong doi goc repo) ma entry import truc tiep/gian tiep.

    Xet ca import trong ham; du thua (over-approx) an toan hon thieu."""
    seen = set()
    todo = [entry_rel]
    while todo:
        rel = todo.pop()
        if rel in seen or not tree.exists(rel):
            continue
        seen.add(rel)
        try:
            node = ast.parse(tree.read(rel), rel)
        except SyntaxError:
            continue        # check_syntax se bao loi
        here = os.path.dirname(rel)
        dirs = list(search_path) + [here]
        for n in ast.walk(node):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                if n.level:
                    base = here
                    for _ in range(n.level - 1):
                        base = os.path.dirname(base)
                    pkg = (n.module or "").replace(".", "/")
                    for a in n.names:
                        for c in ([pkg + ".py", pkg + "/__init__.py"]
                                  if pkg else []) + \
                                 [os.path.join(pkg, a.name) + ".py"]:
                            r = os.path.normpath(os.path.join(base, c))
                            if tree.exists(r):
                                todo.append(r)
                    continue
                mods = [n.module] + ["%s.%s" % (n.module, a.name)
                                     for a in n.names]
            for m in mods:
                todo.extend(_resolve(m, dirs, tree))
    return seen


def app_dependencies(app, tree):
    entry = os.path.normpath(os.path.join(app["cwd"], app["entry"]))
    deps = import_closure(entry, app.get("search_path") or [app["cwd"]], tree)
    # requirements KHONG tinh o day: sua chu ma thu vien khong doi thi khong
    # restart; thu vien that su doi -> step_deps (so pip freeze) bao restart.
    deps.add("deploy/run-app.sh")
    return deps


# ---------------------------------------------------------------- pm2
def pm2_bin(cfg=None):
    path = (cfg or {}).get("PM2_BIN") or shutil.which("pm2")
    if not path:
        raise DeployError("chua cai pm2. Cai: sudo npm install -g pm2 "
                          "(can nodejs >= 16)")
    return path


def pm2(cfg, *args, **kw):
    return run([pm2_bin(cfg)] + list(args), **kw)


def pm2_list(cfg):
    out = pm2(cfg, "jlist", timeout=60).stdout
    data = None
    # pm2 co the in canh bao "[PM2] ..." truoc JSON -> thu tung dong '['
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("["):
            try:
                data = json.loads("\n".join(lines[i:]))
                break
            except ValueError:
                continue
    if not isinstance(data, list):
        raise DeployError("khong doc duoc pm2 jlist:\n" + out[-1000:])
    res = {}
    for p in data:
        env = p.get("pm2_env") or {}
        if env.get("pmx_module"):
            res.setdefault("_modules", set()).add(p.get("name"))
            continue
        status = env.get("status")
        # pm2 + exp_backoff_restart_delay gan nhan 'waiting restart' ca khi
        # exit code nam trong stop_exit_codes (KHONG restart that) -> sua nhan.
        if status == "waiting restart" and not p.get("pid") and \
                env.get("exit_code") in (env.get("stop_exit_codes") or []):
            status = "stopped"
        res[p.get("name")] = {
            "status": status,
            "pid": p.get("pid"),
            "uptime_ms": env.get("pm_uptime"),
            "restarts": env.get("restart_time", 0),
            "out_log": env.get("pm_out_log_path"),
            "err_log": env.get("pm_err_log_path"),
            "exit_code": env.get("exit_code"),
        }
    return res


def ecosystem_defs(cfg):
    node = shutil.which("node")
    if not node:
        raise DeployError("khong tim thay node (pm2 can nodejs)")
    code = ("process.stdout.write(JSON.stringify("
            "require(process.argv[1]).apps))")
    out = run([node, "-e", code, ECOSYSTEM], timeout=30).stdout
    return {a["name"]: a for a in json.loads(out)}


def def_hash(d):
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


def _state_path(*parts):
    p = os.path.join(STATE_DIR, *parts)
    d = os.path.dirname(p)
    if not os.path.isdir(d):
        os.makedirs(d)
    return p


def read_stamp(*parts):
    try:
        with open(os.path.join(STATE_DIR, *parts)) as f:
            return f.read().strip()
    except (IOError, OSError):
        return None


def write_stamp(value, *parts):
    p = _state_path(*parts)
    with open(p + ".tmp", "w") as f:
        f.write(value + "\n")
    os.replace(p + ".tmp", p)


def _changed_path():
    return _state_path("changed_at.json")


def mark_changed(key, when=None):
    """Ghi thoi diem moi truong chay doi (thu vien/build). App start TRUOC moc
    nay phai restart - ke ca khi lan restart ngay sau do bi bo qua/loi (lan
    git up sau van thay, khong phu thuoc 'da cai')."""
    p = _changed_path()
    try:
        with open(p) as f:
            data = json.load(f)
    except (IOError, OSError, ValueError):
        data = {}
    data[key] = float(when if when is not None else time.time())
    with open(p + ".tmp", "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)


def env_changes_for(app):
    """[(moc, mo_ta)] cac lan thu vien/build cua app doi."""
    try:
        with open(os.path.join(STATE_DIR, "changed_at.json")) as f:
            data = json.load(f)
    except (IOError, OSError, ValueError):
        return []
    out = []
    ts = data.get("pip:" + app["python"])
    if ts:
        out.append((ts, "thu vien Python"))
    cwd = os.path.normpath(app["cwd"])
    for key, ts in data.items():
        if key.startswith("build:"):
            d = os.path.normpath(key[6:])
            if d == "." or cwd == d or cwd.startswith(d + os.sep):
                out.append((ts, "build %s" % key[6:]))
    return out


def tail_file(path, n=25):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return lines[-n:]
    except (IOError, OSError, TypeError):
        return []


# ------------------------------------------------------------ git state
def reflog_entries():
    """[(unix_ts, sha)] HEAD tu moi nhat -> cu nhat."""
    out = git("reflog", "show", "--date=unix", "--format=%H %gd", "HEAD",
              check=False)
    res = []
    for line in out.splitlines():
        m = re.match(r"^([0-9a-f]{40}) \S*@\{(\d+)\}$", line.strip())
        if m:
            res.append((int(m.group(2)), m.group(1)))
    return res


def commit_at(ts, entries):
    """Commit HEAD tai thoi diem ts (process start) -> code process dang chay."""
    for when, sha in entries:
        if when <= ts:
            return sha
    return None


def changed_between(a, b):
    if a == b:
        return []
    res = run(["git", "diff", "--name-only", a, b], check=False)
    if res.returncode != 0:
        return None
    return [x for x in res.stdout.splitlines() if x]


def short(sha):
    return sha[:7] if sha else "?"


# ------------------------------------------------------------- locking
def acquire_lock():
    p = _state_path("deploy.lock")
    fh = open(p, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        raise DeployError("dang co mot deploy khac chay (%s)" % p)
    return fh


def confirm(question, assume_yes):
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        ans = input("  " + _c("1;33", "?? " + question + " [y/N] "))
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes", "c", "co")


# ================================================================ steps
def check_branch(cfg):
    branch = git("symbolic-ref", "--short", "-q", "HEAD", check=False)
    if not branch:
        raise DeployError("HEAD dang detached - checkout nhanh deploy truoc")
    want = cfg.get("DEPLOY_BRANCH") or branch
    if want != branch:
        raise DeployError("dang o nhanh '%s' nhung DEPLOY_BRANCH='%s'. "
                          "Chay: git checkout %s" % (branch, want, want))
    return branch


def step_pull(cfg, branch, dry_run):
    """-> (old_head, new_head, pulled)."""
    step("Kiem tra code moi (%s/%s)" % (cfg.get("DEPLOY_REMOTE", "origin"),
                                        branch))
    remote = cfg.get("DEPLOY_REMOTE") or "origin"
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise DeployError("working tree co file da sua (git stash hoac commit "
                          "truoc):\n" + dirty)
    git("fetch", "--quiet", remote, branch, timeout=180)
    local = git("rev-parse", "HEAD")
    target = git("rev-parse", "%s/%s" % (remote, branch))
    if local == target:
        skip("khong co code moi (HEAD %s)" % short(local))
        return local, local, False
    if git_ok("merge-base", "--is-ancestor", local, target):
        log = git("log", "--oneline", "--no-decorate", "-n", "30",
                  "%s..%s" % (local, target))
        info("  Commit moi:")
        for line in log.splitlines():
            info("    " + line)
        if dry_run:
            skip("--dry-run: khong pull")
            return local, target, False
        git("merge", "--ff-only", "--quiet", target)
        ok("da fast-forward %s -> %s" % (short(local), short(target)))
        return local, target, True
    if git_ok("merge-base", "--is-ancestor", target, local):
        n = git("rev-list", "--count", "%s..%s" % (target, local))
        warn("local co %s commit chua push len %s/%s - khong pull "
             "(git push truoc)" % (n, remote, branch))
        return local, local, False
    raise DeployError("local va %s/%s da lech nhau (diverged). Xu ly tay: "
                      "git log --oneline --graph HEAD %s/%s"
                      % (remote, branch, remote, branch))


def step_deps(cfg, apps, dry_run, force):
    """Cai requirements theo tung python. -> set(app) co thu vien thay doi."""
    step("Kiem tra thu vien Python")
    groups = {}
    for a in apps:
        groups.setdefault(a["python"], []).append(a)
    changed_apps = set()
    for py, group in groups.items():
        reqs = sorted({r for a in group for r in a.get("requirements") or []
                       if os.path.exists(abspath(r))})
        names = ", ".join(a["name"] for a in group)
        if not os.path.exists(py):
            bad("khong thay python %s (%s) - dat PYTHON trong "
                "deploy/deploy.local.env" % (py, names))
            continue
        h = hashlib.sha256(py.encode())
        for r in reqs:
            with open(abspath(r), "rb") as f:
                h.update(r.encode() + b"\0" + f.read())
        digest = h.hexdigest()
        stamp_name = ("pip", hashlib.sha1(py.encode()).hexdigest()[:12])
        if not force and read_stamp(*stamp_name) == digest:
            skip("requirements khong doi (%s)" % names)
            continue
        info("  requirements doi/chua cai cho %s: %s" % (names, " ".join(reqs)))
        if dry_run:
            skip("--dry-run: khong cai")
            continue
        before = run([py, "-m", "pip", "freeze", "--all"], check=False).stdout
        cmd = [py, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
        for r in reqs:
            cmd += ["-r", abspath(r)]
        try:
            run(cmd, timeout=1800, capture=False)
        except DeployError:
            # cai duoc mot phan roi loi: van danh dau neu venv da doi, de app
            # duoc restart sau khi sua (lan sau freeze khong con khac nua)
            if run([py, "-m", "pip", "freeze", "--all"],
                   check=False).stdout != before:
                mark_changed("pip:" + py)
            raise
        after = run([py, "-m", "pip", "freeze", "--all"], check=False).stdout
        if before != after:
            mark_changed("pip:" + py)     # TRUOC stamp: chet giua chung -> cai lai
            diff = sorted(set(after.splitlines()) - set(before.splitlines()))
            ok("da cai/nang cap: %s" % (", ".join(diff[:10]) or "?"))
            changed_apps.update(a["name"] for a in group)
        write_stamp(digest, *stamp_name)
        if before == after:
            ok("da du thu vien, khong thay doi gi")
    return changed_apps


def step_build(apps, dry_run):
    """Build web neu co package.json (hien chua co: dashboard la Streamlit)."""
    step("Kiem tra build web")
    pkgs = [p for p in git("ls-files", "*package.json").splitlines()
            if "node_modules/" not in p]
    if not pkgs:
        skip("khong co phan web can build (dashboard Streamlit chay truc tiep "
             "tu app.py)")
        return set()
    rebuilt = set()
    for pkg in pkgs:
        d = os.path.dirname(pkg) or "."
        files = git("ls-files", d).splitlines()
        h = hashlib.sha256()
        for rel in sorted(files):
            if os.path.exists(abspath(rel)):
                with open(abspath(rel), "rb") as f:
                    h.update(rel.encode() + b"\0" + f.read())
        digest = h.hexdigest()
        stamp = ("build", hashlib.sha1(d.encode()).hexdigest()[:12])
        if read_stamp(*stamp) == digest:
            skip("%s khong doi" % d)
            continue
        if dry_run:
            info("  %s can build (--dry-run: bo qua)" % d)
            continue
        with open(abspath(pkg)) as f:
            scripts = json.load(f).get("scripts") or {}
        has_lock = os.path.exists(os.path.join(abspath(d), "package-lock.json"))
        run(["npm", "ci" if has_lock else "install"], cwd=abspath(d),
            timeout=1800, capture=False)
        if "build" in scripts:
            run(["npm", "run", "build"], cwd=abspath(d), timeout=1800,
                capture=False)
        mark_changed("build:" + d)
        write_stamp(digest, *stamp)
        ok("da build %s" % d)
        rebuilt.update(a["name"] for a in apps
                       if os.path.normpath(a["cwd"]).startswith(
                           os.path.normpath(d)) or d == ".")
    return rebuilt


def plan_restarts(cfg, apps, head, force_names=()):
    """-> list dict {app, action, reasons, recreate, info}."""
    procs = pm2_list(cfg)
    defs = ecosystem_defs(cfg)
    tree = TreeReader(head)
    entries = reflog_entries()
    plan = []
    for a in apps:
        name = a["name"]
        p = procs.get(name)
        item = {"app": a, "info": p, "reasons": [], "recreate": False,
                "action": "skip", "running": None}
        plan.append(item)
        if p is None:
            item["action"] = "missing"
            continue
        if p["status"] != "online":
            item["action"] = p["status"] or "unknown"
            continue
        stored = read_stamp("appdef", name)
        cur = def_hash(defs.get(name, {}))
        if stored is None:
            write_stamp(cur, "appdef", name)
        elif stored != cur:
            item["reasons"].append("cau hinh pm2 cua app thay doi")
            item["recreate"] = True
        running = commit_at(int((p["uptime_ms"] or 0) / 1000), entries)
        item["running"] = running
        if running is None:
            item["reasons"].append("khong xac dinh duoc commit dang chay "
                                   "(process start truoc moc reflog)")
        elif running != head:
            files = changed_between(running, head)
            if files is None:
                item["reasons"].append("khong so sanh duoc %s..%s"
                                       % (short(running), short(head)))
            else:
                hit = sorted(set(files) & app_dependencies(a, tree))
                if hit:
                    more = " +%d" % (len(hit) - 4) if len(hit) > 4 else ""
                    item["reasons"].append("code doi: %s%s"
                                           % (", ".join(hit[:4]), more))
        started = (p["uptime_ms"] or 0) / 1000.0
        for ts, what in env_changes_for(a):
            if started < ts:
                item["reasons"].append("%s doi luc %s, sau khi app start" % (
                    what, datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")))
        if name in force_names:
            item["reasons"].append("--restart")
        if item["reasons"]:
            item["action"] = "restart"
    return plan


def describe(item, head):
    a, p = item["app"], item["info"]
    act = item["action"]
    tag = "[tien that] " if a.get("confirm") else ""
    if act == "missing":
        return "%s: %schua chay duoi pm2 (git up setup)" % (a["name"], tag)
    if act == "restart":
        return "%s: %sCAN RESTART - %s" % (a["name"], tag,
                                           "; ".join(item["reasons"]))
    if act == "skip":
        if item["running"] and item["running"] != head:
            return ("%s: dang chay %s, thay doi toi %s khong dong toi app -> "
                    "khong restart" % (a["name"], short(item["running"]),
                                       short(head)))
        return "%s: dang chay code moi nhat (%s), khong restart" % (
            a["name"], short(head))
    extra = ""
    if p and p.get("exit_code") is not None:
        extra = ", exit=%s" % p["exit_code"]
    return ("%s: %spm2 status=%s%s -> khong tu start (pm2 start %s neu muon "
            "chay)" % (a["name"], tag, act, extra, a["name"]))


def check_syntax(app, tree):
    files = [abspath(f) for f in sorted(app_dependencies(app, tree))
             if f.endswith(".py") and os.path.exists(abspath(f))]
    code = ("import sys\nbad=[]\nfor p in sys.argv[1:]:\n"
            "    try:\n        compile(open(p,'rb').read(), p, 'exec')\n"
            "    except SyntaxError as e:\n"
            "        bad.append('%s:%s: %s' % (p, e.lineno, e.msg))\n"
            "print('\\n'.join(bad))\nsys.exit(1 if bad else 0)\n")
    res = run([app["python"], "-c", code] + files, check=False, timeout=120)
    return res.returncode == 0, (res.stdout or res.stderr).strip()


def binance_state_summary(app):
    sf = app.get("state_file")
    if not sf:
        return None
    try:
        with open(abspath(sf)) as f:
            s = json.load(f)
    except Exception:
        return None
    return "state: %d lot dang mo, halt=%s" % (
        len(s.get("positions") or []), s.get("halt_reason") or "khong")


def health_check(cfg, app, baseline_restarts):
    secs = int(app.get("health_seconds") or 20)
    deadline = time.time() + secs
    info("  theo doi %s trong %ds..." % (app["name"], secs))
    p = None
    while time.time() < deadline:
        time.sleep(2)
        p = pm2_list(cfg).get(app["name"])
        if not p or p["status"] != "online" or \
                (p["restarts"] or 0) > baseline_restarts:
            break
    else:
        ok("%s online on dinh (pid %s)" % (app["name"], p and p["pid"]))
        return True
    status = p["status"] if p else "khong thay"
    bad("%s KHONG on dinh: status=%s restarts=%s exit=%s" % (
        app["name"], status, p and p["restarts"], p and p.get("exit_code")))
    for label, key in (("stdout", "out_log"), ("stderr", "err_log")):
        lines = tail_file((p or {}).get(key), 25)
        if lines:
            info("  --- %s (%s) ---" % (label, (p or {}).get(key)))
            for line in lines:
                info("    " + line)
    return False


def start_or_restart(cfg, app, recreate, exists):
    name = app["name"]
    timeout = int(app.get("kill_timeout") or 10000) / 1000.0 + 120
    if exists and recreate:
        pm2(cfg, "delete", name, timeout=timeout)
        exists = False
    if exists:
        pm2(cfg, "restart", name, timeout=timeout)
    else:
        pm2(cfg, "start", ECOSYSTEM, "--only", name, timeout=timeout)
    defs = ecosystem_defs(cfg)
    write_stamp(def_hash(defs.get(name, {})), "appdef", name)
    p = pm2_list(cfg).get(name) or {}
    return p.get("restarts") or 0


def append_history(rec):
    rec["ts"] = datetime.now().isoformat(timespec="seconds")
    with open(_state_path("history.log"), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ============================================================== commands
def cmd_deploy(args, cfg):
    lock = acquire_lock()
    branch = check_branch(cfg)
    only = args.only.split(",") if args.only else None
    apps_all = load_apps(cfg)
    apps = load_apps(cfg, only)

    if args.resume_from:
        old = args.resume_from
        new = git("rev-parse", "HEAD")
        info(_c("2", "(chay tiep bang deploy.py moi sau khi pull)"))
    else:
        old, new, pulled = step_pull(cfg, branch, args.dry_run)
        if pulled:
            files = changed_between(old, new) or []
            if any(f.startswith(SELF_FILES) for f in files):
                info(_c("2", "  script deploy co thay doi -> chay lai ban moi"))
                lock.close()
                argv = [sys.executable, os.path.join(DEPLOY_DIR, "deploy.py")]
                argv += [a for a in sys.argv[1:]] + ["--resume-from", old]
                os.execv(sys.executable, argv)

    head = new if args.dry_run else git("rev-parse", "HEAD")
    deps_changed = step_deps(cfg, apps_all, args.dry_run, args.force_deps)
    rebuilt = step_build(apps_all, args.dry_run)

    step("Kiem tra app can restart")
    if args.dry_run and head != git("rev-parse", "HEAD"):
        info(_c("2", "  (--dry-run: so voi commit %s chua pull)" % short(head)))
    plan = plan_restarts(cfg, apps, head,
                         force_names=set(only or []) if args.restart else ())
    for item in plan:
        (skip if item["action"] == "skip" else warn)(describe(item, head))

    todo = [i for i in plan if i["action"] == "restart"]
    results = {}
    if args.no_restart or args.dry_run:
        if todo:
            skip("%s: khong restart" % ("--dry-run" if args.dry_run
                                        else "--no-restart"))
        todo = []
    tree = TreeReader(head) if todo else None
    failed = False
    for item in todo:
        a = item["app"]
        name = a["name"]
        good, msg = check_syntax(a, tree)
        if not good:
            bad("%s: loi cu phap Python -> KHONG restart:\n%s" % (name, msg))
            results[name] = "syntax-error"
            failed = True
            continue
        if a.get("confirm"):
            summary = binance_state_summary(a)
            if summary:
                info("  %s %s" % (name, summary))
            if not confirm("Restart %s (%s, giao dich tien that)?"
                           % (name, a.get("label", "")), args.yes):
                warn("bo qua restart %s - chay lai: git up --yes --only %s"
                     % (name, name))
                results[name] = "skipped"
                continue
        info("  restart %s ..." % name)
        try:
            base = start_or_restart(cfg, a, item["recreate"], True)
        except DeployError as e:
            bad(str(e))
            results[name] = "error"
            failed = True
            continue
        if health_check(cfg, a, base):
            results[name] = "restarted"
        else:
            results[name] = "unhealthy"
            failed = True
    if results:
        pm2(cfg, "save", check=False, timeout=60)
    if not args.dry_run:
        append_history({"branch": branch, "from": old, "to": head,
                        "deps": sorted(deps_changed), "results": results})
    step("Ket qua")
    info("  HEAD %s%s" % (short(head), "" if old == head
                         else " (truoc: %s)" % short(old)))
    for name, res in results.items():
        (ok if res == "restarted" else bad if res != "skipped" else warn)(
            "%s: %s" % (name, res))
    if not results:
        skip("khong app nao duoc restart")
    return 1 if failed else 0


def cmd_status(args, cfg):
    only = args.only.split(",") if args.only else None
    apps = load_apps(cfg, only)
    head = git("rev-parse", "HEAD")
    branch = git("symbolic-ref", "--short", "-q", "HEAD", check=False)
    step("Repo: nhanh %s, HEAD %s" % (branch or "(detached)", short(head)))
    procs = pm2_list(cfg)
    plan = plan_restarts(cfg, apps, head)
    step("App")
    now = time.time()
    for item in plan:
        p = item["info"]
        if p:
            up = int(now - (p["uptime_ms"] or 0) / 1000.0)
            info("  %-18s %-8s pid=%-7s uptime=%-8s restarts=%-3s commit=%s"
                 % (item["app"]["name"], p["status"], p["pid"],
                    "%dh%02dm" % (up // 3600, up % 3600 // 60)
                    if p["status"] == "online" else "-",
                    p["restarts"], short(item["running"])))
        (warn if item["action"] in ("restart", "missing", "errored")
         else skip)(describe(item, head))
    mods = procs.get("_modules") or set()
    if "pm2-logrotate" not in mods:
        warn("chua cai pm2-logrotate (git up setup)")
    return 0


# ---------------------------------------------------------- systemd/doctor
def systemd_unit(name):
    if not shutil.which("systemctl") or \
            not os.path.isdir("/run/systemd/system"):
        return None
    props = ["LoadState", "ActiveState", "UnitFileState", "ExecStart",
             "WorkingDirectory", "Environment", "EnvironmentFiles", "User",
             "FragmentPath"]
    res = run(["systemctl", "show", name + ".service", "-p", ",".join(props)],
              check=False, timeout=20)
    if res.returncode != 0:
        return None
    out = {}
    for line in res.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    if out.get("LoadState") != "loaded":
        return None
    return out


def unit_env_names(unit):
    """Ten bien (KHONG gia tri) unit systemd cung cap + file khong doc duoc."""
    names, unreadable = set(), []
    try:
        for tok in shlex.split(unit.get("Environment") or ""):
            if "=" in tok:
                names.add(tok.split("=", 1)[0])
    except ValueError:
        pass
    for part in (unit.get("EnvironmentFiles") or "").split(") "):
        path = part.split(" (")[0].strip().lstrip("-")
        if not path:
            continue
        if os.access(path, os.R_OK):
            names.update(parse_env_file(path))
        else:
            unreadable.append(path)
    return names, unreadable


def unit_exec(unit):
    m = re.search(r"argv\[\]=([^;]*);", unit.get("ExecStart") or "")
    return m.group(1).strip() if m else (unit.get("ExecStart") or "")


def foreign_processes(app, managed_pids):
    """Process ngoai pm2 dang chay cung entry (systemd, nohup, cron...)."""
    entry_abs = abspath(os.path.join(app["cwd"], app["entry"]))
    cwd_abs = abspath(app["cwd"])
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) in managed_pids or \
                int(pid) == os.getpid():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                argv = [x.decode("utf-8", "replace")
                        for x in f.read().split(b"\0") if x]
            cwd = os.readlink("/proc/%s/cwd" % pid)
        except (IOError, OSError):
            continue
        if not argv or "python" not in os.path.basename(argv[0]):
            continue
        for arg in argv[1:]:
            full = os.path.normpath(os.path.join(cwd, arg))
            if full == entry_abs:
                found.append((int(pid), " ".join(argv)))
                break
        else:
            if os.path.normpath(cwd) == cwd_abs and app["entry"] in argv:
                found.append((int(pid), " ".join(argv)))
    return found


def managed_pids(procs):
    pids = set()
    for name, p in procs.items():
        if name != "_modules" and p.get("pid"):
            pids.add(int(p["pid"]))
            pids.update(_children(int(p["pid"])))
    return pids


def _children(pid):
    out = set()
    try:
        for t in os.listdir("/proc/%d/task" % pid):
            with open("/proc/%d/task/%s/children" % (pid, t)) as f:
                for c in f.read().split():
                    out.add(int(c))
                    out.update(_children(int(c)))
    except (IOError, OSError):
        pass
    return out


def cron_lines(apps):
    res = run(["crontab", "-l"], check=False) if shutil.which("crontab") \
        else None
    if not res or res.returncode != 0:
        return []
    keys = [a["entry"] for a in apps] + ["streamlit"]
    return [line for line in res.stdout.splitlines()
            if not line.strip().startswith("#") and any(k in line for k in keys)]


def check_app_env(app, unit):
    """-> (loi, canh bao) truoc khi chuyen tu systemd sang pm2."""
    errors, warns = [], []
    if not os.path.exists(app["python"]):
        errors.append("khong thay python %s" % app["python"])
    have = set()
    if app["env_file"]:
        if not os.access(app["env_file"], os.R_OK):
            errors.append("khong doc duoc ENV_FILE %s" % app["env_file"])
        else:
            have = set(parse_env_file(app["env_file"]))
            mode = os.stat(app["env_file"]).st_mode & 0o777
            if mode & 0o077:
                warns.append("ENV_FILE %s quyen %o - nen chmod 600"
                             % (app["env_file"], mode))
    if unit:
        need, unreadable = unit_env_names(unit)
        missing = sorted(need - have - SYSTEM_ENV)
        if missing:
            errors.append("systemd cap bien %s nhung ENV_FILE (%s) khong co "
                          "-> them vao truoc khi chuyen"
                          % (", ".join(missing), app["env_file"] or "trong"))
        for path in unreadable:
            warns.append("khong doc duoc EnvironmentFile %s (quyen root?) - "
                         "tu kiem tra no co trong ENV_FILE" % path)
        if unit.get("User") and unit["User"] != os.environ.get("USER", ""):
            warns.append("systemd chay bang user %s, pm2 se chay bang %s"
                         % (unit["User"], os.environ.get("USER")))
    return errors, warns


def cmd_doctor(args, cfg):
    only = args.only.split(",") if args.only else None
    apps = load_apps(cfg, only)
    step("Moi truong")
    info("  repo     %s" % ROOT)
    info("  nhanh    %s (DEPLOY_BRANCH=%s)" % (
        git("symbolic-ref", "--short", "-q", "HEAD", check=False),
        cfg.get("DEPLOY_BRANCH") or "nhanh hien tai"))
    alias = git("config", "--get", "alias.up", check=False)
    (ok if "deploy.py" in alias else warn)("alias git up: %s" % (
        alias or "chua cai (git up setup)"))
    try:
        info("  pm2      %s (%s)" % (pm2(cfg, "-v").stdout.strip().splitlines()[-1],
                                     pm2_bin(cfg)))
        procs = pm2_list(cfg)
    except DeployError as e:
        bad(str(e))
        procs = {}
    if "pm2-logrotate" not in (procs.get("_modules") or set()):
        warn("chua cai pm2-logrotate")
    user = os.environ.get("USER", "")
    if systemd_unit("pm2-%s" % user):
        ok("pm2 startup (tu chay lai khi reboot) da bat: pm2-%s" % user)
    else:
        warn("chua bat pm2 startup (git up setup)")
    pids = managed_pids(procs)
    for a in apps:
        step("App %s" % a["name"])
        unit = systemd_unit(a["name"])
        p = procs.get(a["name"])
        info("  pm2      %s" % (p["status"] if p else "chua co"))
        if unit:
            info("  systemd  %s / %s  (%s)" % (unit.get("ActiveState"),
                                              unit.get("UnitFileState"),
                                              unit.get("FragmentPath")))
            info("    ExecStart  %s" % unit_exec(unit))
            info("    WorkingDir %s" % unit.get("WorkingDirectory"))
            names, unreadable = unit_env_names(unit)
            info("    Env (ten)  %s" % (", ".join(sorted(names)) or "-"))
        else:
            info("  systemd  khong co unit %s.service" % a["name"])
        info("  python   %s" % a["python"])
        info("  ENV_FILE %s" % (a["env_file"] or "-"))
        errors, warns = check_app_env(a, unit)
        for e in errors:
            bad(e)
        for w in warns:
            warn(w)
        others = foreign_processes(a, pids)
        for pid, cmd in others:
            warn("process ngoai pm2: pid %d  %s" % (pid, cmd))
        if errors:
            continue
        if p:
            ok("dang chay duoi pm2")
        elif others or (unit and unit.get("ActiveState") == "active"):
            warn("san sang - git up setup se dung ban cu (systemd) truoc khi "
                 "chay bang pm2; process ngoai systemd phai tu dung")
        else:
            ok("san sang chay duoi pm2")
    lines = cron_lines(apps)
    if lines:
        step("Crontab lien quan (watchdog cu? xoa sau khi chuyen pm2)")
        for line in lines:
            warn(line)
    return 0


def cmd_setup(args, cfg):
    lock = acquire_lock()  # noqa: F841
    only = args.only.split(",") if args.only else None
    apps = load_apps(cfg, only)
    step("Alias git up")
    want = "!exec python3 deploy/deploy.py"
    if git("config", "--get", "alias.up", check=False) == want:
        skip("da co")
    else:
        git("config", "alias.up", want)
        ok("git config alias.up '%s' (repo nay)" % want)

    step("pm2")
    procs = pm2_list(cfg)
    ok("pm2 %s" % pm2(cfg, "-v").stdout.strip().splitlines()[-1])
    if "pm2-logrotate" in (procs.get("_modules") or set()):
        skip("pm2-logrotate da cai")
    elif confirm("Cai pm2-logrotate (xoay vong log pm2)?", args.yes):
        pm2(cfg, "install", "pm2-logrotate", timeout=600, capture=False)
        for k, v in (("max_size", cfg.get("LOGROTATE_MAX_SIZE", "20M")),
                     ("retain", cfg.get("LOGROTATE_RETAIN", "10")),
                     ("compress", "true")):
            pm2(cfg, "set", "pm2-logrotate:%s" % k, v, check=False)
        ok("da cai pm2-logrotate")

    failed = False
    for a in apps:
        name = a["name"]
        step("App %s" % name)
        procs = pm2_list(cfg)
        if name in procs:
            skip("da chay duoi pm2 (status=%s)" % procs[name]["status"])
            continue
        unit = systemd_unit(name)
        errors, warns = check_app_env(a, unit)
        for w in warns:
            warn(w)
        if errors:
            for e in errors:
                bad(e)
            failed = True
            continue
        if unit and (unit.get("ActiveState") in ("active", "activating",
                                                 "reloading")
                     or unit.get("UnitFileState") == "enabled"):
            info("  systemd: %s (%s)" % (unit_exec(unit),
                                         unit.get("ActiveState")))
            if not confirm("Dung + disable systemd %s.service roi chay bang "
                           "pm2?" % name, args.yes):
                warn("bo qua %s" % name)
                continue
            run(["sudo", "systemctl", "disable", "--now", name + ".service"],
                timeout=300, capture=False)
            ok("da dung + disable %s.service" % name)
        pids = managed_pids(pm2_list(cfg))
        others = foreign_processes(a, pids)
        if others:
            for pid, cmd in others:
                bad("van con process ngoai pm2: pid %d  %s" % (pid, cmd))
            bad("dung cac process tren (va watchdog cron neu co) roi chay lai "
                "git up setup - KHONG chay trung 2 ban")
            failed = True
            continue
        if a.get("confirm") and not unit and not confirm(
                "Start %s (%s, giao dich tien that) bang pm2?"
                % (name, a.get("label", "")), args.yes):
            warn("bo qua %s" % name)
            continue
        base = start_or_restart(cfg, a, False, False)
        if not health_check(cfg, a, base):
            failed = True

    lines = cron_lines(apps)
    if lines:
        step("Crontab")
        for line in lines:
            warn("dong cron co the khoi dong trung app (watchdog cu?) -> xoa "
                 "bang crontab -e: %s" % line)
    pm2(cfg, "save", timeout=60)
    ok("pm2 save")

    step("pm2 startup (tu chay lai sau reboot)")
    user = os.environ.get("USER") or ""
    if not os.path.isdir("/run/systemd/system"):
        warn("may nay khong chay systemd - tu cau hinh pm2 resurrect khi boot")
    elif systemd_unit("pm2-%s" % user):
        skip("da bat (pm2-%s.service)" % user)
    elif confirm("Bat pm2 startup bang systemd (can sudo)?", args.yes):
        node_dir = os.path.dirname(shutil.which("node") or "/usr/bin/node")
        cmd = ["sudo", "env", "PATH=%s:%s" % (os.environ.get("PATH", ""),
                                              node_dir),
               pm2_bin(cfg), "startup", "systemd", "-u", user,
               "--hp", os.path.expanduser("~")]
        try:
            run(cmd, timeout=300, capture=False)
            pm2(cfg, "save", timeout=60)
            ok("da bat pm2 startup")
        except DeployError as e:
            warn("khong bat duoc pm2 startup (%s). Chay tay: %s"
                 % (str(e).splitlines()[0], " ".join(shlex.quote(c)
                                                       for c in cmd)))
    return 1 if failed else 0


# ==================================================================== main
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="git up", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="deploy",
                    choices=["deploy", "status", "doctor", "setup"])
    ap.add_argument("-y", "--yes", action="store_true",
                    help="dong y restart/start app tien that khong hoi")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="chi xem se lam gi (fetch nhung khong pull/cai/restart)")
    ap.add_argument("--only", help="chi xu ly cac app nay (phay ngan cach)")
    ap.add_argument("--no-restart", action="store_true",
                    help="pull + cai thu vien, khong restart")
    ap.add_argument("--restart", action="store_true",
                    help="ep restart cac app trong --only")
    ap.add_argument("--force-deps", action="store_true",
                    help="chay pip install du requirements khong doi")
    ap.add_argument("--resume-from", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.restart and not args.only:
        ap.error("--restart can --only <app>")
    cfg = load_config()
    try:
        return {"deploy": cmd_deploy, "status": cmd_status,
                "doctor": cmd_doctor, "setup": cmd_setup}[args.command](
                    args, cfg)
    except DeployError as e:
        bad(str(e))
        return 2
    except KeyboardInterrupt:
        bad("da huy")
        return 130


if __name__ == "__main__":
    sys.exit(main())
