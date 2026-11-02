// Plain helpers for the desktop shell: no Electron imports, so they run under `node --test`.
"use strict";

const fs = require("fs");
const net = require("net");
const path = require("path");

const PROFILE_NAME = "Proveo", OLD_PROFILE_NAME = "Eco";
const KEY_FILE = "model-key.enc";  // encrypted bytes from safeStorage, never plaintext
const KEY_MIN = 8;                 // same limits as eco_server.valid_key
const KEY_MAX = 512;
const KEY_NAME_MAX = 40;           // same limit as settings.clean_key_name, which also checks the wording
const DEFAULT_KEY_NAME = "Default";  // what an old single-key file loads as
const KEY_PROVIDERS = ["Meta", "OpenAI", "Claude (Anthropic)", "Gemini (Google)", "Other"];  // same as eco_core.KEY_PROVIDERS
const KEY_PREFIXES = [["sk-ant-", "Claude (Anthropic)"], ["sk-proj-", "OpenAI"], ["AIza", "Gemini (Google)"], ["gsk_", "Groq"]];  // same as eco_core.KEY_PREFIXES
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
  return path.join(resourcesPath, "backend", "proveo-backend.exe");
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

/* The one-time move from the old profile folder to the new one. It moves the whole folder: the encrypted key file
   only decrypts next to Chromium's Local State, which holds its key. Only a folder named Proveo takes over a sibling
   named Eco, and only if Proveo does not exist yet. Returns the folder to use: the old one if the move failed
   (it is locked while the old app runs), so nothing looks lost and the next start tries again. */
