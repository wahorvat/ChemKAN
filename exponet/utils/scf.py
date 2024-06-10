
"""Interaction with Hartree-Fock solver in pyscf."""

# Abbreviations used:
# SCF: self-consistent field (method). Another name for Hartree-Fock
# HF: Hartree-Fock method.
# RHF: restricted Hartre-Fock. Require molecular orbital for the i-th alpha-spin
#   and i-th beta-spin electrons to have the same spatial component.
# ROHF: restricted open-shell Hartree-Fock. Same as RHF except allows the number
#   of alpha and beta electrons to differ.
# UHF: unrestricted Hartre-Fock. Permits breaking of spin symmetry and hence
#   alpha and beta electrons to have different spatial components.
# AO: Atomic orbital. Underlying basis set (typically Gaussian-type orbitals and
#   built into pyscf).
# MO: molecular orbitals/Hartree-Fock orbitals. Single-particle orbitals which
#   are solutions to the Hartree-Fock equations.


from typing import Optional, Sequence, Tuple, Union

from absl import logging
from exponet.utils import gto
from exponet.utils import system
import jax.numpy as jnp
import jax.tree_util
import numpy as np
import pyscf
import pyscf.gto
import pyscf.lib
import pyscf.scf


NDArray = Union[jnp.ndarray, np.ndarray]

# For excitations, tuple of (spin, orbital) pairs giving the orbitals which are
# swapped.
SpinOrbitalSwap = tuple[tuple[int, int], tuple[int, int]]


