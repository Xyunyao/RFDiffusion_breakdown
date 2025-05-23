# code updated from https://github.com/qazwsxal/diffusion-extensions
from math import pi
import torch

from torch.distributions import Distribution, constraints, Normal, MultivariateNormal

from util import *


class IsotropicGaussianSO3(Distribution):
    arg_constraints = {'eps': constraints.positive}

    def __init__(self, eps: torch.Tensor, mean: torch.Tensor = torch.eye(3)):
        self.eps = eps
        self._mean = mean.to(self.eps)
        self._mean_inv = self._mean.transpose(-1, -2)  # orthonormal so inverse = Transpose
        pdf_sample_locs = pi * torch.linspace(0, 1.0, 1000) ** 3.0  # Pack more samples near 0
        pdf_sample_locs = pdf_sample_locs.to(self.eps).unsqueeze(-1)
        # As we're sampling using axis-angle form
        # and need to account for the change in density
        # Scale by 1-cos(t)/pi for sampling
        with torch.no_grad():
            pdf_sample_vals = self._eps_ft(pdf_sample_locs) * ((1 - pdf_sample_locs.cos()) / pi)
        # Set to 0.0, otherwise there's a divide by 0 here
        pdf_sample_vals[(pdf_sample_locs == 0).expand_as(pdf_sample_vals)] = 0.0

        # Trapezoidal integration
        pdf_val_sums = pdf_sample_vals[:-1, ...] + pdf_sample_vals[1:, ...]
        pdf_loc_diffs = torch.diff(pdf_sample_locs, dim=0)
        self.trap = (pdf_loc_diffs * pdf_val_sums / 2).cumsum(dim=0)
        self.trap = self.trap/self.trap[-1,None]
        self.trap_loc = pdf_sample_locs[1:]
        super().__init__()

    def sample(self, sample_shape=torch.Size()):
        # Consider axis-angle form.
        axes = torch.randn((*sample_shape, *self.eps.shape, 3)).to(self.eps)
        axes = axes / axes.norm(dim=-1, keepdim=True)
        # Inverse transform sampling based on numerical approximation of CDF
        unif = torch.rand((*sample_shape, *self.eps.shape), device=self.trap.device)
        idx_1 = (self.trap <= unif[None, ...]).sum(dim=0)
        idx_0 = torch.clamp(idx_1 - 1,min=0)

        trap_start = torch.gather(self.trap, 0, idx_0[..., None])[..., 0]
        trap_end = torch.gather(self.trap, 0, idx_1[..., None])[..., 0]

        trap_diff = torch.clamp((trap_end - trap_start), min=1e-6)
        weight = torch.clamp(((unif - trap_start) / trap_diff), 0, 1)
        angle_start = self.trap_loc[idx_0, 0]
        angle_end = self.trap_loc[idx_1, 0]
        angles = torch.lerp(angle_start, angle_end, weight)[..., None]
        out = self._mean @ aa_to_rmat(axes, angles)
        return out

    def _eps_ft(self, t: torch.Tensor) -> torch.Tensor:
        var_d = self.eps.double()**2
        t_d = t.double()
        vals = sqrt(pi) * var_d ** (-3 / 2) * torch.exp(var_d / 4) * torch.exp(-((t_d / 2) ** 2) / var_d) \
               * (t_d - torch.exp((-pi ** 2) / var_d)
                  * ((t_d - 2 * pi) * torch.exp(pi * t_d / var_d) + (
                            t_d + 2 * pi) * torch.exp(-pi * t_d / var_d))
                  ) / (2 * torch.sin(t_d / 2))
        vals[vals.isinf()] = 0.0
        vals[vals.isnan()] = 0.0

        # using the value of the limit t -> 0 to fix nans at 0
        t_big, _ = torch.broadcast_tensors(t_d, var_d)
        # Just trust me on this...
        # This doesn't fix all nans as a lot are still too big to flit in float32 here
        vals[t_big == 0] = sqrt(pi) * (var_d * torch.exp(2 * pi ** 2 / var_d)
                                       - 2 * var_d * torch.exp(pi ** 2 / var_d)
                                       + 4 * pi ** 2 * var_d * torch.exp(pi ** 2 / var_d)
                                       ) * torch.exp(var_d / 4 - (2 * pi ** 2) / var_d) / var_d ** (5 / 2)
        return vals.float()

    def log_prob(self, rotations):
        _, angles = rmat_to_aa(rotations)
        probs = self._eps_ft(angles)
        return probs.log()

    @property
    def mean(self):
        return self._mean


