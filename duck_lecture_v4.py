#!/usr/bin/env python3
"""
duck_lecture.py v2 — Ders kaydinda hoca (yaka) ve seyirci (shotgun) mikrofonlarini
akilli sekilde karistirir.

Yenilikler:
  * Gorece seviye tespiti (shotgun - yaka): hocanin kendi sesi duck'i tetiklemez
  * GCC gecikme hizalama: iki mikrofon arasindaki gecikmeyi duzeltir (tarama etkisi azalir)
  * Cift yonlu ducking: hoca konusurken shotgun da kisilir (oda gurultusu/yanki azalir)
  * Konusma bandinda (150-4000 Hz) tespit
  * Look-ahead: seyirci cumlesinin ilk hecesi kacmaz
  * dB domaininde yumusatma
  * Dusuk bellek kullanimi (cumsum RMS, chunk bazli gain uygulama)
  * Son adimda loudnorm + limiter

Gereksinimler: ffmpeg, numpy, scipy, soundfile
  pip install numpy scipy soundfile

Kullanim:
  python duck_lecture_v4.py analyze ders.mp4
  python duck_lecture_v4.py run ders.mp4 cikti.mp4
"""

import argparse
import copy
import os
import subprocess
import tempfile

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt, resample_poly, fftconvolve

TEACHER_MAP = "0:a:0"   # 1. ses izi -> hoca (yaka)
AUDIENCE_MAP = "0:a:1"  # 2. ses izi -> shotgun / seyirci
SR = 48000
DET_DECIM = 3           # tespit icin 48k -> 16k
SR_DET = SR // DET_DECIM


# ----------------------------------------------------------------- ffmpeg ---
def run_ffmpeg(args):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args, check=True)


def extract_wav(input_mp4, stream_map, out_wav, af):
    run_ffmpeg([
        "-i", input_mp4, "-map", stream_map, "-vn",
        "-ac", "1", "-ar", str(SR), "-af", af,
        "-c:a", "pcm_f32le", out_wav,
    ])


# ----------------------------------------------------------- olcum / tespit ---
def det_signal(x):
    """Tespit icin: 16 kHz'e indir + konusma bandi filtresi."""
    y = resample_poly(x, 1, DET_DECIM).astype(np.float32)
    sos = butter(4, [150, 4000], btype="bandpass", fs=SR_DET, output="sos")
    return sosfilt(sos, y).astype(np.float32)


def level_db(x, sr, win_ms=100, hop_ms=20):
    """Dusuk bellekli kayan RMS (dB). Doner: db, frame_merkez_zamanlari(sn), frame_suresi(sn)."""
    hop = int(sr * hop_ms / 1000)
    k = win_ms // hop_ms
    nb = len(x) // hop
    if nb < k:
        raise ValueError("Kayit cok kisa.")
    xb = x[: nb * hop].reshape(nb, hop)
    e = np.einsum("ij,ij->i", xb, xb, dtype=np.float64)
    c = np.concatenate([[0.0], np.cumsum(e)])
    nf = nb - k + 1
    ms = (c[k:k + nf] - c[:nf]) / (k * hop)
    db = 10 * np.log10(ms + 1e-12)
    t = (np.arange(nf) * hop + k * hop / 2) / sr
    return db, t, hop / sr


def shift_advance(x, k):
    """k>0: sinyali k ornek one al (erkene cek); k<0: geciktir. Uzunluk korunur."""
    if k == 0:
        return x
    if k > 0:
        return np.concatenate([x[k:], np.zeros(k, dtype=x.dtype)])
    return np.concatenate([np.zeros(-k, dtype=x.dtype), x[:k]])


