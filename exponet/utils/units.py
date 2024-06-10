
"""Basic definition of units and converters useful for chemistry."""

from typing import TypeVar
import numpy as np

# 1 Bohr = 0.52917721067 (12) x 10^{-10} m
# https://physics.nist.gov/cgi-bin/cuu/Value?bohrrada0
# Note: pyscf uses a slightly older definition of 0.52917721092 angstrom.
ANGSTROM_BOHR = 0.52917721067
BOHR_ANGSTROM = 1. / ANGSTROM_BOHR

# 1 Hartree = 627.509474 kcal/mol
# https://en.wikipedia.org/wiki/Hartree
KCAL_HARTREE = 627.509474
HARTREE_KCAL = 1. / KCAL_HARTREE

NumericalLike = TypeVar('NumericalLike', float, np.ndarray)


def bohr2angstrom(x_b: NumericalLike) -> NumericalLike:
  return x_b * ANGSTROM_BOHR


def angstrom2bohr(x_a: NumericalLike) -> NumericalLike:
  return x_a * BOHR_ANGSTROM


def hartree2kcal(x_b: NumericalLike) -> NumericalLike:
  return x_b * KCAL_HARTREE


def kcal2hartree(x_a: NumericalLike) -> NumericalLike:
  return x_a * HARTREE_KCAL
