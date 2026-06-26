#parser per le pagine di investing.com.
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup, NavigableString, Tag
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
SUPPORTED_DOMAIN: str = "it.investing.com"
#pattern di righe da scartare
_BOILERPLATE_PATTERNS: list[re.Pattern] = [
    re.compile(r"^image\s*[:\-]", re.IGNORECASE),                  # didascalia
    re.compile(r"^photo\s+by", re.IGNORECASE),                     # credit foto
    re.compile(r"^source\s*[:\-]", re.IGNORECASE),                 # Source: Reuters
    re.compile(r"^by\s+[A-Z][a-z]+\s+[A-Z][a-z]+\s*$"),            # byline isolato
    re.compile(r"^reuters\s*$", re.IGNORECASE),                    # solo agenzia
    re.compile(r"^investing\.com\s*[\-–]", re.IGNORECASE),         # firma dominio
    re.compile(r"^advertis(ement|ing)\s*$", re.IGNORECASE),        # banner residui
    re.compile(r"^sponsored( content)?\s*$", re.IGNORECASE),
    re.compile(r"^read\s+(more|also|next)\b", re.IGNORECASE),      # cross-link
    re.compile(r"^click\s+here\s+to\b", re.IGNORECASE),
    re.compile(r"^continue\s+reading\b", re.IGNORECASE),
    re.compile(r"^share\s+(on|this)\b", re.IGNORECASE),            # widget social
    re.compile(r"^follow\s+us\b", re.IGNORECASE),
    re.compile(r"^subscribe\b", re.IGNORECASE),                    # newsletter
    re.compile(r"^sign\s+up\b", re.IGNORECASE),
    re.compile(r"^download\s+the\s+app\b", re.IGNORECASE),
    re.compile(r"^disclosure\s*:", re.IGNORECASE),                 # disclaimer
    re.compile(r"^disclaimer\s*:", re.IGNORECASE),
    #tag categoria isolati
    re.compile(r"^\*\s*\w+(\s+\w+){0,2}$"),
]
#marker di fine contenuto editoriale tutto quello che viene dopo è non significante
_STOP_PATTERNS: list[re.Pattern] = [
    re.compile(r"^related\s+(articles?|news|analysis)\b", re.IGNORECASE),
    re.compile(r"^you\s+may\s+(also\s+)?(like|be\s+interested)\b", re.IGNORECASE),
    re.compile(r"^more\s+from\s+investing\.com\b", re.IGNORECASE),
    re.compile(r"^trending\s+(articles?|news|now)\b", re.IGNORECASE),
    re.compile(r"^most\s+popular\b", re.IGNORECASE),
    re.compile(r"^latest\s+(comments?|news)\b", re.IGNORECASE),
    re.compile(r"^comments?\s*\(\d+\)\s*$", re.IGNORECASE),
    re.compile(r"^add\s+a\s+comment\b", re.IGNORECASE),
    re.compile(r"^write\s+your\s+thoughts\b", re.IGNORECASE),
    re.compile(r"^post\s+a\s+comment\b", re.IGNORECASE),
    re.compile(r"^similar\s+articles\b", re.IGNORECASE),
    re.compile(r"^continue\s+with\b", re.IGNORECASE),
    re.compile(r"^is\s+\S+\s+a\s+bargain\b", re.IGNORECASE),
    re.compile(r"^the\s+fastest\s+way\s+to\s+find\s+out\b", re.IGNORECASE),
    re.compile(r"^fair\s+value\s+calculator\b", re.IGNORECASE),
    re.compile(r"^get\s+the\s+bottom\s+line\b", re.IGNORECASE),
    re.compile(r"^find\s+your\s+next\s+hidden\s+gem\b", re.IGNORECASE),
    re.compile(r"^flash\s+sale\b", re.IGNORECASE),
    re.compile(r"^try\s+investingpro\b", re.IGNORECASE),
    re.compile(r"^unlock\s+investingpro\b", re.IGNORECASE),
    re.compile(r"^upgrade\s+(to|now)\b", re.IGNORECASE),
    #sezioni FAQ a fine pagina
   
    re.compile(r"^faq\b", re.IGNORECASE),
    re.compile(r"^domande\s+frequenti\b", re.IGNORECASE),
    re.compile(r"^.{10,120}\?$"),                                      #heading che è una domanda
    re.compile(r"^scan\s+the\s+qr\s+code\b", re.IGNORECASE),          #footer QR code
]


