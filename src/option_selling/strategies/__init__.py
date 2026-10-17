"""Strategy registry. Future strategies register here for the session and a strategy selector."""

from .base import Skip, Strategy
from .iron_condor_v1 import SPEC as IRON_CONDOR_V1_SPEC
from .iron_condor_v1 import EntryPlan, IronCondorV1

STRATEGIES = {IronCondorV1.name: IronCondorV1}

__all__ = ["STRATEGIES", "EntryPlan", "IRON_CONDOR_V1_SPEC", "IronCondorV1", "Skip", "Strategy"]
