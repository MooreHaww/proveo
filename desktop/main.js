// Electron shell around the Eco app: starts eco_server.py on a free loopback port, shows it in a
// locked-down window, and keeps the API key encrypted with safeStorage. The key is never logged,
// never put in a URL, an argument or the environment: it goes to the backend by POST /api/key.
"use strict";

const { app, BrowserWindow, Notification, ipcMain, safeStorage, session, shell } = require("electron");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");
const lib = require("./lib");

const REPO = path.resolve(__dirname, "..");
const ICON = path.join(REPO, "assets", process.platform === "win32" ? "icon.ico" : "icon.png");
const HEALTH_TIMEOUT_MS = 20000;
const RESTART_DELAY_MS = 1500;  // lets the page show its "stored" message first

// For tests: run against a throwaway profile instead of the real one.
if (process.env.ECO_DESKTOP_USER_DATA) app.setPath("userData", process.env.ECO_DESKTOP_USER_DATA);

let win = null;
let backend = null;  // { child, port, origin, allowCloud, output, exited, stopping }
let restarting = false;

const keyFile = () => lib.keyFilePath(app.getPath("userData"));
const dataUrl = (html) => "data:text/html;charset=utf-8," + encodeURIComponent(html);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------- Backend process ---------- */

function spawnPython(port, env) {
  const candidates = lib.pythonCandidates(REPO);
  return new Promise((resolve, reject) => {
    const tryNext = (i, errors) => {
      if (i >= candidates.length) return reject(new Error("Could not start Python.\n" + errors.join("\n")));
      const child = spawn(candidates[i], ["-c", lib.BACKEND_BOOT, String(port)],
        { cwd: REPO, env, stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
      child.once("error", (e) => tryNext(i + 1, [...errors, `${candidates[i]}: ${e.message}`]));
      child.once("spawn", () => resolve(child));
    };
    tryNext(0, []);
  });
}

function request(port, method, urlPath, body) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : Buffer.from(JSON.stringify(body));
    const headers = { Host: `127.0.0.1:${port}` };
    if (data) Object.assign(headers, { "Content-Type": "application/json", "Content-Length": data.length });
    const req = http.request({ host: "127.0.0.1", port, method, path: urlPath, headers, timeout: 3000 }, (res) => {
      res.resume();  // the body is not needed (and for /api/key it is only {"set": ...})
      res.on("end", () => resolve(res.statusCode));
    });
    req.on("timeout", () => req.destroy(new Error("timed out")));
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}

async function startBackend(allowCloud) {
  const port = await lib.pickFreePort();
  const env = lib.backendEnv(process.env, app.getPath("userData"), allowCloud);
  fs.mkdirSync(env.ECO_DATA_DIR, { recursive: true });
  const child = await spawnPython(port, env);
  const b = { child, port, origin: `http://127.0.0.1:${port}`, allowCloud, output: "", exited: null, stopping: false };
  backend = b;
  const keep = (chunk) => {
    const text = chunk.toString("utf8");
    b.output = (b.output + text).slice(-8000);
    process.stdout.write(text.replace(/^(?=.)/gm, "[backend] "));
  };
  child.stdout.on("data", keep);
  child.stderr.on("data", keep);
  child.stdin.on("error", () => {});
  child.on("exit", (code, signal) => {
    b.exited = signal ? `signal ${signal}` : `exit code ${code}`;
    if (backend === b && !b.stopping && !restarting) {
      showError("The Eco backend stopped unexpectedly", `${b.exited}\n\n${b.output}`);
    }
  });
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (b.exited) throw new Error(`The backend exited (${b.exited}).\n\n${b.output}`);
    try {
      if (await request(port, "GET", "/api/config") === 200) return b;
    } catch (e) { /* not listening yet */ }
    await sleep(200);
  }
  stopBackend(b);
  throw new Error(`The backend did not answer within ${HEALTH_TIMEOUT_MS / 1000} s.\n\n${b.output}`);
}

