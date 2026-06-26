#modulo di valutazione della qualità del parsing
import math
import re
from collections import Counter


def _strip_markdown(text: str) -> str:
    #rimuovo la sintassi markdown prima di valutare
    #immagini markdown
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    #link a file/immagine con testo vuoto
    text = re.sub(r"\[\]\([^)]*\)", "", text)
    #link markdown -> tengo solo il testo
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    #heading a inizio riga
    text = re.sub(r"(?m)^\s*#{1,6}\s+", "", text)
    #bullet di lista a inizio riga
    text = re.sub(r"(?m)^\s*[\*\-\+]\s+", "", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text, flags=re.DOTALL)
    text = re.sub(r"(\*|_)(.+?)\1", r"\2", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text


def _tokenize(text: str) -> list[str]:
    #tokenizzo in parole lowercase
    #prima tolgo il markdown, poi la punteggiatura
    text = _strip_markdown(text)
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return [t for t in text.split() if t.strip()]


def token_level_eval(parsed_text: str, gold_text: str) -> dict[str, float]:
    # Precision = tokens_estratti ∩ tokens_gs / tokens_estratti
    # Recall    = tokens_estratti ∩ tokens_gs / tokens_gs
    # F1        = 2 * P * R / (P + R)
    tokens_estratti: set[str] = set(_tokenize(parsed_text))
    tokens_gs: set[str] = set(_tokenize(gold_text))

    if not tokens_estratti and not tokens_gs:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    if not tokens_estratti or not tokens_gs:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}

    intersection: int = len(tokens_estratti & tokens_gs)
    precision: float = intersection / len(tokens_estratti)
    recall: float = intersection / len(tokens_gs)
    f1: float = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }
#metrica in piu
def character_overlap_eval(parsed_text: str, gold_text: str) -> dict[str, float]:
    #sovrapposizione di caratteri alfanumerici tra output parser e GS
    def char_counter(text: str) -> Counter:
        return Counter(c for c in _strip_markdown(text).lower() if c.isalnum())

    c_parsed = char_counter(parsed_text)
    c_gold = char_counter(gold_text)

    if not c_parsed and not c_gold:
        return {"char_precision": 1.0, "char_recall": 1.0, "char_f1": 1.0}
    if not c_parsed or not c_gold:
        return {"char_precision": 0.0, "char_recall": 0.0, "char_f1": 0.0}

    intersection = sum((c_parsed & c_gold).values())
    precision = intersection / sum(c_parsed.values())
    recall = intersection / sum(c_gold.values())
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return {
        "char_precision": round(precision, 4),
        "char_recall": round(recall, 4),
        "char_f1": round(f1, 4),
    }


#metrica in piu
def bleu1_eval(parsed_text: str, gold_text: str) -> dict[str, float]:
    #penalizza output molto più corti del GS
    hyp_tokens = _tokenize(parsed_text)
    ref_tokens = _tokenize(gold_text)

    if not hyp_tokens:
        return {"bleu1": 0.0}

    ref_counter = Counter(ref_tokens)
    hyp_counter = Counter(hyp_tokens)
    clipped = sum(min(cnt, ref_counter.get(tok, 0)) for tok, cnt in hyp_counter.items())
    precision = clipped / len(hyp_tokens)

    bp = (
        math.exp(1 - len(ref_tokens) / len(hyp_tokens))
        if len(ref_tokens) > 0 and len(hyp_tokens) < len(ref_tokens)
        else 1.0
    )

    return {"bleu1": round(bp * precision, 4)}


def evaluate_all(parsed_text: str, gold_text: str) -> dict:
    #metriche di valutazione nel formato atteso 
    return {
        "token_level_eval": token_level_eval(parsed_text, gold_text),
        "char_overlap_eval": character_overlap_eval(parsed_text, gold_text),
        "bleu1_eval": bleu1_eval(parsed_text, gold_text),
    }


def average_metrics(metrics_list: list[dict]) -> dict:
    #media delle metriche prodotte da evaluate_all sui singoli elementi del GS
    if not metrics_list:
        return {}

    top_keys = list(metrics_list[0].keys())
    result: dict = {}
    for top_key in top_keys:
        inner_keys = list(metrics_list[0][top_key].keys())
        result[top_key] = {
            ik: round(
                sum(m[top_key][ik] for m in metrics_list if top_key in m) / len(metrics_list),
                4,
            )
            for ik in inner_keys
        }
    return result
