import os

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def get_all_companies():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM companies ORDER BY id;")
            return cur.fetchall()
    finally:
        conn.close()


def insert_company(name, website, sector, country, revenue, employees, description):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO companies (name, website, sector, country, revenue, employees, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
                """,
                (name, website, sector, country, revenue, employees, description),
            )
        conn.commit()
    finally:
        conn.close()
