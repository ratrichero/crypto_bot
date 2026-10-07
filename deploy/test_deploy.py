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
        with open(os.path.join(dep, "deploy.env"), "a") as f:
            f.write("\nAPPS=muse-dashboard muse-binance\nDASHBOARD_PORT=9999\n"
                    "PYTHON_MUSE_BINANCE=/opt/py/bin/python\n"
                    "ENV_FILE_MUSE_DASHBOARD=\nDASHBOARD_ARGS=--a 1 \"--b x\"\n")
        with open(os.path.join(dep, "deploy.local.env"), "w") as f:
            f.write("APPS=muse-radar\nDASHBOARD_PORT=1111\n")  # bi bo qua
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
                 "search_path": ["bot"], "live": True},
                {"name": "web", "cwd": "web", "entry": "app.py",
                 "search_path": ["web"]}]
        procs = {}
        defs = {"bot": {"args": ["x"]}, "web": {"args": ["y"]}}
        entries = []
        d.pm2_list = lambda cfg: procs
        d.ecosystem_defs = lambda cfg: defs
        d.reflog_entries = lambda: list(entries)

        for a in apps:
            a["python"] = "/py/" + a["name"]

        def plan(head):
            with use_root(tmp):
                return {i["app"]["name"]: i for i in
                        d.plan_restarts({}, apps, head)}

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

        with use_root(tmp):
            d.mark_changed("pip:/py/web", 5000)        # sau khi web start
            d.mark_changed("pip:/py/bot", 1000)        # truoc khi bot start
        p = plan(c3)
        check("thu vien doi SAU khi app start -> restart",
              p["web"]["action"] == "restart"
              and "thu vien" in p["web"]["reasons"][0], p["web"]["reasons"])
        check("thu vien doi TRUOC khi app start -> khong restart",
              p["bot"]["action"] == "skip", p["bot"]["reasons"])
        p = plan(c3)
        check("restart bi bo qua -> lan git up sau van doi restart",
              p["web"]["action"] == "restart")
        procs["web"]["uptime_ms"] = 6000 * 1000         # da restart
        p = plan(c3)
        check("da restart sau moc -> het can restart",
              p["web"]["action"] == "skip", p["web"]["reasons"])
        with use_root(tmp):
            d.mark_changed("build:web", 7000)
        p = plan(c3)
        check("build thu muc cua app doi -> restart app do",
              p["web"]["action"] == "restart"
              and p["bot"]["action"] == "skip")
        procs["web"]["uptime_ms"] = 8000 * 1000

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
    check("app tien that duoc danh dau live",
          spec["muse-binance"]["live"] and spec["muse-live-trader"]["live"]
          and not spec["muse-dashboard"]["live"])
    check("apps.json khong con khoa confirm",
          not any("confirm" in v for v in spec.values()))
    repo = os.path.dirname(HERE)
    check("entry cua moi app ton tai",
          all(os.path.exists(os.path.join(repo, s["cwd"], s["entry"]))
              for s in spec.values()))


FAKE_PY = """#!/bin/bash
# python gia: -m pip freeze|install. Trang thai venv trong $DIR/freeze.txt
DIR="$(dirname "$0")"
[ "$1 $2" = "-m pip" ] || exit 1
case "$3" in
  freeze) cat "$DIR/freeze.txt" ;;
  install)
    echo install >> "$DIR/calls.txt"
    mode="$(cat "$DIR/mode.txt" 2>/dev/null)"
    case "$mode" in
      upgrade) echo "newlib==2" >> "$DIR/freeze.txt" ;;
      partial) echo "half==1" >> "$DIR/freeze.txt"; exit 1 ;;
      fail) exit 1 ;;
    esac ;;
esac
"""


