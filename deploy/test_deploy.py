"""Test offline cho deploy/ (khong can pm2 that).

Chay:  python3 deploy/test_deploy.py
Dung repo git tam + pm2 gia; khong dung toi repo that hay ~/.pm2.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import deploy as d  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS " + name)
    else:
        FAIL += 1
        print("  FAIL %s | %s" % (name, detail))


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,
                          universal_newlines=True).stdout.strip()


def git_init(path):
    os.makedirs(path, exist_ok=True)
    sh(["git", "init", "-q", "-b", "main"], path)
    sh(["git", "config", "user.email", "t@t"], path)
    sh(["git", "config", "user.name", "t"], path)


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def commit(root, msg):
    sh(["git", "add", "-A"], root)
    sh(["git", "commit", "-qm", msg], root)
    return sh(["git", "rev-parse", "HEAD"], root)


class use_root(object):
    """Tro deploy.py vao repo tam."""

    def __init__(self, root):
        self.root = root

    def __enter__(self):
        self.saved = (d.ROOT, d.STATE_DIR)
        d.ROOT = self.root
        d.STATE_DIR = os.path.join(self.root, ".deploy")

    def __exit__(self, *a):
        d.ROOT, d.STATE_DIR = self.saved


ENV_TEXT = """# comment
DEMO="a b $c"
export EXP=1
SINGLE='x=y'
 SPACED = v v
