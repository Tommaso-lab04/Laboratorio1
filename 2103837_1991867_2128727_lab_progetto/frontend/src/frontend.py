
import os
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

BACKEND_URL: str = os.environ.get("BACKEND_URL", "http://backend:8003")
GROUP_MATRICOLE: list[dict] = [
    {"name": "Studente 1", "matricola": "2103837"},
    {"name": "Studente 2", "matricola": "1991867"},
    {"name": "Studente 3", "matricola": "2128727"},
]

_TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Web Parsing Pipeline – Frontend")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


#helper

async def _backend_get(path: str, params: Optional[dict] = None) -> dict:
   
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.get(f"{BACKEND_URL}{path}", params=params)
            if resp.status_code >= 400:
                detail = resp.text
                try:
                    detail = resp.json().get("detail", detail)
                except Exception:
                    pass
                return {"error": f"HTTP {resp.status_code}: {detail}"}
            return resp.json()
        except Exception as exc:
            return {"error": str(exc)}


async def _backend_post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=600.0) as client:
        try:
            resp = await client.post(f"{BACKEND_URL}{path}", json=body)
            if resp.status_code >= 400:
                detail = resp.text
                try:
                    detail = resp.json().get("detail", detail)
                except Exception:
                    pass
                return {"error": f"HTTP {resp.status_code}: {detail}"}
            return resp.json()
        except Exception as exc:
            return {"error": str(exc)}


async def _backend_delete(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.request(
                "DELETE", f"{BACKEND_URL}{path}", json=body,
            )
            if resp.status_code >= 400:
                return {"status": "error", "detail": resp.text}
            return resp.json()
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}


#home

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    domains_data = await _backend_get("/domains")
    domains: list[str] = domains_data.get("domains", []) if "domains" in domains_data else []

    status_data = await _backend_get("/status")
    status = {
        "backend": status_data.get("backend", "error"),
        "database": status_data.get("database", "error"),
        "ollama": status_data.get("ollama", "error"),
    }

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "page": "home",
            "domains": domains,
            "status": status,
            "matricole": GROUP_MATRICOLE,
        },
    )


#parser e eval

@app.get("/parser", response_class=HTMLResponse)
async def parser_page(request: Request):
    domains_data = await _backend_get("/domains")
    domains: list[str] = domains_data.get("domains", []) if "domains" in domains_data else []

    #url del gs
    gs_urls: list[str] = []
    for d in domains:
        r = await _backend_get("/gold_standard_urls", {"domain": d})
        if "gold_standard_urls" in r:
            gs_urls.extend(r["gold_standard_urls"])

    return templates.TemplateResponse(
        "parser.html",
        {
            "request": request,
            "page": "parser",
            "domains": domains,
            "gs_urls": gs_urls,
            "result": None,
            "gs_entry": None,
            "eval_result": None,
            "judge_result": None,
            "error": None,
        },
    )


@app.post("/parser", response_class=HTMLResponse)
async def parser_run(
    request: Request,
    url: str = Form(...),
    mode: str = Form("live"),
):
    domains_data = await _backend_get("/domains")
    domains: list[str] = domains_data.get("domains", []) if "domains" in domains_data else []
    gs_urls: list[str] = []
    for d in domains:
        r = await _backend_get("/gold_standard_urls", {"domain": d})
        if "gold_standard_urls" in r:
            gs_urls.extend(r["gold_standard_urls"])

    use_local = mode == "local"
    parse_result = await _backend_post(
        "/parse", {"url": url, "local": use_local},
    )

    if "error" in parse_result:
        return templates.TemplateResponse(
            "parser.html",
            {
                "request": request,
                "page": "parser",
                "domains": domains,
                "gs_urls": gs_urls,
                "result": None,
                "gs_entry": None,
                "eval_result": None,
                "judge_result": None,
                "error": parse_result["error"],
                "submitted_url": url,
                "mode": mode,
            },
        )

    #se l'URL è nel GS calcolo metriche e judge
    gs_entry = await _backend_get("/gold_standard", {"url": url})
    eval_result = None
    judge_result = None
    if "gold_text" in gs_entry:
        body = {
            "parsed_text": parse_result["parsed_text"],
            "gold_text": gs_entry["gold_text"],
        }
        eval_result = await _backend_post("/evaluate", body)
        judge_result = await _backend_post("/evaluate_judge", body)
    else:
        gs_entry = None

    return templates.TemplateResponse(
        "parser.html",
        {
            "request": request,
            "page": "parser",
            "domains": domains,
            "gs_urls": gs_urls,
            "result": parse_result,
            "gs_entry": gs_entry,
            "eval_result": eval_result if eval_result and "error" not in eval_result else None,
            "judge_result": judge_result if judge_result and "error" not in judge_result else None,
            "error": None,
            "submitted_url": url,
            "mode": mode,
        },
    )


