#classe base astratta e registry dei parser
from abc import ABC, abstractmethod
from urllib.parse import urlparse


class BaseParser(ABC):
    """Interfaccia comune a tutti i parser di dominio.

    Una sottoclasse deve:
      - impostare l'attributo di classe ``domain`` (es. "it.wikipedia.org");
      - implementare il metodo asincrono ``parse``.

    Il matching del dominio (``supports``) e' invece comportamento condiviso,
    ereditato da questa classe base.
    """

    #dominio gestito dalla sottoclasse poi sovrascritto dalle sottoclassi
    domain: str = ""

    def supports(self, url_or_host: str) -> bool:
        """True se l'URL (o l'host) appartiene al dominio del parser.

        Accetta sia URL completi (``https://it.wikipedia.org/...``) sia host
        puri (``it.wikipedia.org``) e considera validi anche i sottodomini.
        """
        host = url_or_host.lower()
        if "://" in url_or_host:
            host = urlparse(url_or_host).netloc.lower()
        return host == self.domain or host.endswith("." + self.domain)

    @abstractmethod
    async def parse(self, url: str, html_text: str | None = None) -> dict:
        """Estrae i metadati e il testo pulito da una pagina del dominio.

        Se ``html_text`` e' fornito si parsa quell'HTML (modalita' local,
        nessun download); altrimenti la pagina viene scaricata.

        Ritorna un dict con le chiavi:
        ``url``, ``domain``, ``title``, ``html_text``, ``parsed_text``.
        """
        raise NotImplementedError

    def __repr__(self) -> str:  # utile nei log
        return f"<{type(self).__name__} domain={self.domain!r}>"
_REGISTRY: dict[str, "BaseParser"] = {}


def register(parser: "BaseParser") -> "BaseParser":
    """Registra un'istanza di parser nel registry globale.

    Restituisce l'istanza stessa, cosi' da poter scrivere in modo conciso:
        PARSER = register(WikipediaParser())
    """
    if not parser.domain:
        raise ValueError(f"{type(parser).__name__} non ha impostato 'domain'")
    _REGISTRY[parser.domain] = parser
    return parser


def get_parser(url_or_host: str) -> "BaseParser | None":
    """Restituisce il parser competente per l'URL/host dato, o None."""
    for parser in _REGISTRY.values():
        if parser.supports(url_or_host):
            return parser
    return None


def registered_domains() -> list[str]:
    """Domini per cui esiste un parser registrato."""
    return list(_REGISTRY.keys())