class Scf:
  """Helper class for running Hartree-Fock (self-consistent field) with pyscf.

  Attributes:
    molecule: list of system.Atom objects giving the atoms in the
      molecule and their positions.
    nelectrons: Tuple with number of alpha electrons and beta
      electrons.
    basis: Basis set to use, best specified with the relevant string
      for a built-in basis set in pyscf. A user-defined basis set can be used
      (advanced). See https://sunqm.github.io/pyscf/gto.html#input-basis for
        more details.
    pyscf_mol: the PySCF 'Molecule'. If this is passed to the init,
      the molecule, nelectrons, and basis will not be used, and the
      calculations will be performed on the existing pyscf_mol
    restricted: If true, use the restricted Hartree-Fock method, otherwise use
      the unrestricted Hartree-Fock method.
    mean_field: the actual UHF object.
    mo_coeff: The molecular orbital coefficients computed by Hartree-Fock.
    excitations: Stores a list of atomic orbitals to swap to construct excited
      states of the lowest energy.
  """

  def __init__(self,
               molecule: Optional[Sequence[system.Atom]] = None,
               nelectrons: Optional[Tuple[int, int]] = None,
               basis: Optional[str] = 'cc-pVTZ',
               pyscf_mol: Optional[pyscf.gto.Mole] = None,
               restricted: bool = True):
    if pyscf_mol:
      self._mol = pyscf_mol
      # Create pure-JAX Mol object so that GTOs can be evaluated in traced
      # JAX functions
      self._mol_jax = gto.Mol.from_pyscf_mol(self._mol)
    else:
      self.molecule = molecule
      self.nelectrons = nelectrons
      self.basis = basis
      self._spin = nelectrons[0] - nelectrons[1]
      self._mol = None

    self.restricted = restricted
    self.mean_field = None
    self.excitations = None

    pyscf.lib.param.TMPDIR = None

  def run(self,
          dm0: Optional[np.ndarray] = None,
          excitations: int = 0):
    """Runs the Hartree-Fock calculation.

    Args:
      dm0: Optional density matrix to initialize the calculation.
      excitations: Stores a list of atomic orbitals to swap to construct excited
        states of the lowest energy.

    Returns:
      A pyscf scf object (i.e. pyscf.scf.rhf.RHF, pyscf.scf.uhf.UHF or
      pyscf.scf.rohf.ROHF depending on the spin and restricted settings).

    Raises:
      RuntimeError: If the number of electrons in the PySCF molecule is not
      consistent with self.nelectrons.
    """
    # If not passed a pyscf molecule, create one
    if not self._mol:
      if any(atom.atomic_number - atom.charge > 1.e-8
             for atom in self.molecule):
        logging.info(
            'Fractional nuclear charge detected. '
            'Running SCF on atoms with integer charge.'
        )

      nuclear_charge = sum(atom.atomic_number for atom in self.molecule)
      charge = nuclear_charge - sum(self.nelectrons)
      self._mol = pyscf.gto.Mole(
          atom=[[atom.symbol, atom.coords] for atom in self.molecule],
          unit='bohr')
      self._mol.basis = self.basis
      self._mol.spin = self._spin
      self._mol.charge = charge
      self._mol.build()
      if self._mol.nelectron != sum(self.nelectrons):
        raise RuntimeError('PySCF molecule not consistent with QMC molecule.')
      self._mol_jax = gto.Mol.from_pyscf_mol(self._mol)
    if self.restricted:
      self.mean_field = pyscf.scf.RHF(self._mol)
    else:
      self.mean_field = pyscf.scf.UHF(self._mol)
    try:
      self.mean_field.kernel(dm0=dm0)
    except TypeError:
      logging.info('Mean-field solver does not support specifying an initial '
                   'density matrix.')
      # 1e solvers (e.g. uhf.HF1e) do not take any keyword arguments.
      self.mean_field.kernel()
    if excitations > 0:
      self.excitations = get_excitations(
          self.mean_field, n=excitations, preserve_spin=True)
    return self.mean_field

  @property
  def mo_coeff(self) -> Optional[np.ndarray]:
    if self.mean_field.mo_coeff is None:
      return None
    elif self.restricted:
      return np.asarray(self.mean_field.mo_coeff)[None]
    else:
      return np.asarray(self.mean_field.mo_coeff)

  @mo_coeff.setter
  def mo_coeff(self, mo_coeff):
    if (self.mean_field is not None and
        self.mean_field.mo_coeff is not None and
        self.mean_field.mo_coeff.ndim != mo_coeff.ndim):
      raise ValueError('Attempting to override mo_coeffs with different rank. '
                       f'Got {mo_coeff.shape=}, have '
                       f'{self.mean_field.mo_coeff.shape=}')
    self.mean_field.mo_coeff = mo_coeff

  def eval_mos(self, positions: NDArray) -> Tuple[NDArray, NDArray]:
    """Evaluates the Hartree-Fock single-particle orbitals at a set of points.

    Args:
      positions: numpy array of shape (N, 3) of the positions in space at which
        to evaluate the Hartree-Fock orbitals.

    Returns:
      Pair of numpy float64 arrays of shape (N, M) (deriv=False) or (4, N, M)
      (deriv=True), where 2M is the number of Hartree-Fock orbitals. The (i-th,
      j-th) element in the first (second) array gives the value of the j-th
      alpha (beta) Hartree-Fock orbital at the i-th electron position in
      positions. For restricted (RHF, ROHF) calculations, the two arrays will be
      identical.
      If deriv=True, the first index contains [value, x derivative, y
      derivative, z derivative].

    Raises:
      RuntimeError: If Hartree-Fock calculation has not been performed using
        `run`.
      NotImplementedError: If Hartree-Fock calculation used Cartesian
        Gaussian-type orbitals as the underlying basis set.
    """
    if self.mean_field is None:
      raise RuntimeError('Mean-field calculation has not been run.')
    if self.restricted:
      coeffs = (self.mean_field.mo_coeff,)
    else:
      coeffs = self.mean_field.mo_coeff
    # Assumes self._mol.cart (use of Cartesian Gaussian-type orbitals and
    # integrals) is False (default behaviour of pyscf).
    if self._mol.cart:
      raise NotImplementedError(
          'Evaluation of molecular orbitals using cartesian GTOs.')
    ao_values = self._mol_jax.eval_gto(positions)
    mo_values = tuple(jnp.matmul(ao_values, coeff) for coeff in coeffs)
    if self.restricted:
      # duplicate for beta electrons.
      mo_values *= 2
    return mo_values

  def eval_orbitals(self,
                    pos: NDArray,
                    nspins: Tuple[int, int]) -> Tuple[NDArray, NDArray]:
    """Evaluates SCF orbitals at a set of positions.

    Args:
      pos: an array of electron positions to evaluate the orbitals at, of shape
        (..., nelec*3), where the leading dimensions are arbitrary, nelec is the
        number of electrons and the spin up electrons are ordered before the
        spin down electrons.
      nspins: tuple with number of spin up and spin down electrons.

    Returns:
      tuple with matrices of orbitals for spin up and spin down electrons, with
      the same leading dimensions as in pos.
    """
    if not isinstance(pos, np.ndarray):  # works even with JAX array
      try:
        pos = pos.copy()
      except AttributeError as exc:
        raise ValueError('Input must be either NumPy or JAX array.') from exc
    leading_dims = pos.shape[:-1]
    # split into separate electrons
    pos = jnp.reshape(pos, [-1, 3])  # (batch*nelec, 3)
    mos = self.eval_mos(pos)  # (batch*nelec, nbasis), (batch*nelec, nbasis)
    # Reshape into (batch, nelec, nbasis) for each spin channel.
    mos = [jnp.reshape(mo, leading_dims + (sum(nspins), -1)) for mo in mos]
    # Return (using Aufbau principle) the matrices for the occupied alpha and
    # beta orbitals. Number of alpha electrons given by nspins[0].
    alpha_spin = mos[0][..., :nspins[0], :nspins[0]]
    beta_spin = mos[1][..., nspins[0]:, :nspins[1]]
    if self.excitations is not None:
      # Some indexing gymnastics to get out excited states as well.
      # Excited states are given along the second index, after the batch.
      alpha_spins = [alpha_spin]
      beta_spins = [beta_spin]
      for excitation in self.excitations:
        alpha_excited = alpha_spin.copy()
        beta_excited = beta_spin.copy()
        for occ_index, unocc_index in excitation[2]:
          spin_occ, i_occ = occ_index
          spin_unocc, i_unocc = unocc_index
          if spin_occ == 0:
            alpha_excited = alpha_excited.at[..., i_occ].set(
                mos[spin_unocc][..., :nspins[0], i_unocc])
          elif spin_occ == 1:
            beta_excited = beta_excited.at[..., i_occ].set(
                mos[spin_unocc][..., nspins[0]:, i_unocc])
          else:
            raise ValueError(f'Invalid {spin_occ=}')
        alpha_spins.append(alpha_excited)
        beta_spins.append(beta_excited)
      alpha_spin = jnp.stack(alpha_spins, axis=-3)
      beta_spin = jnp.stack(beta_spins, axis=-3)

    return alpha_spin, beta_spin