def test_step_deps():
    print("\n[thu vien: cai khi doi, danh dau moc]")
    tmp = tempfile.mkdtemp()
    try:
        venv = os.path.join(tmp, "venv")
        os.makedirs(venv)
        py = os.path.join(venv, "python")
        with open(py, "w") as f:
            f.write(FAKE_PY)
        os.chmod(py, 0o755)
        write(venv, "freeze.txt", "a==1\n")
        write(tmp, "app/requirements.txt", "a\n")
        apps = [{"name": "x", "python": py, "cwd": "app",
                 "requirements": ["app/requirements.txt"]}]

        def calls():
            try:
                with open(os.path.join(venv, "calls.txt")) as f:
                    return len(f.read().split())
            except IOError:
                return 0

        def changed():
            with use_root(tmp):
                return d.env_changes_for(apps[0])

        with use_root(tmp):
            res = d.step_deps({}, apps, False, False)
        check("lan dau: chay pip install, venv khong doi -> khong danh dau",
              calls() == 1 and res == set() and changed() == [])
        with use_root(tmp):
            d.step_deps({}, apps, False, False)
        check("requirements khong doi -> bo qua pip", calls() == 1)

        write(tmp, "app/requirements.txt", "a\nnewlib\n")
        write(venv, "mode.txt", "upgrade")
        with use_root(tmp):
            res = d.step_deps({}, apps, False, False)
        check("requirements doi + venv doi -> danh dau moc",
              calls() == 2 and res == {"x"} and changed()
              and changed()[0][1] == "thu vien Python", changed())

        write(tmp, "app/requirements.txt", "a\nnewlib\nhalf\n")
        write(venv, "mode.txt", "partial")
        before = changed()[0][0]
        time_mark = None
        with use_root(tmp):
            try:
                d.step_deps({}, apps, False, False)
            except d.DeployError:
                time_mark = changed()[0][0]
        check("pip loi giua chung nhung venv da doi -> van danh dau moc",
              time_mark is not None and time_mark >= before)
        write(venv, "mode.txt", "")
        with use_root(tmp):
            d.step_deps({}, apps, False, False)
        check("pip loi -> khong ghi stamp, lan sau cai lai", calls() == 4)

        with use_root(tmp):
            d.step_deps({}, apps, False, True)
        check("--force-deps -> cai lai du requirements khong doi",
              calls() == 5)
    finally:
        shutil.rmtree(tmp)


