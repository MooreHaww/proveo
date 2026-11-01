# CLAUDE.md

## Project
pi-edge-router is Eco mode for AI coding: a task goes up a ladder of models, cheapest first, and the first answer that passes the developer's own tests wins.
Honest scope: it only works for tasks with tests, and the code check is a subprocess with a time limit, not a real sandbox.
Built for the Meta Global AI Developer Hackathon. It began as a router (router.py) from a quantized Llama 3.2 3B (Q4_K_M) on a Raspberry Pi 5 to a cloud model (Meta Model API, Muse Spark).

## Environment
- Windows, PowerShell. Python virtualenv in .venv; activate with `.venv\Scripts\Activate.ps1`.
- The Pi address is set by LOCAL_URL in router.py and can change after a reboot.
- Start llama-server on the Pi with a small context. Without `-c 4096` it gets killed for running out of memory:
  `./build/bin/llama-server -m ~/models/Llama-3.2-3B-Instruct-Q4_K_M.gguf -c 4096 --host 0.0.0.0 --port 8080`

## Architecture map
- eco_core.py: the ladder. Builds tiers, gives each its key, runs a task up the rungs, writes the log and verified memory.
- eco_server.py: local server (127.0.0.1 only) with the page and JSON API. Holds keys in memory. eco.py is the command line.
- settings.py: the tier list and budget in one JSON file. Refuses to save a secret.
- detect.py: finds local model servers (Ollama, LM Studio, llama.cpp). Read-only.
- budget.py: spend guard, read from the log, never estimated. ok, warn from 80%, blocked from 100%.
- bench_coding.py: model calls and the code checker (`run_tests`, `run_custom_tests`).
- eco_backend.py: entry point of the bundled backend. Its private `--eco-check` mode is the checker's child process.
- web/index.html: the whole app page (Run, Models, Cloud and Budget, About).
- desktop/: Electron shell. main.js (window, backend lifecycle, keys), lib.js (testable helpers), preload.js (the page's only bridge).
- build_backend.py: builds build/eco-backend/eco-backend.exe with PyInstaller in its own virtualenv under build/.
- Installer: `npm run dist` runs build_backend.py, then electron-builder writes an NSIS installer to desktop/dist/.
- router.py: the original routing decision. In order, these escalate to the cloud: question too long
  (MAX_QUESTION_CHARS = 500), letter/spelling question, empty answer, refusal phrase,
  then average logprob below THRESHOLD = -0.2.
- eval_local.py: runs data/questions.json through the local model.
- validate.py: runs a question file through the router's decision rule.
- data/: question sets and coding tasks. results/: CSV outputs, plus the CLI's Eco log and memory.

## Commands
Explain before running. The test suites use fake servers and make no real calls.
```powershell
.venv\Scripts\python.exe -m unittest discover -p "test_*.py"   # all test_*.py files
cd desktop; npm test                                           # Node tests
.venv\Scripts\python.exe eco_server.py --port 8765             # CLI server; a task run here writes to results/
cd desktop; npm start                                          # desktop app from the repo, uses .venv
cd desktop; npm run dist                                       # build the installer
```
- The packaging tests that need the bundled exe skip until build/eco-backend exists.
- Do not start or stop the user's own servers. For a live check, see Gotchas.

## Measurement and build rules
- THRESHOLD was chosen from data/questions.json.
- data/validation.json and data/validation2.json are held-out sets that have already been used.
  Never tune THRESHOLD or the rules against them.
- data/coding_tasks_heldout.py and data/energy_heldout.py are held-out too. Never tune against them.
- Any new rule or threshold change must be tested on a new, unseen question set.
- Never edit existing data/*.json or results/*.csv files to make numbers look better.
- While building, make no changes to results/*.csv at all.
- Never make a live call to the cloud API without asking me first; the free credits are limited.
- While building or testing, make no real model or cloud calls and run no benchmarks unless I ask.
  Use fake servers, made-up keys and temp folders.

## Git rules
- Commit only when asked. Never push.
- Never add Co-authored-by lines or any Claude attribution to commits.
- Never move, delete or recreate the git tag `pre-hackathon`, and never rewrite history.
  Everything up to that tag is pre-event work; everything after it is hackathon work.
- Do not commit results/router_log.csv, .env files, or *.gguf model files.

## Keys and cloud
- Cloud stays off unless ALLOW_CLOUD=1 is set when the server starts. The desktop app sets it only while it holds a stored key.
- Keys are named, and each has a provider label: Meta, OpenAI, Claude (Anthropic), Gemini (Google) or Other. The label is
  not a secret. In the server keys live in memory only. In the desktop app they are encrypted with safeStorage
  (model-key.enc in the profile) and reach the backend only by POST /api/key.
- The app removes MODEL_API_KEY and ALLOW_CLOUD from the backend's inherited environment.
- A key goes only to a rung that names it. A removed name never falls back to another key.
- One exception: a Meta rung on `https://api.meta.ai` (default port) with no key name uses the only key marked Meta, if
  exactly one is stored. Editing its host, scheme or port ends the exception. A key with any other label is never used automatically.
- A rung on the api.meta.ai host only ever gets a key marked Meta, even when one is named: saving that pairing is refused,
  and it is checked again when the ladder runs. Other rungs accept any named key; the page warns if the provider differs.
- A cloud rung without a usable key is skipped, not failed: "Choose a key"; "No API key set yet" when no key is stored
  or its named key is gone; or "This rung calls Meta, but that key is for <provider>".
- Legacy: router.py, bench_coding.py and the CLI read MODEL_API_KEY if the user set it in their own shell.
  The server treats it as the key named "Default". Never set it or pass a key this way yourself.
- Legacy labels: Default and MODEL_API_KEY count as Meta. Any other key with no label counts as Other.
- Saving refuses a key whose start looks like another provider's (sk-ant-, sk-proj-, AIza, gsk_) under a different known label.
  Other always passes, a bare sk- is not a marker, and no part of the key is ever logged or returned.
- The label is otherwise what the user chose. The app checks only those distinctive prefixes, so a Meta label on an
  unrecognised key still passes and that key can go to Meta.
- Never put keys in code, files, commits, chat, environment variables, arguments, URLs, logs or responses.
  The only file a key may reach is the app's encrypted one. Never read or print the contents of any .env file.
- Cloud endpoint: https://api.meta.ai/v1, model ID `muse-spark-1.3`.
- Cloud calls cost money: count them in the log, and do not raise an output limit without asking me.

## Budget and data locations
Spend is the sum of cost_usd for cloud attempts in that setup's own log, this calendar month. The two budgets are separate.

| | CLI (eco_server.py, eco.py) | Desktop app |
|---|---|---|
| Log | results/eco_log.jsonl | `<profile>/eco/eco_log.jsonl` |
| Verified memory | results/eco_memory.jsonl | `<profile>/eco/eco_memory.jsonl` |
| Settings | user_data/settings.json | `<profile>/eco/settings.json` |
| Keys | memory only | `<profile>/model-key.enc`, encrypted |

- Installed app profile: `%APPDATA%\Eco`. ECO_DESKTOP_USER_DATA overrides the profile; ECO_DATA_DIR overrides the settings folder.
- `--log` and `--memory` on eco_server.py override those paths.
- build_backend.py bundles an explicit list of result CSVs. It never bundles the Eco log, memory, router_log.csv or cloud_calls.csv.

## Decisions that matter
- CLOUD_MAX_TOKENS is 25,000, for openai_compatible rungs only. Reasoning counts against it, and a cut-off answer is paid for and useless.
- Other limits are unchanged and stay small: Meta rungs 3,000, Eco local rungs 500, router.py cloud 1,500.
- router.py is unchanged by the Eco work. Eco reuses its constants.

## Gotchas
- A Windows child process that inherits a stdin pipe hangs while another thread reads that pipe. Checkers use stdin=DEVNULL.
- Use the Edit tool, not shell heredocs, for files with backslashes. Heredocs mangle them.
- For live runs, use a throwaway repo copy and temp profiles, so results/ is never touched.
- Stop only processes you started, by exact PID. Never by name: that can hit the user's own Eco window or servers.
- CRLF warnings from git are harmless.

## Untested
- Real OpenAI, Claude and Gemini providers: only fake servers so far. Presets stay labelled untested.
- SmartScreen on a fresh machine. The installer is not code-signed.
- The default install folder, `%LOCALAPPDATA%\Programs\Eco`: installs were tested in temp folders.

## Working style
- The user is new to this stack. Before running commands, explain what you are about to do in one or two sentences.
- Make small changes, one at a time, and show the diff.
