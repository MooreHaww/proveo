// Plain helpers for the desktop shell: no Electron imports, so they run under `node --test`.
"use strict";

const fs = require("fs");
const net = require("net");
const path = require("path");

const KEY_FILE = "model-key.enc";  // encrypted bytes from safeStorage, never plaintext
const KEY_MIN = 8;                 // same limits as eco_server.valid_key
const KEY_MAX = 512;
const KEY_NAME_MAX = 40;           // same limit as settings.clean_key_name, which also checks the wording
const DEFAULT_KEY_NAME = "Default";  // what an old single-key file loads as
const NOTIFY_TITLE_MAX = 80;
const NOTIFY_BODY_MAX = 240;

// Runs eco_server.py as __main__ with a watchdog: when the desktop app goes away for any
// reason (even a hard kill), the stdin pipe closes and the backend exits too.
// The watchdog blocks on a private, non-inheritable copy of the pipe, and fd 0 becomes NUL. On
// Windows a child that inherits a pipe being read here hangs during its startup, so no child
// (the code checker above all) may ever get this pipe.
const BACKEND_BOOT =
  "import os, runpy, sys, threading\n" +
  "watch = os.dup(0)\n" +
  "null = os.open(os.devnull, os.O_RDONLY)\n" +
  "os.dup2(null, 0)\n" +
  "os.close(null)\n" +
  "threading.Thread(target=lambda: (os.read(watch, 1), os._exit(0)), daemon=True).start()\n" +
  "sys.argv = ['eco_server.py'] + sys.argv[1:]\n" +
  "runpy.run_path('eco_server.py', run_name='__main__')\n";

/** The bundled backend inside an installed app: see eco_backend.py and build_backend.py. */
function bundledBackend(resourcesPath) {
  return path.join(resourcesPath, "backend", "eco-backend.exe");
}

/** The server's options: the spend log and verified memory live with the app's data, not in results/. */
function backendArgs(port, userData) {
  const dir = dataDir(userData);
  return ["--port", String(port), "--log", path.join(dir, "eco_log.jsonl"), "--memory", path.join(dir, "eco_memory.jsonl")];
}

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

function validKeyName(name) {
  return typeof name === "string" && name.trim().length >= 1 && name.trim().length <= KEY_NAME_MAX
    && !/[\x00-\x1f\x7f]/.test(name);
}

/** Encrypt text and write it atomically. `crypto` is Electron's safeStorage (or a test double). */
function writeEncrypted(file, text, crypto) {
  if (!crypto.isEncryptionAvailable()) return { stored: false, reason: "not stored: encryption is not available on this computer" };
  const data = crypto.encryptString(text);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = file + ".tmp";
  fs.writeFileSync(tmp, data, { mode: 0o600 });
  fs.renameSync(tmp, file);
  return { stored: true };
}

function readDecrypted(file, crypto) {
  let data;
  try { data = fs.readFileSync(file); } catch (e) { return null; }
  if (!crypto.isEncryptionAvailable()) return null;
  try { return crypto.decryptString(data); } catch (e) { return null; }
}

/**
 * The old file format, one key and nothing else. Only the tests still use this and loadKeyFile,
 * to check that an old file loads. Returns {stored: true} or {stored: false, reason}.
 */
function saveKeyFile(file, key, crypto) {
  if (!validKey(key)) return { stored: false, reason: "not stored: the key is not 8-512 printable characters" };
  return writeEncrypted(file, key, crypto);
}

/** The key in an old single-key file, or null if there is none or it cannot be decrypted. */
function loadKeyFile(file, crypto) {
  const key = readDecrypted(file, crypto);
  return validKey(key) ? key : null;
}

function removeKeyFile(file) {
  fs.rmSync(file, { force: true });
  fs.rmSync(file + ".tmp", { force: true });
  return { removed: true };
}

/** Every stored key as a Map of name to key. Empty if there is no file or it cannot be decrypted. */
function loadKeys(file, crypto) {
  const text = readDecrypted(file, crypto);
  const keys = new Map();
  if (text === null) return keys;
  let payload = null;
  try { payload = JSON.parse(text); } catch (e) { /* an old single-key file */ }
  if (payload && payload.version === 1 && payload.keys && typeof payload.keys === "object") {
    for (const [name, key] of Object.entries(payload.keys)) {
      if (validKeyName(name) && validKey(key)) keys.set(name, key);
    }
  } else if (validKey(text)) {
    keys.set(DEFAULT_KEY_NAME, text);
  }
  return keys;
}

function writeKeys(file, keys, crypto) {
  return writeEncrypted(file, JSON.stringify({ version: 1, keys: Object.fromEntries(keys) }), crypto);
}

function dropName(keys, name) {
  const wanted = name.trim().toLowerCase();
  for (const existing of [...keys.keys()]) if (existing.toLowerCase() === wanted) keys.delete(existing);
}

/**
 * Add a named key or replace the one with that name (ignoring case). An old single-key file
 * becomes the new format here. Nothing is written unless encryption works.
 * Returns {stored: true} or {stored: false, reason}.
 */
function saveNamedKey(file, name, key, crypto) {
  if (!validKeyName(name)) return { stored: false, reason: `not stored: the name is not 1-${KEY_NAME_MAX} characters` };
  if (!validKey(key)) return { stored: false, reason: "not stored: the key is not 8-512 printable characters" };
  const keys = loadKeys(file, crypto);
  dropName(keys, name);
  keys.set(name.trim(), key);
  return writeKeys(file, keys, crypto);
}

/**
 * Remove one named key; the file is deleted with the last one. A file that cannot be decrypted is
 * left alone: its keys may come back if encryption works again, and this call can't tell what is in it.
 * Returns {removed, remaining}.
 */
function removeNamedKey(file, name, crypto) {
  if (typeof name !== "string") return { removed: false, remaining: 0 };
  if (fs.existsSync(file) && readDecrypted(file, crypto) === null) return { removed: true, remaining: 0 };
  const keys = loadKeys(file, crypto);
  dropName(keys, name);
  if (keys.size === 0) {
    removeKeyFile(file);
    return { removed: true, remaining: 0 };
  }
  const written = writeKeys(file, keys, crypto);
  return { removed: written.stored, remaining: keys.size };
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
  bundledBackend, backendArgs, pickFreePort, pythonCandidates, dataDir, keyFilePath, backendEnv, validKey, validKeyName,
  saveKeyFile, loadKeyFile, removeKeyFile, loadKeys, saveNamedKey, removeNamedKey,
  cleanText, notification, linkAction, escapeHtml, statusPage,
};