def test_step_migrate():
    print("\n[migrate DB (Postgres that qua pgserver)]")
    try:
        import pgserver
        import psycopg
    except ImportError:
        print("  SKIP: can pgserver + psycopg (chay bang python cua venv)")
        return
    tmp = tempfile.mkdtemp()
    srv = pgserver.get_server(os.path.join(tmp, "pg"), cleanup_mode="stop")
    try:
        with psycopg.connect(srv.get_uri(), autocommit=True) as c:
            c.execute("ALTER USER CURRENT_USER WITH PASSWORD 'bimat123'")
        base = srv.get_uri()
        write(tmp, ".env", "DATABASE_URL=%s\n" % base)
        cfg = {"MIGRATE_SQL": "db/s.sql", "ENV_FILE": ".env",
               "PYTHON": sys.executable}
        sql_v1 = ("CREATE TABLE IF NOT EXISTS t (id INT PRIMARY KEY);\n"
                  "CREATE INDEX IF NOT EXISTS t_id ON t (id);\n")
        write(tmp, "db/s.sql", sql_v1)

        def cols():
            with psycopg.connect(base) as c:
                return sorted(r[0] for r in c.execute(
                    "select column_name from information_schema.columns "
                    "where table_name='t'"))

        def tables():
            with psycopg.connect(base) as c:
                return sorted(r[0] for r in c.execute(
                    "select tablename from pg_tables where schemaname="
                    "'public'"))

        with use_root(tmp):
            check("MIGRATE_SQL rong -> tat",
                  d.step_migrate(dict(cfg, MIGRATE_SQL=""), False) == "off")
            check("--dry-run: bao can ap dung, khong dong DB",
                  d.step_migrate(cfg, True) == "pending"
                  and "t" not in tables())
            check("lan dau: ap dung", d.step_migrate(cfg, False) == "applied"
                  and cols() == ["id"])
            check("khong doi -> bo qua", d.step_migrate(cfg, False) == "skip")
            check("force -> chay lai (idempotent)",
                  d.step_migrate(cfg, False, force=True) == "applied")
            write(tmp, "db/s.sql", sql_v1 + "ALTER TABLE t ADD COLUMN IF NOT "
                  "EXISTS fee DOUBLE PRECISION;\n")
            check("file doi -> ap dung, co cot moi",
                  d.step_migrate(cfg, False) == "applied"
                  and cols() == ["fee", "id"])
            write(tmp, "db/s.sql", sql_v1 + "CREATE TABLE IF NOT EXISTS "
                  "moi (x INT);\nSELECT khong_co_cot FROM t;\n")
            err = ""
            try:
                d.step_migrate(cfg, False)
            except d.DeployError as e:
                err = str(e)
            check("SQL loi -> DeployError", "LOI" in err, err[:200])
            check("SQL loi -> rollback ca file (khong tao bang moi)",
                  "moi" not in tables(), tables())
            check("loi khong lo URL/mat khau",
                  "bimat123" not in err and base not in err, err[-300:])
            try:
                d.step_migrate(cfg, False)
                check("loi -> khong ghi stamp, lan sau chay lai", False)
            except d.DeployError:
                check("loi -> khong ghi stamp, lan sau chay lai", True)
            write(tmp, "db/s.sql", sql_v1)
            d.step_migrate(cfg, False)
            write(tmp, ".env", "DATABASE_URL=%s&application_name=x\n"
                  % base if "?" in base else
                  "DATABASE_URL=%s?application_name=x\n" % base)
            check("doi DB (URL khac) -> ap dung lai",
                  d.step_migrate(cfg, False) == "applied")
            write(tmp, ".env", "OTHER=1\n")
            try:
                d.step_migrate(dict(cfg, MIGRATE_SQL="db/s.sql"), False,
                               force=True)
                check("thieu DATABASE_URL -> loi ro rang", False)
            except d.DeployError as e:
                check("thieu DATABASE_URL -> loi ro rang",
                      "DATABASE_URL" in str(e))
    finally:
        try:
            srv.cleanup()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


