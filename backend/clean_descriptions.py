"""
One-time cleanup of descriptions already in the database.

    python clean_descriptions.py            # preview: shows how many rows would change
    python clean_descriptions.py --apply    # writes the cleaned text

Run from the backend folder (the one that contains app/), with the same
environment variables the API uses (DATABASE_URL etc.). The store already
cleans descriptions when it serves them, so this only tidies what is stored.
"""
import sys

from sqlalchemy import text

from app.database import get_engine
from app.sanitize import clean_description


def main(apply: bool) -> None:
    changed = 0
    with get_engine().begin() as conn:
        rows = conn.execute(
            text("select id, description from products where description is not null")
        ).all()
        for row in rows:
            new = clean_description(row.description)
            if new == row.description:
                continue
            changed += 1
            if apply:
                conn.execute(
                    text("update products set description = :d where id = :id"),
                    {"d": new, "id": row.id},
                )
    print(f"{len(rows)} descriptions checked, {changed} {'updated' if apply else 'would change'}.")
    if not apply and changed:
        print("Run again with --apply to save the changes.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
