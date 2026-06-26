# Parser per pmc.ncbi.nlm.nih.gov
import asyncio
import logging
import re
import warnings
from urllib.parse import urlparse
import httpx
from bs4 import BeautifulSoup
from bs4 import XMLParsedAsHTMLWarning
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
#leviamo il warning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

logger = logging.getLogger(__name__)

SUPPORTED_DOMAIN: str = "pmc.ncbi.nlm.nih.gov"
_EUTILS_BASE: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

_SKIP_SECTIONS: frozenset = frozenset({
    "references", "bibliography", "footnotes", "notes",
    "supplementary material", "supplementary data",
    "data availability", "conflict of interest",
    "competing interests",
    "author contributions", "authors' contributions", "authors contributions",
    "author disclosure statement",
    "funding", "funding information", "funding statement",
    "acknowledgments", "acknowledgements",
    "abbreviations", "supporting information",
    "associated data", "publisher's note", "publishers note",
    "actions", "permalink", "resources",
    "similar articles", "cited by other articles",
    "links to ncbi databases", "cite", "add to collections",
})
#headers PMC che indicano l'inizio del contenuto reale dell'articolo
#tutto cio' che appare prima di uno di questi heading e' navigazione
_HTML_START_MARKERS: frozenset = frozenset({
    "abstract", "summary", "introduction", "background",
})
_BROWSER_HEADERS: dict = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4.1 Safari/605.1.15"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/png,image/svg+xml,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}
def _sanitize_url(url: str) -> str:
    #rimuove caratteri whitespace 
    if not url:
        return url
    cleaned = url.strip()
    cleaned = re.sub(r"(?:%0[aAdD9]|%20)+\s*$", "", cleaned)
    cleaned = cleaned.rstrip(" \t\n\r/")
    if re.search(r"/PMC\d+$", cleaned, re.IGNORECASE):
        cleaned += "/"
    return cleaned
def is_supported(url: str) -> bool:
    return SUPPORTED_DOMAIN in urlparse(url).netloc
def _extract_pmc_id(url: str) -> str:
    match = re.search(r'/articles/PMC(\d+)', url, re.IGNORECASE)
    if match:
        return match.group(1)
    raise ValueError(f"Impossibile estrarre PMC ID dall'URL: {url!r}")
def _is_captcha(html: str) -> bool:
    lower = html.lower()
    challenge_markers = [
        "checking your browser before accessing",
        "ddos protection by cloudflare",
        "please complete the security check",
        "g-recaptcha",
        "cf-browser-verification",
        "cf-challenge",
        "_cf_chl_opt",
    ]
    matched = [m for m in challenge_markers if m in lower]
    if matched:
        logger.debug("_is_captcha matched markers: %s", matched)
        return True
    return False
async def _fetch_html_with_crawl4ai(url: str, timeout: float = 60.0) -> str:
    #fallback HTML fetch via browser headless
    # Usato quando httpx riceve una pagina di challenge Cloudflare il vero
    # browser bypassa facilmente queste protezioni perche' esegue JavaScript
    # e supera i fingerprint check
    browser_config = BrowserConfig(headless=True, verbose=False)
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=int(timeout * 1000),
    )
    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await crawler.arun(url=url, config=run_config)
            return result.html or ""
    except Exception as e:
        logger.warning("Fetch via Crawl4AI fallito per %s: %s", url, e)
        return ""