def is_supported(url: str) -> bool:
    #true se l'URL appartiene al dominio gestito da questo parser
    host = urlparse(url).netloc.lower()
    return host == SUPPORTED_DOMAIN or host.endswith("." + SUPPORTED_DOMAIN)
def _is_boilerplate(line: str) -> bool:
    stripped = line.lstrip("# *").strip()
    return any(p.match(stripped) for p in _BOILERPLATE_PATTERNS)
def _is_stop_line(line: str) -> bool:
    stripped = line.lstrip("# *").strip()
    return any(p.match(stripped) for p in _STOP_PATTERNS)
#conversione di un nodo HTML a testo plain con link in markdown
def _element_to_markdown(element: Tag) -> str:
    parts: list[str] = []
    def walk(node) -> None:
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if isinstance(node, Tag):
            #salto figli che sono contenitori da escludere
            if node.name in ("script", "style", "noscript", "iframe", "svg", "img",
                             "figure", "figcaption", "picture", "video", "audio"):
                return
            if node.name == "a":
                href = (node.get("href") or "").strip()
                link_text = node.get_text(" ", strip=True)
                href_lower = href.lower()
                # Link non utili: mailto, tel, javascript, ancore vuote
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


#conversione tabella HTML in formato markdown
#manteniamo le tabelle perché su investing.com contengono dati di valore
def _table_to_markdown(table: Tag) -> str:
    rows: list[list[str]] = []
    head_cells: list[str] = []
    thead = table.find("thead")
    if thead:
        for th in thead.find_all(["th", "td"]):
            head_cells.append(_element_to_markdown(th) or "")
    else:
        first_tr = table.find("tr")
        if first_tr and first_tr.find("th"):
            for th in first_tr.find_all(["th", "td"]):
                head_cells.append(_element_to_markdown(th) or "")
    #body
    body_rows: list[list[str]] = []
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        #se la riga era già usata come header la salto
        if not head_cells and tr is table.find("tr") and tr.find("th"):
            continue
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        row = [(_element_to_markdown(c) or "") for c in cells]
        #se è una riga di soli celle vuote salto
        if not any(cell.strip() for cell in row):
            continue
        body_rows.append(row)
    if not head_cells and not body_rows:
        return ""
    n_cols = max(
        len(head_cells),
        max((len(r) for r in body_rows), default=0),
    )
    if n_cols == 0:
        return ""
    def _pad(row: list[str]) -> list[str]:
        return row + [""] * (n_cols - len(row))
    lines: list[str] = []
    if head_cells:
        lines.append("| " + " | ".join(_pad(head_cells)) + " |")
        lines.append("| " + " | ".join(["---"] * n_cols) + " |")
    for r in body_rows:
        lines.append("| " + " | ".join(_pad(r)) + " |")
    return "\n".join(lines)
_NUM_CELL_RE = re.compile(r"^[+\-]?[\d.,]+\s*%?$")

def _is_quote_widget_table(table: Tag) -> bool:
    #riconosce i widget panoramica mercati che investing.com inserisce in sidebar
    first = table.find("tr")
    if first is None:
        return False
    cells = [c.get_text(" ", strip=True) for c in first.find_all(["td", "th"])]
    cells = [c for c in cells if c]
    if len(cells) < 2:
        return False
    numeric = sum(1 for c in cells if _NUM_CELL_RE.match(c))
    return numeric >= 2
