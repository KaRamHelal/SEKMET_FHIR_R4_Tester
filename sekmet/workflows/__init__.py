"""Hospital workflows. Importing the modules registers them in REGISTRY."""


def load_all() -> None:
    from . import adt, billing, clinical, orders, scheduling  # noqa: F401
