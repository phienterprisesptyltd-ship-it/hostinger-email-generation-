"""ARCS Archaeology: layer 2.

Atomic propositions with full provenance, and a directed discovery graph that
records which observation or search generated which subsequent discovery.

Everything here writes to the *derived* database and requires an open
interpretation gate (:mod:`arcs_bridge.gate`): the raw layer must have verified
before anything interprets it.
"""

from .propositions import (  # noqa: F401
    PROBABILITY_ROLES,
    VERIFICATION_STATUSES,
    add_proposition,
    correct_proposition,
    get_proposition,
    list_propositions,
    proposition_history,
    retract_proposition,
    verify_quote,
)
from .graph import (  # noqa: F401
    add_edge,
    add_search_event,
    ancestors,
    descendants,
    discovery_chain,
    to_dot,
    to_json,
)
