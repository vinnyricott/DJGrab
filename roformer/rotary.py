"""Rotary position embedding, applied without touching rotary-embedding-torch's cache internals
(those changed between 0.6 and 0.9, which silently zeroed the model's output). Angles in float32."""
import torch


def _rotate_half(x):
    x = x.unflatten(-1, (-1, 2))
    x1, x2 = x.unbind(dim=-1)
    return torch.stack((-x2, x1), dim=-1).flatten(-2)


def rotate_queries_or_keys(rotary_embed, tensor):
    input_dtype = tensor.dtype
    seq_dim = rotary_embed.default_seq_dim
    seq_len = tensor.shape[seq_dim]
    interp = float(getattr(rotary_embed, "interpolate_factor", 1.0))
    pos = torch.arange(seq_len, device=tensor.device, dtype=torch.float32) / interp
    freqs = torch.einsum("n, f -> n f", pos, rotary_embed.freqs.to(device=tensor.device, dtype=torch.float32))
    freqs = torch.repeat_interleave(freqs, 2, dim=-1)              # (seq, rot_dim)
    if seq_dim == -3:
        freqs = freqs[:, None, :]
    t = tensor.float()
    rot = freqs.shape[-1]
    mid = t[..., :rot]
    mid = mid * freqs.cos() + _rotate_half(mid) * freqs.sin()
    out = torch.cat((mid, t[..., rot:]), dim=-1) if rot < t.shape[-1] else mid
    return out.to(dtype=input_dtype)
