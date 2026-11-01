"""Entry point of the bundled Windows backend (built by build_backend.py). The installed desktop app
runs this as proveo-backend.exe; `npm start` and the command line use eco_server.py on a normal Python.

  proveo-backend.exe --eco-check SCRIPT      the code checker's child process, see bench_coding._check_command
  proveo-backend.exe [--watch-stdin] [eco_server.py options]      the server

In the bundle sys.executable is this exe, so there is no `python -I` for the checker to start. The
--eco-check mode stands in for it and is handled before the server is imported.
"""
import sys


def run_check(script):
    """Run a checker script the way `python -I script` does: as __main__, and its folder not importable.

    A frozen Python already ignores PYTHON* variables and user site-packages, and never puts the script's
    folder or the working directory on sys.path. It is not a real sandbox, and neither is `python -I`.
    """
    import runpy
    import traceback

    sys.argv = [script]
    try:
        runpy.run_path(script, run_name="__main__")
    except Exception:
        traceback.print_exc()
        sys.exit(1)


def watch_stdin():
    """Exit when the desktop app goes away for any reason: same watchdog as BACKEND_BOOT in desktop/lib.js."""
    import os
    import threading

    watch = os.dup(0)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    threading.Thread(target=lambda: (os.read(watch, 1), os._exit(0)), daemon=True).start()


def main(argv):
    if argv[:1] == ["--eco-check"]:
        if len(argv) != 2:
            sys.exit("usage: proveo-backend --eco-check SCRIPT")
        run_check(argv[1])
        return
    if argv[:1] == ["--watch-stdin"]:
        watch_stdin()
        argv = argv[1:]
    # PYTHONIOENCODING does not reach a frozen Python, and the server prints file paths.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.argv = ["proveo-backend"] + argv
    import eco_server
    eco_server.main()


if __name__ == "__main__":
    main(sys.argv[1:])
