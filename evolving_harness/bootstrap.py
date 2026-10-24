from importlib.resources import files

from .domain import compile_improver
from .store import Store


def bootstrap(store: Store, owner: str, project: str, *, demo: bool = False) -> dict:
    if not owner.strip() or not project.strip():
        raise ValueError("User and project cannot be empty")
    resources = files("evolving_harness") / "resources"
    seeds = {
        "memory-policy": ("memory_policy", {
            "verified_only": not demo, "exclude_expired": not demo,
            "deduplicate": False, "top_k": 10,
        }),
        "improve": ("improver", compile_improver([])),
        "coding": ("task_skill", {"instructions": (resources / "coding.md").read_text(encoding="utf-8")}),
    }
    for name, (kind, payload) in seeds.items():
        try:
            store.current(owner, project, name)
        except ValueError:
            store.create_artifact(owner, project, name, kind, payload, backend="demo-seed" if demo else "seed")
    return {name: store.current(owner, project, name) for name in seeds}
