#modulo per leggere la lista dei domini supportati e per accedere ai file
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse
_GS_DIR = Path(os.environ.get("GS_DATA_PATH", "/app/gs_data"))
_DOMAINS_FILE = Path(os.environ.get("DOMAINS_FILE", "/app/domains.json"))
def _gs_file_for_domain(domain: str) -> Path:
    return _GS_DIR / f"{domain}_gs.json"
def normalize_url(url: str) -> str:
    #normalizzo l'URL per il confronto
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower() or "https"
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/") if parsed.path not in ("", "/") else parsed.path
    normalized = f"{scheme}://{host}{path}"
    if parsed.query:
        normalized += f"?{parsed.query}"
    return normalized


def list_supported_domains() -> list[str]:
    #lista dei domini supportati letta da domains.json
    if not _DOMAINS_FILE.exists():
        return []
    with open(_DOMAINS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("domains", [])


def load_gold_standard_file(domain: str) -> list[dict]:
    #carica il GS dal file JSON
    gs_file = _gs_file_for_domain(domain)
    if not gs_file.exists():
        raise ValueError(f"Nessun Gold Standard trovato per il dominio '{domain}'")

    with open(gs_file, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"Il file GS per '{domain}' non è una lista JSON valida")

    return data