function migrateProfile(profile, rename = fs.renameSync) {
  if (path.basename(profile) !== PROFILE_NAME) return profile;
  const old = path.join(path.dirname(profile), OLD_PROFILE_NAME);
  if (fs.existsSync(profile) || !fs.existsSync(old) || !fs.statSync(old).isDirectory()) return profile;
  try {
    rename(old, profile);
    return profile;
  } catch (e) {
    return old;
  }
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

/** A key's provider label. Without a valid one: Default is Meta, any other name is Other (eco_core.key_provider). */
function keyProvider(name, label) {
  if (KEY_PROVIDERS.includes(label)) return label;
  return name.trim().toLowerCase() === DEFAULT_KEY_NAME.toLowerCase() ? "Meta" : "Other";
}

/** Why this key can't have this label, or "" (eco_core.key_label_problem). Never any part of the key. */
function keyLabelProblem(key, label) {
  const known = KEY_PREFIXES.find(([prefix]) => key.startsWith(prefix));
  if (!known || label === known[1] || label === "Other") return "";
  const problem = `This key looks like ${known[1] === "OpenAI" ? "an" : "a"} ${known[1]} key, but it is labelled ${label}. Choose the right provider.`;
  return KEY_PROVIDERS.includes(known[1]) ? problem : `${problem} ${known[1]} is not on the list, so choose Other.`;
}

/** A stored label the key's prefix contradicts becomes Other, so an older file can't send it to Meta. */
const storedLabel = (key, label) => (keyLabelProblem(key, label) ? "Other" : label);

/** Every stored key as a Map of name to {key, provider, disabled}. Empty if there is no file or it cannot be decrypted. */
function loadKeyEntries(file, crypto) {
  const text = readDecrypted(file, crypto);
  const entries = new Map();
  if (text === null) return entries;
  let payload = null;
  try { payload = JSON.parse(text); } catch (e) { /* an old single-key file */ }
  if (payload && payload.version === 1 && payload.keys && typeof payload.keys === "object") {
    const labels = payload.providers && typeof payload.providers === "object" ? payload.providers : {};
    const off = new Set(Array.isArray(payload.disabled) ? payload.disabled : []);
    for (const [name, key] of Object.entries(payload.keys)) {
      if (validKeyName(name) && validKey(key)) entries.set(name, { key, provider: storedLabel(key, keyProvider(name, labels[name])), ...(off.has(name) ? { disabled: true } : {}) });
    }
  } else if (validKey(text)) {
    entries.set(DEFAULT_KEY_NAME, { key: text, provider: storedLabel(text, "Meta") });
  }
  return entries;
}

/** The stored keys alone, as a Map of name to key. */
function loadKeys(file, crypto) {
  return new Map([...loadKeyEntries(file, crypto)].map(([name, entry]) => [name, entry.key]));
}

function writeKeys(file, entries, crypto) {
  const pick = (field) => Object.fromEntries([...entries].map(([name, entry]) => [name, entry[field]]));
  const disabled = [...entries].filter(([, entry]) => entry.disabled).map(([name]) => name);
  return writeEncrypted(file, JSON.stringify({ version: 1, keys: pick("key"), providers: pick("provider"), ...(disabled.length ? { disabled } : {}) }), crypto);
}

function dropName(keys, name) {
  const wanted = name.trim().toLowerCase();
  for (const existing of [...keys.keys()]) if (existing.toLowerCase() === wanted) keys.delete(existing);
}

/** True if the file is there, encryption works, and it still gives no keys. */
function keyFileUnreadable(file, crypto) {
  return fs.existsSync(file) && crypto.isEncryptionAvailable() && loadKeyEntries(file, crypto).size === 0;
}

/**
 * A key file that gives no keys is moved to <file>.unreadable, so a later save can't overwrite it. Its bytes are
 * not changed. While encryption is off it stays in place, since its keys may come back when encryption works again.
 * Returns "none" (nothing wrong), "aside" (moved) or "kept" (left in place).
 */
function setAsideUnreadableKeys(file, crypto) {
  if (!fs.existsSync(file)) return "none";
  if (!crypto.isEncryptionAvailable()) return "kept";
  if (loadKeyEntries(file, crypto).size > 0) return "none";
  for (let n = 1; n <= 100; n++) {
    const aside = `${file}.unreadable${n === 1 ? "" : `-${n}`}`;
    if (fs.existsSync(aside)) continue;
    try { fs.renameSync(file, aside); return "aside"; } catch (e) { return "kept"; }
  }
  return "kept";
}

/**
 * Add a named key or replace the one with that name (ignoring case). An old single-key file
 * becomes the new format here. Nothing is written unless encryption works, and a file that gives no
 * keys is never overwritten. With no provider, the label follows keyProvider.
 * Returns {stored: true} or {stored: false, reason}.
 */
function saveNamedKey(file, name, key, crypto, provider) {
  if (!validKeyName(name)) return { stored: false, reason: `not stored: the name is not 1-${KEY_NAME_MAX} characters` };
  if (!validKey(key)) return { stored: false, reason: "not stored: the key is not 8-512 printable characters" };
  if (provider && !KEY_PROVIDERS.includes(provider)) return { stored: false, reason: "not stored: that provider is not on the list" };
  const label = keyProvider(name, provider);
  const problem = keyLabelProblem(key, label);
  if (problem) return { stored: false, reason: `not stored: ${problem}` };
  if (keyFileUnreadable(file, crypto)) return { stored: false, reason: "not stored: the keys already stored could not be read" };
  const entries = loadKeyEntries(file, crypto);
  const wanted = name.trim().toLowerCase();
  const disabled = [...entries].some(([n, entry]) => n.toLowerCase() === wanted && entry.disabled);  // a replacement stays disabled
  dropName(entries, name);
  entries.set(name.trim(), { key, provider: label, ...(disabled ? { disabled: true } : {}) });
  return writeKeys(file, entries, crypto);
}

/**
 * Mark a stored key disabled or enabled; it stays stored either way. Nothing is written if the file
 * cannot be decrypted or has no key by that name. Returns {stored: true} or {stored: false, reason}.
 */
function setNamedKeyDisabled(file, name, disabled, crypto) {
  if (typeof name !== "string") return { stored: false, reason: "not stored: no key has that name" };
  const wanted = name.trim().toLowerCase();
  const entries = loadKeyEntries(file, crypto);
  const found = [...entries.keys()].find((n) => n.toLowerCase() === wanted);
  if (!found) return { stored: false, reason: "not stored: no key has that name" };
  if (disabled === true) entries.get(found).disabled = true; else delete entries.get(found).disabled;
  return writeKeys(file, entries, crypto);
}

/**
 * Remove one named key; the file is deleted with the last one. A file that cannot be decrypted is
 * left alone: its keys may come back if encryption works again, and this call can't tell what is in it.
 * Returns {removed, remaining}.
 */
function removeNamedKey(file, name, crypto) {
  if (typeof name !== "string") return { removed: false, remaining: 0 };
  if (fs.existsSync(file) && readDecrypted(file, crypto) === null) return { removed: true, remaining: 0 };
  const entries = loadKeyEntries(file, crypto);
  dropName(entries, name);
  if (entries.size === 0) {
    removeKeyFile(file);
    return { removed: true, remaining: 0 };
  }
  const written = writeKeys(file, entries, crypto);
  return { removed: written.stored, remaining: entries.size };
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
  return t || b ? { title: t || "Proveo", body: b } : null;
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
    "<title>Proveo</title><style>body{font:18px/1.5 system-ui,sans-serif;margin:48px;color:#1d1d1b;background:#f7f6f1}" +
    "h1{font-size:28px}pre{white-space:pre-wrap;background:#fff;border:1px solid #ccc;padding:16px;border-radius:8px;font-size:14px}</style>" +
    `</head><body><h1>${escapeHtml(title)}</h1>` + (detail ? `<pre>${escapeHtml(detail)}</pre>` : "") + "</body></html>";
}

module.exports = {
  BACKEND_BOOT, KEY_FILE, KEY_PROVIDERS, PROFILE_NAME, migrateProfile, NOTIFY_TITLE_MAX, NOTIFY_BODY_MAX,
  bundledBackend, backendArgs, pickFreePort, pythonCandidates, dataDir, keyFilePath, backendEnv, validKey, validKeyName,
  saveKeyFile, loadKeyFile, removeKeyFile, keyProvider, keyLabelProblem, loadKeyEntries, loadKeys, saveNamedKey, setNamedKeyDisabled, removeNamedKey, setAsideUnreadableKeys,
  cleanText, notification, linkAction, escapeHtml, statusPage,
};