def _is_chart_or_widget(tag: Tag) -> bool:
    cls = " ".join(tag.get("class", []) or []).lower()
    tid = (tag.get("id") or "").lower()
    if not cls and not tid:
        return False
    chart_tokens = (
        "chart", "tradingview", "tv-chart", "highchart", "highcharts",
        "js-chart", "instrument-chart", "techchart", "candlestick",
        "sparkline", "graphbox", "graph-box", "graph_box",
    )
    blob = cls + " " + tid
    return any(tok in blob for tok in chart_tokens)
def _extract_text_from_html(html: str, url: str = "") -> str:
    #estrae titolo + corpo + tabelle da una pagina di investing.com
    soup = BeautifulSoup(html, "lxml")
    next_script = soup.find("script", id="__NEXT_DATA__")
    if next_script:
        next_script.decompose()

    #rimozione tag inutili
    for tag_name in (
        "script", "style", "noscript", "iframe", "svg", "img",
        "figure", "figcaption", "picture", "video", "audio", "canvas",
        "button", "form", "input", "select", "textarea",
        "nav", "header", "footer", "aside",
    ):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    #rimozione contenitori specifici per classe / id
    
    drop_tokens: list[str] = [
        #pubblicità
        "ad", "ads", "adv", "advert", "advertising", "advertisement",
        "banner", "promo", "promotion", "sponsor", "sponsored-card", "sponsored-block",
        "google-ad", "googlead", "doubleclick", "dfp", "adsense",
        "newpostad", "new-post-ad", "outbrain", "taboola",
        #articoli correlati
        "related", "related-articles", "related-news", "related-instruments",
        "siblings", "siblings_3_2", "next-article", "prev-article",
        "more-articles", "morenews", "more-news", "more-from",
        "trending", "trending-articles", "popularposts", "popular-posts",
        "most-popular", "you-may-like", "you-may-also",
        "similar", "similar-articles",
        "recommended", "recirculation",
        #grafici/strumenti
        "chart", "tv-chart", "tradingview", "highchart", "highcharts",
        "instrument-chart", "techchart", "tech-chart", "js-chart",
        "candlestick", "sparkline",
        #social/newsletter/ share
        "social", "share", "sharebar", "share-bar", "shareholder-buttons",
        "newsletter", "subscription", "subscribe",
        "follow", "follow-us",
        #UI
        "navigation", "navbar", "sidebar", "breadcrumb", "breadcrumbs",
        "menu", "topbar", "top-bar",
        "cookie", "cookies", "consent", "gdpr",
        "modal", "overlay", "popup", "pop-up", "lightbox",
        "login", "signup", "sign-up", "sign-in",
        "paywall", "preview-wall",
        #sezioni footer dell articolo
        "comments", "comments-section", "disqus",
        "tags", "topic-tags", "article-tags",
        "author-bio", "author-info", "author-card",
        "article-footer", "post-footer", "entry-footer",
        #box dati laterali che non sono tabelle
        "instrumentbox", "instrument-box", "quote-box",
    ]
    boundary_re = re.compile(
        r"(?:^|[\s\-_])(?:" + "|".join(drop_tokens) + r")(?:[\s\-_]|$)",
        re.IGNORECASE,
    )
    for tag in soup.find_all(class_=boundary_re):
        tag.decompose()
    for tag in soup.find_all(id=boundary_re):
        tag.decompose()

    #rimozione di contenitori rimasti
    for tag in list(soup.find_all(True)):
        if _is_chart_or_widget(tag):
            tag.decompose()

    #individuazione del contenitore principale dell'articolo.
    content = (
        soup.find("article")
        or soup.find(id="leftColumn")
        or soup.find("main")
        or soup.find(class_=re.compile(
            r"(?:^|[\s\-_])(?:wysiwyg|articlepage|article-page|article-body|"
            r"content-section|instrument-section|instrument-details)"
            r"(?:[\s\-_]|$)",
            re.IGNORECASE,
        ))
        or soup.find("body")
    )
    if not content:
        return ""

    #estrazione ordinata di heading, paragrafi, liste e tabelle
    interesting = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "table")
    lines: list[str] = []
    seen_tables: set[int] = set()
    for element in content.find_all(interesting):
        if element.name != "table":
            parent_table = element.find_parent("table")
            if parent_table is not None and id(parent_table) in seen_tables:
                continue

        if element.name == "table":
            #scarta i widget panoramica mercati non sono contenuto della pagina e gonfiano l'output abbassando la precision
            if _is_quote_widget_table(element):
                seen_tables.add(id(element))
                continue
            md_table = _table_to_markdown(element)
            if md_table:
                lines.append(md_table)
                seen_tables.add(id(element))
            continue

        text = _element_to_markdown(element)
        if not text:
            continue
        #scarto righe troppo corte e composte solo da rumore
        if len(text) < 2:
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
        elif tag == "blockquote":
            text = f"> {text}"
        lines.append(text)

    #leva righe duplicate consecutive
    dedup: list[str] = []
    prev: str | None = None
    for line in lines:
        if line != prev:
            dedup.append(line)
        prev = line

    #taglio a partire dal primo stop e filtro boilerplate
    truncated: list[str] = []
    for line in dedup:
        if _is_stop_line(line):
            break
        if not _is_boilerplate(line):
            line = re.sub(
                r"\s*\[?\s*(?:read\s+more|continue\s+reading)\s*\]?"
                r"(?:\([^)]*\))?\s*[\.\u2026]*\s*$",
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
    #fallback permissivo se la pulizia ha azzerato il contenuto
    if not parsed:
        parsed = _extract_text_permissive(html)
    return parsed


def _extract_text_permissive(html: str) -> str:
    #estrazione minimale tengo heading paragrafi tabelle dal body rimuovendo solo lo strettamente indispensabile
    soup = BeautifulSoup(html, "lxml")
    next_script = soup.find("script", id="__NEXT_DATA__")
    if next_script:
        next_script.decompose()
    for tag_name in (
        "script", "style", "noscript", "iframe", "svg", "img", "figure",
        "figcaption", "picture", "video", "audio", "canvas",
        "nav", "header", "footer", "aside", "button", "form",
    ):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    body = soup.find("body") or soup
    lines: list[str] = []
    seen_tables: set[int] = set()
    for element in body.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "table"]
    ):
        if element.name != "table":
            parent_table = element.find_parent("table")
            if parent_table is not None and id(parent_table) in seen_tables:
                continue
        if element.name == "table":
            md_table = _table_to_markdown(element)
            if md_table:
                lines.append(md_table)
                seen_tables.add(id(element))
            continue

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
        elif tag == "blockquote":
            text = f"> {text}"
        lines.append(text)
    dedup: list[str] = []
    prev: str | None = None
    for line in lines:
        if line != prev:
            dedup.append(line)
        prev = line
    return "\n".join(dedup).strip()


