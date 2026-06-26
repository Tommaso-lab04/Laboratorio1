#modulo di accesso al database MariaDB


import logging
import os
import time
from contextlib import contextmanager
from typing import Optional

import mariadb

logger = logging.getLogger(__name__)

#parametri di connessione presi da env
_DB_HOST: str = os.environ.get("DB_HOST", "database")
_DB_PORT: int = int(os.environ.get("DB_PORT", "3306"))
_DB_USER: str = os.environ.get("DB_USER", "appuser")
_DB_PASSWORD: str = os.environ.get("DB_PASSWORD", "apppassword")
_DB_NAME: str = os.environ.get("DB_NAME", "parsing_db")

#pool globale
_pool: Optional[mariadb.ConnectionPool] = None


def _get_pool() -> mariadb.ConnectionPool:
    #inizializzail pool di connessioni Mariadb
    global _pool
    if _pool is None:
        _pool = mariadb.ConnectionPool(
            pool_name="parsing_pool",
            pool_size=5,
            host=_DB_HOST,
            port=_DB_PORT,
            user=_DB_USER,
            password=_DB_PASSWORD,
            database=_DB_NAME,
            autocommit=False,
        )
    return _pool


def wait_for_db(max_attempts: int = 60, delay_s: float = 2.0) -> None:
    #aspetta che MariaDB sia raggiungibile
    last_err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            conn = mariadb.connect(
                host=_DB_HOST,
                port=_DB_PORT,
                user=_DB_USER,
                password=_DB_PASSWORD,
                database=_DB_NAME,
            )
            conn.close()
            logger.info("MariaDB raggiungibile dopo %d tentativi", attempt)
            return
        except mariadb.Error as exc:
            last_err = exc
            logger.info(
                "MariaDB non pronto (tentativo %d/%d): %s",
                attempt, max_attempts, exc,
            )
            time.sleep(delay_s)
    raise RuntimeError(f"MariaDB non raggiungibile: {last_err}")


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = pool.get_connection()
    try:
        yield conn
    finally:
        conn.close()


def db_health() -> bool:
    #true se il DB risponde a una SELECT 1
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception as exc:
        logger.warning("DB health check fallito: %s", exc)
        return False


#scheme

