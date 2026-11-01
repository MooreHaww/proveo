// Tests for desktop/lib.js. No Electron, no display, no network beyond 127.0.0.1.
// Run with: npm test (from desktop/)
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { test } = require("node:test");
const lib = require("../lib");

const REPO = path.resolve(__dirname, "..", "..");

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

test("the server options put the log and memory in the app's data folder", () => {
  const dir = path.join("UD", "eco");
  assert.deepEqual(lib.backendArgs(1234, "UD"),
    ["--port", "1234", "--log", path.join(dir, "eco_log.jsonl"), "--memory", path.join(dir, "eco_memory.jsonl")]);
  assert.equal(lib.bundledBackend("RES"), path.join("RES", "backend", "eco-backend.exe"));
});

test("the backend boot code takes its options from its arguments and holds no key", () => {
  assert.match(lib.BACKEND_BOOT, /runpy\.run_path\('eco_server\.py', run_name='__main__'\)/);
  assert.match(lib.BACKEND_BOOT, /os\.read\(watch, 1\)/, "exits when the desktop app goes away");
  assert.match(lib.BACKEND_BOOT, /os\.dup2\(null, 0\)/, "children never inherit the watched pipe");
  assert.doesNotMatch(lib.BACKEND_BOOT, /KEY|key/);
});

// The real boot code around a stub eco_server.py, started the way main.js starts the backend.
function bootStub(t, body) {
  const dir = tempDir(t);
  fs.writeFileSync(path.join(dir, "eco_server.py"), `import sys\nsys.path.insert(0, ${JSON.stringify(REPO)})\n${body}`);
  const env = lib.backendEnv(process.env, dir, false);
  return spawn(lib.pythonCandidates(REPO)[0], ["-c", lib.BACKEND_BOOT, "1"],
    { cwd: dir, env, stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
}

function collect(child) {
  return new Promise((resolve) => {
    let out = "";
    child.stdout.on("data", (d) => { out += d; });
    child.stderr.on("data", (d) => { out += d; });
    child.on("exit", (code) => resolve({ code, out }));
  });
}

test("the code checker works under the real watchdog boot", async (t) => {
  const child = bootStub(t, `
import json, bench_coding
good = "def add(a, b):\\n    return a + b\\n"
task = {"id": "t", "entry": "add", "tests": [[[1, 2], 3]]}
print(json.dumps([bench_coding.run_tests(good, task), bench_coding.run_custom_tests(good, "assert add(1, 2) == 3\\n")]), flush=True)
`);
  const { code, out } = await collect(child);
  assert.equal(code, 0, out);
  const [bench, custom] = JSON.parse(out.trim().split("\n").pop());
  assert.equal(bench.passed, 1, out);
  assert.equal(custom.passed, true, out);
});

test("a child of the backend that inherits stdin does not hang on the watched pipe", async (t) => {
  const child = bootStub(t, `
import json, subprocess
try:
    p = subprocess.run([sys.executable, "-I", "-c", "print('started')"], capture_output=True, text=True, timeout=5)
    print(json.dumps(p.stdout.strip()), flush=True)
except subprocess.TimeoutExpired:
    print(json.dumps("hung"), flush=True)
`);
  const { code, out } = await collect(child);
  assert.equal(code, 0, out);
  assert.equal(JSON.parse(out.trim().split("\n").pop()), "started");
});

test("the backend exits when its stdin pipe closes", async (t) => {
  const child = bootStub(t, "import time\nprint('ready', flush=True)\ntime.sleep(60)\n");
  const done = collect(child);
  await new Promise((resolve) => child.stdout.once("data", resolve));
  const start = Date.now();
  child.stdin.end();
  const { code } = await done;
  assert.equal(code, 0);
  assert.ok(Date.now() - start < 5000, "exits promptly");
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

/* ---------- Named keys in one encrypted file ---------- */

const KEY_2 = "made-up-test-key-second-98765";

test("named keys: encrypted on disk, restored together, one file, no temp left", (t) => {
  const file = path.join(tempDir(t), "sub", "model-key.enc");
  assert.deepEqual(lib.saveNamedKey(file, " Meta personal ", FAKE, fakeCrypto()), { stored: true });
  assert.deepEqual(lib.saveNamedKey(file, "Claude key", KEY_2, fakeCrypto()), { stored: true });
  const raw = fs.readFileSync(file);
  for (const secret of [FAKE, KEY_2, "Meta personal"]) assert.ok(!raw.includes(Buffer.from(secret)), "file is not plaintext");
  assert.deepEqual(fs.readdirSync(path.dirname(file)), ["model-key.enc"]);
  assert.deepEqual([...lib.loadKeys(file, fakeCrypto())], [["Meta personal", FAKE], ["Claude key", KEY_2]]);
});

test("named keys: the same name in another case replaces", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  lib.saveNamedKey(file, "Meta", FAKE, fakeCrypto());
  lib.saveNamedKey(file, "META", KEY_2, fakeCrypto());
  assert.deepEqual([...lib.loadKeys(file, fakeCrypto())], [["META", KEY_2]]);
});

test("named keys: remove one keeps the others; removing the last deletes the file", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  lib.saveNamedKey(file, "One", FAKE, fakeCrypto());
  lib.saveNamedKey(file, "Two", KEY_2, fakeCrypto());
  assert.deepEqual(lib.removeNamedKey(file, "one", fakeCrypto()), { removed: true, remaining: 1 });
  assert.deepEqual([...lib.loadKeys(file, fakeCrypto())], [["Two", KEY_2]]);
  assert.deepEqual(lib.removeNamedKey(file, "Not there", fakeCrypto()), { removed: true, remaining: 1 });
  assert.deepEqual(lib.removeNamedKey(file, "Two", fakeCrypto()), { removed: true, remaining: 0 });
  assert.ok(!fs.existsSync(file), "the file goes with the last key");
  assert.deepEqual(lib.removeNamedKey(file, "Two", fakeCrypto()), { removed: true, remaining: 0 }, "removing twice is fine");
  assert.deepEqual(lib.removeNamedKey(file, 42, fakeCrypto()), { removed: false, remaining: 0 });
});

test("named keys: an old single-key file loads as Default and migrates on the next save", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  assert.deepEqual(lib.saveKeyFile(file, FAKE, fakeCrypto()), { stored: true });
  assert.deepEqual([...lib.loadKeys(file, fakeCrypto())], [["Default", FAKE]]);
  lib.saveNamedKey(file, "Two", KEY_2, fakeCrypto());
  assert.deepEqual([...lib.loadKeys(file, fakeCrypto())], [["Default", FAKE], ["Two", KEY_2]]);
  assert.equal(JSON.parse(fakeCrypto().decryptString(fs.readFileSync(file))).version, 1, "no longer the old format");
  lib.removeNamedKey(file, "Default", fakeCrypto());
  assert.deepEqual([...lib.loadKeys(file, fakeCrypto())], [["Two", KEY_2]]);
});

test("named keys: nothing is written when encryption is unavailable or the input is invalid", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  const r = lib.saveNamedKey(file, "One", FAKE, fakeCrypto(false));
  assert.equal(r.stored, false);
  assert.match(r.reason, /^not stored: /);
  assert.ok(!r.reason.includes(FAKE));
  for (const bad of ["", "   ", "x".repeat(41), "tab\there", null, 42]) {
    assert.equal(lib.saveNamedKey(file, bad, FAKE, fakeCrypto()).stored, false, String(bad));
  }
  assert.equal(lib.saveNamedKey(file, "One", "short", fakeCrypto()).stored, false);
  assert.ok(!fs.existsSync(file));
});

