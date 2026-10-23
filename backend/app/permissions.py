"""
The list of things an employee ("staff" role) can be allowed to do.

This file is the single source of truth. The admin screen reads it from
GET /admin/users/permissions to draw the checkboxes, and the API checks the
same keys on every request. To add a permission later: add it to GROUPS here,
then guard the route that needs it (see `needs` in app/auth.py).

Admins and owners are never limited by this list; they can do everything.
Permissions are stored on the account as app_metadata.permissions (a list of
keys like "products.edit_price"), next to the role, so they travel inside the
sign-in token and need no database table.
"""
from typing import Iterable

# (key, label) pairs, shown in this order.
GROUPS: list[dict] = [
    {
        "key": "products",
        "label": "Products",
        "perms": [
            ("view", "View products"),
            ("create", "Create products"),
            ("edit_details", "Edit details (title, description, part numbers, specs)"),
            ("edit_price", "Edit price and cost"),
            ("publish", "Publish / unpublish, activate / deactivate"),
            ("photos", "Add, replace, remove and reorder photos"),
        ],
    },
    {
        "key": "inventory",
        "label": "Inventory",
        "perms": [
            ("adjust", "Adjust stock (quantity on hand, reorder point)"),
            ("receive", "Receive stock (scan new parts in)"),
            ("ship", "Ship orders (scan parts out)"),
            ("barcodes", "Link barcodes, set bin locations, print shelf labels"),
        ],
    },
    {
        "key": "fitment",
        "label": "Fitment",
        "perms": [
            ("view", "View fitment"),
            ("add", "Add fitment (including marking a part universal)"),
            ("remove", "Remove fitment"),
        ],
    },
]

ALL_KEYS: set[str] = {f"{g['key']}.{k}" for g in GROUPS for k, _ in g["perms"]}

# Granting the left one always grants the right ones too (you cannot edit what
# you cannot open).
IMPLIES: dict[str, list[str]] = {
    "products.create": ["products.view"],
    "products.edit_details": ["products.view"],
    "products.edit_price": ["products.view"],
    "products.publish": ["products.view"],
    "products.photos": ["products.view"],
    "inventory.adjust": ["products.view"],
    "fitment.add": ["fitment.view"],
    "fitment.remove": ["fitment.view"],
}

# Which permission a product field needs in order to be CHANGED.
# Any field not listed needs products.edit_details.
FIELD_PERMS: dict[str, str] = {
    "list_price": "products.edit_price",
    "cost": "products.edit_price",
    "quantity_on_hand": "inventory.adjust",
    "reorder_point": "inventory.adjust",
    "is_published": "products.publish",
    "is_active": "products.publish",
    "is_universal": "fitment.add",
}


def label_for(key: str) -> str:
    for g in GROUPS:
        for k, label in g["perms"]:
            if f"{g['key']}.{k}" == key:
                return label
    return key


def catalog() -> list[dict]:
    """The checkbox list, in the shape the admin screen uses."""
    return [
        {
            "key": g["key"],
            "label": g["label"],
            "perms": [{"key": f"{g['key']}.{k}", "label": label} for k, label in g["perms"]],
        }
        for g in GROUPS
    ]


def normalize(perms: Iterable[str]) -> list[str]:
    """Validate, add implied permissions, drop duplicates. Raises ValueError on unknown keys."""
    out: set[str] = set()
    for p in perms:
        if p not in ALL_KEYS:
            raise ValueError(f"Unknown permission: {p}")
        out.add(p)
        out.update(IMPLIES.get(p, []))
    return sorted(out)
