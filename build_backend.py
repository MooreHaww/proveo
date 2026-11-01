"""Build the self-contained Windows backend, build/eco-backend/eco-backend.exe, with PyInstaller.

Everything goes under build/ (git-ignored), and PyInstaller lives in its own virtualenv there, so
the project's .venv is not changed. `npm run dist` in desktop/ runs this first.
Run with: python build_backend.py
"""
import glob
import os
import subprocess
import sys
import sysconfig
import venv

REPO = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(REPO, "build")
ENV = os.path.join(BUILD, "backend-env")
ENV_PYTHON = os.path.join(ENV, "Scripts", "python.exe")
SPEC = os.path.join(BUILD, "eco-backend.spec")
REQUIREMENTS = ["requests==2.34.2", "pyinstaller==6.22.3"]

# The measured figures on the Run screen, and the estimate of cloud cost avoided, read only these.
# Never add the spend log, the memory, router_log.csv or cloud_calls.csv: they are personal data.
RESULT_FILES = [f"results/coding_{tier}_coding_tasks_{name}.csv"
                for tier in ("pi", "pc", "cloud_minimal") for name in ("dev", "dev2", "heldout")]

# Not needed by code answers that have tests, and large or unusable here. The middle line does not exist on Windows.
STDLIB_SKIP = {"__hello__", "__phello__", "__pycache__", "_tkinter", "antigravity", "ensurepip", "idle_test", "idlelib", "lib2to3",
               "_crypt", "_curses_panel", "_dbm", "_gdbm", "_posixshmem", "nis", "ossaudiodev", "spwd", "syslog",
               "pydoc_data", "site-packages", "test", "tests", "this", "tkinter", "turtle", "turtledemo", "venv"}


def stdlib_modules():
    """Every standard library module. PyInstaller packs only what it sees imported, and the code being
    checked is not known at build time: a correct answer using heapq or fractions must still run."""
    root = sysconfig.get_path("stdlib")
    names = {n for n in sys.stdlib_module_names if n not in STDLIB_SKIP}
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in STDLIB_SKIP and os.path.exists(os.path.join(folder, d, "__init__.py"))]
        rel = os.path.relpath(folder, root)
        package = [] if rel == "." else rel.split(os.sep)
        for name in files:
            stem, ext = os.path.splitext(name)
            if ext != ".py" or stem == "__main__" or stem in STDLIB_SKIP:
                continue
            names.add(".".join(package if stem == "__init__" else package + [stem]))
    names.discard("")
    return sorted(names)


def data_files():
    files = [(os.path.join(REPO, "web"), "web")]
    files += [(path, "data") for path in sorted(glob.glob(os.path.join(REPO, "data", "coding_tasks_*.py")))]
    for rel in RESULT_FILES:
        path = os.path.join(REPO, *rel.split("/"))
        if not os.path.exists(path):
            sys.exit(f"Missing {rel}: the measured figures need it.")
        files.append((path, "results"))
    return files


def write_spec():
    text = f"""\
a = Analysis([{os.path.join(REPO, "eco_backend.py")!r}], pathex=[{REPO!r}], datas={data_files()!r},
             hiddenimports=['eco_server', 'simulate_router'] + {stdlib_modules()!r}, excludes=['matplotlib', 'numpy', 'tkinter'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='eco-backend', console=True, upx=False,
          icon={os.path.join(REPO, "assets", "icon.ico")!r})
coll = COLLECT(exe, a.binaries, a.datas, name='eco-backend', upx=False)
"""
    with open(SPEC, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    os.makedirs(BUILD, exist_ok=True)
    if not os.path.exists(ENV_PYTHON):
        venv.create(ENV, with_pip=True)
    subprocess.run([ENV_PYTHON, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *REQUIREMENTS], check=True)
    write_spec()
    subprocess.run([ENV_PYTHON, "-m", "PyInstaller", SPEC, "--noconfirm", "--clean", "--log-level", "WARN",
                    "--distpath", BUILD, "--workpath", os.path.join(BUILD, "backend-work")], check=True, cwd=REPO)
    print("Built", os.path.join(BUILD, "eco-backend", "eco-backend.exe"))


if __name__ == "__main__":
    main()