function stopBackend(b) {
  if (!b || b.exited || b.stopping) return;
  b.stopping = true;
  try { b.child.stdin.end(); } catch (e) { /* already closed */ }
  if (process.platform === "win32") {
    // /T also ends the real python.exe that the .venv launcher starts as its child.
    spawnSync("taskkill", ["/pid", String(b.child.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
  } else {
    b.child.kill("SIGTERM");
  }
}

/** Start (or restart) the backend, restore a stored key, and load the app page. */
async function launch(hash) {
  let key = lib.loadKeyFile(keyFile(), safeStorage);
  if (key === null && fs.existsSync(keyFile())) console.log("A stored key could not be decrypted; starting without it.");
  try {
    const b = await startBackend(key !== null);
    if (key !== null) {
      const status = await request(b.port, "POST", "/api/key", { key });
      if (status !== 200) console.log(`Restoring the stored key failed (HTTP ${status}).`);
    }
    if (win) await win.loadURL(`${b.origin}/${key !== null ? "?stored=1" : ""}${hash || ""}`);
  } finally {
    key = null;
  }
}

function scheduleRestart() {
  if (restarting) return;
  restarting = true;
  setTimeout(async () => {
    let hash = "";
    try { hash = new URL(win.webContents.getURL()).hash; } catch (e) { /* keep "" */ }
    stopBackend(backend);
    try { await launch(hash); }
    catch (e) { showError("Eco mode could not restart its backend", e.message); }
    finally { restarting = false; }
  }, RESTART_DELAY_MS);
}

/* ---------- Window ---------- */

function showError(title, detail) {
  if (win) win.loadURL(dataUrl(lib.statusPage(title, detail)));
}

function appOrigin() {
  return backend ? backend.origin : "null";
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280, height: 860, minWidth: 900, minHeight: 600, icon: ICON, title: "Eco mode",
    backgroundColor: "#F7F6F1",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true, nodeIntegration: false, sandbox: true, webviewTag: false, spellcheck: false,
    },
  });
  const wc = win.webContents;
  wc.setWindowOpenHandler(({ url }) => {
    if (lib.linkAction(url, appOrigin()) === "external") shell.openExternal(url);
    return { action: "deny" };
  });
  wc.on("will-navigate", (event, url) => {
    const action = lib.linkAction(url, appOrigin());
    if (action === "allow") return;
    event.preventDefault();
    if (action === "external") shell.openExternal(url);
  });
  wc.on("will-redirect", (event, url) => {
    if (lib.linkAction(url, appOrigin()) !== "allow") event.preventDefault();
  });
  win.on("closed", () => { win = null; });
  win.loadURL(dataUrl(lib.statusPage("Starting Eco mode…", "")));
}

/* ---------- Page hooks (window.ecoDesktop) ---------- */

function fromApp(event) {
  try { return !!backend && new URL(event.senderFrame.url).origin === backend.origin; } catch (e) { return false; }
}

ipcMain.handle("eco:notify", (event, title, body) => {
  const n = fromApp(event) && lib.notification(title, body);
  if (!n || !Notification.isSupported()) return false;
  new Notification({ title: n.title, body: n.body, icon: ICON }).show();
  return true;
});

ipcMain.handle("eco:saveKey", (event, key) => {
  if (!fromApp(event)) return { stored: false, reason: "not stored: the request did not come from the app page" };
  const result = lib.saveKeyFile(keyFile(), key, safeStorage);
  if (result.stored && !backend.allowCloud) {  // cloud permission is fixed per backend run
    result.restarting = true;
    scheduleRestart();
  }
  return result;
});

ipcMain.handle("eco:removeKey", (event) => {
  if (!fromApp(event)) return { removed: false };
  const result = lib.removeKeyFile(keyFile());
  if (backend.allowCloud) {
    result.restarting = true;
    scheduleRestart();
  }
  return result;
});

/* ---------- App lifecycle ---------- */

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
    console.log("Second launch: focused the existing window.");
  });

  app.whenReady().then(async () => {
    if (process.platform === "win32") app.setAppUserModelId("pi-edge-router.eco");
    session.defaultSession.setPermissionRequestHandler((wc, permission, callback) => callback(false));
    session.defaultSession.setPermissionCheckHandler(() => false);
    createWindow();
    try { await launch(""); }
    catch (e) { showError("Eco mode could not start", e.message); }
  });

  app.on("window-all-closed", () => app.quit());
  app.on("will-quit", () => stopBackend(backend));
  process.on("exit", () => stopBackend(backend));
  process.on("uncaughtException", (e) => {
    console.error("Desktop shell error:", e && e.message);
    stopBackend(backend);
    app.exit(1);
  });
}
