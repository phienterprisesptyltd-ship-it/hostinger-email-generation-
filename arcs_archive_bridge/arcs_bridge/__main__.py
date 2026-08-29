"""``python -m arcs_bridge`` - the same entry point as the ``arcs`` command."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
