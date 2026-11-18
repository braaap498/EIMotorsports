"""
Submodels as models.

A submodel is a normal row in vehicle_models (so it shows in the website's vehicle lists) that points at the model it came
under (parent_model_id) and remembers eBay's exact text for it (ebay_submodel). Going back to eBay, a fitment row on a submodel
is sent as the PARENT's make and model plus that text as Submodel (see ebay_newdraft._fitment_rows).

Rule used everywhere on the website: a model and its submodels are one family. A part linked to the model or to any one of its
submodels shows for the model and for every submodel of it. (Going back to eBay stays exact: a link on a submodel is sent as that
Submodel, a link on the model itself is sent without one.)
"""
import re
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


def clean_sub(sub: Optional[str]) -> str:
    return " ".join(str(sub or "").split())


def _nk(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def child_name(parent_name: str, sub: str) -> str:
    """'Outlander' + '400' -> 'Outlander 400'; a submodel that already contains the model name stays as it is."""
    p, s = parent_name.strip(), clean_sub(sub)
    if _nk(p) and _nk(p) in _nk(s):
        return s
    return f"{p} {s}".strip()


def child_for(db: Session, parent_id: int, sub: str, create: bool = True) -> Optional[int]:
    """The model for this submodel under this parent. Finds it by eBay's text (case and spacing ignored) and otherwise creates it.

    Returns the parent itself when there is nothing to split off: the text is the same as the parent's own name, or the
    parent is already a submodel (submodels are one level deep). Returns None only when create is False and it does not exist."""
    sub = clean_sub(sub)
    if not sub:
        return parent_id
    parent = db.execute(
        text("select id, make_id, vehicle_type_id, name, parent_model_id from vehicle_models where id = :p"), {"p": parent_id}
    ).first()
    if parent is None:
        return None
    if parent.parent_model_id is not None or _nk(sub) == _nk(parent.name):
        return parent_id
    hit = db.execute(
        text("select id from vehicle_models where parent_model_id = :p and lower(btrim(ebay_submodel)) = lower(:s) order by id limit 1"),
        {"p": parent_id, "s": sub},
    ).first()
    if hit:
        return int(hit.id)
    if not create:
        return None
    name = child_name(parent.name, sub)
    # A model of this make with the same name may already exist as a stand-alone model (the database allows one per make and name).
    # It is the same machine, so it becomes the submodel instead of a duplicate being made: it keeps its id, its years and its parts.
    # Not adopted when it is the parent itself, already has submodels, or already belongs to another model.
    same = db.execute(
        text(
            "select m.id, m.parent_model_id from vehicle_models m "
            "where m.make_id = :mk and m.id <> :p and (m.name = :n or fitment_key(m.name) = fitment_key(cast(:n as text))) "
            "order by (m.name = :n) desc, m.id limit 1"
        ),
        {"mk": parent.make_id, "p": parent_id, "n": name},
    ).first()
    if same is not None and same.parent_model_id is None and not db.execute(
        text("select 1 from vehicle_models where parent_model_id = :i limit 1"), {"i": same.id}
    ).first():
        db.execute(
            text(
                "update vehicle_models set parent_model_id = :p, ebay_submodel = :e, "
                "vehicle_type_id = coalesce(vehicle_type_id, :t) where id = :i"
            ),
            {"p": parent_id, "e": sub, "t": parent.vehicle_type_id, "i": same.id},
        )
        return int(same.id)
    if same is not None:
        # taken by something that cannot be adopted: keep the name unique by adding the model it sits under
        name = f"{name} ({parent.name})"
        n = 1
        while db.execute(text("select 1 from vehicle_models where make_id = :m and name = :n"), {"m": parent.make_id, "n": name}).first():
            n += 1
            name = f"{child_name(parent.name, sub)} ({parent.name} {n})"
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "model"
    slug, n = base, 1
    while db.execute(text("select 1 from vehicle_models where make_id = :m and slug = :s"), {"m": parent.make_id, "s": slug}).first():
        n += 1
        slug = f"{base}-{n}"
    return int(
        db.execute(
            text(
                "insert into vehicle_models (make_id, vehicle_type_id, name, slug, parent_model_id, ebay_submodel) "
                "values (:m, :t, :n, :s, :p, :e) returning id"
            ),
            {"m": parent.make_id, "t": parent.vehicle_type_id, "n": name, "s": slug, "p": parent_id, "e": sub},
        ).scalar()
    )


def family_sql(param: str = "model_id") -> str:
    """SQL for 'the family of this model': the model its submodels hang under, that model itself, and all of its submodels.
    Picking the model or any one of its submodels finds the parts linked to any of them."""
    root = f"(select coalesce(r.parent_model_id, r.id) from vehicle_models r where r.id = cast(:{param} as bigint))"
    return f"(select m.id from vehicle_models m where m.id = {root} or m.parent_model_id = {root})"