1BAD=no
NOEQ
EMPTY=
"""


def test_parse_env_and_run_app():
    print("\n[env file: deploy.py == run-app.sh]")
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "e.env")
        with open(p, "w") as f:
            f.write(ENV_TEXT)
        env = d.parse_env_file(p)
        check("parse: giu nguyen $ va dau cach, bo nhay",
              env.get("DEMO") == "a b $c" and env.get("SINGLE") == "x=y",
              env)
        check("parse: export, khoang trang, bo key sai",
              env.get("EXP") == "1" and env.get("SPACED") == "v v"
              and "1BAD" not in env and "NOEQ" not in env
              and env.get("EMPTY") == "", env)
        code = ("import os,json;print(json.dumps({k:os.environ.get(k) for k "
                "in ['DEMO','EXP','SINGLE','SPACED','EMPTY','KEEP',"
                "'PYTHONUNBUFFERED']}))")
        child_env = dict(os.environ, KEEP="from-env")
        with open(p, "a") as f:
            f.write("KEEP=from-file\n")
        out = subprocess.run(
            ["bash", os.path.join(HERE, "run-app.sh"), p, tmp,
             sys.executable, "-c", code], env=child_env,
            stdout=subprocess.PIPE, universal_newlines=True).stdout
        got = json.loads(out)
        check("run-app.sh nap giong parse_env_file",
              all(got[k] == env[k] for k in
                  ("DEMO", "EXP", "SINGLE", "SPACED", "EMPTY")), got)
        check("run-app.sh khong ghi de bien da co san",
              got["KEEP"] == "from-env", got)
        check("run-app.sh dat PYTHONUNBUFFERED", got["PYTHONUNBUFFERED"] == "1")
        res = subprocess.run(["bash", os.path.join(HERE, "run-app.sh"),
                              "/khong/co.env", tmp, sys.executable, "-c", ""])
        check("run-app.sh thieu ENV_FILE -> ma 78 (pm2 khong restart)",
              res.returncode == 78, res.returncode)
        res = subprocess.run(["bash", os.path.join(HERE, "run-app.sh"), "-",
                              tmp, "/khong/co/python"])
        check("run-app.sh thieu python -> ma 78", res.returncode == 78)
    finally:
        shutil.rmtree(tmp)


def test_ecosystem():
    print("\n[ecosystem.config.js]")
    if not shutil.which("node"):
        print("  (bo qua: khong co node)")
        return
    tmp = tempfile.mkdtemp()
    try:
        dep = os.path.join(tmp, "deploy")
        os.makedirs(dep)
        for f in ("ecosystem.config.js", "apps.json", "deploy.env"):
            shutil.copy(os.path.join(HERE, f), dep)
        with open(os.path.join(dep, "deploy.local.env"), "w") as f:
            f.write("APPS=muse-dashboard muse-binance\nDASHBOARD_PORT=9999\n"
                    "PYTHON_MUSE_BINANCE=/opt/py/bin/python\n"
                    "ENV_FILE_MUSE_DASHBOARD=\nDASHBOARD_ARGS=--a 1 \"--b x\"\n")
        out = subprocess.run(
            ["node", "-e", "process.stdout.write(JSON.stringify("
             "require(process.argv[1]).apps))",
             os.path.join(dep, "ecosystem.config.js")],
            stdout=subprocess.PIPE, universal_newlines=True).stdout
        apps = {a["name"]: a for a in json.loads(out)}
        check("chi app trong APPS, dung thu tu",
              list(apps) == ["muse-dashboard", "muse-binance"], list(apps))
        dash, bn = apps["muse-dashboard"], apps["muse-binance"]
        check("dashboard: port tu config + DASHBOARD_ARGS",
              "9999" in dash["args"] and dash["args"][-3:] == ["--a", "1",
                                                              "--b x"],
              dash["args"])
        check("dashboard: ENV_FILE rieng rong -> '-' (khong nap)",
              dash["args"][0] == os.path.join(tmp, ".env")
              or dash["args"][0] == "-", dash["args"][0])
        check("binance: PYTHON rieng tung app",
              bn["args"][2] == "/opt/py/bin/python", bn["args"])
        check("binance: thoat ma 0/78 KHONG restart (chong spam API)",
              bn["stop_exit_codes"] == [0, 78] and bn["autorestart"])
        check("binance: kill_timeout du dung sach",
              bn["kill_timeout"] >= 60000)
        check("script = run-app.sh qua bash",
              bn["script"].endswith("run-app.sh")
              and bn["interpreter"] == "/bin/bash")
        check("pm2 env khong chua secret", bn["env"] == {
            "DEPLOY_APP": "muse-binance"})
    finally:
        shutil.rmtree(tmp)


def test_commit_at():
    print("\n[commit dang chay theo reflog]")
    entries = [(300, "c3"), (200, "c2"), (100, "c1")]
    check("start sau moc moi nhat", d.commit_at(350, entries) == "c3")
    check("start giua 2 moc", d.commit_at(250, entries) == "c2")
    check("start dung moc", d.commit_at(200, entries) == "c2")
    check("start truoc moi moc -> None", d.commit_at(50, entries) is None)


def test_import_closure():
    print("\n[tap file app phu thuoc (AST)]")
    tmp = tempfile.mkdtemp()
    try:
        git_init(tmp)
        write(tmp, "app/main.py",
              "import os, helper\nfrom pkg import mod\nimport pkg.sub as s\n"
              "import lib\n"
              "def f():\n    import lazy\n")
        write(tmp, "app/helper.py", "from . import rel\n")
        write(tmp, "app/rel.py", "")
        write(tmp, "app/lazy.py", "")
        write(tmp, "app/pkg/__init__.py", "")
        write(tmp, "app/pkg/mod.py", "")
        write(tmp, "app/pkg/sub.py", "import deep\n")
        write(tmp, "app/deep.py", "")
        write(tmp, "app/test_main.py", "import main\n")
        write(tmp, "app/unused.py", "")
        write(tmp, "shared/lib.py", "import json\n")
        write(tmp, "shared/other.py", "")
        write(tmp, "app/broken.py", "def x(:\n")
        commit(tmp, "init")
        with use_root(tmp):
            tree = d.TreeReader("HEAD")
            got = d.import_closure("app/main.py", ["app", "shared"], tree)
        want = {"app/main.py", "app/helper.py", "app/rel.py", "app/lazy.py",
                "app/pkg/__init__.py", "app/pkg/mod.py", "app/pkg/sub.py",
                "app/deep.py", "shared/lib.py"}
        check("du file import (ca trong ham, tuong doi, package)",
              want <= got, sorted(want - got))
        check("khong lay test/file khong import",
              not ({"app/test_main.py", "app/unused.py", "shared/other.py",
                    "app/broken.py"} & got), sorted(got))
    finally:
        shutil.rmtree(tmp)


def _fake_repo():
    tmp = tempfile.mkdtemp()
    git_init(tmp)
    write(tmp, "bot/bot.py", "import eng\n")
    write(tmp, "bot/eng.py", "x = 1\n")
    write(tmp, "bot/test_bot.py", "")
    write(tmp, "web/app.py", "x = 1\n")
    write(tmp, "deploy/run-app.sh", "")
    return tmp


def test_plan_restarts():
    print("\n[quyet dinh restart]")
    tmp = _fake_repo()
    saved = (d.pm2_list, d.ecosystem_defs, d.reflog_entries)
    try:
        c1 = commit(tmp, "c1")
        apps = [{"name": "bot", "cwd": "bot", "entry": "bot.py",
                 "search_path": ["bot"], "confirm": True},
                {"name": "web", "cwd": "web", "entry": "app.py",
                 "search_path": ["web"]}]
        procs = {}
        defs = {"bot": {"args": ["x"]}, "web": {"args": ["y"]}}
        entries = []
        d.pm2_list = lambda cfg: procs
        d.ecosystem_defs = lambda cfg: defs
        d.reflog_entries = lambda: list(entries)

        def plan(head, deps=()):
            with use_root(tmp):
                return {i["app"]["name"]: i for i in
                        d.plan_restarts({}, apps, head, set(deps))}

        entries[:] = [(1000, c1)]
        procs.update(bot={"status": "online", "uptime_ms": 2000 * 1000},
                     web={"status": "online", "uptime_ms": 2000 * 1000})
        p = plan(c1)
        check("dang chay HEAD -> khong restart",
              p["bot"]["action"] == "skip" and p["web"]["action"] == "skip",
              {k: v["reasons"] for k, v in p.items()})

        write(tmp, "bot/eng.py", "x = 2\n")
        write(tmp, "bot/test_bot.py", "# doi test\n")
        c2 = commit(tmp, "c2")
        entries.insert(0, (3000, c2))
        p = plan(c2)
        check("module bot import doi -> restart bot",
              p["bot"]["action"] == "restart"
              and "bot/eng.py" in p["bot"]["reasons"][0], p["bot"]["reasons"])
        check("web khong lien quan -> khong restart (dang chay c1)",
              p["web"]["action"] == "skip" and p["web"]["running"] == c1)

        write(tmp, "bot/test_bot.py", "# chi test\n")
        c3 = commit(tmp, "c3")
        entries.insert(0, (4000, c3))
        procs["bot"]["uptime_ms"] = 3500 * 1000       # da restart o c2
        p = plan(c3)
        check("chi doi file test -> khong restart",
              p["bot"]["action"] == "skip", p["bot"]["reasons"])

        procs["web"]["uptime_ms"] = 500 * 1000         # truoc moc reflog
        p = plan(c3)
        check("khong xac dinh commit dang chay -> restart (than trong)",
              p["web"]["action"] == "restart" and "khong xac dinh" in
              p["web"]["reasons"][0], p["web"]["reasons"])
        procs["web"]["uptime_ms"] = 4500 * 1000

        p = plan(c3, deps={"web"})
        check("thu vien doi -> restart", p["web"]["action"] == "restart")

        defs["web"] = {"args": ["y", "--new"]}
        p = plan(c3)
        check("cau hinh pm2 doi -> restart + tao lai",
              p["web"]["action"] == "restart" and p["web"]["recreate"])
        defs["web"] = {"args": ["y"]}
        plan(c3)

        procs["bot"] = {"status": "stopped", "uptime_ms": 0, "exit_code": 0}
        procs.pop("web")
        p = plan(c3)
        check("app stopped -> khong tu start",
              p["bot"]["action"] == "stopped")
        check("app chua co trong pm2 -> missing",
              p["web"]["action"] == "missing")
    finally:
        d.pm2_list, d.ecosystem_defs, d.reflog_entries = saved
        shutil.rmtree(tmp)


def test_reflog_real():
    print("\n[reflog that]")
    tmp = _fake_repo()
    try:
        c1 = commit(tmp, "c1")
        with use_root(tmp):
            entries = d.reflog_entries()
        check("doc duoc reflog (ts, sha)", entries and entries[0][1] == c1
              and entries[0][0] > 1500000000, entries)
    finally:
        shutil.rmtree(tmp)


def test_step_pull():
    print("\n[keo code: chi fast-forward]")
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, "src")
        git_init(src)
        write(src, "a.txt", "1\n")
        commit(src, "c1")
        remote = os.path.join(tmp, "remote.git")
        sh(["git", "clone", "-q", "--bare", src, remote], tmp)
        vps = os.path.join(tmp, "vps")
        sh(["git", "clone", "-q", remote, vps], tmp)
        sh(["git", "config", "user.email", "t@t"], vps)
        sh(["git", "config", "user.name", "t"], vps)
        dev = os.path.join(tmp, "dev")
        sh(["git", "clone", "-q", remote, dev], tmp)
        sh(["git", "config", "user.email", "t@t"], dev)
        sh(["git", "config", "user.name", "t"], dev)
        cfg = {"DEPLOY_REMOTE": "origin"}
        with use_root(vps):
            old, new, pulled = d.step_pull(cfg, "main", False)
            check("khong co code moi", old == new and not pulled)
            write(dev, "a.txt", "2\n")
            c2 = commit(dev, "c2")
            sh(["git", "push", "-q", "origin", "main"], dev)
            old, new, pulled = d.step_pull(cfg, "main", True)
            check("--dry-run: thay commit moi nhung khong pull",
                  new == c2 and not pulled
                  and sh(["git", "rev-parse", "HEAD"], vps) == old)
            old, new, pulled = d.step_pull(cfg, "main", False)
            check("fast-forward", pulled and new == c2
                  and sh(["git", "rev-parse", "HEAD"], vps) == c2)
            write(vps, "a.txt", "sua tay\n")
            try:
                d.step_pull(cfg, "main", False)
                check("working tree co sua doi -> dung", False)
            except d.DeployError as e:
                check("working tree co sua doi -> dung", "a.txt" in str(e))
            sh(["git", "checkout", "-q", "a.txt"], vps)
            write(vps, "b.txt", "local\n")
            commit(vps, "local")
            old, new, pulled = d.step_pull(cfg, "main", False)
            check("local co commit chua push -> khong pull", not pulled)
            write(dev, "a.txt", "3\n")
            commit(dev, "c3")
            sh(["git", "push", "-q", "origin", "main"], dev)
            try:
                d.step_pull(cfg, "main", False)
                check("lech nhanh -> dung", False)
            except d.DeployError as e:
                check("lech nhanh -> dung", "diverged" in str(e))
    finally:
        shutil.rmtree(tmp)


def test_pm2_list_normalize():
    print("\n[pm2 jlist]")
    saved = d.pm2
    try:
        data = [
            {"name": "pm2-logrotate", "pid": 1,
             "pm2_env": {"pmx_module": True, "status": "online"}},
            {"name": "a", "pid": 0, "pm2_env": {
                "status": "waiting restart", "exit_code": 0,
                "stop_exit_codes": [0, 78]}},
            {"name": "b", "pid": 0, "pm2_env": {
                "status": "waiting restart", "exit_code": 1,
                "stop_exit_codes": [0, 78]}},
            {"name": "c", "pid": 42, "pm2_env": {
                "status": "online", "pm_uptime": 123, "restart_time": 2}},
        ]

        class R(object):
            stdout = "[PM2] warning in-memory out of date\n" + json.dumps(data)
        d.pm2 = lambda cfg, *a, **k: R()
        res = d.pm2_list({})
        check("bo dong canh bao truoc JSON + tach module",
              res["_modules"] == {"pm2-logrotate"} and "c" in res)
        check("exit 0 + 'waiting restart' (nhan sai cua pm2) -> stopped",
              res["a"]["status"] == "stopped", res["a"])
        check("crash (exit 1) -> van 'waiting restart'",
              res["b"]["status"] == "waiting restart")
        check("truong online", res["c"]["pid"] == 42
              and res["c"]["restarts"] == 2 and res["c"]["uptime_ms"] == 123)
    finally:
        d.pm2 = saved


def test_systemd_env_check():
    print("\n[chuyen tu systemd: kiem tra bien moi truong]")
    tmp = tempfile.mkdtemp()
    try:
        unit_env = os.path.join(tmp, "unit.env")
        with open(unit_env, "w") as f:
            f.write("DATABASE_URL=postgres://x\nFROM_FILE=1\n")
        app_env = os.path.join(tmp, "app.env")
        with open(app_env, "w") as f:
            f.write("DATABASE_URL=postgres://x\n")
        os.chmod(app_env, 0o644)
        unit = {"Environment": 'PYTHONUNBUFFERED=1 BINANCE_API_KEY=k '
                               '"NOTE=co dau cach"',
                "EnvironmentFiles": "%s (ignore_errors=no) "
                                    "-/root/secret.env (ignore_errors=yes)"
                                    % unit_env,
                "ExecStart": "{ path=/x/python ; argv[]=/x/python bot.py ; "
                             "ignore_errors=no }",
                "User": os.environ.get("USER", "")}
        names, unreadable = d.unit_env_names(unit)
        check("doc ten bien tu Environment + EnvironmentFiles",
              {"PYTHONUNBUFFERED", "BINANCE_API_KEY", "NOTE", "DATABASE_URL",
               "FROM_FILE"} <= names, names)
        check("bao file khong doc duoc", unreadable == ["/root/secret.env"],
              unreadable)
        check("ExecStart -> lenh", d.unit_exec(unit) == "/x/python bot.py")
        app = {"python": sys.executable, "env_file": app_env}
        errors, warns = d.check_app_env(app, unit)
        check("thieu bien systemd cap -> loi, liet ke ten (khong gia tri)",
              errors and "BINANCE_API_KEY" in errors[0]
              and "FROM_FILE" in errors[0] and "NOTE" in errors[0]
              and "PYTHONUNBUFFERED" not in errors[0]
              and "postgres" not in errors[0], errors)
        check("canh bao quyen ENV_FILE", any("chmod 600" in w for w in warns),
              warns)
        with open(app_env, "a") as f:
            f.write("BINANCE_API_KEY=k\nFROM_FILE=1\nNOTE=x\n")
        errors, _ = d.check_app_env(app, unit)
        check("du bien -> khong loi", errors == [], errors)
        errors, _ = d.check_app_env({"python": "/khong/co", "env_file": ""},
                                    None)
        check("thieu python -> loi", errors and "python" in errors[0])
    finally:
        shutil.rmtree(tmp)


def test_load_apps():
    print("\n[cau hinh app]")
    cfg = {"APPS": "muse-dashboard muse-binance", "PYTHON": ".venv/bin/python",
           "PYTHON_MUSE_BINANCE": "/opt/py", "ENV_FILE": ".env",
           "ENV_FILE_MUSE_DASHBOARD": ""}
    apps = {a["name"]: a for a in d.load_apps(cfg)}
    check("PYTHON chung + rieng",
          apps["muse-binance"]["python"] == "/opt/py"
          and apps["muse-dashboard"]["python"].endswith(".venv/bin/python"))
    check("ENV_FILE rieng rong -> dung ENV_FILE chung",
          apps["muse-dashboard"]["env_file"].endswith(".env"))
    try:
        d.load_apps(dict(cfg, APPS="muse-khong-co"))
        check("app la -> loi", False)
    except d.DeployError:
        check("app la -> loi", True)
    only = d.load_apps(cfg, ["muse-binance"])
    check("--only", [a["name"] for a in only] == ["muse-binance"])
    with open(os.path.join(HERE, "apps.json")) as f:
        spec = json.load(f)["apps"]
    check("app tien that phai confirm",
          spec["muse-binance"]["confirm"] and spec["muse-live-trader"]["confirm"]
          and not spec["muse-dashboard"]["confirm"])
    repo = os.path.dirname(HERE)
    check("entry cua moi app ton tai",
          all(os.path.exists(os.path.join(repo, s["cwd"], s["entry"]))
              for s in spec.values()))


def main():
    test_parse_env_and_run_app()
    test_ecosystem()
    test_commit_at()
    test_import_closure()
    test_plan_restarts()
    test_reflog_real()
    test_step_pull()
    test_pm2_list_normalize()
    test_systemd_env_check()
    test_load_apps()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
