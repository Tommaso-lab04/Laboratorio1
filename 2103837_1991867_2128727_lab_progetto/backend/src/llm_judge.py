#modulo per la valutazione LLM-as-Judge tramite Ollama
#tutta l'interazione con ollama avviene via HTTP sulla porta 11434
import json
import logging
import os
import re
import time
from typing import Optional
import httpx
logger = logging.getLogger(__name__)
#URL del server ollama
OLLAMA_URL: str = os.environ.get("OLLAMA_URL", "http://ollama:11434")
JUDGE_MODEL: str = os.environ.get("JUDGE_MODEL", "llama3.2:3b")
_MAX_CHARS: int = int(os.environ.get("JUDGE_MAX_CHARS", "500"))
def health_check(timeout: float = 3.0) -> bool:
    #true se Ollama risponde su /api/version
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/version", timeout=timeout)
        return r.status_code == 200
    except Exception as exc:
        logger.debug("Ollama health check fallito: %s", exc)
        return False


def list_models(timeout: float = 5.0) -> list[str]:
    #lista dei modelli installati su ollama
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception as exc:
        logger.warning("Impossibile elencare modelli Ollama: %s", exc)
        return []


def model_available(model_name: str = JUDGE_MODEL) -> bool:
    return any(m.startswith(model_name) for m in list_models())
def pull_model(model_name: str = JUDGE_MODEL, timeout: float = 600.0) -> bool:
    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST",
                f"{OLLAMA_URL}/api/pull",
                json={"name": model_name, "stream": False},
            ) as resp:
                resp.raise_for_status()
                #consumiamo lo stream per non lasciare la connessione aperta
                for _ in resp.iter_lines():
                    pass
        return True
    except Exception as exc:
        logger.warning("Pull modello %s fallito: %s", model_name, exc)
        return False


def wait_for_ollama(max_attempts: int = 60, delay_s: float = 2.0) -> bool:
    #attende che ollama risponda
    for attempt in range(1, max_attempts + 1):
        if health_check():
            logger.info("Ollama raggiungibile dopo %d tentativi", attempt)
            return True
        time.sleep(delay_s)
    logger.warning("Ollama non raggiungibile dopo %d tentativi", max_attempts)
    return False


#prompt

_JUDGE_PROMPT_TEMPLATE: str = """Sei un valutatore esperto di parser web. Devi confrontare un \
testo estratto automaticamente da un parser con un Gold Standard fatto a mano \
e dare un giudizio di qualità.

Confronta i due testi e valuta se il parser ha estratto correttamente il \
contenuto informativo principale (titolo + corpo dell'articolo) e se ha \
escluso boilerplate (menu, footer, banner, cookie, link di navigazione).

Rispondi SOLO con un oggetto JSON valido, senza testo prima o dopo. \
Il formato deve essere ESATTAMENTE:
{{
  "score": <intero da 1 a 5>,
  "feedback": "<breve descrizione della qualità del testo, 1-3 frasi>"
}}

Significato dei punteggi:
1 = pessimo (manca quasi tutto il contenuto, oppure pieno di boilerplate)
2 = scarso (contenuto parziale, molto rumore)
3 = medio (contenuto principale presente ma omissioni o boilerplate residui)
4 = buono (testo quasi completo, poco rumore)
5 = ottimo (estrazione fedele al Gold Standard)

Testo estratto dal parser:
\"\"\"
{parsed_text}
\"\"\"

Testo di riferimento (Gold Standard):
\"\"\"
{gold_text}
\"\"\"
"""


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[...troncato...]"


def _build_prompt(parsed_text: str, gold_text: str) -> str:
    return _JUDGE_PROMPT_TEMPLATE.format(
        parsed_text=_truncate(parsed_text or "", _MAX_CHARS),
        gold_text=_truncate(gold_text or "", _MAX_CHARS),
    )


