// Tests for desktop/lib.js. No Electron, no display, no network beyond 127.0.0.1.
// Run with: npm test (from desktop/)
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const lib = require("../lib");

const FAKE = "made-up-test-key-0123456789";

// A stand-in for safeStorage: reversible, and never the plaintext.
const fakeCrypto = (available = true) => ({
  isEncryptionAvailable: () => available,
  encryptString: (s) => Buffer.from([...Buffer.from(s, "utf8")].map((b) => b ^ 0x5a)),
  decryptString: (buf) => Buffer.from([...buf].map((b) => b ^ 0x5a)).toString("utf8"),
});

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "eco-desktop-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test("pickFreePort returns a port that can be bound on 127.0.0.1", async () => {
  const port = await lib.pickFreePort();
  assert.ok(Number.isInteger(port) && port > 0 && port < 65536);
  await new Promise((resolve, reject) => {
    const srv = net.createServer().once("error", reject).listen(port, "127.0.0.1", () => srv.close(resolve));
  });
  const ports = await Promise.all([lib.pickFreePort(), lib.pickFreePort(), lib.pickFreePort()]);
  assert.ok(ports.every((p) => Number.isInteger(p) && p > 0));
});

test("pythonCandidates prefers the repo .venv, then python on PATH", () => {
  const repo = path.join("C:", "repo");
  assert.deepEqual(lib.pythonCandidates(repo, "win32", () => true),
    [path.join(repo, ".venv", "Scripts", "python.exe"), "python"]);
  assert.deepEqual(lib.pythonCandidates(repo, "win32", () => false), ["python"]);
  assert.deepEqual(lib.pythonCandidates(repo, "linux", () => true), [path.join(repo, ".venv", "bin", "python"), "python3"]);
  assert.deepEqual(lib.pythonCandidates(repo, "darwin", () => false), ["python3"]);
});

test("data dir and key file live in userData", () => {
  const ud = path.join("C:", "Users", "x", "AppData", "Roaming", "pi-edge-router-desktop");
  assert.equal(lib.dataDir(ud), path.join(ud, "eco"));
  assert.equal(lib.keyFilePath(ud), path.join(ud, "model-key.enc"));
});

test("backendEnv drops an inherited key and cloud flag; cloud only with a stored key", () => {
  const base = { PATH: "p", MODEL_API_KEY: "inherited", ALLOW_CLOUD: "1" };
  const off = lib.backendEnv(base, "UD", false);
  assert.equal(off.MODEL_API_KEY, undefined);
  assert.equal(off.ALLOW_CLOUD, undefined);
  assert.equal(off.ECO_DATA_DIR, path.join("UD", "eco"));
  assert.equal(off.PATH, "p");
  assert.equal(lib.backendEnv(base, "UD", true).ALLOW_CLOUD, "1");
  assert.equal(base.MODEL_API_KEY, "inherited", "the input is not changed");
  for (const v of Object.values(lib.backendEnv(base, "UD", true))) assert.notEqual(v, FAKE);
});

test("the backend boot code takes only the port as an argument", () => {
  assert.match(lib.BACKEND_BOOT, /runpy\.run_path\('eco_server\.py', run_name='__main__'\)/);
  assert.match(lib.BACKEND_BOOT, /sys\.stdin\.buffer\.read\(\)/, "exits when the desktop app goes away");
  assert.doesNotMatch(lib.BACKEND_BOOT, /KEY|key/);
});

test("saveKeyFile writes encrypted bytes, never the plaintext; load and remove round-trip", (t) => {
  const file = path.join(tempDir(t), "sub", "model-key.enc");
  assert.deepEqual(lib.saveKeyFile(file, FAKE, fakeCrypto()), { stored: true });
  const raw = fs.readFileSync(file);
  assert.ok(!raw.includes(Buffer.from(FAKE)), "file is not plaintext");
  assert.ok(!fs.existsSync(file + ".tmp"));
  assert.equal(lib.loadKeyFile(file, fakeCrypto()), FAKE);
  assert.deepEqual(lib.removeKeyFile(file), { removed: true });
  assert.ok(!fs.existsSync(file));
  assert.equal(lib.loadKeyFile(file, fakeCrypto()), null);
  assert.deepEqual(lib.removeKeyFile(file), { removed: true }, "removing twice is fine");
});

test("nothing is written when encryption is unavailable or the key is invalid", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  const r = lib.saveKeyFile(file, FAKE, fakeCrypto(false));
  assert.equal(r.stored, false);
  assert.match(r.reason, /^not stored: /);
  assert.ok(!r.reason.includes(FAKE));
  for (const bad of ["short", "has space inside", "tab\there!", "x".repeat(513), null, 42]) {
    assert.equal(lib.saveKeyFile(file, bad, fakeCrypto()).stored, false, String(bad));
  }
  assert.ok(!fs.existsSync(file));
});

test("loadKeyFile ignores files it cannot decrypt", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  fs.writeFileSync(file, "garbage");
  const throwing = { ...fakeCrypto(), decryptString: () => { throw new Error("bad data"); } };
  assert.equal(lib.loadKeyFile(file, throwing), null);
  assert.equal(lib.loadKeyFile(file, fakeCrypto(false)), null);
  assert.equal(lib.loadKeyFile(file, fakeCrypto()), null, "decrypts to an invalid key");
});

test("notifications ignore empty input and cut long text", () => {
  assert.equal(lib.notification("", ""), null);
  assert.equal(lib.notification("   ", "\n\t"), null);
  assert.equal(lib.notification(undefined, {}), null);
  assert.deepEqual(lib.notification("", "body only"), { title: "Eco mode", body: "body only" });
  const n = lib.notification("T".repeat(500), "line1\nline2 " + "b".repeat(1000));
  assert.equal(n.title.length, lib.NOTIFY_TITLE_MAX);
  assert.ok(n.title.endsWith("\u2026"));
  assert.equal(n.body.length, lib.NOTIFY_BODY_MAX);
  assert.ok(n.body.startsWith("line1 line2 "));
});

test("links: own origin allowed, https external, everything else blocked", () => {
  const o = "http://127.0.0.1:5123";
  assert.equal(lib.linkAction("http://127.0.0.1:5123/#cloud", o), "allow");
  assert.equal(lib.linkAction("http://127.0.0.1:5123/?stored=1", o), "allow");
  assert.equal(lib.linkAction("https://example.com/docs", o), "external");
  for (const url of ["http://127.0.0.1:5124/", "http://localhost:5123/", "http://example.com/", "file:///C:/Windows/win.ini",
                     "javascript:alert(1)", "data:text/html,hi", "mailto:a@b.c", "not a url", "ms-settings:"]) {
    assert.equal(lib.linkAction(url, o), "block", url);
  }
});

test("status page escapes the error text", () => {
  const html = lib.statusPage("Could not start", "<script>alert(1)</script> & 'x'");
  assert.ok(!html.includes("<script>alert"));
  assert.ok(html.includes("&lt;script&gt;alert(1)&lt;/script&gt; &amp; &#39;x&#39;"));
  assert.match(html, /default-src 'none'/);
});
