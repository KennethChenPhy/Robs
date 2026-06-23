"""Phase 2 models — optional signal augmentation."""

from robs.models.attractor import AttractorResult, analyze_attractor
from robs.models.ising import IsingFitResult, rolling_ising
from robs.models.spins import encode_spins, spin_matrix

__all__ = [
    "encode_spins",
    "spin_matrix",
    "rolling_ising",
    "IsingFitResult",
    "analyze_attractor",
    "AttractorResult",
]
