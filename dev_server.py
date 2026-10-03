"""Local preview without Vercel: python webapp/dev_server.py  ->  http://localhost:8787"""
import importlib.util
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("picks_api", os.path.join(HERE, "api", "picks.py"))
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def do_GET(self):
        if self.path.startswith("/api/picks"):
            return api.handler.do_GET(self)
        if self.path.startswith("/api/"):
            return self.send_error(404)
        return super().do_GET()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8787))
    print(f"Serving on http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