def estimate_delay(t_det, a_det, t_db, frame_s, win_s=20.0, max_ms=100.0):
    """Hocanin en yuksek sesli 20 sn'lik bolumunden shotgun gecikmesini olc.
    Doner: (gecikme_sn, guven). Pozitif -> shotgun gecikmeli."""
    w = int(win_s / frame_s)
    if len(t_db) <= w:
        s0 = 0
        seg_len = len(t_det)
    else:
        c = np.concatenate([[0.0], np.cumsum(t_db)])
        means = (c[w:] - c[:-w])
        s0_frame = int(np.argmax(means))
        s0 = int(s0_frame * frame_s * SR_DET)
        seg_len = int(win_s * SR_DET)
    t = t_det[s0:s0 + seg_len].astype(np.float64)
    a = a_det[s0:s0 + seg_len].astype(np.float64)
    m = min(len(t), len(a))
    t, a = t[:m], a[:m]
    corr = fftconvolve(a, t[::-1], mode="full")
    mid = m - 1
    ml = int(SR_DET * max_ms / 1000)
    seg = np.abs(corr[mid - ml: mid + ml + 1])
    lag = int(np.argmax(seg)) - ml
    conf = float(seg.max() / (np.median(seg) + 1e-12))
    return lag / SR_DET, conf


def hysteresis(on_cond, off_cond):
    state = False
    out = np.zeros(len(on_cond), dtype=bool)
    for i in range(len(on_cond)):
        if not state and on_cond[i]:
            state = True
        elif state and off_cond[i]:
            state = False
        out[i] = state
    return out


def _runs(m):
    d = np.diff(np.concatenate([[0], m.astype(np.int8), [0]]))
    return np.flatnonzero(d == 1), np.flatnonzero(d == -1)


def clean_mask(m, min_on, min_off):
    """Kisa bosluklari doldur (min_off), sonra kisa patlamalari sil (min_on)."""
    m = m.copy()
    n = len(m)
    s, e = _runs(~m)
    for a, b in zip(s, e):
        if a > 0 and b < n and (b - a) < min_off:
            m[a:b] = True
    s, e = _runs(m)
    for a, b in zip(s, e):
        if (b - a) < min_on:
            m[a:b] = False
    return m


def detect(t_db, a_db, frame_s, args):
    n = min(len(t_db), len(a_db))
    t_db, a_db = t_db[:n], a_db[:n]
    score = a_db - t_db                      # yuksekse seyirci baskin
    floor_a = np.percentile(a_db, 10) + args.floor_margin
    floor_t = np.percentile(t_db, 10) + args.floor_margin

    aud_on = (score >= args.on) & (a_db >= floor_a)
    aud_off = (score <= args.off) | (a_db <= floor_a - 3)
    aud_active = hysteresis(aud_on, aud_off)
    aud_active = clean_mask(aud_active, max(1, int(args.min_on / frame_s)),
                            max(1, int(args.min_off / frame_s)))

    teach_active = (t_db >= floor_t) & ~aud_active
    teach_active = clean_mask(teach_active, max(1, int(0.2 / frame_s)),
                              max(1, int(args.teach_hold / frame_s)))
    return dict(score=score, floor_a=floor_a, floor_t=floor_t,
                aud=aud_active, teach=teach_active, n=n)


def apply_lookahead(x, k):
    if k <= 0:
        return x
    return np.concatenate([x[k:], np.full(k, x[-1])])


def smooth_db(target, frame_s, fall_s, rise_s):
    """dB domaininde asimetrik ustel yumusatma."""
    a_f = 1 - np.exp(-frame_s / max(fall_s, 1e-6))
    a_r = 1 - np.exp(-frame_s / max(rise_s, 1e-6))
    out = np.empty_like(target, dtype=np.float64)
    out[0] = target[0]
    for i in range(1, len(target)):
        prev = out[i - 1]
        a = a_f if target[i] < prev else a_r
        out[i] = prev + a * (target[i] - prev)
    return out


def apply_gain(x, gain_db, frame_t, chunk_s=10):
    out = np.empty_like(x)
    chunk = SR * chunk_s
    for s in range(0, len(x), chunk):
        e = min(len(x), s + chunk)
        t = np.arange(s, e) / SR
        g = np.interp(t, frame_t, gain_db)
        out[s:e] = x[s:e] * (10 ** (g / 20)).astype(np.float32)
    return out


