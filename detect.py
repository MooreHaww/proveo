"""Find local model servers and list their models. Read-only: it never starts or stops anything.

Probes, each with a 1 s timeout:
  Ollama     127.0.0.1:11434  GET /api/tags   (models are then called through its /v1 endpoint)
  LM Studio  127.0.0.1:1234   GET /v1/models
  llama.cpp  127.0.0.1:8080   GET /v1/models
  llama.cpp  127.0.0.1:8081   GET /v1/models
probe_address() checks one given address (a Pi or another machine: http://IP:port).
"""
import json
import urllib.request
from urllib.parse import urlsplit

KNOWN_SERVERS = [
    ("Ollama", 11434, "/api/tags"),
    ("LM Studio", 1234, "/v1/models"),
    ("llama.cpp", 8080, "/v1/models"),
    ("llama.cpp", 8081, "/v1/models"),
]
TIMEOUT = 1.0
MAX_BYTES = 1_000_000
MAX_MODELS = 200


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# No proxy (these are local or LAN addresses) and no redirects (a probe stays where it was sent).
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _get_json(url, timeout):
    try:
        with _opener.open(url, timeout=timeout) as r:
            return json.loads(r.read(MAX_BYTES).decode("utf-8"))
    except Exception:
        return None


def _models(data, path):
    """Model names from an Ollama /api/tags or an OpenAI-style /v1/models reply, or None if neither."""
    if not isinstance(data, dict):
        return None
    items = data.get("models") if path == "/api/tags" else data.get("data")
    if not isinstance(items, list):
        return None
    field = "name" if path == "/api/tags" else "id"
    names = [m.get(field) for m in items if isinstance(m, dict)]
    return [n[:200] for n in names if isinstance(n, str) and n][:MAX_MODELS]


def detect_local(host="127.0.0.1", timeout=TIMEOUT, servers=KNOWN_SERVERS):
    """Servers that answered: [{kind, port, base_url, models}]. Closed ports are skipped."""
    found = []
    for kind, port, path in servers:
        models = _models(_get_json(f"http://{host}:{port}{path}", timeout), path)
        if models is not None:
            found.append({"kind": kind, "port": port, "base_url": f"http://{host}:{port}/v1",
                          "models": models})
    return found


def normalize_address(address):
    """'http://IP:port' (an optional trailing /v1 is allowed). Raises ValueError otherwise."""
    if not isinstance(address, str):
        raise ValueError("address must be a string like http://192.168.1.14:8080")
    parts = urlsplit(address.strip())
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path.rstrip("/") not in ("", "/v1")):
        raise ValueError("address must be like http://192.168.1.14:8080")
    try:
        parts.port
    except ValueError:
        raise ValueError("address has an invalid port")
    return f"{parts.scheme}://{parts.netloc}"


def probe_address(address, timeout=TIMEOUT):
    """Probe one address. Returns {kind, address, base_url, models}, or None if nothing answered."""
    root = normalize_address(address)
    for kind, path in (("Ollama", "/api/tags"), ("OpenAI-compatible", "/v1/models")):
        models = _models(_get_json(root + path, timeout), path)
        if models is not None:
            return {"kind": kind, "address": root, "base_url": root + "/v1", "models": models}
    return None