class IGSO3xR3(Distribution):
    arg_constraints = {'eps': constraints.positive}

    def __init__(self, eps: torch.Tensor, mean: AffineT = None, shift_scale=1.0):
        self.eps = eps
        if mean == None:
            rot = torch.eye(3).unsqueeze(0)
            shift = torch.zeros(*eps.shape, 3).to(eps)  #
            mean = AffineT(shift=shift, rot=rot)
        self._mean = mean.to(eps)
        self.igso3 = IsotropicGaussianSO3(eps=eps, mean=self._mean.rot)
        self.r3 = Normal(loc=self._mean.shift, scale=eps[..., None] * shift_scale)
        super().__init__()

    def sample(self, sample_shape=torch.Size()):
        rot = self.igso3.sample(sample_shape)
        shift = self.r3.sample(sample_shape)
        return AffineT(rot, shift)

    def log_prob(self, value):
        rot_prob = self.igso3.log_prob(value.rot)
        shift_prob = self.r3.log_prob(value.shift)
        return rot_prob + shift_prob

    @property
    def mean(self):
        return self._mean


class Bingham(MultivariateNormal):
    arg_constraints = {'covariance_matrix': constraints.positive_definite,
                       'precision_matrix': constraints.positive_definite,
                       'scale_tril': constraints.lower_cholesky}
    support = constraints.real_vector

    def __init__(self, loc, covariance_matrix=None, precision_matrix=None, scale_tril=None, validate_args=None):
        # Location is always 0, axisymmetric distribution
        loc = torch.zeros_like(loc)
        super().__init__(loc, covariance_matrix, precision_matrix, scale_tril, validate_args)

    def rsample(self, sample_shape=torch.Size()):
        vals = super().rsample(sample_shape)
        out = vals / vals.norm(dim=-1, keepdim=True)
        return out


if __name__ == "__main__":
    import matplotlib.pyplot as plt

    device = torch.device(f"cuda") if torch.cuda.is_available() else torch.device("cpu")
    # Test bingham distribution and MMD numbers
    # Small, uncorrelated rotations
    cov1 = torch.diag(torch.tensor([1000.0, 0.1, 0.1, 0.1], device=device))
    # Small, similar-axis rotations, ijk parts need to be correlated
    cov2 = torch.tensor([
        [1e05, 0.00, 0.00, 0.00],
        [0.00, 1.00, 0.99, 0.99],
        [0.00, 0.99, 1.00, 0.99],
        [0.00, 0.99, 0.99, 1.00],
    ], device=device)
    #
    # Big, similar-axis rotations, ijk parts need to be correlated
    cov3 = torch.tensor([
        [1.00, 0.00, 0.00, 0.00],
        [0.00, 1.00, 0.90, 0.90],
        [0.00, 0.90, 1.00, 0.90],
        [0.00, 0.90, 0.90, 1.00],
    ], device=device)
    #

    bing1 = Bingham(loc=torch.zeros(4, device=device), covariance_matrix=cov1)
    bing2 = Bingham(loc=torch.zeros(4, device=device), covariance_matrix=cov2)

    b1samp_1 = bing1.sample((10_000,))
    b1samp_2 = bing1.sample((10_000,))
    b2samp_1 = bing2.sample((10_000,))
    # Convert to rmat
    rb1samp_1 = quat_to_rmat(b1samp_1)
    rb1samp_2 = quat_to_rmat(b1samp_2)
    rb2samp_1 = quat_to_rmat(b2samp_1)

    for samples in (rb1samp_1, rb2samp_1):
        fig = plt.figure()
        ax = fig.add_subplot(projection='3d')
        ax.scatter(*samples[:1000, 0, :].T.cpu())
        ax.scatter(*samples[:1000, 1, :].T.cpu())
        ax.scatter(*samples[:1000, 2, :].T.cpu())
        ax.set_xlim3d(-1, 1)
        ax.set_ylim3d(-1, 1)
        ax.set_zlim3d(-1, 1)
    plt.show()

    with torch.no_grad():
        same_test = Ker_2samp_log_prob(rb1samp_1, rb1samp_2, rmat_gaussian_kernel, chunksize=4000)
        diff_test = Ker_2samp_log_prob(rb2samp_1, rb1samp_1, rmat_gaussian_kernel, chunksize=4000)
    print("MMD same test:", (same_test))
    print("MMD diff test:", (diff_test))

    axis = torch.randn((3,))
    axis = (axis / axis.norm(dim=-1, p=2, keepdim=True)).repeat(100, 1)
    axis.requires_grad = True
    angle = torch.linspace(0.001, pi / 2, steps=100).unsqueeze(-1)
    angle.requires_grad = True
    rmats = aa_to_rmat(axis, angle)
    dist2 = IsotropicGaussianSO3(torch.tensor(0.1))
    l_probs = dist2.log_prob(rmats)
    grads = torch.autograd.grad(l_probs.sum(), rmats, retain_graph=True)
    print('aaaaa')