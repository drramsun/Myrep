"""Local preview without Vercel: python webapp/dev_server.py  ->  http://localhost:8787"""
import importlib.util
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_api", os.path.join(HERE, "api", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


APIS = {"/api/picks": _load("picks"), "/api/momentum": _load("momentum")}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def do_GET(self):
        for route, mod in APIS.items():
            if self.path.split("?")[0] == route:
                return mod.handler.do_GET(self)
        if self.path.startswith("/api/"):
            return self.send_error(404)
        return super().do_GET()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8787))
    print(f"Serving on http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
