"""ARCS Archive Bridge.

A local-only, privacy-preserving, provenance-complete archive for ChatGPT
conversation history and associated ARCS research.

Layer 1 (this package's core) is the *source layer*: raw capture, hashing,
provenance, normalisation, reconstruction and completeness verification.

Layer 2 (``arcs_bridge.archaeology``) is the *ARCS Archaeology layer*: atomic
propositions and the directed discovery graph.  Everything in layer 2 lives in
a physically separate database file and is gated behind a passing layer-1
verification run (see :mod:`arcs_bridge.gate`).
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
