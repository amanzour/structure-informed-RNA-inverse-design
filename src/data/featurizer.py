######################################################################
# Geometric RNA Design, Joshi et al.
# Original repository: https://github.com/chaitjo/geometric-rna-design
######################################################################

import math
import numpy as np
from typing import Optional, Tuple
import torch
import torch.nn.functional as F
import torch_geometric
from torch_geometric.utils import coalesce, to_undirected
import torch_cluster
from sklearn.cluster import DBSCAN
import random
from src.data.data_utils import *
from src.data.sec_struct_utils import get_unpaired

from src.constants import RNA_NUCLEOTIDES, RNA_ATOMS, DISTANCE_EPS


class RNAGraphFeaturizer(object):
    """RNA Graph Featurizer
    
    Builds 3-bead coarse grained representation of an RNA backbone: (P, C4', N1 or N9).

    Returned graph is of type `torch_geometric.data.Data` with attributes:
    - seq        sequence converted to int tensor, shape [num_nodes]
    - node_s     node scalar features, shape [num_nodes, num_conf, num_bb_atoms x 5] 
    - node_v     node vector features, shape [num_nodes, num_conf, 2 + (num_bb_atoms - 1), 3]
    - edge_s     edge scalar features, shape [num_edges, num_conf, num_bb_atoms x num_rbf + num_bb_atoms]
    - edge_v     edge vector features, shape [num_edges, num_conf, num_bb_atoms, 3]
    - edge_index edge indices, shape [2, num_edges]
    - mask       node mask, `False` for nodes with missing data

    Args:
        split: train/validation/test split; coords are noised during training
        radius: maximum distance in space to qualify for edge in DBSCAN
        primary_dist: minimum distance on sequence to qualify for edge in DBSCAN
        base_pairing: software for secondary structure base-pairing identification
        num_rbf: number of radial basis functions
        num_posenc: number of positional encodings per edge
        max_num_conformers: maximum number of conformers sampled per sequence
        noise_scale: standard deviation of gaussian noise added to coordinates
    """
    def __init__(
            self,
            split = 'train',
            radius = 10,
            primary_dist = 500,
            base_pairing = 'sec_bp_list',
            num_rbf = 32,
            max_num_conformers = 3,
            noise_scale = 0.1,
            distance_eps = DISTANCE_EPS,
            device = 'cpu'
        ):
        super().__init__()

        self.split = split
        self.radius = radius
        self.primary_dist = primary_dist
        self.base_pairing = base_pairing
        self.num_rbf = num_rbf
        self.max_num_conformers = max_num_conformers
        self.noise_scale = noise_scale
        self.distance_eps = distance_eps
        self.device = device

        # nucleotide mapping: {'A': 0, 'G': 1, 'C': 2, 'U': 3, '_': 4}
        self.letter_to_num = dict(zip(
            RNA_NUCLEOTIDES, 
            list(range(len(RNA_NUCLEOTIDES)))
        ))
        self.num_to_letter = {v:k for k, v in self.letter_to_num.items()}
        self.letter_to_num["_"] = len(self.letter_to_num)  # unknown nucleotide

    def __call__(self, rna):
        with torch.no_grad():
            # Target sequence: num_res x 1
            seq = torch.as_tensor(
                [self.letter_to_num[residue] for residue in rna['sequence']], 
                device=self.device, 
                dtype=torch.long
            )

            # Set of coordinates: num_conf x num_res x num_bb_atoms x 3
            coords_list, basepairs_all, mask_coords, mask_confs = get_k_random_entries_and_masks_2(
                rna['coords_list'], rna[self.base_pairing], k = self.max_num_conformers
            )

            coords_list = torch.as_tensor(
                coords_list, 
                device=self.device, 
                dtype=torch.float32
            )

            # Add gaussian noise during training 
            # (prevent overfitting on crystalisation artifacts)
            if self.split == 'train':
                coords_list += torch.randn_like(coords_list, device=self.device) * self.noise_scale

            # Mask for missing coordinates for any backbone atom: num_res
            mask_coords = torch.BoolTensor(mask_coords)
            # Also mask non-standard nucleotides
            mask_coords = (mask_coords) & (seq != self.letter_to_num["_"])

            # Node internal coordinates (scalars) and normalised vectors
            dihedrals, angles, lengths = internal_coords(coords_list, mask_coords.unsqueeze(0).expand(self.max_num_conformers, -1))
            angle_stack = torch.cat([dihedrals, angles], dim=-1)
            lengths = torch.log(lengths + self.distance_eps)
            internal_coords_feat = torch.cat([torch.cos(angle_stack), torch.sin(angle_stack), lengths], dim=-1)
            internal_vecs_feat = internal_vecs(coords_list)
            
            # Remove residues with missing coordinates or non-standard nucleotides


            seq = seq[mask_coords]
            coords_list = coords_list[:, mask_coords] # [:, :, 1]  # only retain C4'
            internal_coords_feat = internal_coords_feat[:, mask_coords]
            internal_vecs_feat = internal_vecs_feat[:, mask_coords]
            if (mask_coords == False).sum() > 0:
                basepairs_all = offset_basepairs(basepairs_all,mask_coords)
            # Mask for extra coordinates if fewer than num_conf: num_res x num_conf
            mask_confs = torch.BoolTensor(mask_confs).repeat(len(seq), 1)

            edge_index = []
           
            # ===============modified============================
            unpaired_all = [get_unpaired(len(seq),basepairs_all[i]) for i in range(len(basepairs_all))]
            all_idx =  torch.tensor([i for i in range(len(seq))])
            # primary structure edges
            edge_index_primary = torch.stack((all_idx[:-1],all_idx[1:]), dim = 0)
            edge_index_primary_all = to_undirected(edge_index_primary)
            edge_index_secondary_all = []
            edge_index_tertiary_all = []
            for k in range(self.max_num_conformers):
                
                # secondary structure edges
                bp_all = basepairs_all[k]
                bp = [bp_all[i] for i in range(len(bp_all)) if abs(bp_all[i][1] - bp_all[i][0]) != 1]
                bp1 = [bp[i][0]-1  for i in range(len(bp))]
                bp2 = [bp[i][1]-1  for i in range(len(bp))]
                edge_index_secondary = to_undirected(torch.stack((torch.tensor(bp1),torch.tensor(bp2)),dim = 0))
                edge_index_secondary_all.append(edge_index_secondary)

                # tertiary structure edges
                #========================================================================comment out to eliminate tertiary-type edges=================================================
                edge_index_tertiary_directed = unpaired_cluster_dbscan(unpaired_all[k], coords_list[k], primary_dist = self.primary_dist, dbscan_eps = self.radius)
                if len(edge_index_tertiary_directed[0]) > 2*seq.shape[0]:
                    choices=random.sample(range(len(edge_index_tertiary_directed[0])),2*seq.shape[0])
                    edge_index_tertiary_directed=edge_index_tertiary_directed[:,choices]
               
                edge_index_tertiary = to_undirected(edge_index_tertiary_directed)    
                #========================================================================end==========================================================================================
                # edge_index_tertiary = torch.empty((2,0), dtype=torch.int64)       
                edge_index_tertiary_all.append(edge_index_tertiary)

            edge_index_list = []
            edge_vectors_list = []
            edge_lengths_list = []
            p_s_t_map_list = []
            dim1 = coords_list[0,0].shape[0];dim2 = coords_list[0,0].shape[1]
            for k in range(self.max_num_conformers):
                primary = coords_list[k][edge_index_primary_all[0]] - coords_list[k][edge_index_primary_all[1]]
                secondary=torch.empty(0,dim1,dim2)
                if edge_index_secondary_all[k].shape[1] > 0:
                    secondary = coords_list[k][edge_index_secondary_all[k][0]] - coords_list[k][edge_index_secondary_all[k][1]]
                tertiary=torch.empty(0,dim1,dim2)
                if edge_index_tertiary_all[k].shape[1] > 0:
                    tertiary = coords_list[k][edge_index_tertiary_all[k][0]] - coords_list[k][edge_index_tertiary_all[k][1]]
                cur_edge_index = torch.cat((edge_index_primary_all,edge_index_secondary_all[k],edge_index_tertiary_all[k]), dim = -1)
                cur_edge_vectors = torch.cat((primary, secondary, tertiary), dim = 0)
                cur_edge_lengths = torch.sqrt((cur_edge_vectors ** 2).sum(dim=-1) + self.distance_eps)
                p_mask=torch.tensor([0]); s_mask=torch.tensor([1]); t_mask=torch.tensor([2])
                cur_p_s_t_map = torch.cat((p_mask.repeat(edge_index_primary_all.shape[-1]),
                                           s_mask.repeat(edge_index_secondary_all[k].shape[-1]),
                                           t_mask.repeat(edge_index_tertiary_all[k].shape[-1])), dim = 0)
                
                edge_index_list.append(cur_edge_index)
                edge_vectors_list.append(normed_vec(cur_edge_vectors))
                edge_lengths_list.append(cur_edge_lengths)
                p_s_t_map_list.append(cur_p_s_t_map) 

            # Reshape: num_res x num_conf x ...
            coords_list = coords_list.permute(1, 0, 2, 3) # coords_list[:, :, 1].permute(1, 0, 2)
            internal_coords_feat = internal_coords_feat.permute(1, 0, 2)
            internal_vecs_feat = internal_vecs_feat.permute(1, 0, 2, 3)
            
            node_s = internal_coords_feat
            node_v = internal_vecs_feat
            
            edge_v_list = edge_vectors_list
            edge_s_list = []
            for k in range(self.max_num_conformers):
                cur_edge_rbf = rbf_expansion(edge_lengths_list[k], num_rbf=self.num_rbf)
                edge_s_list.append(torch.cat([cur_edge_rbf, torch.log(edge_lengths_list[k])], dim=-1))
            
        data = torch_geometric.data.Data(
            seq = seq,                  # num_res x 1
            node_s = node_s,            # num_res x num_conf x (num_bb_atoms x 5)
            node_v = node_v,            # num_res x num_conf x (2 + (num_bb_atoms - 1)) x 3
            edge_s_list = edge_s_list,            # num_conf x num_edges x (num_bb_atoms x num_rbf + num_bb_atoms)
            edge_v_list = edge_v_list,            # num_conf x num_edges x num_bb_atoms x 3
            edge_index_list = edge_index_list,    # list of 2 x num_edges
            mask_confs = mask_confs,    # num_res x num_conf
            mask_coords = mask_coords,  # num_res
            p_s_t_map_list = p_s_t_map_list,      # primary/secondary/tertiary edge type
        )
        
        return data
    
    def featurize(self, rna):
        """
        Featurize RNA backbone from dictionary of tensors.

        Args:
            rna (dict): Raw RNA data dictionary with keys:
                - sequence (str): RNA sequence of length `num_residues`.
                - coords_list (Tensor): Backbone coordinates with shape
                    `(num_conformations, num_residues, num_bb_atoms, 3)`.
        """
        return self(rna)
    
    def featurize_from_pdb_file(self, pdb_filepath):
        """
        Featurize RNA backbone from PDB file.

        Args:
            pdb_filepath (str): Path to PDB file.
        """
        sequence, coords, sec_struct, _ , sec_bp = pdb_to_tensor_2(
            pdb_filepath, return_sec_struct=True, return_sasa=False)
        coords = get_backbone_coords(coords, sequence)
        rna = {
            'sequence': sequence,
            'coords_list': [coords],
            'sec_struct_list': [sec_struct],
            'sec_bp_list': [sec_bp],
        }
        return self(rna), rna
    
    def featurize_from_pdb_filelist(self, pdb_filelist):
        """
        Featurize RNA backbone from list of PDB files corresponding to the
        same RNA, i.e. multiple conformations of the same RNA.

        Args:
            pdb_filelist (list): List of PDB filepaths.
        """
        # read first pdb file
        sequence, coords, sec_struct, _ , sec_bp = pdb_to_tensor_2(
            pdb_filelist[0], return_sec_struct=True, return_sasa=False)
        coords = get_backbone_coords(coords, sequence)
        rna = {
            'sequence': sequence,
            'coords_list': [coords],
            'sec_struct_list': [sec_struct],
            'sec_bp_list': [sec_bp],
        }

        # read remaining pdb files
        for pdb_filepath in pdb_filelist[1:]:
            sequence, coords, sec_struct, _ , sec_bp = pdb_to_tensor_2(
                pdb_filepath, return_sec_struct=True, return_sasa=False)
            coords = get_backbone_coords(coords, sequence)
            assert sequence == rna['sequence'], "All PDBs must have the same sequence"
            rna['coords_list'].append(coords)
            rna['sec_struct_list'].append(sec_struct)
            rna['sec_bp_list'].append(sec_bp),
        
        return self(rna), rna