def test_migrate_error_blocks_restart():
    print("\n[migrate loi -> khong restart app nao]")
    tmp = _fake_repo()
    commit(tmp, "c1")
    names = ("check_branch", "step_pull", "step_deps", "step_migrate",
             "step_build", "plan_restarts", "start_or_restart", "pm2",
             "acquire_lock", "health_check", "check_syntax",
             "step_switch_branch")
    saved = {n: getattr(d, n) for n in names}
    restarted = []
    try:
        head = sh(["git", "rev-parse", "HEAD"], tmp)
        app = {"name": "muse-dashboard", "live": False}
        d.check_branch = lambda cfg: "main"
        d.step_pull = lambda cfg, b, dry: (head, head, False)
        d.step_deps = lambda *a: set()
        d.step_build = lambda *a: set()
        d.plan_restarts = lambda *a, **k: [
            {"app": app, "info": {}, "reasons": ["code doi"],
             "recreate": False, "action": "restart", "running": head}]
        d.start_or_restart = lambda cfg, a, r, e: restarted.append(a["name"])
        d.health_check = lambda *a: True
        d.check_syntax = lambda a, t: (True, "")
        d.pm2 = lambda *a, **k: None
        d.acquire_lock = lambda: None

        def boom(cfg, dry, force=False):
            raise d.DeployError("migrate db/schema.sql LOI")
        d.step_migrate = boom

        class A(object):
            only = None
            dry_run = no_restart = restart = yes = force_deps = False
            force = False
            resume_from = None
        cfg = {"APPS": "muse-dashboard"}
        with use_root(tmp):
            rc = d.cmd_deploy(A(), cfg)
        check("migrate loi -> khong restart, ma loi 1",
              restarted == [] and rc == 1, (restarted, rc))
        d.step_migrate = lambda cfg, dry, force=False: "applied"
        with use_root(tmp):
            rc = d.cmd_deploy(A(), cfg)
        check("migrate ok -> restart binh thuong",
              restarted == ["muse-dashboard"] and rc == 0, (restarted, rc))
        seen = {}
        d.step_deps = lambda cfg, apps, dry, force: seen.update(deps=force) \
            or set()
        d.step_migrate = lambda cfg, dry, force=False: seen.update(
            mig=force) or "applied"
        d.step_build = lambda apps, dry, force=False: seen.update(
            build=force) or set()
        plan_args = {}

        def fake_plan(cfg, apps, head, force_names=()):
            plan_args["force"] = set(force_names)
            return []
        d.plan_restarts = fake_plan
        a = A()
        a.force = True
        with use_root(tmp):
            d.cmd_deploy(a, cfg)
        check("--force: lam lai thu vien/migrate/build + restart moi app",
              seen == {"deps": True, "mig": True, "build": True}
              and plan_args["force"] == {"muse-dashboard"}, (seen, plan_args))

        # app tien that: khong hoi y/N, khong can --yes (stdin khong phai tty)
        live = {"name": "muse-binance", "live": True, "label": "Binance"}
        d.plan_restarts = lambda *a, **k: [
            {"app": live, "info": {}, "reasons": ["code doi"],
             "recreate": False, "action": "restart", "running": head}]
        d.step_deps = lambda *a: set()
        d.step_migrate = lambda cfg, dry, force=False: "skip"
        d.step_build = lambda *a, **k: set()
        del restarted[:]
        with use_root(tmp):
            rc = d.cmd_deploy(A(), {"APPS": "muse-binance"})
        check("app tien that tu restart, khong hoi y/N",
              restarted == ["muse-binance"] and rc == 0, (restarted, rc))
        src = open(os.path.join(os.path.dirname(os.path.abspath(d.__file__)),
                                "deploy.py")).read()
        check("deploy.py khong con cho nao hoi y/N",
              "input(" not in src and "def confirm" not in src
              and "[y/N]" not in src)

        # --branch: doi nhanh thay cho pull; resume (sau re-exec) khong doi lai
        calls = []
        d.step_switch_branch = lambda cfg, b, dry: calls.append(
            ("switch", b)) or (head, head, True)
        d.step_pull = lambda cfg, b, dry: calls.append(("pull",)) or \
            (head, head, False)
        a = A()
        a.branch = "feature"
        with use_root(tmp):
            rc = d.cmd_deploy(a, cfg)
        check("--branch: goi step_switch_branch, khong pull lai",
              calls == [("switch", "feature")] and rc == 0, (calls, rc))
        del calls[:]
        a.resume_from = head
        with use_root(tmp):
            d.cmd_deploy(a, cfg)
        check("--branch + --resume-from: khong doi nhanh lan 2",
              calls == [], calls)
    finally:
        for n, f in saved.items():
            setattr(d, n, f)
        shutil.rmtree(tmp)