test("named keys: a name like __proto__ is only a name", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  lib.saveNamedKey(file, "__proto__", FAKE, fakeCrypto());
  assert.deepEqual([...lib.loadKeys(file, fakeCrypto())], [["__proto__", FAKE]]);
  assert.equal({}.version, undefined);
});

test("named keys: unreadable files give no keys, and a remove leaves them alone", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  fs.writeFileSync(file, "garbage");
  const throwing = { ...fakeCrypto(), decryptString: () => { throw new Error("bad data"); } };
  assert.equal(lib.loadKeys(file, throwing).size, 0);
  assert.equal(lib.loadKeys(file, fakeCrypto(false)).size, 0);
  assert.equal(lib.loadKeys(file, fakeCrypto()).size, 0, "decrypts to neither format");
  assert.equal(lib.loadKeys(path.join(path.dirname(file), "none.enc"), fakeCrypto()).size, 0);
  lib.removeNamedKey(file, "One", throwing);
  assert.ok(fs.existsSync(file), "kept: it may decrypt again later");
  const bad = fakeCrypto();
  fs.writeFileSync(file, bad.encryptString(JSON.stringify({ version: 1, keys: { "ok name": "short", Good: FAKE } })));
  assert.deepEqual([...lib.loadKeys(file, bad)], [["Good", FAKE]], "invalid entries are ignored");
});

/* ---------- Provider labels in the key file ---------- */

