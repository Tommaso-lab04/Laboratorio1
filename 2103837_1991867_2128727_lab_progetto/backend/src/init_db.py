#script di inizializzazione del DB.
import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from src import database as db
from src import llm_judge
from src.evaluation import evaluate_all
from src import (
    parser_wikipedia,
    parser_microsoft,
    parser_pubmed,
    parser_investing,
)
from src.parser_base import get_parser

logger = logging.getLogger(__name__)

_GS_DIR = Path(os.environ.get("GS_DATA_PATH", "/app/gs_data"))
_DOMAINS_FILE = Path(os.environ.get("DOMAINS_FILE", "/app/domains.json"))


def _load_supported_domains() -> list[str]:
    if not _DOMAINS_FILE.exists():
        return []
    with open(_DOMAINS_FILE, encoding="utf-8") as f:
        return json.load(f).get("domains", [])


def _gs_file(domain: str) -> Path:
    return _GS_DIR / f"{domain}_gs.json"


async def _parse_for_domain(domain: str, url: str, html_text: str) -> dict | None:
    #seleziona il parser corretto
    #ritorna None se errore
    parser = get_parser(url) or get_parser(domain)
    if parser is None:
        logger.warning("Nessun parser registrato per dominio %s", domain)
        return None
    try:
        return await parser.parse(url, html_text=html_text)
    except Exception as exc:
        logger.warning("Parser %s ha fallito su %s: %s", domain, url, exc)
        return None


def _populate_from_json() -> list[dict]:
    #inserisce i record dei file gs_data/*_gs.json nel DB
    #ritorna la lista delle entry inserite (dict url/domain/title/html_text/gold_text)
    inserted: list[dict] = []
    domains = _load_supported_domains()
    for domain in domains:
        path = _gs_file(domain)
        if not path.exists():
            logger.info("Nessun file GS per dominio %s (%s)", domain, path)
            continue
        try:
            with open(path, encoding="utf-8") as f:
                entries = json.load(f)
        except Exception as exc:
            logger.error("Impossibile leggere %s: %s", path, exc)
            continue
        if not isinstance(entries, list):
            logger.error("Formato non valido per %s (atteso lista)", path)
            continue

        for entry in entries:
            url = (entry.get("url") or "").strip()
            if not url:
                continue
            title = entry.get("title") or ""
            html_text = entry.get("html_text") or ""
            gold_text = entry.get("gold_text") or ""
            db.upsert_web_resource(url, domain, title, html_text)

            #gold_standard solo se gold_text è valido
            if gold_text.strip() and not gold_text.strip().upper().startswith("TODO"):
                db.upsert_gold_standard(url, gold_text)

            inserted.append({
                "url": url,
                "domain": domain,
                "title": title,
                "html_text": html_text,
                "gold_text": gold_text,
            })

        logger.info("Caricati %d record da %s", len(entries), path.name)
    return inserted


async def _precompute_metrics(entries: list[dict]) -> None:
    for entry in entries:
        url = entry["url"]
        domain = entry["domain"]
        gold_text = entry["gold_text"]
        html_text = entry["html_text"]

        if not gold_text.strip() or gold_text.strip().upper().startswith("TODO"):
            continue
        if not html_text.strip():
            continue

        parsed = await _parse_for_domain(domain, url, html_text)
        if parsed is None:
            continue

        try:
            metrics = evaluate_all(parsed["parsed_text"], gold_text)
            db.upsert_evaluation(url, metrics)
        except Exception as exc:
            logger.warning("Eval fallita per %s: %s", url, exc)

    logger.info("Pre-calcolo metriche completato")


def _run_judge_in_background(entries: list[dict]) -> None:
    #esegue il judge LLM su tutte le entry in un thread di background
    def _worker():
        logger.info("Background judge: attendo Ollama...")
        if not llm_judge.wait_for_ollama(max_attempts=120, delay_s=3.0):
            logger.warning("Ollama non disponibile, salto la fase judge")
            return 
        # Assicuriamoci che il modello sia presente
        if not llm_judge.model_available(llm_judge.JUDGE_MODEL):
            logger.info("Pull del modello %s...", llm_judge.JUDGE_MODEL)
            llm_judge.pull_model(llm_judge.JUDGE_MODEL)

        for entry in entries:
            url = entry["url"]
            domain = entry["domain"]
            gold_text = entry["gold_text"]
            html_text = entry["html_text"]

            if not gold_text.strip() or gold_text.strip().upper().startswith("TODO"):
                continue
            if not html_text.strip():
                continue

            #se il giudizio è già nel DB salto
            if db.get_judge_evaluation(url) is not None:
                continue

            #reparsing
            try:
                parsed = asyncio.run(_parse_for_domain(domain, url, html_text))
            except Exception as exc:
                logger.warning("Re-parsing fallito per %s: %s", url, exc)
                continue
            if parsed is None:
                continue

            try:
                judge = llm_judge.judge_eval(parsed["parsed_text"], gold_text)
                db.upsert_judge_evaluation(
                    url,
                    judge["model_name"],
                    judge["judge_score"],
                    judge["judge_feedback"],
                )
                logger.info(
                    "Judge OK %s -> score=%d", url, judge["judge_score"]
                )
            except Exception as exc:
                logger.warning("Judge fallito per %s: %s", url, exc)

        logger.info("Background judge: completato")

    t = threading.Thread(target=_worker, name="judge-worker", daemon=True)
    t.start()


def initialize() -> None:
    #procedura di inizializzazione completa
    logger.info("Inizializzazione database...")
    db.wait_for_db()
    db.init_schema()

    entries = _populate_from_json()
    logger.info("Popolamento iniziale completato: %d entry", len(entries))

    #precalcolo metriche
    try:
        asyncio.run(_precompute_metrics(entries))
    except Exception as exc:
        logger.warning("Pre-calcolo metriche interrotto: %s", exc)

    #judge in background è lento
    _run_judge_in_background(entries)
    logger.info("Inizializzazione completata, server pronto a servire")
