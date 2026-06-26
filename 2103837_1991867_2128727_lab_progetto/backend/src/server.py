#web server FastAPI con tutti gli endpoint del progetto finale
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from src import database as db
from src import init_db
from src import llm_judge
from src.evaluation import evaluate_all
from src.gold_standard import list_supported_domains, normalize_url
from src import (
    parser_wikipedia,
    parser_microsoft,
    parser_pubmed,
    parser_investing,
)
from src.parser_base import get_parser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
#inizializzazione DB all'avvio
@asynccontextmanager
async def lifespan(app: FastAPI):
    # attendo che MariaDB sia raggiungibile creo lo schema popolo web_resources e gold_standard dai JSON pre-calcolo le metriche di parsing
    try:
        await asyncio.to_thread(db.wait_for_db)
        await asyncio.to_thread(db.init_schema)
        entries = await asyncio.to_thread(init_db._populate_from_json)
        logger.info("Popolamento iniziale completato: %d entry", len(entries))
        await init_db._precompute_metrics(entries)
        #judge LLM in background non blocca lo startup
        init_db._run_judge_in_background(entries)
    except Exception as exc:
        logger.error("Inizializzazione fallita: %s", exc)

    yield


app = FastAPI(
    title="Web Parsing Pipeline API",
    description="API REST per parsing, Gold Standard, valutazione e LLM-as-Judge.",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
class ParsedDocument(BaseModel):
    url: str
    domain: str
    title: str
    html_text: str
    parsed_text: str
class ParseRequest(BaseModel):
    url: str
    local: Optional[bool] = False
class GoldStandardEntry(BaseModel):
    url: str
    domain: str
    title: str
    html_text: str
    gold_text: str
class DomainsResponse(BaseModel):
    domains: list[str]
class GoldStandardUrlsResponse(BaseModel):
    gold_standard_urls: list[str]
class EvaluateRequest(BaseModel):
    parsed_text: str
    gold_text: str
class TokenLevelMetrics(BaseModel):
    precision: float
    recall: float
    f1: float
class EvaluateResponse(BaseModel):
    token_level_eval: TokenLevelMetrics
    char_overlap_eval: dict = Field(default_factory=dict)
    bleu1_eval: dict = Field(default_factory=dict)
class FullGsEvalResponse(BaseModel):
    token_level_eval: TokenLevelMetrics
    judge_score: float
    char_overlap_eval: dict = Field(default_factory=dict)
    bleu1_eval: dict = Field(default_factory=dict)
class JudgeResponse(BaseModel):
    model_name: str
    judge_score: int
    judge_feedback: str
class AddWebResourceRequest(BaseModel):
    url: str
    html_text: str
class AddGoldStandardRequest(BaseModel):
    url: str
    gold_text: str
class DeleteRequest(BaseModel):
    url: str
class StatusResponse(BaseModel):
    backend: str
    database: str
    ollama: str
#helper domini
def _host_of(url: str) -> str:
    return urlparse(url).netloc.lower()
def _match_domain(host: str, supported: str) -> bool:
    return host == supported or host.endswith("." + supported)
def _resolve_supported_domain(url_or_host: str) -> Optional[str]:
    host = url_or_host.lower()
    if "://" in url_or_host:
        host = _host_of(url_or_host)
    for supported in list_supported_domains():
        if _match_domain(host, supported):
            return supported
    #match diretto se l'utente passa il dominio puro
    if url_or_host in list_supported_domains():
        return url_or_host
    return None
async def _run_parser(url: str, html_text: Optional[str] = None) -> dict:
    #seleziona il parser competente
    parser = get_parser(url)
    if parser is None:
        msg = f"Dominio non supportato. Domini validi: {list_supported_domains()}"
        raise HTTPException(status_code=400, detail=msg)

    try:
        return await parser.parse(url, html_text=html_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
        #endpoints
@app.post("/parse", response_model=ParsedDocument)
async def parse_endpoint(body: ParseRequest):
    #esegue il parser Se local=true prende l'HTML dal DB invece di scaricarlo
    resolved = _resolve_supported_domain(body.url)
    if resolved is None:
        raise HTTPException(
            status_code=400,
            detail=f"Dominio non supportato. Validi: {list_supported_domains()}",
        )

    html_text: Optional[str] = None
    if body.local:
        wr = db.get_web_resource(body.url)
        if wr is None:
            raise HTTPException(
                status_code=404,
                detail=f"URL non presente nel DB: {body.url}",
            )
        html_text = wr["html_text"]
        if not (html_text or "").strip():
            raise HTTPException(
                status_code=422,
                detail=f"HTML vuoto nel DB per {body.url}, impossibile parsare in modalità local.",
            )

    result = await _run_parser(body.url, html_text=html_text)
    return ParsedDocument(**result)
@app.get("/domains", response_model=DomainsResponse)
def domains_endpoint():
    return DomainsResponse(domains=list_supported_domains())
@app.get("/gold_standard", response_model=GoldStandardEntry)
def gold_standard_endpoint(url: str):
    entry = db.get_gold_standard_entry(url)
    if entry is None:
        #fallback provo a normalizzare l'URL e ricerco
        normalized = normalize_url(url)
        entry = db.get_gold_standard_entry(normalized)
    if entry is None:
        raise HTTPException(status_code=404, detail="URL non presente nel GS.")
    return GoldStandardEntry(**entry)
@app.get("/gold_standard_urls", response_model=GoldStandardUrlsResponse)
def gold_standard_urls_endpoint(domain: str):
    resolved = _resolve_supported_domain(domain)
    if resolved is None:
        raise HTTPException(status_code=400, detail="Dominio non supportato.")
    urls = db.list_gold_standard_urls(resolved)
    return GoldStandardUrlsResponse(gold_standard_urls=urls)
@app.post("/evaluate", response_model=EvaluateResponse)
def evaluate_endpoint(body: EvaluateRequest):
    metrics = evaluate_all(body.parsed_text, body.gold_text)
    return EvaluateResponse(**metrics)
@app.post("/evaluate_judge", response_model=JudgeResponse)
async def evaluate_judge_endpoint(body: EvaluateRequest):
    #llm_judge.judge_eval è sincrono per non bloccare l'event loop lo eseguo in un thread separato
    result = await asyncio.to_thread(
        llm_judge.judge_eval, body.parsed_text, body.gold_text,
    )
    return JudgeResponse(
        model_name=result["model_name"],
        judge_score=result["judge_score"],
        judge_feedback=result["judge_feedback"],
    )
@app.get("/full_gs_eval", response_model=FullGsEvalResponse)
async def full_gs_eval_endpoint(domain: str):
    resolved = _resolve_supported_domain(domain)
    if resolved is None:
        raise HTTPException(status_code=400, detail="Dominio non supportato.")

    entries = db.list_gold_standard_entries(resolved)
    if not entries:
        raise HTTPException(
            status_code=422, detail=f"Nessuna entry valutabile per {resolved}"
        )

    #eseguo i parser su HTML statico salvato nel DB e calcolo metriche e judge
    metrics_list: list[dict] = []
    judge_scores: list[int] = []

    async def _process(entry: dict) -> Optional[dict]:
        url = entry["url"]
        gold_text = entry["gold_text"]
        html_text = entry["html_text"]
        if not gold_text.strip() or not html_text.strip():
            return None
        #salta entry con HTML troppo grande>1MB
        if len(html_text) > 1_000_000:
            return None
        try:
            parsed = await _run_parser(url, html_text=html_text)
        except HTTPException:
            return None

        m = evaluate_all(parsed["parsed_text"], gold_text)

        #uso solo il judge gia' pre-calcolato in DB
        je = db.get_judge_evaluation(url)
        m["_judge_score"] = je["judge_score"] if je is not None else 0
        return m

    results = await asyncio.gather(*[_process(e) for e in entries])
    for r in results:
        if r is None:
            continue
        score = r.pop("_judge_score", 0)
        if score:
            judge_scores.append(score)
        metrics_list.append(r)

    if not metrics_list:
        raise HTTPException(status_code=422, detail="Nessuna entry valutabile.")

    #media delle metriche numeriche
    n = len(metrics_list)
    tle_keys = ("precision", "recall", "f1")
    avg_tle = {
        k: round(
            sum(m["token_level_eval"][k] for m in metrics_list) / n, 4
        ) for k in tle_keys
    }

    avg_char: dict = {}
    if metrics_list[0].get("char_overlap_eval"):
        char_keys = ("char_precision", "char_recall", "char_f1")
        avg_char = {
            k: round(
                sum(m["char_overlap_eval"][k] for m in metrics_list) / n, 4
            ) for k in char_keys
        }

    avg_bleu: dict = {}
    if metrics_list[0].get("bleu1_eval"):
        avg_bleu = {
            "bleu1": round(sum(m["bleu1_eval"]["bleu1"] for m in metrics_list) / n, 4)
        }

    avg_judge = round(sum(judge_scores) / len(judge_scores), 4) if judge_scores else 0.0

    return FullGsEvalResponse(
        token_level_eval=TokenLevelMetrics(**avg_tle),
        char_overlap_eval=avg_char,
        bleu1_eval=avg_bleu,
        judge_score=avg_judge,
    )


#add e dalate

@app.post("/add_web_resource")
def add_web_resource_endpoint(body: AddWebResourceRequest):
    #nessuna restrizione di dominio qualsiasi URL puo' essere aggiunto
    try:
        from urllib.parse import urlparse as _up
        domain = _up(body.url).netloc or ""
        title = ""
        try:
            import re as _re
            m = _re.search(r"<title>(.*?)</title>", body.html_text or "",
                           _re.IGNORECASE | _re.DOTALL)
            if m:
                title = m.group(1).strip()[:500]
        except Exception:
            title = ""
        db.upsert_web_resource(body.url, domain, title, body.html_text)
        return {"status": "ok"}
    except Exception as exc:
        logger.warning("add_web_resource ha fallito: %s", exc)
        return {"status": "error", "detail": str(exc)}


@app.post("/add_gold_standard")
def add_gold_standard_endpoint(body: AddGoldStandardRequest):
    if not db.web_resource_exists(body.url):
        return {
            "status": "error",
            "detail": "URL non presente in web_resources",
        }
    try:
        db.upsert_gold_standard(body.url, body.gold_text)
        return {"status": "ok"}
    except Exception as exc:
        logger.warning("add_gold_standard ha fallito: %s", exc)
        return {"status": "error", "detail": str(exc)}


@app.delete("/web_resource")
def delete_web_resource_endpoint(body: DeleteRequest):
    try:
        ok = db.delete_web_resource(body.url)
        if not ok:
            return {"status": "error", "detail": "URL non presente"}
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@app.delete("/gold_standard")
def delete_gold_standard_endpoint(body: DeleteRequest):
    try:
        if not db.gold_standard_exists(body.url):
            return {"status": "error", "detail": "URL non presente nel GS"}
        ok = db.delete_gold_standard_entry(body.url)
        if not ok:
            return {"status": "error", "detail": "URL non presente nel GS"}
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}
#stats
@app.get("/db_stats")
def db_stats_endpoint():
    web_counts = db.count_web_resources_by_domain()
    gs_counts = db.count_gold_standard_by_domain()
    avg_eval = db.avg_evaluations_by_domain()
    avg_judge = db.avg_judge_by_domain()

    out_avg_eval: dict = {}
    for d, m in avg_eval.items():
        out_avg_eval[d] = m

    out_avg_judge: dict = {}
    for d, j in avg_judge.items():
        out_avg_judge[d] = {"judge_score": j["judge_score"]}
    domains = list_supported_domains()
    for d in domains:
        web_counts.setdefault(d, 0)
        gs_counts.setdefault(d, 0)
        out_avg_judge.setdefault(d, {"judge_score": 0})

    return {
        "web_resources": web_counts,
        "gold_standard": gs_counts,
        "avg_eval": out_avg_eval,
        "avg_eval_judge": out_avg_judge,
    }
@app.get("/db_schema")
def db_schema_endpoint():
    try:
        return db.get_schema_info()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
@app.get("/status", response_model=StatusResponse)
def status_endpoint():
    return StatusResponse(
        backend="ok",
        database="ok" if db.db_health() else "error",
        ollama="ok" if llm_judge.health_check() else "error",
    )
@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}
