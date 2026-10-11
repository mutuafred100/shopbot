"""Entry point for cPanel 'Setup Python App' (Passenger). Not needed for Docker/VPS hosting.
If the app cannot start, the reason is written to startup_error.log in this folder."""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DATA_DIR", os.path.join(HERE, "data"))  # keep this folder OUTSIDE public_html

try:
    from a2wsgi import ASGIMiddleware

    from app import main

    main.startup()  # WSGI does not run FastAPI's startup events, so do it here
    application = ASGIMiddleware(main.app)
except Exception:
    _err = traceback.format_exc()
    try:
        with open(os.path.join(HERE, "startup_error.log"), "w") as _f:
            _f.write(_err)
    except OSError:
        pass

    def application(environ, start_response):
        body = b"The shop assistant could not start. Open startup_error.log in the app folder to see why."
        start_response("503 Service Unavailable", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
        return [body]