#json parsing

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> Optional[dict]:
    #prova a estrarre il primo blocco jsonvalido dalla risposta del modello
    if not text:
        return None
    #prova diretto
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    #cerca il primo { ... } e prova a parsare
    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        #tentativo di pulizia rimuove virgole
        cleaned = re.sub(r",\s*}", "}", match.group(0))
        cleaned = re.sub(r",\s*]", "]", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None
def _normalize_score(value) -> int:
    #riporta lo score nell'intervallo 1-5 e lo converte a int
    try:
        s = int(round(float(value)))
    except (TypeError, ValueError):
        return 1
    return max(1, min(5, s))
#chiamata principale
def judge_eval(
    parsed_text: str,
    gold_text: str,
    model_name: str = JUDGE_MODEL,
    timeout: float = 90.0,
) -> dict:
    # Esegue una valutazione LLM-as-judge
    if not health_check(timeout=3.0):
        return {
            "model_name": model_name,
            "judge_score": 1,
            "judge_feedback": (
                "Ollama non raggiungibile. Controlla che il container "
                "'parsing_ollama' sia in esecuzione."
            ),
            "raw_response": "",
        }
    if not model_available(model_name):
        logger.info(
            "Modello %s non presente su Ollama, avvio pull on-demand", model_name
        )
        pulled = pull_model(model_name, timeout=900.0)
        if not pulled or not model_available(model_name):
            return {
                "model_name": model_name,
                "judge_score": 1,
                "judge_feedback": (
                    f"Modello '{model_name}' non ancora disponibile su Ollama. "
                    "Il download è probabilmente in corso (può richiedere alcuni "
                    "minuti al primo avvio). Riprova tra poco, oppure scaricalo "
                    f"manualmente con: docker exec parsing_ollama ollama pull {model_name}"
                ),
                "raw_response": "",
            }

    prompt = _build_prompt(parsed_text, gold_text)

    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        #forziamo il modello a rispondere in JSON pk supportato da Ollama
        "format": "json",
        "options": {
            "temperature": 0.0,
            "num_ctx": 2048,
            "num_predict": 256,
        },
    }

    raw_response: str = ""
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        raw_response = data.get("response", "") or ""
    except httpx.HTTPStatusError as exc:
        #404 qui significa modello non trovato anche se il pre-check ha detto ok
        status = exc.response.status_code if exc.response is not None else "?"
        if status == 404:
            feedback = (
                f"Modello '{model_name}' non trovato su Ollama. "
                f"Esegui: docker exec parsing_ollama ollama pull {model_name}"
            )
        else:
            feedback = f"Errore HTTP {status} nella chiamata al modello: {exc}"
        logger.warning(feedback)
        return {
            "model_name": model_name,
            "judge_score": 1,
            "judge_feedback": feedback,
            "raw_response": "",
        }
    except Exception as exc:
        logger.warning("Chiamata al judge fallita: %s", exc)
        return {
            "model_name": model_name,
            "judge_score": 1,
            "judge_feedback": f"Errore nella chiamata al modello: {exc}",
            "raw_response": "",
        }

    parsed = _extract_json(raw_response)
    if parsed is None:
        #fallback il modello non ha rispettato il formato json
        logger.warning("Risposta judge non in JSON, applico fallback. Raw: %r",
                       raw_response[:200])
        return {
            "model_name": model_name,
            "judge_score": 1,
            "judge_feedback": (
                "Il modello non ha restituito un JSON valido. "
                f"Risposta grezza (troncata): {raw_response[:200]}"
            ),
            "raw_response": raw_response,
        }

    score = _normalize_score(parsed.get("score", 1))
    feedback = str(parsed.get("feedback", "")).strip()
    if not feedback:
        feedback = "Nessun feedback fornito dal modello."

    return {
        "model_name": model_name,
        "judge_score": score,
        "judge_feedback": feedback,
        "raw_response": raw_response,
    }