def _ensure_title_in_text(parsed_text: str, title: str) -> str:
    if not title or not parsed_text:
        return parsed_text or (f"# {title}" if title else "")
    #normalizzazione
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip().lower()

    norm_title = _norm(title)
    if not norm_title:
        return parsed_text

    #controllo solo le prime 10 righe non vuote  Se titolo gia' presente, skip
    head_lines = [l for l in parsed_text.splitlines()[:20] if l.strip()]
    head_norm = _norm("\n".join(head_lines))
    if norm_title in head_norm:
        return parsed_text

    #titolo assente lo prepongo come H1
    return f"# {title}\n{parsed_text}"


def _extract_title(html: str) -> str:
    #estrae il titolo della pagina
    soup = BeautifulSoup(html, "lxml")
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        return og_title["content"].strip()
    h1 = soup.find(
        "h1",
        class_=re.compile(r"articleheader|article-header|article-title", re.IGNORECASE),
    )
    if h1:
        t = h1.get_text(" ", strip=True)
        if t:
            return t

    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        if t:
            return t

    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
        # Tolgo i suffissi del brand
        title = re.sub(
            r"\s*[|\-–]\s*Investing\.com.*$", "", title, flags=re.IGNORECASE
        )
        return title.strip()

    return ""



# Le pagine strumento hanno una struttura completamente diversa dagli articoli non c'è un corpo testuale ma widget di prezzo tabelle di quotazioni e unvcalendario economico embedded come JSON 
#pattern URL strumento
_INSTRUMENT_URL_RE = re.compile(
    r"/(?:currencies|equities|commodities|crypto|indices)/",
    re.IGNORECASE,
)
_INSTRUMENT_FIELDS: dict[str, str] = {
    "price":                "instrument-price-last",
    "price_change":         "instrument-price-change",
    "price_change_percent": "instrument-price-change-percent",
    "prev_close":           "prevClose",
    "open":                 "open",
    "one_year_return":      "oneYearReturn",
    "bid":                  "bid",
    "ask":                  "ask",
    "daily_range":          "dailyRange",
    "week_range":           "weekRange",
}
def _parse_field(soup: BeautifulSoup, data_test: str) -> dict:
    """Restituisce {gold_text, html_text} per il primo elemento con data-test."""
    el = soup.find(attrs={"data-test": data_test})
    if not el:
        return {"gold_text": None, "html_text": None}
    return {"gold_text": el.get_text(strip=True), "html_text": str(el)}
