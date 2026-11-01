// Plain helpers for the desktop shell: no Electron imports, so they run under `node --test`.
"use strict";

const fs = require("fs");
const net = require("net");
const path = require("path");

const KEY_FILE = "model-key.enc";  // encrypted bytes from safeStorage, never plaintext
const KEY_MIN = 8;                 // same limits as eco_server.valid_key
const KEY_MAX = 512;
const NOTIFY_TITLE_MAX = 80;
const NOTIFY_BODY_MAX = 240;

// Runs eco_server.py as __main__ with a watchdog: when the desktop app goes away for any
// reason (even a hard kill), the stdin pipe closes and the backend exits too.
const BACKEND_BOOT =
  "import os, runpy, sys, threading\n" +
  "threading.Thread(target=lambda: (sys.stdin.buffer.read(), os._exit(0)), daemon=True).start()\n" +
  "sys.argv = ['eco_server.py', '--port', sys.argv[1]]\n" +
  "runpy.run_path('eco_server.py', run_name='__main__')\n";

/** A free TCP port on 127.0.0.1, chosen by the OS. */
function pickFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

/** Python executables to try, in order: the repo's .venv, then python on PATH. */
function pythonCandidates(repoRoot, platform = process.platform, exists = fs.existsSync) {
  const venv = platform === "win32"
    ? path.join(repoRoot, ".venv", "Scripts", "python.exe")
    : path.join(repoRoot, ".venv", "bin", "python");
  const onPath = platform === "win32" ? "python" : "python3";
  return exists(venv) ? [venv, onPath] : [onPath];
}

function dataDir(userData) {
  return path.join(userData, "eco");
}

function keyFilePath(userData) {
  return path.join(userData, KEY_FILE);
}

/** The backend's environment: no inherited key or cloud flag; cloud allowed only with a stored key. */
function backendEnv(baseEnv, userData, allowCloud) {
  const env = { ...baseEnv };
  delete env.MODEL_API_KEY;
  delete env.ALLOW_CLOUD;
  env.ECO_DATA_DIR = dataDir(userData);
  env.PYTHONUNBUFFERED = "1";
  env.PYTHONIOENCODING = "utf-8";
  if (allowCloud) env.ALLOW_CLOUD = "1";
  return env;
}

function validKey(key) {
  return typeof key === "string" && key.length >= KEY_MIN && key.length <= KEY_MAX
    && !/[\s\x00-\x1f\x7f]/.test(key);
}

/**
 * Store the key encrypted. `crypto` is Electron's safeStorage (or a test double).
 * Returns {stored: true} or {stored: false, reason}; nothing is written unless encryption works.
 */
function saveKeyFile(file, key, crypto) {
  if (!validKey(key)) return { stored: false, reason: "not stored: the key is not 8-512 printable characters" };
  if (!crypto.isEncryptionAvailable()) return { stored: false, reason: "not stored: encryption is not available on this computer" };
  const data = crypto.encryptString(key);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = file + ".tmp";
  fs.writeFileSync(tmp, data, { mode: 0o600 });
  fs.renameSync(tmp, file);
  return { stored: true };
}

/** The stored key, or null if there is none or it cannot be decrypted. */
function loadKeyFile(file, crypto) {
  let data;
  try { data = fs.readFileSync(file); } catch (e) { return null; }
  if (!crypto.isEncryptionAvailable()) return null;
  try {
    const key = crypto.decryptString(data);
    return validKey(key) ? key : null;
  } catch (e) {
    return null;
  }
}

function removeKeyFile(file) {
  fs.rmSync(file, { force: true });
  fs.rmSync(file + ".tmp", { force: true });
  return { removed: true };
}

/** Notification text: strings only, control characters flattened, long text cut with an ellipsis. */
function cleanText(value, max) {
  if (typeof value !== "string") return "";
  const text = value.replace(/[\x00-\x1f\x7f]+/g, " ").replace(/\s+/g, " ").trim();
  return text.length > max ? text.slice(0, max - 1).trimEnd() + "…" : text;
}

function notification(title, body) {
  const t = cleanText(title, NOTIFY_TITLE_MAX);
  const b = cleanText(body, NOTIFY_BODY_MAX);
  return t || b ? { title: t || "Eco mode", body: b } : null;
}

/** Where a navigation or new-window request may go. */
function linkAction(url, origin) {
  let u;
  try { u = new URL(url); } catch (e) { return "block"; }
  if (u.origin === origin) return "allow";
  if (u.protocol === "https:") return "external";
  return "block";
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** A plain self-contained page for "starting" and "could not start". */
function statusPage(title, detail) {
  return "<!doctype html><html><head><meta charset='utf-8'>" +
    "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\">" +
    "<title>Eco mode</title><style>body{font:18px/1.5 system-ui,sans-serif;margin:48px;color:#1d1d1b;background:#f7f6f1}" +
    "h1{font-size:28px}pre{white-space:pre-wrap;background:#fff;border:1px solid #ccc;padding:16px;border-radius:8px;font-size:14px}</style>" +
    `</head><body><h1>${escapeHtml(title)}</h1>` + (detail ? `<pre>${escapeHtml(detail)}</pre>` : "") + "</body></html>";
}

module.exports = {
  BACKEND_BOOT, KEY_FILE, NOTIFY_TITLE_MAX, NOTIFY_BODY_MAX,
  pickFreePort, pythonCandidates, dataDir, keyFilePath, backendEnv, validKey,
  saveKeyFile, loadKeyFile, removeKeyFile, cleanText, notification, linkAction, escapeHtml, statusPage,
};
