
#parser per le pagine di news.microsoft.com
#il dominio è dietro Cloudflare e il markdown di Crawl4AI qui spesso viene
#vuoto o pieno di boilerplate usiamo Crawl4AI solo per scaricare l'HTML e
#poi beautifulsoup per tirare fuori il testo dell'articolo
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup, NavigableString, Tag
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
SUPPORTED_DOMAIN: str = "news.microsoft.com"
# pattern da scartare
_BOILERPLATE_PATTERNS: list[re.Pattern] = [
    re.compile(r"^top\s+image\s*:", re.IGNORECASE),        # didascalia foto
    re.compile(r"^\*\s*\w+$"),                              # tag categoria
    re.compile(                                             # bio autore in fondo
        r"^[A-Z][a-z]+ [A-Z][a-z]+ is (a|an) \w+", re.IGNORECASE
    ),
    re.compile(r"^images?\s+in\s+this\s+section", re.IGNORECASE),
    re.compile(r"^.*\bcreative\s+commons\b.*license", re.IGNORECASE),
    re.compile(r"^.*\blicensed\s+under\b.*$", re.IGNORECASE),
    re.compile(r"^\s*read\s+more\.?\s*$", re.IGNORECASE),   # link "Read more" isolato
]
#fine del contenuto editoriale
_STOP_PATTERNS: list[re.Pattern] = [
    re.compile(r"^related\s+links?\s*:?", re.IGNORECASE),
    re.compile(r"^top\s+image\s*:", re.IGNORECASE),
    re.compile(r"^photos?\s*$", re.IGNORECASE),
]
def is_supported(url: str) -> bool:
    #true se l'URL è del dominio gestito da questo parser
    host = urlparse(url).netloc.lower()
    return host == SUPPORTED_DOMAIN or host.endswith("." + SUPPORTED_DOMAIN)
def _is_boilerplate(line: str) -> bool:
    stripped = line.lstrip("# *").strip()
    return any(p.match(stripped) for p in _BOILERPLATE_PATTERNS)
def _is_stop_line(line: str) -> bool:
    stripped = line.lstrip("# *").strip()
    return any(p.match(stripped) for p in _STOP_PATTERNS)
#conversione html in markdown
def _element_to_markdown(element: Tag) -> str:
    parts: list[str] = []
    def walk(node) -> None:
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if isinstance(node, Tag):
            if node.name == "a":
                href = (node.get("href") or "").strip()
                link_text = node.get_text(" ", strip=True)
                #salto link non utili
                href_lower = href.lower()
                if href_lower.startswith(("mailto:", "tel:", "javascript:", "#")) or not href:
                    if link_text:
                        parts.append(link_text)
                    return
                if link_text and href:
                    parts.append(f"[{link_text}]({href})")
                elif link_text:
                    parts.append(link_text)
                return
            for child in node.children:
                walk(child)
    for child in element.children:
        walk(child)
    text = "".join(parts)
    return re.sub(r"\s{2,}", " ", text).strip()
def _exec_like_url(url: str) -> bool:
    #le pagine /source/exec/<persona>/ sono profili executive con i pattern restrittivi pensati per gli articoli di
    # news finirebbero per essere rimosse svuotando l'output rilevandole in anticipo applichiamo una pulizia più leggera
    return "/source/exec/" in (url or "").lower()


