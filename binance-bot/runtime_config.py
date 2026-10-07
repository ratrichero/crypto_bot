"""Config runtime: nap tu DB 1 lan vao cache (dict CFG dung chung), kiem tra
version moi moi `poll_seconds` va ap dung nong, khong can restart.

Thu tu uu tien khi khoi dong:
  1. Version moi nhat trong DB (bang bot_config_versions).
  2. File cache last-known-good (runtime_config.cache.json) neu DB loi.
  3. config.json (gia tri file + default schema).
DB chua co version nao -> bot tu seed version 1 tu config DANG chay (sau khi
ap cache/file); seed loi/DB loi -> poll() thu lai moi 60s.

Code bot luon doc tu CFG (cung 1 dict voi engine) nen ap dung = ghi tai cho.
Config sai -> giu ban cu, ghi status=error vao bot_config_applied de
dashboard hien thi.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time
from typing import Any, Callable, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
_DB_DIR = os.path.join(os.path.dirname(HERE), "db")
if _DB_DIR not in sys.path:
    sys.path.insert(0, _DB_DIR)

import bot_config  # noqa: E402


class DBLink:
    """Ket noi Postgres luoi + tu ket noi lai. Khong co DATABASE_URL -> None."""

    def __init__(self, url: Optional[str] = None,
                 connect: Optional[Callable[[], Any]] = None, log=print):
        self.url = url if url is not None else os.environ.get("DATABASE_URL")
        self._connect = connect
        self._conn = None
        self.log = log
        self._last_fail = 0.0
        self.last_error: Optional[str] = None

    def get(self):
        if self._conn is not None and not getattr(self._conn, "closed", False):
            return self._conn
        if self._connect is None and not self.url:
            self.last_error = "không có DATABASE_URL trong env của bot"
            return None
        if time.time() - self._last_fail < 30:
            return None                  # khong spam ket noi khi DB chet
        try:
            if self._connect is not None:
                self._conn = self._connect()
            else:
                import psycopg
                # timeout ngan: vong lap bot khong duoc treo vi DB
                self._conn = psycopg.connect(
                    self.url, autocommit=True, connect_timeout=5,
                    options="-c statement_timeout=5000")
            self.last_error = None
            return self._conn
        except Exception as e:
            self._last_fail = time.time()
            self.last_error = str(e)[:300] or e.__class__.__name__
            self.log("DB warning: khong ket noi duoc (%s)" % str(e)[:200])
            return None

    def status(self) -> str:
        """'ok' | 'chua ket noi' | mo ta loi (cho state.json -> dashboard)."""
        if self._conn is not None and not getattr(self._conn, "closed",
                                                  False):
            return "ok"
        if self._connect is None and not self.url:
            return "lỗi: không có DATABASE_URL trong env của bot"
        if self.last_error:
            return "lỗi: %s" % self.last_error
        return "chưa kết nối"

    def reset(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None


class RuntimeConfig:
    def __init__(self, cfg: dict, bot: str = bot_config.BOT_BINANCE,
                 db: Optional[DBLink] = None, log=print,
                 cache_path: Optional[str] = None,
                 poll_seconds: float = 10.0, clock=time.time):
        self.cfg = cfg                    # dict dung chung voi engine
        self.base = copy.deepcopy(cfg)    # gia tri file (config.json)
        self.bot = bot
        self.db = db or DBLink(log=log)
        self.log = log
        self.cache_path = cache_path or os.path.join(
            HERE, "runtime_config.cache.json")
        self.poll_seconds = float(poll_seconds)
        self.clock = clock
        self.version: Optional[int] = None
        self.source = "file"
        self.overrides: Dict[str, Any] = {}
        self.last_error: Optional[str] = None
        self._last_poll = 0.0
        self._rejected: Optional[int] = None
        self.seed_retry_seconds = 60.0
        self._last_seed_try = -1e18
        self._seed_error: Optional[str] = None

    # ------------------------------------------------------------ apply
    def _apply(self, flat: Dict[str, Any], version: Optional[int],
               source: str) -> bool:
        clean, errors = bot_config.validate(flat)
        if errors:
            self.last_error = "; ".join(errors)
            self.log("CONFIG version %s KHONG hop le -> giu ban cu: %s"
                     % (version, self.last_error))
            self._record(version, "error", self.last_error)
            return False
        changed = bot_config.apply(self.cfg, clean)
        old = self.version
        self.overrides = clean
        self.version = version
        self.source = source
        self.last_error = None
        if changed or old != version:
            self.log("CONFIG ap dung version %s (nguon %s), doi: %s"
                     % (version, source,
                        ", ".join("%s=%s" % (k, clean[k]) for k in changed)
                        or "khong co"))
        self._write_cache(clean, version)
        if version is None and self._seed_error:
            self._record(None, "error", self._seed_error)
        else:
            self._record(version, "ok")
        return True

    def _record(self, version, status, error=None):
        conn = self.db.get()
        if conn is None:
            return
        try:
            bot_config.record_applied(conn, version, status, error, self.bot)
        except Exception as e:
            self.log("CONFIG warning: khong ghi duoc trang thai ap dung: %s"
                     % str(e)[:200])
            self.db.reset()

    def _write_cache(self, flat, version):
        try:
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"version": version, "config": flat}, f)
            os.replace(tmp, self.cache_path)
        except Exception as e:
            self.log("CONFIG warning: khong ghi duoc cache: %s" % e)

    def _read_cache(self):
        try:
            with open(self.cache_path) as f:
                data = json.load(f)
            return data.get("version"), data.get("config") or {}
        except FileNotFoundError:
            return None, None
        except Exception as e:
            self.log("CONFIG warning: cache hong (%s) -> bo qua" % e)
            return None, None

    # ------------------------------------------------------------- seed
    def _seed_from_running(self) -> bool:
        """DB chua co version nao -> tao version dau tien tu config bot DANG
        chay (file + cache da ap). Truoc day seed tu config.json tho TRUOC
        khi ap cache, chi thu 1 lan luc start: config.json cu vi pham rang
        buoc moi hoac DB loi luc start -> bot chay mai 'version None (nguon
        cache)'. Gio poll() thu lai moi seed_retry_seconds. True neu da seed
        (hoac da co version -> poll se ap dung)."""
        self._last_seed_try = self.clock()
        conn = self.db.get()
        if conn is None:
            return False
        try:
            bot_config.ensure_tables(conn)
            if bot_config.latest_version(conn, self.bot) is not None:
                return True
            seed = bot_config.extract(self.cfg)
            try:
                version = bot_config.save_version(
                    conn, seed, author="bot-seed",
                    note="seed tu config dang chay (nguon %s)" % self.source,
                    bot=self.bot)
            except ValueError as e:
                msg = ("Khong tao duoc version dau tien - config dang chay "
                       "khong hop le: %s. Sua tren dashboard roi Luu de tao "
                       "version." % e)
                if msg != self._seed_error:
                    self.log("CONFIG warning: %s" % msg)
                self._seed_error = msg
                self._record(None, "error", msg)
                return False
            row = bot_config.load_version(conn, self.bot, version)
        except Exception as e:
            msg = "seed DB loi: %s" % str(e)[:200]
            if msg != self._seed_error:
                self.log("CONFIG warning: %s" % msg)
            self._seed_error = msg
            self.db.reset()
            return False
        self._seed_error = None
        self.log("CONFIG DB chua co version -> seed version %s tu config dang "
                 "chay (nguon %s)" % (version, self.source))
        return row is not None and self._apply(row["config"], row["version"],
                                               "db")

    # ------------------------------------------------------------ start
    def start(self) -> None:
        self._last_poll = self.clock()   # vua nap xong, poll sau poll_seconds
        need_seed = False
        conn = self.db.get()
        if conn is not None:
            try:
                bot_config.ensure_tables(conn)
                row = bot_config.load_version(conn, self.bot)
                if row is None:
                    need_seed = True
                elif self._apply(row["config"], row["version"], "db"):
                    return
            except Exception as e:
                self.log("CONFIG warning: doc DB loi (%s) -> dung cache/file"
                         % str(e)[:200])
                self.db.reset()
        version, flat = self._read_cache()
        if not (flat and self._apply(flat, version, "cache")):
            # Chi config.json: van validate de bat loi go tay.
            self._apply(bot_config.extract(self.cfg), None, "file")
        if need_seed:
            self._seed_from_running()
        # DB loi luc start -> poll() se thu seed lai khi DB song.

    # ------------------------------------------------------------- poll
    def poll(self, force: bool = False) -> bool:
        """Kiem tra version moi; True neu vua ap dung version moi."""
        now = self.clock()
        if not force and now - self._last_poll < self.poll_seconds:
            return False
        self._last_poll = now
        conn = self.db.get()
        if conn is None:
            return False
        try:
            latest = bot_config.latest_version(conn, self.bot)
            if latest is None:
                # DB chua co version nao -> seed tu config DANG chay, KE CA
                # khi cache con so version cu (DB moi / doi DB / DB chet luc
                # start). Truoc day chi seed khi version None -> bot nghi
                # minh o version cu, dashboard mai 'Chua co version'.
                if force or now - self._last_seed_try >= self.seed_retry_seconds:
                    if self.version is not None and not self._seed_error:
                        self.log("CONFIG DB chua co version nao nhung bot dang "
                                 "o version %s (nguon %s) -> seed lai tu "
                                 "config dang chay" % (self.version,
                                                       self.source))
                    return self._seed_from_running()
                return False
            if latest == self.version:
                return False
            row = bot_config.load_version(conn, self.bot, latest)
        except Exception as e:
            self.log("CONFIG warning: poll DB loi: %s" % str(e)[:200])
            self.db.reset()
            return False
        if row is None or self._rejected == latest:
            return False                 # da tu choi version nay roi
        ok = self._apply(row["config"], row["version"], "db")
        self._rejected = None if ok else latest
        return ok

    # ------------------------------------------------------ file reload
    def reload_file(self, new_cfg: dict) -> None:
        """config.json doi (reload dinh ky): cap nhat phan file, giu DB
        override (truoc day CFG.clear() xoa moi thay doi)."""
        self.base = copy.deepcopy(new_cfg)
        self.cfg.clear()
        self.cfg.update(copy.deepcopy(new_cfg))
        if self.overrides:
            bot_config.apply(self.cfg, self.overrides)

    def status(self) -> dict:
        return {"version": self.version, "source": self.source,
                "error": self.last_error or self._seed_error,
                "db": self.db.status() if hasattr(self.db, "status")
                else None}