def _parse_instrument_fields(html: str) -> dict[str, dict]:
    """
    Estrae i campi strutturati di una pagina-strumento.
    Ritorna {nome_campo: {gold_text, html_text}}.
    Ritorna {} se il prezzo non è presente (pagina news/analisi).
    """
    soup = BeautifulSoup(html, "lxml")
    fields = {name: _parse_field(soup, dt) for name, dt in _INSTRUMENT_FIELDS.items()}
    if fields["price"]["gold_text"] is None:
        return {}
    return fields
def _extract_paragraphs_fallback(html: str) -> str:
    """
    Fallback di terzo livello: estrae tutti i <p> con testo significativo
    direttamente dal <body>, dopo aver rimosso script/style/nav/header/footer.
    Usato quando né _extract_text_from_html né _build_instrument_parsed_text
    producono output sufficiente (es. pagina /brokers/ con contenuto in
    contenitori non standard).
    Applica un filtro minimo: scarta paragrafi < 40 caratteri (bottoni,
    label, breadcrumb) e deduplicati.
    """
    soup = BeautifulSoup(html, "lxml")

    #rimuovi __NEXT_DATA__ prima di tutto
    next_script = soup.find("script", id="__NEXT_DATA__")
    if next_script:
        next_script.decompose()

    for tag in soup.find_all(["script", "style", "noscript", "nav",
                               "header", "footer", "iframe", "aside"]):
        tag.decompose()

    seen: set[str] = set()
    lines: list[str] = []

    #estrai anche h1 h3 per strutturare il testo
    for el in soup.find_all(["h1", "h2", "h3", "p"]):
        text = el.get_text(" ", strip=True)
        #scarta stringhe troppo corte o già viste
        if len(text) < 40 or text in seen:
            continue
        #scarta testi che sembrano UI cioe tutto maiuscolo o con trattini
        if text.isupper() and len(text) < 80:
            continue
        #interrompi al primo FAQ o stop pattern
        if _is_stop_line(text):
            break
        seen.add(text)
        if el.name in ("h1", "h2", "h3"):
            lines.append(f"{'#' * int(el.name[1])} {text}")
        else:
            lines.append(text)
    return "\n\n".join(lines).strip()