# ------------------------------------------------------------------ yukleme ---
def load_tracks(args, tmpdir):
    tw = os.path.join(tmpdir, "teacher.wav")
    aw = os.path.join(tmpdir, "audience.wav")
    print("Ses trackleri cikariliyor...")
    extract_wav(args.input_mp4, TEACHER_MAP, tw, "highpass=f=80")
    a_af = "highpass=f=80"
    if getattr(args, "denoise", False):
        a_af += ",afftdn=nr=12:nf=-35"
    extract_wav(args.input_mp4, AUDIENCE_MAP, aw, a_af)
    teacher, sr = sf.read(tw, dtype="float32")
    audience, sr2 = sf.read(aw, dtype="float32")
    assert sr == sr2 == SR, "Sample rate uyusmuyor!"
    n = min(len(teacher), len(audience))
    return teacher[:n], audience[:n]


def align_and_measure(teacher, audience, args):
    t_det = det_signal(teacher)
    a_det = det_signal(audience)
    t_db, _, frame_s = level_db(t_det, SR_DET)

    if args.delay_ms is not None:
        delay_s = args.delay_ms / 1000.0
        print(f"Gecikme (elle): {args.delay_ms:.2f} ms")
    elif args.no_align:
        delay_s = 0.0
    else:
        delay_s, conf = estimate_delay(t_det, a_det, t_db, frame_s)
        print(f"Olculen gecikme: {delay_s * 1000:+.2f} ms (guven {conf:.1f})")
        if conf < 3.0:
            print("  Guven dusuk, hizalama atlandi (--delay-ms ile elle verebilirsin).")
            delay_s = 0.0

    k = int(round(delay_s * SR))
    if k != 0:
        audience = shift_advance(audience, k)
        a_det = shift_advance(a_det, int(round(delay_s * SR_DET)))

    a_db, t_frames, frame_s = level_db(a_det, SR_DET)
    t_db, _, _ = level_db(t_det, SR_DET)
    return teacher, audience, t_db, a_db, t_frames, frame_s


# -------------------------------------------------------------------- main ---
def build_parser():
    p = argparse.ArgumentParser(description="Ders kaydi akilli mikrofon karistirici")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("input_mp4")
    common.add_argument("--on", type=float, default=-3.0,
                        help="Seyirci tespiti acilis esigi: (shotgun_dB - yaka_dB) (default -3)")
    common.add_argument("--off", type=float, default=-8.0,
                        help="Seyirci tespiti kapanis esigi (default -8)")
    common.add_argument("--floor-margin", type=float, default=10.0,
                        help="Gurultu tabaninin (10. persentil) kac dB ustu 'aktif' sayilir (default 10)")
    common.add_argument("--min-on", type=float, default=0.3)
    common.add_argument("--min-off", type=float, default=0.6)
    common.add_argument("--teach-hold", type=float, default=1.5,
                        help="Hoca durduktan sonra shotgun kac sn kapali kalsin (yanki icin, default 1.5)")
    common.add_argument("--no-align", action="store_true", help="Gecikme hizalamayi kapat")
    common.add_argument("--delay-ms", type=float, default=None,
                        help="Gecikmeyi elle ver (ms, + = shotgun gecikmeli)")
    common.add_argument("--denoise", action="store_true", help="Shotgun'a afftdn gurultu azaltma")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("analyze", parents=[common], help="Seviye istatistiklerini goster")

    pr = sub.add_parser("run", parents=[common], help="Isle ve cikti uret")
    pr.add_argument("output_mp4")
    pr.add_argument("--duck-db", type=float, default=-10.0, help="Seyirci konusurken hoca kisma (dB)")
    pr.add_argument("--aud-duck-db", type=float, default=-40.0,
                    help="Hoca konusurken shotgun kisma (dB)")
    pr.add_argument("--aud-attack", type=float, default=0.15, help="Shotgun kapanma suresi (sn)")
    pr.add_argument("--aud-release", type=float, default=0.5, help="Shotgun geri acilma suresi (sn)")
    pr.add_argument("--aud-boost-db", type=float, default=3.0,
                    help="Seyirci konusurken shotgun yukseltme (dB)")
    pr.add_argument("--attack", type=float, default=0.12, help="Hoca duck inis suresi (sn)")
    pr.add_argument("--release", type=float, default=0.5, help="Hoca duck donus suresi (sn)")
    pr.add_argument("--lookahead", type=float, default=0.15, help="Look-ahead (sn)")
    pr.add_argument("--no-loudnorm", action="store_true")
    pr.add_argument("--lufs", type=float, default=-16.0)
    return p


