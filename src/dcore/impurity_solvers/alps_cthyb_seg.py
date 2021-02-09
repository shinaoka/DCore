#
# DCore -- Integrated DMFT software for correlated electrons
# Copyright (C) 2017 The University of Tokyo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

import numpy
from scipy.linalg import block_diag
import os
from itertools import product
from triqs.gf import *
from h5 import HDFArchive
from triqs.operators import *
from ..tools import make_block_gf, launch_mpi_subprocesses, extract_H0, umat2dd, get_block_size
from .base import SolverBase


def to_numpy_array(g, names):
    """
    Convert BlockGf object to numpy.
    Rearrange spins and orbitals so that up and down spins appear alternatively.
    If there is a single block, we assume that spin and down spins appear alternatively.
    If there are two blocks, we assume that they are spin1 and spin2 sectors.
    """

    if g.n_blocks > 2:
        raise RuntimeError("n_blocks={} must be 1 or 2.".format(g.n_blocks))

    n_spin_orbital = numpy.sum([get_block_size(block) for name, block in g])

    # FIXME: Bit ugly
    n_data = g[names[0]].data.shape[0]

    data = numpy.zeros((n_data, n_spin_orbital, n_spin_orbital), dtype=complex)
    offset = 0
    for name in names:
        block = g[name]
        block_dim = get_block_size(block)
        data[:, offset:offset + block_dim, offset:offset + block_dim] = block.data
        offset += block_dim

    # from (up,orb1), (up,orb2), ..., (down,orb1), (down,orb2), ...
    # to (up,orb1), (down,orb1), (up,orb2), (down,orb2), ...
    norb = int(n_spin_orbital//2)
    index = numpy.zeros(n_spin_orbital, dtype=int)
    index[0::2] = numpy.arange(norb)
    index[1::2] = numpy.arange(norb) + norb
    # Swap cols and rows
    return (data[:, :, index])[:, index, :]


def assign_from_numpy_array(g, data, names):
    """
    Does inversion of to_numpy_array
    data[spin,orb,iw]
    g[spin].data[iw,orb1,orb2] 
    """
    # print(g.n_blocks)
    if g.n_blocks != 2:
        raise RuntimeError("n_blocks={} must be 1 or 2.".format(g.n_blocks))

    norb = data.shape[1]
    niw = data.shape[2]
    # print(data.shape)
    # print("norb:", norb)

    # check number of Matsubara frequency
    assert data.shape[2]*2 == g[names[0]].data.shape[0]
    # print(g[names[0]].data.shape)

    for spin in range(2):
        for orb in range(norb):
            # print(orb, spin, names[spin])
            # positive frequency
            g[names[spin]].data[niw:, orb, orb] = data[spin][orb][:]
            # negative frequency
            g[names[spin]].data[:niw, orb, orb] = numpy.conj(data[spin][orb][::-1])


def dcore2alpscore(dcore_U):

    dcore_U_len = len(dcore_U)
    alps_U = numpy.zeros((dcore_U_len, dcore_U_len), dtype=float)
    alps_Uprime = numpy.zeros((dcore_U_len, dcore_U_len), dtype=float)
    alps_J = numpy.zeros((dcore_U_len, dcore_U_len), dtype=float)

    # m_range = range(size)
    for i, j in product(list(range(dcore_U_len)), list(range(dcore_U_len))):
        alps_U[i, j] = dcore_U[i, j, i, j].real - dcore_U[i, j, j, i].real
        alps_Uprime[i, j] = dcore_U[i, j, i, j].real
        alps_J[i, j] = dcore_U[i, j, j, i].real
    return alps_U, alps_Uprime, alps_J

def write_Umatrix(U, Uprime, J, norb):
    Uout = numpy.zeros((norb, 2, norb, 2))

    # from (up,orb1), (up,orb2), ..., (down,orb1), (down,orb2), ...
    # to (up,orb1), (down,orb1), (up,orb2), (down,orb2), ...
    def func(u):
        uout = u.reshape((2, norb, 2, norb)).transpose(1, 0, 3, 2)
        return uout

    U_four = func(U)
    Uprime_four = func(Uprime)
    J_four = func(J)

    for a1, a2 in product(list(range(norb)), repeat=2):
        for s1, s2 in product(list(range(2)), repeat=2):  # spin-1/2
            if a1 == a2:
                Uout[a1, s1, a2, s2] = U_four[a1, s1, a2, s2]
            else:
                Uout[a1, s1, a2, s2] = Uprime_four[a1, s1, a2, s2] - J_four[a1, s1, a2, s2]

    Uout = Uout.reshape((2*norb, 2*norb))
    with open('./Umatrix', 'w') as f:
        for i in range(2*norb):
            for j in range(2*norb):
                print('{:.15e} '.format(Uout[i, j].real), file=f, end="")
            print("", file=f)


def set_tail(g_block):
    for bname, gf in g_block:
        gf.tail.zero()
        gf.tail[1] = numpy.identity(gf.N1)


class ALPSCTHYBSEGSolver(SolverBase):

    def __init__(self, beta, gf_struct, u_mat, n_iw=1025):
        """
        Initialize the solver.
        """

        super(ALPSCTHYBSEGSolver, self).__init__(beta, gf_struct, u_mat, n_iw)

        self.n_tau = max(10001, 5 * n_iw)

    def _get_occupation(self):
        """
        Read the spin-orbital-dependent occupation number from HDF5 file

        Returns
        -------
        numpy.ndarray of size (2*self.n_orb)

        """

        array = numpy.zeros(2*self.n_orb, dtype=float)
        with HDFArchive('sim.h5', 'r') as f:
            results = f["simulation"]["results"]
            for i1 in range(2*self.n_orb):
                group = "density_%d" % i1
                if group in results:
                    array[i1] = results[group]["mean"]["value"]

        # [(o1,s1)] -> [o1, s1] -> [s1, o1] -> [(s1, o1)]
        array = array.reshape((self.n_orb, 2))\
                     .transpose((1, 0))\
                     .reshape((2*self.n_orb))
        return array

    def _get_results(self, group_prefix, n_data, orbital_symmetrize, dtype=float, stop_if_data_not_exist=True):
        """
        Read results with two spin-orbital indices from HDF5 file

        Returns
        -------
        numpy.ndarray of size (2*self.n_orb, 2*self.n_orb, n_data)

        """

        data_shape = (2*self.n_orb, 2*self.n_orb, n_data)

        array = numpy.zeros(data_shape, dtype=dtype)
        with HDFArchive('sim.h5', 'r') as f:
            results = f["simulation"]["results"]
            for i1, i2 in product(list(range(2*self.n_orb)), repeat=2):
                group = "%s_%d_%d" % (group_prefix, i1, i2)
                if group in results:
                    array[i1, i2, :] = results[group]["mean"]["value"]
                    if orbital_symmetrize:  # Only i1>i2 is computed in CTQMC.
                        array[i2, i1, :] = array[i1, i2, :]
                elif stop_if_data_not_exist:
                    raise Exception("data does not exist in sim.h5/simulation/results/{}. alps_cthyb might be old.".format(group))


        # [(o1,s1), (o2,s2)] -> [o1, s1, o2, s2] -> [s1, o1, s2, o2] -> [(s1,o1), (s2,o2)]
        array = array.reshape((self.n_orb, 2, self.n_orb, 2, -1))\
                     .transpose((1, 0, 3, 2, 4))\
                     .reshape((2*self.n_orb, 2*self.n_orb, -1))
        return array

    def solve(self, rot, mpirun_command, params_kw):
        """
        In addition to the parameters described in the docstring of SolverBase,
        one can pass solver-dependent parameters using params_kw. For example,
          exec_path : str, path to an executable, mandatory
          dry_run   : bool, actual computation is not performed if dry_run is True, optional
        """
        internal_params = {
            'exec_path'           : '',
            'random_seed_offset'  : 0,
            'dry_run'             : False,
        }

        def _read(key):
            if key in params_kw:
                return params_kw[key]
            else:
                return internal_params[key]
        print (params_kw)

        umat_check = umat2dd(self.u_mat)
        assert numpy.allclose(umat_check, self.u_mat), "Please set density_density = True when you run ALPS/cthyb-seg!"

        # (1) Set configuration for the impurity solver
        # input:
        #   self.beta
        #   self.set_G0_iw
        #   self.u_mat
        #
        # Additionally, the following variables may used:
        #   self.n_orb
        #   self.n_flavor
        #   self.gf_struct

        # (1a) If H0 is necessary:
        # Non-interacting part of the local Hamiltonian including chemical potential
        # Make sure H0 is hermite.
        # Ordering of index in H0 is spin1, spin1, ..., spin2, spin2, ...
        H0 = extract_H0(self._G0_iw, self.block_names)

        # from (up,orb1), (up,orb2), ..., (down,orb1), (down,orb2), ...
        # to (up,orb1), (down,orb1), (up,orb2), (down,orb2), ...
        index = numpy.zeros((2*self.n_orb), dtype=int)
        index[0::2] = numpy.arange(self.n_orb)
        index[1::2] = numpy.arange(self.n_orb) + self.n_orb
        # Swap cols and rows
        H0 = (H0[:, index])[index, :]

        # (1b) If Delta(iw) and/or Delta(tau) are necessary:
        # Compute the hybridization function from G0:
        #     Delta(iwn_n) = iw_n - H0 - G0^{-1}(iw_n)
        # H0 is extracted from the tail of the Green's function.
        self._Delta_iw = delta(self._G0_iw)
        Delta_tau = make_block_gf(GfImTime, self.gf_struct, self.beta, self.n_tau)
        for name in self.block_names:
            Delta_tau[name] << InverseFourier(self._Delta_iw[name])
        Delta_tau_data = to_numpy_array(Delta_tau, self.block_names)

        # (1c) Set U_{ijkl} for the solver
        # Set up input parameters and files for ALPS/CTHYB-SEG

        p_run = {
            'SEED'                            : params_kw['random_seed_offset'],
            'FLAVORS'                         : self.n_orb*2,
            'BETA'                            : self.beta,
            'N'                               : self.n_tau - 1,
            'NMATSUBARA'                      : self.n_iw,
            'U_MATRIX'                        : 'Umatrix',
            'MU_VECTOR'                       : 'MUvector',
            'cthyb.DELTA'                     : 'delta',
        }

        if os.path.exists('./input.out.h5'):
            shutil.move('./input.out.h5', './input_prev.out.h5')
        # Set parameters specified by the user
        for k, v in list(params_kw.items()):
            if k in internal_params:
                continue
            if k in p_run:
                raise RuntimeError("Cannot override input parameter for ALPS/CTHYB-SEG: " + k)
            p_run[k] = v

        with open('./input.ini', 'w') as f:
            for k, v in list(p_run.items()):
                print(k, " = ", v, file=f)

        # TODO: check Delta_tau_deta
        #    Delta_{ab}(tau) should be diagonal, real, negative

        with open('./delta', 'w') as f:
            for itau in range(self.n_tau):
                print('{}'.format(itau), file=f, end="")
                for f1 in range(self.n_flavors):
                    if Delta_tau_data[itau, f1, f1].real >0:
                        Delta_tau_data[itau, f1, f1] = 0
                    print(' {:.15e}'.format(Delta_tau_data[itau, f1, f1].real), file=f, end="")
                print("", file=f)

        U, Uprime, J = dcore2alpscore(self.u_mat)
        write_Umatrix(U, Uprime, J, self.n_orb)

        with open('./MUvector', 'w') as f:
            for orb in range(self.n_orb):
                for spin in range(2):
                    print('{:.15e} '.format(-H0[2*orb+spin][2*orb+spin].real), file=f, end="")
            print("", file=f)

        if _read('dry_run'):
            return

        # Invoke subprocess
        exec_path = os.path.expandvars(_read('exec_path'))
        if exec_path == '':
            raise RuntimeError("Please set exec_path!")
        if not os.path.exists(exec_path):
            raise RuntimeError(exec_path + " does not exist. Set exec_path properly!")

        # (2) Run a working horse
        with open('./output', 'w') as output_f:
            launch_mpi_subprocesses(mpirun_command, [exec_path, './input.ini'], output_f)

        with open('./output', 'r') as output_f:
            for line in output_f:
                print(line, end='')

        # (3) Copy results into
        #   self._Sigma_iw
        #   self._Gimp_iw

        def set_blockgf_from_h5(sigma, group):
            swdata = numpy.zeros((2, self.n_orb, self.n_iw), dtype=complex)
            with HDFArchive('sim.h5', 'r') as f:
                for orb in range(self.n_orb):
                    for spin in range(2):
                        swdata_array = f[group][str(orb*2+spin)]["mean"]["value"]
                        assert swdata_array.dtype == numpy.complex
                        assert swdata_array.shape == (self.n_iw,)
                        swdata[spin, orb, :] = swdata_array
            assign_from_numpy_array(sigma, swdata, self.block_names)

        set_blockgf_from_h5(self._Sigma_iw, "S_omega")
        set_blockgf_from_h5(self._Gimp_iw, "G_omega")

        if triqs_major_version == 1:
            set_tail(self._Gimp_iw)

        #   self.quant_to_save['nn_equal_time']
        nn_equal_time = self._get_results("nn", 1, orbital_symmetrize=True, stop_if_data_not_exist=False)
        # [(s1,o1), (s2,o2), 0]
        self.quant_to_save['nn_equal_time'] = nn_equal_time[:, :, 0]  # copy

    def calc_G2loc_ph(self, rot, mpirun_command, num_wf, num_wb, params_kw):
        """
        compute local G2 in p-h channel
            X_loc = < c_{i1}^+ ; c_{i2} ; c_{i4}^+ ; c_{i3} >

        Parameters
        ----------
        rot
        mpirun_command
        num_wf
        num_wb
        params_kw

        Returns
        -------
        G2_loc : dict
            key = (i1, i2, i3, i4)
            val = numpy.ndarray(n_w2b, 2*n_w2f, 2*n_w2f)

        chi_loc : dict (None if not computed)
            key = (i1, i2, i3, i4)
            val = numpy.ndarray(n_w2b)
        """

        use_chi_loc = False

        params_kw['cthyb.MEASURE_g2w'] = 1
        params_kw['cthyb.N_w2'] = num_wf
        params_kw['cthyb.N_W'] = num_wb
        if use_chi_loc:
            params_kw['cthyb.MEASURE_nnw'] = 1

        self.solve(rot, mpirun_command, params_kw)

        # Save G2(wb, wf, wf')
        # [(s1,o1), (s2,o2), (wb,wf,wf')]
        g2_re = self._get_results("g2w_re", 4*num_wf*num_wf*num_wb, orbital_symmetrize=False)
        g2_im = self._get_results("g2w_im", 4*num_wf*num_wf*num_wb, orbital_symmetrize=False)
        g2_loc = (g2_re + g2_im * 1.0J) / self.beta
        g2_loc = g2_loc.reshape((2*self.n_orb, 2*self.n_orb) + (num_wb, 2*num_wf, 2*num_wf))
        # assign to dict
        g2_dict = {}
        for i1, i2 in product(list(range(2*self.n_orb)), repeat=2):
            g2_dict[(i1, i1, i2, i2)] = g2_loc[i1, i2]

        # return g2_loc for arbitrary wb including wb<0
        def get_g2(_i, _j, _wb, _wf1, _wf2):
            try:
                if _wb >= 0:
                    return g2_loc[_i, _j, _wb, _wf1, _wf2]
                else:
                    # G2_iijj(wb, wf, wf') = G2_jjii(-wb, -wf', -wf)^*
                    return numpy.conj(g2_loc[_j, _i, -_wb, -(1+_wf2), -(1+_wf1)])
            except IndexError:
                return 0

        # Convert G2_iijj -> G2_ijij
        g2_loc_tr = numpy.zeros(g2_loc.shape, dtype=complex)
        for i1, i2 in product(list(range(2*self.n_orb)), repeat=2):
            for wb in range(num_wb):
                for wf1, wf2 in product(list(range(2 * num_wf)), repeat=2):
                    # G2_ijij(wb, wf, wf') = -G2_iijj(wf-wf', wf'+wb, wf')^*
                    g2_loc_tr[i1, i2, wb, wf1, wf2] = -get_g2(i1, i2, wf1-wf2, wf2+wb, wf2)
        # assign to dict
        for i1, i2 in product(list(range(2*self.n_orb)), repeat=2):
            # exclude i1=i2, which was already assigned by g2_loc
            if i1 != i2:
                g2_dict[(i1, i2, i1, i2)] = g2_loc_tr[i1, i2]

        # Occupation number
        # [(s1,o1)]
        occup = self._get_occupation()

        # Save chi(wb)
        # [(s1,o1), (s2,o2), wb]
        chi_dict = None
        if use_chi_loc:
            chi_re = self._get_results("nnw_re", num_wb, orbital_symmetrize=True)
            chi_im = self._get_results("nnw_im", num_wb, orbital_symmetrize=True)
            chi_loc = chi_re + chi_im * 1.0J
            # subtract <n><n>
            chi_loc[:, :, 0] -= occup[:, None] * occup[None, :] * self.beta
            # assign to dict
            chi_dict = {}
            for i1, i2 in product(list(range(2*self.n_orb)), repeat=2):
                chi_dict[(i1, i1, i2, i2)] = chi_loc[i1, i2]

        return g2_dict, chi_dict

    def calc_G2loc_ph_sparse(self, rot, mpirun_command, freqs_ph, num_wb, params_kw):
        raise Exception("This solver does not support the sparse sampling.")

    def name(self):
        return "ALPS/cthyb-seg"