async def _fetch_bioc(pmc_id: str, timeout: float = 30.0) -> str:
    #fetch del full-text via API BioC di NCBI Research
    #https://www.ncbi.nlm.nih.gov/research/bionlp/APIs/BioC-PMC/
    #questa API e' progettata per text mining restituisce JSON strutturato
    #con il testo completo dell'articolo organizzato in sezioni
    #non passa per Cloudflare quindi non viene bloccata come l'HTML pubblico
    bioc_url = (
        f"https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/"
        f"pmcoa.cgi/BioC_json/PMC{pmc_id}/unicode"
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(bioc_url)
            if r.status_code == 200:
                return r.text
            logger.warning(
                "BioC API ha risposto HTTP %d per PMC%s", r.status_code, pmc_id
            )
    except Exception as e:
        logger.warning("Fetch BioC fallito per PMC%s: %s", pmc_id, e)
    return ""


def _parse_bioc_json(bioc_content: str) -> tuple:
    #arse del json bioc estrae titolo e testo strutturato
    import json as _json
    try:
        data = _json.loads(bioc_content)
    except Exception as e:
        logger.warning("BioC JSON parse failed: %s", e)
        return "", ""

    if not isinstance(data, list) or not data:
        return "", ""
    first = data[0]
    documents = first.get("documents", [])
    if not documents:
        return "", ""

    title = ""
    result: list = []
    current_section_header: str = ""

    #szioni da saltare
    bioc_skip = {
        "REF", "ACK_FUND", "AUTH_CONT", "COMP_INT",
        "SUPPL", "ABBR", "APPENDIX",
    }
   
    section_headers = {
        "ABSTRACT": "## Abstract",
        "INTRO": "## Introduction",
        "METHODS": "## Methods",
        "RESULTS": "## Results",
        "DISCUSS": "## Discussion",
        "CONCL": "## Conclusions",
        "CASE": "## Case Report",
        "FIG": None, 
        "TABLE": None,
    }

    for doc in documents:
        for passage in doc.get("passages", []):
            infons = passage.get("infons", {})
            section_type = (infons.get("section_type") or "").upper()
            type_field = (infons.get("type") or "").lower()
            text = (passage.get("text") or "").strip()
            if not text:
                continue
            #titolo dell'articolo
            if section_type == "TITLE" or type_field == "front":
                if not title:
                    title = text
                continue
            #skip sezioni indesiderate
            if section_type in bioc_skip:
                continue
            #aggiungi header solo se cambia sezione e abbiamo un mapping
            if section_type and section_type != current_section_header:
                header = section_headers.get(section_type, f"## {section_type.title()}")
                if header:
                    result.append(header)
                current_section_header = section_type

            #salta titoli di subsezione duplicati
            if type_field == "title":
                result.append(f"### {text}")
            else:
                result.append(text)
    cleaned = "\n\n".join(result)
    return title, cleaned
def _title_from_html(html: str) -> str:
    m = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    if m:
        t = m.group(1)
        t = re.sub(r'\s*[-–]\s*(PMC|PubMed Central|NCBI).*$', '', t, flags=re.IGNORECASE)
        return t.strip()
    return ""
def _process_sec(sec_tag, result: list, level: int = 2) -> None:
    title_tag = sec_tag.find("title", recursive=False)
    heading = title_tag.get_text(strip=True) if title_tag else ""

    if heading.lower() in _SKIP_SECTIONS:
        return

    if heading:
        result.append(f"{'#' * min(level, 6)} {heading}")
    #paragrafi diretti 
    for child in sec_tag.children:
        if not hasattr(child, 'name'):
            continue
        if child.name == "p":
            text = child.get_text(separator=" ", strip=True)
            if text:
                result.append(text)
    #sotto sezioni ricorsive
    for child_sec in sec_tag.find_all("sec", recursive=False):
        _process_sec(child_sec, result, level + 1)


def _parse_jats(xml_content: str) -> tuple:
    #parsing del JATS XML con beautifulsoup
    #ritorna title testo pulito
    err = re.search(r'<[Ee][Rr][Rr][Oo][Rr]>(.*?)</[Ee][Rr][Rr][Oo][Rr]>', xml_content, re.DOTALL)
    if err:
        raise RuntimeError(f"NCBI API errore: {err.group(1).strip()}")
    soup = BeautifulSoup(xml_content, "html.parser")
    #titolo
    title_tag = soup.find("article-title")
    title = title_tag.get_text(separator=" ", strip=True) if title_tag else ""
    result = []
    #abstract
    abstract_tag = soup.find("abstract")
    if abstract_tag:
        result.append("## Abstract")
        for child in abstract_tag.children:
            if not hasattr(child, 'name'):
                continue
            if child.name == "p":
                text = child.get_text(separator=" ", strip=True)
                if text:
                    result.append(text)
            elif child.name == "sec":
                _process_sec(child, result, level=3)
    #corpo dellarticolo
    body_tag = soup.find("body")
    if body_tag is None:
        body_tag = soup.find("sub-article") or soup.find("text")
    if body_tag is None:
        #nessun body trovato ultimo tentativo prendi tutti i <p> del
        #documento esclusi quelli dentro <abstract> e riferimenti bibliografici
        logger.warning("JATS senza <body>: estrazione fallback su tutti i <p>")
        for p in soup.find_all("p"):
            parents = [a.name for a in p.parents if hasattr(a, "name")]
            if "abstract" in parents or "ref-list" in parents or "back" in parents:
                continue
            text = p.get_text(separator=" ", strip=True)
            if text and len(text) > 20:
                result.append(text)

    if body_tag:
        #conta quanti caratteri di testo abbiamo prima di processare il body
        #cosi' a fine possiamo capire se le strategie sec hanno estratto
        #qualcosa o se serve il fallback aggressivo
        result_len_before_body = sum(len(s) for s in result)

        #sec figli diretti
        direct_secs = body_tag.find_all("sec", recursive=False)
        if direct_secs:
            for sec in direct_secs:
                _process_sec(sec, result, level=2)
        else:
            #sec top-level a profondità qualsiasi
            all_secs = body_tag.find_all("sec")
            top_level = [
                s for s in all_secs
                if not any(p.name == "sec" for p in s.parents if p is not body_tag)
            ]
            for sec in top_level:
                _process_sec(sec, result, level=2)

        #paragrafi diretti del body
        for child in body_tag.children:
            if hasattr(child, "name") and child.name == "p":
                text = child.get_text(separator=" ", strip=True)
                if text:
                    result.append(text)

        #Fallback aggressivo se le strategie prima non hanno aggiunto
        #niente al risultato  estraiamo tutti i <p> presenti nel body
        result_len_after_body = sum(len(s) for s in result)
        if result_len_after_body == result_len_before_body:
            logger.warning(
                "JATS body senza <sec>/<p> top-level riconosciuti, "
                "fallback su tutti i <p> nidificati"
            )
            for p in body_tag.find_all("p"):
                text = p.get_text(separator=" ", strip=True)
                if text and len(text) > 20:  # scarta paragrafi spuri brevissimi
                    result.append(text)
    cleaned = "\n\n".join(line for line in result if line.strip())
    return title, cleaned
def _parse_html_web(html_content: str) -> tuple:
    #estrae titolo e testo dall'HTML pubblico di pmc.ncbi.nlm.nih.gov.
    soup = BeautifulSoup(html_content, "html.parser")
    title = _title_from_html(html_content)
    result: list = []
    found_start = False
    skip_current_section = False
    for tag in soup.find_all(['h2', 'h3', 'p']):
        text = tag.get_text(separator=" ", strip=True)
        if not text:
            continue
        #normalizza apostrofi unicodee virgolette per confronto
        normalized = text.lower().replace("\u2019", "'").replace("\u2018", "'")

        if tag.name in ['h2', 'h3']:
            #cerco l'inizio del contenuto reale
            if not found_start:
                if normalized in _HTML_START_MARKERS:
                    found_start = True
                    skip_current_section = False
                else:
                    #prearticolo skip
                    skip_current_section = True
                    continue

            #sezione da saltare
            if normalized in _SKIP_SECTIONS:
                skip_current_section = True
                continue

            #sezione valida disattiva skip
            skip_current_section = False
            level = 2 if tag.name == 'h2' else 3
            if len(text) > 2:
                result.append(f"{'#' * level} {text}")

        elif tag.name == 'p':
            #prima che il contenuto reale inizi tutti i <p> sono boilerplate
            if not found_start:
                continue
            if not skip_current_section and len(text) > 20:
                #scarta paragrafi dentro nav/footer o nel menu di navigazione PMC
                parents = [p.name for p in tag.parents if hasattr(p, 'name')]
                parent_classes = []
                for p in tag.parents:
                    if hasattr(p, 'get'):
                        cl = p.get('class')
                        if cl:
                            parent_classes.extend(cl)
                if ('nav' not in parents and 'footer' not in parents
                        and 'jig-ncbiinpagenav' not in parent_classes):
                    result.append(text)

    cleaned = "\n\n".join(result)
    return title, cleaned


async def parse_pubmed_page(url: str, html_text: str | None = None) -> dict:
    # rimuove spazi e altri caratteri di whitespace che spesso finiscono accidentalmente nell'URL via copy paste dal browser
    url = _sanitize_url(url)
    if not is_supported(url):
        raise ValueError(f"Dominio non supportato: {urlparse(url).netloc!r}")

    pmc_id = _extract_pmc_id(url)
    logger.info("parse_pubmed_page: PMC%s", pmc_id)
    #localmode
    if html_text is not None:
        #Marker che distinguono JATS XML da HTML web
        sample = html_text[:2000].lower()
        is_xml_jats = (
            "<?xml" in sample
            or "<!doctype pmc-articleset" in sample
            or "<pmc-articleset" in sample
            or ("<article " in sample and "<front>" in sample)
        )
        try:
            if is_xml_jats:
                logger.info("Local mode PMC%s: input rilevato come JATS XML", pmc_id)
                title_xml, parsed_text = _parse_jats(html_text)
            else:
                logger.info("Local mode PMC%s: input rilevato come HTML web", pmc_id)
                title_xml, parsed_text = _parse_html_web(html_text)
        except Exception as e:
            logger.warning("Errore parsing Local PMC%s: %s", pmc_id, e)
            #anche in caso di errore non andiamo live ritorniamo vuoto
            #per non sovrascrivere l'HTML utente con quello scaricato dal web
            title_xml, parsed_text = "", ""

        #fallback Bioc anche in LOCAL mode son capitati erorri
        if len(parsed_text) < 3000:
            logger.info(
                "Local PMC%s: JATS/HTML in DB ha dato solo %d char. "
                "Provo fallback BioC NCBI...",
                pmc_id, len(parsed_text),
            )
            try:
                bioc_content = await _fetch_bioc(pmc_id)
                if bioc_content:
                    bioc_title, bioc_text = _parse_bioc_json(bioc_content)
                    if len(bioc_text) > len(parsed_text):
                        logger.info(
                            "Local PMC%s: BioC ha dato %d char (vs %d), uso BioC",
                            pmc_id, len(bioc_text), len(parsed_text),
                        )
                        parsed_text = bioc_text
                        if not title_xml and bioc_title:
                            title_xml = bioc_title
            except Exception as e:
                logger.warning(
                    "Local PMC%s: fallback BioC fallito: %s (resto con parsed corto)",
                    pmc_id, e,
                )

        title = _title_from_html(html_text) or title_xml
        logger.info(
            "Local mode PMC%s: parsed_len=%d, title=%r",
            pmc_id, len(parsed_text), title[:60],
        )
        return {
            "url": url,
            "domain": SUPPORTED_DOMAIN,
            "title": title,
            "html_text": html_text,
            "parsed_text": parsed_text,
        }

    #live

    api_url = f"{_EUTILS_BASE}/efetch.fcgi?db=pmc&id={pmc_id}&rettype=xml&retmode=xml"
    html_text = ""
    title = ""

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:

            #HTML grezzo 
            try:
                html_resp = await client.get(url, headers=_BROWSER_HEADERS)
                html_text = html_resp.text
                if not _is_captcha(html_text):
                    title = _title_from_html(html_text)
                logger.info(
                    "HTML fetch: %d chars, captcha=%s, snippet=%r",
                    len(html_text), _is_captcha(html_text),
                    html_text[:300].replace("\n", " "),
                )
            except Exception as e:
                logger.warning("HTML fetch fallita per %s: %s", url, e)
                html_text = ""

   
            try:
                xml_resp = await client.get(api_url)
                xml_resp.raise_for_status()
                xml_content = xml_resp.text
                logger.debug("XML fetch: %d chars", len(xml_content))
            except httpx.HTTPStatusError as e:
                raise RuntimeError(
                    f"NCBI API HTTP {e.response.status_code} per PMC{pmc_id}"
                ) from e
            except httpx.RequestError as e:
                raise RuntimeError(
                    f"Rete non raggiungibile per NCBI API (PMC{pmc_id}): {e}"
                ) from e

    except (ValueError, RuntimeError):
        raise
    except BaseException as e:
        logger.error("Errore imprevisto fetch PMC%s: %s", pmc_id, e, exc_info=True)
        raise RuntimeError(
            f"Errore imprevisto nel recupero PMC{pmc_id}: {type(e).__name__}: {e}"
        ) from e


    try:
        title_xml, parsed_text = _parse_jats(xml_content)
        logger.info("Parsed PMC%s: title=%r, chars=%d", pmc_id, title_xml[:60], len(parsed_text))
    except RuntimeError:
        raise
    except Exception as e:
        logger.error("Errore parsing XML PMC%s: %s", pmc_id, e, exc_info=True)
        raise RuntimeError(
            f"Errore parsing JATS XML per PMC{pmc_id}: {type(e).__name__}: {e}"
        ) from e

    #fallback se l'XML JATS ci ha dato troppo poco  proviamo l'api BioC di NCBI research
    if len(parsed_text) < 3000:
        logger.info(
            "JATS XML ha dato solo %d char. Provo l'API BioC di NCBI...",
            len(parsed_text),
        )
        bioc_content = await _fetch_bioc(pmc_id)
        if bioc_content:
            bioc_title, bioc_text = _parse_bioc_json(bioc_content)
            if len(bioc_text) > len(parsed_text):
                logger.info(
                    "BioC ha estratto %d char (vs %d JATS), uso BioC",
                    len(bioc_text), len(parsed_text),
                )
                parsed_text = bioc_text
                if not title_xml and bioc_title:
                    title_xml = bioc_title
            else:
                logger.warning(
                    "BioC ha restituito solo %d char, mantengo JATS",
                    len(bioc_text),
                )
        else:
            logger.warning("BioC non disponibile per PMC%s", pmc_id)
    #se anche bioc non basta e abbiamo HTML prova il parsing HTML web di solito bloccato da cloudflare
    logger.info(
        "Diagnostica fallback HTML: html_len=%d, is_captcha=%s, parsed_len=%d, soglia=3000",
        len(html_text) if html_text else 0,
        _is_captcha(html_text) if html_text else "n/a",
        len(parsed_text),
    )
    if html_text and not _is_captcha(html_text) and len(parsed_text) < 3000:
        logger.info(
            "JATS XML restituisce solo %d char (probabilmente solo abstract). "
            "Provo a parsare l'HTML pubblico come fallback.",
            len(parsed_text),
        )
        try:
            title_html, parsed_html = _parse_html_web(html_text)
            if len(parsed_html) > len(parsed_text):
                logger.info(
                    "HTML pubblico estrae %d char (vs %d JATS), uso HTML",
                    len(parsed_html), len(parsed_text),
                )
                parsed_text = parsed_html
                if not title_xml and title_html:
                    title_xml = title_html
        except Exception as e:
            logger.warning("Fallback HTML pubblico fallito: %s", e)

    if not title:
        title = title_xml
    if not html_text:
        html_text = xml_content

    return {
        "url": url,
        "domain": SUPPORTED_DOMAIN,
        "title": title,
        "html_text": html_text,
        "parsed_text": parsed_text,
    }


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.DEBUG)

    for test_id in ["PMC8092263", "PMC1153448"]:
        print(f"\n{'=' * 60}\nTest: {test_id}")
        test_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{test_id}/"
        try:
            res = asyncio.run(parse_pubmed_page(test_url))
            print(json.dumps(
                {k: v[:400] if isinstance(v, str) else v for k, v in res.items()},
                indent=2, ensure_ascii=False,
            ))
        except Exception as e:
            print(f"ERRORE: {type(e).__name__}: {e}")
from src.parser_base import BaseParser, register
class PubmedParser(BaseParser):
    """Parser per le pagine di pmc.ncbi.nlm.nih.gov."""
    domain = SUPPORTED_DOMAIN
    async def parse(self, url: str, html_text: str | None = None) -> dict:
        return await parse_pubmed_page(url, html_text=html_text)
register(PubmedParser())