def _instrument_label_for(value_el: Tag) -> str:
    row = value_el
    for _ in range(5):
        row = row.parent
        if not isinstance(row, Tag):
            break
        dt = row.find("dt")
        if dt:
            return dt.get_text(" ", strip=True)
    return ""


def _build_instrument_parsed_text(html: str) -> str:
    """
    Costruisce il parsed_text per una pagina-strumento (currencies, equities,
    commodities, crypto, indices).

    Il gold standard di queste pagine è il TESTO VISIBILE del blocco di
    panoramica in cima alla pagina, non una tabella curata. Ricostruiamo quel
    blocco ancorandoci ai data-test stabili del sito (validi su tutte le
    pagine strumento, non specifici di una singola coppia/titolo):

        - h1                          → titolo ("EUR/RON - Euro Leu Rumeno")
        - relative-selector           → tipo ("Forex in tempo reale")
        - currency-in-label           → "Valuta in <codice>"
        - instrument-header-details   → prezzo + variazione + % + orario
        - dailyRange / weekRange       → range giornaliero e a 52 settimane
        - banner insight ("Un movimento…")
        - dati principali (dt/dd): prevClose, open, oneYearReturn, bid, ask,
          dailyRange, weekRange — con le etichette reali lette dal DOM.

    Se la pagina non è una pagina-strumento (nessun prezzo) ricade sul
    fallback generico per tabelle (pagine Next.js tipo rates-bonds, ecc.).
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    def _dt_text(name: str) -> str:
        el = soup.find(attrs={"data-test": name})
        return el.get_text(" ", strip=True) if el else ""

    price = soup.find(attrs={"data-test": "instrument-price-last"})

    #pagina-strumento
    if price:
        lines: list[str] = []

        # Titolo: preferiamo l<h1>che è il nome pulito dello strumento
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else _dt_text("section-sub-title")
        if title:
            lines.append(f"# {title}")
        rel = _dt_text("relative-selector")
        if rel:
            lines.append(rel)
        cil = _dt_text("currency-in-label")
        if cil:
            cil = re.split(r"\bValuta\s+Nome\b", cil)[0].strip()
            if cil:
                lines.append(cil)
        header = _dt_text("instrument-header-details")
        if header:
            lines.append(header)
        elif price:  #fallback se l'header aggregato non c'è
            chg = _dt_text("instrument-price-change")
            pct = _dt_text("instrument-price-change-percent")
            lines.append(" ".join(filter(None, [price.get_text(strip=True), chg, pct])))

        #range giornaliero e a 52 settimane
        daily = _dt_text("dailyRange")
        week = _dt_text("weekRange")
        if daily:
            lines.append(f"Min-Max gg {daily}")
        if week:
            lines.append(f"52 settimane {week}")
        insight = soup.find(
            string=re.compile(r"movimento\s+del", re.IGNORECASE)
        )
        if insight is not None:
            block = insight.parent.get_text(" ", strip=True) if insight.parent else str(insight)
            if block and len(block) < 400:
                lines.append(block)
        for name in ("prevClose", "open", "oneYearReturn", "bid", "ask",
                     "dailyRange", "weekRange"):
            el = soup.find(attrs={"data-test": name})
            if not el:
                continue
            label = _instrument_label_for(el)
            value = el.get_text(" ", strip=True)
            lines.append(f"{label} {value}".strip())

        return "\n".join(x for x in lines if x and x.strip()).strip()
    #fallback tabelle HTML generiche
    lines = []
    _WIDGET_TABLE_RE = re.compile(
        r"VISITA\s+IL\s+SITO|account_balance_wallet|phone_iphone|equalizer"
        r"|Deposito\s+Minimo|Asset\s+scambiati",
        re.IGNORECASE,
    )
    for tag in soup.find_all(["nav", "header", "footer"]):
        tag.decompose()
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        #scarta i widget panoramica mercati che non fanno parte del contenuto informativo della pagina
        if _is_quote_widget_table(tbl):
            continue
        header_cells = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if len(header_cells) < 2 or all(len(c) < 3 for c in header_cells):
            continue
        tbl_sample = tbl.get_text(" ", strip=True)[:500]
        if _WIDGET_TABLE_RE.search(tbl_sample):
            continue
        lines.append("| " + " | ".join(header_cells) + " |")
        lines.append("| " + " | ".join(["---"] * len(header_cells)) + " |")
        for tr in rows[1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if not any(cells):
                continue
            while len(cells) < len(header_cells):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header_cells)]) + " |")
        lines.append("")

    return "\n".join(lines).strip()
#download + parsing
async def parse_investing_page(url: str, html_text: str | None = None) -> dict:
    #scarica la pagina o usa l'HTML passato dal DB  e ne estrae titolo + corpo + tabelle con BeautifulSoup Per pagine-strumento usa _build_instrument_parsed_text invece di _extract_text_from_html perché la struttura è completamente diversa da un articolo
    if not is_supported(url):
        raise ValueError(f"Dominio non supportato: {urlparse(url).netloc}")
    is_instrument = bool(_INSTRUMENT_URL_RE.search(url))

    #soglia minima di testo utilesotto la quale consideriamo l'output del parser generico insufficiente Corrisponde circa a
    # un titolo + 1-2 righe di testo tutto ciò che è solo il titolo o quasi vuoto viene considerato fallito
    _MIN_PARSED_CHARS = 200
    def _build_result(final_html: str) -> dict:
        title = _extract_title(final_html)
        if is_instrument:
            parsed_text = _build_instrument_parsed_text(final_html)
            if not parsed_text:
                parsed_text = _extract_text_from_html(final_html, url=url)
            else:
                #per le pagine strumento il titolo pulito è l'<h1>
                h1 = BeautifulSoup(final_html, "lxml").find("h1")
                if h1 and h1.get_text(strip=True):
                    title = h1.get_text(" ", strip=True)
        else:
            #pagine generiche news analisi calendari ecc. prova prima il parser generico se produce meno di _MIN_PARSED_CHARS di testo significa che
            # siamo su una pagina Next.js / widget senza <article> né leftColumne quindi prova il parser strutturato come secondo tentativo
            parsed_text = _extract_text_from_html(final_html, url=url)
            text_without_title = parsed_text.replace(title, "").strip()
            if len(text_without_title) < _MIN_PARSED_CHARS:
                #secondo tentativo
                structured = _build_instrument_parsed_text(final_html)
                if structured:
                    parsed_text = structured
                else:
                    #terzo tentativo estrazione grezza di tutti i <p> dal body
                    #funziona per pagine come brokers che hanno testo in
                    #paragrafi ma in contenitori non standard
                    parsed_text = _extract_paragraphs_fallback(final_html)

        parsed_text = _ensure_title_in_text(parsed_text, title)
        out: dict = {
            "url": url,
            "domain": SUPPORTED_DOMAIN,
            "title": title,
            "html_text": final_html,
            "parsed_text": parsed_text,
        }
        if is_instrument:
            fields = _parse_instrument_fields(final_html)
            if fields:
                out["instrument_fields"] = fields
        return out

    #modalità locale l'HTML è già disponibile, salto il crawler
    if html_text is not None:
        return _build_result(html_text)

    #download via Crawl4AI UA reale per non farsi bloccare da Cloudflare
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
            f"Impossibile raggiungere '{url}': "
            f"{result.error_message or 'errore sconosciuto'}"
        )

    return _build_result(result.html or "")
from src.parser_base import BaseParser, register


class InvestingParser(BaseParser):
    """Parser per le pagine di it.investing.com."""

    domain = SUPPORTED_DOMAIN

    async def parse(self, url: str, html_text: str | None = None) -> dict:
        return await parse_investing_page(url, html_text=html_text)


register(InvestingParser())
