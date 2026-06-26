#parser dedicato per le pagine di it.wikipedia.org
import re
from urllib.parse import urlparse
import html2text
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
SUPPORTED_DOMAIN: str = "it.wikipedia.org"
def is_supported(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == SUPPORTED_DOMAIN or host.endswith("." + SUPPORTED_DOMAIN)

_SKIP_SECTIONS: tuple[str, ...] = (
    "note",
    "voci correlate",
    "altri progetti",
    "collegamenti esterni",
    "bibliografia",
    "sitografia",
    "portale",
    "categorie",
    "wikizionario",
    "indice",  
    "see also",
    "references",
    "external links",
    "further reading",
    "notes",
    "bibliography",
)

#inizi di paragrafi  da saltare
_SKIP_BOLD_STARTS: tuple[str, ...] = (
    "questa voce",
    "questa sezione",
    "questa pagina",
    "la voce",
    "le informazioni",
    "la neutralità",
    "puoi migliorare",
    "aiuta wikipedia",
)


def _find_content_start(lines: list[str]) -> int:
    #salto nav menu laterale e template di avviso iniziali
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if re.match(r"^#{1,2}\s+\S", s):
            header = re.sub(r"^#{1,6}\s+", "", s).strip().lower()
            if header not in ("indice", "lingue", "in altri progetti"):
                return i

        if re.match(r"^\*\*[^\*]+\*\*", s) and len(s) > 60:
            lower = s.lower()
            if not any(lower.startswith(f"**{t}") for t in _SKIP_BOLD_STARTS):
                return i

    return 0


def _clean_markdown(raw_md: str) -> str:
    #pulisce il markdown che torna da Crawl4AI per una pagina di wikipedia
    #teniamo titoli paragrafi liste tabelle nel corpo dell'articolo
    lines = raw_md.splitlines()

    #taglio tutto fino al primo contenuto
    start = _find_content_start(lines)
    lines = lines[start:]

    # rimuovo sezioni di coda e righe di servizio
    skip_mode = False
    cleaned: list[str] = []

    for line in lines:
        s = line.strip()

        # se apre una sezione da escludere
        if re.match(r"^#{1,6}\s", s):
            header_text = re.sub(r"^#{1,6}\s+", "", s).strip().lower()
            if any(header_text.startswith(x) for x in _SKIP_SECTIONS):
                skip_mode = True
                continue
            skip_mode = False

        if skip_mode:
            continue

        # avvisi wikipedia
        if re.match(
            r"^\*?\*?(?:Questa voce|Questa sezione|Puoi migliorare|"
            r"Aiuta Wikipedia|La neutralità|Le informazioni|Segui i suggerimenti)",
            s,
            re.IGNORECASE,
        ):
            continue
        if re.match(r"^Puoi\s+\[migliorare", s, re.IGNORECASE):
            continue
        if re.match(r"^Segui i suggerimenti", s, re.IGNORECASE):
            continue

        #Link di navigazione
        if re.match(r"^\*\s*\[", s):
            continue

        #footer
        if re.match(r"^Da Wikipedia", s, re.IGNORECASE):
            continue

        #avvisi disambiguazione
        if re.search(r"Disambiguazione|Se stai cercando altri significati", s, re.IGNORECASE):
            continue
        #navigazione
        if re.match(
            r"^sposta nella barra|^nascondi$|^Mostra/Nascondi", s, re.IGNORECASE
        ):
            continue
        if re.fullmatch(r"italiano", s, re.IGNORECASE):
            continue
        if re.match(r"^\d+\s+lingue?\s*$", s, re.IGNORECASE):
            continue
        if re.fullmatch(
            r"Strumenti|Azioni|Generale|Aspetto|Comunità|Navigazione|"
            r"Stampa/esporta|In altri progetti|Strumenti personali|"
            r"Testo|Larghezza|Colore \(beta\)|Colore|Ricerca|Lingua",
            s,
            re.IGNORECASE,
        ):
            continue
        if re.fullmatch(
            r"\*?\s*(?:Piccolo|Grande|Largo|Automatico|Chiaro|Scuro|Standard)",
            s,
            re.IGNORECASE,
        ):
            continue
        #disclaimer
        if re.match(
            r"^(Questa pagina (utilizza|è sempre)|Il contenuto è il più ampio)",
            s,
            re.IGNORECASE,
        ):
            continue
        # link vari 
        if re.search(r"\[modifica|\[&veaction=edit|\[&action=edit", s, re.IGNORECASE):
            s = re.sub(r"\[modifica[^\]]*\](\([^)]*\))?", "", s)
            s = re.sub(r"\[&[^\]]*\](\([^)]*\))?", "", s)
            s = s.strip()
            if not s:
                continue

        if re.fullmatch(r"\[.*?(modifica|edit).*?\].*", s, re.IGNORECASE):
            continue
        #immagini markdown
        if re.fullmatch(r"!\[.*?\]\(.*?\)", s):
            continue

        #link a file o immagine
        if re.match(r"^\[\]\(//[^\)]*(?:File:|file:)[^\)]*\)", s):
            continue
        if re.match(r"^\[\]\(//[^\)]*\)\s*", s):
            s = re.sub(r"^\[\]\([^\)]*\)\s*", "", s).strip()
            if not s:
                continue
        #separatori vuoti
        if re.fullmatch(r"\[\s*\|\s*\]", s):
            continue
        #note bibliografiche
        s = re.sub(r"\[\[\d+\]\]\(#cite_note[^)]*\)", "", s).strip()
        if not s:
            continue
        if s.startswith("Lo stesso argomento in dettaglio"):
            continue
        # immagini
        s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s).strip()
        if not s:
            continue
        # link inline a file
        if re.match(r"^\[\]\(", s):
            continue
        s = re.sub(r"\[\]\([^)]*\)", "", s).strip()
        if not s:
            continue
        # residui
        if re.fullmatch(r"!\S.*", s) and len(s) < 30:
            continue

        cleaned.append(s)

    # compatto righe vuote consecutive
    result: list[str] = []
    prev_empty = False
    for line in cleaned:
        if not line.strip():
            if not prev_empty:
                result.append("")
            prev_empty = True
        else:
            result.append(line)
            prev_empty = False

    return "\n".join(result).strip()