def _extract_text_from_html(html: str, url: str = "") -> str:
    #estrae il testo dell'articolo direttamente dall'HTML
    #tengo i link in formato markdown 
    soup = BeautifulSoup(html, "lxml")
    #rimuovi tag non informativi
    for tag_name in (
        "nav", "header", "footer", "script", "style", "aside",
        "noscript", "iframe", "button", "form", "svg", "img",
        "figure", "figcaption",
    ):
        for tag in soup.find_all(tag_name):
            tag.decompose()
    # Rimuovi contenuti navigazione UI
    base_patterns: list[str] = [
        "navigation", "sidebar", "breadcrumb", "menu",
        "cookie", "banner", "social", "share", "related",
        "newsletter", "subscription", "advertisement",
        "article-footer", "post-footer", "entry-footer",
        "related-links", "read-more", "link-list",
    ]
    if not _exec_like_url(url):
        base_patterns += ["author", "byline", "bio"]
    boundary_re = re.compile(
        r"(?:^|[\s\-_])(?:" + "|".join(base_patterns) + r")(?:[\s\-_]|$)",
        re.IGNORECASE,
    )
    for tag in soup.find_all(class_=boundary_re):
        tag.decompose()
    for tag in soup.find_all(id=boundary_re):
        tag.decompose()

    #individua il contenitore principale dell'articolo
    content = (
        soup.find("article")
        or soup.find("main")
        or soup.find(class_=re.compile(r"article|content|post|story|executive|profile|bio", re.IGNORECASE))
        or soup.find("body")
    )
    if not content:
        return ""
    lines: list[str] = []
    for element in content.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th"]):
        text = _element_to_markdown(element)
        if not text:
            continue
        tag = element.name
        if tag == "h1":
            text = f"# {text}"
        elif tag == "h2":
            text = f"## {text}"
        elif tag == "h3":
            text = f"### {text}"
        elif tag in ("h4", "h5", "h6"):
            text = f"#### {text}"
        elif tag == "li":
            text = f"* {text}"

        lines.append(text)

    #rimuovi duplicati consecutivi
    dedup: list[str] = []
    prev: str | None = None
    for line in lines:
        if line != prev:
            dedup.append(line)
        prev = line
    truncated: list[str] = []
    for line in dedup:
        if _is_stop_line(line):
            break
        if not _is_boilerplate(line):
            #pulizia Read more il regex copre tutti  casi
            line = re.sub(
                r"\s*\[?\s*read\s+more\s*\]?(?:\([^)]*\))?\s*[\.\u2026]*\s*$",
                "",
                line,
                flags=re.IGNORECASE,
            ).rstrip(" :")
            if line:
                truncated.append(line)
    #compatta righe vuote consecutive
    result: list[str] = []
    prev_empty = False
    for line in truncated:
        if not line.strip():
            if not prev_empty:
                result.append("")
            prev_empty = True
        else:
            result.append(line)
            prev_empty = False

    parsed = "\n".join(result).strip()

    #fallback: se la pulizia ha azzerato il contenuto succede su pagine con
    # layout non standard, es. profili /exec/ o template custom provo una
    # seconda passata più permissiva niente rimozione di selettori
    # estrazione diretta da <body>
    if not parsed:
        parsed = _extract_text_permissive(html)

    return parsed
def _extract_text_permissive(html: str) -> str:
    #estrazione minimale tengo solo header e paragrafi dal body scartando
    # script/style/nav/header/footer niente filtri sulle class
    soup = BeautifulSoup(html, "lxml")
    for tag_name in (
        "nav", "header", "footer", "script", "style", "noscript",
        "iframe", "button", "form", "svg",
    ):
        for tag in soup.find_all(tag_name):
            tag.decompose()
    body = soup.find("body") or soup
    lines: list[str] = []
    for element in body.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li"]):
        text = element.get_text(" ", strip=True)
        if not text or len(text) < 3:
            continue
        tag = element.name
        if tag == "h1":
            text = f"# {text}"
        elif tag == "h2":
            text = f"## {text}"
        elif tag == "h3":
            text = f"### {text}"
        elif tag in ("h4", "h5", "h6"):
            text = f"#### {text}"
        elif tag == "li":
            text = f"* {text}"
        lines.append(text)

    #elimina righe identiche consecutive
    dedup: list[str] = []
    prev: str | None = None
    for line in lines:
        if line != prev:
            dedup.append(line)
        prev = line
    return "\n".join(dedup).strip()

def _extract_title(html: str) -> str:
    #estrae il titolo della pagina
    soup = BeautifulSoup(html, "lxml")

    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        return og_title["content"].strip()

    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
        title = re.sub(r"\s*[|\-–]\s*Microsoft.*$", "", title, flags=re.IGNORECASE)
        return title.strip()

    return ""


#download e parsing
async def parse_microsoft_page(url: str, html_text: str | None = None) -> dict:
    #scarica la pagina  ne estrae titolo + corpo con beautifulsoup
    if not is_supported(url):
        raise ValueError(f"Dominio non supportato: {urlparse(url).netloc}")
    if html_text is not None:
        title = _extract_title(html_text)
        parsed_text = _extract_text_from_html(html_text, url=url)        
        return {
            "url": url,
            "domain": SUPPORTED_DOMAIN,
            "title": title,
            "html_text": html_text,
            "parsed_text": parsed_text,
        }
    browser_cfg = BrowserConfig(
        headless=True,
        verbose=False,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    crawler_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        result = await crawler.arun(url=url, config=crawler_cfg)

    if not result.success:
        raise RuntimeError(
            f"Impossibile raggiungere '{url}': {result.error_message or 'errore sconosciuto'}"
        )
    final_html: str = result.html or ""
    title: str = _extract_title(final_html)
    parsed_text: str = _extract_text_from_html(final_html, url=url)

    return {
        "url": url,
        "domain": SUPPORTED_DOMAIN,
        "title": title,
        "html_text": final_html,
        "parsed_text": parsed_text,
    }
from src.parser_base import BaseParser, register
class MicrosoftParser(BaseParser):
    """Parser per le pagine di news.microsoft.com."""
    domain = SUPPORTED_DOMAIN
    async def parse(self, url: str, html_text: str | None = None) -> dict:
        return await parse_microsoft_page(url, html_text=html_text)
register(MicrosoftParser())