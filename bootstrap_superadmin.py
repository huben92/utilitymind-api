"""Create the first super admin once and print its generated initial password."""

import argparse
import os
import secrets

import psycopg

from auth import normalize_username, password_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="superadmin")
    args = parser.parse_args()
    username = normalize_username(args.username)
    password = secrets.token_urlsafe(24)
    database_url = os.getenv("DATABASE_URL", "postgresql://wheelworks:wheelworks_local@localhost:5433/wheelworks")
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("LOCK TABLE app_users IN EXCLUSIVE MODE")
            cursor.execute("SELECT 1 FROM app_users WHERE role='super_admin' LIMIT 1")
            if cursor.fetchone():
                raise SystemExit("A super admin already exists; no credentials were changed.")
            cursor.execute("INSERT INTO app_users (username,password_hash,role) VALUES (%s,%s,'super_admin')",
                           (username, password_hash(password)))
    print(f"Username: {username}")
    print(f"Password: {password}")


if __name__ == "__main__":
    main()