def _extract_title(html: str) -> str:
    # prendo il titolo dal tag <title>
    match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    title = match.group(1)
    title = re.sub(r"\s*[-–]\s*Wikipedia.*$", "", title, flags=re.IGNORECASE)
    return title.strip()


#download + parsing
async def parse_wikipedia_page(url: str, html_text: str | None = None) -> dict:
    #scarica una pagina di it.wikipedia.org con Crawl4AI o usa l'html passato
    # e tira fuori metadati + testo pulito in markdown
    if not is_supported(url):
        raise ValueError(f"Dominio non supportato: {urlparse(url).netloc}")

   
    if html_text is not None:
        converter = html2text.HTML2Text()
        converter.body_width = 0         
        converter.ignore_images = True   
        converter.ignore_emphasis = False
        converter.unicode_snob = True    
        converter.skip_internal_links = True
        raw_markdown = converter.handle(html_text)

        title: str = _extract_title(html_text)
        parsed_text: str = _clean_markdown(raw_markdown)
        return {
            "url": url,
            "domain": SUPPORTED_DOMAIN,
            "title": title,
            "html_text": html_text,
            "parsed_text": parsed_text,
        }

    
    browser_cfg = BrowserConfig(headless=True, verbose=False)
    crawler_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        result = await crawler.arun(url=url, config=crawler_cfg)

    if not result.success:
        raise RuntimeError(
            f"Impossibile raggiungere '{url}': {result.error_message or 'errore sconosciuto'}"
        )

    final_html: str = result.html or ""
    raw_markdown: str = result.markdown or ""

    title = _extract_title(final_html)
    if not title and getattr(result, "metadata", None):
        title = result.metadata.get("title", "") or ""

    parsed_text = _clean_markdown(raw_markdown)

    return {
        "url": url,
        "domain": SUPPORTED_DOMAIN,
        "title": title,
        "html_text": final_html,
        "parsed_text": parsed_text,
    }
from src.parser_base import BaseParser, register
class WikipediaParser(BaseParser):
    """Parser per le pagine di it.wikipedia.org."""
    domain = SUPPORTED_DOMAIN
    async def parse(self, url: str, html_text: str | None = None) -> dict:
        return await parse_wikipedia_page(url, html_text=html_text)
register(WikipediaParser())
