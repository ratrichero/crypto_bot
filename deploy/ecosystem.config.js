// pm2 ecosystem - sinh tu deploy/apps.json + deploy/deploy.env.
// Dung qua deploy.py (git up / git up setup); chay tay:
//   pm2 start deploy/ecosystem.config.js --only muse-dashboard
'use strict';
const fs = require('fs');
const path = require('path');

const DEPLOY = __dirname;
const ROOT = path.dirname(DEPLOY);

// Cung ngu nghia voi parse_env_file trong deploy.py va run-app.sh.
function parseEnvFile(file) {
  const out = {};
  if (!fs.existsSync(file)) return out;
  for (let line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    line = line.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    let key = line.slice(0, line.indexOf('=')).trim().replace(/^export\s+/, '');
    let val = line.slice(line.indexOf('=') + 1).trim();
    if (val.length >= 2 && ((val[0] === '"' && val.endsWith('"')) ||
                            (val[0] === "'" && val.endsWith("'")))) {
      val = val.slice(1, -1);
    }
    if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) out[key] = val;
  }
  return out;
}

const cfg = parseEnvFile(path.join(DEPLOY, 'deploy.env'));
const spec = JSON.parse(fs.readFileSync(path.join(DEPLOY, 'apps.json'), 'utf8')).apps;

const appKey = (name) => name.toUpperCase().replace(/[^A-Z0-9]/g, '_');
const pick = (key, name) => {
  const own = cfg[key + '_' + appKey(name)];
  return own !== undefined && own !== '' ? own : (cfg[key] || '');
};
const abs = (p) => (p ? path.resolve(ROOT, p) : p);
const fill = (s) => String(s).replace(/\{([A-Z0-9_]+)\}/g, (_, k) => cfg[k] || '');
const splitArgs = (s) => (s || '').match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) || [];

const names = (cfg.APPS || Object.keys(spec).join(' ')).split(/\s+/).filter(Boolean);

module.exports = {
  apps: names.filter((n) => spec[n]).map((name) => {
    const s = spec[name];
    const pyArgs = (s.python_args || [s.entry]).map(fill)
      .concat(s.extra_args_var ? splitArgs(cfg[s.extra_args_var])
        .map((a) => a.replace(/^["']|["']$/g, '')) : []);
    const envFile = pick('ENV_FILE', name);
    return {
      name,
      cwd: ROOT,
      script: path.join(DEPLOY, 'run-app.sh'),
      interpreter: '/bin/bash',
      args: [envFile ? abs(envFile) : '-', abs(s.cwd), abs(pick('PYTHON', name))]
        .concat(pyArgs),
      autorestart: true,
      // 0 = bot tu dung (file STOP, safety circuit 429/418, start loi):
      //     KHONG restart, neu khong pm2 se khoi dong lai lien tuc va spam
      //     API Binance. 78 = thieu python/env (run-app.sh).
      stop_exit_codes: [0, 78],
      exp_backoff_restart_delay: 2000,
      min_uptime: 10000,
      kill_timeout: s.kill_timeout || 10000,
      watch: false,
      merge_logs: true,
      env: { DEPLOY_APP: name },
    };
  }),
};
