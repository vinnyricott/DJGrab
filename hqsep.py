"""High-quality vocal / instrumental split for DJ Grab intros.

Uses the BS-RoFormer "viperx 1297" model (vocals SDR 12.97, instrumental ~17 on MUSDB) - far cleaner
than Demucs' vocal estimate, so the instrumental keeps the song's full top end and punch.
Only the stretch of the song that becomes the intro is run through it, so it adds seconds, not minutes.
"""
import os
import urllib.request
from pathlib import Path

import numpy as np

SR = 44100
MODEL_FILE = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
MODEL_URL = ("https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/" + MODEL_FILE)
MODEL_BYTES = 639331213
CHUNK = 441 * 800            # 8 s, what the model was trained on
OVERLAP = 2               # measured: 2x faster, result within -45 dB of 4 (inaudible)

CONFIG = dict(
    dim=512, depth=12, stereo=True, num_stems=1, time_transformer_depth=1, freq_transformer_depth=1,
    freqs_per_bands=tuple([2] * 24 + [4] * 12 + [12] * 8 + [24] * 8 + [48] * 8 + [128, 129]),
    dim_head=64, heads=8, attn_dropout=0.0, ff_dropout=0.0, flash_attn=True, dim_freqs_in=1025,
    stft_n_fft=2048, stft_hop_length=441, stft_win_length=2048, stft_normalized=False,
    mask_estimator_depth=2, multi_stft_resolution_loss_weight=1.0,
    multi_stft_resolutions_window_sizes=(4096, 2048, 1024, 512, 256), multi_stft_hop_size=147,
    multi_stft_normalized=False,
)

_MODEL = None


def model_path(model_dir):
    return Path(model_dir) / MODEL_FILE


def download(model_dir, progress=None):
    """Fetch the model once (about 640 MB). Resumable; written to a temp name, then renamed."""
    p = model_path(model_dir)
    if p.exists() and p.stat().st_size == MODEL_BYTES:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    have = tmp.stat().st_size if tmp.exists() else 0
    req = urllib.request.Request(MODEL_URL, headers={"Range": f"bytes={have}-"} if have else {})
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and r.status != 206:
            have = 0
        with open(tmp, "ab" if have else "wb") as f:
            done = have
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if progress:
                    progress(done / MODEL_BYTES)
    if tmp.stat().st_size != MODEL_BYTES:
        raise RuntimeError("model download incomplete")
    os.replace(tmp, p)
    return p


def load(model_dir):
    global _MODEL
    if _MODEL is None:
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        import torch
        from roformer.bs_roformer import BSRoformer
        m = BSRoformer(**CONFIG)
        sd = torch.load(str(model_path(model_dir)), map_location="cpu", weights_only=True)
        m.load_state_dict(sd)
        dev = ("mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu")
        m.to(dev).eval()
        _MODEL = (m, dev)
    return _MODEL


def vocals(audio, model_dir):
    """audio: float32 (n, 2) at 44.1 kHz -> vocals estimate (n, 2). Instrumental = audio - vocals."""
    import torch
    m, dev = load(model_dir)
    a = audio if audio.shape[1] == 2 else np.repeat(audio[:, :1], 2, axis=1)
    n = len(a)
    mix = torch.from_numpy(np.ascontiguousarray(a.T)).float()
    pad = 0
    if n < CHUNK:                                        # short clip: reflect-pad up to one chunk
        pad = CHUNK - n
        mix = torch.nn.functional.pad(mix[None], (0, pad), mode="constant")[0]
    total = mix.shape[1]
    step = CHUNK // OVERLAP
    starts = list(range(0, max(total - CHUNK, 0) + 1, step))
    if starts[-1] + CHUNK < total:
        starts.append(total - CHUNK)
    win = torch.from_numpy(np.hamming(CHUNK).astype("float32"))
    out = torch.zeros(2, total)
    cnt = torch.zeros(total)
    with torch.no_grad():
        for s in starts:
            part = mix[:, s:s + CHUNK].to(dev)
            try:
                y = m(part[None])[0]
            except Exception:
                if dev == "cpu":
                    raise
                m.to("cpu")
                globals()["_MODEL"] = (m, "cpu")
                dev = "cpu"
                y = m(part.cpu()[None])[0]
            y = y.float().cpu()
            if y.dim() == 3:
                y = y[0]
            out[:, s:s + CHUNK] += y * win
            cnt[s:s + CHUNK] += win
    out = out / cnt.clamp(min=1e-8)
    v = out.numpy().T[:n]
    if audio.shape[1] == 1:
        v = v[:, :1]
    return np.ascontiguousarray(v.astype("float32"))
