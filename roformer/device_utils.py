"""Minimal stand-ins for audio-separator's device helpers (MIT). On Apple GPUs the
spectral (complex) steps run on the CPU, which always works."""
from contextlib import nullcontext
import torch


def autocast_disabled(device):
    try:
        with torch.autocast(device_type=device.type, enabled=False):
            pass
        return torch.autocast(device_type=device.type, enabled=False)
    except Exception:
        return nullcontext()


def should_fallback_to_cpu_for_complex_ops(device):
    return device.type == "mps"