#le 2 tabelle obbligatorie devono avere questi nomi e queste colonne
#aggiungiamo due tabelle ausiliarie per memorizzare le valutazioniautomatiche e i giudizi LLM
_SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS web_resources (
        url        VARCHAR(2048) CHARACTER SET ascii NOT NULL,
        domain     VARCHAR(255)  NOT NULL,
        title      VARCHAR(2048) NULL,
        html_text  LONGTEXT      NULL,
        created_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (url),
        INDEX idx_domain (domain)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC
    """,
    """
    CREATE TABLE IF NOT EXISTS gold_standard (
        url        VARCHAR(2048) CHARACTER SET ascii NOT NULL,
        gold_text  LONGTEXT      NOT NULL,
        created_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (url),
        CONSTRAINT fk_gs_url FOREIGN KEY (url)
            REFERENCES web_resources(url)
            ON DELETE CASCADE
            ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC
    """,
    """
    CREATE TABLE IF NOT EXISTS evaluations (
        url             VARCHAR(2048) CHARACTER SET ascii NOT NULL,
        token_precision DOUBLE        NOT NULL,
        token_recall    DOUBLE        NOT NULL,
        token_f1        DOUBLE        NOT NULL,
        char_precision  DOUBLE        NULL,
        char_recall     DOUBLE        NULL,
        char_f1         DOUBLE        NULL,
        bleu1           DOUBLE        NULL,
        created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (url),
        CONSTRAINT fk_eval_url FOREIGN KEY (url)
            REFERENCES web_resources(url)
            ON DELETE CASCADE
            ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC
    """,
    """
    CREATE TABLE IF NOT EXISTS judge_evaluations (
        url            VARCHAR(2048) CHARACTER SET ascii NOT NULL,
        model_name     VARCHAR(128)  NOT NULL,
        judge_score    INT           NOT NULL,
        judge_feedback LONGTEXT      NULL,
        created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (url),
        CONSTRAINT fk_judge_url FOREIGN KEY (url)
            REFERENCES web_resources(url)
            ON DELETE CASCADE
            ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC
    """,
]


def init_schema() -> None:
    #crea le tabelle se non esistono già
    with get_conn() as conn:
        cur = conn.cursor()
        for stmt in _SCHEMA_STATEMENTS:
            cur.execute(stmt)
        conn.commit()
    logger.info("Schema DB inizializzato")


#web resources

def upsert_web_resource(url: str, domain: str, title: str, html_text: str) -> None:
    sql = (
        "INSERT INTO web_resources (url, domain, title, html_text) "
        "VALUES (?, ?, ?, ?) "
        "ON DUPLICATE KEY UPDATE domain=VALUES(domain), title=VALUES(title), "
        "html_text=VALUES(html_text)"
    )
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (url, domain, title or "", html_text or ""))
        conn.commit()


def get_web_resource(url: str) -> Optional[dict]:
    sql = (
        "SELECT url, domain, title, html_text, created_at "
        "FROM web_resources WHERE url=?"
    )
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (url,))
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "url": row[0],
        "domain": row[1],
        "title": row[2] or "",
        "html_text": row[3] or "",
        "created_at": row[4].isoformat() if row[4] else None,
    }


def delete_web_resource(url: str) -> bool:
    #cancella la risorsa web
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM web_resources WHERE url=?", (url,))
        affected = cur.rowcount
        conn.commit()
    return affected > 0


def web_resource_exists(url: str) -> bool:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM web_resources WHERE url=?", (url,))
        return cur.fetchone() is not None


def list_urls_by_domain(domain: str) -> list[str]:
    sql = "SELECT url FROM web_resources WHERE domain=? ORDER BY url"
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (domain,))
        return [r[0] for r in cur.fetchall()]


#GS

def upsert_gold_standard(url: str, gold_text: str) -> None:
    sql = (
        "INSERT INTO gold_standard (url, gold_text) VALUES (?, ?) "
        "ON DUPLICATE KEY UPDATE gold_text=VALUES(gold_text)"
    )
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (url, gold_text or ""))
        conn.commit()


def get_gold_standard_entry(url: str) -> Optional[dict]:
    sql = (
        "SELECT w.url, w.domain, w.title, w.html_text, g.gold_text "
        "FROM gold_standard g "
        "JOIN web_resources w ON w.url = g.url "
        "WHERE g.url = ?"
    )
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (url,))
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "url": row[0],
        "domain": row[1],
        "title": row[2] or "",
        "html_text": row[3] or "",
        "gold_text": row[4] or "",
    }


def delete_gold_standard_entry(url: str) -> bool:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM gold_standard WHERE url=?", (url,))
        affected = cur.rowcount
        conn.commit()
    return affected > 0


def gold_standard_exists(url: str) -> bool:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM gold_standard WHERE url=?", (url,))
        return cur.fetchone() is not None


def list_gold_standard_urls(domain: str) -> list[str]:
    sql = (
        "SELECT g.url FROM gold_standard g "
        "JOIN web_resources w ON w.url = g.url "
        "WHERE w.domain = ? ORDER BY g.url"
    )
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (domain,))
        return [r[0] for r in cur.fetchall()]


def list_gold_standard_entries(domain: str) -> list[dict]:
    #tutti i record del GS per un dominio
    sql = (
        "SELECT w.url, w.domain, w.title, w.html_text, g.gold_text "
        "FROM gold_standard g "
        "JOIN web_resources w ON w.url = g.url "
        "WHERE w.domain = ? ORDER BY g.url"
    )
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (domain,))
        rows = cur.fetchall()
    return [
        {
            "url": r[0],
            "domain": r[1],
            "title": r[2] or "",
            "html_text": r[3] or "",
            "gold_text": r[4] or "",
        }
        for r in rows
    ]


#eval

def upsert_evaluation(url: str, metrics: dict) -> None:
    #salva le metriche per una URL.
    tle = metrics.get("token_level_eval", {})
    coe = metrics.get("char_overlap_eval", {})
    bleu = metrics.get("bleu1_eval", {})
    sql = (
        "INSERT INTO evaluations "
        "(url, token_precision, token_recall, token_f1, "
        " char_precision, char_recall, char_f1, bleu1) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON DUPLICATE KEY UPDATE "
        " token_precision=VALUES(token_precision), "
        " token_recall=VALUES(token_recall), "
        " token_f1=VALUES(token_f1), "
        " char_precision=VALUES(char_precision), "
        " char_recall=VALUES(char_recall), "
        " char_f1=VALUES(char_f1), "
        " bleu1=VALUES(bleu1)"
    )
    args = (
        url,
        float(tle.get("precision", 0.0)),
        float(tle.get("recall", 0.0)),
        float(tle.get("f1", 0.0)),
        float(coe.get("char_precision", 0.0)) if coe else None,
        float(coe.get("char_recall", 0.0)) if coe else None,
        float(coe.get("char_f1", 0.0)) if coe else None,
        float(bleu.get("bleu1", 0.0)) if bleu else None,
    )
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, args)
        conn.commit()


def get_evaluation(url: str) -> Optional[dict]:
    sql = (
        "SELECT token_precision, token_recall, token_f1, "
        "char_precision, char_recall, char_f1, bleu1 "
        "FROM evaluations WHERE url=?"
    )
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (url,))
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "token_level_eval": {
            "precision": row[0], "recall": row[1], "f1": row[2],
        },
        "char_overlap_eval": {
            "char_precision": row[3], "char_recall": row[4], "char_f1": row[5],
        } if row[3] is not None else {},
        "bleu1_eval": {"bleu1": row[6]} if row[6] is not None else {},
    }


#judge_evaluations

def upsert_judge_evaluation(
    url: str, model_name: str, judge_score: int, judge_feedback: str
) -> None:
    sql = (
        "INSERT INTO judge_evaluations "
        "(url, model_name, judge_score, judge_feedback) "
        "VALUES (?, ?, ?, ?) "
        "ON DUPLICATE KEY UPDATE "
        " model_name=VALUES(model_name), "
        " judge_score=VALUES(judge_score), "
        " judge_feedback=VALUES(judge_feedback)"
    )
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (url, model_name, int(judge_score), judge_feedback or ""))
        conn.commit()


def get_judge_evaluation(url: str) -> Optional[dict]:
    sql = (
        "SELECT model_name, judge_score, judge_feedback "
        "FROM judge_evaluations WHERE url=?"
    )
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (url,))
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "model_name": row[0],
        "judge_score": int(row[1]),
        "judge_feedback": row[2] or "",
    }


#stats

def count_web_resources_by_domain() -> dict[str, int]:
    sql = "SELECT domain, COUNT(*) FROM web_resources GROUP BY domain"
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        return {r[0]: int(r[1]) for r in cur.fetchall()}


def count_gold_standard_by_domain() -> dict[str, int]:
    sql = (
        "SELECT w.domain, COUNT(*) FROM gold_standard g "
        "JOIN web_resources w ON w.url = g.url "
        "GROUP BY w.domain"
    )
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        return {r[0]: int(r[1]) for r in cur.fetchall()}


def avg_evaluations_by_domain() -> dict[str, dict]:
    #media delle metriche di evaluations raggruppata per dominio
    sql = (
        "SELECT w.domain, "
        " AVG(e.token_precision), AVG(e.token_recall), AVG(e.token_f1), "
        " AVG(e.char_precision), AVG(e.char_recall), AVG(e.char_f1), "
        " AVG(e.bleu1) "
        "FROM evaluations e "
        "JOIN web_resources w ON w.url = e.url "
        "GROUP BY w.domain"
    )
    out: dict[str, dict] = {}
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        for row in cur.fetchall():
            domain = row[0]
            entry: dict = {
                "token_level_eval": {
                    "precision": round(float(row[1] or 0.0), 4),
                    "recall": round(float(row[2] or 0.0), 4),
                    "f1": round(float(row[3] or 0.0), 4),
                }
            }
            if row[4] is not None:
                entry["char_overlap_eval"] = {
                    "char_precision": round(float(row[4]), 4),
                    "char_recall": round(float(row[5] or 0.0), 4),
                    "char_f1": round(float(row[6] or 0.0), 4),
                }
            if row[7] is not None:
                entry["bleu1_eval"] = {"bleu1": round(float(row[7]), 4)}
            out[domain] = entry
    return out


def avg_judge_by_domain() -> dict[str, dict]:
    sql = (
        "SELECT w.domain, AVG(j.judge_score), COUNT(*) "
        "FROM judge_evaluations j "
        "JOIN web_resources w ON w.url = j.url "
        "GROUP BY w.domain"
    )
    out: dict[str, dict] = {}
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        for row in cur.fetchall():
            out[row[0]] = {
                "judge_score": round(float(row[1] or 0.0), 4),
                "n": int(row[2]),
            }
    return out


#schema info

def get_schema_info() -> dict:
    info: dict[str, dict] = {}
    with get_conn() as conn:
        cur = conn.cursor()

        cur.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=? ORDER BY TABLE_NAME",
            (_DB_NAME,),
        )
        tables = [r[0] for r in cur.fetchall()]

        for table in tables:
            cur.execute(
                "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=? AND TABLE_NAME=? "
                "ORDER BY ORDINAL_POSITION",
                (_DB_NAME, table),
            )
            cols = cur.fetchall()

            cur.execute(
                "SELECT k.COLUMN_NAME, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE k "
                "WHERE k.TABLE_SCHEMA=? AND k.TABLE_NAME=? "
                "AND k.REFERENCED_TABLE_NAME IS NOT NULL",
                (_DB_NAME, table),
            )
            fk_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

            table_info: dict[str, str] = {}
            for col_name, col_type, _is_null, col_key in cols:
                desc = col_type
                tags: list[str] = []
                if col_key == "PRI":
                    tags.append("PK")
                if col_name in fk_map:
                    ref_table, ref_col = fk_map[col_name]
                    tags.append(f"FK({ref_table}.{ref_col})")
                if tags:
                    desc = f"{col_type}, {', '.join(tags)}"
                table_info[col_name] = desc
            info[table] = table_info
    return info
