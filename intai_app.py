#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
INTAI APP — v0.6 HUB EDITION (dashboard + scan terhubung)
====================================
/            -> dashboard (sidebar, KPI, riwayat scan)  [dashboard.html di disk]
/meta        -> halaman scan (kata kunci / intip kompetitor) + deep-link ?q=&mode=
/api/*       -> ringkasan, auth (register/login/logout/me), versi  [in-memory]

Riwayat scan terisi otomatis tiap /scan — dashboard menampilkannya
(klik riwayat -> /meta?q=... -> scan ulang). Akun disimpan di memori
(reset saat server mati; ini alat riset lokal, bukan layanan publik).

Jalankan : python3 intai_app.py   -> http://localhost:8765
Windows  : double-click app.bat
"""

import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote

import requests
import intai_mvp as mesin

PORT = int(os.environ.get("PORT") or 8765)   # env PORT utk deploy (HF Spaces/Render set otomatis)
VERSI = "1.4.7"
FOTO_MAKS = 12
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BERKAS_DATA = os.path.join(BASE_DIR, "intai_data.json")
lock = threading.RLock()   # reentrant: jalankan_scan dipanggil di bawah lock, catat_scan ikut lock
img_cache = {}
url_cache = {}
AKUN_DB = {}    # email -> {"nama": str, "hash": sha256}
TOKENS = {}     # v1.4: tidak dipakai lagi (sesi = token bertanda tangan, lihat buat_sesi)
RIWAYAT = []    # [{kata, mode, waktu, jumlah, baru, hilang}]
LAST_SEEN = {}  # (kata, mode) -> {archive_id: {"h": halaman, "hr": harga_min}} utk delta & "sejak scan terakhir"


def ke_indeks(ids):
    """Normalisasi isi kolom ids: list lama ['123', ...] atau dict baru {'123': {h, hr}}."""
    if isinstance(ids, dict):
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in ids.items()}
    return {str(i): {} for i in (ids or [])}

# Supabase (opsional via env var: SUPABASE_URL + SUPABASE_KEY).
# Terisi  -> Supabase jadi penyimpanan utama (riwayat & akun awet walau deploy ulang),
#           intai_data.json tetap ditulis sebagai cadangan lokal.
# Kosong  -> aplikasi jalan normal dengan intai_data.json saja.
SUPA_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
if SUPA_URL.endswith("/rest/v1"):
    SUPA_URL = SUPA_URL[: -len("/rest/v1")]  # toleran: user kadang ikut menyalin ekor /rest/v1
SUPA_KEY = os.environ.get("SUPABASE_KEY") or ""
SOSIAL = ("instagram.com", "facebook.com", "fb.com", "wa.me", "api.whatsapp.com",
          "twitter.com", "x.com", "threads.net", "youtube.com", "youtu.be", "line.me")
MARKETPLACE = ("shopee.co.id", "shope.ee", "tokopedia.com", "lazada.co.id",
               "bukalapak.com", "blibli.com", "tiktok.com/shop", "orami.co.id")
KATA_PROMO = ("promo", "diskon", "sale", "gratis ongkir", "free ongkir",
              "cashback", "bonus", "harga coret", "cod")
BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
PAT_VERIFY = re.compile(r"fetch\('(/__rd_verify_[^']+)'")

HTML = r"""<!DOCTYPE html>
<html lang="id"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>INTAI — Dashboard Iklan</title>
<style>
  /* ============ INTAI · DESIGN SYSTEM v3 "STUDIO HANGAT" ============
     Referensi: dashboard analytics modern (canvas krem + band teal + angka raksasa),
     pola komponen bersih (stat tiles, pill badge, kartu putih sudut besar).
     Prinsip: satu aksen (teal), warna = status saja, tabel & angka jadi raja. */
  :root{
    --bg:#F7F3EC;            /* kanvas krem hangat */
    --card:#FFFFFF;
    --ink:#22302E;           /* hijau-kehitaman */
    --muted:#6E7C78;
    --faint:#9AA6A1;
    --line:rgba(34,48,46,.10);
    --line2:rgba(34,48,46,.06);
    --accent:#0F5B52;        /* teal gelap */
    --accent2:#134E48;
    --accent-soft:#DCEDE8;
    --soft:#F6EBD4;
    --ok:#2F7D64; --ok-soft:#E4F0EA;
    --warn:#B7791F; --warn-soft:#F6EBD4;
    --bad:#B5443A; --bad-soft:#F6E7E3;
    --blue:#2E6B8F; --blue-soft:#E3EEF4;
    --radius:20px;
    --shadow:0 2px 4px rgba(34,48,46,.04), 0 14px 34px rgba(34,48,46,.07);
    --shadow2:0 24px 60px rgba(34,48,46,.16);
    --ease:cubic-bezier(.22,1,.36,1);
  }
  *{box-sizing:border-box;margin:0;padding:0}
  img{max-width:100%}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
    background:var(--bg); color:var(--ink); line-height:1.5;
    -webkit-font-smoothing:antialiased; font-variant-numeric:tabular-nums;
    background-image:radial-gradient(rgba(15,91,82,.05) 1px, transparent 1px);
    background-size:24px 24px;
  }
  .wrap{max-width:1280px;margin:0 auto;padding:22px 28px 70px}

  /* ---------- Header ---------- */
  header{
    display:flex;align-items:center;gap:12px;
    position:sticky;top:0;z-index:100;
    background:rgba(247,243,236,.85);backdrop-filter:blur(16px) saturate(1.5);-webkit-backdrop-filter:blur(16px) saturate(1.5);
    padding:10px 2px;margin:-22px -2px 0;border-bottom:1px solid var(--line2);
  }
  .logo{display:flex;align-items:center;gap:4px;font-size:19px;font-weight:900;letter-spacing:-.7px;color:var(--ink)}
  .logo .dot{width:23px;height:23px;border-radius:8px;background:var(--accent);color:#fff;font-size:12px;display:inline-flex;align-items:center;justify-content:center;margin-right:8px;box-shadow:0 4px 10px rgba(15,91,82,.3)}
  .logo i{color:var(--accent);font-style:normal}
  .status{margin-left:auto;font-size:11px;color:var(--muted);display:flex;align-items:center;gap:7px;
    background:var(--card);border:1px solid var(--line);padding:5px 12px;border-radius:999px}
  .status .dot{width:7px;height:7px;border-radius:50%;background:var(--ok)}

  /* ---------- Hero / Panggung scan ---------- */
  .panggung{
    position:relative;border-radius:24px;padding:30px 30px 22px;margin:18px 0 8px;
    background:var(--card);border:1px solid var(--line);
    box-shadow:var(--shadow);
  }
  .panggung::before{
    content:"";position:absolute;inset:0;pointer-events:none;border-radius:24px;
    background:
      radial-gradient(600px 200px at 100% 0%, rgba(15,91,82,.06), transparent 65%),
      radial-gradient(500px 180px at 0% 110%, rgba(183,121,31,.05), transparent 60%);
  }
  h1{font-size:24px;font-weight:800;letter-spacing:-.7px;margin:6px 0 5px;position:relative}
  h1 .aksen{color:var(--accent)}
  /* Hero workspace: panel utama + sisi Quick Actions (arsitektur dashboard SaaS) */
  .hero-grid{display:grid;grid-template-columns:1.55fr .85fr;gap:18px;position:relative}
  .hero-main{min-width:0}
  .hero-chips{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:4px}
  .hero-side{
    background:linear-gradient(165deg,#FBFAF7 0%, #F3EFE6 100%);
    border:1px solid var(--line);border-radius:18px;padding:17px 16px;
    display:flex;flex-direction:column;gap:8px;align-self:start;
    box-shadow:inset 0 2px 6px rgba(34,48,46,.03);
  }
  .side-judul{font-size:10px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
  .side-btn{
    display:flex;flex-direction:column;align-items:flex-start;gap:1px;width:100%;
    background:var(--card);border:1px solid var(--line);border-radius:13px;padding:11px 14px;
    cursor:pointer;font-family:inherit;transition:.22s var(--ease);text-align:left;
    box-shadow:0 2px 5px rgba(34,48,46,.04);
  }
  .side-btn:hover{border-color:var(--accent);transform:translateY(-2px);box-shadow:0 10px 22px rgba(15,91,82,.13)}
  .side-btn span{font-size:12px;font-weight:800;color:var(--ink)}
  .side-btn small{font-size:9.5px;color:var(--faint);font-weight:600}
  .side-note{font-size:9.5px;color:var(--faint);line-height:1.7;margin-top:3px}
  /* Alur kerja 4 langkah */
  .alur-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:13px}
  .alur-kartu{
    background:var(--card);border:1px solid var(--line);border-radius:18px;padding:17px 17px 15px;
    box-shadow:var(--shadow);transition:.22s var(--ease);
  }
  .alur-kartu:hover{transform:translateY(-3px);box-shadow:0 18px 40px rgba(34,48,46,.11)}
  .alur-no{
    width:28px;height:28px;border-radius:10px;background:var(--accent-soft);color:var(--accent);
    font-size:12.5px;font-weight:900;display:flex;align-items:center;justify-content:center;margin-bottom:9px;
  }
  .alur-kartu b{font-size:12.5px;font-weight:800;color:var(--ink);letter-spacing:-.1px}
  .alur-kartu p{font-size:10.5px;color:var(--muted);line-height:1.75;margin-top:5px}
  .sub{font-size:13px;color:var(--muted);position:relative;max-width:640px}
  .modebar{display:inline-flex;gap:3px;background:var(--accent-soft);border-radius:12px;padding:4px;margin:16px 0 12px;position:relative}
  .modebar button{
    border:none;background:transparent;padding:8px 15px;border-radius:9px;cursor:pointer;
    font-family:inherit;font-size:12px;font-weight:700;color:var(--accent);transition:.22s var(--ease);
  }
  .modebar button.on{background:var(--accent);color:#fff;box-shadow:0 4px 12px rgba(15,91,82,.25)}
  .scanbar{
    display:flex;gap:10px;align-items:center;background:#FBFAF7;border:1.5px solid var(--line);
    border-radius:16px;padding:9px 9px 9px 18px;transition:.22s var(--ease);position:relative;
    box-shadow:inset 0 2px 6px rgba(34,48,46,.04);
  }
  .scanbar:focus-within{
    border-color:var(--accent);
    box-shadow:0 0 0 4px var(--accent-soft), 0 14px 34px rgba(15,91,82,.12);
    transform:translateY(-1px);
  }
  .scanbar input{
    flex:1;border:none;outline:none;background:transparent;font-size:15px;color:var(--ink);
    padding:10px 0;font-family:inherit;
  }
  .scanbar input::placeholder{color:var(--faint)}
  .btn{
    background:var(--accent);color:#fff;border:none;padding:12px 24px;border-radius:11px;
    font-size:13px;font-weight:800;letter-spacing:.3px;cursor:pointer;transition:.22s var(--ease);font-family:inherit;
    box-shadow:0 6px 18px rgba(15,91,82,.28);
  }
  .btn:hover:not(:disabled){background:var(--accent2);transform:translateY(-1px);box-shadow:0 10px 24px rgba(15,91,82,.34)}
  .btn:active:not(:disabled){transform:translateY(0)}
  .btn:disabled{opacity:.65;cursor:progress}
  .btn-utama{background:var(--accent);color:#fff;border:none;padding:12px 24px;border-radius:11px;
    font-size:13px;font-weight:800;letter-spacing:.3px;cursor:pointer;transition:.22s var(--ease);font-family:inherit;
    box-shadow:0 6px 18px rgba(15,91,82,.28)}
  .btn-utama:hover:not(:disabled){background:var(--accent2);transform:translateY(-1px)}
  .btn-utama:disabled{opacity:.65;cursor:progress}
  .hint{font-size:11px;color:var(--faint);margin-top:9px;position:relative}
  .cbar{display:flex;gap:7px;flex-wrap:wrap;align-items:center;position:relative}

  /* chips & tombol kecil */
  .chip2{
    background:var(--card);border:1px solid var(--line);padding:7px 13px;border-radius:999px;
    font-size:11.5px;font-weight:700;cursor:pointer;transition:.22s var(--ease);font-family:inherit;color:var(--ink);
  }
  .chip2:hover{border-color:var(--accent);color:var(--accent);transform:translateY(-1px)}
  .chip2.on{background:var(--accent);color:#fff;border-color:var(--accent);box-shadow:0 4px 12px rgba(15,91,82,.22)}
  .chip{
    display:inline-block;padding:2px 8px;border-radius:999px;font-size:9.5px;font-weight:800;
    letter-spacing:.3px;white-space:nowrap;
  }
  .tombol{
    background:var(--accent);color:#fff;border:none;padding:9px 15px;border-radius:10px;
    font-size:11.5px;font-weight:750;cursor:pointer;transition:.22s var(--ease);font-family:inherit;
  }
  .tombol:hover{background:var(--accent2);transform:translateY(-1px)}
  .tombol2{
    background:var(--card);color:var(--ink);border:1px solid var(--line);padding:9px 15px;border-radius:10px;
    font-size:11.5px;font-weight:750;cursor:pointer;transition:.22s var(--ease);font-family:inherit;
  }
  .tombol2:hover{border-color:var(--accent);color:var(--accent);transform:translateY(-1px)}
  .cari{
    border:1px solid var(--line);border-radius:11px;padding:8px 13px;font-size:12.5px;
    font-family:inherit;outline:none;background:var(--card);transition:.22s var(--ease);
  }
  .cari:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}

  /* ---------- Status & loading ---------- */
  .status-strip{margin:11px 0 2px;display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--muted)}
  .loading{
    display:none;align-items:center;gap:13px;padding:17px 21px;background:var(--card);
    border:1px solid var(--line);border-radius:var(--radius);margin:14px 0;font-size:13px;color:var(--ink);
    box-shadow:var(--shadow);
  }
  .loading.on{display:flex}
  .spin{width:21px;height:21px;border:3px solid var(--accent-soft);border-top-color:var(--accent);border-radius:50%;animation:spin 1s linear infinite;flex-shrink:0}
  @keyframes spin{to{transform:rotate(360deg)}}
  .error{
    display:none;background:var(--bad-soft);border:1px solid #E8C4BE;color:#7C332B;
    padding:13px 17px;border-radius:14px;margin:12px 0;font-size:12.5px;line-height:1.65;
  }

  /* ---------- Judul bagian ---------- */
  .judul-bagian{
    display:flex;align-items:baseline;gap:11px;flex-wrap:wrap;
    margin:42px 0 15px;padding:0 2px;
  }
  .judul-bagian:first-of-type{margin-top:14px}
  .judul-bagian .eb{
    font-size:9.5px;font-weight:800;letter-spacing:1.2px;color:var(--accent);
    background:var(--accent-soft);padding:4px 11px;border-radius:999px;white-space:nowrap;
  }
  .judul-bagian .jt{font-size:17px;font-weight:800;color:var(--ink);letter-spacing:-.4px}
  .judul-bagian .ket{font-size:11px;color:var(--muted);margin-left:auto;text-align:right}

  /* ---------- KPI band (teal) ---------- */
  .kartus{
    display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:2px;
    background:linear-gradient(135deg,#134E48 0%, #0F5B52 55%, #0D4B44 100%);
    border-radius:22px;padding:10px;box-shadow:0 18px 44px rgba(15,91,82,.28);
  }
  .kpi{padding:15px 18px;position:relative}
  .kpi + .kpi::before{content:"";position:absolute;left:0;top:18%;height:64%;width:1px;background:rgba(255,255,255,.14)}
  .kpi .k{font-size:9.5px;font-weight:800;letter-spacing:1.1px;text-transform:uppercase;color:rgba(255,255,255,.62)}
  .kpi .v{font-size:26px;font-weight:800;letter-spacing:-.8px;margin:5px 0 2px;color:#fff}
  .kpi .s{font-size:10.5px;color:rgba(255,255,255,.55);line-height:1.5}

  /* ---------- Kartu & konten ---------- */
  .chart{
    background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    padding:19px 21px;box-shadow:var(--shadow);position:relative;
  }
  .chart>.k{
    font-size:10.5px;font-weight:800;letter-spacing:1px;text-transform:uppercase;color:var(--muted);
    margin-bottom:14px;position:relative;padding-bottom:10px;
  }
  .chart>.k::after{content:"";position:absolute;left:0;bottom:0;width:26px;height:2.5px;background:var(--accent);border-radius:2px}
  .baris{display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-top:15px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:11px}
  .sel{background:#FBFAF7;border:1px solid var(--line2);border-radius:13px;padding:11px 14px}
  .sel .k{font-size:9.5px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;color:var(--muted)}
  .sel .v{font-size:13px;font-weight:700;color:var(--ink);margin-top:4px;line-height:1.55}
  .subjudul{font-size:12.5px;font-weight:800;letter-spacing:.2px;color:var(--ink);margin:18px 0 9px}
  .note{
    background:var(--accent-soft);border-radius:13px;padding:11px 15px;font-size:11px;
    color:#274F49;line-height:1.75;margin-top:11px;
  }
  .ringkas{
    font-size:13px;line-height:1.9;color:var(--ink);background:var(--card);
    border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:16px;padding:17px 21px;
    box-shadow:var(--shadow);
  }
  /* bar chart mini */
  .cbar{display:flex;gap:10px;align-items:center;padding:4.5px 0}
  .cl{width:112px;font-size:10.5px;font-weight:750;color:var(--muted);text-align:right;flex-shrink:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ct{flex:1;height:13px;background:#F1EDE4;border-radius:999px;overflow:hidden}
  .cf{height:100%;background:linear-gradient(90deg,var(--accent),#2E8B7C);border-radius:999px;transition:width .5s var(--ease)}
  .cn{width:36px;font-size:10px;font-weight:800;color:var(--ink);text-align:right;flex-shrink:0}
  .sbar{display:flex;align-items:center;gap:10px;padding:4.5px 0}
  .slab{width:110px;font-size:10.5px;font-weight:750;color:var(--muted);text-align:right;flex-shrink:0}
  .strack{flex:1;height:13px;background:#F1EDE4;border-radius:999px;overflow:hidden}
  .sfill{height:100%;background:linear-gradient(90deg,var(--accent),#2E8B7C);border-radius:999px;transition:width .5s var(--ease)}
  .sval{width:52px;font-size:10px;font-weight:800;color:var(--ink);text-align:right;flex-shrink:0}

  /* ---------- Toolbar & tabel ---------- */
  .toolbar{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin:4px 0 13px}
  .tabelwrap{
    background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    overflow-x:auto;box-shadow:var(--shadow);
  }
  table{width:100%;border-collapse:collapse;min-width:920px}
  thead th{
    position:sticky;top:53px;background:#FBFAF7;z-index:5;
    font-size:9.5px;font-weight:800;letter-spacing:.9px;text-transform:uppercase;color:var(--muted);
    padding:12px 11px;text-align:left;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;
    transition:.15s;
  }
  thead th:hover{color:var(--accent)}
  thead th.no-sort{cursor:default}
  thead th.no-sort:hover{color:var(--muted)}
  tbody td{padding:11px;border-bottom:1px solid var(--line2);font-size:12px;vertical-align:middle}
  tbody tr.baris-row{cursor:pointer;transition:.15s}
  tbody tr.baris-row:hover{background:#FAF7F1}
  tbody tr.baris-row.open{background:var(--accent-soft)}
  .detail-row td{background:#FBFAF7;padding:0}
  .detail{padding:17px 19px;border-top:1px solid var(--line2)}
  .nama2{font-weight:750;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}
  .cup2{font-size:10.5px;color:var(--faint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:230px;margin-top:1px}
  tbody td img, .ph{width:52px;height:52px;object-fit:cover;border-radius:11px;background:#EFEAE0;display:flex;align-items:center;justify-content:center;font-size:8px;font-weight:800;color:var(--faint)}
  .skor2{
    display:inline-block;background:var(--accent);color:#fff;font-weight:800;font-size:11.5px;
    padding:3.5px 10px;border-radius:8px;min-width:36px;text-align:center;
  }
  .hari2{font-weight:800;font-size:11.5px;color:var(--warn)}
  .col-extra{display:none}
  .tabelwrap.lengkap .col-extra{display:table-cell}

  /* ---------- Bar detail (header baris terbuka / modal) ---------- */
  .bar-detail{display:flex;gap:9px;align-items:center;margin-bottom:11px;flex-wrap:wrap}
  .bar-detail .jd{font-size:13px;font-weight:800;color:var(--ink);letter-spacing:-.2px;flex:1;min-width:220px}
  .idbox,.urlbox,.rawbox{
    background:#FBFAF7;border:1px solid var(--line2);border-radius:12px;padding:11px 14px;
    font-size:11px;color:var(--muted);line-height:1.85;margin-top:10px;word-break:break-all;
  }
  .idbox .k{color:var(--faint);font-weight:750}
  .linkdesc{background:var(--warn-soft);border-radius:11px;padding:10px 14px;font-size:11.5px;color:#7A5A19;margin:8px 0}
  .teks-judul{font-size:13px;font-weight:800;color:var(--ink);margin:10px 0 4px;line-height:1.5}
  .teks-body{font-size:12px;color:#3E4B48;line-height:1.85;white-space:pre-wrap}
  .teks-cap{font-size:10.5px;color:var(--faint);margin-top:5px}
  .galeri{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
  .galeri img{width:86px;height:86px;object-fit:cover;border-radius:12px;cursor:pointer;border:1px solid var(--line2);transition:.22s var(--ease)}
  .galeri img:hover{transform:translateY(-2px);box-shadow:0 10px 22px rgba(34,48,46,.16)}
  .vidwrap video{width:100%;max-width:330px;border-radius:13px;background:#000}
  .play{display:inline-block;background:var(--accent);color:#fff;padding:8px 14px;border-radius:999px;font-size:11px;font-weight:800;cursor:pointer;margin:5px 4px 0 0}
  .taut{display:inline-block;background:var(--accent-soft);color:var(--accent);padding:7px 13px;border-radius:999px;font-size:11px;font-weight:800;cursor:pointer;margin:5px 6px 0 0;transition:.22s var(--ease);word-break:break-all}
  .taut:hover{background:var(--accent);color:#fff}
  .skorbox{display:flex;gap:15px;align-items:center;flex-wrap:wrap;margin:10px 0}
  .aksi{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0;align-items:center}
  .kartu2{
    background:#FBFAF7;border:1px solid var(--line2);border-radius:13px;padding:12px 14px;
    font-size:11px;color:#3E4B48;line-height:1.75;margin:8px 0;
  }
  .kartu2 img{width:62px;height:62px;object-fit:cover;border-radius:10px;float:right;margin-left:10px;border:1px solid var(--line2)}
  .skor-judul{font-size:12px;font-weight:800;color:var(--ink)}
  .angka{font-size:19px;font-weight:900;color:var(--accent);letter-spacing:-.5px}
  .besar{font-size:15px;font-weight:850}
  .resep{background:#FBFAF7;border:1px dashed var(--line);border-radius:13px;padding:12px 16px;font-size:12px;line-height:1.8;color:#3E4B48;margin-top:9px}

  /* ---------- Top produk (tp-*) ---------- */
  .tp-utama{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);overflow:hidden}
  .tp-row{display:flex;gap:13px;align-items:center;padding:13px 17px;border-bottom:1px solid var(--line2);transition:.18s}
  .tp-row:hover{background:#FAF7F1}
  .tp-rank{width:30px;height:30px;border-radius:10px;background:var(--accent-soft);color:var(--accent);font-weight:900;font-size:12.5px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
  .tp-angka{margin-left:auto;text-align:right;font-size:11px;color:var(--muted);line-height:1.7}
  .tp-detail{display:none;padding:12px 17px 15px 60px;background:#FBFAF7;border-bottom:1px solid var(--line2)}
  .tp-ad{background:var(--card);border:1px solid var(--line2);border-radius:11px;padding:10px 14px;font-size:11.5px;margin:6px 0;cursor:pointer;transition:.18s}
  .tp-ad:hover{border-color:var(--accent);transform:translateX(2px)}

  /* ---------- Grid produk ala marketplace ---------- */
  .mp-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:13px}
  .mp-kartu{
    background:var(--card);border:1px solid var(--line);border-radius:17px;overflow:hidden;
    box-shadow:var(--shadow);transition:.22s var(--ease);display:flex;flex-direction:column;
  }
  .mp-kartu:hover{transform:translateY(-4px);box-shadow:0 22px 46px rgba(34,48,46,.13)}
  .mp-img{position:relative;aspect-ratio:1/1;background:#EFEAE0}
  .mp-img img{width:100%;height:100%;object-fit:cover;display:block}
  .mp-img .ph{width:100%;height:100%;border-radius:0}
  .mp-rank{
    position:absolute;top:9px;left:9px;background:rgba(34,48,46,.78);color:#fff;
    font-size:10.5px;font-weight:900;padding:4px 10px;border-radius:999px;backdrop-filter:blur(4px);
  }
  .mp-rank.top{background:rgba(183,121,31,.92)}
  .mp-skor{
    position:absolute;top:9px;right:9px;background:rgba(255,255,255,.92);color:var(--accent);
    font-size:10.5px;font-weight:900;padding:4px 9px;border-radius:999px;box-shadow:0 3px 10px rgba(34,48,46,.14);
  }
  .mp-isi{padding:11px 13px 13px;display:flex;flex-direction:column;gap:4px;flex:1}
  .mp-nama{font-size:11.5px;font-weight:700;color:var(--ink);line-height:1.5;height:2.9em;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
  .mp-toko{font-size:9.5px;color:var(--faint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .mp-harga{font-size:16px;font-weight:900;color:#C24A2E;letter-spacing:-.4px;margin-top:2px}
  .mp-harga small{font-size:10.5px;font-weight:700;color:#C24A2E;opacity:.72}
  .mp-meta{font-size:9.5px;color:var(--muted);font-weight:650}
  .mp-foot{display:flex;align-items:center;justify-content:space-between;gap:6px;margin-top:auto;padding-top:8px}
  .mp-jual{font-size:9px;font-weight:900;color:var(--warn);background:var(--warn-soft);padding:3.5px 9px;border-radius:999px;white-space:nowrap}
  .mp-tombol{
    background:var(--accent);color:#fff;border:none;padding:7px 11px;border-radius:9px;
    font-size:9.5px;font-weight:800;cursor:pointer;transition:.22s var(--ease);font-family:inherit;white-space:nowrap;
  }
  .mp-tombol:hover{background:var(--accent2);transform:translateY(-1px)}

  /* ---------- Modal & lightbox ---------- */
  .modal-bg{
    display:none;position:fixed;inset:0;background:rgba(34,48,46,.42);backdrop-filter:blur(9px);-webkit-backdrop-filter:blur(9px);
    z-index:420;align-items:center;justify-content:center;padding:24px;
  }
  .modal{
    background:rgba(255,255,255,.93);backdrop-filter:blur(26px) saturate(1.6);-webkit-backdrop-filter:blur(26px) saturate(1.6);
    border:1px solid rgba(255,255,255,.8);border-radius:22px;max-width:520px;width:100%;padding:24px;
    box-shadow:var(--shadow2), inset 0 1px 0 rgba(255,255,255,.9), inset 0 -1px 0 rgba(255,255,255,.4);
    max-height:86vh;overflow:auto;
  }
  .modal-judul{font-size:15px;font-weight:800;margin-bottom:8px;letter-spacing:-.2px}
  .modal-aksi{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
  #murl{background:#FBFAF7;border:1px solid var(--line2);border-radius:11px;padding:11px 14px;font-size:11px;color:var(--muted);word-break:break-all;margin:10px 0;line-height:1.7}
  .lightbox{
    display:none;position:fixed;inset:0;background:rgba(22,32,30,.88);z-index:400;
    flex-direction:column;align-items:center;justify-content:center;gap:14px;padding:22px;
  }
  .lb-med img{max-width:88vw;max-height:74vh;border-radius:16px}
  .lb-med video{max-width:88vw;max-height:74vh;border-radius:16px}
  .lb-aksi{display:flex;gap:9px;flex-wrap:wrap;justify-content:center}
  .lb-x{position:absolute;top:18px;right:22px;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25);color:#fff;
    width:38px;height:38px;border-radius:50%;font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center}

  /* ---------- Halaman detail iklan (overlay penuh) ---------- */
  #halaman-iklan{display:none;position:fixed;inset:0;background:rgba(22,32,30,.5);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);overflow-y:auto;z-index:300;padding:22px}
  .wrap-halaman{
    max-width:1020px;margin:0 auto;background:rgba(255,255,255,.96);backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px);
    border:1px solid rgba(255,255,255,.85);border-radius:22px;padding:24px;
    box-shadow:var(--shadow2), inset 0 1px 0 rgba(255,255,255,.9);
  }

  /* ---------- Toast & footer ---------- */
  .toast{
    position:fixed;bottom:26px;left:50%;transform:translateX(-50%) translateY(16px);opacity:0;pointer-events:none;
    background:#22302E;color:#fff;padding:11px 21px;border-radius:999px;font-size:12px;font-weight:750;z-index:500;
    transition:.28s var(--ease);box-shadow:0 14px 34px rgba(34,48,46,.35);
  }
  .toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
  .foot{margin-top:52px;padding-top:21px;border-top:1px solid var(--line2);font-size:10.5px;color:var(--faint);line-height:1.9}
  .mode{display:inline-block;background:var(--accent-soft);color:var(--accent);padding:3px 10px;border-radius:999px;font-size:10px;font-weight:800;letter-spacing:.4px}

  /* ---------- Responsif laptop & kecil ---------- */
  @media (max-width: 1440px){
    .wrap{padding:18px 18px 55px}
    .panggung{padding:22px 20px 18px}
    h1{font-size:21px}
    .nama2{max-width:180px}
    .cup2{max-width:190px}
    .judul-bagian .ket{width:100%;margin-left:0;text-align:left}
    .alur-grid{grid-template-columns:1fr 1fr}
    .mp-grid{grid-template-columns:repeat(4,1fr)}
  }
  @media (max-width: 900px){
    .grid2,.baris{grid-template-columns:1fr}
    .kartus{grid-template-columns:1fr 1fr}
    .modebar{flex-wrap:wrap}
    .hero-grid{grid-template-columns:1fr}
    .alur-grid{grid-template-columns:1fr}
    .mp-grid{grid-template-columns:1fr 1fr}
  }
</style>
</head>
<body><div class="wrap">

<header>
  <div class="logo"><span class="dot">I</span>INT<i>AI</i></div>
  <button class="tombol2" onclick="location.href='/'" title="Kembali ke dashboard INTAI">← Dashboard</button>
  <div class="status" id="status">Siap — mesin standby</div>
</header>

<div class="panggung">
  <div class="hero-grid">
    <div class="hero-main">
      <div class="hero-chips">
        <span class="mode">Workspace Riset</span>
        <span class="mode" id="meta-mode">Mode: Kata Kunci</span>
        <span class="mode" id="meta-waktu">Siap dipakai</span>
      </div>
      <h1>Intelijen Iklan <span class="aksen">Meta Ad Library</span></h1>
      <div class="sub">Ketik satu niche → dapatkan 10 produk teratas, peta harga, estimasi ekonomi, dan resep jualan. Data publik, dianalisis untuk seller Indonesia.</div>

      <div class="modebar">
        <button class="mode on" id="md-cari" onclick="setMode('cari')">🔍 Kata Kunci</button>
        <button class="mode" id="md-halaman" onclick="setMode('halaman')">🎯 Intip Kompetitor</button>
      </div>
      <div class="scanbar">
        <input id="q" type="text" placeholder="Kata kunci produk... (contoh: rak sepatu, serum, kopi diet)" autocomplete="off">
        <button class="btn" id="go" onclick="scan()">SCAN</button>
      </div>
      <div class="hint" id="modetext">Mode kata kunci: semua iklan aktif yang mengandung kata tsb · Tekan Enter untuk scan</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:12px 0 2px;align-items:center">
        <span style="font-size:11px;color:var(--faint);font-weight:650">Coba cepat:</span>
        <button class="chip2" onclick="isiCepat('rak sepatu')">rak sepatu</button>
        <button class="chip2" onclick="isiCepat('serum')">serum</button>
        <button class="chip2" onclick="isiCepat('kopi diet')">kopi diet</button>
        <button class="chip2" onclick="isiCepat('hijab')">hijab</button>
        <button class="chip2" onclick="isiCepat('madu')">madu</button>
      </div>
    </div>

    <aside class="hero-side">
      <div class="side-judul">⚡ Aksi Cepat</div>
      <button class="side-btn" onclick="eksporCSV()"><span>⬇ Ekspor CSV</span><small>unduh semua hasil scan</small></button>
      <button class="side-btn" onclick="toggleFilterBar();const fb=document.getElementById('filterbar');fb.style.display='flex';fb.scrollIntoView({behavior:'smooth',block:'center'})"><span>⚙ Filter Lanjutan</span><small>rentang umur · harga · format</small></button>
      <button class="side-btn" onclick="lihatPantauan()"><span>⭐ Pantauan (<span id="jml-pantauan">0</span>)</span><small>produk yang kamu bintangi</small></button>
      <button class="side-btn" id="btn-kolom" onclick="toggleKolom()"><span>▦ Tampilan: Ringkas</span><small>atur jumlah kolom tabel</small></button>
      <div class="side-note">Semua aksi terkumpul di sini supaya area kerja tetap bersih — fokus ke data, bukan nyari tombol.</div>
    </aside>
  </div>
</div>

<div class="judul-bagian">
  <span class="eb">CARA BACA</span>
  <span class="jt">Alur kerja dalam 4 langkah</span>
  <span class="ket">Ikuti urutannya — dari gambaran besar sampai bedah data</span>
</div>
<div class="alur-grid">
  <div class="alur-kartu"><div class="alur-no">1</div><b>Ringkasan & Produk Teratas</b><p>Baca situasi niche dalam satu paragraf, lalu lihat siapa yang menang dan resep jualannya.</p></div>
  <div class="alur-kartu"><div class="alur-no">2</div><b>Grafik Niche</b><p>Pahami medan jualan: funnel, format kreatif, waktu tayang, dan CTA yang dipakai pasar.</p></div>
  <div class="alur-kartu"><div class="alur-no">3</div><b>Rak Angle</b><p>Kumpulkan hook terbukti dari iklan paling kuat — bahan untuk naskah kamu sendiri.</p></div>
  <div class="alur-kartu"><div class="alur-no">4</div><b>Data Lengkap</b><p>Bedah tiap iklan: harga 3 lapis, video, estimasi duit, sampai rakit naskah video.</p></div>
</div>

<div class="loading" id="loading"><div class="spin"></div><span id="tahap-loading">① Menghubungi Meta Ad Library… ±30 detik</span></div>
<div class="error" id="error"></div>

<div id="dashboard" style="display:none">
  <div class="judul-bagian">
    <span class="eb">RINGKASAN</span>
    <span class="jt">Ringkasan Eksekutif</span>
    <span class="ket">Disusun otomatis dari hasil scan — baca ini dulu sebelum menyelam</span>
  </div>
  <div id="isi-ringkasan" class="ringkas"></div>

  <div class="judul-bagian">
    <span class="eb">ANGKA PENTING</span>
    <span class="jt">Indikator Niche</span>
    <span class="ket">Sentuh judul untuk penjelasan · pita warna = asal data</span>
  </div>
  <div class="kartus" id="kpis"></div>

  <div class="judul-bagian">
    <span class="eb">LANGKAH 1</span>
    <span class="jt">🥇 10 Produk Ads Teratas</span>
    <span class="ket">Ranking acuan jualan: penjualan × omzet × lama aktif · klik untuk detail</span>
  </div>
  <div id="top10"></div>
  <div id="acuan" style="margin-top:10px"></div>

  <div class="judul-bagian">
    <span class="eb">LANGKAH 2</span>
    <span class="jt">📊 Grafik Niche</span>
    <span class="ket">Medan jualan, format, waktu, dan CTA yang dipakai pasar</span>
  </div>
  <div class="baris">
    <div class="chart"><div class="k">Funnel Penjualan</div><div id="ch-funnel"></div></div>
    <div class="chart"><div class="k">Format Iklan</div><div id="ch-format"></div></div>
  </div>
  <div class="baris">
    <div class="chart"><div class="k">📅 Kapan Iklan Dimulai</div><div id="ch-bulan"></div></div>
    <div class="chart"><div class="k">🎯 CTA yang Dipakai</div><div id="ch-cta"></div></div>
  </div>

  <div class="judul-bagian">
    <span class="eb">LANGKAH 3</span>
    <span class="jt">🧠 Rak Angle</span>
    <span class="ket">Hook dari iklan paling kuat — klik baris untuk baca lengkap</span>
  </div>
  <div class="chart" style="margin-top:0"><div id="ch-hook"></div></div>

  <div class="judul-bagian">
    <span class="eb">LANGKAH 4</span>
    <span class="jt">📋 Data Iklan Lengkap</span>
    <span class="ket">Sortable · klik baris = detail penuh · tab status untuk fokus cepat</span>
  </div>
  <div class="toolbar">
    <button class="chip2 on" data-f="semua" onclick="setFilter('semua',this)">Semua (<span id="ct-semua">0</span>)</button>
    <button class="chip2" data-f="MENANG" onclick="setFilter('MENANG',this)" title="Sinyal kuat: skor tinggi + lama tayang / bukti jual">★ MENANG (<span id="ct-menang">0</span>)</button>
    <button class="chip2" data-f="NAIK" onclick="setFilter('NAIK',this)" title="Kandidat kuat untuk dites/dinaikkan">↗ NAIK (<span id="ct-naik">0</span>)</button>
    <button class="chip2" data-f="UJI" onclick="setFilter('UJI',this)" title="Sinyal awal untuk eksplorasi">◌ UJI (<span id="ct-uji">0</span>)</button>
    <span style="width:1px;height:18px;background:var(--line);margin:0 5px"></span>
    <button class="chip2" data-f="CTWA/WA" onclick="setFilter('CTWA/WA',this)">CTWA/WA</button>
    <button class="chip2" data-f="Marketplace" onclick="setFilter('Marketplace',this)">Marketplace</button>
    <button class="chip2" data-f="LP/Website" onclick="setFilter('LP/Website',this)">LP/Website</button>
    <button class="chip2" data-f="Tanpa tautan" onclick="setFilter('Tanpa tautan',this)">Tanpa tautan</button>
    <span style="width:1px;height:18px;background:var(--line);margin:0 5px"></span>
    <button class="chip2 on" id="ch-lepas" onclick="toggleLepas()" title="Sembunyikan iklan yang tidak nyambung dengan kata kunci">🚫 Sembunyikan niche luar</button>
    <button class="chip2" data-f="Baru" onclick="setFilter('Baru',this)" title="Hanya iklan yang mulai ≤7 hari — sinyal awal">🔥 Baru ≤7h</button>
    <button class="chip2" onclick="toggleFilterBar()" title="Filter lanjutan: rentang umur, harga, format">⚙ Filter</button>
    <input class="cari" id="cari" type="text" placeholder="Cari di hasil..." oninput="render()">
    <div style="margin-left:auto;font-size:11px;color:var(--muted)" id="countinfo"></div>
  </div>

  <div class="toolbar" id="filterbar" style="display:none;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:9px 13px;align-items:center">
    <span style="font-size:11px;color:var(--muted);font-weight:700;letter-spacing:.4px">UMUR (hari):</span>
    <input class="cari" id="f-umur-min" type="number" min="0" placeholder="min" style="width:62px;border:1px solid var(--line);border-radius:8px;padding:5px 7px" oninput="render()">
    <span style="color:var(--muted)">–</span>
    <input class="cari" id="f-umur-max" type="number" min="0" placeholder="maks" style="width:62px;border:1px solid var(--line);border-radius:8px;padding:5px 7px" oninput="render()">
    <span style="font-size:11px;color:var(--muted);font-weight:700;letter-spacing:.4px;margin-left:10px">HARGA (Rp):</span>
    <input class="cari" id="f-hrg-min" type="number" min="0" placeholder="min" style="width:88px;border:1px solid var(--line);border-radius:8px;padding:5px 7px" oninput="render()">
    <span style="color:var(--muted)">–</span>
    <input class="cari" id="f-hrg-max" type="number" min="0" placeholder="maks" style="width:88px;border:1px solid var(--line);border-radius:8px;padding:5px 7px" oninput="render()">
    <span style="font-size:11px;color:var(--muted);font-weight:700;letter-spacing:.4px;margin-left:10px">FORMAT:</span>
    <select id="f-format" onchange="render()" style="border:1px solid var(--line);border-radius:8px;padding:5px 8px;font-family:inherit;font-size:12px;background:#fff">
      <option value="semua">Semua</option>
      <option value="VIDEO">Video</option>
      <option value="IMAGE">Gambar</option>
      <option value="CAROUSEL">Carousel</option>
      <option value="DPA">DPA (katalog)</option>
      <option value="DCO">DCO</option>
    </select>
    <button class="chip2" onclick="resetFilterLanjutan()">↺ Reset</button>
  </div>

  <div class="tabelwrap">
    <table>
      <thead><tr>
        <th class="no-sort">Foto</th>
        <th onclick="sortBy('halaman')" title="Nama halaman pengiklan (akun FB/IG yang pasang iklan)">Pengiklan</th>
        <th class="col-extra" onclick="sortBy('rel_nilai')" title="Kecocokan iklan dengan kata kunci scan — 100 = nyambung penuh, kecil = menjorok ke niche lain">Relevansi</th>
        <th onclick="sortBy('skor')" title="Skor Cuan 0–100: umur tayang + jumlah varian + funnel penjualan + video. Makin tinggi = makin teruji pasar">Skor</th>
        <th onclick="sortBy('jual')" title="Skor Jual 0–100: bukti terjual / estimasi omzet + ketahanan + varian + nilai transaksi. 🏆 ≥60 = layak jadi acuan">Skor Jual</th>
        <th onclick="sortBy('umur')" title="Berapa hari iklan ini sudah tayang — proxy profitabilitas (iklan boncos jarang bertahan lama)">Umur</th>
        <th onclick="sortBy('harga_min')" title="Rentang harga yang terdeteksi: katalog iklan (DPA) + halaman tujuan (LP) + marketplace">Harga</th>
        <th onclick="sortBy('est_min')" title="Estimasi belanja iklan per bulan — MODEL 🟠 (Meta tak merilis data belanja iklan komersial)">Est. Blanja/bln</th>
        <th class="col-extra" onclick="sortBy('mulai')" title="Tanggal iklan mulai tayang (dari Meta)">Mulai</th>
        <th class="col-extra" onclick="sortBy('akhir')" title="Perkiraan tanggal berakhir, dari lamanya siklus batch varian">Akhir</th>
        <th class="col-extra" onclick="sortBy('format')" title="Bentuk kreatif: Video / Gambar / Carousel">Format</th>
        <th onclick="sortBy('funnel')" title="Kemana iklan mengarah: WA (chat) / Marketplace (Shopee dll) / Website (LP)">Funnel</th>
        <th class="col-extra" onclick="sortBy('cta')" title="Call to Action — tombol yang diklik audiens">CTA</th>
        <th class="col-extra" onclick="sortBy('varian')" title="Jumlah iklan sejenis dalam satu batch — sinyal seriusnya pengiklan (makin banyak = makin yakin menang)">Varian</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</div>

<div class="foot">INTAI v0.6 Hub · sinyal: umur + varian + funnel + format · klik baris = detail lengkap · <b onclick="location.href='/'" style="cursor:pointer">← kembali ke dashboard</b></div>

<div class="lightbox" id="lb" onclick="if(event.target===this)tutupLb()">
  <button class="lb-x" onclick="tutupLb()" title="Tutup (Esc)">✕</button>
  <div class="lb-med" id="lbmed"></div>
  <div class="lb-aksi" id="lbaksi"></div>
</div>
<div id="halaman-iklan"><div class="wrap-halaman" id="isi-halaman-iklan"></div></div>
<div class="modal-bg" id="mbg" onclick="if(event.target===this)tutupModal()">
  <div class="modal">
    <div class="modal-judul">🔗 Link</div>
    <div class="urlbox besar" id="murl"></div>
    <div class="modal-aksi">
      <button class="tombol" onclick="salin(document.getElementById('murl').textContent)">Salin Link</button>
      <button class="tombol2" onclick="tutupModal()">← Kembali / Tutup (Esc)</button>
    </div>
    <div class="hint" style="margin-top:11px">Preview memblokir tab baru — salin link lalu buka di browser lu. Di laptop (app.bat) tombol buka langsung jalan.</div>
  </div>
</div>
<div class="toast" id="toast">Tersalin!</div>

</div>
<script>
const WARN = {"CTWA/WA":"#6D4AFF","Marketplace":"#C97F00","LP/Website":"#0E7490","Tanpa tautan":"#6B7280"};
const esc = s => (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const bersih = s => (s||"").replace(/\{\{[^}]*\}\}/g,"…").replace(/\s+/g," ");
function tandai(teksEsc, cocok){
  (cocok || []).forEach(t => {
    const kata = String(t).replace(" (nama halaman)","").replace(" (tautan)","").trim();
    if(kata.length < 2) return;
    const re = new RegExp("(" + kata.replace(/[.*+?^${}()|[\]\\]/g,"\\$&") + ")", "gi");
    teksEsc = teksEsc.replace(re, '<mark style="background:#C9E2DB;padding:0 2px;border-radius:3px">$1</mark>');
  });
  return teksEsc;
}
let DATA = [], FILTER = "semua", SORT = {k:"skor", asc:false}, MODE = "cari", HOOKS = [];
let SEMBUNYIKAN_LEPAS = true;

function toggleLepas(){
  SEMBUNYIKAN_LEPAS = !SEMBUNYIKAN_LEPAS;
  document.getElementById("ch-lepas").classList.toggle("on", SEMBUNYIKAN_LEPAS);
  render();
}

const WARNA_REL = {"Relevan":"#2F7D64","Longgar":"#B7791F","Lepas":"#B5443A"};
function fmtHarga(n){
  if(n === null || n === undefined) return "—";
  n = Number(n);
  if(n >= 1e9) return (n/1e9).toFixed(1).replace(".0","").replace(".",",") + "M";
  if(n >= 1e6) return (n/1e6).toFixed(n >= 1e7 ? 0 : 1).replace(".0","").replace(".",",") + "jt";
  if(n >= 1e3) return Math.round(n/1e3) + "rb";
  return String(n);
}
function rupiah(n){ return "Rp" + Number(n).toLocaleString("id-ID"); }

function isiCepat(kata){
  if(MODE !== "cari") setMode("cari");
  document.getElementById("q").value = kata;
  scan();
}
function setMode(m){
  MODE = m;
  document.getElementById("md-cari").classList.toggle("on", m === "cari");
  document.getElementById("md-halaman").classList.toggle("on", m === "halaman");
  const q = document.getElementById("q");
  if(m === "halaman"){
    q.placeholder = "ID Halaman kompetitor (angka)... ambil dari detail iklan";
    document.getElementById("modetext").innerHTML = "Mode kompetitor: <b>semua iklan aktif dari satu halaman</b> — tombolnya ada di detail tiap iklan · Enter untuk scan";
  } else {
    q.placeholder = "Kata kunci produk... (contoh: rak sepatu, serum, kopi diet)";
    document.getElementById("modetext").innerHTML = "Mode kata kunci: semua iklan aktif yang mengandung kata tsb · Tekan Enter untuk scan";
  }
}
function intipHalaman(pid){
  setMode("halaman");
  document.getElementById("q").value = pid;
  window.scrollTo({top:0, behavior:"smooth"});
  scan();
}

function buka(u){
  if(!u) return;
  const w = window.open(u, "_blank");
  if(!w){ document.getElementById("murl").textContent = u; document.getElementById("mbg").style.display = "flex"; }
}
function tutupModal(){ document.getElementById("mbg").style.display = "none"; }
function salin(u){
  if(!u) return;
  const sukses = () => { const t = document.getElementById("toast"); t.classList.add("on"); setTimeout(() => t.classList.remove("on"), 1600); };
  const manual = () => { const ta = document.createElement("textarea"); ta.value = u; document.body.appendChild(ta); ta.select(); try{ document.execCommand("copy"); }catch(e){} ta.remove(); sukses(); };
  if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(u).then(sukses).catch(manual); } else { manual(); }
}
function imgTag(u){
  if(!u) return "";
  return '<img loading="lazy" src="/img?u=' + encodeURIComponent(u) + '" data-u="' + esc(u) + '" onclick="event.stopPropagation();lihatGambar(this.dataset.u)" title="Klik untuk memperbesar">';
}
function lihatGambar(u){
  if(!u) return;
  document.getElementById("lbmed").innerHTML = '<img src="/img?u=' + encodeURIComponent(u) + '">';
  document.getElementById("lbaksi").innerHTML =
    '<button class="tombol2" onclick="tutupLb()">← Kembali (Esc)</button>' +
    '<button class="tombol2" data-u="' + esc(u) + '" onclick="buka(this.dataset.u)">Buka file asli</button>' +
    '<button class="tombol2" data-u="' + esc(u) + '" onclick="salin(this.dataset.u)">Salin link media</button>';
  document.getElementById("lb").style.display = "flex";
}
function lihatVideo(hd, sd){
  const u = hd || sd;
  if(!u) return;
  document.getElementById("lbmed").innerHTML =
    '<video controls autoplay playsinline src="/vid?u=' + encodeURIComponent(u) + '"></video>';
  document.getElementById("lbaksi").innerHTML =
    '<button class="tombol2" onclick="tutupLb()">← Kembali (Esc)</button>' +
    (hd && sd ? '<button class="tombol2" data-u="' + esc(hd) + '" onclick="gantiVideo(this.dataset.u)">HD</button>' +
      '<button class="tombol2" data-u="' + esc(sd) + '" onclick="gantiVideo(this.dataset.u)">SD</button>' : "") +
    '<button class="tombol2" data-u="' + esc(u) + '" onclick="buka(this.dataset.u)">Buka file asli</button>' +
    '<button class="tombol2" data-u="' + esc(u) + '" onclick="salin(this.dataset.u)">Salin link video</button>';
  document.getElementById("lb").style.display = "flex";
}
function gantiVideo(u){
  const v = document.querySelector("#lbmed video");
  if(v){ v.src = "/vid?u=" + encodeURIComponent(u); v.play().catch(() => {}); }
}
function tutupLb(){
  document.getElementById("lbmed").innerHTML = "";
  document.getElementById("lb").style.display = "none";
}
document.addEventListener("keydown", e => { if(e.key === "Escape"){ const tampil = id => { const el = document.getElementById(id); return el && getComputedStyle(el).display !== "none"; }; if(tampil("mbg")){ tutupModal(); return; } if(tampil("lb")){ tutupLb(); return; } if(tampil("halaman-iklan")){ tutupHalamanIklan(); return; } const br = document.querySelector(".brief-ov"); if(br){ br.remove(); return; } } });

function bar(nama, v, max){
  return '<div class="sbar"><span class="slab">' + nama + '</span><div class="strack"><div class="sfill" style="width:' +
    Math.min(100, Math.round(v / max * 100)) + '%"></div></div><span class="sval">' + v.toFixed(1) + ' / ' + max + '</span></div>';
}
function sel(k, v){
  const hampa = (v === null || v === undefined || v === "" || v === "None" ||
    v === "null" || v === "undefined" || v === "[]" || v === "{}");
  return '<div class="sel"><div class="k">' + k + '</div><div class="v">' + (hampa ? "—" : v) + '</div></div>';
}

function detailHTML(x){
  const d = x.detail;
  let h = "";
  const vMeta = '<span class="chip" style="color:#2F7D64;border-color:#BEDCD2;background:#E4F0EA;font-size:9px;padding:1px 6px;margin-left:5px">🟢 Asli Meta</span>';
  const vGali = '<span class="chip" style="color:#2F7D64;border-color:#BEDCD2;background:#E4F0EA;font-size:9px;padding:1px 6px;margin-left:5px">🟢 Digali web</span>';
  const vHitung = '<span class="chip" style="color:#2E6B8F;border-color:#DDD6CA;background:#E3EEF4;font-size:9px;padding:1px 6px;margin-left:5px">🔵 Dihitung</span>';
  const vModel = '<span class="chip" style="color:#B7791F;border-color:#E8D9B8;background:var(--soft);font-size:9px;padding:1px 6px;margin-left:5px">🟠 Model</span>';
  h += '<div class="note" style="margin-bottom:8px"><b>📋 Keterangan sumber data:</b> 🟢 <b>Asli</b> (dari Meta / digali langsung dari web) · 🔵 <b>Dihitung</b> (olah dari data Meta) · 🟠 <b>Model</b> (estimasi transparan, bukan data Meta). Setiap angka di panel ini diberi pita asal-usulnya.</div>';

  // MEDIA
  const imgs = (d.images || []).concat(d.extra_images || []), vids = d.videos || [];
  if(imgs.length || vids.length){
    h += '<div class="galeri">';
    imgs.forEach(u => { h += imgTag(u); });
    vids.forEach(v => {
      const t = v.thumb ? '<img loading="lazy" src="/img?u=' + encodeURIComponent(v.thumb) + '">' : '<div style="height:128px;width:190px;background:#EAE6DC;border-radius:11px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:800;color:#B08A2E">VIDEO</div>';
      h += '<div class="vidwrap" data-hd="' + esc(v.hd || "") + '" data-sd="' + esc(v.sd || "") +
        '" onclick="event.stopPropagation();lihatVideo(this.dataset.hd,this.dataset.sd)">' + t + '<div class="play">▶</div></div>';
    });
    h += '</div><div class="hint">Klik foto = perbesar · klik ▶ = putar video asli (HD/SD)</div>';
  }

  // SKOR
  const sk = d.skor_detail;
  h += '<div class="subjudul">Skor Cuan — rincian</div><div class="skorbox">' +
    '<div class="skor-judul"><span>Perhitungan transparan</span><span class="angka">' + sk.total.toFixed(1) + '</span></div>' +
    bar("Umur iklan", sk.umur, 50) + bar("Varian", sk.varian, 25) +
    bar("Funnel", sk.funnel, 15) + bar("Format", sk.video, 10) + '</div>';

  // TEKS
  const cocok = (x.relevansi && x.relevansi.cocok) || [];
  h += '<div class="subjudul">Teks Iklan Lengkap</div>';
  if(d.title) h += '<div class="teks-judul">' + tandai(esc(d.title), cocok) + '</div>';
  if(d.body)  h += '<div class="teks-body">' + tandai(esc(d.body), cocok) + '</div>';
  if(d.caption) h += '<div class="teks-cap">' + tandai(esc(d.caption), cocok) + '</div>';
  if(d.link_desc) h += '<div class="linkdesc"><b>Deskripsi link:</b> ' + tandai(esc(d.link_desc), cocok) + '</div>';
  if(!d.title && !d.body && !d.caption) h += '<div class="hint">Tanpa teks (kemungkinan murni visual).</div>';

  // INFO
  const cta = (d.cta_text || "") + (d.cta_type ? ' <span style="color:#6E7C78;font-weight:600">(' + esc(String(d.cta_type).replace(/_/g," ")) + ')</span>' : "");
  const rel = x.relevansi || {nilai:100, label:"Relevan", cocok:[]};
  const wr = WARNA_REL[rel.label] || "#6B7280";
  h += '<div class="subjudul">Info Iklan</div><div class="grid2">' +
    sel("CTA", cta || "—") + sel("Format", esc(d.display_format)) +
    sel("Relevansi kata kunci", '<span class="chip" style="color:' + wr + ';border-color:' + wr + '33;background:' + wr + '12">' + esc(rel.label) + ' (' + rel.nilai + '/100)</span>' +
      (rel.cocok && rel.cocok.length ? '<div style="font-size:11px;color:#6E7C78;margin-top:4px">cocok: ' + esc(rel.cocok.join(", ")) + '</div>' : "")) +
    sel("Mulai tayang", esc(d.mulai)) + sel("Berakhir", esc(d.berakhir)) +
    sel("Umur", d.umur + " hari") + sel("Status", d.is_active ? "🟢 Aktif" : "🔴 Tidak aktif") +
    sel("Varian (batch)", (d.varian != null ? d.varian + " iklan" : "—")) +
    sel("Grup varian (collation)", esc(String(d.collation_id || "—"))) +
    sel("Kategori iklan", esc((d.kategori || []).join(", "))) +
    sel("Negara ditarget", esc((d.negara || []).join(", "))) + '</div>';

  // PETA HARGA (digali otomatis)
  const hd = (d.harga_dpa || []).filter(v => v);
  const hl = d.harga_lp || {};
  const hm = d.harga_mp || {};
  if(hd.length || (hl.tipe || hm.tipe)){
    const teksLP = (hl.harga || []).length
      ? (hl.min === hl.max ? rupiah(hl.min) : rupiah(hl.min) + " – " + rupiah(hl.max)) +
        ((hl.promo || []).length ? ' <b style="color:#B7791F">[' + esc(hl.promo.join(", ")) + ']</b>' : "") +
        ((hl.terjual || []).length ? ' · <b>📦 terjual ' + esc(hl.terjual.join(", ")) + '</b>' : "") +
        ((hl.stok || []).length ? ' · sisa stok ' + esc(hl.stok.join(", ")) : "") +
        ((hl.sosial || []).length ? ' · 👁 ' + esc(hl.sosial.join(", ")) + ' orang merespons' : "")
      : (hl.tipe === "sosial" ? "Tautan sosial — tanpa harga di halaman" :
         hl.tipe === "web" ? "Halaman hidup, harga tidak terbaca otomatis" :
         hl.tipe === "gagal" ? "Gagal diambil" + (hl.error ? " (" + esc(hl.error) + ")" : "") : "—");
    const teksMP = (hm.harga || []).length
      ? (hm.min === hm.max ? rupiah(hm.min) : rupiah(hm.min) + " – " + rupiah(hm.max)) +
        ((hm.terjual || []).length ? ' · <b>📦 terjual ' + esc(hm.terjual.join(", ")) + '</b>' : "") +
        ((hm.rating || []).length ? ' · ⭐ ' + esc(hm.rating.join(", ")) + '</b>' : "") +
        ((hm.stok || []).length ? ' · sisa stok ' + esc(hm.stok.join(", ")) : "")
      : (hm.tipe === "terkunci" ? "🔒 Anti-bot — buka manual lewat tombol di bawah" :
         hm.tipe === "gagal" ? "Gagal diambil" : "—");
    h += '<div class="subjudul">Peta Harga (digali otomatis)</div><div class="grid2">' +
      sel("Harga katalog (DPA)" + vMeta, hd.length ? hd.map(v => rupiah(v)).join("\n") : "—") +
      sel("Harga di LP tujuan" + vGali, teksLP) +
      sel("Harga di Marketplace" + vGali, teksMP) + '</div>' +
      '<div class="note">💡 Harga DPA = katalog iklan · Harga LP = yang dilihat pembeli (sering beda karena promo) · Marketplace = harga + bukti terjual (jika gerbang anti-bot terbuka). Angka digali langsung dari halaman tujuan saat scan.</div>';
    if(d.cek_harga){
      const ch = d.cek_harga;
      const oke = ch.status === "konsisten";
      const warna = oke ? "#2F7D64" : "#B7791F";
      h += '<div class="note" style="color:' + warna + '">' + (oke ? '✅' : '⚠️') + ' <b>Validasi silang harga:</b> katalog DPA ' + rupiah(ch.dpa) + ' vs LP ' + rupiah(ch.lp) + ' → <b>' + esc(ch.status) + '</b> (selisih ' + Math.abs(ch.selisih) + '%)' +
        (oke ? ' — dua sumber saling menguatkan.' : (ch.selisih > 0 ? ' — LP mengkoreksi harga ke bawah (kemungkinan promo/psikologi harga).' : ' — LP memasang lebih mahal dari katalog.')) + '</div>';
    }
  }

  // ESTIMASI EKONOMI (MODEL)
  const ek = d.ekonomi;
  if(ek){
    const sj = d.skor_jual || {};
    h += '<div class="subjudul">Estimasi Ekonomi (Model)</div><div class="grid2">' +
      sel("Skor Jual (acuan menang)" + vHitung, (sj.nilai != null ? '<b style="font-size:15px">' + sj.nilai + '/100</b> ' + esc(sj.label || '') : '—') +
        (sj.rincian ? '<div style="font-size:10.5px;color:#6E7C78;margin-top:4px">' + esc(Object.entries(sj.rincian).map(kv => kv[0] + ' ' + kv[1]).join(' · ')) + '</div>' : "")) +
      sel("Keyakinan model", ek.keyakinan ? '<b style="color:' + (ek.keyakinan.level === "tinggi" ? "#2F7D64" : ek.keyakinan.level === "sedang" ? "#B7791F" : "#6E7C78") + '">' + esc(String(ek.keyakinan.level).toUpperCase()) + '</b> — ' + esc(ek.keyakinan.alasan) : "—") +
      sel("Belanja iklan / bulan" + vModel, rupiah(ek.bulanan_min) + " – " + rupiah(ek.bulanan_max)) +
      sel("Belanja kumulatif (" + d.umur + " hari)" + vModel, rupiah(ek.kumulatif_min) + " – " + rupiah(ek.kumulatif_max)) +
      sel("Omzet minimum (titik impas)" + vModel, rupiah(ek.bulanan_min) + " – " + rupiah(ek.bulanan_max) + " / bln") +
      sel("Estimasi omzet (ROAS 1,5–3)" + vModel, rupiah(ek.omzet_min) + " – " + rupiah(ek.omzet_max) + " / bln") +
      sel("Estimasi unit terjual" + vModel, ek.unit ? ek.unit[0] + " – " + ek.unit[1] + " unit / bln" : "Harga produk tidak diketahui") +
      sel("Dasar skala (varian)", ek.varian + " iklan dalam batch") + '</div>' +
      (d.kalibrasi ? '<div class="note" style="margin-top:6px;border-left:3px solid #2F7D64">🎯 <b>Kalibrasi model vs bukti nyata:</b> halaman tujuan mencatat <b>' + Number(d.kalibrasi.bukti).toLocaleString("id-ID") + ' terjual</b> (🟢 digali). Model mengestimasi <b>' + d.kalibrasi.model_kum[0].toLocaleString("id-ID") + '–' + d.kalibrasi.model_kum[1].toLocaleString("id-ID") + '</b> sepanjang umur iklan (' + d.umur + ' hari) — ' + esc(d.kalibrasi.posisi) + '. Catatan: angka terjual biasanya kumulatif sejak produk tayang (bisa lebih tua dari iklannya), jadi ini batas pembanding, bukan pengganti.</div>' : '') +
      '<div class="note">🧮 <b>MODEL, bukan data Meta</b> (Meta tak pernah rilis belanja iklan komersial). Asumsi transparan: operasi harian dari jumlah varian (kecil 50–400rb · sedang 300rb–1,5jt · besar 1–4jt) × faktor keberlanjutan umur · ROAS sehat pasar ID 1,5–3 · omzet = belanja × ROAS. Gunakan untuk <b>membandingkan arah & skala</b> antar iklan/niche — presisi ±20–30% ala standar industri.</div>';
  }

  // PLATFORM
  h += '<div class="subjudul">Platform Tayang</div>' + (d.platform || []).map(p =>
    '<span class="chip" style="color:#3E4B48;border-color:#D8D0C0;background:#FBFAF7;margin-right:6px;display:inline-block">' + esc(p.replace(/_/g," ")) + '</span>').join("");

  // OBJEKTIF & AUDIENS (INFERENSI)
  const inf = d.infer || {};
  if(inf.objektif){
    h += '<div class="subjudul">Objektif &amp; Audiens (Inferensi)</div><div class="grid2">' +
      sel("Objektif iklan", esc(inf.objektif)) +
      sel("Alasan", esc(inf.alasan)) +
      sel("Demografi", esc(inf.gender)) +
      sel("Rentang umur", esc(inf.umur)) + '</div>' +
      '<div class="note">🔬 <b>Inferensi, bukan data targeting Meta.</b> Meta hanya merilis targeting (interest/umur/gender) untuk iklan politik. Yang tampil di sini = perkiraan cerdas dari jejak kreatif: CTA, tautan, dan bahasa iklan. Cara kerja yang sama dipakai tool seperti AdSpy/Minea.</div>';
  }

  // PROFIL PENGIKLAN
  h += '<div class="subjudul">Profil Pengiklan</div><div class="grid2">' +
    sel("Halaman", esc(d.page_name || "—")) +
    sel("Jumlah suka", (d.page_like_count != null ? Number(d.page_like_count).toLocaleString("id-ID") + " suka" : "—")) +
    sel("Kategori halaman", esc((d.page_cats || []).join(", "))) +
    sel("Byline", esc(d.byline)) + '</div>';
  if(d.branded) h += '<div class="note">🤝 Branded content / kolab dengan: <b>' + esc(d.branded) + '</b></div>';

  // PENANDA
  const flags = [];
  if(d.ai_media) flags.push("🤖 Mengandung media buatan AI");
  if(d.sensitive) flags.push("⚠️ Konten sensitif");
  if(d.limited) flags.push("🚫 Delivery dibatasi (flag regulasi)");
  if(d.report_count) flags.push("🚩 Laporan pengguna: " + d.report_count);
  if(d.page_deleted) flags.push("🗑️ Halaman sudah dihapus");
  if(flags.length){
    h += '<div class="subjudul">Penanda</div>' + flags.map(f =>
      '<span class="chip" style="color:#B7791F;border-color:#E8D9B8;background:var(--soft);margin:0 6px 6px 0;display:inline-block">' + esc(f) + '</span>').join("");
  }

  // DATA META
  const kosong = v => (v === null || v === undefined) ? "Tidak dirilis" : v;
  h += '<div class="subjudul">Data Meta (Transparansi)</div><div class="grid2">' +
    sel("Belanja iklan" + vMeta, kosong(d.spend) + (d.currency ? " " + esc(d.currency) : "")) +
    sel("Impresi" + vMeta, kosong(d.impressions)) + sel("Jangkauan" + vMeta, kosong(d.reach)) +
    sel("Total hari aktif" + vMeta, kosong(d.active_time)) +
    sel("Negara disasar", esc((d.negara || []).join(", ") || "—")) +
    sel("Grup varian (collation)", esc(String(d.collation_id || "—"))) + '</div>' +
    '<div class="note">📌 Meta hanya merilis angka belanja/impresi untuk <b>iklan politik &amp; isu sosial</b>. Untuk iklan komersial, sinyal kekuatan iklan = <b>umur tayang, varian, funnel, format</b> — persis yang dihitung Skor Cuan.</div>';

  // IDENTITAS
  h += '<div class="subjudul">Identitas (klik untuk salin)</div><div class="idbox">' +
    '<span onclick="salin(this.dataset.u)" data-u="' + esc(String(d.archive_id||"")) + '"><span class="k">archive_id:</span> ' + esc(String(d.archive_id||"—")) + '</span>\n' +
    '<span onclick="salin(this.dataset.u)" data-u="' + esc(String(d.ad_id||"")) + '"><span class="k">ad_id:</span> ' + esc(String(d.ad_id||"—")) + '</span>\n' +
    '<span onclick="salin(this.dataset.u)" data-u="' + esc(String(d.page_id||"")) + '"><span class="k">page_id:</span> ' + esc(String(d.page_id||"—")) + '</span></div>';

  // TAUTAN
  h += '<div class="subjudul">Tautan</div>';
  if(d.link_url) h += '<div class="taut" data-u="' + esc(d.link_url) + '" onclick="salin(this.dataset.u)" title="Klik untuk salin">Tujuan: ' + esc(d.link_url) + '</div>';
  if(d.page_profile_uri) h += '<div class="taut" data-u="' + esc(d.page_profile_uri) + '" onclick="salin(this.dataset.u)" title="Klik untuk salin">Profil halaman: ' + esc(d.page_profile_uri) + '</div>';
  (d.extra_links || []).forEach(l => {
    h += '<div class="taut" data-u="' + esc(l) + '" onclick="salin(this.dataset.u)" title="Klik untuk salin">Link tambahan: ' + esc(l) + '</div>';
  });
  if(!d.link_url && !d.page_profile_uri && !(d.extra_links||[]).length) h += '<div class="hint">Tidak ada tautan terlampir.</div>';

  // AKSI
  h += '<div class="aksi">';
  if(d.link_url) h += '<button class="tombol" data-u="' + esc(d.link_url) + '" onclick="buka(this.dataset.u)">Buka Tujuan Iklan</button>' +
    '<button class="tombol2" data-u="' + esc(d.link_url) + '" onclick="salin(this.dataset.u)">Salin Link</button>';
  if(d.archive_id) h += '<button class="tombol2" data-u="' + esc("https://www.facebook.com/ads/library/?id=" + d.archive_id) + '" onclick="buka(this.dataset.u)">Buka di Ad Library</button>';
  if(d.page_id) h += '<button class="tombol" onclick="event.stopPropagation();bukaIklanPenuh(' + esc(String(x._id)) + ')">📇 Buka Halaman Detail Iklan (penuh)</button>' +
    '<button class="tombol2" onclick="event.stopPropagation();intipHalaman(\'' + esc(String(d.page_id)) + '\')">🎯 Semua iklan halaman ini (mode kompetitor)</button>';
  h += '</div>';

  // KARTU
  if((d.cards || []).length){
    h += '<div class="subjudul">Kartu Carousel (' + d.cards.length + ')</div>';
    d.cards.forEach(c => {
      h += '<div class="kartu2">' + (c.img ? imgTag(c.img) : "") +
        '<div><b>' + esc(c.title || "(tanpa judul)") + '</b>' +
        (c.harga ? '<p style="color:#B7791F;font-weight:800">' + esc(c.harga) + '</p>' : "") +
        (c.body ? '<p>' + esc(c.body) + '</p>' : "") +
        (c.link ? '<div class="taut" data-u="' + esc(c.link) + '" onclick="salin(this.dataset.u)">' + esc(c.link) + '</div>' : "") +
        '</div></div>';
    });
  }

  // MENTAH
  h += '<details class="rawbox"><summary>⚙️ Lihat data mentah (JSON lengkap dari Meta)</summary><pre>' +
    esc(JSON.stringify(d.raw, null, 2)) + '</pre></details>';
  return h;
}

function hitungChart(peta, el){
  const total = Object.values(peta).reduce((a,b) => a+b, 0) || 1;
  const entri = Object.entries(peta).sort((a,b) => b[1]-a[1]);
  document.getElementById(el).innerHTML = entri.map(([k,v]) =>
    '<div class="cbar"><span class="cl">' + esc(k) + '</span><div class="ct"><div class="cf" style="width:' +
    Math.round(v/total*100) + '%"></div></div><span class="cn">' + v + '</span></div>').join("");
}

function sortBy(k){
  if(SORT.k === k) SORT.asc = !SORT.asc;
  else { SORT.k = k; SORT.asc = (k === "halaman"); }
  render();
}

function toggleKolom(){
  const w = document.querySelector(".tabelwrap");
  w.classList.toggle("lengkap");
  const btn = document.getElementById("btn-kolom");
  btn.textContent = w.classList.contains("lengkap") ? "▦ Tampilan: Lengkap" : "▦ Tampilan: Ringkas";
}
function setFilter(f, el){
  FILTER = f;
  document.querySelectorAll(".chip2[data-f]").forEach(c => c.classList.remove("on"));
  el.classList.add("on");
  render();
}

function render(){
  const cari = (document.getElementById("cari").value || "").toLowerCase();
  const modeKompetitor = (MODE === "halaman");
  // C2: filter lanjutan
  const fUmurMin = parseInt(document.getElementById("f-umur-min").value) || 0;
  const fUmurMax = parseInt(document.getElementById("f-umur-max").value) || 0;
  const fHrgMin = parseInt(document.getElementById("f-hrg-min").value) || 0;
  const fHrgMax = parseInt(document.getElementById("f-hrg-max").value) || 0;
  const fFormat = document.getElementById("f-format").value;
  const pantau = pantauanMuat();
  const dipantau = x => pantau.some(p => p.halaman === x.halaman && p.link === x.link);
  let tersembunyi = 0;
  const statusTab = (FILTER === "MENANG" || FILTER === "NAIK" || FILTER === "UJI");
  let rows = DATA.filter(x => {
    if(statusTab){ if(x.status !== FILTER) return false; }
    else{
      if(FILTER === "Baru" && x.umur > 7) return false;
      if(FILTER !== "semua" && FILTER !== "Baru" && x.funnel !== FILTER) return false;
    }
    if(fUmurMin && x.umur < fUmurMin) return false;
    if(fUmurMax && x.umur > fUmurMax) return false;
    if(fHrgMin && !((x.harga_max || 0) >= fHrgMin)) return false;
    if(fHrgMax && x.harga_min != null && x.harga_min > fHrgMax) return false;
    if(fFormat && fFormat !== "semua" && x.format !== fFormat) return false;
    if(cari && !(x.halaman + " " + x.teks + " " + x.cta).toLowerCase().includes(cari)) return false;
    if(!modeKompetitor && SEMBUNYIKAN_LEPAS && x.relevansi && x.relevansi.label === "Lepas"){
      tersembunyi++;
      return false;
    }
    return true;
  });
  const k = SORT.k;
  rows.sort((a,b) => {
    let va = a[k], vb = b[k];
    if(typeof va === "string"){ va = va.toLowerCase(); vb = String(vb).toLowerCase(); }
    if(va < vb) return SORT.asc ? -1 : 1;
    if(va > vb) return SORT.asc ? 1 : -1;
    return 0;
  });
  document.getElementById("countinfo").innerHTML = rows.length + " tampil" +
    (tersembunyi ? " · <b style=\"color:#B5443A\">" + tersembunyi + " niche luar disembunyikan</b>" : "") +
    " dari " + DATA.length + " iklan";
  document.getElementById("jml-pantauan").textContent = pantau.length;
  // hitungan tab status (hidup)
  const ct = {MENANG: 0, NAIK: 0, UJI: 0};
  DATA.forEach(x => { if(ct[x.status] != null) ct[x.status]++; });
  const setCt = (id, v) => { const el = document.getElementById(id); if(el) el.textContent = v; };
  setCt("ct-semua", DATA.length); setCt("ct-menang", ct.MENANG); setCt("ct-naik", ct.NAIK); setCt("ct-uji", ct.UJI);
  const tb = document.getElementById("tbody");
  tb.innerHTML = "";
  rows.forEach(x => {
    const w = WARN[x.funnel] || "#6B7280";
    const id = x._id;
    const rel = x.relevansi || {nilai:100, label:"Relevan"};
    const wr = WARNA_REL[rel.label] || "#6B7280";
    const thumb = x.foto
      ? '<img src="' + x.foto + '" alt="">'
      : (x.thumb ? '<img loading="lazy" src="/img?u=' + encodeURIComponent(x.thumb) + '" alt="">' : '<div class="ph">IMG</div>');
    tb.insertAdjacentHTML("beforeend",
      '<tr class="baris-row" data-i="' + id + '">' +
      '<td style="width:64px">' + thumb + '</td>' +
      '<td><div class="nama2" title="' + esc(x.halaman) + '">' +
        '<span data-bintang="' + id + '" onclick="event.stopPropagation();togglePantauan(' + id + ')" title="Bintangi — simpan ke Pantauan (tersimpan di browser)" style="cursor:pointer;margin-right:4px;font-size:13px;color:' + (dipantau(x) ? '#B7791F' : '#CBD5E1') + '">' + (dipantau(x) ? '★' : '☆') + '</span>' +
        esc(x.halaman) +
        ' ' + statusChip(x.status) +
        (x.umur <= 3 ? ' <span class="chip" style="color:#B5443A;border-color:#E8C4BE;background:#F6E7E3">⚡ PANAS</span>' :
         x.umur <= 7 ? ' <span class="chip" style="color:#2F7D64;border-color:#BEDCD2;background:#E4F0EA">🔥 BARU</span>' : '') +
        '</div><div class="cup2">' + esc(bersih(x.teks).slice(0,60)) + '</div></td>' +
      '<td class="col-extra"><span class="chip" style="color:' + wr + ';border-color:' + wr + '33;background:' + wr + '12" title="Nilai kecocokan dgn kata kunci: ' + rel.nilai + '/100">' + esc(rel.label) + ' ' + rel.nilai + '</span></td>' +
      '<td><span class="skor2">' + x.skor + '</span></td>' +
      '<td><span class="chip" style="color:#B7791F;border-color:#E8D9B8;background:var(--soft);font-weight:900">' + (x.jual != null ? x.jual : '—') + '</span></td>' +
      '<td><span class="hari2">' + x.umur + 'h</span></td>' +
      '<td style="white-space:nowrap;font-weight:700">' + (x.harga_min ? (x.harga_min === x.harga_max ? fmtHarga(x.harga_min) : fmtHarga(x.harga_min) + '–' + fmtHarga(x.harga_max)) : '—') + '</td>' +
      '<td style="white-space:nowrap;color:#2F7D64;font-weight:800">' + (x.est_min ? fmtHarga(x.est_min) + '–' + fmtHarga(x.est_max) : '—') + '</td>' +
      '<td class="col-extra">' + esc(x.tgl) + '</td><td class="col-extra">' + esc(x.akhir) + '</td>' +
      '<td class="col-extra">' + esc(x.format) + '</td>' +
      '<td><span class="chip" style="color:' + w + ';border-color:' + w + '33;background:' + w + '12">' + esc(x.funnel) + '</span></td>' +
      '<td class="col-extra">' + esc(x.cta || "—") + '</td><td class="col-extra">' + esc(String(x.varian != null ? x.varian : "—")) + '</td></tr>' +
      '<tr class="detail-row" data-d="' + id + '" style="display:none"><td colspan="14"><div class="detail" data-fill="' + id + '"></div></td></tr>');
    const br = tb.querySelector('tr[data-i="' + id + '"]');
    br.addEventListener("click", () => {
      const dr = tb.querySelector('tr[data-d="' + id + '"]');
      const bukaSekarang = dr.style.display === "none";
      dr.style.display = bukaSekarang ? "" : "none";
      br.classList.toggle("open", bukaSekarang);
      const wadah = dr.querySelector(".detail");
      if(bukaSekarang && !wadah.dataset.filled){
        wadah.dataset.filled = "1";
        wadah.innerHTML = '<div class="bar-detail"><span class="jd">Detail Lengkap — ' + esc(DATA[id].halaman) + '</span>' +
          '<button class="tombol2" onclick="event.stopPropagation();rakitNaskah(' + id + ')">🧬 Rakit Naskah Video</button>' +
          '<button class="tombol2" onclick="event.stopPropagation();tutupDetail(' + id + ')">✕ Tutup Detail</button></div>' +
          detailHTML(DATA[id]);
      }
    });
  });
}

function statusChip(st){
  st = st || "UJI";
  const w = st === "MENANG" ? ["#2F7D64","#BEDCD2","#E4F0EA"] :
            st === "NAIK"  ? ["#0F5B52","#B7D8D0","#DCEDE8"] : ["#6E7C78","#DDD6CA","#F1F5F9"];
  return '<span class="chip" style="color:' + w[0] + ';border-color:' + w[1] + ';background:' + w[2] + ';font-weight:800" title="MENANG = sinyal kuat (skor tinggi + lama tayang) · NAIK = kandidat naik kelas · UJI = sinyal awal">' + st + '</span>';
}
function eksporCSV(){
  if(!DATA.length){ alert("Belum ada hasil untuk diekspor. Jalankan scan dulu."); return; }
  const kol = ["status","halaman","format","funnel","relevansi_label","relevansi_nilai","skor_cuan","skor_jual","umur_hari","varian","mulai","akhir","harga_min","harga_max","est_belanja_bln_min","est_belanja_bln_max","cta","platform","link","teks_ringkas"];
  const isi = [kol.join(",")].concat(DATA.map(x => {
    const d = x.detail || {};
    const v = [
      x.status || "", x.halaman || "", x.format || "", x.funnel || "",
      (x.relevansi && x.relevansi.label) || "", x.rel_nilai != null ? x.rel_nilai : "",
      x.skor != null ? x.skor : "", x.jual != null ? x.jual : "",
      x.umur != null ? x.umur : "", x.varian != null ? x.varian : "",
      x.tgl || "", x.akhir || "",
      x.harga_min != null ? x.harga_min : "", x.harga_max != null ? x.harga_max : "",
      x.est_min != null ? x.est_min : "", x.est_max != null ? x.est_max : "",
      x.cta || "", x.platform || "", x.link || "",
      bersih(x.teks || "").slice(0, 160)
    ];
    return v.map(s => '"' + String(s).replace(/"/g, '""') + '"').join(",");
  })).join("\n");
  const b = new Blob(["\ufeff" + isi], {type: "text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(b);
  a.download = "intai_" + ((DATA[0] && DATA[0].halaman) ? "scan" : "scan") + "_" + new Date().toISOString().slice(0,10) + ".csv";
  document.body.appendChild(a); a.click(); a.remove();
  alert("CSV " + DATA.length + " baris sudah diunduh.");
}
function rakitNaskah(id){
  const x = DATA[id];
  if(!x) return;
  const teks = bersih(x.teks || "");
  const kal = teks.split(/[.!?\n]/).map(s => s.trim()).filter(s => s.length > 8);
  const hk = (HOOKS || []).slice(0, 3).map(h => h.hook);
  const hookAsli = (kal[0] || "").slice(0, 90);
  const d = x.detail || {};
  const bukti = [];
  if(x.varian) bukti.push(x.varian + " varian aktif");
  if(x.jual != null) bukti.push("skor jual " + x.jual + "/100");
  if(x.umur) bukti.push("iklan hidup " + x.umur + " hari");
  const hrg = x.harga_min ? fmtHarga(x.harga_min) + (x.harga_max && x.harga_max !== x.harga_min ? "–" + fmtHarga(x.harga_max) : "") : "";
  const vo = [
    "0–3 dtk · MASALAH: " + (kal[0] ? "angkat ulang pola: “" + kal[0].slice(0, 80) + "”" : "keluhan paling sering di niche ini"),
    "3–10 dtk · BUKTI: " + (bukti.length ? bukti.join(" · ") : "angka spesifik (terjual / rating / lama tayang)"),
    "10–20 dtk · SOLUSI + OFFER: " + (hrg ? "tampilkan harga " + hrg + " + bonus terbatas" : "tawaran utama + bonus terbatas"),
    "20–30 dtk · CTA: " + (x.cta || "Shop Now / Chat WhatsApp")
  ];
  const visual = String(x.format).toUpperCase() === "VIDEO"
    ? ["1 detik pertama: close-up produk + teks masalah di layar", "Tengah: demo pakai / before-after", "Akhir: frame harga + logo + CTA"]
    : ["Visual utama: produk + headline 3–5 kata di gambar", "Carousel: slide 1 masalah, slide 2 bukti, slide 3 offer", "Akhir: frame harga + CTA"];
  const label = "Status " + (x.status || "UJI") + " · Skor Cuan " + x.skor + "/100 · Skor Jual " + (x.jual != null ? x.jual : "—") + " · Umur " + x.umur + "h · " + x.format + " · " + x.funnel + (hrg ? " · Harga " + hrg : "");
  const briefTeks =
    "PERAKIT NASKAH VIDEO — " + x.halaman + "\n" +
    "1. HOOK (3 sudut):\n" + hk.map((h,i) => "   " + (i+1) + ". " + h).join("\n") +
    (hookAsli ? "\n   4. Pola kalimat pembuka iklan ini: \"" + hookAsli + "\"" : "") +
    "\n2. VOICE OVER 20–30 detik:\n   " + vo.join("\n   ") +
    "\n3. VISUAL:\n   " + visual.join("\n   ") +
    "\n4. ANGLE & CTA: " + ((d.angle || d.raw && d.raw.angle) || "—") + " · CTA: " + (x.cta || "—") +
    "\n5. BAHAN KLAIM: " + label +
    "\n(🟠 kerangka brief dari data — untuk memandu tim kreatif, bukan naskah AI jadi)";
  const html =
    '<div class="bar-detail"><span class="jd">🧬 Perakit Naskah Video — ' + esc(x.halaman) + '</span>' +
    '<button class="tombol2" onclick="this.closest(\'.brief-ov\').remove()">✕ Tutup (Esc)</button></div>' +
    '<div class="note" style="margin:8px 0"><b>🟠 Pita Model:</b> kerangka brief yang dirakit otomatis dari DATA iklan ini + pola hook juara niche — panduan untuk tim kreatif, bukan naskah jadi dari AI generatif.</div>' +
    '<div class="sel"><div class="k">1 · Hook — 3 sudut</div><div class="v" style="white-space:pre-line">' +
      esc(hk.map((h,i) => (i+1) + '. "' + h + '"').join("\n") + (hookAsli ? '\n4. Pola kalimat pembuka iklan ini: "' + hookAsli + '"' : "")) + '</div></div>' +
    '<div class="sel"><div class="k">2 · Voice Over 20–30 detik</div><div class="v" style="white-space:pre-line">' + esc(vo.join("\n")) + '</div></div>' +
    '<div class="sel"><div class="k">3 · Visual</div><div class="v" style="white-space:pre-line">' + esc(visual.join("\n")) + '</div></div>' +
    '<div class="sel"><div class="k">4 · Angle & CTA</div><div class="v">' + esc((d.angle || "—")) + ' · CTA: ' + esc(x.cta || "—") + '</div></div>' +
    '<div class="sel"><div class="k">5 · Bahan Klaim (data pendukung)</div><div class="v">' + esc(label) + '</div></div>' +
    '<button class="tombol2" style="margin:10px 0" data-t="' + esc(briefTeks) + '" onclick="salin(this.dataset.t)">📋 Salin brief</button>';
  let ov = document.querySelector(".brief-ov");
  if(ov) ov.remove();
  ov = document.createElement("div");
  ov.className = "brief-ov";
  ov.style.cssText = "position:fixed;inset:0;z-index:520;background:rgba(15,23,42,.45);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;padding:24px";
  ov.addEventListener("click", e => { if(e.target === ov) ov.remove(); });
  ov.innerHTML = '<div class="modal" style="max-width:580px;width:100%;max-height:82vh;overflow:auto">' + html + '</div>';
  document.body.appendChild(ov);
}
function pantauanMuat(){ try{ return JSON.parse(localStorage.getItem("intai_pantauan") || "[]"); }catch(e){ return []; } }
function pantauanSimpan(list){ try{ localStorage.setItem("intai_pantauan", JSON.stringify(list)); }catch(e){} }
function togglePantauan(id){
  const x = DATA[id]; if(!x) return;
  const list = pantauanMuat();
  const idx = list.findIndex(p => p.halaman === x.halaman && p.link === x.link);
  if(idx >= 0){ list.splice(idx, 1); }
  else{
    list.push({
      halaman: x.halaman, teks: bersih(x.teks || "").slice(0, 120),
      status: x.status || "UJI", skor: x.skor, jual: x.jual, umur: x.umur,
      harga_min: x.harga_min, harga_max: x.harga_max,
      est_min: x.est_min, est_max: x.est_max,
      format: x.format, funnel: x.funnel, cta: x.cta, link: x.link, tgl: x.tgl,
      disimpan: new Date().toISOString().slice(0, 10)
    });
  }
  pantauanSimpan(list);
  render();
}
function hapusPantauan(i){
  const list = pantauanMuat();
  list.splice(i, 1);
  pantauanSimpan(list);
  lihatPantauan();
  render();
}
function eksporPantauan(){
  const list = pantauanMuat();
  if(!list.length){ alert("Belum ada produk di Pantauan."); return; }
  const kol = ["halaman","status","skor_cuan","skor_jual","umur_hari","harga_min","harga_max","est_min","est_max","format","funnel","cta","tgl_mulai","disimpan","link","teks"];
  const isi = [kol.join(",")].concat(list.map(p => kol.map(k => '"' + String(p[k] != null ? p[k] : "").replace(/"/g, '""') + '"').join(","))).join("\n");
  const b = new Blob(["\ufeff" + isi], {type: "text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(b);
  a.download = "intai_pantauan_" + new Date().toISOString().slice(0, 10) + ".csv";
  document.body.appendChild(a); a.click(); a.remove();
}
function lihatPantauan(){
  const list = pantauanMuat();
  const isi = list.length
    ? '<table style="min-width:0"><thead><tr><th>Produk</th><th>Status</th><th>Skor</th><th>Harga</th><th>Umur</th><th style="width:70px">Aksi</th></tr></thead><tbody>' +
      list.map((p, i) =>
        '<tr><td><div class="nama2">' + esc(p.halaman) + '</div><div class="cup2">' + esc(p.teks || "") + '</div></td>' +
        '<td>' + statusChip(p.status) + '</td>' +
        '<td>' + (p.skor != null ? p.skor : "—") + ' / ' + (p.jual != null ? p.jual : "—") + '</td>' +
        '<td style="white-space:nowrap;font-weight:650">' + (p.harga_min ? fmtHarga(p.harga_min) + (p.harga_max && p.harga_max !== p.harga_min ? "–" + fmtHarga(p.harga_max) : "") : "—") + '</td>' +
        '<td>' + (p.umur != null ? p.umur + "h" : "—") + '</td>' +
        '<td><button class="tombol2" onclick="hapusPantauan(' + i + ')">✕</button></td></tr>').join("") +
      '</tbody></table>' +
      '<div class="note" style="margin-top:8px">⭐ Tersimpan di browser ini (localStorage) — aman untuk kerja harian. Ingin dipakai tim? Ekspor CSV lalu sebarkan.</div>'
    : '<div class="note">Belum ada produk dibintangi. Klik <b>☆</b> di kolom nama produk untuk menyimpannya ke Pantauan.</div>';
  let ov = document.querySelector(".brief-ov");
  if(ov) ov.remove();
  ov = document.createElement("div");
  ov.className = "brief-ov";
  ov.style.cssText = "position:fixed;inset:0;z-index:520;background:rgba(15,23,42,.45);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;padding:24px";
  ov.addEventListener("click", e => { if(e.target === ov) ov.remove(); });
  ov.innerHTML = '<div class="modal" style="max-width:680px;width:100%;max-height:82vh;overflow:auto">' +
    '<div class="bar-detail"><span class="jd">⭐ Pantauan — ' + list.length + ' produk</span>' +
    '<button class="tombol2" onclick="this.closest(\'.brief-ov\').remove()">✕ Tutup (Esc)</button></div>' +
    '<div style="margin:8px 0"><button class="tombol2" onclick="eksporPantauan()">⬇ Ekspor CSV Pantauan</button></div>' +
    isi + '</div>';
  document.body.appendChild(ov);
}
function toggleFilterBar(){
  const el = document.getElementById("filterbar");
  el.style.display = el.style.display === "none" ? "flex" : "none";
}
function resetFilterLanjutan(){
  ["f-umur-min","f-umur-max","f-hrg-min","f-hrg-max"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("f-format").value = "semua";
  render();
}
function tutupDetail(id){
  const dr = document.querySelector('tr[data-d="' + id + '"]');
  const br = document.querySelector('tr[data-i="' + id + '"]');
  if(dr) dr.style.display = "none";
  if(br) br.classList.remove("open");
}
function toggleTop(i){
  const p = (window.TP || [])[i];
  if(!p) return;
  const isi =
    '<div class="resep">🧾 <b>Resep jualan:</b> Jual <b>' + esc((p.nama || "").slice(0, 45)) + '</b> — kreatif ' + esc(p.format) + ' · harga ' + (p.harga_min ? fmtHarga(p.harga_min) + '–' + fmtHarga(p.harga_max) : '—') + ' · funnel ' + esc(p.funnel) + ' (' + esc(p.cta || "—") + ') · ' + (p.harga_min && p.unit ? 'est. ' + p.unit[0] + '–' + p.unit[1] + ' unit/bln · ' : '') + 'iklan semodel yang cuan bertahan ' + p.umur + '+ hari</div>' +
    '<div class="subjudul">Iklan dalam grup ini (' + p.ads.length + ') — klik untuk bedah detail</div>' +
    (p.ads || []).map(a => '<div class="tp-ad" onclick="bukaIklanDariModal(' + a.idx + ')">▶ Iklan · ' + esc(a.format) + ' · ' + a.umur + ' hari aktif · skor ' + a.skor + ' · skor jual ' + (a.skor_jual || 0) + '</div>').join("");
  let ov = document.querySelector(".brief-ov");
  if(ov) ov.remove();
  ov = document.createElement("div");
  ov.className = "brief-ov";
  ov.style.cssText = "position:fixed;inset:0;z-index:520;background:rgba(34,48,46,.45);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;padding:24px";
  ov.addEventListener("click", e => { if(e.target === ov) ov.remove(); });
  ov.innerHTML = '<div class="modal" style="max-width:560px;width:100%;max-height:82vh;overflow:auto">' +
    '<div class="bar-detail"><span class="jd">🧾 ' + esc((p.nama || "").slice(0, 50)) + '</span>' +
    '<button class="tombol2" onclick="this.closest(\'.brief-ov\').remove()">✕ Tutup (Esc)</button></div>' +
    isi + '</div>';
  document.body.appendChild(ov);
}
function bukaIklanDariModal(idx){
  const ov = document.querySelector(".brief-ov");
  if(ov) ov.remove();
  bukaIklan(idx);
}
function bukaIklan(idx){ bukaIklanPenuh(idx); }
function bukaIklanPenuh(idx){
  const x = DATA[idx];
  if(!x) return;
  const wadah = document.getElementById("isi-halaman-iklan");
  wadah.innerHTML =
    '<div class="bar-detail"><span class="jd">📇 Halaman Detail Iklan — ' + esc(x.halaman) + '</span>' +
    '<button class="tombol2" onclick="tutupHalamanIklan()">← Kembali (Esc)</button></div>' +
    '<div style="margin:10px 0"><span class="chip" style="color:#3E4B48;border-color:#D8D0C0;background:#FBFAF7">Skor Cuan ' + x.skor + '</span> ' +
    '<span class="chip" style="color:#B7791F;border-color:#E8D9B8;background:var(--soft)">Skor Jual ' + (x.jual || 0) + '</span> ' +
    '<span class="chip" style="color:#3E4B48;border-color:#D8D0C0;background:#FBFAF7">' + esc(x.format) + ' · ' + esc(x.funnel) + ' · ' + x.umur + ' hari</span></div>' +
    detailHTML(x);
  document.getElementById("halaman-iklan").style.display = "block";
  document.body.style.overflow = "hidden";
}
function tutupHalamanIklan(){
  document.getElementById("halaman-iklan").style.display = "none";
  document.body.style.overflow = "";
}

async function scan(){
  const q = document.getElementById("q").value.trim();
  const err = document.getElementById("error");
  const btn = document.getElementById("go");
  if(!q){ err.textContent = "Isi " + (MODE === "halaman" ? "ID halaman" : "kata kunci") + " dulu."; err.style.display="block"; return; }
  if(MODE === "halaman" && !/^\d+$/.test(q)){ err.textContent = "ID halaman harus angka (contoh: 454661924401136). Ambil dari tombol di detail iklan."; err.style.display="block"; return; }
  err.style.display = "none";
  btn.disabled = true;
  const btnTeks = btn.textContent;
  btn.textContent = "MEMINDAI...";
  document.getElementById("loading").classList.add("on");
  document.getElementById("dashboard").style.display = "none";
  document.getElementById("status").textContent = "Memindai...";
  // B1: tahap loading informatif — cerminan alur mesin di belakang
  const tahapan = [
    "① Menghubungi Meta Ad Library…",
    "② Membongkar struktur iklan…",
    "③ Menggali harga LP & Marketplace…",
    "④ Menyusun rekap, ranking & estimasi…"
  ];
  const elTahap = document.getElementById("tahap-loading");
  let iTahap = 0;
  const timerTahap = setInterval(() => {
    iTahap = Math.min(iTahap + 1, tahapan.length - 1);
    if(elTahap) elTahap.textContent = tahapan[iTahap] + " (±30 detik)";
    document.getElementById("status").textContent = tahapan[iTahap];
  }, 4500);
  const t0 = Date.now();
  try{
    const r = await fetch("/scan?q=" + encodeURIComponent(q) + "&mode=" + MODE);
    const d = await r.json();
    const det = ((Date.now()-t0)/1000).toFixed(0);
    clearInterval(timerTahap);
    if(!d.ok){
      const pesan = d.error || "tidak diketahui";
      const saran = /rate|1675004|terlalu/i.test(pesan) ? " Saran: tunggu ±1 menit, lalu scan lagi (Meta membatasi bertubi-tubi)." :
        /ID halaman/i.test(pesan) ? " Saran: ID halaman harus angka — ambil dari tombol di detail iklan." :
        /tidak merespons|timeout/i.test(pesan) ? " Saran: cek koneksi, atau coba lagi sebentar lagi." : " Saran: coba lagi — kalau terus gagal, ganti kata kunci dulu.";
      err.innerHTML = '<b>Gagal memindai:</b> ' + esc(pesan) + '\n<span style="color:#6E7C78">' + saran + '</span>';
      err.style.display = "block";
      document.getElementById("status").textContent = "Gagal";
      return;
    }
    document.getElementById("status").textContent = "Selesai dalam " + det + " detik";
    const mw = document.getElementById("meta-waktu");
    if(mw) mw.textContent = "Terakhir: " + det + " dtk · " + d.jumlah + " iklan";
    const mm = document.getElementById("meta-mode");
    if(mm) mm.textContent = "Mode: " + (MODE === "halaman" ? "Intip Kompetitor" : "Kata Kunci");
    DATA = d.rows.map(x => Object.assign({}, x, {
      akhir: (x.detail && x.detail.berakhir) || "—",
      cta: (x.detail && x.detail.cta_text) || "",
      mulai: (x.detail && x.detail.mulai_epoch) || 0,
    }));
    DATA.forEach((x, i) => { x._id = i; });
    HOOKS = (d.angle && d.angle.top_hooks) || [];
    // KPI
    const unik = new Set(DATA.map(x => x.halaman)).size;
    const video = DATA.filter(x => String(x.format).toUpperCase() === "VIDEO").length;
    const ctwa = DATA.filter(x => x.funnel === "CTWA/WA").length;
    const ph = d.peta_harga || {jumlah_titik: 0};
    const wkt = d.waktu || {per_bulan:{}, baru_7hari:0, panas_3hari:0};
    const en = d.ekonomi_niche || {bulanan_min:0, bulanan_max:0, median_bulanan:0};
    document.getElementById("kpis").innerHTML =
      '<div class="kpi"><div class="k">Total Iklan</div><div class="v">' + d.jumlah + '</div><div class="s">' + esc(d.kata) + '</div></div>' +
      '<div class="kpi"><div class="k">Pengiklan Unik</div><div class="v">' + unik + '</div><div class="s">halaman berbeda</div></div>' +
      '<div class="kpi"><div class="k">Rata2 Umur</div><div class="v">' + d.rata2 + '</div><div class="s">hari tayang</div></div>' +
      '<div class="kpi"><div class="k">Tertua</div><div class="v">' + d.tertua.hari + 'h</div><div class="s">' + esc(d.tertua.nama) + '</div></div>' +
      '<div class="kpi"><div class="k">Rentang Harga</div><div class="v">' + (ph.jumlah_titik ? fmtHarga(ph.termurah.harga) + '–' + fmtHarga(ph.termahal.harga) : '—') + '</div><div class="s">' + ph.jumlah_titik + ' titik harga · median ' + (ph.median ? fmtHarga(ph.median) : '—') + '</div></div>' +
      '<div class="kpi" title="Estimasi TOTAL belanja iklan semua pengiklan di niche ini per bulan — MODEL 🟠, bukan data Meta"><div class="k">Est. Belanja Iklan Niche</div><div class="v" style="color:#2F7D64">' + (en.bulanan_max ? fmtHarga(en.bulanan_min) + '–' + fmtHarga(en.bulanan_max) : '—') + '</div><div class="s">per bulan (model) · median ' + (en.median_bulanan ? fmtHarga(en.median_bulanan) : '—') + '</div></div>' +
      '<div class="kpi"><div class="k">Baru ≤7 Hari</div><div class="v" style="color:' + (wkt.baru_7hari ? '#B5443A' : 'inherit') + '">' + wkt.baru_7hari + '</div><div class="s">⚡ ' + wkt.panas_3hari + ' mulai ≤3 hari</div></div>' +
      '<div class="kpi"><div class="k">Video</div><div class="v">' + Math.round(video/Math.max(d.jumlah,1)*100) + '%</div><div class="s">' + video + ' iklan</div></div>' +
      '<div class="kpi" title="CTWA = Click-to-WhatsApp — iklan yang tujuannya chat WA (penjualan via chat)"><div class="k">CTWA/WA</div><div class="v">' + Math.round(ctwa/Math.max(d.jumlah,1)*100) + '%</div><div class="s">' + ctwa + ' iklan</div></div>';
    const pf = {}, pm = {};
    DATA.forEach(x => { pf[x.funnel] = (pf[x.funnel]||0)+1; pm[x.format] = (pm[x.format]||0)+1; });
    hitungChart(pf, "ch-funnel");
    hitungChart(pm, "ch-format");
    hitungChart(wkt.per_bulan || {}, "ch-bulan");
    hitungChart((d.angle && d.angle.cta) || {}, "ch-cta");
    const hooks = (d.angle && d.angle.top_hooks) || [];
    document.getElementById("ch-hook").innerHTML = hooks.length
      ? hooks.map(hk =>
          '<div style="display:flex;gap:10px;align-items:baseline;padding:7px 2px;border-bottom:1px dashed var(--line);flex-wrap:wrap">' +
          '<span style="flex:1;min-width:220px;font-size:12.5px;color:#22302E">“' + esc(hk.hook) + '”</span>' +
          '<span style="font-size:10.5px;color:var(--muted);white-space:nowrap">' + esc(hk.halaman) + ' · ' + hk.umur + 'h · skor ' + hk.skor + '</span></div>').join("") +
        ((d.angle && d.angle.rata2_teks_awet) ? '<div class="hint" style="margin-top:9px">📏 Rata2 panjang teks: iklan awet (≥60h) = <b>' + d.angle.rata2_teks_awet + ' karakter</b> · iklan baru (≤14h) = <b>' + d.angle.rata2_teks_baru + ' karakter</b></div>' : '')
      : '<div class="hint">Tidak ada teks untuk dirangkum.</div>';
    const tp = d.top_produk || [], acn = d.acuan || {};
    window.TP = tp;
    document.getElementById("top10").innerHTML = tp.length
      ? '<div class="mp-grid">' + tp.map((p, i) => {
          const img = p.foto
            ? (String(p.foto).startsWith("data:") ? '<img src="' + p.foto + '" alt="">' : '<img loading="lazy" src="/img?u=' + encodeURIComponent(p.foto) + '" alt="">')
            : '<div class="ph">IMG</div>';
          const harga = p.harga_min
            ? fmtHarga(p.harga_min) + (p.harga_max && p.harga_max !== p.harga_min ? '<small> – ' + fmtHarga(p.harga_max) + '</small>' : '')
            : '<span style="font-size:11px;font-weight:700;color:var(--faint)">Harga belum terbaca</span>';
          const jual = p.terjual
            ? '📦 ' + Number(p.terjual).toLocaleString('id-ID') + ' terjual'
            : (p.unit ? '📦 est. ' + p.unit[0] + '–' + p.unit[1] + ' unit/bln' : '📦 —');
          const medali = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : '#' + (i + 1);
          return '<div class="mp-kartu">' +
            '<div class="mp-img">' + img +
              '<span class="mp-rank' + (i < 3 ? ' top' : '') + '">' + medali + '</span>' +
              '<span class="mp-skor" title="Skor Jual 0–100 — makin tinggi makin layak jadi acuan">★ ' + p.skor_jual + '</span>' +
            '</div>' +
            '<div class="mp-isi">' +
              '<div class="mp-nama">' + esc((p.nama || "").slice(0, 52)) + '</div>' +
              '<div class="mp-toko" title="' + esc(p.halaman) + '">' + esc(p.halaman) + '</div>' +
              '<div class="mp-harga">' + harga + '</div>' +
              '<div class="mp-meta">' + esc(jual) + ' · ⏱ ' + p.umur + ' hari aktif</div>' +
              '<div class="mp-foot">' +
                '<span class="mp-jual">' + esc(p.label || "") + '</span>' +
                '<button class="mp-tombol" onclick="toggleTop(' + i + ')">Resep &amp; Iklan (' + p.ads.length + ')</button>' +
              '</div>' +
            '</div>' +
          '</div>';
        }).join("") + '</div>'
      : '<div class="hint">Tidak ada produk terdeteksi.</div>';
    document.getElementById("acuan").innerHTML = (acn.funnel && acn.funnel !== "—")
      ? '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:11.5px;color:#43484F">' +
        '<b style="color:#B7791F">📐 Ringkasan acuan niche:</b>' +
        '<span class="chip" style="color:#3E4B48;border-color:#D8D0C0;background:#FBFAF7">Funnel dominan: ' + esc(acn.funnel) + '</span>' +
        '<span class="chip" style="color:#3E4B48;border-color:#D8D0C0;background:#FBFAF7">Format: ' + esc(acn.format) + '</span>' +
        '<span class="chip" style="color:#3E4B48;border-color:#D8D0C0;background:#FBFAF7">CTA: ' + esc(acn.cta) + '</span>' +
        '<span class="chip" style="color:#3E4B48;border-color:#D8D0C0;background:#FBFAF7">Harga median: ' + (acn.harga_median ? fmtHarga(acn.harga_median) : '—') + '</span>' +
        '<span class="chip" style="color:#3E4B48;border-color:#D8D0C0;background:#FBFAF7">Budget test: ' + fmtHarga(acn.budget_test[0]) + '–' + fmtHarga(acn.budget_test[1]) + '/hari</span>' +
        '</div>'
      : '';
    const kal = [];
    kal.push('Niche <b>' + esc(d.kata) + '</b>: <b>' + d.jumlah + '</b> iklan aktif dari <b>' + unik + '</b> pengiklan' + (wkt.baru_7hari ? ' — <b style="color:#B5443A">' + wkt.baru_7hari + ' di antaranya baru mulai ≤7 hari</b> (sinyal yang layak dipantau)' : '') + '.');
    if(tp[0]) kal.push('Pemenang saat ini: <b>' + esc(tp[0].halaman) + '</b> (skor jual ' + tp[0].skor_jual + ') — ' + tp[0].umur + ' hari bertahan di iklan' + (tp[0].terjual ? ', <b>' + Number(tp[0].terjual).toLocaleString('id-ID') + ' terjual</b>' : '') + '.');
    if(acn.harga_median) kal.push('Medan dominan: <b>' + esc(acn.funnel) + '</b> (' + esc(acn.format) + ', CTA: ' + esc(acn.cta) + ') · harga median <b>' + fmtHarga(acn.harga_median) + '</b> · budget test ' + fmtHarga(acn.budget_test[0]) + '–' + fmtHarga(acn.budget_test[1]) + '/hari.');
    if(en.bulanan_max) kal.push('Estimasi belanja iklan niche ini: <b>' + fmtHarga(en.bulanan_min) + '–' + fmtHarga(en.bulanan_max) + '/bulan</b> (model 🟠, ±20–30%).');
    document.getElementById("isi-ringkasan").innerHTML = kal.join(' ');
    document.getElementById("dashboard").style.display = "";
    render();
  }catch(e){
    err.innerHTML = '<b>Scan terputus:</b> ' + esc(String(e && e.message || e).slice(0, 120)) + '\n<span style="color:#6E7C78">Saran: cek koneksi / server INTAI masih jalan, lalu tekan SCAN lagi.</span>';
    err.style.display = "block";
    document.getElementById("status").textContent = "Error";
  }finally{
    clearInterval(timerTahap);
    btn.disabled = false;
    btn.textContent = "SCAN";
    document.getElementById("loading").classList.remove("on");
  }
}
document.getElementById("q").addEventListener("keydown", e => { if(e.key === "Enter") scan(); });

/* deep-link dari dashboard: /meta?q=kata[&mode=cari|halaman] — kata "halaman <id>" juga dikenali */
(function(){
  const p = new URLSearchParams(location.search);
  let q = (p.get("q") || "").trim();
  let m = (p.get("mode") || "").trim();
  if(!q) return;
  const mHal = /^halaman\s+(\d+)$/i.exec(q);
  if(mHal){ m = "halaman"; q = mHal[1]; }
  if(m === "halaman"){
    setMode("halaman");
    document.getElementById("q").value = q;
    if(/^\d+$/.test(q)) scan();
  } else {
    setMode("cari");
    document.getElementById("q").value = q;
    scan();
  }
})();
</script>
</body></html>"""


def fmt_epoch(ep):
    if not ep:
        return "—", 0
    try:
        d = datetime.fromtimestamp(int(ep), tz=timezone.utc)
        return f"{d.day} {BULAN[d.month - 1]} {d.year}", int(ep)
    except Exception:
        return "—", 0


def ambil_halaman_url(s, url):
    """GET halaman Ad Library (URL bebas) + lewati challenge anti-bot."""
    r = s.get(url, timeout=30)
    for _ in range(6):
        if r.status_code == 200 and len(r.content) > 50000:
            return r.text
        if r.status_code == 403 and "__rd_verify" in r.text:
            mm = PAT_VERIFY.search(r.text)
            if mm:
                s.post("https://www.facebook.com" + mm.group(1), timeout=25)
        time.sleep(2)
        r = s.get(url, timeout=30)
    return r.text


def teks(snap, kunci):
    v = snap.get(kunci) or ""
    if isinstance(v, dict):
        v = v.get("text") or v.get("title") or ""
    return v if isinstance(v, str) else str(v)


TPL_RE = re.compile(r"\{\{[^{}]*\}\}")


def ada_tpl(v):
    return "{{" in str(v or "")


def bersih_tpl(v):
    """v1.4.2: buang token template iklan katalog Meta ({{product.brand}}, {{product.name}}, …) dari teks
    yang DITAMPILKAN. Teks asli Meta tetap utuh di 'raw'. Kosong bila sisanya tak bermakna."""
    t = TPL_RE.sub(" ", str(v or ""))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t).strip()
    t = re.sub(r"^[\s\-–—|:·,.]+|[\s\-–—|:·,]+$", "", t)
    return t if re.search(r"\w", t) else ""


def _bersih_baris_arsip(r):
    """Baris arsip lama (sebelum v1.4.2) bisa masih berisi {{…}} → rapikan saat dibaca + tandai katalog."""
    if isinstance(r, dict) and (ada_tpl(r.get("hook")) or ada_tpl(r.get("teks"))):
        for k in ("hook", "teks"):
            if k in r:
                r[k] = bersih_tpl(r.get(k)) or None
        r["katalog"] = True
    return r


def inferensi(snap, o):
    """Perkirakan objektif & demografi dari jejak kreatif (CTA, tautan, bahasa).
    INFERENSI, bukan data targeting Meta (hanya dirilis utk iklan politik)."""
    gabung = (" ".join([teks(snap, "title"), teks(snap, "body"), teks(snap, "caption"),
                        teks(snap, "link_description"), str(snap.get("cta_text") or "")])
              ).lower()
    cta = str(snap.get("cta_type") or "").upper()
    link = str(snap.get("link_url") or "").lower()

    objektif, alasan = "Engagement / Brand Awareness", "tidak ada sinyal kuat — kemungkinan iklan jangkauan/engagement"
    if ("wa.me" in link or "api.whatsapp.com" in link or "whatsapp" in link
            or "MESSAGE" in cta or "WHATSAPP" in cta or "CONTACT" in cta):
        objektif, alasan = "Pesan / Messages (CTWA)", "CTA/tautan mengarah ke WhatsApp"
    elif any(m in link for m in ["shopee", "tokopedia", "lazada", "tiktok.com/shop",
                                 "bukalapak", "blibli", "shope.ee", "s.shopee"]):
        objektif, alasan = "Penjualan di Marketplace", "tautan langsung ke halaman produk marketplace"
    elif any(c in cta for c in ["SHOP", "BUY", "ORDER", "CART", "DONATE"]):
        objektif, alasan = "Penjualan (Sales/Conversions)", "CTA belanja (Shop Now/Buy Now/dll)"
    elif any(c in cta for c in ["SIGN_UP", "APPLY", "QUOTE", "SUBSCRIBE"]):
        objektif, alasan = "Leads / Prospek", "CTA pengisian data (Sign Up/Get Quote/dll)"
    elif any(c in cta for c in ["LEARN_MORE", "WATCH", "INSTALL", "DOWNLOAD"]):
        objektif, alasan = "Traffic / Aplikasi", "CTA kunjungan atau unduh"

    wan = ["bunda", "kak ", "sista", "cewek", "wanita", "ibu ", "mama", "ladies",
           "girls", "kakak", "mba", "mbak", "sweety", "cantik"]
    pri = ["mas ", "bro", "brow", "cowok", "pria", "bapak", "gentlemen", "kang", "ganteng"]
    gw = sum(gabung.count(w) for w in wan)
    gp = sum(gabung.count(w) for w in pri)
    if gw > gp and gw > 0:
        gender = f"Indikasi WANITA ({gw} sinyal bahasa)"
    elif gp > gw and gp > 0:
        gender = f"Indikasi PRIA ({gp} sinyal bahasa)"
    elif gw or gp:
        gender = "Campuran (sinyal pria & wanita seimbang)"
    else:
        gender = "Tidak terdeteksi dari teks"

    umur = "Tidak terdeteksi dari teks"
    th = re.findall(r"(\d{2})\s*(?:tahun|thn|th\b)", gabung)
    if th:
        nums = sorted(int(x) for x in th)
        umur = f"Sinyal angka {nums[0]}–{nums[-1]} tahun muncul di teks"
    elif any(k in gabung for k in ["remaja", "anak muda", "pelajar", "mahasiswa", "gen z"]):
        umur = "Indikasi muda (remaja / dewasa muda)"
    elif any(k in gabung for k in ["lansia", "usia lanjut", "ibu hamil", "menyusui",
                                   "anak-anak", "batita", "balita"]):
        umur = "Indikasi segmen khusus (ibu hamil/anak/lansia dsb)"

    return {"objektif": objektif, "alasan": alasan, "gender": gender, "umur": umur}


# Keluarga kata senada (ID + EN) — biar iklan yg bahasanya beda ttp nyambung niche-nya
KELUARGA_KATA = [
    ["rak", "lemari", "furnitur", "furniture", "mebel", "shelf", "kitchen set", "backdrop"],
    ["sepatu", "sandal", "sneaker", "footwear", "shoes", "slip on", "heels"],
    ["tas", "handbag", "dompet", "backpack", "ransel", "koper"],
    ["kopi", "coffee", "cappuccino", "latte", "espresso"],
    ["diet", "langsing", "pelangsing", "kurus", "fat burner", "detox", "berat badan"],
    ["serum", "skincare", "skin care", "krim wajah", "cream", "pelembab", "moisturizer",
     "sunscreen", "tabir surya", "facial", "glowing", "acne", "jerawat", "toner", "essence"],
    ["hijab", "jilbab", "kerudung", "gamis", "abaya", "modest wear"],
    ["madu", "herbal", "suplemen", "vitamin", "obat", "jamu", "thibbun", "collagen"],
    ["susu", "formula", "bayi", "balita", "popok", "diapers", "stroller", "mpasi"],
    ["hp", "gadget", "smartphone", "iphone", "android", "case", "charger", "earphone",
     "tws", "laptop", "komputer", "headset"],
    ["parfum", "perfume", "fragrance", "cologne", "mist"],
    ["kaos", "baju", "kemeja", "celana", "dress", "outfit", "fashion", "hoodie", "jaket"],
    ["motor", "mobil", "otomotif", "helm", "oli", "ban", "knalpot"],
    ["makanan", "snack", "kue", "roti", "frozen", "catering", "kuliner", "coklat", "cokelat"],
    ["elektronik", "ac", "kulkas", "mesin cuci", "vacuum", "tv", "kipas", "kompor"],
    ["kursi", "meja", "sofa", "springbed", "kasur", "furnitur"],
    ["lulur", "sabun", "body care", "perawatan tubuh", "masker", "hair", "rambut", "shampoo"],
]


# v2 anti-bocor (dari kamar sebelah; daftar kata = rekonstruksi): kategori hiburan, bukan produk jualan
HIBURAN = ("short drama", "full episode", "full episodes", "watch full", "drama", "episode",
           "episodes", "movie", "movies", "novel", "chapter", "streaming", "nonton", "tonton",
           "trailer", "binge", "gameplay", "game", "games", "gaming")

# v2.1: penanda katalog lintas negara (importir — bukan penjual niche lokal)
LINTAS = ("alibaba", "aliexpress", "1688", "made in china", "ship from china", "ships from china",
          "shipping worldwide", "worldwide delivery", "ship worldwide", "delivery worldwide")


def _ada_kata(pola_kata, *teks_list):
    rx = r"\b" + re.escape(pola_kata) + r"\b"
    return any(re.search(rx, t) for t in teks_list)


def hitung_relevansi(kata, snap, o, b):
    """Nilai kecocokan iklan thd kata kunci scan (0-100) — berantas niche luar.
    Dicek ke: teks utama, judul, caption, deskripsi link, nama halaman, isi kartu,
    kategori halaman, link (ter-decode) + keluarga kata senada bahasa ID.
    v0.8: + anti-bocor v2–v2.3 (multi-kata, hiburan, lintas negara, katalog campuran,
    produk-bukan-bahan). Bobot dasar & ambang label = v0.7."""
    kata = (kata or "").strip().lower()
    if not kata:
        return {"nilai": 100, "label": "Relevan", "cocok": []}
    token = [t for t in re.split(r"\s+", kata) if len(t) > 2]
    if not token:
        return {"nilai": 100, "label": "Relevan", "cocok": []}

    potongan = [teks(snap, "title"), teks(snap, "body"), teks(snap, "caption"),
                teks(snap, "link_description"), b.get("teks") or "",
                teks(snap, "additional_info"), teks(snap, "disclaimer_label"),
                " ".join(str(x) for x in (snap.get("extra_texts") or [])),
                " ".join(str(x.get("page_name") or "") for x in [snap.get("branded_content") or {}] if isinstance(x, dict)),
                " ".join(str(x) for x in (snap.get("page_categories") or o.get("categories") or [])),
                " ".join(str(x.get("name") or x.get("title") or "") for x in (o.get("menu_items") or []) if isinstance(x, dict))]
    kartu_teks = []
    for c in (snap.get("cards") or []):
        if isinstance(c, dict):
            kt = (str(c.get("title") or "") + " " + str(c.get("body") or "")
                  + " " + str(c.get("link_description") or ""))
            potongan.append(kt)
            kartu_teks.append(kt.lower())
    teks_utama = " ".join(str(p) for p in potongan).lower()
    nama_hal = str(o.get("page_name") or snap.get("page_name") or "").lower()
    link = unquote(str(snap.get("link_url") or "")).lower()

    skor, cocok = 0, []
    frasa = kata in teks_utama
    if frasa:
        skor += 50
        cocok.append(kata)
    jml_token = 0
    for t in token:
        if t in teks_utama:
            skor += 20
            jml_token += 1
            if t not in cocok:
                cocok.append(t)
        elif t in nama_hal:
            skor += 8
            jml_token += 1
            cocok.append(t + " (nama halaman)")
        elif t in link:
            skor += 10
            jml_token += 1
            cocok.append(t + " (tautan)")
    # keluarga kata senada: "rak sepatu" nyambung ke iklan yang bilang "furniture"
    # v2.1: kata query sendiri tidak dihitung lagi sebagai "senada" (dulu double-count)
    for f in KELUARGA_KATA:
        if any(w in kata for w in f):
            for w in f:
                if len(w) > 3 and w not in token and w != kata and (w in teks_utama or w in nama_hal or w in link):
                    skor += 15
                    cocok.append(w + " (senada)")
                    break
    # v2: multi-kata wajib semua token nyambung — satu kata saja tidak cukup
    if len(token) >= 2 and jml_token < len(token):
        skor = min(skor, 28)
    # v2: penalti kategori hiburan (biang bocor: drama/game/novel)
    for h in HIBURAN:
        if _ada_kata(h, teks_utama, nama_hal):
            skor = max(0, skor - (25 if frasa else 50))
            cocok.append("hiburan: " + h)
            break
    # v2.1: katalog lintas negara
    for l in LINTAS:
        if _ada_kata(l, teks_utama, nama_hal):
            skor = min(skor, 12)
            cocok.append("lintas negara: " + l)
            break
    # v2.2: katalog campuran (DPA) — mayoritas kartu BUKAN niche query
    if len(kartu_teks) >= 3:
        senada = set(token)
        for f in KELUARGA_KATA:
            if any(w in kata for w in f):
                senada.update(w for w in f if len(w) > 2)
        nyambung = sum(1 for kt in kartu_teks if any(w in kt for w in senada))
        if nyambung / len(kartu_teks) < 0.3:
            skor = min(skor, 12)
            cocok.append("katalog campuran: %d/%d kartu nyambung" % (nyambung, len(kartu_teks)))
    # v2.3: "produk, bukan bahan" — kata kunci disebut sekadar bahan/varian rasa produk lain
    if len(token) == 1 and skor >= 15:
        k = re.escape(kata)
        bahan = (re.search(r"\b(dengan|plus|mengandung|berbahan|ekstrak|extract|diperkaya|topping|varian|rasa)\b[^.?!]{0,45}\b" + k + r"\b", teks_utama)
                 or re.search(r"\b(ubi|singkong|ketela|kentang|roti|kue|donat|bakar|goreng|sate|ayam|ikan|daging|sosis|es|jus|teh|kopi|susu|yoghurt|granola|cokelat|coklat|permen|keripik|snack|biskuit|mie|sambal|saus|sabun|shampoo|lotion|serum|masker|lulur)\s+" + k + r"\b", teks_utama))
        produk = (kata in nama_hal or kata in link
                  or re.search(r"\b(minum|konsumsi|diminum|dimakan|pakai|dipakai|beli|order|pesan|checkout|ready|restock|manfaat|khasiat|ampuh)\b[^.?!]{0,40}\b" + k + r"\b", teks_utama)
                  or re.search(r"\b" + k + r"\s+(asli|murni|hutan|super|premium|botol|gram|kg|ml|herbal|original|ori|termurah|terlaris)\b", teks_utama))
        if bahan and not produk:
            skor = min(skor, 12)
            cocok.append(kata + " sekadar bahan/varian, bukan produk utama")
    skor = max(0, min(100, skor))
    if skor >= 40:
        label = "Relevan"
    elif skor >= 15:
        label = "Longgar"
    else:
        label = "Lepas"
    return {"nilai": skor, "label": label, "cocok": cocok}


def rincian(o, b):
    """Paket detail LENGKAP dari objek iklan mentah + baris olahan."""
    snap = o.get("snapshot") or {}
    imgs, vids, cards, extra_imgs, extra_links = [], [], [], [], []
    for im in (snap.get("images") or []):
        if isinstance(im, dict):
            u = im.get("original_image_url") or im.get("resized_image_url")
            if u:
                imgs.append(u)
    for im in (snap.get("extra_images") or []):
        if isinstance(im, dict):
            u = im.get("original_image_url") or im.get("resized_image_url")
            if u:
                extra_imgs.append(u)
    for v in (snap.get("videos") or []):
        if isinstance(v, dict):
            vids.append({
                "thumb": v.get("video_preview_image_url") or "",
                "hd": v.get("video_hd_url") or v.get("watermarked_video_hd_url") or "",
                "sd": v.get("video_sd_url") or v.get("watermarked_video_sd_url") or "",
            })
    for c in (snap.get("cards") or []):
        if isinstance(c, dict):
            cu = c.get("resized_image_url") or c.get("original_image_url") or ""
            if not cu:
                ci = c.get("images") or []
                if ci and isinstance(ci[0], dict):
                    cu = ci[0].get("resized_image_url") or ci[0].get("original_image_url") or ""
            cards.append({"title": bersih_tpl(c.get("title")), "body": bersih_tpl(c.get("body")),
                          "link": c.get("link_url") or "", "img": cu,
                          "harga": teks(c, "link_description"),
                          "video": c.get("video_hd_url") or c.get("video_sd_url") or ""})
    for l in (snap.get("extra_links") or []):
        if isinstance(l, dict):
            u = l.get("link_url") or ""
            if u:
                extra_links.append(u)
        elif isinstance(l, str) and l:
            extra_links.append(l)

    sk_umur = round(min(b["umur_hari"] / 60, 1) * 50, 1)
    sk_varian = round(min((b.get("varian_halaman") or b["varian_batch"]) / 10, 1) * 25, 1)
    sk_funnel = {"CTWA/WA": 15, "Marketplace": 10, "LP/Website": 5}.get(b["funnel"], 0)
    sk_video = 10 if b["format"] == "Video" else 4

    mulai_str, mulai_ep = fmt_epoch(o.get("start_date"))
    akhir_str, _ = fmt_epoch(o.get("end_date"))

    impresi = o.get("impressions_with_index")
    if isinstance(impresi, dict):
        impresi = impresi.get("impressions_text") or (
            "index " + str(impresi.get("impressions_index"))
            if impresi.get("impressions_index", -1) not in (-1, None) else None)
    reach = o.get("reach_estimate")
    if isinstance(reach, dict):
        reach = reach.get("reach_estimate_text") or json.dumps(reach)[:60]

    reg = o.get("regional_regulation_data") or {}
    limited = False
    if isinstance(reg, dict):
        for v in reg.values():
            if isinstance(v, dict) and any(v.values()):
                if v.get("is_limited_delivery"):
                    limited = True

    branded = ""
    bc = snap.get("branded_content")
    if isinstance(bc, dict):
        branded = bc.get("page_name") or bc.get("page_profile_uri") or str(bc.get("page_id") or "")

    return {
        "archive_id": o.get("ad_archive_id"),
        "ad_id": o.get("ad_id"),
        "page_id": o.get("page_id"),
        "page_name": o.get("page_name") or snap.get("page_name"),
        "page_profile_uri": snap.get("page_profile_uri") or "",
        "page_like_count": snap.get("page_like_count"),
        "page_cats": snap.get("page_categories") or [],
        "byline": teks(snap, "byline"),
        "branded": branded,
        "is_active": bool(o.get("is_active")),
        "mulai": b["tgl_mulai"] or mulai_str,
        "mulai_epoch": mulai_ep,
        "berakhir": akhir_str,
        "umur": b["umur_hari"],
        "spend": o.get("spend"),
        "currency": o.get("currency") or "",
        "impressions": impresi,
        "reach": reach,
        "active_time": o.get("total_active_time"),
        "negara": o.get("targeted_or_reached_countries") or [],
        "platform": o.get("publisher_platform") or [],
        "kategori": o.get("categories") or [],
        "varian": o.get("collation_count"),
        "collation_id": o.get("collation_id"),
        "ai_media": bool(o.get("contains_digital_created_media")),
        "sensitive": bool(o.get("contains_sensitive_content")),
        "report_count": o.get("report_count") or 0,
        "limited": limited,
        "page_deleted": bool(o.get("page_is_deleted") or snap.get("page_is_deleted")),
        "title": bersih_tpl(teks(snap, "title")),   # v1.4.2: tanpa {{product.brand}} dkk (asli di raw)
        "body": bersih_tpl(teks(snap, "body")),
        "caption": bersih_tpl(teks(snap, "caption")),
        "link_desc": bersih_tpl(teks(snap, "link_description")),
        "katalog_tpl": any(ada_tpl(teks(snap, k)) for k in ("title", "body", "caption", "link_description"))
                       or any(isinstance(c, dict) and (ada_tpl(c.get("title")) or ada_tpl(c.get("body")))
                              for c in (snap.get("cards") or [])),
        "cta_text": snap.get("cta_text") or "",
        "cta_type": str(snap.get("cta_type") or ""),
        "display_format": snap.get("display_format") or "",
        "images": imgs, "extra_images": extra_imgs, "videos": vids, "cards": cards,
        "extra_links": extra_links,
        "link_url": snap.get("link_url") or "",
        "infer": inferensi(snap, o),
        "skor_detail": {"umur": sk_umur, "varian": sk_varian, "funnel": sk_funnel,
                        "video": sk_video,
                        "total": round(sk_umur + sk_varian + sk_funnel + sk_video, 1)},
        "raw": o,
    }


def parse_harga(v):
    """'IDR525,000' / 'Rp 1.259.000' / '59000' / 'Rp59.000,00' -> int rupiah.
    v0.8.1: teks bebas (mis. deskripsi kartu DPA "maks 15.00 WIB", "50g SPF50+") TIDAK lagi
    digabung angkanya — hanya angka berawalan Rp/IDR yang diambil dari teks campuran."""
    if v is None:
        return None
    t = str(v).strip()
    if not re.fullmatch(r"(?:rp\.?|idr)?\s*[0-9][0-9.,]*\s*", t, re.I):
        m = re.search(r"(?:\bRp\.?|\bIDR)\s?([0-9]{1,3}(?:[.,][0-9]{3})+(?:[.,][0-9]{2})?|[0-9]{4,9})(?![0-9])", t, re.I)
        if not m:
            return None
        t = m.group(1)
    t = re.sub(r"(?<=[0-9]{3})[.,][0-9]{2}\s*$", "", t.strip())  # buang desimal sen: 59.000,00 / 59000.00
    ang = re.sub(r"[^0-9]", "", t)
    if not ang or not (4 <= len(ang) <= 11):
        return None
    try:
        n = int(ang)
    except Exception:
        return None
    return n if 500 <= n <= 100_000_000 else None


def klasifikasi_tautan(url):
    u = url.lower()
    if any(m in u for m in MARKETPLACE):
        return "mp"
    if any(s in u for s in SOSIAL):
        return "sosial"
    return "lp"


def gali_harga_lp(url):
    """Tarik harga + sinyal promo dari halaman tujuan iklan (LP)."""
    if url in url_cache:
        return url_cache[url]
    hasil = {"jenis": "lp", "ok": False, "tipe": "gagal", "harga": [], "promo": []}
    try:
        r = requests.get(url, timeout=14, headers={
            "User-Agent": mesin.UA, "Accept-Language": "id-ID,id;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        t = r.text
        if len(t) < 500:
            hasil["tipe"] = "kosong"
        else:
            kandidat = []
            # v0.8.1: toko luar negeri (USD/EUR...) → angka "price" (sering dlm sen: 2600 = $26.00)
            # bukan rupiah; hanya pola "Rp ..." yang tetap dipercaya.
            mu = re.search(r'"(?:priceCurrency|currency|currencyCode|currency_code)"\s*:\s*"([A-Za-z]{3})"', t)
            asing = bool(mu and mu.group(1).upper() != "IDR")
            hasil["mata_uang"] = mu.group(1).upper() if mu else None
            for m in ([] if asing else re.findall(r'"price"\s*:\s*"?([0-9]{4,9})', t)):
                v = parse_harga(m)
                if v:
                    kandidat.append(v)
            for m in re.findall(r'Rp\.?\s?([0-9]{1,3}(?:[.,][0-9]{3})+)', t):
                v = parse_harga(m)
                if v:
                    kandidat.append(v)
            for m in ([] if asing else re.findall(r'"(?:lowPrice|highPrice)"\s*:\s*"?([0-9]{4,9})', t)):
                v = parse_harga(m)
                if v:
                    kandidat.append(v)
            kandidat = sorted(set(kandidat))[:12]
            rendah = t.lower()
            promo = [k for k in KATA_PROMO if k in rendah]
            # A5: pola terjual lebih dalam — "1,2rb terjual", "1rb+ terjual", stok, social proof
            terjual = re.findall(
                r"([0-9.,]+)\s*(?:rb|ribu|jt|juta|k|m)?\s*\+?\s*(?:terjual|sudah dibeli|pembeli|sold|laku)",
                rendah)[:5]
            stok = re.findall(
                r"(?:stok\s*(?:tersisa|tinggal|sisa)|tersisa)\s*([0-9.,]+\s*(?:rb|ribu|pcs|buah|unit)?)",
                rendah)[:3]
            sosial = re.findall(
                r"([0-9.,]+)\s*(?:orang\s*)?(?:sedang\s*)?(?:melihat|dilihat|menonton|dalam keranjang|di keranjang)",
                rendah)[:3]
            if kandidat:
                hasil.update({"ok": True, "tipe": "ecommerce", "harga": kandidat,
                              "min": kandidat[0], "max": kandidat[-1], "promo": promo,
                              "terjual": terjual, "stok": stok, "sosial": sosial})
            else:
                hasil.update({"ok": True, "tipe": "web", "promo": promo,
                              "terjual": terjual, "stok": stok, "sosial": sosial})
    except Exception as e:
        hasil["error"] = str(e)[:80]
    if len(url_cache) < 200:
        url_cache[url] = hasil
    return hasil


def gali_harga_mp(url):
    """Coba tarik harga/terjual/rating dari halaman marketplace (best-effort,
    sering kena anti-bot — kalaupun gagal, identitas produk tetap dibawa)."""
    if url in url_cache:
        return url_cache[url]
    hasil = {"jenis": "mp", "ok": False, "tipe": "terkunci", "harga": [], "url_final": ""}
    try:
        r = requests.get(url, timeout=14, allow_redirects=True, headers={
            "User-Agent": mesin.UA, "Accept-Language": "id-ID,id;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        t = r.text
        hasil["url_final"] = r.url
        kandidat = []
        for m in re.findall(r'"(?:price|item_price|price_min|price_max)"\s*:\s*"?([0-9]{4,9})', t):
            v = parse_harga(m)
            if v:
                kandidat.append(v)
        for m in re.findall(r'Rp\.?\s?([0-9]{1,3}(?:[.,][0-9]{3})+)', t):
            v = parse_harga(m)
            if v:
                kandidat.append(v)
        terjual = re.findall(
            r'([0-9.,]+)\s*(?:rb|ribu|jt|juta|k|m)?\s*\+?\s*(?:terjual|sold)', t, re.I)[:5]
        rating = re.findall(r'"(?:rating_star|ratingValue)"\s*:\s*"?([0-9.]+)', t)[:2]
        if kandidat:
            kandidat = sorted(set(kandidat))[:12]
            hasil.update({"ok": True, "tipe": "dapat", "harga": kandidat,
                          "min": kandidat[0], "max": kandidat[-1],
                          "terjual": terjual, "rating": rating})
        else:
            # kemungkinan halaman shell anti-bot
            hasil["tipe"] = "terkunci"
    except Exception as e:
        hasil["error"] = str(e)[:80]
    if len(url_cache) < 200:
        url_cache[url] = hasil
    return hasil


def estimasi_ekonomi(x):
    """MODEL angka duit (bukan data Meta): estimasi belanja iklan & omzet dari
    sinyal umur + varian + harga. Asumsi transparan, akurasi arah ±20-30%."""
    V = x.get("varian") or 1
    D = max(x.get("umur") or 1, 1)
    # tier operasi harian dari jumlah varian (makin banyak varian = makin besar akunnya)
    if V >= 10:
        lo, hi = 1_000_000, 4_000_000
    elif V >= 3:
        lo, hi = 300_000, 1_500_000
    else:
        lo, hi = 50_000, 400_000
    # faktor keberlanjutan: iklan lama = belanja terjaga; iklan baru = fase uji
    if D >= 60:
        sf_lo, sf_hi = 0.6, 1.0
    elif D >= 14:
        sf_lo, sf_hi = 0.5, 1.0
    else:
        sf_lo, sf_hi = 0.3, 1.0
    harian_lo, harian_hi = lo * sf_lo, hi * sf_hi
    bulanan_lo, bulanan_hi = int(harian_lo * 30), int(harian_hi * 30)
    kumulatif_lo, kumulatif_hi = int(harian_lo * D), int(harian_hi * D)
    omzet_lo, omzet_hi = int(bulanan_lo * 1.5), int(bulanan_hi * 3)
    unit = None
    h_min, h_max = x.get("harga_min"), x.get("harga_max")
    if h_min and h_max:
        unit = [int(omzet_lo / max(h_max, 1)), int(omzet_hi / max(h_min, 1))]
    return {
        "harian_min": int(harian_lo), "harian_max": int(harian_hi),
        "bulanan_min": bulanan_lo, "bulanan_max": bulanan_hi,
        "kumulatif_min": kumulatif_lo, "kumulatif_max": kumulatif_hi,
        "omzet_min": omzet_lo, "omzet_max": omzet_hi,
        "unit": unit, "varian": V,
    }


def parse_volume(v):
    """'1,2rb' / '12rb' / '3,5jt' / '1234' -> int (jumlah terjual)."""
    if not v:
        return 0
    s = str(v).lower().replace(",", ".")
    m = re.search(r"([0-9.]+)\s*(rb|ribu|jt|juta|k|m)?", s)
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except Exception:
        return 0
    mult = {None: 1, "": 1, "rb": 1e3, "ribu": 1e3, "k": 1e3,
            "jt": 1e6, "juta": 1e6, "m": 1e6}.get(m.group(2), 1)
    return int(n * mult)


def skor_jual(x, ek):
    """Skor 'penjualan maksimal' (0-100): bukti terjual / est. omzet + umur +
    varian + nilai transaksi. Semakin tinggi = semakin layak jadi acuan jualan."""
    poin, rincian = 0.0, {}
    terjual = x.get("terjual_n") or 0
    if terjual:
        p = min(terjual / 100, 1) * 40
        rincian["bukti terjual"] = round(p, 1)
    else:
        omzet_mid = ((ek["omzet_min"] + ek["omzet_max"]) // 2) if ek else 0
        p = min(omzet_mid / 150_000_000, 1) * 25
        rincian["est. omzet"] = round(p, 1)
    poin += p
    p = min(x["umur"] / 90, 1) * 25
    rincian["ketahanan"] = round(p, 1)
    poin += p
    p = min((x.get("varian") or 0) / 10, 1) * 20
    rincian["skala varian"] = round(p, 1)
    poin += p
    p = min((x.get("harga_max") or 0) / 2_000_000, 1) * 15
    rincian["nilai transaksi"] = round(p, 1)
    poin += p
    label = "🏆 Pemenang" if poin >= 60 else ("Kuat" if poin >= 40 else "Biasa")
    return {"nilai": round(poin, 1), "label": label, "rincian": rincian}


def bangun_top_produk(out):
    """Agregasi per PRODUK (bukan per iklan) -> ranking 10 teratas
    berdasarkan penjualan × omzet × lama aktif (Skor Jual)."""
    grup = {}
    for idx, x in enumerate(out):
        if x.get("aktif") is False:  # pemenang harus hidup — iklan mati bukan acuan
            continue
        reln = (x.get("relevansi") or {}).get("nilai", 100)
        if reln < 15:  # iklan Lepas (niche luar) tidak masuk ranking produk
            continue
        det = x["detail"]
        nama = (det.get("title") or "").strip()
        nama = re.sub(r"\{\{[^}]*\}\}", " ", nama).strip()
        nama = re.sub(r"\s+", " ", nama)
        if len(nama) < 4 or nama.lower() in ("mari mengobrol bersama kami", "none", "null"):
            nama = x["halaman"]
        kunci = (x["halaman"], re.sub(r"\W+", "", nama.lower())[:40])
        g = grup.setdefault(kunci, {
            "nama": nama, "halaman": x["halaman"],
            "foto": x.get("foto") or x.get("thumb"),
            "harga_min": None, "harga_max": None, "terjual": 0,
            "omzet_min": 0, "omzet_max": 0, "umur": 0, "varian": 0,
            "funnel": x["funnel"], "format": x["format"],
            "cta": det.get("cta_text") or "", "ads": [],
        })
        ek = det.get("ekonomi") or {}
        if x.get("harga_min"):
            g["harga_min"] = min(g["harga_min"] or x["harga_min"], x["harga_min"])
            g["harga_max"] = max(g["harga_max"] or x["harga_max"], x["harga_max"])
        g["terjual"] = max(g["terjual"], x.get("terjual_n") or 0)
        g["omzet_min"] = max(g["omzet_min"], ek.get("omzet_min") or 0)
        g["omzet_max"] = max(g["omzet_max"], ek.get("omzet_max") or 0)
        g["umur"] = max(g["umur"], x["umur"])
        g["rel"] = max(g.get("rel", 0), reln)
        g["varian"] += x.get("varian") or 0
        g["ads"].append({"idx": idx, "umur": x["umur"], "skor": x["skor"],
                         "format": x["format"], "skor_jual": x.get("jual")})

    hasil = []
    for g in grup.values():
        baris_tiru = {"umur": g["umur"], "varian": g["varian"],
                      "harga_min": g["harga_min"], "harga_max": g["harga_max"],
                      "terjual_n": g["terjual"]}
        ek_tiru = {"omzet_min": g["omzet_min"], "omzet_max": g["omzet_max"]}
        sj = skor_jual(baris_tiru, ek_tiru)
        unit = None
        if g["harga_min"] and g["harga_max"]:
            unit = [int(g["omzet_min"] / max(g["harga_max"], 1)),
                    int(g["omzet_max"] / max(g["harga_min"], 1))]
        # gerbang mutu kartu: wajib ada bukti niaga ATAU relevansi kuat
        if not (g["harga_min"] or g["terjual"] or unit or g.get("rel", 0) >= 60):
            continue
        g["skor_jual"] = sj["nilai"]
        g["label"] = sj["label"]
        g["unit"] = unit
        g["ads"].sort(key=lambda a: -(a["skor_jual"] or 0))
        hasil.append(g)
    hasil.sort(key=lambda g: -g["skor_jual"])
    return hasil[:10]


def status_iklan(umur, skor, jual=None):
    """Label keputusan MENANG/NAIK/UJI (tandingan Winning/Scale/Test).
    Basis: kekuatan sinyal (Skor Cuan) + ketahanan umur + bukti (Skor Jual).
    Ambang di-tuning 2026-10-10 dari benchmark: median Cuan 66 → MENANG wajib di kuartil atas."""
    if (skor >= 70 and umur >= 14) or (jual or 0) >= 55:
        return "MENANG"
    if skor >= 45 and umur >= 7:
        return "NAIK"
    return "UJI"


# ===================== v1.4.7: M7 Rekam jejak pengiklan =====================
# Sumber: tab "Tentang" Meta Ad Library (data transparansi halaman, publik) → 🟢 asli Meta.
# Diambil ON-DEMAND (saat profil dibuka / tombol di detail iklan), BUKAN saat scan → scan tidak melambat.
# Cache: setelan "jejak:<pid>" 7 hari + memori; gagal dicache 20 menit supaya tidak membebani Meta.
JEJAK_TTL = 7 * 86400
JEJAK_DOC_ID = os.environ.get("INTAI_JEJAK_DOC", "26438337149177216")
JEJAK_MEM = {}
JEJAK_GAGAL = {}
_jejak_sem = threading.BoundedSemaphore(2)
_PAT_LSD = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')


def _cari_kunci(o, kunci, kedalaman=0):
    if kedalaman > 14:
        return None
    if isinstance(o, dict):
        if kunci in o:
            return o
        for v in o.values():
            r = _cari_kunci(v, kunci, kedalaman + 1)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _cari_kunci(v, kunci, kedalaman + 1)
            if r is not None:
                return r
    return None


def _jejak_ambil_meta(pid):
    """1 halaman Ad Library (LSD + variabel) + 1 POST graphql tab Tentang → dict mentah."""
    s = mesin.buka_sesi()
    url = ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
           f"&country=ID&view_all_page_id={pid}&search_type=page&media_type=all")
    html = ambil_halaman_url(s, url)
    m = _PAT_LSD.search(html or "")
    if not m:
        raise RuntimeError("token Meta tidak terbaca")
    lsd = m.group(1)
    vs = {}
    i = html.find('"queryName":"AdLibraryFoundationRootQuery"')
    j = html.rfind('"variables":', 0, i) if i > 0 else -1
    if j > 0:
        try:
            vs = json.loads(html[j + 12:i].rstrip().rstrip(","))
        except Exception:
            vs = {}
    if not vs:
        vs = {"activeStatus": "all", "adType": "ALL", "audienceTimeframe": "LAST_7_DAYS", "bylines": [],
              "collationToken": None, "contentLanguages": [], "countries": ["ID"], "country": "ID",
              "deeplinkAdID": None, "isAudienceTab": False, "isLandingPage": False, "isTargetedCountry": False,
              "hasDeeplinkAdID": False, "location": None, "mediaType": "all", "multiCountryFilterMode": None,
              "pageIDs": [], "potentialReachInput": None, "publisherPlatforms": [], "regions": None,
              "sessionID": secrets.token_hex(16), "source": None, "sortData": None, "startDate": None,
              "fetchSharedDisclaimers": True}
    vs.pop("v", None)
    vs.update({"queryString": "", "searchType": "page", "viewAllPageID": pid, "activeStatus": "all",
               "isAboutTab": True, "fetchPageInfo": True})
    r = s.post("https://www.facebook.com/api/graphql/", timeout=30, data={
        "lsd": lsd, "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": "AdLibraryFoundationRootQuery",
        "variables": json.dumps(vs), "server_timestamps": "true", "doc_id": JEJAK_DOC_ID},
        headers={"X-FB-LSD": lsd, "X-FB-Friendly-Name": "AdLibraryFoundationRootQuery",
                 "Origin": "https://www.facebook.com", "Referer": url,
                 "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
                 "Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code != 200:
        raise RuntimeError(f"Meta menjawab {r.status_code}")
    halaman, info = None, None
    for baris in r.text.splitlines():
        baris = baris.strip()
        if not baris.startswith("{"):
            continue
        try:
            o = json.loads(baris)
        except Exception:
            continue
        if halaman is None:
            halaman = _cari_kunci(o, "pages_transparency_info")
        if info is None:
            pi = _cari_kunci(o, "page_verification")
            if pi is not None and "ig_followers" in pi:
                info = pi
    if halaman is None:
        raise RuntimeError("format Meta berubah (data transparansi tidak ada)")
    return halaman, info or {}


def _jejak_olah(pid, halaman, info):
    now = time.time()
    pti = halaman.get("pages_transparency_info") or {}
    hist = [x for x in (pti.get("history_items") or []) if isinstance(x, dict)]
    dibuat = None
    ganti = []
    for x in hist:
        try:
            t = int(x.get("event_time") or 0)
        except Exception:
            t = 0
        if x.get("item_type") == "CREATION" and t:
            dibuat = t if dibuat is None else min(dibuat, t)
        elif x.get("item_type") == "NAME_CHANGE" and t:
            ganti.append(t)
    ganti.sort(reverse=True)
    admin = []
    for c in ((pti.get("admin_locations") or {}).get("admin_country_counts") or []):
        try:
            admin.append({"negara": str(((c.get("country") or {}).get("iso_name")) or "?"), "n": int(c.get("count") or 0)})
        except Exception:
            pass
    admin.sort(key=lambda a: -a["n"])
    pem = halaman.get("confirmed_page_owner")
    if isinstance(pem, dict):
        pem = pem.get("name") or pem.get("text") or "terkonfirmasi"
    pem = str(pem) if pem else None
    d = {
        "pid": pid, "nama": halaman.get("name") or info.get("page_name"),
        "dibuat": dibuat, "umur_hari": int((now - dibuat) // 86400) if dibuat else None,
        "ganti_nama": ganti, "admin": admin, "pemilik": pem,
        "tentang": ((halaman.get("about") or {}).get("text") or "")[:400],
        "media_negara": pti.get("state_media_country_label"),
        "verifikasi": info.get("page_verification"), "kategori": info.get("page_category"),
        "likes": info.get("likes"), "ig_username": info.get("ig_username"),
        "ig_followers": info.get("ig_followers"), "ig_verifikasi": info.get("ig_verification"),
        "dihapus": bool(info.get("page_is_deleted")), "iklan_tersembunyi": info.get("hidden_ads"),
    }
    # tanda = interpretasi INTAI 🔵 dari fakta 🟢 di atas
    t = []
    u = d["umur_hari"]
    if u is not None:
        if u >= 730:
            t.append({"j": "kuat", "t": f"Halaman berumur {u // 365} tahun — bukan halaman baru/sekali pakai."})
        elif u < 90:
            t.append({"j": "waspada", "t": f"Halaman baru dibuat ({u} hari lalu). Bisa pemain baru, bisa juga halaman cadangan/sekali pakai."})
    n_g = len(ganti)
    if n_g >= 3:
        t.append({"j": "waspada", "t": f"Sudah ganti nama {n_g}×. Pola umum halaman yang dipakai ulang untuk produk berbeda."})
    if ganti and now - ganti[0] < 30 * 86400:
        t.append({"j": "waspada", "t": "Baru ganti nama dalam 30 hari terakhir."})
    if not ganti and u is not None and u >= 365:
        t.append({"j": "kuat", "t": "Tidak pernah ganti nama sejak dibuat — identitas merek konsisten."})
    if pem:
        t.append({"j": "kuat", "t": "Pemilik halaman terkonfirmasi Meta."})
    if str(d["verifikasi"] or "") in ("BLUE_VERIFIED", "VERIFIED", "GRAY_VERIFIED"):
        t.append({"j": "kuat", "t": "Halaman bercentang (terverifikasi)."})
    asing = [a["negara"] for a in admin if a["negara"] not in ("Indonesia", "?")]
    if asing:
        t.append({"j": "info", "t": "Ada admin di luar Indonesia: " + ", ".join(asing[:4]) + "."})
    tot = sum(a["n"] for a in admin)
    if tot >= 10:
        t.append({"j": "info", "t": f"Dikelola {tot} admin — kemungkinan tim/agensi, bukan perorangan."})
    if d["dihapus"]:
        t.append({"j": "waspada", "t": "Meta menandai halaman ini sudah dihapus."})
    d["tanda"] = t
    return d


def jejak_pengiklan(pid, segar=False):
    pid = re.sub(r"\D", "", str(pid or ""))[:20]
    if not pid:
        return {"ok": False, "error": "ID halaman tidak valid"}
    now = time.time()
    if not segar:
        c = JEJAK_MEM.get(pid)
        if not c:
            ok, v = setelan_baca("jejak:" + pid)
            if ok and isinstance(v, dict) and v.get("data"):
                c = v
                JEJAK_MEM[pid] = c
        if c and now - float(c.get("t") or 0) < JEJAK_TTL:
            return {"ok": True, "cache": True, "diambil": c["t"], "data": c["data"]}
    g = JEJAK_GAGAL.get(pid)
    if g and now - g[0] < 1200:
        return {"ok": False, "error": g[1] + " (coba lagi beberapa menit lagi)"}
    if not _jejak_sem.acquire(timeout=25):
        return {"ok": False, "error": "INTAI sedang mengambil data lain. Coba lagi sebentar."}
    try:
        halaman, info = _jejak_ambil_meta(pid)
        data = _jejak_olah(pid, halaman, info)
    except Exception as e:
        msg = str(e)[:140] or e.__class__.__name__
        JEJAK_GAGAL[pid] = (now, "Rekam jejak tidak terbaca dari Meta: " + msg)
        print(f"[INTAI] jejak {pid} gagal: {msg}")
        return {"ok": False, "error": JEJAK_GAGAL[pid][1]}
    finally:
        _jejak_sem.release()
    JEJAK_GAGAL.pop(pid, None)
    c = {"t": int(now), "data": data}
    JEJAK_MEM[pid] = c
    if len(JEJAK_MEM) > 800:
        for k in sorted(JEJAK_MEM, key=lambda k: JEJAK_MEM[k]["t"])[:200]:
            JEJAK_MEM.pop(k, None)
    setelan_tulis("jejak:" + pid, c)
    return {"ok": True, "cache": False, "diambil": c["t"], "data": data}


def jalankan_scan(nilai, mode="cari", pemilik=None):
    """Scan penuh per mode: 'cari' (keyword) atau 'halaman' (intip kompetitor)."""
    s = mesin.buka_sesi()
    if mode == "halaman":
        nilai = re.sub(r"\D", "", nilai)
        if not nilai:
            return {"ok": False, "error": "ID halaman tidak valid"}
        label = f"halaman {nilai}"
        mentah, lihat_k = [], set()
        for media in ("all", "video", "image"):  # variasi media + ikut iklan mati
            url = ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
                   f"&country=ID&view_all_page_id={quote(nilai)}&search_type=page&media_type={media}")
            try:
                html = ambil_halaman_url(s, url)
                for o in mesin.ekstrak_iklan(html):
                    aid = str(o.get("ad_archive_id") or "")
                    if aid and aid not in lihat_k:
                        lihat_k.add(aid)
                        mentah.append(o)
            except Exception:
                pass
            time.sleep(3)
    else:
        label = nilai
        mentah = mesin.kumpul_iklan(s, nilai, "ID")  # 3 variasi media + iklan mati
    if not mentah:
        return {"ok": False, "error": "Tidak ada iklan ditemukan (atau Ad Library sedang tidak merespons — coba lagi 1 menit)"}
    # A4 perapian: buang duplikat archive_id (ekstraksi bisa menangkap blok sama dua kali)
    lihat_id, mentah_unik = set(), []
    for o in mentah:
        aid = str(o.get("ad_archive_id") or "")
        if aid in lihat_id:
            continue
        lihat_id.add(aid)
        mentah_unik.append(o)
    mentah = mentah_unik
    rows = mesin.hitung_skor(mesin.rapihkan(mentah))
    by_id = {str(o.get("ad_archive_id")): o for o in mentah}

    idx_foto, idx_thumb = {}, {}
    for n, o in enumerate(mentah):
        snap = o.get("snapshot") or {}
        url = ""
        imgs = snap.get("images") or []
        if imgs and isinstance(imgs[0], dict):
            url = imgs[0].get("resized_image_url") or imgs[0].get("original_image_url") or ""
        if not url:
            vids = snap.get("videos") or []
            if vids and isinstance(vids[0], dict):
                url = vids[0].get("video_preview_image_url") or ""
        if not url:
            kartus = snap.get("cards") or []  # iklan DPA/carousel: gambar nempel di kartu
            if kartus and isinstance(kartus[0], dict):
                k0 = kartus[0]
                url = (k0.get("resized_image_url") or k0.get("original_image_url") or "")
                if not url:
                    ki = k0.get("images") or []
                    if ki and isinstance(ki[0], dict):
                        url = ki[0].get("resized_image_url") or ki[0].get("original_image_url") or ""
        if not url:
            continue
        idx_thumb[o.get("ad_archive_id")] = url
        if n >= FOTO_MAKS:
            continue  # base64 hanya utk 12 teratas (hemat); sisanya via proxy /img
        try:
            r = s.get(url, timeout=10,
                      headers={"User-Agent": mesin.UA, "Referer": "https://www.facebook.com/"})
            if r.status_code == 200 and len(r.content) > 800:
                tipe = "image/jpeg" if r.content[:2] == b"\xff\xd8" else "image/png"
                idx_foto[o.get("ad_archive_id")] = (
                    f"data:{tipe};base64," + base64.b64encode(r.content).decode())
        except Exception:
            pass

    out = []
    for b in rows:
        o = by_id.get(str(b["archive_id"]), {})
        if o:
            snap_r = o.get("snapshot") or {}
            rel = hitung_relevansi(nilai if mode == "cari" else "", snap_r, o, b)
        else:
            rel = {"nilai": 100, "label": "Relevan", "cocok": []}
        # A4: bersihkan token template {{...}} & tanggal aneh
        teks_bersih = re.sub(r"\s+", " ", bersih_tpl(b["teks"])).strip()
        katalog = ada_tpl(b["teks"])
        if o and (not teks_bersih or katalog):   # v1.4.2: teks katalog kosong → cadangan dari judul/deskripsi/caption
            snap_t = o.get("snapshot") or {}
            katalog = katalog or any(ada_tpl(teks(snap_t, k)) for k in ("title", "body", "caption", "link_description"))
            if not teks_bersih:
                for k in ("title", "link_description", "caption"):
                    teks_bersih = re.sub(r"\s+", " ", bersih_tpl(teks(snap_t, k))).strip()
                    if teks_bersih:
                        break
        umur, tgl = b["umur_hari"], b["tgl_mulai"]
        if umur is None or umur < 0 or umur > 3650:
            umur, tgl = 0, "?"
        out.append({
            "aktif": b.get("aktif") is not False,
            "halaman": b["halaman"], "teks": teks_bersih, "katalog": katalog, "format": b["format"],
            "funnel": b["funnel"], "link": b["link"], "tgl": tgl,
            "umur": umur, "skor": b["skor"], "varian": b["varian_batch"],
            "platform": b["platform"], "foto": idx_foto.get(b["archive_id"]),
            "thumb": idx_thumb.get(b["archive_id"]),
            "relevansi": rel, "rel_nilai": rel["nilai"],
            "status": status_iklan(umur, b["skor"]),
            "detail": rincian(o, b) if o else {"skor_detail": {"total": b["skor"]}, "raw": {}},
        })
    rows_hidup = [b for b in rows if b.get("aktif") is not False] or rows
    tertua = max(rows_hidup, key=lambda x: x["umur_hari"])

    # === GALI HARGA: DPA (kartu) + LP + Marketplace (paralel, best-effort) ===
    LP_MAKS, MP_MAKS = 8, 6
    lp_hit, mp_hit = 0, 0
    tugas = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for x in out:
            # jatah gali harga hanya utk iklan aktif & nyambung (iklan mati / niche luar dilewati)
            if x.get("aktif") is False or (x.get("relevansi") or {}).get("label") == "Lepas":
                continue
            lk = (x["detail"].get("link_url") or "").strip()
            if not lk.startswith("http"):
                continue
            jn = klasifikasi_tautan(lk)
            if jn == "mp" and mp_hit < MP_MAKS:
                mp_hit += 1
                tugas.append((x, ex.submit(gali_harga_mp, lk)))
            elif jn == "lp" and lp_hit < LP_MAKS:
                lp_hit += 1
                tugas.append((x, ex.submit(gali_harga_lp, lk)))
        for x, fut in tugas:
            try:
                hs = fut.result(timeout=30)
            except Exception:
                hs = {"ok": False, "tipe": "gagal", "harga": []}
            if hs.get("jenis") == "mp":
                x["detail"]["harga_mp"] = hs
            else:
                x["detail"]["harga_lp"] = hs

    semua_titik = []
    for x in out:
        det = x["detail"]
        dpa = [parse_harga(c.get("harga")) for c in (det.get("cards") or [])]
        det["harga_dpa"] = [v for v in dpa if v]
        kumpul = list(det["harga_dpa"])
        for src in ("harga_lp", "harga_mp"):
            kumpul += [v for v in (det.get(src) or {}).get("harga") or []]
        x["harga_min"] = min(kumpul) if kumpul else None
        x["harga_max"] = max(kumpul) if kumpul else None
        ek = estimasi_ekonomi(x)
        det["ekonomi"] = ek
        x["est_min"] = ek["bulanan_min"]
        x["est_max"] = ek["bulanan_max"]
        # angka terjual dari LP / marketplace -> bukti penjualan terkuat
        volume = []
        for src in ("harga_lp", "harga_mp"):
            for t in (det.get(src) or {}).get("terjual") or []:
                n = parse_volume(t)
                if n:
                    volume.append(n)
        x["terjual_n"] = max(volume) if volume else 0
        if x["terjual_n"]:
            ek["keyakinan"] = {"level": "tinggi", "alasan": "ada bukti terjual dari halaman tujuan"}
            # A1: KALIBRASI model vs bukti — bandingkan angka nyata dengan model
            if ek.get("unit"):
                um, xm = ek["unit"]
                n = x["terjual_n"]
                bulan_hidup = max(x["umur"] / 30.0, 1)
                model_kum = [um * bulan_hidup, xm * bulan_hidup]
                if n > model_kum[1]:
                    posisi = "bukti di ATAS model — model terlalu konservatif"
                elif n < model_kum[0] * 0.5:
                    posisi = "bukti di BAWAH model — model terlalu agresif"
                else:
                    posisi = "sejajar dengan model"
                det["kalibrasi"] = {
                    "bukti": n, "model_kum": [int(model_kum[0]), int(model_kum[1])],
                    "model_unit": [um, xm], "posisi": posisi,
                }
        elif x["harga_min"]:
            ek["keyakinan"] = {"level": "sedang", "alasan": "harga produk diketahui, omzet dari model ROAS"}
        else:
            ek["keyakinan"] = {"level": "arah", "alasan": "tanpa harga/bukti terjual — patokan skala saja"}
        sj = skor_jual(x, ek)
        det["skor_jual"] = sj
        x["jual"] = sj["nilai"]
        # A3: status final — pakai juga bukti (Skor Jual) yang baru terhitung
        x["status"] = status_iklan(x["umur"], x["skor"], sj["nilai"])
        # validasi silang harga: katalog DPA vs harga LP (dua sumber independen)
        dpa_v = det.get("harga_dpa") or []
        lp_h = (det.get("harga_lp") or {}).get("harga") or []
        if dpa_v and lp_h:
            dpa_med = sorted(dpa_v)[len(dpa_v) // 2]
            lp_med = sorted(lp_h)[len(lp_h) // 2]
            selisih = int(round((dpa_med - lp_med) / max(lp_med, 1) * 100))
            det["cek_harga"] = {
                "dpa": dpa_med, "lp": lp_med, "selisih": selisih,
                "status": "konsisten" if abs(selisih) <= 5 else
                          ("LP lebih murah" if selisih > 0 else "LP lebih mahal"),
            }
        for v in (kumpul if x.get("aktif") is not False
                  and (x.get("relevansi") or {}).get("label") != "Lepas" else []):
            label_produk = bersih_tpl(det.get("title") or x["teks"][:30]) or x["halaman"]
            semua_titik.append((v, x["halaman"], label_produk))
    peta_harga = {"jumlah_titik": 0}
    if semua_titik:
        urut = sorted(semua_titik, key=lambda z: z[0])
        peta_harga = {
            "jumlah_titik": len(urut),
            "termurah": {"harga": urut[0][0], "halaman": urut[0][1], "produk": urut[0][2]},
            "termahal": {"harga": urut[-1][0], "halaman": urut[-1][1], "produk": urut[-1][2]},
            "median": urut[len(urut) // 2][0],
        }

    # v0.8: agregat niche (uang, pemenang, acuan, waktu) HANYA dari iklan yang masih tayang
    # + buang niche luar (Lepas) supaya uang/pemenang/acuan tidak tercemar iklan tak nyambung
    hidup = ([x for x in out if x.get("aktif") is not False
              and (x.get("relevansi") or {}).get("label") != "Lepas"]
             or [x for x in out if x.get("aktif") is not False] or out)

    # ekonomi niche: total belanja iklan terpantau (model)
    est_list = sorted((x["est_min"] + x["est_max"]) // 2 for x in hidup)
    ekonomi_niche = {
        "bulanan_min": sum(x["est_min"] for x in hidup),
        "bulanan_max": sum(x["est_max"] for x in hidup),
        "median_bulanan": est_list[len(est_list) // 2] if est_list else 0,
    }

    # === PEMENANG + FORMULA JUALAN (acuan untuk iklan user sendiri) ===
    urut_jual = sorted(hidup, key=lambda z: -(z.get("jual") or 0))
    pemenang = []
    for x in urut_jual[:5]:
        det = x["detail"]
        ek = det.get("ekonomi") or {}
        teks_iklan = det.get("body") or det.get("title") or x.get("teks") or ""
        hook = re.sub(r"\s+", " ", re.split(r"[.\n!?]", teks_iklan)[0])[:80].strip() if teks_iklan else ""
        if x.get("harga_min") and x.get("harga_max") and x["harga_max"] != x["harga_min"]:
            harga_str = f"{x['harga_min']:,}–{x['harga_max']:,}".replace(",", ".")
        elif x.get("harga_min"):
            harga_str = f"{x['harga_min']:,}".replace(",", ".")
        else:
            harga_str = "belum terbaca"
        resep = (f"Jual sejenis di Meta Ads: kreatif {x['format']} + hook “{hook[:60]}” · "
                 f"harga {harga_str} · funnel {x['funnel']} (CTA: {det.get('cta_text') or '—'}) · "
                 f"budget test {(ek.get('harian_min') or 0):,}–{(ek.get('harian_max') or 0):,}/hari".replace(",", ".") +
                 f" · iklan semodel yang cuan bertahan {x['umur']}+ hari")
        pemenang.append({
            "halaman": x["halaman"], "skor_jual": x.get("jual"), "label": (det.get("skor_jual") or {}).get("label"),
            "hook": hook, "harga": harga_str, "funnel": x["funnel"], "format": x["format"],
            "cta": det.get("cta_text") or "", "umur": x["umur"], "varian": x.get("varian"),
            "terjual": x.get("terjual_n") or 0,
            "est_omzet": [ek.get("omzet_min"), ek.get("omzet_max")],
            "resep": resep, "archive_id": det.get("archive_id"),
        })

    hitung = lambda kunci: sorted(
        {k: sum(1 for x in hidup if (x.get(kunci) or x["detail"].get(kunci)) == k)
         for k in {x.get(kunci) or x["detail"].get(kunci) for x in hidup}}.items(),
        key=lambda kv: -kv[1])
    funnel_dom = (hitung("funnel") or [("—", 0)])[0][0]
    format_dom = (hitung("format") or [("—", 0)])[0][0]
    cta_dom = sorted({(x["detail"].get("cta_text") or "—") for x in hidup},
                     key=lambda c: -sum(1 for x in hidup if (x["detail"].get("cta_text") or "—") == c))
    harga_urut = sorted(x["harga_min"] for x in hidup if x.get("harga_min"))
    harian_urut = sorted(((x["detail"].get("ekonomi") or {}).get("harian_min") or 0) for x in hidup)
    acuan = {
        "funnel": funnel_dom,
        "format": format_dom,
        "cta": cta_dom[0] if cta_dom else "—",
        "harga_median": harga_urut[len(harga_urut) // 2] if harga_urut else 0,
        "budget_test": [harian_urut[len(harian_urut) // 2] if harian_urut else 0,
                        max(harian_urut) if harian_urut else 0],
    }

    # === WAKTU & ANGLE: kapan mereka mulai + hook yang dipakai ===
    per_bulan, hooks, cta_dist = {}, [], {}
    panjang_awet, panjang_baru = [], []
    for idx, x in enumerate(out):
        if x.get("aktif") is False:
            continue
        det = x["detail"]
        ep = det.get("mulai_epoch")
        if ep:
            dt = datetime.fromtimestamp(int(ep), tz=timezone.utc)
            kunci_bulan = f"{dt.year}-{dt.month:02d}"
            per_bulan[kunci_bulan] = per_bulan.get(kunci_bulan, 0) + 1
        teks_iklan = det.get("body") or det.get("title") or x.get("teks") or ""
        if teks_iklan:
            hook = re.split(r"[.\n!?]", teks_iklan)[0]
            hook = re.sub(r"\s+", " ", hook)[:90].strip()
            reln = (x.get("relevansi") or {}).get("nilai") or 0
            if (len(hook) > 6 and "{{" not in hook and "}}" not in hook
                    and "http" not in hook.lower() and "www." not in hook.lower() and reln >= 15):
                hooks.append({"hook": hook, "halaman": x["halaman"],
                              "umur": x["umur"], "skor": x["skor"], "idx": idx})
        cta = det.get("cta_text") or "—"
        cta_dist[cta] = cta_dist.get(cta, 0) + 1
        L = len(teks_iklan or "")
        if x["umur"] >= 60:
            panjang_awet.append(L)
        elif x["umur"] <= 14:
            panjang_baru.append(L)
    # rak angle: kelompokkan hook identik — pola yang dipakai banyak iklan = hook terbukti
    grup_hook = {}
    for h in hooks:
        k = (h["hook"] or "").lower()
        g = grup_hook.setdefault(k, {"hook": h["hook"], "jml": 0, "skor_max": 0, "umur_max": 0,
                                     "halaman": [], "idx": []})
        g["jml"] += 1
        g["skor_max"] = max(g["skor_max"], h.get("skor") or 0)
        g["umur_max"] = max(g["umur_max"], h.get("umur") or 0)
        if h["halaman"] not in g["halaman"]:
            g["halaman"].append(h["halaman"])
        g["idx"].append(h.get("idx"))
    hooks = sorted(grup_hook.values(), key=lambda z: (-z["jml"], -z["skor_max"]))
    waktu = {
        "baru_7hari": sum(1 for x in hidup if x["umur"] <= 7),
        "panas_3hari": sum(1 for x in hidup if x["umur"] <= 3),
        "per_bulan": dict(sorted(per_bulan.items())),
    }
    angle = {
        "top_hooks": hooks[:8],
        "cta": dict(sorted(cta_dist.items(), key=lambda kv: -kv[1])[:8]),
        "rata2_teks_awet": (sum(panjang_awet) // len(panjang_awet)) if panjang_awet else 0,
        "rata2_teks_baru": (sum(panjang_baru) // len(panjang_baru)) if panjang_baru else 0,
    }

    tertua_x = max(hidup, key=lambda x: x["umur"])
    # catat ke riwayat mesin -> muncul otomatis di dashboard (/api/ringkasan)
    indeks_kini = {}
    for x in out:
        if x.get("aktif") is False:
            continue
        aid = str((x.get("detail") or {}).get("archive_id") or "")
        if aid:
            indeks_kini[aid] = {"h": x.get("halaman"), "hr": x.get("harga_min") or 0}
    # v1.4: dicatat ke riwayat tiap pemilik (scan manual = 1 akun; scan otomatis = semua akun yg memintanya)
    daftar_pemilik = [pemilik] if isinstance(pemilik, str) else [p for p in (pemilik or []) if p]
    sejak_lalu = None
    for i_pm, pm in enumerate(daftar_pemilik or [""]):
        try:
            sl = catat_scan(label, mode, len(rows_hidup), indeks_kini, pm)
        except Exception:
            sl = None
        if i_pm == 0:
            sejak_lalu = sl
    # v1.0 P1: arsipkan semua iklan + pengiklan + potret niche (latar, best-effort)
    ARSIP_STATUS["antri"] = ARSIP_STATUS.get("antri", 0) + 1
    threading.Thread(target=arsipkan_scan, daemon=True, name="arsip",
                     args=(out, label, mode, len(rows_hidup), ekonomi_niche)).start()

    return {
        "ok": True, "kata": label, "mode": mode, "jumlah": len(rows_hidup), "sejak_lalu": sejak_lalu,
        "label_tampil": _nama_dari_rows(out) if mode == "halaman" else None,   # v1.4.2: nama halaman, bukan ID mentah
        "jumlah_mati": sum(1 for b in rows if b.get("aktif") is False),
        "rata2": sum(x["umur"] for x in hidup) // max(len(hidup), 1),
        "tertua": {"nama": tertua_x["halaman"], "hari": tertua_x["umur"]},
        "peta_harga": peta_harga,
        "ekonomi_niche": ekonomi_niche,
        "top_produk": bangun_top_produk(out),
        "pemenang": pemenang,
        "acuan": acuan,
        "waktu": waktu,
        "angle": angle,
        "rows": out,
    }


def ambil_gambar(target):
    if target in img_cache:
        return img_cache[target]
    host = urlparse(target).netloc
    if not any(h in host for h in ("fbcdn.net", "akamaihd.net", "facebook.com")):
        return None
    try:
        r = requests.get(target, timeout=12, headers={
            "User-Agent": mesin.UA, "Referer": "https://www.facebook.com/"})
        if r.status_code != 200 or len(r.content) < 100:
            return None
        tipe = "image/jpeg" if r.content[:2] == b"\xff\xd8" else (
            "image/png" if r.content[:4] == b"\x89PNG" else "image/webp")
        data = (tipe, r.content)
        if len(img_cache) < 300:
            img_cache[target] = data
        return data
    except Exception:
        return None


def ambil_video(target):
    if target in img_cache:
        return img_cache[target]
    host = urlparse(target).netloc
    if not any(h in host for h in ("fbcdn.net", "akamaihd.net")):
        return None
    try:
        r = requests.get(target, timeout=30, headers={
            "User-Agent": mesin.UA, "Referer": "https://www.facebook.com/"})
        if r.status_code != 200 or len(r.content) < 1000:
            return None
        ctype = r.headers.get("Content-Type", "video/mp4").split(";")[0]
        if not ctype.startswith("video/"):
            ctype = "video/mp4"
        data = (ctype, r.content)
        if len(img_cache) < 300:
            img_cache[target] = data
        return data
    except Exception:
        return None


def supa_aktif():
    return bool(SUPA_URL and SUPA_KEY)


def supa(method, table, payload=None, params="", prefer="return=minimal"):
    """Panggil PostgREST Supabase (best-effort, tidak pernah melempar error).
    Return (ok, data)."""
    if not supa_aktif():
        return False, None
    try:
        r = requests.request(
            method, f"{SUPA_URL}/rest/v1/{table}{params}",
            json=payload,
            headers={"apikey": SUPA_KEY, "Authorization": "Bearer " + SUPA_KEY,
                     "Content-Type": "application/json", "Prefer": prefer},
            timeout=15)
        data = None
        if r.content:
            try:
                data = r.json()
            except Exception:
                data = None
        return r.status_code in (200, 201, 204), data
    except Exception:
        return False, None


def hash_pw_lama(pw):
    """Format v0.7 (SHA-256 + garam tetap) — hanya utk verifikasi akun lama."""
    return hashlib.sha256(("intai#" + pw).encode("utf-8")).hexdigest()


def hash_pw(pw):
    """v0.8: PBKDF2-SHA256 100rb iterasi + salt acak per pengguna → 'pbkdf2$salt$hash'.
    Muat di kolom intai_akun.hash yang sama (tanpa ubah tabel)."""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 100000).hex()
    return "pbkdf2$" + salt + "$" + h


def cek_pw(pw, tersimpan):
    """Return (cocok, perlu_upgrade)."""
    tersimpan = str(tersimpan or "")
    if tersimpan.startswith("pbkdf2$"):
        try:
            _, salt, h = tersimpan.split("$", 2)
            uji = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 100000).hex()
            return hmac.compare_digest(uji, h), False
        except Exception:
            return False, False
    cocok = bool(tersimpan) and hmac.compare_digest(hash_pw_lama(pw), tersimpan)
    return cocok, cocok


def simpan_data():
    """Simpan riwayat scan + delta + akun ke intai_data.json (tulis atomik: .tmp -> rename)."""
    with lock:
        d = {
            "versi": VERSI,
            "riwayat": RIWAYAT,
            "last_seen": [{"pemilik": k[0], "kata": k[1], "mode": k[2], "ids": v}
                          for k, v in LAST_SEEN.items()],
            "akun": AKUN_DB,
        }
    try:
        with open(BERKAS_DATA + ".tmp", "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(BERKAS_DATA + ".tmp", BERKAS_DATA)
    except Exception:
        pass


def muat_data():
    """Muat data saat startup: intai_data.json, lalu bila Supabase terpasang,
    Supabase jadi sumber utama (menimpa). Kalau tabel Supabase masih kosong tapi
    file lokal berisi, file lokal di-seed ke Supabase — riwayat lama ikut pindah."""
    try:
        with open(BERKAS_DATA, "r", encoding="utf-8") as f:
            lokal = json.load(f)
    except Exception:
        lokal = {}

    def terapkan_lokal():
        with lock:
            RIWAYAT.extend(lokal.get("riwayat") or [])
            for e in lokal.get("last_seen") or []:
                try:
                    LAST_SEEN[(e.get("pemilik") or "", e["kata"], e["mode"])] = ke_indeks(e.get("ids"))
                except Exception:
                    pass
            for email, a in (lokal.get("akun") or {}).items():
                if isinstance(a, dict) and a.get("hash"):
                    AKUN_DB[email] = {"nama": a.get("nama") or email, "hash": a["hash"]}

    if not supa_aktif():
        terapkan_lokal()
        return

    # --- riwayat ---
    ok, rows = supa("GET", "intai_riwayat",
                    params="?select=pemilik,kata,mode,waktu,jumlah,baru,hilang&order=id.desc&limit=1000")
    if not ok:   # v1.4: kolom pemilik belum ada (SUPABASE-AKUN.sql belum dijalankan)
        ok, rows = supa("GET", "intai_riwayat",
                        params="?select=kata,mode,waktu,jumlah,baru,hilang&order=id.desc&limit=1000")
    if ok and isinstance(rows, list) and rows:
        with lock:
            RIWAYAT.clear()
            for r in reversed(rows):  # v1.2: TERBARU (dulu id.asc → setelah >1000 baris yg termuat malah yg terlama)
                b = {k: r.get(k) for k in ("kata", "mode", "waktu", "jumlah", "baru", "hilang")}
                b["pemilik"] = r.get("pemilik") or ""
                RIWAYAT.append(b)
    else:
        terapkan_lokal()
        if lokal.get("riwayat"):
            supa("POST", "intai_riwayat", payload=lokal["riwayat"])  # seed massal

    # --- last_seen ---
    ok, ls = supa("GET", "intai_last_seen", params="?select=pemilik,kata,mode,ids&limit=1000")
    if not ok:
        ok, ls = supa("GET", "intai_last_seen", params="?select=kata,mode,ids&limit=1000")
    if ok and isinstance(ls, list) and ls:
        with lock:
            LAST_SEEN.clear()
            for e in ls:
                try:
                    LAST_SEEN[(e.get("pemilik") or "", e["kata"], e["mode"])] = ke_indeks(e.get("ids"))
                except Exception:
                    pass
    elif lokal.get("last_seen"):
        supa("POST", "intai_last_seen", payload=lokal["last_seen"],
             prefer="return=minimal,resolution=merge-duplicates")

    # --- akun ---
    ok, ak = supa("GET", "intai_akun", params="?select=email,nama,hash&limit=10000")
    if ok and isinstance(ak, list) and ak:
        with lock:
            AKUN_DB.clear()
            for a in ak:
                AKUN_DB[a["email"]] = {"nama": a.get("nama") or a["email"], "hash": a["hash"]}
    elif lokal.get("akun"):
        supa("POST", "intai_akun",
             payload=[{"email": e, "nama": a.get("nama") or e, "hash": a["hash"]}
                      for e, a in lokal["akun"].items() if isinstance(a, dict) and a.get("hash")],
             prefer="return=minimal,resolution=merge-duplicates")


def jantung_terowongan():
    """Keepalive internal: ping alamat publik (alamat-publik.txt) tiap 20 dtk
    agar koneksi terowongan tidak menganggur lalu putus. Thread daemon —
    hidup & mati bersama server, tak bisa hilang seperti proses terpisah."""
    while True:
        try:
            with open(os.path.join(BASE_DIR, "alamat-publik.txt"), "r", encoding="utf-8") as f:
                urls = [u.strip() for u in f if u.strip().startswith("https://")]
            for u in urls:  # jaga SEMUA terowongan tetap hangat
                requests.get(u + "/api/versi", timeout=10)
        except Exception:
            pass
        time.sleep(20)


def catat_scan(label, mode, jumlah, indeks, pemilik=""):
    """Catat hasil scan ke riwayat mesin (sumber data dashboard /api/ringkasan).
    delta baru/hilang dihitung dari perbandingan archive_id vs scan sebelumnya
    dengan kata/mode yang sama. v0.8: indeks = {archive_id: {"h", "hr"}} →
    return `sejak_lalu` (nama yg baru/hilang + harga yg berubah) atau None (scan pertama)."""
    indeks = ke_indeks(indeks)
    with lock:
        kunci = (pemilik, label, mode)   # v1.4: "sejak scan terakhir" = scan terakhir AKUN INI
        prev = LAST_SEEN.get(kunci)
        waktu_lalu = jumlah_lalu = None
        for r in reversed(RIWAYAT):
            if r.get("kata") == label and r.get("mode") == mode and (r.get("pemilik") or "") == pemilik:
                waktu_lalu, jumlah_lalu = r.get("waktu"), r.get("jumlah")
                break
        sejak_lalu = None
        if prev:
            baru_id = [k for k in indeks if k not in prev]
            hilang_id = [k for k in prev if k not in indeks]
            harga_ubah = [{"h": indeks[k].get("h"), "lama": prev[k].get("hr"), "baru": indeks[k].get("hr")}
                          for k in indeks if k in prev
                          and prev[k].get("hr") and indeks[k].get("hr")
                          and prev[k].get("hr") != indeks[k].get("hr")]
            unik = lambda xs: list(dict.fromkeys(x for x in xs if x))
            sejak_lalu = {
                "waktu_lalu": waktu_lalu, "scan_lalu": jumlah_lalu,
                "baru": len(baru_id), "hilang": len(hilang_id), "harga_berubah": len(harga_ubah),
                "baru_nama": unik(indeks[k].get("h") for k in baru_id)[:5],
                "hilang_nama": unik(prev[k].get("h") for k in hilang_id)[:5],
                "harga_detail": harga_ubah[:5],
                # snapshot lama (v0.7) belum menyimpan nama/harga → keterangan jujur di UI
                "lama_tanpa_detail": not any(v for v in prev.values()),
            }
        baru = sejak_lalu["baru"] if sejak_lalu else 0
        hilang = sejak_lalu["hilang"] if sejak_lalu else 0
        LAST_SEEN[kunci] = indeks
        RIWAYAT.append({
            "kata": label, "mode": mode,
            "waktu": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC",
            "jumlah": jumlah, "baru": baru, "hilang": hilang, "pemilik": pemilik,
        })
        waktu_ini = RIWAYAT[-1]["waktu"]
        if len(RIWAYAT) > 5000:
            del RIWAYAT[:len(RIWAYAT) - 5000]
    simpan_data()
    if supa_aktif():
        pm = {"pemilik": pemilik} if akun_siap() else {}
        supa("POST", "intai_riwayat", payload=dict({
            "kata": label, "mode": mode, "waktu": waktu_ini,
            "jumlah": jumlah, "baru": baru, "hilang": hilang}, **pm))
        supa("POST", "intai_last_seen", payload=dict({"kata": label, "mode": mode, "ids": indeks}, **pm),
             prefer="return=minimal,resolution=merge-duplicates")
    return sejak_lalu


# ======================= ARSIP v1.0 (P1): database yang tumbuh =======================
# Tiap scan: semua iklan (aktif & mati) + pengiklan + potret angka niche disimpan.
# Supabase bila terpasang (tabel: SUPABASE-ARSIP.sql), kalau tidak → intai_arsip.json lokal.
# Best-effort & di thread latar: arsip gagal TIDAK boleh menggagalkan scan.
BERKAS_ARSIP = os.path.join(BASE_DIR, "intai_arsip.json")
ARSIP_MAKS_LOKAL = 20000
kunci_arsip = threading.Lock()
ARSIP_LOKAL = {"dimuat": False, "iklan": {}, "pengiklan": {}, "snapshot": [], "peristiwa": [], "pantau": {}, "setelan": {}}
ARSIP_STATUS = {"terakhir_ok": None, "terakhir_galat": "", "terakhir_waktu": None, "antri": 0, "peristiwa_terakhir": 0}
_ARSIP_CACHE = {"t": 0.0, "data": None}


def _iso_kini():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(v, bawaan=None):
    try:
        if v is None or v == "":
            return bawaan
        return int(round(float(str(v).replace(",", "."))))
    except Exception:
        return bawaan


def _teks(v, maks):
    t = str(v or "").strip()
    return t[:maks] if t else None


def _domain(url):
    try:
        h = (urlparse(url or "").hostname or "").lower()
        return h[4:] if h.startswith("www.") else (h or None)
    except Exception:
        return None


def _hook(teks):
    for baris in str(teks or "").splitlines():
        b = baris.strip()
        if len(b) >= 4:
            return b[:140]
    return None


def _tgl_dari_id(s_):
    """'8 Jun 2026' (format fmt_epoch) -> '2026-06-08'. Selain itu None."""
    m = re.fullmatch(r"(\d{1,2}) (\w{3}) (\d{4})", str(s_ or "").strip())
    if not m or m.group(2) not in BULAN:
        return None
    return f"{int(m.group(3)):04d}-{BULAN.index(m.group(2)) + 1:02d}-{int(m.group(1)):02d}"


def _tgl(s_):
    try:
        return datetime.strptime(str(s_)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def baris_arsip_iklan(x, label, mode, kini):
    """1 iklan hasil scan → 1 baris intai_iklan (kunci & tipe SELALU sama: syarat upsert massal PostgREST)."""
    det = x.get("detail") or {}
    aid = str(det.get("archive_id") or "").strip()
    if not aid:
        return None
    tgl = str(x.get("tgl") or det.get("mulai") or "")
    teks = det.get("body") or det.get("title") or bersih_tpl(x.get("teks")) or ""
    link = x.get("link") or det.get("link_url") or ""
    rel = x.get("relevansi") or {}
    thumb = x.get("thumb") or ""
    return {
        "archive_id": aid,
        "page_id": _teks(det.get("page_id"), 40),
        "halaman": _teks(x.get("halaman") or det.get("page_name"), 160),
        "niche": _teks(label, 120),
        "mode": _teks(mode, 20),
        "aktif": (x.get("aktif") is not False),
        "mulai": tgl if re.fullmatch(r"\d{4}-\d{2}-\d{2}", tgl) else None,
        "umur": _int(x.get("umur")),
        "format": _teks(x.get("format"), 30),
        "funnel": _teks(x.get("funnel"), 30),
        "platform": _teks(x.get("platform"), 80),
        "cta": _teks(det.get("cta_text"), 60),
        "hook": _hook(teks),
        "teks": _teks(teks, 600),
        "link": _teks(link, 400),
        "domain": _teks(_domain(link), 120),
        "harga_min": _int(x.get("harga_min")),
        "harga_max": _int(x.get("harga_max")),
        "status": _teks(x.get("status"), 20),
        "skor": _int(x.get("skor")),
        "skor_jual": (round(float(x["jual"]), 1) if isinstance(x.get("jual"), (int, float)) else None),
        "varian": _int(x.get("varian")),
        "relevansi": _teks(rel.get("label"), 20),
        "rel_nilai": _int(x.get("rel_nilai", rel.get("nilai"))),
        "est_min": _int(x.get("est_min")),
        "est_max": _int(x.get("est_max")),
        # URL gambar fbcdn kedaluwarsa dlm hitungan hari — disimpan sbg petunjuk saja, bukan arsip gambar
        "thumb": _teks(thumb, 900) if thumb.startswith("http") else None,
        "berakhir": (_tgl_dari_id(det.get("berakhir")) if x.get("aktif") is False else None),
        "terakhir_terlihat": kini,
    }


def baris_arsip_pengiklan(out, label, kini):
    grup = {}
    for x in out:
        det = x.get("detail") or {}
        pid = str(det.get("page_id") or "").strip()
        if not pid:
            continue
        g_ = grup.setdefault(pid, {
            "page_id": pid,
            "nama": _teks(x.get("halaman") or det.get("page_name"), 160),
            "profil": _teks(det.get("page_profile_uri"), 300),
            "likes": _int(det.get("page_like_count")),
            "niche_terakhir": _teks(label, 120),
            "iklan_aktif_scan": 0, "iklan_scan": 0,
            "terakhir_terlihat": kini,
        })
        g_["iklan_scan"] += 1
        if x.get("aktif") is not False:
            g_["iklan_aktif_scan"] += 1
        if g_["likes"] is None:
            g_["likes"] = _int(det.get("page_like_count"))
        if not g_["profil"]:
            g_["profil"] = _teks(det.get("page_profile_uri"), 300)
    return list(grup.values())


def ringkas_snapshot(out, label, mode, jumlah, ekonomi_niche, kini):
    aktif = [x for x in out if x.get("aktif") is not False]
    hidup = [x for x in aktif if (x.get("relevansi") or {}).get("label") != "Lepas"] or aktif
    med = lambda xs: (sorted(xs)[len(xs) // 2] if xs else None)
    harga = [x["harga_min"] for x in hidup if isinstance(x.get("harga_min"), (int, float)) and x["harga_min"] > 0]
    hitung = lambda kunci: {k: v for k, v in sorted(
        ((k, sum(1 for x in hidup if (x.get(kunci) or "—") == k)) for k in {x.get(kunci) or "—" for x in hidup}),
        key=lambda kv: -kv[1])}
    return {
        "niche": _teks(label, 120) or "-", "mode": _teks(mode, 20) or "cari", "waktu": kini,
        "jumlah": _int(jumlah, 0), "aktif": len(aktif), "mati": len(out) - len(aktif), "relevan": len(hidup),
        "pengiklan": len({str((x.get("detail") or {}).get("page_id") or x.get("halaman")) for x in hidup}),
        "baru_7h": sum(1 for x in hidup if (x.get("umur") or 0) <= 7),
        "menang": sum(1 for x in hidup if x.get("status") == "MENANG"),
        "umur_median": med([_int(x.get("umur"), 0) for x in hidup]),
        "harga_median": med([int(h) for h in harga]), "harga_titik": len(harga),
        "belanja_min": _int((ekonomi_niche or {}).get("bulanan_min")),
        "belanja_max": _int((ekonomi_niche or {}).get("bulanan_max")),
        "extra": {"status": hitung("status"), "format": hitung("format"), "funnel": hitung("funnel")},
    }


def _muat_arsip_lokal():
    if ARSIP_LOKAL["dimuat"]:
        return
    ARSIP_LOKAL["dimuat"] = True
    try:
        with open(BERKAS_ARSIP, "r", encoding="utf-8") as f:
            d = json.load(f)
        for k, bawaan in (("iklan", {}), ("pengiklan", {}), ("snapshot", []), ("peristiwa", []), ("pantau", {}), ("setelan", {})):
            ARSIP_LOKAL[k] = d.get(k) or bawaan
    except Exception:
        pass


def _tulis_arsip_lokal():
    try:
        with open(BERKAS_ARSIP + ".tmp", "w", encoding="utf-8") as f:
            json.dump({k: ARSIP_LOKAL[k] for k in ("iklan", "pengiklan", "snapshot", "peristiwa", "pantau", "setelan")},
                      f, ensure_ascii=False)
        os.replace(BERKAS_ARSIP + ".tmp", BERKAS_ARSIP)
        return True, ""
    except Exception as e:
        return False, str(e)[:120]


def _simpan_arsip_lokal(iklan, pengiklan, snap, kini, peristiwa=None, pid_scan=None):
    _muat_arsip_lokal()
    for tabel, baris, kunci in (("iklan", iklan, "archive_id"), ("pengiklan", pengiklan, "page_id")):
        db = ARSIP_LOKAL[tabel]
        for b in baris:
            lama = db.get(b[kunci]) or {}
            gabung = dict(lama)
            gabung.update(b)   # seperti merge-duplicates: hanya kolom yg dikirim yg berubah
            gabung["pertama_terlihat"] = lama.get("pertama_terlihat") or kini
            db[b[kunci]] = gabung
    ARSIP_LOKAL["snapshot"].append(snap)
    ARSIP_LOKAL["snapshot"] = ARSIP_LOKAL["snapshot"][-2000:]
    if peristiwa:
        ARSIP_LOKAL["peristiwa"].extend(peristiwa)
        ARSIP_LOKAL["peristiwa"] = ARSIP_LOKAL["peristiwa"][-5000:]
    if pid_scan:
        for v_pn in ARSIP_LOKAL["pantau"].values():
            if str(v_pn.get("page_id")) == pid_scan:
                v_pn["terakhir_cek"] = kini
    if len(ARSIP_LOKAL["iklan"]) > ARSIP_MAKS_LOKAL:  # buang yg paling lama tak terlihat
        urut = sorted(ARSIP_LOKAL["iklan"].items(), key=lambda kv: kv[1].get("terakhir_terlihat") or "")
        ARSIP_LOKAL["iklan"] = dict(urut[-ARSIP_MAKS_LOKAL:])
    return _tulis_arsip_lokal()


# ---------- P2: deteksi perubahan (hanya yg BENAR-BENAR teramati antar dua scan) ----------
_P2 = {"ok": None, "t": 0.0}


def p2_siap():
    """Tabel/kolom P2 (SUPABASE-PESAING.sql) sudah ada? Positif di-cache permanen, negatif 2 menit."""
    if not supa_aktif():
        return True
    if _P2["ok"] or (_P2["ok"] is False and time.time() - _P2["t"] < 120):
        return bool(_P2["ok"])
    ok1, _ = supa("GET", "intai_iklan", params="?select=berakhir&limit=1")
    ok2, _ = supa("GET", "intai_peristiwa", params="?select=id&limit=1")
    ok3, _ = supa("GET", "intai_pantau", params="?select=page_id&limit=1")
    _P2.update({"ok": bool(ok1 and ok2 and ok3), "t": time.time()})
    return _P2["ok"]


def _supa_in(tabel, kolom, nilai, select, ekstra=""):
    """GET ...?kolom=in.(a,b,..) per 100 nilai. Return (ok, baris). Nilai non-ID dibuang."""
    nilai = [v for v in dict.fromkeys(str(v) for v in nilai if v) if re.fullmatch(r"[0-9A-Za-z_.-]{1,64}", v)]
    hasil = []
    for i in range(0, len(nilai), 100):
        ok, r = supa("GET", tabel, params=f"?select={select}&{kolom}=in.({','.join(nilai[i:i + 100])}){ekstra}")
        if not ok or not isinstance(r, list):
            return False, []
        hasil.extend(r)
    return True, hasil


def deteksi_peristiwa(iklan, lama_iklan, lama_pg, kini):
    """lama_iklan {archive_id: {aktif, niche}}, lama_pg {page_id: terakhir_terlihat}.
    'mati'/'hidup' = status aktif berubah dibanding arsip. 'baru' = iklan belum pernah terarsip,
    pengiklannya SUDAH dikenal, dan tanggal mulai (Meta) >= hari terakhir pengiklan itu terlihat.
    Pengiklan yang baru pertama kali terarsip tidak menghasilkan 'baru' (bukan perubahan)."""
    ev = []
    for b in iklan:
        aid, pid, aktif = b["archive_id"], b.get("page_id"), b.get("aktif")
        lama = lama_iklan.get(aid)
        jenis = None
        if lama is not None:
            if lama.get("aktif") is True and aktif is False:
                jenis = "mati"
            elif lama.get("aktif") is False and aktif is True:
                jenis = "hidup"
        elif aktif and pid and pid in lama_pg and b.get("mulai"):
            t_lalu = str(lama_pg.get(pid) or "")[:10]
            if t_lalu and b["mulai"] >= t_lalu:
                jenis = "baru"
        if jenis:
            ev.append({"page_id": pid, "archive_id": aid, "jenis": jenis, "waktu": kini,
                       "halaman": b.get("halaman"), "niche": b.get("niche") or (lama or {}).get("niche"),
                       "hook": b.get("hook"), "mulai": b.get("mulai"), "berakhir": b.get("berakhir"),
                       "format": b.get("format"), "funnel": b.get("funnel"), "harga_min": b.get("harga_min")})
    return ev


def arsipkan_scan(out, label, mode, jumlah, ekonomi_niche):
    """Dipanggil di thread latar sesudah scan. Return (ok, galat)."""
    kini = _iso_kini()
    n_ev = 0
    try:
        iklan, lihat = [], set()
        for x in out:
            b = baris_arsip_iklan(x, label, mode, kini)
            if b and b["archive_id"] not in lihat:
                lihat.add(b["archive_id"])
                iklan.append(b)
        pengiklan = baris_arsip_pengiklan(out, label, kini)
        snap = ringkas_snapshot(out, label, mode, jumlah, ekonomi_niche, kini)
        pid_scan = None
        if mode == "halaman":
            m = re.search(r"(\d+)", str(label or ""))
            pid_scan = m.group(1) if m else None
            # Intip Kompetitor bukan niche: jangan timpa niche/mode hasil scan kata kunci.
            # Kunci dibuang dari SEMUA baris (syarat upsert massal) → merge-duplicates tak menyentuh kolom itu.
            for b in iklan:
                b.pop("niche", None)
                b.pop("mode", None)
            for b in pengiklan:
                b.pop("niche_terakhir", None)
        with kunci_arsip:
            if supa_aktif():
                galat = []
                gabung = "return=minimal,resolution=merge-duplicates"
                p2 = p2_siap()
                ev = []
                if p2:
                    ok_a, la = _supa_in("intai_iklan", "archive_id", [b["archive_id"] for b in iklan],
                                        "archive_id,aktif,niche")
                    ok_b, lp = _supa_in("intai_pengiklan", "page_id", [b["page_id"] for b in pengiklan],
                                        "page_id,terakhir_terlihat")
                    if ok_a and ok_b:   # salah satu gagal dibaca → lebih baik tanpa peristiwa daripada peristiwa palsu
                        ev = deteksi_peristiwa(iklan, {str(r.get("archive_id")): r for r in la},
                                               {str(r.get("page_id")): r.get("terakhir_terlihat") for r in lp}, kini)
                else:
                    for b in iklan:
                        b.pop("berakhir", None)   # kolom belum ada (SUPABASE-PESAING.sql belum dijalankan)
                ok_ik = True
                for i in range(0, len(iklan), 200):
                    ok, d = supa("POST", "intai_iklan", payload=iklan[i:i + 200], prefer=gabung)
                    if not ok:
                        ok_ik = False
                        galat.append("iklan: " + str(d)[:160])
                        break
                if pengiklan:
                    ok, d = supa("POST", "intai_pengiklan", payload=pengiklan, prefer=gabung)
                    if not ok:
                        galat.append("pengiklan: " + str(d)[:160])
                ok, d = supa("POST", "intai_snapshot", payload=snap)
                if not ok:
                    galat.append("snapshot: " + str(d)[:160])
                if p2 and ok_ik and ev:
                    for i in range(0, len(ev), 200):
                        ok, d = supa("POST", "intai_peristiwa", payload=ev[i:i + 200])
                        if not ok:
                            galat.append("peristiwa: " + str(d)[:160])
                            break
                    else:
                        n_ev = len(ev)
                if p2 and pid_scan:
                    supa("PATCH", "intai_pantau", payload={"terakhir_cek": kini}, params=f"?page_id=eq.{pid_scan}")
                hasil = (not galat, "; ".join(galat))
            else:
                _muat_arsip_lokal()
                ev = deteksi_peristiwa(iklan, ARSIP_LOKAL["iklan"],
                                       {k: v.get("terakhir_terlihat") for k, v in ARSIP_LOKAL["pengiklan"].items()},
                                       kini)
                hasil = _simpan_arsip_lokal(iklan, pengiklan, snap, kini, ev, pid_scan)
                n_ev = len(ev) if hasil[0] else 0
    except Exception as e:
        hasil = (False, "arsip: " + str(e)[:160])
    finally:
        ARSIP_STATUS["antri"] = max(0, ARSIP_STATUS.get("antri", 1) - 1)
    ARSIP_STATUS.update({"terakhir_ok": hasil[0], "terakhir_galat": hasil[1], "terakhir_waktu": kini,
                         "peristiwa_terakhir": n_ev})
    _ARSIP_CACHE["t"] = 0.0
    return hasil


def supa_hitung(tabel, filter_=""):
    """Jumlah baris via header Content-Range (Prefer: count=exact). None = tabel tak ada / galat."""
    if not supa_aktif():
        return None
    try:
        r = requests.get(f"{SUPA_URL}/rest/v1/{tabel}?select=*{filter_}",
                         headers={"apikey": SUPA_KEY, "Authorization": "Bearer " + SUPA_KEY,
                                  "Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
                         timeout=15)
        cr = r.headers.get("content-range") or r.headers.get("Content-Range") or ""
        if r.status_code in (200, 206) and "/" in cr and cr.split("/")[-1].isdigit():
            return int(cr.split("/")[-1])
    except Exception:
        pass
    return None


def statistik_arsip():
    """Ringkasan isi arsip utk dashboard (cache 30 dtk agar hemat panggilan Supabase)."""
    if _ARSIP_CACHE["data"] and time.time() - _ARSIP_CACHE["t"] < 30:
        return _ARSIP_CACHE["data"]
    if supa_aktif():
        n_iklan = supa_hitung("intai_iklan")
        siap = n_iklan is not None
        d = {"ok": True, "sumber": "supabase", "siap": siap}
        if siap:
            d.update({
                "iklan": n_iklan,
                "iklan_aktif": supa_hitung("intai_iklan", "&aktif=is.true") or 0,
                "pengiklan": supa_hitung("intai_pengiklan") or 0,
                "snapshot": supa_hitung("intai_snapshot") or 0,
            })
            ok, r = supa("GET", "intai_snapshot", params="?select=waktu&order=waktu.asc&limit=1")
            d["sejak"] = r[0]["waktu"] if ok and isinstance(r, list) and r else None
            ok, r = supa("GET", "intai_snapshot", params="?select=niche&mode=neq.halaman&limit=5000")
            d["niche"] = len({x.get("niche") for x in r}) if ok and isinstance(r, list) else 0
            d["p2_siap"] = p2_siap()
            if d["p2_siap"]:
                d["pantau"] = supa_hitung("intai_pantau") or 0
                d["peristiwa"] = supa_hitung("intai_peristiwa") or 0
    else:
        with kunci_arsip:
            _muat_arsip_lokal()
            ik = ARSIP_LOKAL["iklan"]
            sn = ARSIP_LOKAL["snapshot"]
            d = {"ok": True, "sumber": "lokal", "siap": True, "iklan": len(ik),
                 "iklan_aktif": sum(1 for v in ik.values() if v.get("aktif")),
                 "pengiklan": len(ARSIP_LOKAL["pengiklan"]), "snapshot": len(sn),
                 "sejak": (sn[0].get("waktu") if sn else None),
                 "niche": len({x.get("niche") for x in sn if x.get("mode") != "halaman"}),
                 "p2_siap": True, "pantau": len(ARSIP_LOKAL["pantau"]), "peristiwa": len(ARSIP_LOKAL["peristiwa"])}
    try:
        d["jadwal"] = None   # v1.4: diisi per akun di handler (/api/arsip)
    except Exception:
        d["jadwal"] = {"siap": False}
    d["status_terakhir"] = dict(ARSIP_STATUS)
    _ARSIP_CACHE.update({"t": time.time(), "data": d})
    return d


# ======================= GALERI v1.3 (P5): jelajah semua iklan terarsip =======================
# Filter di server atas seluruh arsip (cache 45 dtk; segar otomatis tiap arsip bertambah).
# Label: 🟢 tanggal/status asli Meta · 🔵 skor & status model INTAI · 🟠 estimasi belanja (model).
GALERI_KOLOM = ("archive_id,page_id,halaman,niche,mode,aktif,mulai,umur,format,funnel,cta,hook,domain,"
                "harga_min,harga_max,status,skor,relevansi,est_min,est_max,thumb,pertama_terlihat,terakhir_terlihat")
GALERI_MAKS = 8000
GALERI_CACHE = {"t": 0.0, "versi": None, "rows": None, "ok": False}
kunci_galeri = threading.Lock()


def _galeri_semua():
    """Return (ok, rows). Semua baris intai_iklan (maks GALERI_MAKS), kolom ringkas."""
    versi = ARSIP_STATUS.get("terakhir_waktu")
    with kunci_galeri:
        if GALERI_CACHE["rows"] is not None and GALERI_CACHE["versi"] == versi and time.time() - GALERI_CACHE["t"] < 45:
            return GALERI_CACHE["ok"], GALERI_CACHE["rows"]
        if supa_aktif():
            kol = GALERI_KOLOM + (",berakhir" if p2_siap() else "")
            rows, ok = [], True
            for off in range(0, GALERI_MAKS, 1000):
                o, r = supa("GET", "intai_iklan", params=f"?select={kol}&order=archive_id.asc&limit=1000&offset={off}")
                if not o or not isinstance(r, list):
                    ok = bool(rows)
                    break
                rows += r
                if len(r) < 1000:
                    break
        else:
            with kunci_arsip:
                _muat_arsip_lokal()
                rows, ok = [dict(v) for v in ARSIP_LOKAL["iklan"].values()], True
        for r_ in rows:
            _bersih_baris_arsip(r_)
        GALERI_CACHE.update({"t": time.time(), "versi": versi, "rows": rows, "ok": ok})
        return ok, rows


def pantauan_status(ids_teks):
    """v1.3.1 Pantauan: status terbaru per archive_id dari arsip (pakai cache galeri, maks 200 id)."""
    ids = [re.sub(r"\D", "", x)[:40] for x in str(ids_teks or "").split(",")]
    ids = [x for x in dict.fromkeys(ids) if x][:200]
    if not ids:
        return {"ok": True, "iklan": {}, "tidak_ada": []}
    ok, rows = _galeri_semua()
    hari = _hari_ini()
    cari = set(ids)
    peta = {}
    for r in rows:
        k = str(r.get("archive_id") or "")
        if k in cari:
            peta[k] = r
    return {"ok": bool(ok), "iklan": {i: _galeri_baris(peta[i], hari) for i in ids if i in peta},
            "tidak_ada": [i for i in ids if i not in peta]}


def _hari_ini():
    return datetime.now(timezone.utc).date()


def _galeri_baris(r, hari):
    b = _baris_profil(r, hari)
    b.update({"halaman": r.get("halaman"), "page_id": r.get("page_id"), "thumb": r.get("thumb"),
              "relevansi": r.get("relevansi"), "mode": r.get("mode")})
    return b


def _umur_atau_durasi(r, hari):
    return _umur_kini(r, hari) if r.get("aktif") is True else _durasi(r)


GALERI_PRESET = {
    # kunci: (label, saring(r, hari) -> bool, urutan bawaan)
    "tahan":  ("Tahan lama ≥30 hari", lambda r, h: r.get("aktif") is True and (_umur_kini(r, h) or 0) >= 30, "umur"),
    "baru":   ("Baru 7 hari", lambda r, h: r.get("aktif") is True and bool(_tgl(r.get("mulai")))
               and (h - _tgl(r.get("mulai"))).days < 7, "mulai"),
    "menang": ("Status MENANG", lambda r, h: r.get("aktif") is True and r.get("status") == "MENANG", "skor"),
    "video":  ("Video aktif", lambda r, h: r.get("aktif") is True and r.get("format") == "Video", "umur"),
    "harga":  ("Ada harga", lambda r, h: (_int(r.get("harga_min"), 0) or 0) > 0, "harga"),
    "wa":     ("Jualan via WA", lambda r, h: r.get("funnel") == "CTWA/WA", "umur"),
    "gagal":  ("Gagal cepat ≤7 hari", lambda r, h: r.get("aktif") is False and (_durasi(r) or 99) <= 7, "berakhir"),
    "semua":  ("Semua iklan", lambda r, h: True, "dilihat"),
}
GALERI_URUT = {
    "umur": lambda r, h: -(_umur_atau_durasi(r, h) or 0),
    "mulai": lambda r, h: -int(str(r.get("mulai") or "0000-00-00").replace("-", "") or 0),
    "skor": lambda r, h: -(_int(r.get("skor"), 0) or 0),
    "belanja": lambda r, h: -(_int(r.get("est_max"), 0) or 0),
    "harga": lambda r, h: (_int(r.get("harga_min"), 0) or 10 ** 12),
    "berakhir": lambda r, h: -int(str(r.get("berakhir") or "0000-00-00").replace("-", "") or 0),
    "dilihat": lambda r, h: "".join(chr(255 - ord(c)) for c in str(r.get("terakhir_terlihat") or "")),
}


def _galeri_saring(rows, p, hari):
    q = re.sub(r"\s+", " ", str(p.get("cari") or "")).strip().lower()[:80]
    niche, fmt, fun, st = (str(p.get(k) or "").strip() for k in ("niche", "format", "funnel", "status"))
    aktif = str(p.get("aktif") or "")
    lepas = str(p.get("lepas") or "") == "1"
    out = []
    for r in rows:
        if not lepas and r.get("relevansi") == "Lepas":
            continue
        if niche == "__pesaing":   # iklan yg hanya terlihat lewat scan halaman pesaing (niche kosong, aturan P1)
            if r.get("niche"):
                continue
        elif niche and r.get("niche") != niche:
            continue
        if fmt and r.get("format") != fmt:
            continue
        if fun and r.get("funnel") != fun:
            continue
        if st and r.get("status") != st:
            continue
        if aktif == "aktif" and r.get("aktif") is not True:
            continue
        if aktif == "mati" and r.get("aktif") is not False:
            continue
        if q:
            gabung = " ".join(str(r.get(k) or "") for k in ("hook", "halaman", "domain", "niche", "cta")).lower()
            if not all(kata in gabung for kata in q.split(" ")):
                continue
        out.append(r)
    return out


def galeri_iklan(p):
    """p: dict parameter (preset, cari, niche, format, funnel, status, aktif, lepas, urut, hal, per)."""
    ok, rows = _galeri_semua()
    if not ok and not rows:
        return {"ok": True, "siap": False}
    hari = _hari_ini()
    dasar = _galeri_saring(rows, p, hari)
    hitung = {k: sum(1 for r in dasar if f(r, hari)) for k, (_, f, _u) in GALERI_PRESET.items()}
    preset = str(p.get("preset") or "")
    if preset not in GALERI_PRESET:
        preset = "tahan" if hitung["tahan"] >= 6 else "semua"
    label, saring, urut0 = GALERI_PRESET[preset]
    hasil = [r for r in dasar if saring(r, hari)]
    urut = str(p.get("urut") or "") or urut0
    if urut not in GALERI_URUT:
        urut = urut0
    hasil.sort(key=lambda r: GALERI_URUT[urut](r, hari))
    per = min(60, max(6, _int(p.get("per"), 24) or 24))
    total = len(hasil)
    n_hal = max(1, -(-total // per))
    hal = min(n_hal, max(1, _int(p.get("hal"), 1) or 1))
    bukan_lepas = [r for r in rows if r.get("relevansi") != "Lepas"]

    def faset(k):
        c = {}
        for r in bukan_lepas:
            v = r.get(k)
            if v:
                c[v] = c.get(v, 0) + 1
        return [{"v": v, "n": n} for v, n in sorted(c.items(), key=lambda kv: -kv[1])]

    nama_hal = {}
    for r in rows:
        if str(r.get("niche") or "").startswith("halaman ") and r.get("halaman"):
            nama_hal.setdefault(r["niche"], r["halaman"])
    niche_f = faset("niche")
    for e in niche_f:
        e["label"] = ("Pesaing · " + nama_hal.get(e["v"], e["v"][8:])) if e["v"].startswith("halaman ") else e["v"]
    n_tanpa = sum(1 for r in bukan_lepas if not r.get("niche"))
    if n_tanpa:
        niche_f.append({"v": "__pesaing", "n": n_tanpa, "label": "Pesaing (scan halaman)"})
    return {
        "ok": True, "siap": True, "preset": preset, "label": label, "urut": urut,
        "hitung": hitung, "total": total, "hal": hal, "n_hal": n_hal, "per": per,
        "arsip": len(rows), "lepas_disembunyikan": (0 if str(p.get("lepas") or "") == "1" else len(rows) - len(bukan_lepas)),
        "iklan": [_galeri_baris(r, hari) for r in hasil[(hal - 1) * per: hal * per]],
        "faset": {"niche": niche_f, "format": faset("format"), "funnel": faset("funnel"), "status": faset("status")},
        "preset_label": {k: v[0] for k, v in GALERI_PRESET.items()},
        "terpotong": len(rows) >= GALERI_MAKS,
    }


def galeri_csv(p):
    ok, rows = _galeri_semua()
    hari = _hari_ini()
    p = dict(p)
    preset = str(p.get("preset") or "")
    dasar = _galeri_saring(rows, p, hari)
    if preset in GALERI_PRESET:
        dasar = [r for r in dasar if GALERI_PRESET[preset][1](r, hari)]
        urut = str(p.get("urut") or "") or GALERI_PRESET[preset][2]
        if urut in GALERI_URUT:
            dasar.sort(key=lambda r: GALERI_URUT[urut](r, hari))
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["archive_id", "ad_library", "pengiklan", "page_id", "niche", "aktif", "mulai", "berakhir",
                "hari_tayang", "format", "funnel", "cta", "status_intai", "skor_intai", "harga_min", "harga_max",
                "est_belanja_bln_min", "est_belanja_bln_max", "domain", "hook"])
    for r in dasar:
        b = _galeri_baris(r, hari)
        w.writerow([b["archive_id"], "https://www.facebook.com/ads/library/?id=" + str(b["archive_id"]), b["halaman"] or "",
                    b["page_id"] or "", b["niche"] or "", "ya" if b["aktif"] else "tidak", b["mulai"] or "", b["berakhir"] or "",
                    b["hari"] if b["hari"] is not None else "", b["format"] or "", b["funnel"] or "", b["cta"] or "",
                    b["status"] or "", b["skor"] if b["skor"] is not None else "", b["harga_min"] or "", b["harga_max"] or "",
                    b["est_min"] or "", b["est_max"] or "", b["domain"] or "", (b["hook"] or "").replace("\n", " ")])
    return "\ufeff" + buf.getvalue()   # BOM → Excel membaca UTF-8 (emoji/aksen aman)


# ======================= JADWAL v1.2 (P4): scan otomatis harian + tren =======================
# Render gratis tidur bila 15 mnt tanpa kunjungan → pemicu dari luar (cron-job.org) memanggil
# /api/jadwal/tick tiap 10 mnt di jendela jam tertentu. Tick menjawab < 1 dtk; scan jalan di thread.
# Antrean + hasil disimpan per item (intai_setelan) → server tidur di tengah jalan = dilanjutkan tick berikutnya.
JADWAL_KUNCI = (os.environ.get("JADWAL_KUNCI") or "").strip()   # opsional: bila diisi, tick wajib ?kunci=
# v1.4.3: pemilik server (lihat panduan pemicu cron). Env INTAI_ADMIN boleh berisi beberapa email dipisah koma.
ADMIN_DAFTAR = [e.strip().lower() for e in (os.environ.get("INTAI_ADMIN") or "eradaneen@gmail.com").split(",") if e.strip()]
ADMIN_EMAIL = set(ADMIN_DAFTAR)


def is_admin(email):
    return bool(email) and str(email).strip().lower() in ADMIN_EMAIL
WIB = timezone(timedelta(hours=7))
NICHE_HARIAN_MAKS = 3    # v1.4: per akun
JADWAL_BAWAAN = {"aktif": False, "niche": [], "pesaing": True}
# v1.4: 1 antrean global per hari = gabungan niche + pesaing SEMUA akun yg aktif (tanpa dobel);
# tiap item mencatat pemiliknya ("u") → riwayat & tampilan per akun. Jam mulai = 1 utk semua akun
# (pemicu cron dipasang pemilik server), dibaca dari env JADWAL_JAM / setelan lama "jadwal".jam / 5.
JADWAL = {"cfgs": None, "t_cfgs": 0.0, "jam": 5, "status": None, "thread": None, "siap": None, "t_siap": 0.0}
JADWAL_SEGAR_DTK = 120   # v1.4.1: setelan per akun dibaca ulang berkala (mis. setelah dipindah lewat SQL)
kunci_jadwal = threading.RLock()


def setelan_siap():
    if not supa_aktif():
        return True
    if JADWAL["siap"] or (JADWAL["siap"] is False and time.time() - JADWAL["t_siap"] < 120):
        return bool(JADWAL["siap"])
    ok, _ = supa("GET", "intai_setelan", params="?select=kunci&limit=1")
    JADWAL.update({"siap": bool(ok), "t_siap": time.time()})
    return JADWAL["siap"]


def setelan_baca(kunci):
    """Return (ok, nilai|None). ok=False → tabel belum ada / galat baca."""
    if supa_aktif():
        if not setelan_siap():
            return False, None
        ok, r = supa("GET", "intai_setelan", params=f"?select=nilai&kunci=eq.{quote(kunci)}")
        if not ok or not isinstance(r, list):
            return False, None
        return True, (r[0].get("nilai") if r else None)
    with kunci_arsip:
        _muat_arsip_lokal()
        return True, ARSIP_LOKAL["setelan"].get(kunci)


def setelan_tulis(kunci, nilai):
    if supa_aktif():
        if not setelan_siap():
            return False
        ok, _ = supa("POST", "intai_setelan", payload={"kunci": kunci, "nilai": nilai, "diubah": _iso_kini()},
                     prefer="return=minimal,resolution=merge-duplicates")
        return ok
    with kunci_arsip:
        _muat_arsip_lokal()
        ARSIP_LOKAL["setelan"][kunci] = nilai
        return _tulis_arsip_lokal()[0]


def setelan_awalan(awalan):
    """Semua setelan yg kuncinya diawali `awalan` → (ok, {kunci: nilai})."""
    if supa_aktif():
        if not setelan_siap():
            return False, {}
        ok, r = supa("GET", "intai_setelan", params=f"?select=kunci,nilai&kunci=like.{quote(awalan)}*&limit=5000")
        if not ok or not isinstance(r, list):
            return False, {}
        return True, {x.get("kunci"): x.get("nilai") for x in r}
    with kunci_arsip:
        _muat_arsip_lokal()
        return True, {k: v for k, v in ARSIP_LOKAL["setelan"].items() if str(k).startswith(awalan)}


def _rapikan_cfg(c, ketat=False):
    """ketat=True (saat simpan): >3 niche ditolak. Saat baca setelan lama: dipotong, sisanya dilaporkan."""
    c = c if isinstance(c, dict) else {}
    niche, lihat = [], set()
    for n in (c.get("niche") or []):
        n = re.sub(r"\s+", " ", str(n or "")).strip()[:60]
        if n and n.lower() not in lihat and not re.fullmatch(r"halaman\s+\d+", n, re.I):
            lihat.add(n.lower())
            niche.append(n)
    if ketat and len(niche) > NICHE_HARIAN_MAKS:
        return None
    return {"aktif": bool(c.get("aktif")), "niche": niche[:NICHE_HARIAN_MAKS],
            "niche_lebih": niche[NICHE_HARIAN_MAKS:], "pesaing": c.get("pesaing", True) is not False}


def _muat_jadwal():
    with kunci_jadwal:
        if JADWAL["cfgs"] is None or time.time() - JADWAL["t_cfgs"] > JADWAL_SEGAR_DTK:
            ok, semua = setelan_awalan("jadwal:")
            if ok:
                JADWAL["cfgs"] = {k.split(":", 1)[1]: _rapikan_cfg(v) for k, v in semua.items() if k and ":" in k}
                JADWAL["t_cfgs"] = time.time()
            if ok and JADWAL["status"] is None:
                _, lama = setelan_baca("jadwal")   # setelan global v1.3 → hanya jam-nya yg dipakai
                jam = _int(os.environ.get("JADWAL_JAM"), None)
                if jam is None and isinstance(lama, dict):
                    jam = _int(lama.get("jam"), None)
                JADWAL["jam"] = jam if (jam is not None and 0 <= jam <= 23) else 5
                ok2, st = setelan_baca("jadwal_status")
                JADWAL["status"] = st if (ok2 and isinstance(st, dict)) else {}
        return JADWAL["cfgs"] is not None


def _cfg_akun(email):
    return dict((JADWAL["cfgs"] or {}).get(email) or _rapikan_cfg(JADWAL_BAWAAN))


def _sisa(st, email=None):
    h = st.get("hasil") or {}
    sisa = [it for it in (st.get("antrian") or [])
            if not (h.get(it["k"]) or {}).get("ok") and (h.get(it["k"]) or {}).get("coba", 0) < 2
            and (email is None or email in (it.get("u") or []))]
    sisa.sort(key=lambda it: (h.get(it["k"]) or {}).get("coba", 0))   # yg gagal sekali dicoba ulang di akhir
    return sisa


def _pesaing_semua():
    """{email: [{page_id, nama}]} — 1 query utk semua akun."""
    out = {}
    if supa_aktif():
        if not (p2_siap() and akun_siap()):
            return out
        ok, r = supa("GET", "intai_pantau", params="?select=pemilik,page_id,nama&order=ditambah.asc&limit=5000")
        for p in (r if ok and isinstance(r, list) else []):
            out.setdefault(p.get("pemilik") or "", []).append(p)
        return out
    with kunci_arsip:
        _muat_arsip_lokal()
        for v in ARSIP_LOKAL["pantau"].values():
            out.setdefault(v.get("pemilik") or "", []).append({"page_id": v.get("page_id"), "nama": v.get("nama")})
    return out


def _daftar_pesaing_ringkas(email):
    return [p for p in _pesaing_semua().get(email, []) if p.get("page_id")]


def _item_akun(email, cfg, pesaing_semua):
    q = [{"k": "cari:" + n.lower(), "mode": "cari", "q": n, "nama": n} for n in cfg["niche"]]
    if cfg["pesaing"]:
        for p in pesaing_semua.get(email, []):
            pid = re.sub(r"\D", "", str(p.get("page_id") or ""))
            if pid:
                q.append({"k": "halaman:" + pid, "mode": "halaman", "q": pid, "nama": p.get("nama") or ("Halaman " + pid)})
    return q


def _gabung_antrian(st, peserta, pesaing_semua):
    """Tambahkan item milik `peserta` ke antrean hari ini (niche dulu, lalu pesaing). Return jumlah item baru."""
    antrian = st.setdefault("antrian", [])
    per_k = {it["k"]: it for it in antrian}
    baru_niche, baru_hal = [], []
    for email, cfg in peserta.items():
        for it in _item_akun(email, cfg, pesaing_semua):
            lama = per_k.get(it["k"])
            if lama:
                if email not in lama.setdefault("u", []):
                    lama["u"].append(email)
                continue
            it["u"] = [email]
            per_k[it["k"]] = it
            (baru_niche if it["mode"] == "cari" else baru_hal).append(it)
    # niche baru disisipkan sebelum item halaman yg belum diproses → niche semua akun didahulukan
    h = st.get("hasil") or {}
    if baru_niche:
        i = next((n for n, it in enumerate(antrian) if it["mode"] == "halaman" and not (h.get(it["k"]) or {}).get("waktu")),
                 len(antrian))
        antrian[i:i] = baru_niche
    antrian.extend(baru_hal)
    return len(baru_niche) + len(baru_hal)


def _pekerja_jadwal():
    while True:
        with kunci_jadwal:
            st = JADWAL["status"]
            sisa = _sisa(st)
            if not sisa:
                st["akhir"] = _iso_kini()
                st["item_kini"] = None
                setelan_tulis("jadwal_status", st)
                JADWAL["thread"] = None   # dilepas DI DALAM kunci → tick berikutnya pasti memulai pekerja baru
                return
            it = sisa[0]
            st["item_kini"] = it["k"]
        try:
            with lock:   # kunci yang sama dengan /scan → tidak pernah dua scan bersamaan
                h = jalankan_scan(it["q"], it["mode"], pemilik=list(it.get("u") or []))
        except Exception as e:
            h = {"ok": False, "error": str(e)[:160]}
        with kunci_jadwal:
            st = JADWAL["status"]
            lama = st.setdefault("hasil", {}).get(it["k"]) or {}
            st["hasil"][it["k"]] = {
                "ok": bool(h.get("ok")), "aktif": h.get("jumlah"), "mati": h.get("jumlah_mati"),
                "galat": "" if h.get("ok") else str(h.get("error") or "gagal")[:160],
                "waktu": _iso_kini(), "coba": lama.get("coba", 0) + 1,
            }
            setelan_tulis("jadwal_status", st)
            masih = bool(_sisa(st))
        if masih:
            time.sleep(4)   # sopan ke Meta Ad Library


def jadwal_tick(paksa=False, pemicu="cron", email=None):
    """Cepat: putuskan mulai / tambah / lanjut / tidak, lalu jalankan di thread latar.
    Cron: semua akun yg aktif. paksa ("Jalankan sekarang"): hanya antrean akun `email`."""
    if not _muat_jadwal():
        return {"ok": False, "siap": False, "pesan": "Tabel setelan belum dibuat - jalankan SUPABASE-JADWAL.sql"}
    pesaing_semua = _pesaing_semua()
    with kunci_jadwal:
        kini = datetime.now(WIB)
        hari = kini.strftime("%Y-%m-%d")
        st = JADWAL["status"] or {}
        if paksa:
            peserta = {email: _cfg_akun(email)} if email else {}
        else:
            aktif = {e: c for e, c in (JADWAL["cfgs"] or {}).items() if c.get("aktif")}
            if not aktif:
                return {"ok": True, "aksi": "nonaktif", "pesan": "Scan otomatis nonaktif di semua akun"}
            if kini.hour < JADWAL["jam"]:
                return {"ok": True, "aksi": "menunggu", "pesan": f"Menunggu jam {JADWAL['jam']:02d}:00 WIB"}
            peserta = aktif
        hari_baru = st.get("tanggal") != hari
        if hari_baru:
            st = {"tanggal": hari, "pemicu": pemicu, "mulai": _iso_kini(), "akhir": None,
                  "antrian": [], "hasil": {}, "item_kini": None}
            JADWAL["status"] = st
        n_baru = _gabung_antrian(st, peserta, pesaing_semua)
        if n_baru and not hari_baru:
            st["akhir"] = None
        if paksa and email:   # gagal kemarin/tadi boleh dicoba lagi saat diminta manual
            for it in st["antrian"]:
                r = st["hasil"].get(it["k"])
                if email in it.get("u", []) and r and not r.get("ok") and r.get("coba", 0) >= 2:
                    r["coba"] = 1
        t = JADWAL["thread"]
        berjalan = bool(t and t.is_alive())
        sisa_semua = _sisa(st)
        sisa_saya = len(_sisa(st, email)) if email else len(sisa_semua)
        if hari_baru or n_baru or paksa:
            if not sisa_semua:
                st["akhir"] = st.get("akhir") or _iso_kini()
            setelan_tulis("jadwal_status", st)
        if berjalan:
            return {"ok": True, "aksi": "berjalan", "antrian": sisa_saya,
                    "pesan": "Putaran sedang berjalan" + (f" - {n_baru} scan ditambahkan" if n_baru else "")}
        if not sisa_semua:
            if not st["antrian"] or (email and not any(email in it.get("u", []) for it in st["antrian"])):
                return {"ok": True, "aksi": "kosong", "pesan": "Antrean kosong - isi niche harian atau pantau pesaing"}
            return {"ok": True, "aksi": "selesai", "pesan": "Putaran hari ini sudah selesai"}
        t = threading.Thread(target=_pekerja_jadwal, daemon=True, name="jadwal")
        JADWAL["thread"] = t
        t.start()
        aksi = "mulai" if hari_baru else ("tambah" if n_baru else "lanjut")
        return {"ok": True, "aksi": aksi, "antrian": sisa_saya,
                "pesan": {"mulai": "Putaran dimulai", "tambah": "Scan ditambahkan ke putaran hari ini",
                          "lanjut": "Putaran dilanjutkan"}[aksi]}


def jadwal_ringkas(email):
    if not _muat_jadwal():
        return {"siap": False}
    with kunci_jadwal:
        cfg, st = _cfg_akun(email), JADWAL["status"] or {}
        h = st.get("hasil") or {}
        milik = [it for it in (st.get("antrian") or []) if email in (it.get("u") or [])]
        t = JADWAL["thread"]
        return {"siap": True, "aktif": cfg["aktif"], "jam": JADWAL["jam"], "tanggal": st.get("tanggal") if milik else None,
                "total": len(milik), "ok": sum(1 for it in milik if (h.get(it["k"]) or {}).get("ok")),
                "berjalan": bool(t and t.is_alive()) and bool(_sisa(st, email)), "akhir": st.get("akhir")}


def jadwal_publik(email):
    admin = is_admin(email)
    if not _muat_jadwal():
        return {"ok": True, "siap": False, "admin": admin}
    pesaing = _daftar_pesaing_ringkas(email)
    with kunci_jadwal:
        cfg, st = _cfg_akun(email), JADWAL["status"] or {}
        t = JADWAL["thread"]
        berjalan_global = bool(t and t.is_alive())
        h = st.get("hasil") or {}
        milik = [it for it in (st.get("antrian") or []) if email in (it.get("u") or [])]
        items = [dict({"nama": it["nama"], "mode": it["mode"]}, **(h.get(it["k"]) or {})) for it in milik]
        sisa = len(_sisa(st, email)) if st else 0
        berjalan = berjalan_global and sisa > 0
        kini_k = st.get("item_kini") if berjalan_global else None
        kini_it = next((it for it in milik if it["k"] == kini_k), None)
        antre_lain = len([it for it in _sisa(st) if email not in (it.get("u") or [])]) if berjalan else 0
    cfg_pub = {k: cfg[k] for k in ("aktif", "niche", "pesaing")}
    cfg_pub["jam"] = JADWAL["jam"]
    return {
        "ok": True, "siap": True, "cfg": cfg_pub, "niche_lebih": cfg.get("niche_lebih") or [],
        "berjalan": berjalan, "kunci_wajib": bool(JADWAL_KUNCI) and admin, "jam_global": True,
        "admin": admin,   # v1.4.3: panduan pemicu cron hanya untuk pemilik server
        "sekarang_wib": datetime.now(WIB).strftime("%Y-%m-%d %H:%M"), "n_pesaing": len(pesaing),
        "niche_maks": NICHE_HARIAN_MAKS,
        "putaran": {"tanggal": st.get("tanggal") if milik else None, "pemicu": st.get("pemicu"),
                    "mulai": st.get("mulai"), "akhir": st.get("akhir"),
                    "item_kini": kini_it["nama"] if kini_it else None, "antre_lain": antre_lain,
                    "total": len(items), "ok": sum(1 for i in items if i.get("ok")),
                    "sisa": sisa, "terputus": bool(milik) and not berjalan_global and sisa > 0,
                    "items": items},
    }


def jadwal_simpan(email, d):
    if not _muat_jadwal():
        return {"ok": False, "error": "Tabel setelan belum dibuat - jalankan SUPABASE-JADWAL.sql di Supabase"}
    cfg = _rapikan_cfg(d, ketat=True)
    if cfg is None:
        return {"ok": False, "error": f"Maksimal {NICHE_HARIAN_MAKS} niche harian per akun"}
    simpan = {k: cfg[k] for k in ("aktif", "niche", "pesaing")}
    if not setelan_tulis("jadwal:" + email, simpan):
        return {"ok": False, "error": "Gagal menyimpan setelan"}
    with kunci_jadwal:
        JADWAL["cfgs"][email] = cfg
    _ARSIP_CACHE["t"] = 0.0
    return dict(jadwal_publik(email), ok=True)


def niche_akun(email):
    """Niche (huruf kecil) yang pernah di-scan akun ini + niche harian-nya → filter Tren Harian."""
    rw = None
    if supa_aktif() and akun_siap():   # v1.4.1: langsung dari Supabase (memori bisa basi setelah data dipindah lewat SQL)
        ok, r = supa("GET", "intai_riwayat", params=f"?select=kata,mode&pemilik=eq.{quote(email)}&order=id.desc&limit=1000")
        if ok and isinstance(r, list):
            rw = r
    if rw is None:
        with lock:
            rw = [r for r in RIWAYAT if r.get("pemilik") == email]
    s = {str(r.get("kata") or "").lower() for r in rw if r.get("mode") != "halaman"}
    if _muat_jadwal():
        with kunci_jadwal:
            s |= {n.lower() for n in _cfg_akun(email)["niche"]}
    return s

KOLOM_TREN = ("waktu,jumlah,aktif,mati,relevan,pengiklan,baru_7h,menang,umur_median,"
              "harga_median,harga_titik,belanja_min,belanja_max")


def _hari_wib(w):
    try:
        return (datetime.strptime(str(w)[:19], "%Y-%m-%dT%H:%M:%S") + timedelta(hours=7)).strftime("%Y-%m-%d")
    except Exception:
        return str(w or "")[:10]


def tren_niche(niche="", email=None):
    """Tanpa niche → daftar niche (kata kunci) beserta jumlah hari data. Dengan niche → 1 titik per hari
    (potret TERAKHIR hari itu, WIB) dari intai_snapshot. Angka = sampel scan, bukan total pasar."""
    niche = str(niche or "").strip()[:120]
    if supa_aktif():
        if niche:
            ok, r = supa("GET", "intai_snapshot", params=f"?select={KOLOM_TREN}&niche=eq.{quote(niche)}"
                                                         "&mode=neq.halaman&order=waktu.asc&limit=1000")
        else:
            ok, r = supa("GET", "intai_snapshot", params="?select=niche,waktu&mode=neq.halaman&order=waktu.desc&limit=5000")
        if not ok or not isinstance(r, list):
            return {"ok": True, "siap": False}
    else:
        with kunci_arsip:
            _muat_arsip_lokal()
            r = [dict(x) for x in ARSIP_LOKAL["snapshot"]
                 if x.get("mode") != "halaman" and (not niche or x.get("niche") == niche)]
    if not niche:
        g = {}
        for x in r:
            n = x.get("niche")
            if not n:
                continue
            e = g.setdefault(n, {"niche": n, "scan": 0, "hari": set(), "terakhir": ""})
            e["scan"] += 1
            e["hari"].add(_hari_wib(x.get("waktu")))
            e["terakhir"] = max(e["terakhir"], str(x.get("waktu") or ""))
        daftar = [dict(e, hari=len(e["hari"])) for e in g.values()]
        if email is not None:   # v1.4: Tren Harian pribadi = niche yg pernah di-scan / dijadwalkan akun ini
            punya = niche_akun(email)
            daftar = [e for e in daftar if str(e["niche"]).lower() in punya]
        daftar.sort(key=lambda e: e["terakhir"], reverse=True)
        return {"ok": True, "siap": True, "daftar": daftar}
    per_hari = {}
    for x in sorted(r, key=lambda x: str(x.get("waktu") or "")):
        per_hari[_hari_wib(x.get("waktu"))] = x
    titik = [dict(v, hari=k) for k, v in sorted(per_hari.items())]
    return {"ok": True, "siap": True, "niche": niche, "scan": len(r), "titik": titik[-180:]}


# ======================= PESAING v1.1 (P2): profil pengiklan + pantau + feed =======================
# Semua angka dari arsip INTAI (iklan yang pernah terlihat di scan) — bukan seluruh iklan si pengiklan,
# kecuali sudah pernah di-scan mode halaman. Frontend menampilkan cakupan ini apa adanya.
PANTAU_MAKS = 30
KOLOM_PROFIL = ("archive_id,halaman,niche,aktif,mulai,umur,format,funnel,platform,cta,hook,link,domain,"
                "harga_min,harga_max,status,skor,skor_jual,varian,est_min,est_max,pertama_terlihat,terakhir_terlihat")


def _umur_kini(r, hari):
    m = _tgl(r.get("mulai"))
    return (hari - m).days + 1 if m else _int(r.get("umur"), 0)


def _durasi(r):
    m, b = _tgl(r.get("mulai")), _tgl(r.get("berakhir"))
    return (b - m).days + 1 if (m and b and b >= m) else None


def ringkas_profil(rows):
    hari = datetime.now(timezone.utc).date()
    aktif = [r for r in rows if r.get("aktif") is True]
    mati = [r for r in rows if r.get("aktif") is False]
    med = lambda xs: (sorted(xs)[len(xs) // 2] if xs else None)
    umur_a = [_umur_kini(r, hari) for r in aktif]
    tertua = max(aktif, key=lambda r: _umur_kini(r, hari)) if aktif else None
    dur = [d for d in (_durasi(r) for r in mati) if d]
    basis = aktif or rows

    def dist(k, rs=None, top=None):
        c = {}
        for r in (basis if rs is None else rs):
            v = r.get(k) or "—"
            c[v] = c.get(v, 0) + 1
        items = sorted(c.items(), key=lambda kv: -kv[1])
        return dict(items[:top] if top else items)

    bulan = []
    for i in range(5, -1, -1):
        yy, mm = hari.year, hari.month - i
        while mm <= 0:
            mm += 12
            yy -= 1
        k = f"{yy:04d}-{mm:02d}"
        bulan.append({"bulan": k, "n": sum(1 for r in rows if str(r.get("mulai") or "").startswith(k))})
    harga = sorted(h for h in (_int(r.get("harga_min")) for r in aktif) if h and h > 0)
    est = [r for r in aktif if _int(r.get("est_min"))]
    return {
        "total": len(rows), "aktif": len(aktif), "mati": len(mati),
        "umur_median": med(umur_a), "umur_max": (_umur_kini(tertua, hari) if tertua else None),
        "tertua": ({"hook": tertua.get("hook"), "archive_id": tertua.get("archive_id"),
                    "mulai": tertua.get("mulai")} if tertua else None),
        "baru_30h": sum(1 for r in rows if _tgl(r.get("mulai")) and (hari - _tgl(r.get("mulai"))).days < 30),
        "per_bulan": bulan,
        "durasi_mati": {"n": len(dur), "median": med(dur), "cepat": sum(1 for x in dur if x <= 7),
                        "tahan": sum(1 for x in dur if x >= 30), "tanpa_tanggal": len(mati) - len(dur)},
        "basis": "aktif" if aktif else "semua",
        "format": dist("format"), "funnel": dist("funnel"), "status": dist("status") if aktif else {},
        "niche": dist("niche", [r for r in rows if r.get("niche")], 8),
        "domain": dist("domain", [r for r in rows if r.get("domain")], 6),
        "cta": dist("cta", [r for r in basis if r.get("cta")], 5),
        "harga": {"n": len(harga), "min": harga[0] if harga else None, "median": med(harga),
                  "max": harga[-1] if harga else None},
        "belanja": {"n": len(est), "min": sum(_int(r.get("est_min"), 0) for r in est),
                    "max": sum(_int(r.get("est_max"), 0) for r in est)},
        "menang": sum(1 for r in aktif if r.get("status") == "MENANG"),
    }


def _baris_profil(r, hari):
    aktif = r.get("aktif") is True
    return {
        "archive_id": r.get("archive_id"), "aktif": aktif, "hook": r.get("hook"), "katalog": bool(r.get("katalog")),
        "format": r.get("format"), "funnel": r.get("funnel"), "niche": r.get("niche"),
        "mulai": r.get("mulai"), "berakhir": r.get("berakhir"),
        "hari": (_umur_kini(r, hari) if aktif else _durasi(r)),  # aktif: umur s/d hari ini · mati: lama tayang
        "harga_min": r.get("harga_min"), "harga_max": r.get("harga_max"), "status": r.get("status"),
        "skor": r.get("skor"), "est_min": r.get("est_min"), "est_max": r.get("est_max"),
        "domain": r.get("domain"), "cta": r.get("cta"),
        "pertama_terlihat": r.get("pertama_terlihat"), "terakhir_terlihat": r.get("terakhir_terlihat"),
    }


def profil_pengiklan(pid, email=""):
    pid = re.sub(r"\D", "", str(pid or ""))[:40]
    if not pid:
        return {"ok": False, "error": "page_id tidak valid"}
    if supa_aktif():
        ok, pg = supa("GET", "intai_pengiklan", params=f"?select=*&page_id=eq.{pid}")
        if not ok:
            return {"ok": True, "siap": False, "sumber": "supabase", "page_id": pid}
        p2 = p2_siap()
        kol = KOLOM_PROFIL + (",berakhir" if p2 else "")
        ok, ik = supa("GET", "intai_iklan", params=f"?select={kol}&page_id=eq.{pid}&order=terakhir_terlihat.desc&limit=1000")
        ik = ik if ok and isinstance(ik, list) else []
        ev, dipantau = [], False
        if p2:
            ok, r = supa("GET", "intai_peristiwa", params=f"?select=*&page_id=eq.{pid}&order=waktu.desc&limit=60")
            ev = r if ok and isinstance(r, list) else []
            ok, r = supa("GET", "intai_pantau", params=f"?select=page_id&page_id=eq.{pid}&pemilik=eq.{quote(email)}") \
                if akun_siap() else (False, None)
            dipantau = bool(ok and isinstance(r, list) and r)
        ok, sn = supa("GET", "intai_snapshot",
                      params=f"?select=waktu,aktif,mati,jumlah&niche=eq.{quote('halaman ' + pid)}&order=waktu.asc&limit=500")
        sn = sn if ok and isinstance(sn, list) else []
        pg = pg[0] if isinstance(pg, list) and pg else None
        sumber = "supabase"
    else:
        with kunci_arsip:
            _muat_arsip_lokal()
            pg = ARSIP_LOKAL["pengiklan"].get(pid)
            ik = [dict(v) for v in ARSIP_LOKAL["iklan"].values() if str(v.get("page_id")) == pid]
            ev = [dict(e) for e in reversed(ARSIP_LOKAL["peristiwa"]) if str(e.get("page_id")) == pid][:60]
            dipantau = f"{email}|{pid}" in ARSIP_LOKAL["pantau"]
            sn = [s_ for s_ in ARSIP_LOKAL["snapshot"] if s_.get("niche") == "halaman " + pid]
        p2, sumber = True, "lokal"
    for r_ in list(ik) + list(ev):
        _bersih_baris_arsip(r_)
    hari = datetime.now(timezone.utc).date()
    baris = sorted((_baris_profil(r, hari) for r in ik), key=lambda b: (not b["aktif"], -(b["hari"] or 0)))
    return {
        "ok": True, "siap": True, "p2_siap": p2, "sumber": sumber, "page_id": pid,
        "kosong": not pg and not ik, "pengiklan": pg, "dipantau": dipantau,
        "ringkas": ringkas_profil(ik), "iklan": baris[:400], "iklan_total": len(baris),
        "peristiwa": ev,
        "riwayat": [{"waktu": x.get("waktu"), "aktif": x.get("aktif"), "mati": x.get("mati")} for x in sn][-60:],
        "scan_halaman": len(sn), "scan_halaman_terakhir": (sn[-1].get("waktu") if sn else None),
    }


def daftar_pantau(email):
    batas = datetime.fromtimestamp(time.time() - 7 * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if supa_aktif():
        if not p2_siap():
            return {"ok": True, "siap": False, "sumber": "supabase"}
        if not akun_siap():
            return {"ok": True, "siap": False, "sumber": "supabase", "pesan": PESAN_AKUN_SQL}
        ok, pn = supa("GET", "intai_pantau", params=f"?select=*&pemilik=eq.{quote(email)}&order=ditambah.desc&limit=100")
        if not ok or not isinstance(pn, list):
            return {"ok": True, "siap": False, "sumber": "supabase"}
        pids = [str(p.get("page_id")) for p in pn]
        pg, hit, ev7 = {}, {}, []
        if pids:
            ok, r = _supa_in("intai_pengiklan", "page_id", pids, "page_id,nama,likes,profil,terakhir_terlihat")
            pg = {str(x.get("page_id")): x for x in r}

            def hitung(pid):
                return pid, supa_hitung("intai_iklan", f"&page_id=eq.{pid}"), \
                    supa_hitung("intai_iklan", f"&page_id=eq.{pid}&aktif=is.true")
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                hit = {p: (t, a) for p, t, a in ex.map(hitung, pids)}
            ok, ev7 = _supa_in("intai_peristiwa", "page_id", pids, "page_id,jenis", f"&waktu=gte.{batas}")
        sumber = "supabase"
    else:
        with kunci_arsip:
            _muat_arsip_lokal()
            pn = sorted((dict(v) for v in ARSIP_LOKAL["pantau"].values() if v.get("pemilik") == email),
                        key=lambda p: p.get("ditambah") or "", reverse=True)
            pids = [str(p.get("page_id")) for p in pn]
            pg = {p: ARSIP_LOKAL["pengiklan"].get(p) or {} for p in pids}
            hit = {p: (0, 0) for p in pids}
            for v in ARSIP_LOKAL["iklan"].values():
                p = str(v.get("page_id"))
                if p in hit:
                    t, a = hit[p]
                    hit[p] = (t + 1, a + (1 if v.get("aktif") else 0))
            ev7 = [e for e in ARSIP_LOKAL["peristiwa"] if str(e.get("page_id")) in hit and (e.get("waktu") or "") >= batas]
        sumber = "lokal"
    daftar = []
    for p in pn:
        pid = str(p.get("page_id"))
        g_ = pg.get(pid) or {}
        t, a = hit.get(pid, (None, None))
        jn = [e.get("jenis") for e in ev7 if str(e.get("page_id")) == pid]
        daftar.append({
            "page_id": pid, "nama": g_.get("nama") or p.get("nama"), "likes": g_.get("likes"),
            "profil": g_.get("profil"), "ditambah": p.get("ditambah"), "terakhir_cek": p.get("terakhir_cek"),
            "terakhir_terlihat": g_.get("terakhir_terlihat"), "total": t, "aktif": a,
            "baru_7h": jn.count("baru"), "mati_7h": jn.count("mati"), "hidup_7h": jn.count("hidup"),
        })
    return {"ok": True, "siap": True, "sumber": sumber, "maks": PANTAU_MAKS, "daftar": daftar}


def peristiwa_feed(lingkup="pantau", batas_n=60, email=""):
    batas_n = max(1, min(int(batas_n or 60), 200))
    if supa_aktif():
        if not p2_siap():
            return {"ok": True, "siap": False, "feed": []}
        if lingkup == "pantau":
            ok, pn = supa("GET", "intai_pantau", params=f"?select=page_id&pemilik=eq.{quote(email)}&limit=100") \
                if akun_siap() else (False, None)
            pids = [str(p.get("page_id")) for p in pn] if ok and isinstance(pn, list) else []
            ok, r = _supa_in("intai_peristiwa", "page_id", pids, "*", f"&order=waktu.desc&limit={batas_n}") if pids else (True, [])
        else:
            ok, r = supa("GET", "intai_peristiwa", params=f"?select=*&order=waktu.desc&limit={batas_n}")
        r = r if ok and isinstance(r, list) else []
    else:
        with kunci_arsip:
            _muat_arsip_lokal()
            pids = {str(v.get("page_id")) for v in ARSIP_LOKAL["pantau"].values() if v.get("pemilik") == email}
            r = [dict(e) for e in ARSIP_LOKAL["peristiwa"] if lingkup != "pantau" or str(e.get("page_id")) in pids]
    r.sort(key=lambda e: str(e.get("waktu") or ""), reverse=True)
    for e_ in r[:batas_n]:
        _bersih_baris_arsip(e_)
    return {"ok": True, "siap": True, "lingkup": lingkup, "feed": r[:batas_n]}


def ubah_pantau(email, aksi, pid, nama=""):
    pid = re.sub(r"\D", "", str(pid or ""))[:40]
    if not pid:
        return {"ok": False, "error": "page_id tidak valid"}
    nama = _teks(nama, 160)
    if supa_aktif():
        if not p2_siap():
            return {"ok": False, "error": "Tabel pantau belum dibuat — jalankan SUPABASE-PESAING.sql di Supabase"}
        if not akun_siap():
            return {"ok": False, "error": PESAN_AKUN_SQL}
        qe = quote(email)
        if aksi == "hapus":
            ok, d = supa("DELETE", "intai_pantau", params=f"?pemilik=eq.{qe}&page_id=eq.{pid}")
        else:
            _, ada = supa("GET", "intai_pantau", params=f"?select=page_id&pemilik=eq.{qe}&page_id=eq.{pid}")
            if not ada and (supa_hitung("intai_pantau", f"&pemilik=eq.{qe}") or 0) >= PANTAU_MAKS:
                return {"ok": False, "error": f"Maksimal {PANTAU_MAKS} pesaing dipantau — hapus salah satu dulu"}
            ok, d = supa("POST", "intai_pantau", payload={"pemilik": email, "page_id": pid, "nama": nama},
                         prefer="return=minimal,resolution=merge-duplicates")
        if not ok:
            return {"ok": False, "error": "Gagal menyimpan: " + str(d)[:140]}
    else:
        with kunci_arsip:
            _muat_arsip_lokal()
            pn = ARSIP_LOKAL["pantau"]
            kk = f"{email}|{pid}"
            if aksi == "hapus":
                pn.pop(kk, None)
            else:
                if kk not in pn and sum(1 for v in pn.values() if v.get("pemilik") == email) >= PANTAU_MAKS:
                    return {"ok": False, "error": f"Maksimal {PANTAU_MAKS} pesaing dipantau — hapus salah satu dulu"}
                lama = pn.get(kk) or {}
                pn[kk] = {"pemilik": email, "page_id": pid, "nama": nama or lama.get("nama"),
                           "ditambah": lama.get("ditambah") or _iso_kini(), "terakhir_cek": lama.get("terakhir_cek")}
            ok, galat = _tulis_arsip_lokal()
        if not ok:
            return {"ok": False, "error": galat}
    _ARSIP_CACHE["t"] = 0.0
    return {"ok": True, "page_id": pid, "dipantau": aksi != "hapus"}


def jumlah_pantau(email):
    if supa_aktif():
        return (supa_hitung("intai_pantau", f"&pemilik=eq.{quote(email)}") or 0) if akun_siap() else 0
    with kunci_arsip:
        _muat_arsip_lokal()
        return sum(1 for v in ARSIP_LOKAL["pantau"].values() if v.get("pemilik") == email)


def _nama_dari_rows(rows):
    """Nama halaman terbanyak di hasil scan mode halaman (page_name dari Meta). None bila tak ada."""
    c = {}
    for r in rows or []:
        n = str(r.get("halaman") or "").strip()
        if n and n != "(tanpa nama)":
            c[n] = c.get(n, 0) + 1
    return max(c, key=c.get) if c else None


def nama_halaman_map(pids):
    """{page_id: nama} dari arsip pengiklan (data bersama). Best-effort: gagal → {} (UI pakai cadangan)."""
    pids = [p for p in dict.fromkeys(str(x) for x in pids if x) if p.isdigit()]
    if not pids:
        return {}
    try:
        if supa_aktif():
            ok, r = _supa_in("intai_pengiklan", "page_id", pids, "page_id,nama")
            return {str(x.get("page_id")): x.get("nama") for x in (r if ok else []) if x.get("nama")}
        with kunci_arsip:
            _muat_arsip_lokal()
            pg = ARSIP_LOKAL.get("pengiklan") or {}
            return {p: (pg.get(p) or {}).get("nama") for p in pids if (pg.get(p) or {}).get("nama")}
    except Exception:
        return {}


def _beri_label_halaman(daftar, terakhir):
    """v1.4.2: entri mode halaman ('halaman <id>') diberi label = nama pengiklan dari arsip."""
    pid_dari = lambda k: (re.fullmatch(r"halaman\s+(\d+)", str(k or "")) or [None, None])[1]
    pids = [pid_dari(g.get("kata")) for g in daftar] + ([pid_dari(terakhir.get("kata"))] if terakhir else [])
    nm = nama_halaman_map([p for p in pids if p])
    for g in daftar + ([terakhir] if terakhir else []):
        p = pid_dari(g.get("kata"))
        if p:
            g["page_id"] = p
            g["label"] = nm.get(p)


def ringkasan_api(email):
    """Struktur persis kontrak dashboard: niche/total_scan/total_iklan/delta/
    terakhir/daftar[{kata,mode,scan[]}] (total_iklan = jumlah scan TERAKHIR per niche). v1.4: hanya riwayat akun ini."""
    rw = None
    if supa_aktif() and akun_siap():   # langsung dari Supabase → tak terpotong oleh riwayat akun lain di memori
        ok, r = supa("GET", "intai_riwayat", params=f"?select=kata,mode,waktu,jumlah,baru,hilang"
                                                    f"&pemilik=eq.{quote(email)}&order=id.desc&limit=1000")
        if ok and isinstance(r, list):
            rw = list(reversed(r))
    with lock:
        if rw is None:
            rw = [r for r in RIWAYAT if (r.get("pemilik") or "") == email]
        grup = {}
        for r in rw:
            g = grup.setdefault(r["kata"], {"kata": r["kata"], "mode": r["mode"], "scan": []})
            g["mode"] = r["mode"]
            g["scan"].append({k: r[k] for k in ("waktu", "jumlah", "baru", "hilang")})
        daftar = list(grup.values())
        total_iklan = sum(g["scan"][-1]["jumlah"] for g in daftar)
        terakhir = dict(rw[-1]) if rw else None
    _beri_label_halaman(daftar, terakhir)   # di luar lock: bisa memanggil Supabase
    return {
        "ok": True, "niche": len(daftar), "total_scan": len(rw),
        "total_iklan": total_iklan,
        "delta": terakhir["jumlah"] if terakhir else 0,
        "terakhir": terakhir, "daftar": daftar,
    }


def muat_halaman():
    """Aplikasi utuh (index.html: dashboard + scan satu SPA) dari disk — dibaca
    per-request supaya edit langsung terlihat. Fallback: halaman scan embedded."""
    for nama in ("index.html", "dashboard.html"):
        try:
            with open(os.path.join(BASE_DIR, nama), "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            continue
    return HTML


# ======================= AKUN v1.4: sesi persisten + data per akun =======================
# Sesi = token bertanda tangan HMAC (email + kedaluwarsa) → tetap sah walau Render restart/tidur.
# Dikirim lewat header X-Intai-Sesi (dari localStorage) + cookie HttpOnly (cadangan utk tautan langsung).
SESI_HARI = 30
SESI_COOKIE = "intai_sesi"


def _rahasia_sesi():
    r = (os.environ.get("INTAI_RAHASIA") or "").strip()
    if r:
        return r.encode("utf-8")
    if SUPA_KEY:
        return hashlib.sha256(("intai-sesi|" + SUPA_KEY).encode("utf-8")).digest()
    jalur = os.path.join(BASE_DIR, ".intai_rahasia")
    try:
        with open(jalur, "r", encoding="utf-8") as f:
            r = f.read().strip()
    except Exception:
        r = ""
    if len(r) < 32:
        r = secrets.token_hex(32)
        try:
            with open(jalur, "w", encoding="utf-8") as f:
                f.write(r)
        except Exception:
            pass
    return r.encode("utf-8")


SESI_RAHASIA = _rahasia_sesi()


def _b64u(b):
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


# v1.4.7: sesi "s2" diikat ke (a) sidik hash password dan (b) nomor "angkatan" sesi per akun.
#   Ganti password → semua sesi lama putus. "Keluarkan semua perangkat" → angkatan naik → semua sesi lama putus.
#   Angkatan disimpan di setelan "sesi:<email>" {"e": n} + cache memori (dibaca sekali saat pertama dipakai).
#   Token format lama "s1" tidak diterima lagi → semua pengguna masuk ulang sekali setelah versi ini.
SESI_EPOCH = {}
_epoch_muat = {"ok": False, "t": 0.0}
_kunci_epoch = threading.Lock()


def _muat_epoch():
    if _epoch_muat["ok"] or time.time() - _epoch_muat["t"] < 30:
        return
    with _kunci_epoch:
        if _epoch_muat["ok"]:
            return
        _epoch_muat["t"] = time.time()
        ok, semua = setelan_awalan("sesi:")
        if ok:
            for k, v in (semua or {}).items():
                try:
                    SESI_EPOCH[str(k)[5:]] = int((v or {}).get("e") or 0)
                except Exception:
                    pass
            _epoch_muat["ok"] = True


def _sidik_hash(h):
    return hashlib.sha256(("sidik|" + str(h or "")).encode("utf-8")).hexdigest()[:16] if h else ""


def _ttd_sesi(isi, h, epoch):
    return hmac.new(SESI_RAHASIA, ("s2|" + isi + "|" + _sidik_hash(h) + "|" + str(epoch)).encode("utf-8"), hashlib.sha256).hexdigest()[:48]


def buat_sesi(email):
    _muat_epoch()
    with lock:
        h = (AKUN_DB.get(email) or {}).get("hash")
    isi = _b64u(email.encode("utf-8")) + "." + str(int(time.time()) + SESI_HARI * 86400)
    return "s2." + isi + "." + _ttd_sesi(isi, h, SESI_EPOCH.get(email, 0))


def baca_sesi(tok):
    """email bila token sah, belum kedaluwarsa, akunnya masih ada, password & angkatan sesi belum berubah — selain itu None."""
    try:
        tok = str(tok or "").strip()
        if not tok.startswith("s2."):
            return None
        _, b, exp, sig = tok.split(".", 3)
        if int(exp) < time.time():
            return None
        email = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8")
        _muat_epoch()
        with lock:
            a = AKUN_DB.get(email)
            h = a.get("hash") if a else None
        if not a:
            return None
        return email if hmac.compare_digest(sig, _ttd_sesi(b + "." + exp, h, SESI_EPOCH.get(email, 0))) else None
    except Exception:
        return None


def putus_semua_sesi(email):
    """Naikkan angkatan sesi → semua token lama akun ini tidak berlaku. Return True bila tersimpan permanen."""
    _muat_epoch()
    e = max(int(time.time()), SESI_EPOCH.get(email, 0) + 1)
    SESI_EPOCH[email] = e
    return setelan_tulis("sesi:" + email, {"e": e})


AKUN_SIAP = {"v": None, "t": 0.0}


def akun_siap():
    """Kolom pemilik + tabel intai_ekspor sudah dibuat (SUPABASE-AKUN.sql)? Lokal selalu siap."""
    if not supa_aktif():
        return True
    if AKUN_SIAP["v"] or (AKUN_SIAP["v"] is False and time.time() - AKUN_SIAP["t"] < 60):
        return bool(AKUN_SIAP["v"])
    ok1, _ = supa("GET", "intai_riwayat", params="?select=pemilik&limit=1")
    ok2, _ = supa("GET", "intai_ekspor", params="?select=id&limit=1")
    ok3, _ = supa("GET", "intai_pantau", params="?select=pemilik&limit=1") if p2_siap() else (True, None)
    AKUN_SIAP.update({"v": bool(ok1 and ok2 and ok3), "t": time.time()})
    return AKUN_SIAP["v"]


PESAN_AKUN_SQL = "Database belum disiapkan untuk akun - jalankan SUPABASE-AKUN.sql sekali di Supabase (SQL Editor)"


# ---- ⭐ Pantauan per akun (1 baris setelan per akun: bintang:<email>) ----
BINTANG_MAKS = 300


def bintang_baca(email):
    ok, v = setelan_baca("bintang:" + email)
    if not ok:
        return {"ok": True, "siap": False, "daftar": []}
    return {"ok": True, "siap": True, "daftar": v if isinstance(v, list) else []}


def bintang_tulis(email, daftar):
    if not isinstance(daftar, list):
        return {"ok": False, "error": "format salah"}
    bersih = []
    for e in daftar[:BINTANG_MAKS]:
        if isinstance(e, dict) and len(json.dumps(e, ensure_ascii=False)) <= 6000:
            bersih.append(e)
    if not setelan_tulis("bintang:" + email, bersih):
        return {"ok": False, "error": "Gagal menyimpan Pantauan (tabel setelan belum siap?)"}
    return {"ok": True, "n": len(bersih)}


# ---- Penampung Ekspor CSV per akun (Supabase: intai_ekspor · lokal: setelan ekspor:<email>) ----
EKSPOR_MAKS = 20
EKSPOR_BYTE_MAKS = 3_000_000


def _ekspor_id(v):
    return re.sub(r"[^A-Za-z0-9_.:-]", "", str(v or ""))[:80]


def ekspor_baca(email):
    if supa_aktif():
        if not akun_siap():
            return {"ok": True, "siap": False, "pesan": PESAN_AKUN_SQL, "daftar": []}
        ok, r = supa("GET", "intai_ekspor",
                     params=f"?select=data&pemilik=eq.{quote(email)}&order=waktu.desc&limit={EKSPOR_MAKS}")
        if not ok or not isinstance(r, list):
            return {"ok": True, "siap": False, "daftar": []}
        return {"ok": True, "siap": True, "daftar": [x.get("data") for x in r if isinstance(x.get("data"), dict)]}
    ok, v = setelan_baca("ekspor:" + email)
    return {"ok": True, "siap": True, "daftar": v if isinstance(v, list) else []}


def ekspor_ubah(email, d):
    aksi = str(d.get("aksi") or "")
    if supa_aktif() and not akun_siap():
        return {"ok": False, "error": PESAN_AKUN_SQL}
    if aksi == "simpan":
        e = d.get("entri")
        if not isinstance(e, dict) or not _ekspor_id(e.get("id")):
            return {"ok": False, "error": "entri tidak valid"}
        if len(json.dumps(e, ensure_ascii=False)) > EKSPOR_BYTE_MAKS:
            return {"ok": False, "error": "Ekspor terlalu besar untuk disimpan (maks ±3 MB) - persempit filter"}
        eid = _ekspor_id(e.get("id"))
        if supa_aktif():
            filt = f"?pemilik=eq.{quote(email)}&id=eq.{quote(eid)}"
            _, ada = supa("GET", "intai_ekspor", params="?select=id&" + filt[1:])
            if ada:   # perbarui isi saja (mis. tanda "sudah diunduh") → urutan/waktu tetap
                ok, g = supa("PATCH", "intai_ekspor", payload={"data": e}, params=filt)
                return {"ok": bool(ok)} if ok else {"ok": False, "error": "Gagal memperbarui ekspor: " + str(g)[:120]}
            w = str(e.get("waktu") or "")
            w = w[:19] + "Z" if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d.*", w) else _iso_kini()   # urutan asli (ekspor pindahan)
            ok, g = supa("POST", "intai_ekspor", payload={"id": eid, "pemilik": email, "waktu": w, "data": e},
                         prefer="return=minimal,resolution=merge-duplicates")
            if not ok:
                return {"ok": False, "error": "Gagal menyimpan ekspor: " + str(g)[:120]}
            ok, r = supa("GET", "intai_ekspor", params=f"?select=id&pemilik=eq.{quote(email)}&order=waktu.desc"
                                                       f"&offset={EKSPOR_MAKS}&limit=100")
            for x in (r if ok and isinstance(r, list) else []):
                supa("DELETE", "intai_ekspor", params=f"?pemilik=eq.{quote(email)}&id=eq.{quote(str(x.get('id')))}")
            return {"ok": True}
        _, v = setelan_baca("ekspor:" + email)
        daftar = [x for x in (v if isinstance(v, list) else []) if isinstance(x, dict) and x.get("id") != e.get("id")]
        lama = next((x for x in (v or []) if isinstance(x, dict) and x.get("id") == e.get("id")), None)
        if lama is not None:   # perbarui di tempat (mis. tanda "sudah diunduh") tanpa mengubah urutan
            daftar = [e if (isinstance(x, dict) and x.get("id") == e.get("id")) else x for x in (v or []) if isinstance(x, dict)]
        else:
            daftar.insert(0, e)
        return {"ok": bool(setelan_tulis("ekspor:" + email, daftar[:EKSPOR_MAKS]))}
    if aksi in ("hapus", "hapus_semua"):
        ids = [] if aksi == "hapus_semua" else [_ekspor_id(i) for i in (d.get("ids") or []) if _ekspor_id(i)]
        if supa_aktif():
            if aksi == "hapus_semua":
                ok, _ = supa("DELETE", "intai_ekspor", params=f"?pemilik=eq.{quote(email)}")
            else:
                ok = True
                for i in ids:
                    ok, _ = supa("DELETE", "intai_ekspor", params=f"?pemilik=eq.{quote(email)}&id=eq.{quote(i)}")
            return {"ok": bool(ok)}
        _, v = setelan_baca("ekspor:" + email)
        daftar = [] if aksi == "hapus_semua" else [x for x in (v if isinstance(v, list) else [])
                                                   if isinstance(x, dict) and _ekspor_id(x.get("id")) not in ids]
        return {"ok": bool(setelan_tulis("ekspor:" + email, daftar))}
    return {"ok": False, "error": "aksi tidak dikenal"}


# ---- v1.4.4: CS — kontak + tiket bantuan per akun ----
# Disimpan di tabel intai_setelan yang sudah ada (kunci tiket:<email>) → TANPA SQL baru.
# Admin (INTAI_ADMIN) melihat & membalas tiket semua akun.
CS_TIKET_MAKS = 30          # per akun (yang selesai paling lama dibuang duluan)
CS_TERBUKA_MAKS = 10        # tiket belum selesai per akun
CS_PESAN_MAKS = 60          # pesan per tiket
CS_TEKS_MAKS = 2000
CS_KATEGORI = ["Akun & login", "Scan & data", "Scan otomatis", "Ekspor CSV", "Saran fitur", "Lainnya"]
kunci_cs = threading.Lock()


def cs_kontak():
    """Kontak CS publik. Env: INTAI_CS_WA (nomor WA), INTAI_CS_EMAIL ('-' = sembunyikan). Bawaan email = admin pertama."""
    wa = re.sub(r"\D", "", os.environ.get("INTAI_CS_WA") or "")
    if wa.startswith("0"):
        wa = "62" + wa[1:]
    em = (os.environ.get("INTAI_CS_EMAIL") or "").strip()
    if not em:
        em = ADMIN_DAFTAR[0] if ADMIN_DAFTAR else ""
    if em == "-" or "@" not in em:
        em = ""
    return {"wa": wa if 8 <= len(wa) <= 15 else "", "email": em}


def _cs_teks(v, n=CS_TEKS_MAKS):
    return str(v or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:n]


def _cs_daftar(email):
    ok, v = setelan_baca("tiket:" + email)
    return ok, [t for t in (v if isinstance(v, list) else []) if isinstance(t, dict) and t.get("id")]


def _nama_akun(email):
    with lock:
        return (AKUN_DB.get(email) or {}).get("nama") or email


def cs_baca(email):
    admin = is_admin(email)
    ok, milik = _cs_daftar(email)
    hasil = {"ok": True, "siap": ok, "admin": admin, "kontak": cs_kontak(), "kategori": CS_KATEGORI,
             "tiket": milik, "belum": sum(1 for t in milik if t.get("baca_user") is False)}
    if admin:
        ok2, peta = setelan_awalan("tiket:")
        masuk = []
        for k, v in (peta or {}).items():
            pem = str(k)[len("tiket:"):]
            nama = _nama_akun(pem)
            for t in (v if isinstance(v, list) else []):
                if isinstance(t, dict) and t.get("id"):
                    masuk.append(dict(t, pemilik=pem, nama_pemilik=nama))
        masuk.sort(key=lambda t: str(t.get("diperbarui") or ""), reverse=True)
        hasil["masuk"] = masuk[:300]
        hasil["masuk_belum"] = sum(1 for t in masuk if t.get("baca_admin") is False)
    return hasil


def cs_ubah(email, d):
    aksi = str(d.get("aksi") or "")
    admin = is_admin(email)
    peran = "admin" if (admin and d.get("sebagai") == "admin") else "user"
    pemilik = str(d.get("pemilik") or "").strip().lower() if peran == "admin" else email
    if not pemilik:
        return {"ok": False, "error": "pemilik tiket kosong"}
    kini = _iso_kini()
    with kunci_cs:   # bukan `lock` global → panggilan Supabase tidak menahan scan
        ok, daftar = _cs_daftar(pemilik)
        if not ok:
            return {"ok": False, "error": "Penyimpanan tiket belum siap. Hubungi CS lewat WhatsApp/email dulu."}
        if aksi == "baru":
            if peran != "user":
                return {"ok": False, "error": "Admin tidak membuat tiket atas nama akun lain"}
            judul, teks = _cs_teks(d.get("judul"), 120), _cs_teks(d.get("teks"))
            if len(judul) < 3 or len(teks) < 5:
                return {"ok": False, "error": "Isi judul (min. 3 huruf) dan pesan (min. 5 huruf)"}
            if sum(1 for t in daftar if t.get("status") != "selesai") >= CS_TERBUKA_MAKS:
                return {"ok": False, "error": f"Maksimal {CS_TERBUKA_MAKS} tiket terbuka. Tandai selesai tiket lama dulu."}
            kat = d.get("kategori") if d.get("kategori") in CS_KATEGORI else "Lainnya"
            t = {"id": "t" + secrets.token_hex(5), "judul": judul, "kategori": kat, "status": "terbuka",
                 "dibuat": kini, "diperbarui": kini, "baca_user": True, "baca_admin": False,
                 "pesan": [{"dari": "user", "nama": _nama_akun(email), "teks": teks, "waktu": kini}]}
            daftar.insert(0, t)
            while len(daftar) > CS_TIKET_MAKS:
                lama = [x for x in daftar if x.get("status") == "selesai"]
                daftar.remove(lama[-1] if lama else daftar[-1])
        else:
            tid = str(d.get("id") or "")
            t = next((x for x in daftar if x.get("id") == tid), None)
            if not t:
                return {"ok": False, "error": "Tiket tidak ditemukan"}
            if aksi == "balas":
                teks = _cs_teks(d.get("teks"))
                if len(teks) < 1:
                    return {"ok": False, "error": "Pesan kosong"}
                if len(t.get("pesan") or []) >= CS_PESAN_MAKS:
                    return {"ok": False, "error": "Percakapan tiket ini sudah panjang. Buat tiket baru."}
                t.setdefault("pesan", []).append({"dari": peran, "nama": "CS INTAI" if peran == "admin" else _nama_akun(email),
                                                  "teks": teks, "waktu": kini})
                t["status"] = "dibalas" if peran == "admin" else "terbuka"
                t["baca_user"], t["baca_admin"] = (False, True) if peran == "admin" else (True, False)
                t["diperbarui"] = kini
            elif aksi in ("selesai", "buka"):
                t["status"] = "selesai" if aksi == "selesai" else "terbuka"
                t["diperbarui"] = kini
                t["baca_" + peran] = True
            elif aksi == "baca":
                if t.get("baca_" + peran) is not False:
                    return {"ok": True, "tiket": t}   # sudah terbaca → tanpa tulis
                t["baca_" + peran] = True
            elif aksi == "hapus":
                daftar = [x for x in daftar if x.get("id") != tid]
                t = None
            else:
                return {"ok": False, "error": "aksi tidak dikenal"}
        if not setelan_tulis("tiket:" + pemilik, daftar):
            return {"ok": False, "error": "Gagal menyimpan tiket. Coba lagi sebentar."}
    return {"ok": True, "tiket": t}


# ---- v1.4.4: halaman Admin (akun, reset password, status sistem) + "Akun saya" (ubah nama / ganti password) ----
def _simpan_akun(email):
    """Tulis 1 akun dari AKUN_DB ke file lokal + Supabase (di luar `lock`)."""
    with lock:
        a = dict(AKUN_DB.get(email) or {})
    if not a:
        return False
    simpan_data()
    if supa_aktif():
        ok, _ = supa("POST", "intai_akun", payload={"email": email, "nama": a.get("nama") or email, "hash": a.get("hash")},
                     prefer="return=minimal,resolution=merge-duplicates")
        return bool(ok)
    return True


def _nama_valid(nama):
    nama = re.sub(r"\s+", " ", str(nama or "")).strip()[:60]
    if len(nama) < 2:
        return None, "Nama minimal 2 huruf"
    if "@" in nama:
        return None, "Nama tidak boleh berupa email"
    return nama, ""


def akun_ubah_sendiri(email, d):
    aksi = str(d.get("aksi") or "")
    if aksi == "nama":
        nama, err = _nama_valid(d.get("nama"))
        if not nama:
            return {"ok": False, "error": err}
        with lock:
            if email not in AKUN_DB:
                return {"ok": False, "error": "Akun tidak ditemukan"}
            AKUN_DB[email]["nama"] = nama
        if not _simpan_akun(email):
            return {"ok": False, "error": "Gagal menyimpan ke database. Coba lagi."}
        return {"ok": True, "nama": nama}
    if aksi == "password":
        lama, baru = str(d.get("lama") or ""), str(d.get("baru") or "")
        if len(baru) < 6:
            return {"ok": False, "error": "Password baru minimal 6 karakter"}
        with lock:
            h = (AKUN_DB.get(email) or {}).get("hash")
        if not cek_pw(lama, h)[0]:
            return {"ok": False, "error": "Password lama salah"}
        h_baru = hash_pw(baru)
        with lock:
            AKUN_DB[email]["hash"] = h_baru
        if not _simpan_akun(email):
            return {"ok": False, "error": "Gagal menyimpan ke database. Coba lagi."}
        return {"ok": True, "token": buat_sesi(email)}   # v1.4.7: perangkat ini tetap masuk, perangkat lain putus
    if aksi == "keluar_semua":   # v1.4.7
        tetap = putus_semua_sesi(email)
        return {"ok": True, "token": buat_sesi(email), "permanen": tetap}
    return {"ok": False, "error": "aksi tidak dikenal"}


def admin_data():
    _muat_jadwal()
    with lock:
        akun = [{"email": e, "nama": a.get("nama") or e} for e, a in AKUN_DB.items()]
    cfgs = JADWAL["cfgs"] or {}
    _, tiket = setelan_awalan("tiket:")
    for a in akun:
        c = cfgs.get(a["email"]) or {}
        a["niche"] = list(c.get("niche") or []) if c.get("aktif") else []
        tk = tiket.get("tiket:" + a["email"]) if isinstance(tiket, dict) else None
        a["tiket_terbuka"] = sum(1 for t in (tk or []) if isinstance(t, dict) and t.get("status") != "selesai")
        a["admin"] = is_admin(a["email"])
    akun.sort(key=lambda a: (not a["admin"], a["nama"].lower()))
    with kunci_jadwal:
        st = dict(JADWAL["status"] or {})
        t = JADWAL["thread"]
        berjalan = bool(t and t.is_alive())
        jam = JADWAL["jam"]
    h = st.get("hasil") or {}
    items = []
    for it in st.get("antrian") or []:
        r = h.get(it.get("k")) or {}
        items.append({"nama": it.get("nama"), "mode": it.get("mode"), "akun": len(it.get("u") or []),
                      "ok": r.get("ok"), "aktif": r.get("aktif"), "galat": r.get("galat") or "", "coba": r.get("coba", 0)})
    try:
        arsip = statistik_arsip()
    except Exception:
        arsip = {}
    return {
        "ok": True,
        "akun": akun,
        "sistem": {
            "versi": VERSI, "supabase": supa_aktif(), "akun_siap": akun_siap(), "setelan_siap": setelan_siap(),
            "admin": ADMIN_DAFTAR, "cs": cs_kontak(), "jam": jam, "kunci_wajib": bool(JADWAL_KUNCI),
            "email": {"layanan": mail_layanan(), "kunci": bool(MAIL_KUNCI), "dari": MAIL_DARI if mail_layanan() == "brevo" else "",
                      "terakhir": dict(MAIL_STATUS), "menit": RESET_MENIT},
            "sekarang_wib": datetime.now(WIB).strftime("%Y-%m-%d %H:%M"),
            "arsip": {k: arsip.get(k) for k in ("iklan", "iklan_aktif", "pengiklan", "snapshot", "niche") if k in arsip},
            "putaran": {"tanggal": st.get("tanggal"), "pemicu": st.get("pemicu"), "mulai": st.get("mulai"), "akhir": st.get("akhir"),
                        "berjalan": berjalan, "total": len(items), "ok": sum(1 for i in items if i["ok"]),
                        "gagal": sum(1 for i in items if i["ok"] is False and i["coba"] >= 2),
                        "sisa": len(_sisa(st)) if st else 0, "items": items},
        },
    }


# ---- v1.4.5: lupa password lewat email ----
# Render gratis memblokir SMTP (port 25/465/587) → email dikirim lewat HTTPS:
#   1) Google Apps Script milik admin (INTAI_MAIL_URL + INTAI_MAIL_KUNCI) — terkirim asli dari Gmail admin, ±100 email/hari
#   2) Brevo API (BREVO_API_KEY + INTAI_MAIL_DARI) — 300 email/hari; pengirim @gmail diganti Brevo jadi @brevosend.com
# Token reset tanpa tabel baru: HMAC(email|kedaluwarsa|sidik hash password) → otomatis sekali pakai (hash berubah setelah dipakai).
RESET_MENIT = 30
MAIL_URL = (os.environ.get("INTAI_MAIL_URL") or "").strip()
MAIL_KUNCI = (os.environ.get("INTAI_MAIL_KUNCI") or "").strip()
BREVO_KEY = (os.environ.get("BREVO_API_KEY") or "").strip()
MAIL_DARI = (os.environ.get("INTAI_MAIL_DARI") or "").strip()
MAIL_STATUS = {"t": None, "ok": None, "galat": "", "sisa": None}
_kunci_lupa = threading.Lock()
LUPA_JEJAK = {}   # "e:<email>" / "i:<ip>" -> [waktu]


def mail_layanan():
    if MAIL_URL.startswith("https://script.google.com/"):
        return "apps_script"
    if BREVO_KEY and "@" in MAIL_DARI:
        return "brevo"
    return ""


def kirim_email(ke, subjek, teks, html):
    """(ok, galat). Tidak pernah melempar exception."""
    lay = mail_layanan()
    try:
        if lay == "apps_script":
            r = requests.post(MAIL_URL, json={"kunci": MAIL_KUNCI, "ke": ke, "subjek": subjek, "teks": teks, "html": html},
                              timeout=25, allow_redirects=True)
            try:
                d = r.json()
            except Exception:
                d = {}
            ok, galat = bool(d.get("ok")), str(d.get("error") or ("" if d else f"HTTP {r.status_code} (bukan JSON - cek akses Web App = Anyone)"))
            if ok:
                MAIL_STATUS["sisa"] = d.get("sisa")
        elif lay == "brevo":
            r = requests.post("https://api.brevo.com/v3/smtp/email", timeout=25,
                              headers={"api-key": BREVO_KEY, "accept": "application/json", "content-type": "application/json"},
                              json={"sender": {"name": "INTAI", "email": MAIL_DARI}, "to": [{"email": ke}],
                                    "subject": subjek, "textContent": teks, "htmlContent": html})
            ok, galat = r.status_code in (200, 201, 202), ("" if r.status_code in (200, 201, 202) else f"Brevo HTTP {r.status_code}: {r.text[:160]}")
        else:
            ok, galat = False, "Layanan email belum diatur"
    except Exception as e:
        ok, galat = False, str(e)[:200]
    MAIL_STATUS.update({"t": datetime.now(WIB).strftime("%Y-%m-%d %H:%M"), "ok": ok, "galat": galat[:200]})
    if not ok:
        print(f"[INTAI EMAIL] gagal ke {ke}: {galat}")
    return ok, galat


def _sidik_pw(email):
    with lock:
        h = str((AKUN_DB.get(email) or {}).get("hash") or "")
    return hashlib.sha256(("sidik|" + h).encode("utf-8")).hexdigest()[:16] if h else ""


def _ttd_reset(isi, sidik):
    return hmac.new(SESI_RAHASIA, ("reset|" + isi + "|" + sidik).encode("utf-8"), hashlib.sha256).hexdigest()[:40]


def buat_token_reset(email):
    isi = _b64u(email.encode("utf-8")) + "." + str(int(time.time()) + RESET_MENIT * 60)
    return "r1." + isi + "." + _ttd_reset(isi, _sidik_pw(email))


def baca_token_reset(tok):
    """(email, '') bila sah · (None, alasan) bila tidak."""
    try:
        tok = str(tok or "").strip()
        if not tok.startswith("r1."):
            return None, "Tautan reset tidak sah."
        _, b, exp, sig = tok.split(".", 3)
        email = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8")
        sidik = _sidik_pw(email)
        if not sidik or not hmac.compare_digest(sig, _ttd_reset(b + "." + exp, sidik)):
            return None, "Tautan reset sudah dipakai atau tidak sah. Minta tautan baru."
        if int(exp) < time.time():
            return None, f"Tautan reset sudah kedaluwarsa (berlaku {RESET_MENIT} menit). Minta tautan baru."
        return email, ""
    except Exception:
        return None, "Tautan reset tidak sah."


# ---- v1.4.7: kode 6 angka di email (tetap bisa dipakai walau Gmail mematikan tautan di folder Spam) ----
# Disimpan di setelan "resetkode:<email>" (tahan server tidur/restart); cadangan memori bila tabel setelan belum siap.
KODE_MAKS_SALAH = 5
KODE_MEM = {}


def _kode_hash(email, kode):
    return hmac.new(SESI_RAHASIA, ("kode|" + email + "|" + kode).encode("utf-8"), hashlib.sha256).hexdigest()[:40]


def _kode_simpan(email, v):
    if setelan_tulis("resetkode:" + email, v):
        KODE_MEM.pop(email, None)
    else:
        KODE_MEM[email] = v


def _kode_baca(email):
    ok, v = setelan_baca("resetkode:" + email)
    if ok and isinstance(v, dict) and v.get("h"):
        return v
    v = KODE_MEM.get(email)
    return v if isinstance(v, dict) and v.get("h") else None


def buat_kode_reset(email):
    """Satu kode aktif per akun; permintaan baru menggantikan kode lama."""
    kode = f"{secrets.randbelow(10 ** 6):06d}"
    _kode_simpan(email, {"h": _kode_hash(email, kode), "exp": int(time.time()) + RESET_MENIT * 60,
                         "sidik": _sidik_pw(email), "salah": 0})
    return kode


def cek_kode_reset(email, kode):
    """(email, '') bila kode benar · (None, alasan). Tiap kode salah dihitung; 5x salah → kode hangus."""
    email = str(email or "").strip().lower()
    kode = re.sub(r"\D", "", str(kode or ""))
    if len(kode) != 6:
        return None, "Kode berisi 6 angka."
    salah_umum = "Kode salah atau sudah tidak berlaku. Periksa lagi, atau minta kode baru."
    v = _kode_baca(email)
    if not v:
        return None, salah_umum
    if int(v.get("exp") or 0) < time.time():
        return None, f"Kode sudah kedaluwarsa (berlaku {RESET_MENIT} menit). Minta kode baru."
    if v.get("sidik") != _sidik_pw(email):
        return None, "Kode sudah dipakai. Minta kode baru bila perlu."
    if int(v.get("salah") or 0) >= KODE_MAKS_SALAH:
        return None, f"Kode salah {KODE_MAKS_SALAH} kali. Demi keamanan, minta kode baru."
    if not hmac.compare_digest(str(v.get("h") or ""), _kode_hash(email, kode)):
        v["salah"] = int(v.get("salah") or 0) + 1
        _kode_simpan(email, v)
        if v["salah"] >= KODE_MAKS_SALAH:
            return None, f"Kode salah {KODE_MAKS_SALAH} kali. Demi keamanan, minta kode baru."
        return None, salah_umum
    return email, ""


def _samarkan(email):
    a, _, b = email.partition("@")
    return (a[:2] + "•" * max(1, min(6, len(a) - 2)) + "@" + b) if b else email


def _boleh_lupa(kunci, maks, detik):
    now = time.time()
    with _kunci_lupa:
        j = [t for t in LUPA_JEJAK.get(kunci, []) if now - t < detik]
        if len(j) >= maks:
            LUPA_JEJAK[kunci] = j
            return False
        j.append(now)
        LUPA_JEJAK[kunci] = j
        if len(LUPA_JEJAK) > 5000:
            for k in list(LUPA_JEJAK)[:2500]:
                LUPA_JEJAK.pop(k, None)
        return True


def email_reset(email, basis):
    with lock:
        nama = (AKUN_DB.get(email) or {}).get("nama") or "Pengguna INTAI"
    link = basis.rstrip("/") + "/reset?t=" + buat_token_reset(email)
    kode = buat_kode_reset(email)
    kode_tampil = kode[:3] + " " + kode[3:]
    teks = (f"Halo {nama},\n\nAda permintaan reset password untuk akun INTAI {email}.\n\n"
            f"KODE RESET: {kode_tampil}\n"
            "Ketik kode ini di layar Lupa password INTAI.\n\n"
            f"Atau buka tautan ini (berlaku {RESET_MENIT} menit, sekali pakai):\n{link}\n\n"
            "Email ini masuk folder Spam? Tautan dimatikan Gmail di sana. Pakai kodenya, atau tekan 'Bukan spam'.\n\n"
            "Kalau bukan kamu yang meminta, abaikan email ini. Password kamu tidak berubah.\n\nINTAI · Intelijen Jualan")
    from html import escape as _e
    html = ('<div style="font-family:Arial,Helvetica,sans-serif;max-width:480px;margin:auto;color:#051F20">'
            '<div style="background:#0B2B26;color:#DAF1DE;padding:16px 20px;border-radius:14px 14px 0 0;font-weight:800;font-size:18px">◆ INTAI</div>'
            '<div style="border:1px solid #8EB69B;border-top:0;border-radius:0 0 14px 14px;padding:20px">'
            f'<p>Halo <b>{_e(nama)}</b>,</p><p>Ada permintaan reset password untuk akun INTAI <b>{_e(email)}</b>.</p>'
            '<p style="margin:18px 0 6px;font-size:13px;color:#235347;text-align:center">Kode reset (ketik di layar <b>Lupa password</b> INTAI)</p>'
            f'<div style="text-align:center;font-family:Consolas,Menlo,monospace;font-size:32px;font-weight:800;letter-spacing:6px;'
            f'background:#EEF7F0;border:1px dashed #8EB69B;border-radius:12px;padding:12px;color:#051F20">{_e(kode_tampil)}</div>'
            '<p style="text-align:center;font-size:13px;color:#7a8f86;margin:16px 0 0">atau</p>'
            f'<p style="text-align:center;margin:12px 0 24px"><a href="{_e(link)}" style="background:#235347;color:#fff;text-decoration:none;'
            'padding:12px 22px;border-radius:999px;font-weight:800;display:inline-block">Buat password baru</a></p>'
            f'<p style="font-size:13px;color:#235347">Tautan berlaku {RESET_MENIT} menit dan hanya bisa dipakai sekali. '
            'Kalau bukan kamu yang meminta, abaikan email ini. Password kamu tidak berubah.</p>'
            '<p style="font-size:12px;color:#8A5A12;background:#FFF6E5;border-radius:8px;padding:8px 10px">Email ini ada di folder <b>Spam</b>? '
            'Gmail mematikan tombol di sana. Pakai <b>kode</b> di atas, atau tekan <b>Bukan spam</b> supaya tombolnya aktif.</p>'
            f'<p style="font-size:12px;color:#7a8f86;word-break:break-all">Tombol tidak bisa diklik? Salin alamat ini: {_e(link)}</p>'
            '</div></div>')
    return kirim_email(email, "Kode reset password INTAI: " + kode_tampil, teks, html)


def lupa_password(email, ip, basis):
    """Jawaban SELALU sama apakah email terdaftar atau tidak (cegah tebak-tebakan akun). Email dikirim di latar."""
    if not mail_layanan():
        return {"ok": False, "mail": False, "error": "Reset lewat email belum diaktifkan. Hubungi CS."}
    email = str(email or "").strip().lower()
    if "@" not in email:
        return {"ok": False, "mail": True, "error": "Format email belum benar."}
    if not _boleh_lupa("i:" + ip, 10, 3600):
        return {"ok": False, "mail": True, "error": "Terlalu banyak permintaan. Coba lagi 1 jam lagi."}
    with lock:
        ada = email in AKUN_DB
    if ada and _boleh_lupa("e1:" + email, 1, 60) and _boleh_lupa("e:" + email, 4, 3600):
        threading.Thread(target=email_reset, args=(email, basis), daemon=True, name="email-reset").start()
    return {"ok": True, "mail": True, "menit": RESET_MENIT}


def reset_dengan_token(tok, pw_baru, email_kode=None):
    if email_kode:
        email, alasan = cek_kode_reset(email_kode[0], email_kode[1])
    else:
        email, alasan = baca_token_reset(tok)
    if not email:
        return {"ok": False, "error": alasan}
    if len(str(pw_baru or "")) < 6:
        return {"ok": False, "error": "Password baru minimal 6 karakter"}
    h_baru = hash_pw(str(pw_baru))
    with lock:
        if email not in AKUN_DB:
            return {"ok": False, "error": "Akun tidak ditemukan"}
        h_lama = AKUN_DB[email].get("hash")
        AKUN_DB[email]["hash"] = h_baru
    if not _simpan_akun(email):
        with lock:
            AKUN_DB[email]["hash"] = h_lama
        return {"ok": False, "error": "Gagal menyimpan ke database. Coba lagi."}
    if email_kode:
        _kode_simpan(email, {})
    print(f"[INTAI] password {email} diganti lewat {'kode' if email_kode else 'tautan'} email")
    with lock:
        nama = AKUN_DB[email].get("nama") or email
    return {"ok": True, "email": email, "nama": nama}


def basis_url(handler):
    """Alamat publik utk tautan email. Tidak memakai header Host mentah di server publik (cegah tautan palsu)."""
    for k in ("INTAI_URL", "RENDER_EXTERNAL_URL"):
        v = (os.environ.get(k) or "").strip()
        if v.startswith("http"):
            return v.rstrip("/")
    host = str(handler.headers.get("Host") or "").strip()
    if re.fullmatch(r"(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d{1,5})?", host) or re.fullmatch(r"[a-z0-9-]+\.onrender\.com", host) \
            or re.fullmatch(r"\d{2,5}-[a-z0-9]+\.e2b\.app", host):
        return ("https://" if not host.startswith(("localhost", "127.", "0.0.0.0")) else "http://") + host
    return "http://localhost:" + str(PORT)


_HURUF_PW = "abcdefghjkmnpqrstuvwxyz23456789"


def admin_ubah(email_admin, d, basis=""):
    aksi = str(d.get("aksi") or "")
    target = str(d.get("email") or "").strip().lower()
    with lock:
        ada = target in AKUN_DB
    if not ada:
        return {"ok": False, "error": "Akun tidak ditemukan"}
    if aksi == "putus_sesi":   # v1.4.7
        tetap = putus_semua_sesi(target)
        print(f"[INTAI ADMIN] {email_admin} mengeluarkan {target} dari semua perangkat")
        return {"ok": True, "email": target, "permanen": tetap}
    if aksi == "kirim_reset":
        if not mail_layanan():
            return {"ok": False, "error": "Layanan email belum diatur (INTAI_MAIL_URL / BREVO_API_KEY di Render)."}
        ok, galat = email_reset(target, basis)
        if not ok:
            return {"ok": False, "error": "Email gagal terkirim: " + galat}
        print(f"[INTAI ADMIN] {email_admin} mengirim tautan reset ke {target}")
        return {"ok": True, "email": target, "menit": RESET_MENIT}
    if aksi == "reset_pw":
        pw = "".join(secrets.choice(_HURUF_PW) for _ in range(10))
        h_baru = hash_pw(pw)
        with lock:
            AKUN_DB[target]["hash"] = h_baru
        if not _simpan_akun(target):
            return {"ok": False, "error": "Gagal menyimpan ke database. Password TIDAK berubah di Supabase; coba lagi."}
        print(f"[INTAI ADMIN] {email_admin} mereset password {target}")
        return {"ok": True, "password": pw, "email": target}
    if aksi == "nama":
        nama, err = _nama_valid(d.get("nama"))
        if not nama:
            return {"ok": False, "error": err}
        with lock:
            AKUN_DB[target]["nama"] = nama
        if not _simpan_akun(target):
            return {"ok": False, "error": "Gagal menyimpan ke database. Coba lagi."}
        return {"ok": True, "nama": nama}
    return {"ok": False, "error": "aksi tidak dikenal"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def tulis(self, kode, isi, tipe, ekstra=None):
        isi = isi if isinstance(isi, bytes) else isi.encode("utf-8")
        gz = len(isi) > 20000 and "gzip" in (self.headers.get("Accept-Encoding") or "") and tipe.startswith("application/json")
        if gz:
            import gzip
            isi = gzip.compress(isi, 5)
        self.send_response(kode)
        self.send_header("Content-Type", tipe)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")  # preview iframe (origin null)
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(isi)))
        for k, v in (ekstra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(isi)

    # ---- v1.4: sesi ----
    def email_sesi(self):
        tok = self.headers.get("X-Intai-Sesi") or ""
        if not tok:
            m = re.search(r"(?:^|;\s*)" + SESI_COOKIE + r"=([^;]+)", self.headers.get("Cookie") or "")
            tok = unquote(m.group(1)) if m else ""
        return baca_sesi(tok)

    def cookie_sesi(self, tok, umur):
        https = (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"
        return ("Set-Cookie", f"{SESI_COOKIE}={tok}; Path=/; Max-Age={umur}; HttpOnly; "
                              + ("SameSite=None; Secure" if https else "SameSite=Lax"))

    def gerbang(self, path, publik):
        """Semua /api/* & /scan wajib masuk, kecuali `publik`. Return False bila sudah dijawab 401."""
        self.email = None
        if (path.startswith("/api/") or path == "/scan") and path not in publik:
            self.email = self.email_sesi()
            if not self.email:
                self.tulis(401, json.dumps({"ok": False, "masuk": False, "error": "Silakan masuk dulu"}), "application/json")
                return False
        return True

    def json_ok(self, obj):
        self.tulis(200, json.dumps(obj, ensure_ascii=False, default=str), "application/json")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Intai-Sesi")
        self.end_headers()

    def baca_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html", "/dashboard", "/meta", "/shopee", "/tiktok", "/pesaing", "/tren", "/galeri", "/ekspor", "/cs", "/admin", "/reset") \
                or re.fullmatch(r"/pengiklan/\d{1,40}/?", u.path):
            self.tulis(200, muat_halaman(), "text/html; charset=utf-8")
        elif not self.gerbang(u.path, ("/api/versi", "/api/me", "/api/jadwal/tick")):
            return
        elif u.path == "/api/versi":
            self.tulis(200, json.dumps({"ok": True, "v": VERSI, "cs": cs_kontak()}), "application/json")   # v1.4.4: kontak CS utk layar Masuk (lupa password)
        elif u.path == "/api/me":
            email = self.email_sesi() or baca_sesi(parse_qs(u.query).get("token", [""])[0])
            with lock:
                akun = AKUN_DB.get(email) if email else None
            if akun:
                self.tulis(200, json.dumps({"ok": True, "nama": akun["nama"], "email": email,
                                            "akun_siap": akun_siap(), "admin": is_admin(email)}), "application/json")
            else:
                self.tulis(200, json.dumps({"ok": False}), "application/json")
        elif u.path == "/api/arsip":
            d = dict(statistik_arsip())
            try:
                d["jadwal"] = jadwal_ringkas(self.email)
            except Exception:
                d["jadwal"] = {"siap": False}
            if d.get("p2_siap"):
                d["pantau"] = jumlah_pantau(self.email)
            self.json_ok(d)
        elif u.path == "/api/arsip/status":
            self.tulis(200, json.dumps(dict(ARSIP_STATUS, ok=True), ensure_ascii=False), "application/json")
        elif u.path == "/api/pengiklan/jejak":   # v1.4.7 M7
            q = parse_qs(u.query)
            self.json_ok(jejak_pengiklan(q.get("id", [""])[0], q.get("segar", [""])[0] == "1"))
        elif u.path == "/api/pengiklan":
            pid = parse_qs(u.query).get("id", [""])[0]
            self.tulis(200, json.dumps(profil_pengiklan(pid, self.email), ensure_ascii=False), "application/json")
        elif u.path == "/api/iklan/status":
            ids = parse_qs(u.query).get("ids", [""])[0]
            self.tulis(200, json.dumps(pantauan_status(ids), ensure_ascii=False, default=str), "application/json")
        elif u.path == "/api/pantau":
            self.tulis(200, json.dumps(daftar_pantau(self.email), ensure_ascii=False), "application/json")
        elif u.path == "/api/bintang":
            self.json_ok(bintang_baca(self.email))
        elif u.path == "/api/ekspor":
            self.json_ok(ekspor_baca(self.email))
        elif u.path == "/api/cs":
            self.json_ok(cs_baca(self.email))
        elif u.path == "/api/admin":
            if not is_admin(self.email):
                self.tulis(403, json.dumps({"ok": False, "error": "Khusus admin"}), "application/json")
            else:
                self.json_ok(admin_data())
        elif u.path == "/api/jadwal":
            self.tulis(200, json.dumps(jadwal_publik(self.email), ensure_ascii=False), "application/json")
        elif u.path == "/api/jadwal/tick":
            kunci = parse_qs(u.query).get("kunci", [""])[0]
            if JADWAL_KUNCI and not hmac.compare_digest(kunci.encode(), JADWAL_KUNCI.encode()):
                self.tulis(403, json.dumps({"ok": False, "pesan": "kunci salah"}), "application/json")
                return
            self.tulis(200, json.dumps(jadwal_tick()), "application/json")   # ringkas (< 200 byte) utk cron-job.org
        elif u.path == "/api/galeri":
            prm = {k: v[0] for k, v in parse_qs(u.query).items()}
            self.tulis(200, json.dumps(galeri_iklan(prm), ensure_ascii=False), "application/json")
        elif u.path == "/api/galeri.csv":
            prm = {k: v[0] for k, v in parse_qs(u.query).items()}
            isi = galeri_csv(prm).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="intai-galeri-{datetime.now(WIB).strftime("%Y%m%d-%H%M")}.csv"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(isi)
        elif u.path == "/api/tren":
            self.tulis(200, json.dumps(tren_niche(parse_qs(u.query).get("niche", [""])[0], self.email), ensure_ascii=False),
                       "application/json")
        elif u.path == "/api/peristiwa":
            qs = parse_qs(u.query)
            self.tulis(200, json.dumps(peristiwa_feed(qs.get("lingkup", ["pantau"])[0],
                                                      _int(qs.get("limit", ["60"])[0], 60), self.email),
                                       ensure_ascii=False), "application/json")
        elif u.path == "/api/ringkasan":
            self.tulis(200, json.dumps(ringkasan_api(self.email), ensure_ascii=False), "application/json")
        elif u.path == "/scan":
            qs = parse_qs(u.query)
            nilai = qs.get("q", [""])[0].strip()
            mode = qs.get("mode", ["cari"])[0]
            if not nilai:
                self.tulis(200, json.dumps({"ok": False, "error": "Input kosong"}),
                           "application/json")
                return
            with lock:
                try:
                    hasil = jalankan_scan(nilai, mode, pemilik=self.email)
                except Exception as e:
                    hasil = {"ok": False, "error": str(e)}
            self.tulis(200, json.dumps(hasil, ensure_ascii=False), "application/json")
        elif u.path == "/img":
            target = parse_qs(u.query).get("u", [""])[0]
            data = ambil_gambar(target) if target else None
            if data:
                self.tulis(200, data[1], data[0])
            else:
                self.tulis(404, b"Gagal memuat gambar", "text/plain")
        elif u.path == "/vid":
            target = parse_qs(u.query).get("u", [""])[0]
            data = ambil_video(target) if target else None
            if not data:
                self.tulis(404, b"Gagal memuat video", "text/plain")
                return
            ctype, isi = data
            rng = self.headers.get("Range") or ""
            if rng.startswith("bytes="):
                try:
                    a, b_ = rng[6:].split("-", 1)
                    a = int(a) if a else 0
                    b_ = int(b_) if b_ else len(isi) - 1
                    b_ = min(b_, len(isi) - 1)
                    potong = isi[a:b_ + 1]
                    self.send_response(206)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {a}-{b_}/{len(isi)}")
                    self.send_header("Content-Length", str(len(potong)))
                    self.end_headers()
                    self.wfile.write(potong)
                    return
                except Exception:
                    pass
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(isi)))
            self.end_headers()
            self.wfile.write(isi)
        else:
            self.tulis(404, "Not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        if not self.gerbang(u.path, ("/api/login", "/api/register", "/api/logout", "/api/lupa", "/api/reset")):
            return
        if u.path == "/api/lupa":   # v1.4.5
            d = self.baca_json()
            ip = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or self.client_address[0]
            self.json_ok(lupa_password(d.get("email"), ip, basis_url(self)))
            return
        if u.path == "/api/reset":   # v1.4.5
            d = self.baca_json()
            ip = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or self.client_address[0]
            pakai_kode = bool(d.get("kode"))
            if (pakai_kode or not d.get("cek")) and not _boleh_lupa("r:" + ip, 20, 3600):
                self.json_ok({"ok": False, "error": "Terlalu banyak percobaan. Coba lagi 1 jam lagi."})
                return
            if d.get("cek"):
                if pakai_kode:
                    em, alasan = cek_kode_reset(d.get("email"), d.get("kode"))
                else:
                    em, alasan = baca_token_reset(d.get("t"))
                self.json_ok({"ok": bool(em), "email": _samarkan(em) if em else "", "error": alasan})
                return
            h = reset_dengan_token(d.get("t"), d.get("password"), (d.get("email"), d.get("kode")) if pakai_kode else None)
            if h.get("ok"):
                tok = buat_sesi(h["email"])
                self.tulis(200, json.dumps({"ok": True, "token": tok, "nama": h["nama"], "email": h["email"]}),
                           "application/json", [self.cookie_sesi(tok, SESI_HARI * 86400)])
            else:
                self.json_ok(h)
            return
        if u.path == "/api/login":
            d = self.baca_json()
            email = str(d.get("email") or "").strip().lower()
            pw = str(d.get("password") or "")
            with lock:
                akun = dict(AKUN_DB.get(email) or {})
            oke, upgrade = cek_pw(pw, akun.get("hash")) if akun else (False, False)  # hashing di luar lock
            if oke:
                nama = akun["nama"]
                if upgrade:  # akun v0.7 (sha256) → otomatis naik ke PBKDF2 saat login
                    h_baru = hash_pw(pw)
                    with lock:
                        if email in AKUN_DB:
                            AKUN_DB[email]["hash"] = h_baru
                    simpan_data()
                    if supa_aktif():
                        supa("POST", "intai_akun", payload={"email": email, "nama": nama, "hash": h_baru},
                             prefer="return=minimal,resolution=merge-duplicates")
                tok = buat_sesi(email)   # v1.4.7: SETELAH upgrade hash (token terikat ke hash)
            if oke:
                self.tulis(200, json.dumps({"ok": True, "token": tok, "nama": nama, "email": email}),
                           "application/json", [self.cookie_sesi(tok, SESI_HARI * 86400)])
            else:
                self.tulis(200, json.dumps({"ok": False, "error": "Email atau password salah"}),
                           "application/json")
        elif u.path == "/api/register":
            d = self.baca_json()
            nama = str(d.get("nama") or "").strip()
            email = str(d.get("email") or "").strip().lower()
            pw = str(d.get("password") or "")
            if "@" in nama:
                self.tulis(200, json.dumps({"ok": False, "error": "Kolom Nama berisi email. Isi dengan nama kamu."}),
                           "application/json")
                return
            if not nama or len(pw) < 6 or "@" not in email or "." not in email.split("@")[-1]:
                self.tulis(200, json.dumps({"ok": False, "error": "Data belum lengkap (password min. 6 karakter)"}),
                           "application/json")
                return
            with lock:
                if email in AKUN_DB:
                    self.tulis(200, json.dumps({"ok": False, "error": "Email sudah terdaftar di mesin ini"}),
                               "application/json")
                    return
                h_pw = hash_pw(pw)  # sekali saja (salt acak → tiap panggilan beda)
                AKUN_DB[email] = {"nama": nama, "hash": h_pw}
            tok = buat_sesi(email)
            simpan_data()
            if supa_aktif():
                supa("POST", "intai_akun",
                     payload={"email": email, "nama": nama, "hash": h_pw},
                     prefer="return=minimal,resolution=merge-duplicates")
            self.tulis(200, json.dumps({"ok": True, "token": tok, "nama": nama, "email": email}),
                       "application/json", [self.cookie_sesi(tok, SESI_HARI * 86400)])
        elif u.path == "/api/jadwal":
            d = self.baca_json()
            if str(d.get("aksi") or "") == "jalan":
                hasil = jadwal_tick(paksa=True, pemicu="manual", email=self.email)
            else:
                hasil = jadwal_simpan(self.email, d)
            self.tulis(200, json.dumps(hasil, ensure_ascii=False), "application/json")
        elif u.path == "/api/pantau":
            d = self.baca_json()
            hasil = ubah_pantau(self.email, str(d.get("aksi") or "tambah"), d.get("page_id"), d.get("nama") or "")
            self.tulis(200, json.dumps(hasil, ensure_ascii=False), "application/json")
        elif u.path == "/api/bintang":
            self.json_ok(bintang_tulis(self.email, self.baca_json().get("daftar")))
        elif u.path == "/api/ekspor":
            self.json_ok(ekspor_ubah(self.email, self.baca_json()))
        elif u.path == "/api/cs":
            self.json_ok(cs_ubah(self.email, self.baca_json()))
        elif u.path == "/api/akun":
            h = akun_ubah_sendiri(self.email, self.baca_json())
            if h.get("token"):
                self.tulis(200, json.dumps(h, ensure_ascii=False), "application/json", [self.cookie_sesi(h["token"], SESI_HARI * 86400)])
            else:
                self.json_ok(h)
        elif u.path == "/api/admin":
            if not is_admin(self.email):
                self.tulis(403, json.dumps({"ok": False, "error": "Khusus admin"}), "application/json")
            else:
                self.json_ok(admin_ubah(self.email, self.baca_json(), basis_url(self)))
        elif u.path == "/api/logout":
            self.tulis(200, json.dumps({"ok": True}), "application/json", [self.cookie_sesi("", 0)])
        else:
            self.tulis(404, json.dumps({"ok": False, "error": "Tidak ditemukan"}), "application/json")


def main():
    muat_data()
    with lock:
        n_riwayat = len(RIWAYAT)
    threading.Thread(target=jantung_terowongan, daemon=True, name="keepalive").start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[INTAI APP v{VERSI} HUB] Hidup di http://localhost:{PORT}  (dashboard: / · scan: /meta · Ctrl+C berhenti)")
    print(f"[INTAI APP] Penyimpanan: {'Supabase + cadangan lokal' if supa_aktif() else 'lokal saja (' + os.path.basename(BERKAS_DATA) + ')'}")
    if n_riwayat:
        print(f"[INTAI APP] {n_riwayat} riwayat scan dimuat")
    try:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[INTAI APP] Dimatikan.")


if __name__ == "__main__":
    main()
