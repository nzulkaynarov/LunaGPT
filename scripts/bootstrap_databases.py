#!/usr/bin/env python3
"""
LunaGPT Database Bootstrapper and Migration Helper.

Ensures that:
1. `lunagpt` database exists with `vector` and `uuid-ossp` extensions.
2. `openwebui` database exists with `vector` and `uuid-ossp` extensions.
3. Obsolete Open WebUI tables in `lunagpt` are cleanly migrated to `openwebui`.
"""

import json
import logging
import os
import sys

import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bootstrap_databases")


def bootstrap() -> None:
    postgres_user = os.getenv("POSTGRES_USER", "postgres")
    postgres_password = os.getenv("POSTGRES_PASSWORD", "lunagpt_secure_postgres_pass")
    postgres_host = os.getenv("POSTGRES_HOST", "localhost")
    postgres_port = int(os.getenv("POSTGRES_PORT", "5433"))

    admin_conn_str = f"postgresql://{postgres_user}:{postgres_password}@{postgres_host}:{postgres_port}/postgres"

    logger.info("Connecting to PostgreSQL admin on %s:%s...", postgres_host, postgres_port)
    try:
        with psycopg.connect(admin_conn_str, autocommit=True) as conn:
            with conn.cursor() as cur:
                for db_name in ("lunagpt", "openwebui"):
                    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
                    if not cur.fetchone():
                        logger.info("Creating database %s...", db_name)
                        cur.execute(f'CREATE DATABASE "{db_name}"')
                    else:
                        logger.info("Database %s already exists.", db_name)
    except Exception as exc:
        logger.error("Failed to connect to PostgreSQL admin: %s", exc)
        sys.exit(1)

    for db_name in ("lunagpt", "openwebui"):
        db_conn_str = f"postgresql://{postgres_user}:{postgres_password}@{postgres_host}:{postgres_port}/{db_name}"
        try:
            with psycopg.connect(db_conn_str, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                    cur.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
                    logger.info("Extensions enabled on database %s.", db_name)
        except Exception as exc:
            logger.error("Failed to configure database %s: %s", db_name, exc)
            sys.exit(1)

    # Migrate any legacy document_chunk table from lunagpt to openwebui
    lunagpt_conn_str = f"postgresql://{postgres_user}:{postgres_password}@{postgres_host}:{postgres_port}/lunagpt"
    openwebui_conn_str = f"postgresql://{postgres_user}:{postgres_password}@{postgres_host}:{postgres_port}/openwebui"

    try:
        with psycopg.connect(lunagpt_conn_str) as conn_src:
            with conn_src.cursor() as cur_src:
                cur_src.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='document_chunk'"
                )
                if cur_src.fetchone():
                    logger.info("Found legacy document_chunk table in lunagpt. Migrating to openwebui...")
                    with psycopg.connect(openwebui_conn_str, autocommit=True) as conn_dst:
                        with conn_dst.cursor() as cur_dst:
                            cur_dst.execute(
                                """
                                CREATE TABLE IF NOT EXISTS document_chunk (
                                    id text PRIMARY KEY,
                                    collection_name text,
                                    text text,
                                    vector vector(1024),
                                    vmetadata jsonb
                                );
                            """
                            )
                            cur_src.execute("SELECT id, collection_name, text, vector, vmetadata FROM document_chunk")
                            rows = cur_src.fetchall()
                            for r in rows:
                                meta = json.dumps(r[4]) if isinstance(r[4], (dict, list)) else r[4]
                                cur_dst.execute(
                                    """
                                    INSERT INTO document_chunk (id, collection_name, text, vector, vmetadata)
                                    VALUES (%s, %s, %s, %s, %s)
                                    ON CONFLICT (id) DO NOTHING;
                                """,
                                    (r[0], r[1], r[2], r[3], meta),
                                )
                            logger.info("Migrated %d chunks to openwebui.document_chunk.", len(rows))
                    with psycopg.connect(lunagpt_conn_str, autocommit=True) as conn_drop:
                        with conn_drop.cursor() as cur_drop:
                            cur_drop.execute("DROP TABLE IF EXISTS document_chunk")
                            logger.info("Cleaned up legacy document_chunk table from lunagpt.")
    except Exception as exc:
        logger.warning("Legacy table check/migration notice: %s", exc)

    logger.info("Database bootstrap completed successfully.")


if __name__ == "__main__":
    bootstrap()