def test_switch_branch():
    print("\n[git up --branch: doi nhanh deploy]")
    tmp = tempfile.mkdtemp()
    try:
        dev = os.path.join(tmp, "dev")
        git_init(dev)
        write(dev, "deploy/deploy.py", "# v1\n")
        write(dev, "app.txt", "main\n")
        commit(dev, "main c1")
        sh(["git", "checkout", "-q", "-b", "old"], dev)
        sh(["git", "rm", "-q", "-r", "deploy"], dev)
        commit(dev, "old: chua co deploy")
        sh(["git", "checkout", "-q", "main"], dev)
        sh(["git", "checkout", "-q", "-b", "feature"], dev)
        write(dev, "app.txt", "feature\n")
        f1 = commit(dev, "feature c1")
        sh(["git", "checkout", "-q", "main"], dev)
        remote = os.path.join(tmp, "remote.git")
        sh(["git", "clone", "-q", "--bare", dev, remote], tmp)
        sh(["git", "remote", "add", "origin", remote], dev)
        sh(["git", "fetch", "-q", "origin"], dev)
        vps = os.path.join(tmp, "vps")
        sh(["git", "clone", "-q", "-b", "main", remote, vps], tmp)
        sh(["git", "config", "user.email", "t@t"], vps)
        sh(["git", "config", "user.name", "t"], vps)
        # file cu Muse tao tren VPS: phai bi bo qua hoan toan
        bad_local = "DEPLOY_BRANCH=sai-nhanh\nPYTHON=/khong/co\n"
        write(vps, "deploy/deploy.local.env", bad_local)
        local_path = os.path.join(vps, "deploy", "deploy.local.env")
        m1 = sh(["git", "rev-parse", "HEAD"], vps)
        cur = lambda: sh(["git", "symbolic-ref", "--short", "HEAD"], vps)
        with use_root(vps):
            cfg = d.load_config()
            check("deploy.local.env bi bo qua (khong doc PYTHON/DEPLOY_BRANCH)",
                  "PYTHON" not in cfg and "DEPLOY_BRANCH" not in cfg
                  and d.check_branch(cfg) == "main", cfg)
            try:
                d.step_switch_branch(cfg, "khong-co", False)
                check("nhanh khong ton tai -> loi", False)
            except d.DeployError as e:
                check("nhanh khong ton tai -> loi", "khong co nhanh" in str(e))
            try:
                d.step_switch_branch(cfg, "old", False)
                check("nhanh chua co deploy/ -> tu choi", False)
            except d.DeployError as e:
                check("nhanh chua co deploy/ -> tu choi",
                      "chua co bo deploy" in str(e) and cur() == "main")
            try:
                d.step_switch_branch(cfg, "--force", False)
                check("ten nhanh dang tuy chon -> tu choi", False)
            except d.DeployError:
                check("ten nhanh dang tuy chon -> tu choi", True)
            old, new, sw = d.step_switch_branch(cfg, "feature", True)
            check("--dry-run: bao commit moi, khong doi nhanh",
                  new == f1 and not sw and cur() == "main")
            write(vps, "app.txt", "sua tay\n")
            try:
                d.step_switch_branch(cfg, "feature", False)
                check("working tree co sua doi -> tu choi", False)
            except d.DeployError:
                check("working tree co sua doi -> tu choi", cur() == "main")
            sh(["git", "checkout", "-q", "app.txt"], vps)

            old, new, sw = d.step_switch_branch(cfg, "feature", False)
            check("doi sang feature: checkout tracking + HEAD moi",
                  sw and old == m1 and new == f1 and cur() == "feature")
            check("khong ghi file cau hinh nao (deploy.local.env giu nguyen)",
                  open(local_path).read() == bad_local)
            cfg = d.load_config()
            check("lan sau git up theo nhanh dang checkout",
                  d.check_branch(cfg) == "feature")

            sh(["git", "checkout", "-q", "main"], dev)
            write(dev, "app.txt", "main v2\n")
            m2 = commit(dev, "main c2")
            sh(["git", "push", "-q", "origin", "main"], dev)
            old, new, sw = d.step_switch_branch(cfg, "main", False)
            check("quay ve main (nhanh local cu) -> fast-forward toi c2",
                  sw and new == m2 and cur() == "main"
                  and d.check_branch(d.load_config()) == "main")
    finally:
        shutil.rmtree(tmp)


def main():
    test_parse_env_and_run_app()
    test_ecosystem()
    test_commit_at()
    test_import_closure()
    test_plan_restarts()
    test_step_deps()
    test_step_migrate()
    test_migrate_error_blocks_restart()
    test_reflog_real()
    test_step_pull()
    test_switch_branch()
    test_pm2_list_normalize()
    test_systemd_env_check()
    test_load_apps()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
