# Web Parsing Pipeline

Pipeline di estrazione e valutazione di contenuti web multi-dominio. Preleva pagine
da domini eterogenei, ne estrae il testo pulito in **Markdown** e ne misura la qualità
con metriche automatiche e un **LLM-as-Judge**. Tutto è esposto tramite **REST API**
(FastAPI), corredato da una piccola interfaccia web, e orchestrato con **Docker Compose**.

> Progetto di Laboratorio di Ingegneria Informatica — Sapienza Università di Roma.
> Lavoro di gruppo (3 persone).

---

## Indice

- [Caratteristiche](#caratteristiche)
- [Architettura](#architettura)
- [Domini supportati](#domini-supportati)
- [Stack tecnologico](#stack-tecnologico)
- [Avvio rapido](#avvio-rapido)
- [Endpoint principali](#endpoint-principali)
- [Valutazione della qualità](#valutazione-della-qualità)
- [Struttura del repository](#struttura-del-repository)
- [Risultati](#risultati)

---

## Caratteristiche

- **Parser specifici per dominio** con pattern *factory/registry*: il parser corretto
  viene selezionato a runtime dall'host dell'URL, senza catene di `if/elif`. Aggiungere
  un nuovo dominio significa scrivere una sottoclasse e registrarla.
- **Estrazione robusta** anche da siti protetti da Cloudflare / challenge JavaScript,
  tramite rendering headless e successiva pulizia del contenuto.
- **Output normalizzato in Markdown**, con rimozione di boilerplate, widget, banner e
  cross-link.
- **Persistenza su MariaDB**: risorse web, gold standard, valutazioni automatiche e
  valutazioni del judge.
- **Valutazione automatica** con tre metriche (token-level P/R/F1, char overlap, BLEU-1)
  confrontate contro un gold standard.
- **LLM-as-Judge** via Ollama (`llama3.2:3b`), con punteggio 1–5 e fallback robusti se il
  modello non è raggiungibile.
- **REST API** in FastAPI + interfaccia web minimale (Jinja2).
- **Avvio con un comando** grazie a Docker Compose; il modello giudice viene scaricato
  automaticamente al primo avvio.

## Architettura

```
┌────────────┐      ┌────────────┐      ┌────────────┐
│  Frontend  │ ───▶ │  Backend   │ ───▶ │  MariaDB   │
│ (Jinja2)   │      │ (FastAPI)  │      │            │
└────────────┘      └─────┬──────┘      └────────────┘
                          │
                          ▼
                    ┌────────────┐
                    │   Ollama   │  (LLM-as-Judge: llama3.2:3b)
                    └────────────┘
```

Quattro servizi orchestrati da Docker Compose: `database` (MariaDB), `ollama`
(+ servizio one-shot che scarica il modello), `backend` (FastAPI) e `frontend`.

## Domini supportati

| Dominio                 | Tipo di contenuto                                  |
|-------------------------|----------------------------------------------------|
| `it.wikipedia.org`      | Voci enciclopediche                                |
| `news.microsoft.com`    | Articoli / comunicati                              |
| `pmc.ncbi.nlm.nih.gov`  | Articoli scientifici (PubMed Central)              |
| `it.investing.com`      | News finanziarie, schede strumento, listing        |

## Stack tecnologico

- **Backend:** Python, FastAPI, Uvicorn, Pydantic
- **Parsing / scraping:** BeautifulSoup, lxml, Crawl4AI (rendering headless), html2text
- **Database:** MariaDB (connector ufficiale `mariadb`)
- **LLM:** Ollama (`llama3.2:3b`)
- **Frontend:** FastAPI + Jinja2
- **Infra:** Docker, Docker Compose

## Avvio rapido

**Prerequisiti:** Docker e Docker Compose.

```bash
git clone <url-del-repo>
cd web-parsing-pipeline

# Avvia l'intero stack (al primo avvio scarica il modello LLM: può richiedere
# qualche minuto)
docker compose up --build
```

Una volta avviato:

| Servizio       | URL                       |
|----------------|---------------------------|
| Interfaccia web| http://localhost:8004     |
| API (Swagger)  | http://localhost:8003/docs|
| MariaDB        | `localhost:3306`          |
| Ollama         | http://localhost:11434    |

## Endpoint principali

| Metodo | Endpoint              | Descrizione                                              |
|--------|-----------------------|---------------------------------------------------------|
| POST   | `/parse`              | Estrae testo pulito da un URL (o da HTML fornito)        |
| GET    | `/domains`            | Elenco dei domini supportati                             |
| GET    | `/gold_standard`      | Gold standard associato a un URL                         |
| POST   | `/evaluate`           | Metriche automatiche (parsed vs gold standard)           |
| POST   | `/evaluate_judge`     | Valutazione LLM-as-Judge                                 |
| GET    | `/full_gs_eval`       | Valutazione completa su tutto il gold standard di un dominio |
| GET    | `/db_stats`           | Statistiche aggregate dal database                       |
| GET    | `/status`             | Stato dei servizi (DB, Ollama, modello)                  |

Documentazione interattiva completa su `/docs`.

## Valutazione della qualità

Per ogni pagina il testo estratto viene confrontato con un **gold standard** tramite:

- **Token-level** — precision, recall, F1 sull'insieme dei token.
- **Char overlap** — più tollerante a differenze di spaziatura/punteggiatura.
- **BLEU-1** — penalizza output troppo corti rispetto al gold standard, utile per
  individuare parser che tagliano troppo contenuto.

A queste si affianca un **LLM-as-Judge** (Ollama) che restituisce un punteggio intero
1–5 e un breve feedback testuale. Qualsiasi problema (Ollama non raggiungibile, modello
non ancora scaricato, JSON malformato) è gestito da un fallback che restituisce comunque
un risultato valido, così l'endpoint non va mai in crash.

## Struttura del repository

```
.
├── docker-compose.yaml
├── domains.json                 # elenco domini supportati
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── src/
│       ├── server.py            # API FastAPI (tutti gli endpoint)
│       ├── database.py          # connessione + schema + query MariaDB
│       ├── init_db.py           # creazione schema e popolamento dal GS
│       ├── parser_base.py       # classe astratta + registry dei parser
│       ├── parser_wikipedia.py
│       ├── parser_microsoft.py
│       ├── parser_pubmed.py
│       ├── parser_investing.py
│       ├── evaluation.py        # metriche automatiche (F1, char overlap, BLEU-1)
│       └── llm_judge.py         # integrazione LLM-as-Judge (Ollama)
├── frontend/
│   ├── Dockerfile
│   └── src/                     # app FastAPI + template Jinja2
└── gs_data/                     # dataset gold standard per dominio
```

## Risultati

Sui quattro domini gestiti la metrica principale (F1) si attesta **tra 0.94 e 0.98**,
con i punteggi del judge coerenti con le metriche automatiche (i domini con F1 più alto
ricevono punteggi più alti).

---

*Progetto universitario a scopo didattico.*