#GS builder

@app.get("/builder", response_class=HTMLResponse)
async def builder_page(
    request: Request, domain: Optional[str] = None, url: Optional[str] = None,
):
    domains_data = await _backend_get("/domains")
    domains: list[str] = domains_data.get("domains", []) if "domains" in domains_data else []
    selected_domain = domain or (domains[0] if domains else "")

    gs_urls: list[str] = []
    if selected_domain:
        r = await _backend_get(
            "/gold_standard_urls", {"domain": selected_domain},
        )
        if "gold_standard_urls" in r:
            gs_urls = r["gold_standard_urls"]

    html_text = ""
    parsed_text = ""
    if url:
        #provo a scaricare l'HTML facendoparse live mostro html_text
        parse = await _backend_post("/parse", {"url": url, "local": False})
        if "error" not in parse:
            html_text = parse.get("html_text", "")
            parsed_text = parse.get("parsed_text", "")

    return templates.TemplateResponse(
        "builder.html",
        {
            "request": request,
            "page": "builder",
            "domains": domains,
            "selected_domain": selected_domain,
            "gs_urls": gs_urls,
            "submitted_url": url or "",
            "html_text": html_text,
            "parsed_text": parsed_text,
            "message": None,
            "error": None,
        },
    )


@app.post("/builder/save", response_class=HTMLResponse)
async def builder_save(
    request: Request,
    domain: str = Form(...),
    url: str = Form(...),
    html_text: str = Form(...),
    gold_text: str = Form(...),
):
    # salvao aggiorna la web_resource e il gold_standard
    add_wr = await _backend_post(
        "/add_web_resource", {"url": url, "html_text": html_text},
    )
    if add_wr.get("status") != "ok":
        error = add_wr.get("detail", "Errore inserimento web_resource")
        return await _builder_redisplay(
            request, domain, url, html_text, "", error=error,
        )
    add_gs = await _backend_post(
        "/add_gold_standard", {"url": url, "gold_text": gold_text},
    )
    if add_gs.get("status") != "ok":
        error = add_gs.get("detail", "Errore inserimento gold_standard")
        return await _builder_redisplay(
            request, domain, url, html_text, "", error=error,
        )

    return RedirectResponse(
        url=f"/builder?domain={domain}", status_code=303,
    )


@app.post("/builder/delete", response_class=HTMLResponse)
async def builder_delete(
    request: Request,
    domain: str = Form(...),
    url: str = Form(...),
):
    await _backend_delete("/gold_standard", {"url": url})
    return RedirectResponse(
        url=f"/builder?domain={domain}", status_code=303,
    )


async def _builder_redisplay(
    request, domain, url, html_text, parsed_text,
    message=None, error=None,
):
    domains_data = await _backend_get("/domains")
    domains = domains_data.get("domains", []) if "domains" in domains_data else []
    r = await _backend_get("/gold_standard_urls", {"domain": domain})
    gs_urls = r.get("gold_standard_urls", []) if "gold_standard_urls" in r else []
    return templates.TemplateResponse(
        "builder.html",
        {
            "request": request,
            "page": "builder",
            "domains": domains,
            "selected_domain": domain,
            "gs_urls": gs_urls,
            "submitted_url": url,
            "html_text": html_text,
            "parsed_text": parsed_text,
            "message": message,
            "error": error,
        },
    )


#stats

@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    stats = await _backend_get("/db_stats")
    schema = await _backend_get("/db_schema")
    return templates.TemplateResponse(
        "stats.html",
        {
            "request": request,
            "page": "stats",
            "stats": stats if "error" not in stats else None,
            "schema": schema if not isinstance(schema, dict) or "error" not in schema else None,
            "error": stats.get("error") if "error" in stats else None,
        },
    )