const entries = (file) => [...lib.loadKeyEntries(file, fakeCrypto())];
const rawFile = (file, payload) => fs.writeFileSync(file, fakeCrypto().encryptString(typeof payload === "string" ? payload : JSON.stringify(payload)));

test("providers: kept beside the name, encrypted, and every entry keeps its own", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  assert.deepEqual(lib.saveNamedKey(file, "Claude key", FAKE, fakeCrypto(), "Claude (Anthropic)"), { stored: true });
  assert.deepEqual(lib.saveNamedKey(file, "Meta personal", KEY_2, fakeCrypto(), "Meta"), { stored: true });
  assert.deepEqual(entries(file), [["Claude key", { key: FAKE, provider: "Claude (Anthropic)" }], ["Meta personal", { key: KEY_2, provider: "Meta" }]]);
  assert.ok(!fs.readFileSync(file).includes(Buffer.from("Anthropic")), "the label is inside the encrypted file");
  assert.deepEqual(lib.removeNamedKey(file, "Meta personal", fakeCrypto()), { removed: true, remaining: 1 });
  assert.deepEqual(entries(file), [["Claude key", { key: FAKE, provider: "Claude (Anthropic)" }]], "a remove keeps the others' labels");
});

test("providers: replacing a name replaces its label; an unknown label stores nothing", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  lib.saveNamedKey(file, "Mine", FAKE, fakeCrypto(), "Meta");
  lib.saveNamedKey(file, "MINE", KEY_2, fakeCrypto(), "OpenAI");
  assert.deepEqual(entries(file), [["MINE", { key: KEY_2, provider: "OpenAI" }]]);
  const r = lib.saveNamedKey(file, "New", FAKE, fakeCrypto(), "Anyone");
  assert.equal(r.stored, false);
  assert.ok(!r.reason.includes(FAKE));
  assert.deepEqual(entries(file).map(([n]) => n), ["MINE"]);
});

test("providers: with no label, Default means Meta and any other name means Other", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  for (const none of [undefined, ""]) {
    lib.saveNamedKey(file, "Default", FAKE, fakeCrypto(), none);
    lib.saveNamedKey(file, "Mine", KEY_2, fakeCrypto(), none);
    assert.deepEqual(entries(file), [["Default", { key: FAKE, provider: "Meta" }], ["Mine", { key: KEY_2, provider: "Other" }]]);
    lib.removeNamedKey(file, "Default", fakeCrypto());
    lib.removeNamedKey(file, "Mine", fakeCrypto());
  }
});

test("providers: legacy files load by those rules, and migrate without changing them", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  rawFile(file, FAKE);
  assert.deepEqual(entries(file), [["Default", { key: FAKE, provider: "Meta" }]], "the old single-key file");

  rawFile(file, { version: 1, keys: { Default: FAKE, "Claude key": KEY_2 } });
  const legacy = [["Default", { key: FAKE, provider: "Meta" }], ["Claude key", { key: KEY_2, provider: "Other" }]];
  assert.deepEqual(entries(file), legacy, "named keys with no providers");

  lib.saveNamedKey(file, "New", "made-up-test-key-third-11111", fakeCrypto(), "Gemini (Google)");
  assert.deepEqual(entries(file).slice(0, 2), legacy, "written out with the same labels");
  assert.deepEqual(JSON.parse(fakeCrypto().decryptString(fs.readFileSync(file))).providers,
    { Default: "Meta", "Claude key": "Other", New: "Gemini (Google)" });
});

test("providers: a bad label in the file falls back to the same rules", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  for (const providers of ["Meta", null, 12, { Mine: "Anyone", Default: 7, __proto__: { Mine: "Meta" } }]) {
    rawFile(file, { version: 1, keys: { Default: FAKE, Mine: KEY_2 }, providers });
    assert.deepEqual(entries(file), [["Default", { key: FAKE, provider: "Meta" }], ["Mine", { key: KEY_2, provider: "Other" }]], JSON.stringify(providers));
  }
});

/* ---------- Key prefixes ---------- */

const LOOKS = { "Claude (Anthropic)": "sk-ant-made-up-guard-QQ11", OpenAI: "sk-proj-made-up-guard-QQ22", "Gemini (Google)": "AIzamade-up-guard-QQ33", Groq: "gsk_made-up-guard-QQ44" };
const CLEAR = ["sk-made-up-bare-QQ55", "sk-antelope-made-up-QQ66", "sk-project-made-up-QQ77", "SK-ANT-made-up-QQ88", "aizamade-up-QQ99", FAKE];