# pylint: disable=protected-access
def scf_flatten(scf: Scf):
  # `children` are fields which require an extra leading dimension with pmap
  children = ()
  # `aux_data` are any fields which are required to reconstruct the original
  # object, but would not actually end up as data in the compiled graph.
  aux_data = (scf.mo_coeff,
              scf._mol_jax._spec,
              scf._mol,
              scf.restricted,
              scf.excitations)
  return children, aux_data


def scf_unflatten(aux_data, children) -> Scf:
  assert not children  # children should be empty.
  mo_coeff, spec, mol, restricted, excitations = aux_data
  scf = Scf(pyscf_mol=mol.copy(), restricted=restricted)
  scf.mo_coeff = mo_coeff
  scf._mol_jax._spec = spec
  scf.excitations = excitations
  return scf
# pylint: enable=protected-access


jax.tree_util.register_pytree_node(Scf, scf_flatten, scf_unflatten)


def get_excitations(
    mean_field: ...,
    n: int = 10,
    preserve_spin: bool = False
) -> list[tuple[float, int, list[SpinOrbitalSwap]]]:
  """Compute energies of lowest n single/double excitations from HF result.

  Args:
     mean_field: scf mean_field returned by pyscf.
     n: Maximum number of excitations to return.
     preserve_spin: If true, return only excitations which preserve spin.

  Returns:
    List of at most n excitations, where each excitation is a tuple of energy
    difference of the HF eigenvalues of the occupied orbitals, change in spin,
    followed by an arbitrary number of tuples containing pairs of occupied /
    unoccupied indices, each of which is a tuple of (spin, orbital), giving the
    orbitals which are swapped.
  """
  occ, energy = mean_field.mo_occ, mean_field.mo_energy
  nocc = [int(occ[spin].sum()) for spin in range(2)]
  norb = [len(occ[spin]) for spin in range(2)]
  # A list of tuples containing energy difference, change in spin, followed by
  # an arbitrary number of tuples containing pairs of occupied / unoccupied
  # indices, each of which is a tuple of (spin, orbital), giving the orbitals
  # which are swapped.
  res = []

  # Single excitations

  for spin_occ in range(2):
    for i_occ in range(nocc[spin_occ]):
      for spin_unocc in range(2):
        for i_unocc in range(nocc[spin_unocc], norb[spin_unocc]):
          delta_e = (energy[spin_unocc][i_unocc] - energy[spin_occ][i_occ])
          res.append((delta_e, spin_occ - spin_unocc,
                      [((spin_occ, i_occ), (spin_unocc, i_unocc))]))

  # Double excitations
  for spin_occ1 in range(2):
    for i_occ1 in range(nocc[spin_occ1]):
      for spin_unocc1 in range(2):
        for i_unocc1 in range(nocc[spin_unocc1], norb[spin_unocc1]):
          for spin_occ2 in range(2):
            for i_occ2 in range(nocc[spin_occ2]):
              for spin_unocc2 in range(2):
                for i_unocc2 in range(nocc[spin_unocc2], norb[spin_unocc2]):
                  occ_index1 = spin_occ1, i_occ1
                  occ_index2 = spin_occ2, i_occ2
                  unocc_index1 = spin_unocc1, i_unocc1
                  unocc_index2 = spin_unocc2, i_unocc2
                  if ((occ_index1 < occ_index2) and
                      (unocc_index1 < unocc_index2)):
                    delta_e = (
                        energy[spin_unocc1][i_unocc1] +
                        energy[spin_unocc2][i_unocc2] -
                        energy[spin_occ1][i_occ1] - energy[spin_occ2][i_occ2])
                    res.append(
                        (delta_e,
                         spin_occ1 + spin_occ2 - spin_unocc1 - spin_unocc2, [
                             (occ_index1, unocc_index1),
                             (occ_index2, unocc_index2)
                         ]))

  if preserve_spin:
    res = [x for x in res if x[1] == 0]
  if len(res) < n:
    raise ValueError('Insufficient single and double excitations. '
                     f'Want {n}, have {len(res)}. Try a larger basis set?')
  # Take the first n excitations.
  # Note that this has a heavy bias towards single excitations.
  return sorted(res, key=lambda x: x[0])[:n]
