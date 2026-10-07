"""Config runtime: nap tu DB 1 lan vao cache (dict CFG dung chung), kiem tra
version moi moi `poll_seconds` va ap dung nong, khong can restart.

Thu tu uu tien khi khoi dong:
  1. Version moi nhat trong DB (bang bot_config_versions).
  2. File cache last-known-good (runtime_config.cache.json) neu DB loi.
  3. config.json (gia tri file + default schema).
DB chua co version nao -> bot tu seed version 1 tu config hien hanh.

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

    def get(self):
        if self._conn is not None and not getattr(self._conn, "closed", False):
            return self._conn
        if self._connect is None and not self.url:
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
            return self._conn
        except Exception as e:
            self._last_fail = time.time()
            self.log("DB warning: khong ket noi duoc (%s)" % str(e)[:200])
            return None

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

    # ------------------------------------------------------------ start
    def start(self) -> None:
        self._last_poll = self.clock()   # vua nap xong, poll sau poll_seconds
        conn = self.db.get()
        if conn is not None:
            try:
                bot_config.ensure_tables(conn)
                row = bot_config.load_version(conn, self.bot)
                if row is None:
                    seed = bot_config.extract(self.cfg)
                    try:
                        version = bot_config.save_version(
                            conn, seed, author="bot-seed",
                            note="seed tu config.json + default schema",
                            bot=self.bot)
                    except ValueError as e:
                        # config.json co gia tri ngoai bien schema: khong seed
                        # (dashboard se tao version dau tien), chay theo file.
                        self.log("CONFIG warning: khong seed duoc (%s)" % e)
                        self._record(None, "error", "seed: %s" % e)
                        version = None
                    if version is not None:
                        self.log("CONFIG DB chua co version -> seed version %s"
                                 % version)
                        row = bot_config.load_version(conn, self.bot, version)
                if row is not None and self._apply(row["config"],
                                                   row["version"], "db"):
                    return
            except Exception as e:
                self.log("CONFIG warning: doc DB loi (%s) -> dung cache/file"
                         % str(e)[:200])
                self.db.reset()
        version, flat = self._read_cache()
        if flat and self._apply(flat, version, "cache"):
            return
        # Chi config.json: van validate de bat loi go tay.
        self._apply(bot_config.extract(self.cfg), None, "file")

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
            if latest is None or latest == self.version:
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
                "error": self.last_error}