def internal_coords(
    X: torch.Tensor,
    C: Optional[torch.Tensor] = None,
    return_masks: bool = False,
    distance_eps: float = DISTANCE_EPS,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Internal coordinates layer for RNA.

    This layer computes internal coordinates (ICs) from a batch of RNA
    backbones. To make the ICs differentiable everywhere, this layer replaces
    distance calculations of the form `sqrt(sum_sq)` with smooth, non-cusped
    approximation `sqrt(sum_sq + eps)`.
    
    Adapted from Chroma. In our case, num_batch == num_conformations, so we
    could almost directly repurpose their batched featurisation code in torch.

    Args:
        distance_eps (float, optional): Small parameter to add to squared
            distances to make gradients smooth near 0.

    Inputs:
        X (Tensor): Backbone coordinates with shape
            `(num_batch, num_residues, num_atom_types, 3)`.
        C (Tensor): Chain map tensor with shape
            `(num_batch, num_residues)`.

    Outputs:
        dihedrals (Tensor): Backbone dihedral angles with shape
            `(num_batch, num_residues, 4)`
        angles (Tensor): Backbone bond lengths with shape
            `(num_batch, num_residues, 4)`
        lengths (Tensor): Backbone bond lengths with shape
            `(num_batch, num_residues, 4)`
    """
    mask = (C > 0).float()
    X_chain = X[:, :, :2, :]
    num_batch, num_residues, _, _ = X_chain.shape
    X_chain = X_chain.reshape(num_batch, 2 * num_residues, 3)

    # This function historically returns the angle complement
    _lengths = lambda Xi, Xj: lengths(Xi, Xj, distance_eps=distance_eps)
    _angles = lambda Xi, Xj, Xk: np.pi - angles(
        Xi, Xj, Xk, distance_eps=distance_eps
    )
    _dihedrals = lambda Xi, Xj, Xk, Xl: dihedrals(
        Xi, Xj, Xk, Xl, distance_eps=distance_eps
    )

    # Compute internal coordinates associated with -[P]-[C4']-
    PC4p_L = _lengths(X_chain[:, 1:, :], X_chain[:, :-1, :])
    PC4p_A = _angles(X_chain[:, :-2, :], X_chain[:, 1:-1, :], X_chain[:, 2:, :])
    PC4p_D = _dihedrals(
        X_chain[:, :-3, :],
        X_chain[:, 1:-2, :],
        X_chain[:, 2:-1, :],
        X_chain[:, 3:, :],
    )

    # Compute internal coordinates associated with [C4']-[N]
    X_P, X_C4p, X_N = X.unbind(dim=2)
    X_P_next = X[:, 1:, 0, :]
    N_L = _lengths(X_C4p, X_N)
    N_A = _angles(X_P, X_C4p, X_N)
    N_D = _dihedrals(X_P_next, X_N[:, :-1, :], X_C4p[:, :-1, :], X_P[:, :-1, :])

    if C is None:
        C = torch.zeros_like(mask)

    # Mask nonphysical bonds and angles
    # Note: this could probably also be expressed as a Conv, unclear
    # which is faster and this probably not rate-limiting.
    C = C * (mask.type(torch.long))
    ii = torch.stack(2 * [C], dim=-1).view([num_batch, -1])
    L0, L1 = ii[:, :-1], ii[:, 1:]
    A0, A1, A2 = ii[:, :-2], ii[:, 1:-1], ii[:, 2:]
    D0, D1, D2, D3 = ii[:, :-3], ii[:, 1:-2], ii[:, 2:-1], ii[:, 3:]

    # Mask for linear backbone
    mask_L = torch.eq(L0, L1)
    mask_A = torch.eq(A0, A1) * torch.eq(A0, A2)
    mask_D = torch.eq(D0, D1) * torch.eq(D0, D2) * torch.eq(D0, D3)
    mask_L = mask_L.type(torch.float32)
    mask_A = mask_A.type(torch.float32)
    mask_D = mask_D.type(torch.float32)

    # Masks for branched nitrogen
    mask_N_D = torch.eq(C[:, :-1], C[:, 1:])
    mask_N_D = mask_N_D.type(torch.float32)
    mask_N_A = mask
    mask_N_L = mask

    def _pad_pack(D, A, L, N_D, N_A, N_L):
        # Pad and pack together the components
        D = F.pad(D, (1, 2))
        A = F.pad(A, (1, 1))
        L = F.pad(L, (0, 1))
        N_D = F.pad(N_D, (0, 1))
        D, A, L = [x.reshape(num_batch, num_residues, 2) for x in [D, A, L]]
        _pack = lambda a, b: torch.cat([a, b.unsqueeze(-1)], dim=-1)
        L = _pack(L, N_L)
        A = _pack(A, N_A)
        D = _pack(D, N_D)
        return D, A, L

    D, A, L = _pad_pack(PC4p_D, PC4p_A, PC4p_L, N_D, N_A, N_L)
    mask_D, mask_A, mask_L = _pad_pack(
        mask_D, mask_A, mask_L, mask_N_D, mask_N_A, mask_N_L
    )
    mask_expand = mask.unsqueeze(-1)
    mask_D = mask_expand * mask_D
    mask_A = mask_expand * mask_A
    mask_L = mask_expand * mask_L

    D = mask_D * D
    A = mask_A * A
    L = mask_L * L

    if not return_masks:
        return D, A, L
    else:
        return D, A, L, mask_D, mask_A, mask_L
    

def normed_vec(V: torch.Tensor, distance_eps: float = DISTANCE_EPS) -> torch.Tensor:
    """Normalized vectors with distance smoothing.

    This normalization is computed as `U = V / sqrt(|V|^2 + eps)` to avoid cusps
    and gradient discontinuities.

    Args:
        V (Tensor): Batch of vectors with shape `(..., num_dims)`.
        distance_eps (float, optional): Distance smoothing parameter for
            for computing distances as `sqrt(sum_sq) -> sqrt(sum_sq + eps)`.
            Default: 1E-3.

    Returns:
        U (Tensor): Batch of normalized vectors with shape `(..., num_dims)`.
    """
    # Unit vector from i to j
    mag_sq = (V ** 2).sum(dim=-1, keepdim=True)
    mag = torch.sqrt(mag_sq + distance_eps)
    U = V / mag
    return U


def normed_cross(
    V1: torch.Tensor, V2: torch.Tensor, distance_eps: float = DISTANCE_EPS
) -> torch.Tensor:
    """Normalized cross product between vectors.

    This normalization is computed as `U = V / sqrt(|V|^2 + eps)` to avoid cusps
    and gradient discontinuities.

    Args:
        V1 (Tensor): Batch of vectors with shape `(..., 3)`.
        V2 (Tensor): Batch of vectors with shape `(..., 3)`.
        distance_eps (float, optional): Distance smoothing parameter for
            for computing distances as `sqrt(sum_sq) -> sqrt(sum_sq + eps)`.
            Default: 1E-3.

    Returns:
        C (Tensor): Batch of cross products `v_1 x v_2` with shape `(..., 3)`.
    """
    C = normed_vec(torch.cross(V1, V2, dim=-1), distance_eps=distance_eps)
    return C


def lengths(
    atom_i: torch.Tensor, atom_j: torch.Tensor, distance_eps: float = DISTANCE_EPS
) -> torch.Tensor:
    """Batched bond lengths given batches of atom i and j.

    Args:
        atom_i (Tensor): Atom `i` coordinates with shape `(..., 3)`.
        atom_j (Tensor): Atom `j` coordinates with shape `(..., 3)`.
        distance_eps (float, optional): Distance smoothing parameter for
            for computing distances as `sqrt(sum_sq) -> sqrt(sum_sq + eps)`.
            Default: 1E-3.

    Returns:
        L (Tensor): Elementwise bond lengths `||x_i - x_j||` with shape `(...)`.
    """
    # Bond length of i-j
    dX = atom_j - atom_i
    L = torch.sqrt((dX ** 2).sum(dim=-1) + distance_eps)
    return L


def angles(
    atom_i: torch.Tensor,
    atom_j: torch.Tensor,
    atom_k: torch.Tensor,
    distance_eps: float = DISTANCE_EPS,
    degrees: bool = False,
) -> torch.Tensor:
    """Batched bond angles given atoms `i-j-k`.

    Args:
        atom_i (Tensor): Atom `i` coordinates with shape `(..., 3)`.
        atom_j (Tensor): Atom `j` coordinates with shape `(..., 3)`.
        atom_k (Tensor): Atom `k` coordinates with shape `(..., 3)`.
        distance_eps (float, optional): Distance smoothing parameter for
            for computing distances as `sqrt(sum_sq) -> sqrt(sum_sq + eps)`.
            Default: 1E-3.
        degrees (bool, optional): If True, convert to degrees. Default: False.

    Returns:
        A (Tensor): Elementwise bond angles with shape `(...)`.
    """
    # Bond angle of i-j-k
    U_ji = normed_vec(atom_i - atom_j, distance_eps=distance_eps)
    U_jk = normed_vec(atom_k - atom_j, distance_eps=distance_eps)
    inner_prod = torch.einsum("bix,bix->bi", U_ji, U_jk)
    inner_prod = torch.clamp(inner_prod, -1, 1)
    A = torch.acos(inner_prod)
    if degrees:
        A = A * 180.0 / np.pi
    return A


def dihedrals(
    atom_i: torch.Tensor,
    atom_j: torch.Tensor,
    atom_k: torch.Tensor,
    atom_l: torch.Tensor,
    distance_eps: float = DISTANCE_EPS,
    degrees: bool = False,
) -> torch.Tensor:
    """Batched bond dihedrals given atoms `i-j-k-l`.

    Args:
        atom_i (Tensor): Atom `i` coordinates with shape `(..., 3)`.
        atom_j (Tensor): Atom `j` coordinates with shape `(..., 3)`.
        atom_k (Tensor): Atom `k` coordinates with shape `(..., 3)`.
        atom_l (Tensor): Atom `l` coordinates with shape `(..., 3)`.
        distance_eps (float, optional): Distance smoothing parameter for
            for computing distances as `sqrt(sum_sq) -> sqrt(sum_sq + eps)`.
            Default: 1E-3.
        degrees (bool, optional): If True, convert to degrees. Default: False.

    Returns:
        D (Tensor): Elementwise bond dihedrals with shape `(...)`.
    """
    U_ij = normed_vec(atom_j - atom_i, distance_eps=distance_eps)
    U_jk = normed_vec(atom_k - atom_j, distance_eps=distance_eps)
    U_kl = normed_vec(atom_l - atom_k, distance_eps=distance_eps)
    normal_ijk = normed_cross(U_ij, U_jk, distance_eps=distance_eps)
    normal_jkl = normed_cross(U_jk, U_kl, distance_eps=distance_eps)
    # _inner_product = lambda a, b: torch.einsum("bix,bix->bi", a, b)
    _inner_product = lambda a, b: (a * b).sum(-1)
    cos_dihedrals = _inner_product(normal_ijk, normal_jkl)
    angle_sign = _inner_product(U_ij, normal_jkl)
    cos_dihedrals = torch.clamp(cos_dihedrals, -1, 1)
    D = torch.sign(angle_sign) * torch.acos(cos_dihedrals)
    if degrees:
        D = D * 180.0 / np.pi
    return D


def rbf_expansion(
        h: torch.Tensor,
        value_min: float = 0.0,
        value_max: float = 30.0,
        num_rbf: int = 32,
    ):
    rbf_centers = torch.linspace(value_min, value_max, num_rbf)
    std = (rbf_centers[1] - rbf_centers[0]).item()
    shape = list(h.shape)
    shape_ones = [1 for _ in range(len(shape))] + [-1]
    rbf_centers = rbf_centers.view(shape_ones)
    h = torch.exp(-(((h.unsqueeze(-1) - rbf_centers) / std) ** 2))
    h = h.view(shape[:-1] + [-1])
    return h


def positional_encoding(inputs, num_posenc=32, period_range=(1.0, 1000.0)):
    
    num_frequencies = num_posenc // 2
    log_bounds = np.log10(period_range)
    p = torch.logspace(log_bounds[0], log_bounds[1], num_frequencies, base=10.0)
    w = 2 * math.pi / p
    
    batch_dims = list(inputs.shape)[:-1]
    # (..., 1, num_out) * (..., num_in, 1)
    w = w.reshape(len(batch_dims) * [1] + [1, -1])
    h = w * inputs[..., None]
    h = torch.cat([h.cos(), h.sin()], -1).reshape(batch_dims + [-1])
    return h


def internal_vecs(X):
    # Relative displacement vectors along backbone
    # X : num_conf x num_res x num_bb_atoms x 3
    p, c4p, n = X[:, :, 0], X[:, :, 1], X[:, :, 2]
    n, p = n - c4p, p - c4p
    forward = F.pad(c4p[:, 1:] - c4p[:, :-1], [0, 0, 0, 1])
    backward = F.pad(c4p[:, :-1] - c4p[:, 1:], [0, 0, 1, 0])
    return torch.cat([
        normed_vec(p).unsqueeze_(-2), 
        normed_vec(n).unsqueeze_(-2), 
        normed_vec(forward).unsqueeze_(-2), 
        normed_vec(backward).unsqueeze_(-2),
    ], dim=-2)


def normalize(tensor, dim=-1):
    '''
    Normalizes a `torch.Tensor` along dimension `dim` without `nan`s.
    '''
    return torch.nan_to_num(
        torch.div(tensor, torch.linalg.norm(tensor, dim=dim, keepdim=True)))

#=====modified============================================
def get_angle(vi,vj):
    _inner_product = lambda a, b: (a * b).sum(-1)
    cos = _inner_product(vi, vj)
    
    cos = torch.clamp(cos, -1, 1)
    # D = torch.sign(cos) * torch.acos(cos)
    D = torch.acos(cos)
    # D * 180.0 / np.pi
    return D


def get_k_random_entries_and_masks_2(coords_list, basepairs, k):
    """
    Returns k random entries from a list of 3D coordinates, along with
    the corresponding masks (1 = valid, 0 = not valid).
    
    Args:
        coords_list (list): List of np.array entries of 3D coordinates
        k (int): number of random entries to be selected from coords_list
    
    Returns:
        confs_list (np.array): Coordinates array of shape (k, num_residues, num_atoms, 3)
        mask_coords (np.array): Mask of valid coordinates of shape (num_atoms)
        mask_confs (np.array): Mask of valid conformers of shape (k)
    """
    n = len(coords_list)
    coords_list = np.array(coords_list)

    if k > n:
        # If k is greater than the length of the list,
        # return all the entries in the list and pad random entries up to k
        rand_idx = np.random.choice(n, size=k-n, replace=True)
        confs_list = np.concatenate((coords_list, coords_list[rand_idx]), axis=0)
        for idx in rand_idx:
            basepairs.append(basepairs[idx])
        mask_coords = (coords_list == FILL_VALUE).sum(axis=(0,2,3)) < 4
        # if mask_coords[0] == False:
        #     if (coords_list[:,0] == FILL_VALUE).sum(axis=(0,1,2)) < 4:
        #         mask_coords[0] = True
        mask_confs = np.array([1]*k)
    else:
        # If k is less than or equal to the length of the list, 
        # randomly select k entries
        rand_idx = np.random.choice(n, size=k, replace=False)
        confs_list =  coords_list[rand_idx]
        basepairs = [basepairs[i] for i in rand_idx]
        mask_coords = (confs_list == FILL_VALUE).sum(axis=(0,2,3)) == 0
        if mask_coords[0] == False:
            if (coords_list[:,0] == FILL_VALUE).sum(axis=(0,1,2)) < 4:
                mask_coords[0] = True
        mask_confs = np.array([1]*k)

    return confs_list, basepairs, mask_coords, mask_confs

def unpaired_cluster_dbscan(unpaired_idx, coord, primary_dist = 500, dbscan_eps = 10):
    edges_source = []
    edges_dest = []
    coord_mean = coord.mean(1)
    unpaired_coords_mean = coord_mean[unpaired_idx]
    if unpaired_coords_mean.shape[0] > 0:
        db = DBSCAN(eps=dbscan_eps, min_samples=5).fit(unpaired_coords_mean)
    else:
        dummy = 2
        return torch.empty((2,0), dtype=torch.int64) 
    core_samples_mask = np.zeros_like(db.labels_, dtype=bool)
    core_samples_mask[db.core_sample_indices_] = True
    unique_labels = set(db.labels_)
    for cur_label_idx in unique_labels:
        if cur_label_idx != -1:
            class_member_mask = db.labels_ == cur_label_idx
            masks = class_member_mask & core_samples_mask
            idx_mem = np.array(unpaired_idx)[masks]
            # idx = unpaired_idx[class_member_mask]
            for i in range(len(idx_mem)-1):
                for j in range(i+1, len(idx_mem)):
                    if (abs(idx_mem[i] - idx_mem[j]) > primary_dist) & (2 < dist_2(coord_mean[idx_mem[i]],coord_mean[idx_mem[j]]).item() < 2*dbscan_eps):
                        edges_source.append(idx_mem[i])
                        edges_dest.append(idx_mem[j])
            
    return torch.stack((torch.tensor(edges_source),torch.tensor(edges_dest)), dim = 0).to(torch.int64)

def offset_basepairs(basepairs,masks):
    return_bp = []
    pair_map = np.arange(start=1, stop=len(masks)+1, step=1)
    pair_masked_keys = pair_map[masks]
    pair_masked_vals = np.arange(start=1, stop=len(pair_masked_keys)+1, step=1)
    dict = {}
    for key, val in zip(pair_masked_keys, pair_masked_vals):
        dict[key] = val
    for k in range(len(basepairs)):
        cur_basepairs_1 = [dict.get(basepairs[k][i][0],0) for i in range(len(basepairs[k]))]
        cur_basepairs_2 = [dict.get(basepairs[k][i][1],0) for i in range(len(basepairs[k]))]
        cur_basepairs = []
        for idx in range(len(basepairs[k])):
            if (cur_basepairs_1[idx] != 0) and ((cur_basepairs_2[idx] != 0)):
                cur_basepairs.append([cur_basepairs_1[idx],cur_basepairs_2[idx]])
        return_bp.append(cur_basepairs)
    return return_bp
                    

            
                 
                        
            
