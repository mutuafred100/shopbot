"""cPanel without Terminal: run this from  Setup Python App > 'Execute python script'  (enter: diagnose.py).
It shows your Python version, installs anything missing, tries to start the app and prints the reason if it can't."""
import importlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
print("Python:", sys.version.split()[0], "| folder:", HERE, flush=True)

MODULES = ["fastapi", "starlette", "pydantic", "httpx", "openpyxl", "multipart", "a2wsgi"]


def problems():
    out = []
    for m in MODULES:
        try:
            importlib.import_module(m)
        except Exception as e:
            out.append(f"{m}: {type(e).__name__}: {e}")
    return out


def pip_install(req):
    print(f"\n--- installing from {req} (can take a few minutes) ---", flush=True)
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--no-input", "--default-timeout", "60", "-r", req],
                       capture_output=True, text=True)
    print((r.stdout + r.stderr)[-2500:], flush=True)
    return r.returncode == 0


bad = problems()
print("Problems with modules:", bad or "none", flush=True)
if bad and not os.getenv("DIAG_NO_INSTALL"):
    ok = pip_install("requirements-cpanel.txt")
    if not ok:
        print("First install failed. Trying the older-server list...", flush=True)
        ok = pip_install("requirements-cpanel-legacy.txt")
    print("\nInstall result:", "OK" if ok else "FAILED (copy the lines above and send them to your developer)")
    print("Still missing after install:", problems() or "none", flush=True)

print("\n--- trying to start the app ---", flush=True)
log = os.path.join(HERE, "startup_error.log")
if os.path.exists(log):
    os.remove(log)
try:
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.") or n == "passenger_wsgi"]:
        del sys.modules[name]
    import passenger_wsgi  # noqa: F401
    if os.path.exists(log):
        print("The app could NOT start. Reason:\n")
        print(open(log).read()[-2500:])
    else:
        data = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
        print("App started OK. Data folder:", data, "| writable:", os.access(data, os.W_OK))
        print("Next: press Restart on the Python App page, then open your address + /health")
except Exception as e:
    print("Unexpected error:", type(e).__name__, e)