test("key prefixes: a known prefix under another known label is refused, without the key", () => {
  for (const [looksLike, key] of Object.entries(LOOKS)) {
    for (const label of lib.KEY_PROVIDERS) {
      const problem = lib.keyLabelProblem(key, label);
      if (label === looksLike || label === "Other") { assert.equal(problem, "", `${key} as ${label}`); continue; }
      const article = looksLike === "OpenAI" ? "an" : "a";
      assert.ok(problem.startsWith(`This key looks like ${article} ${looksLike} key, but it is labelled ${label}. Choose the right provider.`), problem);
      assert.ok(!problem.includes(key.slice(0, 4)) && !problem.includes(key.slice(-4)), "no part of the key");
    }
  }
  for (const key of CLEAR) for (const label of lib.KEY_PROVIDERS) assert.equal(lib.keyLabelProblem(key, label), "", `${key} as ${label}`);
});

test("key prefixes: the page's own check gives the same answers", () => {
  const page = fs.readFileSync(path.join(REPO, "web", "index.html"), "utf8");
  const table = /const KEY_PREFIXES = \[\[.*?\]\];/.exec(page)[0];
  const fn = /function keyLabelProblem\(key, label\) \{[\s\S]*?\n  \}/.exec(page)[0];
  const options = lib.KEY_PROVIDERS.map((value) => ({ value }));
  const check = new Function("$", `${table}\n${fn}\nreturn keyLabelProblem;`)(() => ({ options }));
  for (const key of [...Object.values(LOOKS), ...CLEAR]) {
    for (const label of lib.KEY_PROVIDERS) assert.equal(check(key, label), lib.keyLabelProblem(key, label), `${key} as ${label}`);
  }
});

test("key prefixes: saving refuses a wrong label and writes nothing; Other and a bare sk- pass", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  for (const [args, label] of [[["Mine", LOOKS.OpenAI, fakeCrypto(), "Meta"], "Meta"], [["Default", LOOKS.Groq, fakeCrypto()], "no label, Default"]]) {
    const r = lib.saveNamedKey(file, ...args);
    assert.equal(r.stored, false, label);
    assert.match(r.reason, /^not stored: This key looks like an? /);
    for (const key of Object.values(LOOKS)) assert.ok(!r.reason.includes(key));
    assert.ok(!fs.existsSync(file), label);
  }
  assert.equal(lib.saveNamedKey(file, "Bare", CLEAR[0], fakeCrypto(), "Meta").stored, true);
  assert.equal(lib.saveNamedKey(file, "Groq key", LOOKS.Groq, fakeCrypto(), "Other").stored, true);
  assert.equal(lib.saveNamedKey(file, "Named", LOOKS.OpenAI, fakeCrypto()).stored, true, "no label, another name: Other");
  assert.deepEqual(entries(file).map(([n, e]) => [n, e.provider]), [["Bare", "Meta"], ["Groq key", "Other"], ["Named", "Other"]]);
});

test("key prefixes: an older file's contradicted label loads as Other, and the key is kept", (t) => {
  const file = path.join(tempDir(t), "model-key.enc");
  rawFile(file, LOOKS["Claude (Anthropic)"]);
  assert.deepEqual(entries(file), [["Default", { key: LOOKS["Claude (Anthropic)"], provider: "Other" }]], "the old single-key file");
  rawFile(file, { version: 1, keys: { Mine: LOOKS.OpenAI, Bare: CLEAR[0] }, providers: { Mine: "Meta", Bare: "Meta" } });
  assert.deepEqual(entries(file), [["Mine", { key: LOOKS.OpenAI, provider: "Other" }], ["Bare", { key: CLEAR[0], provider: "Meta" }]]);
});

test("the bridge passes the provider on, and the save handler refuses other origins first", () => {
  const preload = fs.readFileSync(path.join(__dirname, "..", "preload.js"), "utf8");
  assert.match(preload, /saveKey: \(name, key, provider\) => ipcRenderer\.invoke\("eco:saveKey", str\(name\), str\(key\), str\(provider\)\)/);
  assert.match(preload, /removeKey: \(name\) => ipcRenderer\.invoke\("eco:removeKey", str\(name\)\)/);
  const main = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  assert.match(main, /"eco:saveKey", \(event, name, key, provider\) => \{\s+if \(!fromApp\(event\)\) return \{ stored: false/);
  assert.match(main, /"eco:removeKey", \(event, name\) => \{\s+if \(!fromApp\(event\)\) return \{ removed: false/);
  assert.match(main, /"\/api\/key", \{ name, key, provider \}/);
});
