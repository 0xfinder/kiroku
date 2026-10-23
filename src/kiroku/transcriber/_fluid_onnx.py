# pyright: basic
"""Adapted Fluid Speech Mini ONNX runner, CC BY 4.0.

Upstream: FluidVoice-Labs/fluid-speech-mini-onnx, revision
fc592620325027419883cf272e70588d8a788c0a, transcribe.py.
Credit: Fluid Speech Mini by FluidVoice, based on NVIDIA Parakeet and Moondream Parakeet Ultra.
Local changes: PCM-only entrypoint, sentence segments on the TDT emission clock,
no model download or CLI, and no soundfile dependency. See fluid_LICENSE and fluid_NOTICE.
"""

import importlib
import json
import os
import time
from typing import Any, cast

# Optional native dependencies stay inside the worker, behind its typed JSON boundary.
np: Any = importlib.import_module("numpy")
ort: Any = importlib.import_module("onnxruntime")


def release_memory():
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


LOG_ZERO_GUARD = 2.0**-24
NORM_EPS = 1e-5


def log_mel(wav, cfg, filters):
    """Parakeet features: ([1, T, mels] float32, [1, T] int64)."""
    n_fft, hop, win = cfg["n_fft"], cfg["hop_length"], cfg["win_length"]
    x = wav.astype(np.float32)
    if cfg.get("preemphasis"):
        x = np.concatenate([x[:1], x[1:] - np.float32(cfg["preemphasis"]) * x[:-1]])
    window = np.zeros(n_fft, dtype=np.float64)
    left = (n_fft - win) // 2
    n = np.arange(win, dtype=np.float64)
    window[left : left + win] = (0.5 - 0.5 * np.cos(2.0 * np.pi * n / (win - 1))).astype(np.float32)
    padded = np.pad(x, n_fft // 2)
    frames = 1 + len(x) // hop
    mel = np.empty((frames, filters.shape[0]), dtype=np.float32)
    offsets = np.arange(n_fft)[None, :]
    for start in range(
        0, frames, 256
    ):  # in blocks: the STFT of a whole clip at once costs tens of MB
        idx = offsets + hop * np.arange(start, min(start + 256, frames))[:, None]
        spec = np.fft.rfft(padded[idx].astype(np.float64) * window, axis=1)
        power = (spec.real**2 + spec.imag**2).astype(np.float32)
        mel[start : start + len(idx)] = np.log(power @ filters.T + np.float32(LOG_ZERO_GUARD))
    valid = min(len(x) // hop, frames)
    mean = mel[:valid].sum(axis=0, dtype=np.float32) / np.float32(valid)
    var = ((mel[:valid] - mean) ** 2).sum(axis=0, dtype=np.float32) / np.float32(max(valid - 1, 1))
    out = (mel - mean) / (np.sqrt(var) + np.float32(NORM_EPS))
    out[valid:] = 0.0
    mask = (np.arange(frames) < valid).astype(np.int64)
    return out[None].astype(np.float32), mask[None]


class Tokenizer:
    def __init__(self, path, cfg):
        with open(path, encoding="utf-8") as file:
            t = json.load(file)
        self.pieces = {i: s for s, i in t["model"]["vocab"].items()}
        skip = {cfg["blank_token_id"], cfg["pad_token_id"]}
        for a in t.get("added_tokens", []):
            self.pieces[a["id"]] = a["content"]
            if a.get("special"):
                skip.add(a["id"])
        self.skip = skip
        dec = t.get("decoder") or {}
        self.space = dec.get("replacement", "▁")

    def decode(self, ids):
        text = "".join(self.pieces.get(i, "") for i in ids if i not in self.skip).replace(
            self.space, " "
        )
        return text[1:] if text.startswith(" ") else text


class Model:
    def __init__(self, model_dir, threads=0, provider="cpu", spin=True):
        with open(os.path.join(model_dir, "config.json")) as file:
            self.cfg = json.load(file)
        self.filters = np.load(os.path.join(model_dir, "mel_filters.npy")).astype(np.float32)
        self.tokenizer = Tokenizer(os.path.join(model_dir, "tokenizer.json"), self.cfg)
        providers = ["CPUExecutionProvider"]
        if provider == "cuda":
            if hasattr(ort, "preload_dlls"):
                ort.preload_dlls()
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        so = ort.SessionOptions()
        # Free convolution activations between calls instead of retaining a CPU arena.
        so.enable_cpu_mem_arena = False
        so.enable_mem_pattern = False
        if threads:
            so.intra_op_num_threads = threads
        if not spin:
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        small = ort.SessionOptions()
        small.intra_op_num_threads = 1
        small.enable_cpu_mem_arena = False
        small.enable_mem_pattern = False
        self.encoder = ort.InferenceSession(
            os.path.join(model_dir, "encoder.onnx"), so, providers=providers
        )
        front = os.path.join(model_dir, "frontend.onnx")
        self.frontend = (
            ort.InferenceSession(front, so, providers=providers) if os.path.exists(front) else None
        )
        self.decoder = ort.InferenceSession(
            os.path.join(model_dir, "decoder.onnx"), small, providers=["CPUExecutionProvider"]
        )
        self.joint = ort.InferenceSession(
            os.path.join(model_dir, "joint.onnx"), small, providers=["CPUExecutionProvider"]
        )
        self.timing = {"mel": 0.0, "encoder": 0.0, "decode": 0.0}
        release_memory()

    def subsampled(self, frames):
        cfg = self.cfg
        pad = (cfg["subsampling_kernel"] - 1) // 2 * 2 - cfg["subsampling_kernel"]
        for _ in range(cfg["subsampling_layers"]):
            frames = int(np.floor((frames + pad) / cfg["subsampling_stride"] + 1.0))
        return frames

    def encode(self, feats, mask):
        """Encode features; the chunked frontend keeps one output frame of context per side."""
        if self.frontend is None:
            enc, enc_len = cast(
                tuple[Any, Any],
                self.encoder.run(None, {"input_features": feats, "attention_mask": mask}),
            )
            return enc[0], int(enc_len[0])
        cfg = self.cfg
        factor = cfg["subsampling_stride"] ** cfg["subsampling_layers"]
        frames, total, step = (
            feats.shape[1],
            self.subsampled(feats.shape[1]),
            cfg["frontend_chunk_frames"],
        )
        parts = []
        for a in range(0, total, step):
            b = min(a + step, total)
            s = factor * (a - 1) if a else 0
            e = min(frames, factor * (b + 1))
            y = cast(
                list[Any],
                self.frontend.run(
                    None, {"input_features": feats[:, s:e], "attention_mask": mask[:, s:e]}
                ),
            )[0]
            skip = 1 if a else 0
            parts.append(y[:, skip : skip + (b - a)])
        hidden = np.concatenate(parts, axis=1) if len(parts) > 1 else parts[0]
        valid = self.subsampled(int(mask.sum()))
        enc = cast(
            list[Any],
            self.encoder.run(
                None, {"hidden_states": hidden, "length": np.array([valid], dtype=np.int64)}
            ),
        )[0]
        return enc[0], valid

    def greedy_tdt(self, enc, valid_len):
        """Greedy Parakeet token-and-duration decoding for one clip."""
        cfg = self.cfg
        vocab, blank, eos = cfg["vocab_size"], cfg["blank_token_id"], cfg.get("eos_token_id")
        durations = cfg["durations"]
        state = np.zeros((cfg["decoder_layers"], 1, cfg["decoder_hidden_size"]), dtype=np.float32)
        dec_out, h, c = self.decoder.run(
            None, {"token": np.array([[blank]], dtype=np.int64), "h_in": state, "c_in": state}
        )
        frames = enc.shape[0]
        budget = cfg["max_symbols_per_step"] * frames - 1
        tokens, times, t = [], [], 0
        while True:
            logits = cast(
                list[Any],
                self.joint.run(
                    None, {"encoder_frame": enc[min(t, frames - 1)][None], "decoder_out": dec_out}
                ),
            )[0][0]
            token = int(logits[:vocab].argmax())
            step = durations[int(logits[vocab:].argmax())]
            if token == blank and step == 0:
                step = 1
            tokens.append(token)
            times.append(t)
            t += step
            if t >= valid_len or len(tokens) >= budget or token == eos:
                return tokens, times
            if token != blank:
                dec_out, h, c = self.decoder.run(
                    None, {"token": np.array([[token]], dtype=np.int64), "h_in": h, "c_in": c}
                )

    def transcribe_pass(self, wav) -> list[dict[str, int | str]]:
        t0 = time.perf_counter()
        feats, mask = log_mel(wav, self.cfg, self.filters)
        t1 = time.perf_counter()
        enc, enc_len = self.encode(feats, mask)
        del feats
        release_memory()
        t2 = time.perf_counter()
        tokens, times = self.greedy_tdt(enc, enc_len)
        t3 = time.perf_counter()
        self.timing["mel"] += t1 - t0
        self.timing["encoder"] += t2 - t1
        self.timing["decode"] += t3 - t2
        return self.segments(tokens, times, len(wav))

    def segments(self, tokens, times, samples) -> list[dict[str, int | str]]:
        # TDT emission positions are on the 8x-subsampled 10 ms feature clock.
        frame_ms = (
            self.cfg["hop_length"]
            * (self.cfg["subsampling_stride"] ** self.cfg["subsampling_layers"])
            * 1000
            / self.cfg["sampling_rate"]
        )
        end_ms = samples * 1000 // self.cfg["sampling_rate"]
        out: list[dict[str, int | str]] = []
        current, start, last = [], None, 0
        for token, frame in zip(tokens, times, strict=True):
            if token in self.tokenizer.skip:
                continue
            if start is None:
                start = round(frame * frame_ms)
            current.append(token)
            last = round((frame + 1) * frame_ms)
            text = self.tokenizer.decode(current).strip()
            if text.endswith((".", "!", "?")):
                out.append({"start_ms": start or 0, "end_ms": min(end_ms, last), "text": text})
                current, start = [], None
        text = self.tokenizer.decode(current).strip()
        if text:
            out.append({"start_ms": start or 0, "end_ms": min(end_ms, last), "text": text})
        return out

    def transcribe(self, pcm: bytes) -> list[dict[str, int | str]]:
        wav = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if len(wav) > self.cfg["max_single_pass_seconds"] * self.cfg["sampling_rate"]:
            raise ValueError("Fluid expects audio chunks of at most 40 seconds")
        return self.transcribe_pass(wav)