def main():
    args = build_parser().parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        teacher, audience = load_tracks(args, tmpdir)
        teacher, audience, t_db, a_db, t_frames, frame_s = align_and_measure(teacher, audience, args)
        d = detect(t_db, a_db, frame_s, args)
        n = d["n"]
        t_frames = t_frames[:n]

        if args.cmd == "analyze":
            print(f"\nToplam sure: {len(teacher) / SR:.1f} sn")
            for name, arr in [("Yaka (hoca) dB", t_db), ("Shotgun dB", a_db), ("Skor (shotgun-yaka)", d["score"])]:
                ps = {p: np.percentile(arr, p) for p in [5, 25, 50, 75, 90, 95, 99]}
                print(f"\n{name}:")
                print("  " + "  ".join(f"p{p}:{v:.1f}" for p, v in ps.items()))
            print(f"\nShotgun taban esigi: {d['floor_a']:.1f} dB | Yaka taban esigi: {d['floor_t']:.1f} dB")
            print(f"Seyirci aktif orani: %{100 * d['aud'].mean():.1f}")
            print(f"Hoca aktif orani:    %{100 * d['teach'].mean():.1f}")
            print("\nTarama tablosu (seyirci aktif % / hoca aktif %), off = on - 5:")
            margins = [30, 40, 50]
            print("            " + "".join(f"   margin={m:<3}      " for m in margins))
            for on in [4, 8, 12, 16, 20, 25, 30]:
                row = f"on={on:>3}      "
                for m in margins:
                    a = copy.copy(args)
                    a.on, a.off, a.floor_margin = on, on - 5, m
                    r = detect(t_db, a_db, frame_s, a)
                    row += f"  %{100 * r['aud'].mean():5.1f} / %{100 * r['teach'].mean():5.1f}   "
                print(row)
            print("\nIpucu: seyirci oranini %5-20 araligina getiren satiri sec.")
            return

        print(f"Seyirci aktif: %{100 * d['aud'].mean():.1f} | Hoca aktif: %{100 * d['teach'].mean():.1f}")

        # hedef kazanclar (dB)
        t_target = np.where(d["aud"], args.duck_db, 0.0)
        a_target = np.where(d["aud"], args.aud_boost_db,
                            np.where(d["teach"], args.aud_duck_db, 0.0))

        k = int(round(args.lookahead / frame_s))
        t_target = apply_lookahead(t_target, k)
        a_target = apply_lookahead(a_target, k)

        t_gain = smooth_db(t_target, frame_s, fall_s=args.attack, rise_s=args.release)
        a_gain = smooth_db(a_target, frame_s, fall_s=args.aud_attack, rise_s=args.aud_release)

        teacher_out = apply_gain(teacher, t_gain, t_frames)
        audience_out = apply_gain(audience, a_gain, t_frames)
        mix = teacher_out + audience_out
        del teacher_out, audience_out

        mix_wav = os.path.join(tmpdir, "mix.wav")
        sf.write(mix_wav, mix, SR, subtype="FLOAT")

        print("Video ile birlestiriyorum...")
        af = "highpass=f=70"
        if not args.no_loudnorm:
            af += f",loudnorm=I={args.lufs}:TP=-1.5:LRA=11"
        af += ",alimiter=limit=0.95"
        run_ffmpeg([
            "-i", args.input_mp4, "-i", mix_wav,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",
            "-af", af, "-ar", "48000", "-ac", "2",
            "-c:a", "aac", "-b:a", "192k",
            args.output_mp4,
        ])
        print(f"Bitti: {args.output_mp4}")


if __name__ == "__main__":
    main()