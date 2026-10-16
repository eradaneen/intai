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
import zlib
import webbrowser
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import OrderedDict
from urllib.parse import urlparse, parse_qs, quote, unquote, urljoin

import requests
import intai_mvp as mesin

PORT = int(os.environ.get("PORT") or 8765)   # env PORT utk deploy (HF Spaces/Render set otomatis)
VERSI = "1.5.10"
FOTO_MAKS = 12
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BERKAS_DATA = os.path.join(BASE_DIR, "intai_data.json")
lock = threading.RLock()   # reentrant: jalankan_scan dipanggil di bawah lock, catat_scan ikut lock
img_cache = OrderedDict()   # v1.4.7b: LRU dibatasi BYTE (dulu 300 item tanpa batas ukuran → RAM bisa habis)
# v1.4.7b: scan diantre dengan kunci SENDIRI. Dulu memakai `lock` global yang juga dipakai cek sesi/login →
# selama ada scan (±20 dtk), semua request user lain ikut tertahan ("situs hang").
kunci_scan = threading.Lock()
_kunci_scan_jalan = threading.Lock()
SCAN_JALAN = set()          # akun yang scan-nya sedang berjalan (maks 1 per akun)
SCAN_PER_JAM = 60           # per akun (admin bebas). "Cek semua pesaing" bisa 30 scan berurutan.
SCAN_ANTRE_DTK = 240        # menunggu giliran scan maksimal 4 menit
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


# ===== v1.6 Relevansi v3 (mesin tajam) =====
# 1) kata utuh + imbuhan (tas ≠ kualitas); 2) huruf hias dinormalkan (NFKC);
# 3) POSISI: judul/nama halaman/tautan/deskripsi/judul kartu/awal teks = subjek; sekali di tengah teks panjang = sambil lalu;
# 4) frasa multi-kata harus berdekatan; 5) sinonim ketat setara, "senada" (KELUARGA_KATA) maks Longgar;
# 6) derau keras: novel/drama, bahasa asing, iklan artikel, iklan platform, politik; 7) konteks halaman.
import unicodedata
# Sinonim KETAT: barang yang sama (juga dipakai untuk perluasan kata kunci saat scan)
SINONIM = [
    ["tas", "tas wanita", "totebag", "sling bag", "tas selempang", "ransel", "tas pria", "tote bag", "bag", "handbag", "backpack", "clutch", "shoulder bag"],
    ["hijab", "jilbab", "kerudung", "pashmina", "bergo", "khimar", "hijab instan", "ciput", "hijab segi empat"],
    ["sepatu", "sepatu wanita", "sepatu pria", "sneakers", "sepatu anak", "pantofel", "sneaker", "shoes", "loafer", "boots", "running shoes"],
    ["sandal", "sendal", "sandals", "slipper", "selop"],
    ["kopi", "kopi susu", "biji kopi", "kopi bubuk", "arabika", "robusta", "kopi literan", "coffee", "espresso", "cold brew"],
    ["madu", "madu murni", "madu hutan", "madu asli", "madu anak", "honey"],
    ["skincare", "skin care", "serum", "moisturizer", "pelembab", "sunscreen", "tabir surya", "toner",
     "facial wash", "sabun muka", "krim wajah", "face cream", "cleanser", "essence", "ampoule"],
    ["mainan anak", "mainan edukasi", "mainan edukatif", "mainan bayi", "mainan", "toys", "toy", "mainan anak laki", "mainan anak perempuan"],
    ["rak sepatu", "lemari sepatu", "rak sendal", "kabinet sepatu", "rak sepatu susun", "shoe rack", "shoebox"],
    ["parfum", "perfume", "parfume", "eau de parfum", "edp", "fragrance", "minyak wangi"],
    ["kaos", "t-shirt", "tshirt", "kaos polos", "tee"],
    ["gamis", "abaya", "dress muslim", "longdress", "maxi dress"],
    ["kemeja", "shirt", "hem", "blouse"],
    ["celana", "jeans", "pants", "chino", "jogger", "kulot", "legging"],
    ["jam tangan", "arloji", "watch", "smartwatch"],
    ["helm", "helmet"],
    ["kosmetik", "makeup", "make up", "lipstik", "lipstick", "lip tint", "cushion", "foundation", "bedak", "maskara"],
    ["susu", "milk", "susu formula", "susu bubuk"],
    ["popok", "diapers", "pampers", "popok bayi"],
    ["suplemen", "supplement", "vitamin", "multivitamin", "kapsul herbal"],
    ["pelangsing", "diet", "pelangsing badan", "fat burner", "slimming"],
    ["kasur", "springbed", "spring bed", "matras", "mattress"],
    ["kursi", "chair", "sofa", "bangku"],
    ["meja", "table", "desk"],
    ["lemari", "wardrobe", "kabinet", "cabinet"],
    ["sabun", "soap", "body wash", "sabun mandi"],
    ["shampoo", "sampo", "shampo", "hair tonic", "conditioner"],
    ["hp", "handphone", "smartphone", "ponsel", "iphone", "android"],
    ["laptop", "notebook", "macbook", "chromebook"],
    ["earphone", "tws", "earbuds", "headset", "headphone"],
    ["frozen food", "makanan beku", "nugget", "sosis", "dimsum"],
    ["sambal", "sambel", "chili oil", "saus sambal"],
    ["keripik", "kripik", "chips", "snack"],
    ["obat lambung", "obat maag", "obat magh", "obat asam lambung", "asam lambung", "maag", "magh", "gerd", "lambung"],
    ["obat jerawat", "jerawat", "acne", "anti acne", "acne spot"],
]

PLATFORM = {"chatgpt", "openai", "disney+", "disney+ hotstar", "netflix", "vidio", "prime video", "viu", "wetv", "iqiyi", "spotify", "youtube", "shopee", "tokopedia", "lazada", "tiktok", "tiktok shop", "blibli", "blibli.com", "bukalapak",
            "shopeepay", "spaylater", "shopeefood indonesia", "shopeefood", "gopay", "gopay indonesia", "dana",
            "ovo", "grab", "grabfood", "gojek", "google", "google indonesia", "google gemini", "gemini", "alfamart", "indomaret", "akulaku", "kredivo", "temu", "zalora"}
PLATFORM_GENERIK = re.compile(
    r"belanja semua kebutuhan|download (aplikasi|shopee|sekarang)|yuk,? download|serbu rabu|xtra voucher|"
    r"cek keranjang belanja|cod cek dulu|jaminan 100% uang kembali|bayar qris|aplikasi gopay|"
    r"temukan koleksi .{0,40} di shopee|diskon shopee dari toko|apps\.apple\.com|play\.google\.com|itunes\.apple\.com", re.I)

NOVEL_HAL = re.compile(r"novel|books?\b|reading|reads\b|story|stories|romance|drama|cerita|bacaan|webtoon|"
                       r"komik|fiction|chapter|heartbeat|love ?story|happily ever|goodnovel|fizzo|dreame|"
                       r"[\u3040-\u30ff\u4e00-\u9fff]", re.I)
EKSPLISIT = re.compile(r"\b(tembus (pandang )?pakaian|kemaluan|meraba|mendesah|desahan|bercinta|gairah|kehidupan seks|ibu angkat|ayah angkat|perselingkuhan|selingkuh(an)?ku|kaisar langit|sekte|kultivasi|ceo dingin|istri kontrak)\b")
DIALOG = re.compile(r"[\"“][^\"”\n]{4,160}[\"”]")
NOVEL_TEKS = re.compile(r"🔞|baca (gratis|selengkapnya|versi lengkap|bab)|klik (untuk|di sini untuk) membaca|"
                        r"\bbab \d|\bchapter \d|instal dan baca|do not read in public|jangan membaca di tempat umum|"
                        r"lire la version|drama pendek|short drama|full episode|tonton drama|nonton drama|"
                        r"buku ini ada micin|makin dibaca makin|versi lengkapnya|click here to read|click to read|read more exciting|read on\s*👉|continue reading|lightreader|webnovel|"
                        r"\bjin (?:di )?dalam lampu|suami jin", re.I)
# v1.5.3 kampanye donasi / galang dana
DONASI = re.compile(r"\b(?:yuk|ayo|mari) (?:donasi|berdonasi|wakaf|sedekah)\b|\b(?:yuk|ayo|mari) bantu (?:mereka|sesama|saudara|adik.adik|yatim|dhuafa|korban|para|kucing|anabul|hewan)\b|\bdonasi (?:sekarang|terbaik)\b|\b(?:galang|penggalangan) dana\b|\bkitabisa\b|"
                    r"\b(?:butuh|membutuhkan) bantuan (?:teman|kita|kamu|orang|para)\b|\b(?:kucing|anjing|hewan|anak|lansia|warga|santri|yatim|dhuafa)[a-z0-9 .+]{0,25} (?:butuh|membutuhkan) bantuan\b|"
                    r"\b(?:yuk|ayo) wakaf\b|\bwakaf al.?qur|"
                    r"\bdonate (?:now|today|here)\b|\b(?:emergency|humanitarian) (?:aid|appeal|relief)\b|\b(?:give|pay) (?:your )?(?:zakat|sadaqah)\b", re.I)   # v1.5.3 donasi en (Mohammed Hijab)
DONASI_LINK = re.compile(r"/campaign/(?=[^?]*(?:donasi|bantu|peduli|sedekah|wakaf|zakat|yatim|dhuafa|sakit|operasi|korban|kucing|anabul))|kitabisa\.|bantuteman\.|kawanbantu|sharinghappiness|dompetdhuafa|rumahzakat|baznas|laznas|amalsholeh|wecare\.id", re.I)
# v1.5.3 tautan aplikasi novel/drama (play.google … id=com.newleaf.app)
NOVEL_APP = re.compile(r"newleaf|dramabox|reelshort|goodnovel|novelah|fizzo|stardust|shortmax|flextv|netshort|moboreels|joyread|hinovel|novelme|innovel|chereads|kiss ?novel|webfic|goodshort", re.I)
POLITIK = re.compile(r"\b(bupati|gubernur|wali ?kota|partai|caleg|pemilu|pilkada|pileg|dprd?|kampanye|capres|cawapres|"
                     r"relawan|psi|pdip|gerindra|golkar|nasdem|ganjar|prabowo|jokowi|anies|"
                     r"coblos|nyoblos|nomor urut|pilih nomor (?:urut )?\d|"   # v1.5.3 "Coblos #JilbabIjo", "Pilih nomor 1, yang pakai kerudung"
                     r"cabup|cawabup|cagub|cawagub|cawalkot|cawali|paslon|calon (?:bupati|gubernur|wali ?kota|legislatif|anggota dewan))\b", re.I)
NEGASI = re.compile(r"\b(bukan|tanpa|tidak pakai|gak pakai|ga pake|selain|(?:gak|ga|nggak|ngga|enggak|tidak|tak|ndak) (?:perlu|usah|butuh)(?: lagi)?|gausah|"
                    r"stop|berhenti(?: pakai| beli)?|lupakan|selamat tinggal|beralih dari|daripada|dibanding(?:kan)?|ketimbang|"
                    r"(?:habiskan|buang|boros) (?:uang|duit) (?:untuk|buat|beli))\s+([^\s.!?,;:]+\s+)?$")   # v1.5.4 pembanding: "Nggak perlu obat nyamuk lagi" (iklan kelambu)   # v1.5.2: "anti" dibuang ("Anti Tas Berantakan" = tas organizer)
# v1.5.2: kata kunci sebagai KETERANGAN produk lain — "Helm Hijab", "Mesin Kopi", "Bros Hijab", "Rak Sepatu" (utk niche sepatu)
# v1.5.3: tagar yg kebetulan diawali/diakhiri kata niche (#portable→table, #teether→tee)
TAGAR_PALSU = {"portable", "vegetable", "vegetables", "comfortable", "adjustable", "foldable", "washable", "notable", "stable", "suitable",
               "teether", "teethers", "teen", "teens", "teenager", "teeth", "sofabed", "bagus", "bagusbanget", "baguss",
               "hemat", "hematbanget", "hemmm", "capek", "tasty", "tasting", "mejikuhibiniu", "susunan", "tasik", "tasikmalaya"}
KEPALA_DAFTAR = re.compile(r"\b(penyimpanan|organizer|rak|kotak|wadah|tempat|storage)\s+(?:[a-z]+\s*(?:,|&|dan|\+)\s*){1,3}$")
KEPALA_LAIN = re.compile(
    r"\b(popok|diaper|diapers|kerak|noda|ayam|udang|cumi|bebek|ikan|nasi|tahu|tempe|telur|geprek|penyet|helm|bros|jarum|sarung|taplak|hias|hiasan|tanaman|alas|tas|ransel|backpack|softcase|hardcase|peniti|gantungan|ganci|charm|organizer|rak|lemari|mesin|alat|gelas|cangkir|mug|tumbler|"
    r"lem|semir|sikat|tali|pasak|pembersih|supplier bahan|supplier kain|bahan baku|parfum|pewangi|detergen|deterjen|sabun|softener|pelembut|pemutih|stiker|sticker|cover|case|casing|strap|cetakan|kursus|kelas|pelatihan|"
    r"jasa|sablon|kemasan|packaging|box|dus|etalase|display|cuci|sewa|servis|service|pouch|plastik|toples|lampu|kipas|hanger|tempat|dispenser|grinder|filter|teko|kaca|visor|meja|penjepit|capitan|permen|softlens|sadel|pedal|rantai|aksesoris|aksesori|sparepart|pengering|sterilizer|penghangat|warmer)\s+$")
# v1.5.2: iklan JASA/APLIKASI yg memakai niche sbg contoh ("Kopinya sama enak… Upload 1 foto produk ke Auto Feeds")
JASA_KUAT = re.compile(r"\bcoaching (olahraga|fitness|gym|diet|1 ?on ?1|privat|pribadi)\b|\bpersonal trainer\b|\bprogram \d+ (hari|minggu|bulan) (diet|turun berat|fitness|olahraga)\b|"   # v1.5.3 Powerclub "Program 10 Hari Diet … Coaching Olahraga 1 On 1"
                       r"\bmudah ditemukan di (google|maps|gmaps)\b|\bgoogle (bisnis|business|business profile|maps) (anda|kamu|toko)\b|\bjasa (seo|iklan|ads|digital marketing|kelola sosmed)\b|"   # v1.5.3 ToffeeDev/localboost "Buat pet shop Anda mudah ditemukan di Google"
                       r"\bcorporate gifts?\b|\b(souvenir|merchandise|merch) custom\b|\bcustom (souvenir|merchandise|merch)\b|\b(hampers|giftset) corporate\b|\bcorporate (hampers|giftset)\b|"   # v1.5.3 Dapu Gift (giftset isi powerbank)
                       r"\blowongan (kerja|pastry|chef|staff|karyawan|admin|barista|kasir|sales|di)\b|\bloker\b|\blamar (online|sekarang|di)\b|\bwe'?re hiring\b|\bdibutuhkan segera\b|"   # v1.5.3 lowongan kerja (Pitt Dates kurma)
                       r"\b(layanan|jasa) (kurir|kirim|pengiriman|ekspedisi)\b|\b(paxel|lalamove|deliveree|anteraja|sicepat|ninja xpress|lion parcel|jne express|j&t express)\b|\bpinjam(an)? (hingga|sampai|dana|online|tunai)\b|\bdana cair\b|\b(?:dana|pinjaman|uang|limit|pengajuan|kredit)\s+(?:\w+\s+){0,2}cair cepat\b|\bcair cepat (?:tanpa|cuma|hanya|dalam|\d)|\bpinjol\b|\bdibeli (freezer|kulkas|ac|tv|barang|perabot|elektronik|mebel|furniture|hp|laptop|motor|mobil)\b|\bkami (beli|terima) barang bekas\b|\bmaklon|\bmakloon|\bpabrik ?maklon\b|\bjasa (rias|make ?up|mua|maklon)\b|\bmake ?up ?artist\b|\bmua ?(bandung|jakarta|surabaya|jogja|yogya|medan|bali|semarang|malang|bekasi|depok|bogor|tangerang|wisuda|wedding|murah|panggilan)\b|"
                       r"\bmau jual (?:kamera|hp|laptop|iphone|motor|mobil|barang)\b[^.!?\n]{0,12}\?|\bterima (?:jual|beli|tukar tambah) (?:kamera|hp|laptop|iphone)\b|"   # v1.5.3 TogetherKamera "MAU JUAL KAMERA BISA ??"
                       r"\bjasa cuci\b|\blaundry (?:alat )?(?:camping|outdoor|tenda)\b|\bcuci tenda\b|\bpusat penyewaan\b|\bpenyewaan (perlengkapan|alat)\b|\blaundry ?(stroller|carseat|car seat|sofa|kasur|boneka|bayi)|\b(sewa|rental) ?(stroller|carseat|car seat|perlengkapan bayi|baby ?gear)\b|\brent your (baby )?gear\b|"   # v1.5.3 Azkia Baby Rental, LaundryStrollerID, "JASA CUCI BED SOFA KARPET STROLLER"
                       r"\bmake ?up ?(wisuda|wedding|pengantin|lamaran|panggilan)\b|\brias pengantin\b|\bterima (job|panggilan) (make ?up|rias)\b")
JASA_APLIKASI = re.compile(r"\bfeed (ig|instagram)\b|\bdesain(nya)? (feed|konten|logo|kemasan)\b|\bjasa (desain|admin|iklan|ads|foto|video|konten|pembuatan)|"
                           r"\btemplate (feed|konten|canva)|\baplikasi kasir\b|\bsoftware\b|\bwebinar\b|\be-?book\b|\bkelas (online|bisnis|jualan)|"
                           r"\bjasa (cuci|laundry)\b|\bpanggil jasa\b|\bpendaftaran (?:untuk )?(?:haji|umroh|umrah)\b|\bhaji (?:plus|furoda|khusus)\b|\b(?:travel|biro) (?:haji|umroh|umrah)\b|\bkeberangkatan (?:haji|umroh|umrah)\b|#haji(?:plus|furoda|khusus)\b|\binfo (?:detail )?haji\b|\bpijatan (?:dalam|di)\b|reflexology|refleksologi|\bjasa (bersih|cleaning|vacuu?m|vacu?um|deep ?clean)|\bbersihin rumah\b|\bdeep ?clean(ing)? (kasur|sofa|gorden|rumah)\b|\bpijat panggilan\b|\bmassage panggilan\b|\bhome service (treatment|massage|spa|facial)\b|\b(kami )?menyewakan\b|\b(sewa|rental) (motor|mobil|mainan)\b|\blaundry (kiloan|sepatu|alat|helm|tas|boneka|karpet|express|camping)\b|\bcuci (sofa|kasur|springbed|karpet|sepatu|helm|tas|boneka|stroller)\b|\bsewa (alat|alkes|perlengkapan)\b|\b\d+ ?kt \d+ ?km\b|\bshm\b|\bkavling\b|\bperumahan\b|\bdijual rumah\b|\bkami (senang |siap )?membeli\b|\bjual (barang|emas|perhiasan)( bermerek| mewah| branded)?( &| dan)?( emas| barang)? (anda|kamu)\b|\bgadai\b|\bbuyback\b|\bterima jual\b|\bupload (1 )?foto produk|\bfoto produk (kamu|anda|lo)\b|\bomzet (kamu|anda) naik|\bbelajar (jualan|bisnis|ads|iklan)")
KATA_KONTEKS = {
    "teh": re.compile(r"\b(teh|tea) (hijau|celup|melati|tubruk|seduh|daun|manis|tarik|poci|susu|herbal|bunga|rempah|sereh|kelor|botol|kotak|kemasan|tawar|leci|lemon|thai|oolong|hitam|putih|pelangsing|diet|detoks?|jahe|rosella|chamomile|peppermint|bubuk|tubruk)\b|"
                      r"\b(es|minum|seduh|secangkir|cangkir|segelas|daun|kebun|celup) teh\b|\btea\b|matcha|chai|\bteh[a-z]{0,3} (ini|nya)\b|\b\d+ ?(gr|gram|sachet|kantong|pcs)\b"),
    "hp": re.compile(r"\b(samsung|iphone|xiaomi|redmi|oppo|vivo|infinix|realme|tecno|poco|itel|nokia|pixel|huawei|honor|motorola|nubia|galaxy|android|ios)\b|"
                     r"\b\d+ ?(gb|tb)\b|\b\d+/\d+\b|\bram\b|\bmah\b|\b\d+ ?mp\b|snapdragon|helio|dimensity|unisoc|"
                     r"\b(jual|beli|tukar tambah|second|seken|bekas|cicilan|kredit|garansi resmi|unit)\b.{0,15}\b(hp|handphone|smartphone)\b|"
                     r"\b(hp|handphone|smartphone) (baru|bekas|second|seken|murah|gaming|android|5g|4g|terbaru|flagship|kentang|mid ?range|lipat)\b"),
}
SINONIM_AMBIGU = {
    "foundation": re.compile(r"makeup|make up|bedak|cushion|lipstik|shade|coverage|wajah|kulit|matte|flawless|concealer"),
    "android": re.compile(r"\bhp\b|handphone|smartphone|ponsel|\bram\b|\d ?gb\b|kamera|baterai|mah\b|snapdragon|helio"),
    "bag": re.compile(r"\btas\b|ransel|selempang|sling|tote|backpack|dompet|handbag|pouch|\bbags?\b.{0,20}(kulit|leather|kanvas|canvas)"),
    "hem": re.compile(r"kemeja|\bbaju\b|lengan|kerah|shirt|katun|flanel|batik|\bpria\b|kantor|slim ?fit"),
    "pants": re.compile(r"\bcelana\b|jeans|chino|cargo|kulot|jogger|denim|pinggang|formal|trousers|legging|shorts|\b(running|track|hiking|yoga|cargo) pants\b|jogging|\bgym\b|olahraga|jahitan"),
    "watch": re.compile(r"\bjam\b|strap|tali|dial|analog|digital|chronograph|smartwatch|arloji|quartz|automatic|wrist|\batm\b|water ?resist"),
    "bags": re.compile(r"\btas\b|ransel|selempang|sling|tote|backpack|dompet|handbag"),
}
BONUS = re.compile(r"\b(gratis|free|bonus|fasilitas|include|includes|termasuk|hadiah|freebie|gift)\b(?:(?! - )[^.!?\n|]){0,14}$|\bfasilitas\s*:[^.!?]{0,20}$|\b(gratis|free|bonus|hadiah|fasilitas)\s*:\s*\n[^.!?|\n]{0,14}$")   # v1.5.4 "bonus:\n2 bantal"
VARIETAS = re.compile(r"\b(pisang|mangga|jambu|melon|nanas|nangka|semangka|pepaya|ubi|tape|ayam|bebek|bolu|kue|roti|martabak|jeruk|klengkeng|lengkeng|apel|sirsak|salak|bakpia|dodol|lemon)\s+$")
MASALAH = re.compile(r"\b(bau|apek|lembap|lembab|jamur|berjamur|kotor|kusam|rusak|berdebu|debu|bernoda|noda|kuning|menguning|lecet|licin|bocor|gatal|berantakan|numpuk|sempit|pengap)\b")
# v1.5.3: penjual pasar MALAYSIA (iklan tetap tayang di ID) — "jom madu asli", "RM59", "pos laju", "Gombak"
MALAYSIA = re.compile(r"\bberkualiti\b|\bberasaskan\b|\bpercuma\b|\bjom\b|\brm ?\d|\bringgit\b|\bpos ?laju\b|\bsemenanjung\b|\bpenghantaran\b|\bselangor\b|\bkuala lumpur\b|"
                      r"\bnegeri sembilan\b|\bpahang\b|\bperlis\b|\bmelaka\b|\blangsir\b|\bpelbagai\b|\bberpatutan\b|"   # v1.5.3 MK Curtain Nilai 3
                      r"\bkatil\b|\bkilang\b|\bjohor\b|\bsabah\b|\bsarawak\b|\bgombak\b|\bshah alam\b|\bpenang\b|\bkedah\b|\bkelantan\b|\bterengganu\b|\bputrajaya\b|\bwhatsapp kami sekarang\b")
ID_KUAT = re.compile(r"\brp\.? ?\d|\d+ ?(rb|ribu|jt|juta)\b|\.co\.id|shopee\.co|tokopedia|\bcod\b|gratis ongkir|\bwib\b|jabodetabek|jakarta|bandung|surabaya|indonesia|"   # v1.5.3 provinsi/kota (lapstore "Sukarame Sabah Balau … Bandar Lampung" ≠ Sabah MY)
                     r"\b(lampung|jawa (timur|tengah|barat)|jatim|jateng|jabar|yogyakarta|jogja|jogjakarta|semarang|makassar|palembang|denpasar|bekasi|tangerang|depok|bogor|sidoarjo|gresik|cirebon|tasikmalaya|kediri|pekanbaru|balikpapan|samarinda|pontianak|banjarmasin|manado|kalimantan|sulawesi|sumatera|sumatra|papua|lombok|riau)\b")
# v1.5.2: urutan Inggris (keterangan di depan): "coffee table", "shoe rack", "bag charm", "hijab pin"
EKOR_LAIN = re.compile(r"^\s+(application|applicator|spreader|sprayer|kaki|roda(?!\s*(?:dua|tiga|empat|2|3|4)\b)|mainan|mainannya|mobil|mobilmu|motor|motormu|otomotif|table|tables|maker|machine|grinder|mug|cup|rack|cabinet|charm|pin|pins|organizer|cleaner|glue|holder|hanger|tray|scrub|soap|cushion|cushions|pad|pads|liner|hook|hooks|fan|canopy|footmuff|cover|covers|bag|bags)\b")   # v1.5.3 "stroller cushion/pad/hook"
# v1.5.2: kategori halaman Meta (snapshot.page_categories) yang jelas BUKAN penjual barang niche tsb
RESTO_TEGAS = re.compile(r"burger|restaurant|restoran|fast food|pizza|\bcafe\b|coffee shop|diner|seafood|steakhouse|sushi|ramen|bistro|eatery|rumah makan|warung makan|food court|grill|\bbar\b")
KATEGORI_LUAR = [
    ("nirlaba", re.compile(r"non ?profit|nonprofit|charity|charitable|nirlaba"), set()),   # v1.5.3 LAZNAS wakaf
    ("resto", re.compile(r"burger|restaurant|restoran|diner|seafood|steakhouse|sushi|ramen|bistro|eatery|rumah makan|food court|grill|fast food|pizza|cafe|coffee shop|bakery|food (&|and) beverage|chicken|noodle|caterer|warung makan"), {"makanan"}),
    ("otomotif", re.compile(r"\bauto|automotive|otomotif|motorcycle|sepeda motor|car dealership|vehicle|tire|gas station|bengkel|detailing|car wash|cuci mobil|salon mobil"), {"otomotif"}),
    ("keuangan", re.compile(r"\bbank|financ|keuangan|insurance|asuransi|loan|pinjaman|credit|investment|investasi|crypto|payment|e-wallet"), set()),
    ("telko", re.compile(r"telecommunication|telekomunikasi|internet service|mobile network"), {"elektronik"}),
    ("hiburan", re.compile(r"movie|film|\btv\b|television|televisi|streaming|media/news|news & media|entertainment|hiburan|video game"), set()),
    ("travel", re.compile(r"airline|maskapai|travel|hotel|lodging|tourism|\btour\b|pariwisata"), set()),
    ("elektronik", re.compile(r"phone/tablet|mobile phone|electronics|elektronik|computer|komputer|gadget|telepon seluler"), {"elektronik", "alat"}),
    ("publik", re.compile(r"public service|layanan publik|government organi[sz]ation|instansi pemerintah"), set()),   # v1.5.3 Gugus Tugas Covid-19
    ("pijat", re.compile(r"\bmassage|\bpijat|reflexology|refleksi"), set()),   # v1.5.3 Halo Jasa "face mask, body mask" = paket pijat panggilan
    ("cetak", re.compile(r"^printing|printing service|percetakan|digital printing"), {"cetak"}),   # v1.5.3 Sublimindo makloon printing kerudung
    ("tekstil", re.compile(r"fabric|textile|tekstil"), {"tekstil"}),   # v1.5.3 Putra Aneka Sandang (Fabrics) #hijab
    ("rumah tangga", re.compile(r"household supplies|cleaning products?|produk kebersihan|perlengkapan rumah tangga"), {"rumah", "alat"}),   # v1.5.3 Klinbos penghilang noda "Mukenah berjamur"
    ("jasa rumah", re.compile(r"perawatan di rumah|cleaning|kebersihan|laundry|dry clean|pest control|pembasmi hama|in-home service|layanan rumah"), set()),
    ("aksesori", re.compile(r"^\s*(accessories|accessory|aksesoris|aksesori)\s*$"), {"fashion", "kecantikan", "anak", "elektronik", "otomotif"}),   # v1.5.3 Jete (aksesori HP) jual speaker
    ("fashion", re.compile(r"clothing|apparel|pakaian|fashion|bags|luggage|\bshoe|sepatu|jewelry|perhiasan|boutique|butik|accessor|aksesori"), {"fashion", "kecantikan", "anak"}),
]
NICHE_KELUARGA = [
    ("fashion", re.compile(r"\b(fashion|busana|pakaian|outfit|ootd|tas|totebag|ransel|hijab|jilbab|kerudung|pashmina|bergo|khimar|gamis|sepatu|sneakers?|sandal|jaket|hoodie|kaos|baju|kemeja|celana|dress|rok|mukena|sarung|dompet|topi|kacamata|jam tangan|casing|softcase|hardcase|perhiasan|cincin|kalung)\b")),
    ("makanan", re.compile(r"\b(kuliner|oleh oleh|oleh-oleh|frozen food|catering|kopi|madu|teh|snack|camilan|keripik|kue|cokelat|coklat|sambal|frozen|susu|minuman|makanan|roti|kurma|abon|rendang|mie|beras|porang|gula|tepung)\b")),
    ("kecantikan", re.compile(r"\b(kecantikan|cantik|beauty|kosmetik|kosmetika|facial|salon|klinik kecantikan|perawatan wajah|perawatan kulit|jerawat|glowing|nail art|bulu mata|eyelash|sulam alis|skincare|bodycare|serum|sabun|sampo|shampoo|lotion|parfum|masker|lipstik|bedak|toner|sunscreen|kosmetik|makeup)\b")),
    ("anak", re.compile(r"\b(mainan|bayi|anak|stroller|popok|diaper)\b")),
    ("cetak", re.compile(r"\b(printer|sablon|cetak|percetakan|printing|stiker|sticker|banner|spanduk|tinta)\b")),
    ("tekstil", re.compile(r"\b(kain|bahan|tekstil|batik|katun|linen|rayon|wol|benang)\b")),
    ("rumah", re.compile(r"\b(rak|lemari|kasur|bantal|sprei|gorden|meja|kursi|sofa|dapur|panci|wajan)\b")),
    ("otomotif", re.compile(r"\b(helm|oli|ban|motor|mobil|sparepart|aki|knalpot|velg|spion)\b")),
    ("alat", re.compile(r"\b(blender|rice cooker|magic ?com|mixer|juicer|chopper|air fryer|oven|microwave|kompor|kipas angin|setrika|dispenser|kulkas|mesin cuci|vacuum|humidifier|purifier|teko listrik|water heater|lampu)\b")),   # v1.5.3 alat rumah tangga elektrik (izin: Electronics & Household, bukan telko)
    ("elektronik", re.compile(r"\b(hp|handphone|smartphone|laptop|charger|powerbank|earphone|headset|speaker|kamera|tv|printer|proyektor|projector|monitor|scanner)\b")),
]


def keluarga_niche(kata):
    """Himpunan keluarga niche (bisa >1: "salon mobil" = kecantikan + otomotif); kosong = tak dikenal."""
    return {nama for nama, rx in NICHE_KELUARGA if rx.search(kata)}
LOKAL = re.compile(r"\brp\.? ?\d|\d+ ?(rb|ribu|jt|juta)\b|\b(?:harga|cuma|hanya|mulai|diskon|potongan|cashback|voucher|ongkir)\s+\d+ ?k\b|\.co\.id|\.id\b|shopee|tokopedia|lazada|tiktok ?shop|blibli|"
                   r"wa\.me/62|phone=62|\+62 ?8|\b08\d{2}[ -]?\d{3,4}|\bcod\b|ongkir|gratis ongkir|indonesia|jakarta|bandung|surabaya|bali|"
                   r"jogja|yogyakarta|medan|semarang|makassar|malang|bekasi|tangerang|depok|bogor|official store|checkout|"
                   # v1.5.3 kosakata Indonesia sehari-hari (teks tanpa kata fungsi baku: "Bahannya bener nyaman, detail cantik")
                   r"\b(?:banget|bgt|bener|nyaman|cantik|bahan(?:nya|nnya)?|warna(?:nya)?|harga(?:nya)?|murah|kakak|bunda|yuk|gratis|diskon|terlaris|langsung|sekarang|juga|aja|udah|sudah|bisa|untuk|dengan|dan|yang)\b", re.I)
AFILIASI = re.compile(r"beli(nya)? ?di ?sini|cek harga|spill|link (produk|shopee|beli)|keranjang kuning|s\.shopee\.co\.id/", re.I)
ARTIKEL = re.compile(r"\b(read more about|read tips|explore (helpful )?tips|learn (more )?about|"
                     r"discover (helpful )?tips|explore how|tips for|curious about ways)\b", re.I)

STOP_ID = set("yang dan di ke dari ini itu untuk dengan kamu aku kami bisa aja jadi udah sudah lagi buat "
              "banget juga nya kak cek beli harga gratis ongkir promo diskon sekarang hanya mau tidak gak "
              "nggak ada pakai sama atau karena biar langsung yuk dong deh".split())
STOP_EN = set("the and for with you your our this that are is to of in on it get now new shop buy best "
              "free all from more off sale today".split())
STOP_ASING = set("le la les des et pour vous est une que du pas qui sur avec ir un kas ar par "
                 "ang ng mga siya ko sa niya hindi el los las por para con una del que es o não com "
                 "và của là có không được cho người der die das und nicht mit sie ist "
                 "että myös mutta joka ovat sekä tämä kuin ole oli ei vuoden aikaa kui see mis ka".split())   # v1.5.3 Finlandia/Estonia (China Meets Finland)


def _norm(s):
    s = unicodedata.normalize("NFKC", str(s or ""))
    return s.lower()


def pola_kata(w):
    """kata utuh + imbuhan umum; frasa: sela spasi/strip, boleh ≤2 kata sisipan."""
    bag = [re.escape(x) for x in re.split(r"[\s\-]+", w.strip()) if x]
    if not bag:
        return None
    # v1.5.3 uji emas: sisipan bukan kata sambung/tanda baca ("sepatu lari dan bola padel", "serum ingredients, rambut" ≠ frasa)
    inti = r"(?:[\s\-]+(?!(?:dan|atau|serta|hingga|sampai|plus|and|or|with|ke|di)(?![^\W_]))[^\s\-,.;:/&|!?()+]+){0,2}?[\s\-]+".join(bag) if len(bag) > 1 else bag[0]
    return re.compile(r"(?<![^\W_])" + inti + r"(?:nya|ku|mu|lah|kah|-" + bag[-1] + r")?(?![^\W_])")


# v1.5.3: padanan SEMPIT — didahulukan dari SINONIM (anak grup tidak mewarisi saudaranya:
# "sunscreen" ≠ serum/toner, "lipstik" ≠ bedak, "sneakers" ≠ pantofel; "kaos kaki" ≠ "t-shirt kaki")
SINONIM_KHUSUS = {
    "sunscreen": ["sun screen", "tabir surya", "sunblock", "sun block", "sunscreen gel", "sun cream"],
    "lipstik": ["lipstick", "lip cream", "lip matte", "lip tint", "lip stick", "lipcream", "liptint", "lipmatte", "lipgloss", "lip gloss", "lip glaze", "lip glasir", "lip vinyl", "lip velvet", "lip stain", "lipstain", "lipen"],   # v1.5.3 Cléviant "liptint/lipmatte/lipgloss"
    "sneakers": ["sneaker", "sepatu sneakers", "sepatu sneaker"],
    "serum wajah": ["serum", "face serum", "serum muka", "facial serum"],
    "kaos kaki": ["kaus kaki", "socks", "sock"],
    "sepatu bola": ["sepatu sepak bola", "sepatu futsal", "football boots", "soccer shoes", "sepatu soccer", "sepatu bola anak"],   # v1.5.3 uji emas: Specs (katalog futsal)
    # v1.5.3 uji emas: varian kipas = kipas angin ("Kipas Mini Turbo", "KIPAS GANTUNG", "Kipas Portable")
    "kipas angin": ["kipas mini", "kipas portable", "kipas portabel", "kipas dinding", "kipas berdiri", "kipas meja", "kipas gantung",
                    "kipas tornado", "kipas turbo", "kipas tangan elektrik", "kipas charger", "kipas usb", "stand fan", "wall fan", "desk fan", "ceiling fan", "box fan"],
    "obat herbal": ["jamu", "kapsul herbal", "herbal alami", "100% herbal", "ramuan herbal", "pengobatan herbal", "herbal cina", "herbal china",
                    "obat tradisional", "suplemen herbal", "minuman herbal", "obat alami", "herbal original", "produk herbal"],   # v1.5.3
    # v1.5.4 set ke-3: istilah Inggris & sebutan lazim penjual
    "botol susu bayi": ["botol susu", "botol bayi", "baby bottle", "feeding bottle", "botol dot", "botol asi", "botol ppsu", "botol anti kolik"],
    "kaos polo": ["polo shirt", "poloshirt", "kaos kerah", "kaus kerah", "kaus polo", "baju polo", "polo tee", "kaos berkerah", "kemeja polo", "t-shirt polo", "tshirt polo"],
    "obat nyamuk": ["lotion anti nyamuk", "lotion nyamuk", "losion nyamuk", "losion anti nyamuk", "obat anti nyamuk", "semprotan nyamuk", "semprot nyamuk",
                    "anti nyamuk bakar", "obat nyamuk bakar", "obat nyamuk elektrik", "pengusir nyamuk", "dupa nyamuk", "bakar nyamuk", "mosquito repellent", "insect repellent", "soffell", "autan", "baygon"],
    "minyak goreng": ["minyak sayur", "cooking oil", "minyak kelapa sawit", "minyak sawit", "minyakita", "minyak kita", "bimoli", "sunco", "filma", "sania", "sovia", "fortune minyak"],
    "dashcam": ["dash cam", "dash camera", "dvr mobil", "car dvr", "kamera dashboard", "kamera dashcam", "kamera perekam mobil"],
    "tas laptop": ["laptop bag", "laptop backpack", "backpack laptop", "ransel laptop", "laptop sleeve", "sleeve laptop", "tas notebook", "tas macbook", "tas kerja laptop"],
    "kopi diet": ["kopi pelangsing", "kopi slimming", "slimming coffee", "diet coffee", "kopi langsing"],
    "susu kambing": ["susu etawa", "goat milk", "etawa", "susu kambing etawa"],
    "vitamin anak": ["multivitamin anak", "suplemen anak", "vitamin si kecil", "kids vitamin"],
    "mukena": ["telekung", "mukenah", "mukena travel"],
    "sprei": ["seprai", "seprei", "sprai", "bed cover", "bedcover", "sprei set"],
    "jaket": ["jacket", "hoodie", "windbreaker", "parka", "bomber", "jaket pria", "jaket wanita"],
    "kacamata": ["eyewear", "sunglasses", "kacamata hitam", "frame kacamata", "kacamata minus", "kacamata anti radiasi"],
    "dompet": ["wallet", "card holder", "dompet wanita", "dompet pria"],
    "tumbler": ["termos", "thermos", "tumbler stainless", "vacuum flask", "botol minum"],
    "botol minum": ["botol air", "water bottle", "drinking bottle", "botol minum anak", "tumbler"],
    "rice cooker": ["magic com", "magicom", "magic jar", "penanak nasi", "ricecooker", "mesin penanak nasi", "multicooker"],
    "blender": ["blender portable", "juicer blender", "mini blender"],
    "panci": ["cookware", "panci set", "panci presto", "presto"],
    "kurma": ["kurma ajwa", "kurma sukari", "kurma medjool", "kurma tunisia"],
    "cokelat": ["coklat", "chocolate", "choco", "dark chocolate"],
    "power bank": ["powerbank", "power-bank"],
    "casing hp": ["case hp", "softcase", "soft case", "hardcase", "hard case", "phone case", "case iphone", "custom case", "casing", "case"],   # casing/case polos: wajib konteks HP (KONTEKS_SINONIM)
    "lampu": ["lampu led", "bohlam", "lampu tidur", "lampu hias", "lamp"],
    "sepeda": ["bicycle", "sepeda listrik", "sepeda lipat", "seli", "sepeda anak"],
    "oli": ["pelumas", "oli mesin", "engine oil", "motor oil", "oli motor"],   # v1.5.3 "lubricant" dibuang (Durex Lubricant)
    "pupuk": ["fertilizer", "pupuk organik", "pupuk cair", "npk"],
    "makanan kucing": ["cat food", "pakan kucing", "makanan kitten", "dry food kucing", "wet food kucing", "whiskas", "snack kucing", "camilan kucing", "cat treat", "cat treats", "cat strip", "creamy cat", "kitten food", "makanan anabul", "pakan anabul"],   # v1.5.3 Chiyo Creamy Cat Strip
    "pomade": ["hair wax", "clay wax", "hair clay", "minyak rambut"],
    "minyak kayu putih": ["kayu putih", "eucalyptus oil", "cajuput"],
    "jam dinding": ["wall clock"],
    "baju anak": ["pakaian anak", "setelan anak", "kids wear", "baju anak laki", "baju anak perempuan"],
    "peninggi badan": ["peninggi", "tinggi badan", "penambah tinggi", "suplemen tinggi badan"],
    "masker wajah": ["face mask", "sheet mask", "masker muka", "clay mask", "masker organik"],
    "gorden": ["gordyn", "hordeng", "gorden jendela", "curtain", "tirai", "vitrase"],
    "stroller": ["kereta dorong", "kereta bayi", "baby stroller"],
    "bantal": ["pillow", "bantal tidur", "bantal leher"],
    "tenda": ["tent", "tenda camping", "tenda dome"],
    "alat pancing": ["joran", "reel pancing", "umpan pancing", "pancing", "fishing rod"],
    "kamera": ["camera", "action cam", "mirrorless", "dslr"],
    "speaker": ["speaker bluetooth", "soundbar", "sound system"],
    "printer": ["printer thermal", "printer inkjet"],
}


# v1.5.4 niche "wadah isi" (tas laptop, tas kamera, rak sepatu, kotak perhiasan): sebutan wadah tambahan di luar SINONIM
WADAH_NICHE = {"tas": ["slingbag", "sling", "tote", "sienna bag", "messenger bag", "carry bag", "daypack", "briefcase", "bag"],
               "ransel": ["backpack", "daypack"], "rak": ["shelf", "rack"], "kotak": ["box", "organizer"], "sarung": ["sleeve", "case", "pouch"],
               "pouch": ["case"], "case": ["casing", "pouch"], "dompet": ["wallet", "pouch"], "tempat": ["wadah", "organizer", "holder"]}
PENGUBAH_NICHE = {
    "diet": ["pelangsing", "langsing", "slim", "slimming", "kurus", "berat badan", "turun bb", "fat burner", "rendah kalori"],
    "anak": ["si kecil", "kids", "balita", "anak-anak"],
    "wajah": ["muka", "face", "facial"],
    "bayi": ["baby", "newborn"],
    "hamil": ["bumil", "ibu hamil", "maternity"],
    "pria": ["cowok", "laki-laki", "men"],
    "wanita": ["cewek", "perempuan", "women"],
}


# v1.5.3: pola sinonim per-niche dgn pengecualian ("serum" utk serum wajah ≠ body/hair serum)
_SERUM_BUKAN = r"(?<!body )(?<!hair )(?<!lip )(?<!eye )(?<!nail )(?<!tubuh )serum(?:nya)?(?!\s+(?:rambut|tubuh|badan|ketiak|bulu|kuku|bibir|body|hair|lip|lash|brow))(?![^\W_])"
POLA_KHUSUS = {
    ("serum wajah", "serum"): re.compile(r"(?<![^\W_])" + _SERUM_BUKAN),
    # v1.5.3 "case" polos: bukan "worst case / in case / use case / case study"
    ("casing hp", "case"): re.compile(r"(?<!worst )(?<!best )(?<!\bin )(?<!\buse )(?<!\btest )\bcases?(?:nya|-nya)?\b(?! stud)"),
    # v1.5.3 "150K Optik Pancing" / "Jl. Pancing" = nama tempat (Medan)
    ("alat pancing", "pancing"): re.compile(r"(?<!optik )(?<!jl )(?<!jl\. )(?<!jalan )(?<!simpang )\bpancing(?:nya)?\b(?! (?:medan|raya|ujung|indah)\b)"),
    ("casing hp", "casing"): re.compile(r"\bcasing(?:nya|-nya)?\b(?! (?:pc|komputer|cpu|motor|mobil|lampu|pompa|kabel|power|psu)\b)"),
}
# v1.5.3 sinonim yg hanya sah bila iklan juga menyebut konteksnya ("case ungu glitter … HP kamu"; bukan "Worst case? … snore")
KONTEKS_SINONIM = {
    # v1.5.3 Dove "hairfall control / Bi-Phase Serum + Oil / Rambut 10x lebih kuat" = serum rambut
    "serum wajah": (re.compile(r"(?s)^(?!.*(rambut|\bhair|ketiak|bulu mata|\blash|kuku|\bnail)).|wajah|muka|kulit|\bface\b|facial|\bskin|glow|cerah|jerawat|\bacne|flek|kusam|pori|aging|kerut|brighten|whiten|niacinamide|retinol|hyaluron|vitamin c|pdrn|centella|dark spot|noda|barrier|kenyal|bekas|serum (emas|wajah|muka)|beauty|kecantikan|cosmetic|kosmetik|makeup"),
                    {"serum"}),
    "casing hp": (re.compile(r"\b(hp|handphone|iphone|smartphone|phone|ponsel|samsung|galaxy|oppo|vivo|xiaomi|redmi|realme|infinix|poco|tecno|itel|huawei|magsafe)\b"),
                  {"casing", "case", "hardcase", "hard case", "soft case"}),   # hardcase = juga koper (Swiss Military suitcase)
}


IDIOM_NICHE = {
    "dompet": re.compile(r"(?:bikin|buat|membuat|bakal|biar|tanpa|nggak|gak|ga|tidak|ngga|anti)?\s*(?:bikin\s+)?dompet(?:mu|ku|nya)?\s+"
                         r"(?:tipis|jebol|kebakaran|panas|terkuras|menipis|bolong|kering|aman|tenang|tetap tenang|tetap aman|tebal|tetap tebal|bahagia|senyum|menjerit|nangis|meringis|sehat)\b"
                         r"|(?:lindungi|selamatkan|penyelamat|isi|menguras|kuras|ramah|bersahabat dengan|hemat|jaga)\s+(?:di\s+)?dompet(?:mu|ku|nya)?\b"
                         r"|dompet\s+(?:digital|elektronik|serba bisa)\b|#?dompetserbabisa|e-?wallet"),
    # v1.5.3 blender kecantikan (spons/beauty blender) & software 3D Blender ≠ blender dapur
    "blender": re.compile(r"(?:one\s+)?take\s+blender|beauty\s*blender|make\s*up\s+blender|blender\s+(?:sponge|spons|puff|brush|makeup|make up)\b"
                          r"|blender\s+3d|3d\s+blender|(?:belajar|kursus|kelas|tutorial|software|aplikasi)\s+blender\b|blender\s+(?:course|tutorial|animation|animasi|modeling|modelling)\b"
                          r"|blender\.(?=[a-z]+\.(?:school|academy|courses?|edu)\b)"),
}
# v1.5.3 cokelat = WARNA ("Softlens Cokelat Terang", "warna coklat", "cokelat tua") ≠ produk cokelat
_COKELAT_WARNA = re.compile(r"(?:ber)?warna\s+(?:cokelat|coklat)\b|\b(?:cokelat|coklat)\s+(?:terang|tua|muda|gelap|kemerahan|keemasan|kehitaman|keabuan|natural|almond|hazel|mocca|mocha|kopi|bata|susu muda)\b")
IDIOM_NICHE["cokelat"] = IDIOM_NICHE["coklat"] = _COKELAT_WARNA
# v1.5.3 lampu lalu lintas / "lampu hijau" (izin) / CCTV berbentuk bohlam ≠ lampu; sepeda motor / bicycle kick ≠ sepeda
IDIOM_NICHE["lampu"] = re.compile(r"lampu lalu ?lintas|\bdi lampu merah|lampu merah (?:perempatan|jalan)|(?:dapat|kasih|beri|memberi|mendapat) lampu hijau|lampu hijau dari"
                                  r"|cctv(?:\s+[a-z]+){0,2}?\s+(?:bohlam\s+)?lampu(?:\s+bohlam)?|lampu\s+(?:sekaligus\s+|plus\s+|\+\s*)?cctv|bohlam\s+lampu\s+cctv")
IDIOM_NICHE["oli"] = re.compile(r"oli giroud|olivier giroud")
# v1.5.3 uji emas: "Matras Yoga Foot Massager" (karpet pijat) ≠ kasur; "Gemini Notebook" (fitur HP Samsung) ≠ laptop
IDIOM_NICHE["kasur"] = IDIOM_NICHE["matras"] = re.compile(r"matras (?:yoga|olahraga|senam|gym|pilates|pijat|refleksi|camping|kemah|tummy time)|yoga ?mat|(?:alas|tikar) yoga")
IDIOM_NICHE["kipas angin"] = re.compile(r"kipas tangan(?! (?:elektrik|listrik|usb|charger|portable|portabel|mini|cas))|kipas (?:batik|souvenir|suvenir|kertas|bambu|kayu|kain|hias|tradisional|pernikahan)\b")   # kipas suvenir ≠ elektronik
IDIOM_NICHE["laptop"] = IDIOM_NICHE["notebook"] = re.compile(r"gemini (?:in )?notebook|notebook ?lm|notebook (?:a4|a5|a6|b5|kertas|catatan|spiral)|buku (?:catatan|tulis) notebook")
IDIOM_NICHE["pupuk"] = re.compile(r"\bdi ?pupuk sejak|pupuk sejak dini|harus di ?pupuk\b|\bdipupuk\b")   # "cinta tanah air harus di pupuk sejak dini"
# v1.5.3 "tabir surya" kaca mobil (Fawaz "Mobil Sering Kepanasan Saat Parkir? Pakai Tabir Surya Ini!")
IDIOM_NICHE["sunscreen"] = IDIOM_NICHE["tabir surya"] = re.compile(r"pelindung panas kaca|(?:mobil|kendaraan|kaca)[^\n]{0,60}?tabir surya|tabir surya (?:mobil|kaca|kendaraan)")
IDIOM_NICHE["kamera"] = IDIOM_NICHE["camera"] = re.compile(r"(?:terekam|tertangkap|terciduk|direkam) (?:kamera|camera|cctv)|(?:di )?depan kamera|kamera tersembunyi|\bon camera\b")   # v1.5.3 clickbait "Terekam Kamera"
IDIOM_NICHE["tenda"] = re.compile(r"tenda (?:arafah|mina|jamaah|haji)")   # v1.5.3 jejak imani "Live Tur di Tenda Arafah"
IDIOM_NICHE["sepeda"] = re.compile(r"sepeda motor|bicycle kick|tendangan (?:sepeda|salto)|sepeda gantung")


def keluarga_sinonim(kata):
    """Kumpulan sinonim ketat untuk kata kunci (kata itu sendiri selalu di depan)."""
    kata = _norm(kata).strip()
    if kata in SINONIM_KHUSUS:
        return [kata] + [w for w in SINONIM_KHUSUS[kata] if w != kata]
    out = [kata]
    for g in SINONIM:
        if kata in g or any(kata == w for w in g):
            out += [w for w in g if w != kata]
    # v1.5.3: "obat X" → X sendiri juga bukti (pengiklan menulis "batuk", bukan "obat batuk")
    m_ = re.match(r"(obat|salep|jamu|herbal|ramuan)\s+(.{4,})$", kata)
    if len(out) == 1 and m_ and m_.group(2) not in ("nyamuk", "kecoa", "tikus", "semut", "lalat", "rayap", "kutu", "serangga", "hama", "jamur", "herbal", "alami", "tradisional", "generik", "kuat", "murah", "asli"):
        out.append(m_.group(2))
    # kata kunci multi-kata yg mengandung anggota grup (mis. "tas wanita" → bag)
    if len(out) == 1:
        for g in SINONIM:
            for w in g:
                if " " not in w and len(w) > 3 and re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", kata):
                    out += [kata.replace(w, x) for x in g if x != w and " " not in x][:4]
                    break
    lihat, hasil = set(), []
    for w in out:
        if w not in lihat:
            lihat.add(w)
            hasil.append(w)
    return hasil


def _bahasa(teks):
    teks = re.sub(r"https?://\S+|\S+\.(com|id|co|net|org|me)\S*|@\S+|#\S+", " ", teks)
    kata = re.findall(r"[a-zà-ÿāēīūčšžļņķģ]+", teks)
    if len(kata) < 6:
        return "pendek"
    i = sum(1 for k in kata if k in STOP_ID)
    e = sum(1 for k in kata if k in STOP_EN)
    a = sum(1 for k in kata if k in STOP_ASING)
    cjk = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff\u0e00-\u0e7f]", teks))
    if cjk > 8 and cjk > len(kata):
        return "asing"
    if a >= 3 and a > (i + e):
        return "asing"
    if i + e == 0 and a >= 1 and len(kata) >= 15:
        return "asing"
    if len(re.findall(r"[āēīūčšžļņķģőűñçãõøåæßğışřůěďäöü]", teks)) > 3 and i < 3:
        return "asing"
    return "id" if i >= e else "en"


def _t(snap, k):
    v = snap.get(k) or ""
    if isinstance(v, dict):
        v = v.get("text") or ""
    return str(v)


def _relevansi_v3(kata, snap, o, b=None, bersih=lambda s: s, keluarga=()):
    kata = _norm(kata).strip()
    if not kata:
        return {"nilai": 100, "label": "Relevan", "cocok": []}
    snap = snap or {}
    judul = _norm(bersih(_t(snap, "title")))
    body = _norm(bersih(_t(snap, "body")))
    ldesc = _norm(bersih(_t(snap, "link_description"))) + " " + _norm(_t(snap, "caption"))
    nama_hal = _norm(o.get("page_name") or snap.get("page_name") or "")
    link = re.sub(r"\{\{[^}]*\}\}", " ", _norm(unquote(str(snap.get("link_url") or "")))).replace("-", " ").replace("_", " ").replace("/", " ")   # v1.5.3 {{ad.id}} ≠ ".id"
    kartu = [_norm(str(c.get("title") or "") + " " + str(c.get("body") or "") + " " + str(c.get("link_description") or ""))
             for c in (snap.get("cards") or []) if isinstance(c, dict)]
    kategori = _norm(" ".join(str(x) for x in (snap.get("page_categories") or o.get("categories") or [])))
    if kata in IDIOM_NICHE:   # v1.5.3 idiom ("bikin dompet jebol") bukan sebutan produk → dihapus sebelum dinilai
        _id = IDIOM_NICHE[kata]
        judul, body, ldesc = _id.sub(" ~ ", judul), _id.sub(" ~ ", body), _id.sub(" ~ ", ldesc)
        kartu = [_id.sub(" ~ ", k) for k in kartu]
        link = _id.sub(" ~ ", link)
    awal_body = body[:170]
    kuat = " | ".join([judul, ldesc, " ".join(k[:120] for k in kartu), awal_body])
    _bk = [0, len(judul) + 3]; _bk.append(_bk[1] + len(ldesc) + 3); _bk.append(_bk[2] + len(" ".join(k[:120] for k in kartu)) + 3)   # awal tiap bagian zona kuat (judul Shopee bisa memuat " | ")
    semua = " | ".join([judul, body, ldesc, " ".join(kartu)])

    fam = keluarga_sinonim(kata)
    pol = [(w, POLA_KHUSUS.get((kata, w)) or pola_kata(w)) for w in fam if pola_kata(w)]
    if kata in KONTEKS_SINONIM and not KONTEKS_SINONIM[kata][0].search(" | ".join([semua, nama_hal, link])):
        pol = [p for p in pol if p[0] == kata or p[0] not in KONTEKS_SINONIM[kata][1]]
    pk = pol[0][1]
    skor, cocok = 0, []

    def ada(rx, t):
        return bool(rx.search(t))

    # 1) kata kunci persis
    if ada(pk, kuat):
        skor += 60
        cocok.append(kata)
    else:
        n = len(pk.findall(semua))
        if n >= 2:
            skor += 45
            cocok.append(f"{kata} ×{n}")
        elif n == 1:
            skor += 18
            cocok.append(kata + " (sambil lalu)")
    rapat = kata.replace(" ", "")
    # v1.5.3 halaman khusus produk lain: "Permen Nafas Segar Teh Hijau" (teh = rasa permen)
    hal_lain = bool(re.search(r"\b(permen|sabun|lilin)\b", nama_hal) and not re.search(r"permen|sabun|lilin", kata))
    if (ada(pk, nama_hal) or (len(rapat) >= 4 and rapat in nama_hal.replace(" ", ""))) and not hal_lain:
        skor += 25
        cocok.append(kata + " (nama halaman)")
    if ada(pk, link) or (len(rapat) >= 4 and rapat in link.replace(" ", "")):
        skor += 15
        cocok.append(kata + " (tautan)")
    # tagar: #tasbahu #slingbag #rakSepatu → kuat
    tagar = re.findall(r"#([a-z0-9_]{3,40})", semua)
    if tagar and skor < 60:
        for w in [w_ for w_, _ in pol]:   # v1.5.3 pol (sudah disaring KONTEKS_SINONIM), bukan fam
            r_ = w.replace(" ", "")
            if any(h == r_ or (h not in TAGAR_PALSU and ((len(r_) >= 3 and h.startswith(r_)) or
                                                       (len(r_) >= 4 and h.endswith(r_) and not KEPALA_LAIN.search(h[:-len(r_)] + " ")))) for h in tagar):
                skor += 45 if w == kata else 40
                cocok.append("#" + r_ + " (tagar)")
                break
    # 2) sinonim ketat (hanya bila kata persis lemah)
    if skor < 60:
        # v1.5.3: zona kuat diperiksa utk SEMUA sinonim dulu (dulu "asam lambung ×2" menghentikan loop sebelum judul "Lambung …")
        hit_ = next((w for w, rx in pol[1:] if ada(rx, kuat) or ada(rx, nama_hal)), None)
        if hit_:
            skor += 45
            cocok.append(hit_ + " (sinonim)")
        else:
            for w, rx in pol[1:]:
                if len(rx.findall(semua)) >= 2:
                    skor += 30
                    cocok.append(w + " (sinonim ×2)")
                    break
    # 2b) senada (kategori tetangga) — hanya mengangkat sampai Longgar, tidak pernah Relevan
    if skor < 15:
        for f in keluarga:
            if any(w in kata for w in f if len(w) > 2):
                hit = next((w for w in f if len(w) > 3 and w not in kata and
                            (ada(pola_kata(w), kuat) or ada(pola_kata(w), nama_hal))), None)
                if hit:
                    skor = 20
                    cocok.append(hit + " (senada)")
                    break
    # 2c) v1.5.3 niche "inti + pengubah" (kopi diet, vitamin anak, serum wajah): komponen boleh terpisah
    #     ("Curvie Coffee" + "minuman diet…"); frasa baku (jam tangan, rice cooker, rak sepatu) tidak ikut.
    tok_ = kata.split()
    if len(tok_) == 2 and skor < 40 and tok_[1] in PENGUBAH_NICHE:
        # inti mewarisi grup hanya bila ia KEPALA grup (kopi → coffee); "serum" ∉ kepala → tidak mewarisi skincare/krim
        h_fam = keluarga_sinonim(tok_[0]) if any(g[0] == tok_[0] for g in SINONIM) else [tok_[0]]
        h_rx = [pola_kata(w) for w in h_fam if " " not in w]
        m_rx = [pola_kata(w) for w in [tok_[1]] + PENGUBAH_NICHE[tok_[1]]]
        h_k = any(ada(r, kuat) or ada(r, nama_hal) for r in h_rx)
        m_k = any(ada(r, kuat) or ada(r, nama_hal) for r in m_rx)
        m_n = sum(len(r.findall(semua)) for r in m_rx)
        h_n = sum(len(r.findall(semua)) for r in h_rx)
        dekat_ = False
        if h_n and m_n and (h_k or m_k):
            ph_ = [m.start() for r in h_rx for m in r.finditer(semua)]
            pm_ = [m.start() for r in m_rx for m in r.finditer(semua)]
            dekat_ = any(abs(p - q) <= 50 and not re.search(r"[.!?\n]", semua[min(p, q):max(p, q)]) for p in ph_ for q in pm_)
        if dekat_:
            skor = max(skor, 40)
            cocok.append(f"komponen: {tok_[0]} + {tok_[1]}")
        elif h_k and m_k and skor < 20:
            skor = 20
            cocok.append(f"komponen lepas: {tok_[0]} + {tok_[1]}")
    # 2d) v1.5.4 niche WADAH + ISI ("tas laptop"): wadah disebut + isi dgn penanda muat ("Slingbag … muat laptop 14 inch",
    #     "Laptop & Tumbler Fit", "kompartemen laptop") = produk niche walau frasanya tidak menempel
    muat_ok = False
    if len(tok_) == 2 and skor < 60 and tok_[0] in WADAH_NICHE:
        w_rx = [pola_kata(w) for w in keluarga_sinonim(tok_[0]) + WADAH_NICHE[tok_[0]] if pola_kata(w)]
        isi_ = re.escape(tok_[1])
        muat_rx = re.compile(r"\b(muat|memuat|cukup|masuk|fit|kompartemen|slot|sekat|ruang|kantong|saku|sleeve)\b[^.!?\n|,/]{0,12}\b" + isi_ + r"\b|"
                             r"\b" + isi_ + r"\b(?:\s*(?:&|dan|/|\+)\s*\w+)?\s+(fit|muat|masuk|aman)\b|\b" + isi_ + r"\s+(sampai|hingga|up to)?\s*\d{2}(?:[.,]\d)?\s*(inch|inci|\"|”|in\b)")
        wadah_ada = any(ada(r, kuat) or ada(r, nama_hal) for r in w_rx) or re.search(r"\bbags?\b|luggage|tas\b", kategori)
        if wadah_ada and muat_rx.search(semua):
            skor = max(skor, 40)
            muat_ok = True
            cocok.append(f"wadah + isi: {tok_[0]} muat {tok_[1]}")
    # 2e) v1.5.4 judul DAFTAR HARGA niche ("DAFTAR HARGA MINYAK GORENG Ukuran 2L…") = penjual niche (grosir/sembako)
    _dh = re.compile(r"\b(daftar|list|price ?list|info) harga(?:\s+(?:promo|grosir|terbaru|spesial|murah|hari ini))?[^a-z0-9]{0,8}$")
    if skor < 40 and any(_dh.search(semua[max(0, m.start() - 40):m.start()]) for m in pk.finditer(semua)):
        skor = max(skor, 45)
        cocok.append(kata + " (judul daftar harga)")
    # 3) multi-kata: kata-katanya ada tapi tidak berdekatan → sebutan lepas
    token = [t for t in re.split(r"\s+", kata) if len(t) > 1]
    if len(token) >= 2 and skor < 40:
        if all(ada(pola_kata(t), semua) for t in token):
            cocok.append("kata terpisah (bukan frasa)")
    # 4) katalog campuran (DPA): mayoritas kartu bukan niche
    # v1.5.3: kartu tanpa teks (carousel gambar saja) tidak dihitung
    #          — hanya bila niche sudah terbukti di nama halaman/tautan/judul (kartu kosong ikut menyaring derau)
    _bukti_u = ada(pk, nama_hal) or ada(pk, link) or ada(pk, judul) or ada(pk, ldesc) or ada(pk, awal_body)   # v1.5.3 uji emas: Sporti.ID "…pengen sepatu bola original" + 3 kartu kosong
    _bukti_k = _bukti_u or any(ada(rx_, nama_hal) or ada(rx_, judul) for _, rx_ in pol[1:])   # v1.5.3 uji emas: sinonim di judul/nama halaman juga bukti ("Catto Cat Food")
    kartu_t = [k for k in kartu if len(re.sub(r"[\W_]", "", k)) >= 4] if _bukti_k else kartu   # v1.5.3 + pembuka body ("Susu Peninggi Badan Anak" + 3 kartu kosong)
    # v1.5.3: kartu kembar / kartu = nama halaman bukan "katalog" ("Etafit.id" ×5, "Herbal Solusi Gula Darah" ×4)
    #          — sama seperti kartu kosong: HANYA bila niche sudah terbukti di nama halaman/tautan/judul/pembuka body
    if kartu_t is not kartu:
        _nh = re.sub(r"\W+", " ", nama_hal).strip()
        kartu_t = list(dict.fromkeys(k.strip() for k in kartu_t if re.sub(r"\W+", " ", k).strip() != _nh))
        # v1.5.3 uji emas: carousel SLOGAN ("Muat Dalam Box", "50.000+ Ulasan", "FREE Bantal") bukan daftar produk
        if _bukti_u:
            kartu_t = [k for k in kartu_t if not (len(re.findall(r"[a-z]{2,}", k)) <= 3 and len(k) <= 25)]
    if len(kartu_t) >= 3:
        nyambung = sum(1 for k in kartu_t if any(ada(rx, k) for _, rx in pol))
        if 0.3 <= nyambung / len(kartu_t) < 0.5 and skor >= 40 and not any(ada(rx, nama_hal) for _, rx in pol):   # "Elzatta Hijab" utk kerudung
            # v1.5.3 uji emas: katalog campur dgn niche MINORITAS (Fantech 2/5 mouse, Bullion 2/5 rak piring, Jiniso 2/5 hijab) → kerabat
            skor = 30
            cocok.append("katalog campur: niche minoritas %d/%d kartu" % (nyambung, len(kartu_t)))
        if nyambung / len(kartu_t) < 0.3:
            # v1.5.3: kartu senada (toko "sepatu" utk niche "sneakers") = katalog tetangga, bukan campuran → maks Longgar
            _snd = [pola_kata(w) for f in keluarga if any(w in kata for w in f if len(w) > 2) for w in f if len(w) > 3 and w not in kata]
            n_snd = sum(1 for k in kartu_t if any(ada(rx, k) for _, rx in pol) or any(ada(rx, k) for rx in _snd))
            if n_snd / len(kartu_t) >= 0.5 and skor >= 15:
                skor = min(skor, 30)
                cocok.append("katalog senada: %d/%d kartu" % (n_snd, len(kartu_t)))
            else:
                skor = min(skor, 12)
                cocok.append("katalog campuran: %d/%d kartu nyambung" % (nyambung, len(kartu_t)))
    # 5) derau keras
    hib_query = re.search(r"novel|buku|drama|film|komik|game", kata)
    narasi = bool(EKSPLISIT.search(semua)) or (len(DIALOG.findall(_t(snap, "body"))) >= 2 and re.search(r"\b(aku|gua|gue|ku|dia|ia|nona|tuan)\b", body) and not LOKAL.search(semua))
    if not hib_query and (NOVEL_TEKS.search(semua) or narasi or NOVEL_APP.search(str(snap.get("link_url") or "")) or (NOVEL_HAL.search(nama_hal) and not ada(pk, kuat))):
        skor = min(skor, 5)
        cocok.append("derau: novel/drama")
    elif (ARTIKEL.search(judul + " " + body[:200]) or re.search(r"/(?:opini|berita|artikel)/", str(snap.get("link_url") or ""))) and not ada(pk, nama_hal):   # v1.5.3 giring.id/opini/
        skor = min(skor, 10)
        cocok.append("derau: iklan artikel")
    bhs = _bahasa(semua + " " + nama_hal)
    if bhs != "asing" and _bahasa(body) == "asing" and not ID_KUAT.search(semua + " " + link + " " + nama_hal):
        bhs = "asing"   # v1.5.3 badan Turki (idefix) diencerkan judul kartu Inggris/merek ("Vitamin B12 Tablet")
    if bhs == "asing":
        skor = min(skor, 8)
        cocok.append("derau: bahasa asing")
    elif bhs in ("en", "pendek") and skor >= 40 and not (ID_KUAT.search(semua + " " + link + " " + nama_hal) or
                                                       (len(body) <= 3000 and LOKAL.search(re.sub(r"\bcheckout\w*", " ", semua + " " + link + " " + nama_hal)))):
        skor = 38
        cocok.append("pasar luar? (tanpa tanda Indonesia)")
    # v1.5.3: teks Inggris tanpa tanda Indonesia & bukti HANYA sinonim/senada (jacket/hoodie FARFETCH, KUIU, cerita panjang) → lepas
    bukti_s = [c for c in cocok if not any(s_ in c for s_ in ("derau", "katalog", "sambil lalu", "kata terpisah", "pasar luar"))]
    if bhs == "en" and skor >= 15 and bukti_s and all("(sinonim" in c or "(senada)" in c for c in bukti_s) \
            and not (ID_KUAT.search(semua + " " + link + " " + nama_hal) or
                     (len(body) <= 3000 and LOKAL.search(re.sub(r"\bcheckout\w*", " ", semua + " " + link + " " + nama_hal)))):
        skor = min(skor, 12)
        cocok.append("luar: hanya sinonim Inggris")
    if skor >= 15 and MALAYSIA.search(semua + " " + nama_hal) and not ID_KUAT.search(semua + " " + link + " " + nama_hal):
        skor = min(skor, 30)
        cocok.append("pasar luar (Malaysia)")
    if skor >= 15 and re.search(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af\u0e00-\u0e7f\u0400-\u04ff]", o.get("page_name") or snap.get("page_name") or "") \
            and not ID_KUAT.search(semua + " " + link):
        skor = min(skor, 30)
        cocok.append("pasar luar (nama halaman asing)")
    if nama_hal.strip() in PLATFORM:
        afiliasi = nama_hal.strip() == "shopee" and AFILIASI.search(semua) and skor >= 40 and not PLATFORM_GENERIK.search(semua)
        if not afiliasi:
            skor = min(skor, 10)
            cocok.append("derau: iklan platform")
    if (POLITIK.search(nama_hal + " " + semua) or re.search(r"politician|politikus|political (?:party|candidate)|partai politik|government official|government organi[sz]ation|pejabat pemerintah", kategori)) and not re.search(r"partai|politik|caleg", kata):
        skor = min(skor, 10)
        cocok.append("derau: politik")
    # penyangkal: semua sebutan kuat didahului "bukan/tanpa/anti" → bukan subjek iklan
    m_all = list(pk.finditer(kuat))
    if m_all and all(NEGASI.search(kuat[max(0, m.start() - 25):m.start()]) for m in m_all) and skor >= 40:
        skor = min(skor, 18)
        cocok.append("disangkal (bukan/tanpa)")
    # 6) "produk, bukan bahan" (warisan v2.3)
    _AWALAN_BUKAN = r"(?<!mandi )(?<!air )(?<!tepung )(?<!bedak )(?<!sereal )(?<!cuka )(?<!scrub )"
    if len(token) == 1 and skor >= 15 and not any("(sinonim)" in c or "(nama halaman)" in c for c in cocok):
        k = re.escape(kata)
        pemicu_ = r"dengan|plus|mengandung|berbahan|ekstrak|extract|diperkaya|topping|varian|rasa"
        bahan = (re.search(r"\b(" + pemicu_ + r")\b[^.?!\n|\u2600-\u27bf\U0001f300-\U0001faff]{0,45}\b" + k + r"\b", semua)   # v1.5.4 emoji/baris baru = batas klausa

                 or re.search(r"\b(ubi|singkong|kentang|roti|kue|donat|bakar|goreng|ayam|ikan|es|jus|teh|susu|yoghurt|granola|cokelat|coklat|permen|keripik|snack|biskuit|mie|sambal|saus|sabun|shampoo|lotion|serum|masker|lulur)\s+" + k + r"\b", semua))
        produk = (ada(pk, nama_hal) or ada(pk, link) or ada(pk, judul)
                  or any(re.match(r"^\W*(?:[^\W_]+\W+){0,2}" + _AWALAN_BUKAN + k + r"\b", t_) for t_ in (body, judul))   # v1.5.3 uji emas "Kasur INTHEBOX favorit…"
                  or re.search(r"\b(minum|konsumsi|diminum|pakai|beli|order|pesan|checkout|ready|restock|manfaat|khasiat|ampuh)\b[^.?!]{0,40}\b" + k + r"\b", semua)
                  or re.search(r"\b" + k + r"\s+(asli|murni|hutan|super|premium|botol|gram|kg|ml|herbal|original|ori|termurah|terlaris|pintar|canggih|terbaru|andalan|favorit)\b", semua)
                  or re.search(r"(?:^|\n)[^\w\n]{0,6}" + k + r"\b(?!\s+(rasa|aroma|varian)\b)", semua)   # v1.5.4 "…70mai A410 🚗📹\nDashcam pintar…" = baris diawali niche
                  # v1.5.3: "dengan mukena yang nyaman dari …" / "dengan Mukena cantik dari Alluna" → X = barang yg dijual
                  or re.search(r"\b(dengan|pakai)\s+" + k + r"\s+(yang\s+(nyaman|cantik|adem|lembut|elegan|berkualitas|terbaik|premium)|(cantik|nyaman|adem|elegan|premium|terbaik)\s+dari)\b", semua))
        # v1.5.3 uji emas (beras): SEMUA sebutan = bahan dlm DAFTAR ("Perpaduan oat, jahe, … dan beras ketan", "kandungan … seperti beras merah",
        #          "MANDI BERAS") → bukan produk niche walau di zona kuat. Tanpa penanda daftar = kepemilikan ("komposisi lengkap suplemen",
        #          "kandungannya hanya susu", "Perpaduan Helm dengan desain retro") → tetap produk
        _ms6 = list(re.finditer(r"\b" + k + r"\b", semua))
        if not bahan and _ms6 and all(re.search(r"\b(perpaduan|kandungan\w*|komposisi|terbuat dari|berbahan dasar|campuran|formula|diformulasikan|bahan alami|bahan utama)\b(?=[^.?!|]{0,80}$)[^.?!|]*?(?:\b(?:seperti|yaitu|yakni|antara lain|berupa|meliputi|dan|serta|dari)\b|[:,&+])[^.?!|]*$", semua[max(0, m.start() - 90):m.start()])
                                      or re.search(r"\b(mandi|air|tepung|bedak|sereal|cuka|scrub)\s+$", semua[max(0, m.start() - 12):m.start()]) for m in _ms6):
            bahan = True
        if bahan and not produk:
            skor = min(skor, 12)
            cocok.append(kata + " sekadar bahan/varian")
    # 6b) v1.5.3 VARIETAS/olahan: semua sebutan (termasuk nama halaman) = "pisang madu", "mangga madu", "ayam madu" → bukan niche
    if len(token) == 1 and skor >= 15:
        zona = kuat + " | " + nama_hal + " | " + judul
        ms_v = list(pk.finditer(zona))
        _kiri = lambda m: zona[max(0, m.start() - 14):m.start()]
        if ms_v and any(VARIETAS.search(_kiri(m)) for m in ms_v) and \
                all(VARIETAS.search(_kiri(m)) or re.search(r"\b(seperti|bagai|bagaikan|kayak|semanis|selembut|ala)\s+$", _kiri(m)) for m in ms_v):
            v = next(VARIETAS.search(_kiri(m)).group(1) for m in ms_v if VARIETAS.search(_kiri(m)))
            skor = min(skor, 12)
            cocok.append(f"varietas/olahan lain ({v} {kata})")
    # 6c) v1.5.3 bukti hanya dari SINONIM AMBIGU tanpa kata konteks niche ("Orangutan Survival Foundation", "diaper bag", smartwatch "android")
    kuat_bukti = [c for c in cocok if not any(s_ in c for s_ in ("sambil lalu", "kata terpisah", "derau", "katalog", "disangkal", "sekadar"))]
    if skor >= 15 and kuat_bukti:
        amb = [c for c in kuat_bukti if any(re.match(r"#?" + w + r"\b", c) for w in SINONIM_AMBIGU)]
        if len(amb) == len(kuat_bukti):
            w0 = next(w for w in SINONIM_AMBIGU if re.match(r"#?" + w + r"\b", amb[0]))
            if not SINONIM_AMBIGU[w0].search(semua + " " + nama_hal):
                skor = min(skor, 12)
                cocok.append(f"sinonim ambigu tanpa konteks ({w0})")
    # 6d) v1.5.3 kata niche yg sering disebut sambil lalu ("tanpa pegang HP", "HP lega") wajib konteks produk
    if skor >= 40 and kata in KATA_KONTEKS and not KATA_KONTEKS[kata].search(semua + " " + nama_hal):
        skor = 30
        cocok.append("kata umum tanpa konteks produk")
    # 6e) v1.5.3 "HP" = merek laptop/printer (HP OmniBook, LaserJet), bukan handphone
    if skor >= 15 and kata == "hp" and re.search(r"\b(omnibook|pavilion|victus|envy|elitebook|probook|zbook|spectre|deskjet|laserjet|officejet|smart tank|printer|omen|chromebook|laptop|notebook)\b", semua) \
            and not re.search(r"\b(handphone|smartphone|ponsel|android|iphone|samsung|xiaomi|oppo|vivo|infinix|realme)\b", semua):
        skor = min(skor, 12)
        cocok.append("merek HP (laptop/printer)")
    # 7) v1.5.2 kata kunci hanya KETERANGAN produk lain ("Helm Hijab") di zona kuat & bukan nama halaman → maks Longgar
    if skor >= 40 and not ada(pk, nama_hal):
        for w, rx in pol:   # kata utama + sinonim (coffee/shoe/bag ikut dicek)
            hit_ket = None
            for m in rx.finditer(kuat):
                # v1.5.3 sudah ada sebutan POLOS lebih awal di segmen yg sama ("Tas Wanita … Rantai Tas Bahu") → yg ini cuma kata kunci tambahan
                _seg0 = max(b_ for b_ in _bk if b_ <= m.start())
                _polos_awal = any(not re.search(r"\b(di|ke|dalam|pada|untuk|utk)\s+$", kuat[max(0, m0.start() - 8):m0.start()])
                                  and not KEPALA_LAIN.search(kuat[max(0, m0.start() - 20):m0.start()])
                                  for _w0, rx0 in pol for m0 in rx0.finditer(kuat[:max(0, m.start() - 1)]) if m0.start() >= _seg0)
                mk = KEPALA_LAIN.search(kuat[max(0, m.start() - 20):m.start()])
                if mk and _polos_awal:
                    mk = None
                if mk and mk.group(1) in ("pemutih", "whitening") and "kecantikan" in keluarga_niche(kata):
                    mk = None   # v1.5.3 "Set Pemutih Tabir Surya" = sunscreen pencerah (produk niche itu sendiri)
                if not mk:   # v1.5.3 "Kotak Penyimpanan Pakaian, Kosmetik & Aksesoris" → wadah utk daftar barang
                    mk = KEPALA_DAFTAR.search(kuat[max(0, m.start() - 45):m.start()])
                if mk and re.search(r"\b(free|gratis|bonus|freebie)\b[^.!?\n]{0,20}$", kuat[max(0, m.start() - 45):m.start()]):
                    mk = None   # v1.5.3 "Free: Box kacamata" = bonus pelengkap, bukan produk lain
                if mk and not any(re.search(r"\b" + re.escape(mk.group(1)) + r"\b", w2) for w2 in [kata] + [p_[0] for p_ in pol]):
                    hit_ket = f"{mk.group(1)} {w}"
                    break
                me = EKOR_LAIN.search(kuat[m.end():m.end() + 16])
                if me and me.group(1) == "kaki" and w not in ("kaos", "kaus"):
                    me = None   # "kaos kaki" ≠ kaos, tapi "selop kaki" tetap sandal
                if me and me.group(1) in ("mobil", "mobilmu", "motor", "motormu", "otomotif") and "otomotif" in keluarga_niche(kata):
                    me = None   # v1.5.3 "Oli Mobil™ jaga mesin" / "helm motor" = produk otomotif itu sendiri
                if me and not any(re.search(r"\b" + re.escape(me.group(1)) + r"\b", w2) for w2 in [kata] + [p_[0] for p_ in pol]):
                    hit_ket = f"{w} {me.group(1)}"
                    break
            if hit_ket:
                skor = 30
                cocok.append(f"keterangan produk lain ({hit_ket})")
                break
    # 7f) v1.5.3 niche cuma muncul sbg "ruam popok"/"diaper rash" → krim perawatan, bukan popoknya (Kinderen, Expert Care)
    if skor >= 40 and not any(ada(rx, nama_hal) for _, rx in pol):
        sisa_ = 0; ruam_ = 0
        for w, rx in pol:
            for m in rx.finditer(semua):
                if re.search(r"\b(ruam|lecet|iritasi)\s+$", semua[max(0, m.start() - 10):m.start()]) or re.match(r"^\s*rash", semua[m.end():m.end() + 8]):
                    ruam_ += 1
                else:
                    sisa_ += 1
        if ruam_ and not sisa_:
            skor = 30
            cocok.append("keterangan produk lain (perawatan ruam)")
    # 7g) v1.5.3 niche cuma disebut sbg TEMPAT PEMAKAIAN ("Cocok buat: meja kerja", "taruh di kamar & meja") → produk lain
    if skor >= 40 and not any(ada(rx, nama_hal) or ada(rx, link) or ada(rx, judul) for _, rx in pol):
        n_lok = n_lain = 0
        for w, rx in pol:
            for m in rx.finditer(semua):
                kiri = semua[max(0, m.start() - 70):m.start()]
                if kiri.endswith("#"):
                    n_lain += 1; continue
                if re.search(r"\b(rahasia|kunci|solusi|jawaban|caranya|resep)\w*\s+(?:ada\s+|terletak\s+)?di\s+$", kiri):
                    n_lain += 1; continue   # v1.5.3 "Rahasianya ada di Pupuk Pembungaan" = produk itu sendiri
                if re.match(r"^[^.!?\n#]{0,30}\bini\b", semua[m.end():m.end() + 36]):
                    n_lain += 1; continue   # v1.5.3 "sandaran … di casing pink polkadot ini" = produk itu sendiri
                if re.search(r"\b(di|di atas|dari atas|taruh di|simpan di|letakkan di|pasang di|pajang di)\s+(?:[a-z]+\s*(?:&|dan|/)\s*)?$", kiri) or \
                   (re.search(r"\b(cocok|pas|ideal|sempurna) (buat|untuk|di|dipakai di|digunakan di)\s*:[^.!?#]{0,45}$", kiri.replace("\n", " ")) or
                    re.search(r"\b(cocok|pas|ideal|sempurna) (buat|untuk|di|dipakai di|digunakan di)\b[^.!?#:|\n\u2600-\u27bf\U0001f300-\U0001faff]{0,45}$", kiri)) and not re.search(r"\brp\.? ?\d", kiri[-45:]):
                    n_lok += 1
                else:
                    n_lain += 1
        if n_lok and not n_lain:
            skor = 30
            cocok.append("keterangan produk lain (tempat pemakaian)")
    # 7h) v1.5.3 keterangan di SELURUH teks: ≥ separuh sebutan didahului produk lain ("Popok Bayi Celana", "popok celana bayi")
    if skor >= 40 and not any((ada(rx, nama_hal) and not hal_lain) or ada(rx, judul) for _, rx in pol):
        n_k = n_s = 0
        for m in pk.finditer(semua):
            kiri = semua[max(0, m.start() - 30):m.start()]
            mk = KEPALA_LAIN.search(kiri) or re.search(KEPALA_LAIN.pattern[:-4] + r"\s+(bayi|anak|dewasa|wanita|pria|cowok|cewek|mini|kecil|besar|lipat)\s+$", kiri)
            if mk and re.search(r"(/|\bper|\bisi \d+ ?\w*)\s*$", kiri[:mk.start()]):
                mk = None   # v1.5.4 daftar grosir "Rp50.000/dus\n\nMinyak Goreng" = satuan harga, bukan "dus minyak goreng"
            if mk and not re.search(r"\b" + re.escape(mk.group(1)) + r"\b", kata):
                # "Free: Box kacamata", "gratis box + lap" = bonus, bukan produk lain yg dijual
                if not re.search(r"\b(free|gratis|bonus|freebie)\b[^.!?\n]{0,20}$", kiri):
                    n_k += 1
            else:
                n_s += 1
        if n_k and n_k >= n_s and (n_k >= 2 or n_s == 0):
            skor = 30
            cocok.append("keterangan produk lain (seluruh teks)")
    # 7k) v1.5.3 judul produk diawali KEPALA lain & niche cuma kegunaan ("Bros Korea Premium … Kebaya Hijab Jilbab Jas")
    _jdl = judul or (re.split(r"\n", _t(snap, "body").strip(), 1)[0][:120].lower() if _t(snap, "body") else "")   # judul kosong → baris pertama body
    if skor >= 40 and _jdl:
        _kj = KEPALA_LAIN.search(re.sub(r"^(jual|promo|new|ready|\W)+\s*", "", _jdl).split(" ")[0] + " ")
        _sisa = body.replace(_norm(bersih(_jdl)), " ") if not judul else body.replace(judul, " ")
        _jk = [_norm(str(c.get("title") or "")) for c in (snap.get("cards") or []) if isinstance(c, dict) and str(c.get("title") or "").strip()]
        _kartu_niche = (len(_jk) >= 2 and 2 * sum(1 for k in _jk if any(ada(rx, k) for _, rx in pol)) >= len(_jk)) \
            or any(rx.match(k.lstrip()) for k in _jk for _, rx in pol)   # kartu "Earphone | Handsfree … IDR17,000" = produk niche   # v1.5.3 Kucadi "Meja … dan Kursi …" + 3 kartu "Kursi Gaming"
        _kj_milik = _kj and any(re.match(r"\s*" + re.escape(_kj.group(1)) + r"\b", _jdl[m.end():]) for _, rx in pol for m in rx.finditer(_jdl))   # v1.5.3 "Cuci muka … sabun cuci muka" → "cuci" deskriptor niche
        if _kj and _kj.start() == 0 and not re.search(r"\b" + re.escape(_kj.group(1)) + r"\b", kata) and not _kartu_niche and not _kj_milik \
                and not any(ada(rx, nama_hal) or ada(rx, _sisa) for _, rx in pol) \
                and not any(rx.match(_jdl.lstrip()) for _, rx in pol):
            skor = 30
            cocok.append("keterangan produk lain (%s …)" % _kj.group(1))
    # 7m) v1.5.3 niche HANYA di deskripsi tautan, body bicara lain & slug tautan = produk lain (Artra "Mukenah Lipat Viral" → /page/buku-parenting)
    if skor >= 40 and len(body.strip()) >= 20 and any(ada(rx, ldesc) for _, rx in pol) \
            and not any(ada(rx, x_) for _, rx in pol for x_ in (judul, body, " ".join(kartu), nama_hal)):
        _path = re.sub(r"^\w+://[^/]+", "", str(snap.get("link_url") or "").lower().split("?")[0])
        _slug = [w_ for w_ in re.findall(r"[a-z]{4,}", " ".join(sg for sg in _path.split("/") if re.search(r"[a-z]{3,}[-_][a-z]{3,}", sg))) if w_ not in ("page", "product", "products", "shop", "item", "index", "html", "home", "promo", "order", "checkout", "landing", "universal", "link", "indonesia", "official", "store", "pages", "channel", "collections") and w_ not in nama_hal]   # slug = nama toko (espoir_indonesia) ≠ produk lain
        if len(_slug) >= 2 and not any(ada(rx, " ".join(_slug)) for _, rx in pol):
            skor = 30
            cocok.append("cuma di deskripsi tautan (tujuan: %s)" % "-".join(_slug[:3]))
    # 7l) v1.5.3 niche cuma OBJEK PEMBERSIHAN alat/jasa ("Hydro Vacuum membersihkan … kasur, gorden, bantal")
    if skor >= 40 and not re.search(r"bersih|vacu|vakum|cuci|sedot", kata):
        _ms = list(pk.finditer(semua))
        _cl = re.compile(r"\b(membersihkan|bersihkan|bersihin|dibersihkan|menyedot|sedot|vacuu?m(?!\s*(flask|insulated|bottle|tumbler|cup|mug|termos|thermos|seal|sealer|storage))|vakum(?!\s*(termos|botol))|mencuci|cuci|nyuci)\b[^.!?\n]{0,70}$")   # v1.5.3 "Vacuum Flask" = termos, bukan alat pembersih
        if _ms and all(_cl.search(semua[max(0, m.start() - 90):m.start()]) and semua[max(0, m.start() - 1):m.start()] != "#" for m in _ms):   # tagar ≠ objek
            skor = 30
            cocok.append("keterangan produk lain (objek pembersihan)")
    # 7j) v1.5.3 niche cuma BENTUK/TEMA aksesori: "Gantungan kunci freshcare fresh care kayu putih", "keychain botol kecap"
    if skor >= 40 and not re.search(r"keychain|gantungan|ganci|charm|miniatur|replika|squishy", kata):
        _mini = re.compile(r"\b(keychain|gantungan kunci|ganci|bag charm|miniatur|replika|squishy)\b[^.!?\n|]{0,70}$")
        _ms = list(pk.finditer(kuat)) or [m for _, rx in pol[1:] for m in rx.finditer(kuat)]
        if _ms and all(_mini.search(kuat[max(0, m.start() - 80):m.start()]) for m in _ms):
            skor = 30
            cocok.append("keterangan produk lain (aksesori/miniatur)")
    # 7t) v1.5.3 TINDAKAN klinik (IPL/laser/peeling/booking treatment) di halaman klinik/estetika = jasa, bukan produk niche → maks Longgar
    if skor >= 40 and not re.search(r"klinik|clinic|laser|facial|treatment|perawatan|estetik|spa\b|kecantikan|cantik|beauty", kata):
        _tind = re.compile(r"\b(ipl|laser|peeling|chemical peel|microneedling|dermapen|hifu|botox|filler|injeksi|infus whitening|"
                           r"booking treatment|booking perawatan|sesi tindakan|\d+ sesi|tindakan (dokter|medis)|treatment (di|ke) klinik|promo treatment|harga treatment)\b")
        if _tind.search(semua) and (re.search(r"klinik|clinic|estetika|aesthetic|skin ?(center|care center)|dermatolog", nama_hal + " " + kategori)
                                    or re.search(r"\b(klinik|clinic)\b", semua)):
            skor = 30
            cocok.append("jasa tindakan klinik")
    # 7r) v1.5.3 niche HANYA sbg VARIAN RASA produk lain ("tablet kunyah … rasa coklat", "4 varian: Coklat, Cheese, Strawberry")
    if skor >= 40 and not any(ada(rx, nama_hal) for _, rx in pol):
        _rasa = re.compile(r"\b(rasa|varian|varians|aroma|flavou?rs?|isian|topping|pilihan rasa|varian rasa)\b[^.!?\n#]{0,30}$")
        _mr = [m for _, rx in pol for m in rx.finditer(semua) if semua[max(0, m.start() - 1):m.start()] != "#"]
        _sifat = re.compile(r"\b(gurih|manis|alami|asli|lembut|creamy|nikmat|enak|khas|segar|lezat|autentik|original|murni|pahit)\b")
        def _var(m):
            ks = _rasa.search(semua[max(0, m.start() - 40):m.start()])
            return bool(ks) and not _sifat.search(ks.group(0))   # "rasa gurih alami susu etawa" = rasa produk niche itu sendiri
        if _mr and all(_var(m) for m in _mr):
            skor = 30
            cocok.append("cuma varian rasa produk lain")
    # 7i) v1.5.3 niche HANYA muncul sbg satu tagar di antara banyak tagar (#cycling #running #sepeda #lari … di iklan serum) → maks Longgar
    if skor >= 40 and len(tagar) >= 6 and not ada(pk, nama_hal) and not ada(pk, link):
        _tanpa_tag = re.sub(r"#[\w]+", " ", semua)
        _rp = [w_.replace(" ", "") for w_, _ in pol]
        _n_tag_niche = sum(1 for h in tagar if any(r_ in h for r_ in _rp if len(r_) >= 3))   # Siesta Bed: 4 dari 6 tagar = kasur → sah
        _sisa_kata = re.findall(r"[a-z]{3,}", re.sub(r"https?://\S+|\S+\.(?:com|id|co)\S*", " ", _tanpa_tag))   # v1.5.3 "Belanja di sini: <link> #VitaminAnak #ProdukAnak…" = tagar satu²nya isi
        _cuma_link = len(_sisa_kata) < 8 and (AFILIASI.search(semua) or any(m_ in str(snap.get("link_url") or "").lower() for m_ in MARKETPLACE))
        if not _cuma_link and not muat_ok and (_n_tag_niche == 1 or (len(tagar) >= 15 and 1 <= _n_tag_niche <= len(tagar) // 5)) and not any(ada(rx, _tanpa_tag) for _, rx in pol):   # v1.5.3 Sop Subarashi: 3 dari 21 tagar penyakit
            skor = 30
            cocok.append("cuma tagar di antara banyak tagar")
    # 7b) v1.5.2 kata kunci cuma satu item dlm DAFTAR ("untuk teh, kopi, matcha, oatmeal") & tak ada di nama halaman → maks Longgar
    if skor >= 40 and not ada(pk, nama_hal):
        ms = list(pk.finditer(kuat)) or [m for _, rx in pol[1:] for m in rx.finditer(kuat)]   # v1.5.3 juga sinonim ("kunci mobil, earphone, card holder, jepitan")
        def _di_daftar(m):
            kiri, kanan = kuat[max(0, m.start() - 30):m.start()], kuat[m.end():m.end() + 30]
            # diapit pemisah di KIRI dan KANAN: "teh, kopi, matcha" / "teh/kopi/susu" (bukan "Dari tas, dompet…" / "HIJAB SERING GERAH, …")
            kiri80 = kuat[max(0, m.start() - 80):m.start()].replace("\n", " ")
            ekor_daftar = bool(re.search(r",[^,.!?]{2,30},[^,.!?]{2,30}\s(dan|serta|&)\s*$", kiri80) and re.match(r"^\s*($|[.!\n])", kanan))
            # v1.5.4 item PERTAMA daftar di awal kalimat: "Botol susu, dot, breastpump, hingga perlengkapan MPASI …" (iklan sterilizer)
            kepala_daftar = bool(re.search(r"(^|[|.!?\n]\W*)$", kiri) and re.match(r"^\s*,\s*[^,.!?|]{1,22},\s*[^,.!?|]{1,22}(,|\s(dan|hingga|sampai|serta|atau)\b)", kuat[m.end():m.end() + 60]))
            return bool((re.search(r"(,|/|\bdan|\batau)\s*$", kiri) and re.match(r"^\s*(,|/|dan\b|atau\b|sampai\b)", kanan)) or ekor_daftar or kepala_daftar) and \
                   not re.search(r"\b" + re.escape(kata) + r"\b", judul[:len(kata) + 12])
        if ms and all(_di_daftar(m) for m in ms):
            skor = 30
            cocok.append("sebutan dalam daftar (produk lain)")
    # 7d) v1.5.3 niche cuma disebut di KALIMAT TANYA (pembuka masalah) & judul produk tanpa niche → produk lain
    #     "Lemari lembap? Rak sepatu mulai bau apek? Saatnya … Dehumidifier!" (judul: Portable Dehumidifier …)
    if skor >= 40 and judul.strip() and judul.strip() != nama_hal.strip() and not any(ada(rx, judul) for _, rx in pol) \
            and not ada(pk, nama_hal) and not ada(pk, link):
        ms = list(pk.finditer(semua))
        def _tanya(m):   # "Rak sepatu mulai bau apek?" = niche sbg TEMPAT MASALAH, bukan produk yg dijual
            ujung = re.search(r"[.!?\n]", semua[m.end():m.end() + 80])
            return bool(ujung and ujung.group(0) == "?" and MASALAH.search(semua[m.end():m.end() + ujung.start() + 1]))
        def _daftar(m):  # "lemari pakaian, rak sepatu, kabinet"
            return bool(re.search(r"(,|/)\s*$", semua[max(0, m.start() - 4):m.start()]) and re.match(r"^\s*(,|/|dan\b|hingga\b|sampai\b)", semua[m.end():m.end() + 10]))
        if ms and len(ms) <= 3 and any(_tanya(m) for m in ms) and all(_tanya(m) or _daftar(m) for m in ms):
            skor = 30
            cocok.append("disebut sbg masalah (produk lain)")
    # 7e) v1.5.3 niche cuma BONUS/FASILITAS ("FASILITAS: ✔️ 2 Helm", "BONUS hijab", "free tas") & tak di judul/nama halaman → maks Longgar
    _jb = [m for m in pk.finditer(judul)]   # v1.5.3 judul "Beli ZALA Gummy, GRATIS Masker Wajah!" = niche cuma hadiah
    _judul_bonus = bool(_jb) and all(re.search(r"\b(beli|order|pembelian|setiap)\b[^.!?\n]{2,60}\b(gratis|free|bonus)\W{0,3}$", judul[:m.start()]) for m in _jb)
    if skor >= 40 and (not ada(pk, judul) or _judul_bonus) and not ada(pk, nama_hal):
        ms_b = list(pk.finditer(semua)) or [m for _, rx in pol[1:] for m in rx.finditer(semua)]   # v1.5.3 + sinonim ("Free adaptor ugreen softcase")   # SELURUH teks: "Sering bingung cari parfum…" di bawah = produk utama
        def _bonus(m):
            kiri = semua[max(0, m.start() - 22):m.start()]
            if not BONUS.search(kiri) and re.search(r"\b(gratis|free|bonus|fasilitas)\b(?:(?! - )[^.!?\n|,]){0,22}$", semua[max(0, m.start() - 30):m.start()]):
                kiri = semua[max(0, m.start() - 30):m.start()]; return "ongkir" not in kiri   # "free adaptor ugreen softcase"
            hadiah = re.search(r"\b((?:me|di)?menangkan|undian|doorprize|door prize|giveaway|berhadiah|hadiah utama)\b[^.!?\n]{0,70}$", semua[max(0, m.start() - 80):m.start()])   # v1.5.3 "Menangkan kulkas, kompor, sepeda listrik"
            return (bool(BONUS.search(kiri)) and "ongkir" not in kiri and not re.search(r"\b(beli|buy)\s*\d+\s*(gratis|free|get|bonus)", kiri)) or bool(hadiah)
        if ms_b and all(_bonus(m) for m in ms_b):
            skor = 30
            cocok.append("cuma bonus/fasilitas")
    # 7c-1) v1.5.3 kampanye DONASI (bantuteman.org/campaign, "150+ Kucing Butuh Bantuan", wakaf) — niche cuma objek bantuan
    if skor >= 15 and (DONASI.search(semua + " " + nama_hal) or DONASI_LINK.search(str(snap.get("link_url") or ""))) and not re.search(r"donasi|wakaf|zakat|sedekah|amal", kata):
        skor = min(skor, 12)
        cocok.append("derau: donasi/penggalangan dana")
    # 7c0) v1.5.3 jasa DI DALAM niche (maklon, MUA, rias) — tetap jasa walau niche ada di judul/nama halaman
    #      tautan ke halaman PRODUK marketplace → tagar (#muabandung #makeupartist) tidak dihitung: kreator jual produk
    _sm_jasa = re.sub(r"#\w+", " ", semua) if re.search(r"shopee\.co\.id/(universal-link/)?product|tokopedia\.com/[^/\s]+/[^/\s?]+|lazada\.co\.id/products|shop\.tiktok", str(snap.get("link_url") or "")) else semua
    _jm = [m.group(0) for m in JASA_KUAT.finditer(_sm_jasa + " " + nama_hal)]
    if _jm and all(re.match(r"(mau jual|terima (jual|beli|tukar))", j) for j in _jm) \
            and re.search(r"\bmau (cari|beli)\b|\b(tempatnya |tempat )?cari (laptop|hp|kamera|iphone|macbook)\b|\bready stock\b|\b(laptop|hp|kamera|iphone|macbook) (baru|second) (ready|bergaransi)\b", _sm_jasa):
        _jm = []   # v1.5.3 Adrian Laptop "Mau cari laptop? Mau jual laptop?" = toko jual-beli, bukan cuma pembeli bekas
    if skor >= 15 and _jm and not JASA_KUAT.search(kata):
        skor = min(skor, 12)
        cocok.append("derau: jasa (maklon/MUA/kurir/pinjaman/beli bekas)")
    # 7c-app) v1.5.3 iklan APLIKASI (tautan Play Store/App Store, halaman "App page"/"Software") — bukan iklan produk niche
    _lk = str(snap.get("link_url") or "")
    if skor >= 15 and not re.search(r"aplikasi|app\b|apps|software|game", kata) and \
            (re.search(r"play\.google\.com/store|apps\.apple\.com|itunes\.apple\.com|appgallery\.huawei", _lk)
             or (any(re.fullmatch(r"\s*(app page|software|application|aplikasi|video game)\s*", str(c).lower()) for c in (snap.get("page_categories") or []))
                 and not any(m_ in _lk.lower() for m_ in MARKETPLACE))):   # v1.5.3 afiliasi Shopee berkategori "Software" (Spill By Mowii) ≠ iklan aplikasi
        if re.search(r"retail|shopping|clothing|grocery|supermarket|department store|toko|e-?commerce", kategori) and skor >= 40:
            skor = 30   # v1.5.3 Uniqlo "temukan jaket … di aplikasi UNIQLO" = peritel jualan lewat aplikasi belanja → Longgar, bukan derau
            cocok.append("lewat aplikasi belanja peritel")
        else:
            skor = min(skor, 12)
            cocok.append("derau: iklan aplikasi")
    # 7c) v1.5.2 iklan jasa/aplikasi yg pakai niche sbg contoh — kecuali niche ada di judul/nama halaman
    if skor >= 15 and JASA_APLIKASI.search(semua) and not JASA_APLIKASI.search(kata) and not ada(pk, nama_hal) \
            and (not ada(pk, judul) or re.search(r"\b(punya|bikin|buat|membuat|bangun|membangun|mulai|memulai|merintis) (brand|bisnis|usaha|merek)\b", judul)):   # v1.5.3 ebook "Punya Brand Baju Anak Tanpa Bisa Jahit"
        skor = min(skor, 12)
        cocok.append("derau: iklan jasa/aplikasi")
    # 8) v1.5.2 kategori halaman Meta jelas bukan penjual barang niche ini (McDonald's = Burgers, Cargloss = Auto Service)
    # v1.5.3 afiliasi marketplace (s.shopee…, "beli di sini") yg menjual niche di badan + tagar: kategori halaman afiliasi acak
    #        ("Kuliner 1 Menit" = Tour Agency jualan Welhome Rice Cooker) → kategori tidak dipakai
    _afl8 = _afiliasi_niche(snap, kata)
    if skor >= 15 and kategori.strip() and not _afl8:
        fam_niche = keluarga_niche(kata)
        isi_kat = [_norm(str(c)) for c in (snap.get("page_categories") or o.get("categories") or []) if str(c).strip()]
        if isi_kat:
            luar = []
            for c in isi_kat:
                hit = next((izin for _nm, rx, izin in KATEGORI_LUAR if rx.search(c)), None)
                luar.append(hit)
            nama_luar = [next((nm for nm, rx, _iz in KATEGORI_LUAR if rx.search(c)), None) for c in isi_kat]
            if all(n == "fashion" for n in nama_luar) and (ada(pk, judul) or ada(pk, body[:90])):
                luar = [None]   # fashion = sinyal lunak: niche di judul/pembuka (mis. "Helm Retro" toko apparel motor) → tak dibatasi
            if all(h is not None for h in luar):
                izin_semua = set().union(*luar)
                tegas = all(len(h) == 0 for h in luar)   # bank/media/travel: berlaku walau niche tak dikenal
                if (fam_niche and not (fam_niche & izin_semua)) or tegas:
                    if skor > 30:
                        skor = 30
                    cocok.append("kategori halaman: " + ", ".join(str(x) for x in (snap.get("page_categories") or o.get("categories") or []))[:40])
    # 8b) v1.5.3 halaman RESTO yg cuma menyebut niche SEKALI ("Crispy Duck, sambal kick", daftar menu) → bukan penjual produk niche
    if skor >= 40 and kategori.strip() and RESTO_TEGAS.search(_norm(kategori)) and not any(ada(rx, nama_hal) or ada(rx, judul) for _, rx in pol):
        _tg = len(re.findall(r"#" + re.escape(kata.replace(" ", "")) + r"\w*", semua))   # v1.5.3 Raja Roa "#sambalroa #sambalpedas #jualsambalroa"
        if sum(len(rx.findall(semua)) for _, rx in pol) + _tg <= 1:
            skor = 30
            cocok.append("resto: niche cuma disebut sekali")
    skor = max(0, min(100, skor))
    label = "Relevan" if skor >= 40 else ("Longgar" if skor >= 15 else "Lepas")
    return {"nilai": skor, "label": label, "cocok": cocok}


def _keluarga_kategori(cats):
    """Nama keluarga KATEGORI_LUAR bila SEMUA kategori halaman masuk keluarga luar yang sama-sama dikenali; selain itu None."""
    isi = [_norm(str(c)) for c in (cats or []) if str(c).strip()]
    if not isi:
        return None
    nama = []
    for c in isi:
        hit = next((nm for nm, rx, _iz in KATEGORI_LUAR if rx.search(c)), None)
        if hit is None:
            return None
        nama.append(hit)
    return nama[0]


def _afiliasi_niche(snap, kata):
    """v1.5.3: iklan afiliasi marketplace (s.shopee…/"beli di sini") yg menyebut niche di badan (di luar tagar) DAN di tagar.
    Kategori halaman afiliasi acak ("Kuliner 1 Menit" = Tour Agency jualan rice cooker) → kategori tak dipakai."""
    pk_ = pola_kata(_norm(kata or ""))
    if not pk_:
        return False
    body_ = _norm(_t(snap, "body"))
    if not (AFILIASI.search(body_) or any(m_ in str(snap.get("link_url") or "").lower() for m_ in MARKETPLACE)):
        return False
    if not pk_.search(re.sub(r"#[\w]+", " ", body_)):
        return False
    rp = _norm(kata).replace(" ", "")
    return len(rp) >= 3 and any(rp in h for h in re.findall(r"#([a-z0-9_]{3,40})", body_))


def kategori_minoritas(hasil, kata):
    """v1.5.3: hasil = list (rel, objek). Iklan Relevan dari halaman berkategori 'luar' (otomotif/resto/bank/…) yang
    MINORITAS (<20%) di antara iklan Relevan scan ini & tak diizinkan keluarga niche → maks Longgar.
    Contoh: scan 'kecantikan' → mayoritas Health & Beauty; Scuto (Servis Otomotif) = 1 halaman nyasar."""
    fam_niche = keluarga_niche(_norm(kata or ""))
    izin = {nm: iz for nm, _rx, iz in KATEGORI_LUAR}
    rel = [(r, o, _keluarga_kategori((o.get("snapshot") or {}).get("page_categories") or o.get("categories"))) for r, o in hasil if r.get("label") == "Relevan"]
    if len(rel) < 5:
        return hasil
    hitung = {}
    for _r, _o, f in rel:
        if f:
            hitung[f] = hitung.get(f, 0) + 1
    pk_m = pola_kata(_norm(kata or ""))
    for r, o, f in rel:
        if not f or (fam_niche & izin.get(f, set())) or _afiliasi_niche(o.get("snapshot") or {}, kata):
            continue
        if f == "fashion" and pk_m:
            sn = o.get("snapshot") or {}
            if pk_m.search(_norm(_t(sn, "title"))) or pk_m.search(_norm(_t(sn, "body"))[:90]):
                continue
        if hitung[f] / len(rel) < 0.2:
            r["nilai"] = min(r["nilai"], 30)
            r["label"] = "Longgar"
            r["cocok"].append("kategori halaman minoritas: " + ", ".join(str(x) for x in ((o.get("snapshot") or {}).get("page_categories") or o.get("categories") or []))[:40])
    return hasil


def konteks_halaman(hasil, kata=""):
    lokal = set()
    for r, o in hasil:
        sn = o.get("snapshot") or {}
        t = re.sub(r"\{\{[^}]*\}\}", " ", _norm(_t(sn, "body") + " " + _t(sn, "title") + " " + str(sn.get("link_url") or "")))
        if _bahasa(t) == "id" or ID_KUAT.search(t):   # v1.5.3: tanda kuat saja (dulu LOKAL lemah + "{{ad.id}}" → ".id")
            lokal.add(str(o.get("page_id") or o.get("page_name")))
    _batas = ("kategori halaman", "keterangan", "sebutan dalam daftar", "derau", "katalog", "cuma bonus", "sinonim ambigu",
              "luar:", "pasar luar (", "disangkal", "sekadar", "varietas", "jasa", "disebut sbg masalah", "kata umum", "merek hp")
    # v1.5.3 halaman yg salah satu iklannya terbukti Malaysia bukan "lokal"; iklan Relevan-nya maks Longgar (MK Curtain HQ)
    asing = {str(o.get("page_id") or o.get("page_name")) for r, o in hasil if any(c.startswith("pasar luar (Malaysia") for c in r["cocok"])}
    lokal -= asing
    for r, o in hasil:
        if str(o.get("page_id") or o.get("page_name")) in asing and r.get("nilai", 0) >= 40 and r.get("label") == "Relevan":
            r["nilai"], r["label"] = 30, "Longgar"
            r["cocok"].append("pasar luar (halaman Malaysia)")
    for r, o in hasil:
        if any(c.startswith("pasar luar?") for c in r["cocok"]) and str(o.get("page_id") or o.get("page_name")) in lokal \
                and r.get("nilai") == 38 and not any(any(c.startswith(b_) for b_ in _batas) for c in r["cocok"]):
            r["nilai"], r["label"] = 45, "Relevan"
            r["cocok"] = [c for c in r["cocok"] if not c.startswith("pasar luar?")] + ["halaman lokal"]
    """hasil: list of (rel_dict, objek). Halaman yang punya ≥1 iklan kuat (≥60, tanpa derau)
    → iklan lain halaman itu yang menyebut kata kunci/sinonim walau lemah naik +25 (maks Relevan).
    Iklan tanpa sebutan sama sekali naik ke Longgar saja (bukan Relevan) supaya tetap jujur."""
    kuat = set()
    for r, o in hasil:
        if r["nilai"] >= 60 and not any(c.startswith("derau") for c in r["cocok"]):
            kuat.add(str(o.get("page_id") or o.get("page_name")))
    for r, o in hasil:
        pid = str(o.get("page_id") or o.get("page_name"))
        if pid not in kuat or r["nilai"] >= 40 or any(c.startswith("derau") or c.startswith("katalog") or c.startswith("pasar luar") or c.startswith("disangkal") or "bahan" in c
                                                       or c.startswith("keterangan produk lain") or c.startswith("kategori halaman") or c.startswith("sebutan dalam daftar") or c.startswith("disebut sbg masalah") or c.startswith("varietas/olahan") or c.startswith("cuma bonus") or c.startswith("pasar luar (") for c in r["cocok"]):
            continue
        _kata_hal = re.escape(_norm(kata or "").strip())
        _nh_ = _norm(str((o.get("snapshot") or {}).get("page_name") or o.get("page_name") or ""))
        if r["cocok"] and all(c.endswith("(nama halaman)") for c in r["cocok"]) and _kata_hal and \
                re.search(r"\b(dekorasi|dekor|konveksi|sewa|rental|jasa|laundry|cuci|servis|service|bengkel|aksesoris|sparepart)\b[\w\s&]{0,15}" + _kata_hal, _nh_):
            r["nilai"] = min(r["nilai"] + 25, 30)   # v1.5.3 niche cuma di NAMA halaman ("Sahrul konveksi dekorasi tenda": plafon/taplak) → maks Longgar
        elif r["nilai"] >= 15:
            r["nilai"] = min(100, r["nilai"] + 25)
            if r["cocok"] and all("(senada)" in c for c in r["cocok"]):
                r["nilai"] = min(r["nilai"], 30)   # v1.5.3 uji emas: "cream (senada)" GMEELAN ≠ sunscreen — senada tetap maks Longgar
        else:
            r["nilai"] = max(r["nilai"], 20)
        r["cocok"].append("halaman niche (iklan lain halaman ini kuat)")
        r["label"] = "Relevan" if r["nilai"] >= 40 else ("Longgar" if r["nilai"] >= 15 else "Lepas")
    return hasil


def hitung_relevansi(kata, snap, o, b):
    """Nilai kecocokan iklan thd kata kunci scan (0-100) — v1.6 relevansi v3 (lihat blok di atas)."""
    return _relevansi_v3(kata, snap, o, b, bersih_tpl, KELUARGA_KATA)


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
    hb = x.get("bertahan_hari") or 0   # v1.5.3: hari BERBEDA iklan terlihat aktif oleh scan INTAI (bukti langsung, bukan tebakan)
    if hb >= 2:
        p = min((hb - 1) / 4, 1) * 10
        rincian["bertahan lintas scan"] = round(p, 1)
        poin = min(poin + p, 100)
    label = "🏆 Pemenang" if poin >= 60 else ("Kuat" if poin >= 40 else "Biasa")
    return {"nilai": round(poin, 1), "label": label, "rincian": rincian}


# ===== v1.5.3 KUMPULAN TOP 10 LINTAS SCAN (setelan "topkumpul:<niche>", tanpa SQL baru) =====
# Meta hanya memberi ±30 iklan per pencarian → tiap scan melihat potongan berbeda. Iklan yang lolos syarat Top 10
# disimpan maks TOPKUMPUL_HARI hari; scan berikutnya (manual / terjadwal, akun mana pun — data iklan publik) ikut
# menimbangnya. Iklan yang di scan kini terlihat MATI / tak lagi Relevan langsung dibuang dari kumpulan.
TOPKUMPUL_HARI = 7
TOPKUMPUL_MAKS = 40   # ±3 KB/iklan setelah dirampingkan + dikompres → ±100–150 KB per niche
TOPKUMPUL_JEJAK_HARI = 30   # v1.5.3 "bertahan lintas scan": tanggal-tanggal iklan terlihat aktif disimpan lebih lama dari iklannya
TOPKUMPUL_JEJAK_MAKS = 400  # ±60 B/iklan → ±25 KB sebelum kompres
_TK_CACHE = {}
_tk_kunci = threading.Lock()


def _tgl_wib(iso=None):
    """'YYYY-MM-DD' zona WIB (hari kalender pengguna) dari ISO UTC; None = sekarang."""
    t = _tk_epoch(iso) if iso else time.time()
    return datetime.fromtimestamp(t + 7 * 3600, tz=timezone.utc).strftime("%Y-%m-%d") if t else ""


def _tk_jejak(pool):
    """Buku jejak {aid: {p: iso pertama, h: [tgl WIB…]}}. Kumpulan lama (tanpa jejak) diturunkan dari pertama/lihat."""
    j = pool.get("jejak") if isinstance(pool.get("jejak"), dict) else {}
    j = {k: v for k, v in j.items() if isinstance(v, dict) and isinstance(v.get("h"), list)}
    for aid, e in ((pool.get("iklan") or {}) if isinstance(pool.get("iklan"), dict) else {}).items():
        if aid not in j and isinstance(e, dict):
            h = sorted({t for t in (_tgl_wib(e.get("pertama")), _tgl_wib(e.get("lihat"))) if t})
            if h:
                j[aid] = {"p": e.get("pertama") or e.get("lihat"), "h": h}
    return j


def tk_bertahan(pool, aid, kini=False):
    """Bukti 'bertahan lintas scan': di berapa HARI BERBEDA iklan ini terlihat aktif + relevan oleh scan INTAI.
    kini=True → scan sekarang (data segar Meta) ikut dihitung. None bila baru terlihat di < 2 hari."""
    try:
        e = _tk_jejak(pool or {}).get(str(aid))
        h = set((e or {}).get("h") or [])
        if kini:
            h.add(_tgl_wib())
        if len(h) < 2:
            return None
        a, b = min(h), max(h)
        rentang = (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days
        return {"hari": len(h), "sejak": a, "terakhir": b, "rentang": rentang}
    except Exception:
        return None


def _tk_kunci_niche(kata):
    return "topkumpul:" + re.sub(r"\s+", " ", _norm(kata or "")).strip()[:80]


def _tk_epoch(iso):
    try:
        return datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return 0.0


# v1.5.4 CACHE HARIAN dari scan otomatis (cron): hasil mentah Meta per niche disimpan di intai_setelan
# ("scanharian:<niche>", terkompres ±50–250 KB, tanpa SQL baru). User lain yang scan niche yang sama di HARI yang
# sama (WIB) langsung dapat hasilnya: tanpa menunggu & tanpa menambah beban ke Meta. Tombol "Scan segar" = lewati.
_CC_RAM = {}


def _copy_dalam(x):
    return json.loads(json.dumps(x))


def _cc_kunci(kata):
    return "scanharian:" + re.sub(r"\s+", " ", _norm(kata or "")).strip()[:80]


def cc_simpan(kata, mentah, info):
    try:
        isi = {"v": 1, "t": _iso_kini(), "hari": _tgl_wib(), "q": kata, "ik": mentah,
               "info": {k: v for k, v in (info or {}).items() if k not in ("log", "cache", "umur_cache")}}
        if setelan_tulis(_cc_kunci(kata), _tk_bungkus(isi)):
            _CC_RAM[_cc_kunci(kata)] = (time.time(), isi)
            while len(_CC_RAM) > 4:   # RAM Render 512 MB
                _CC_RAM.pop(min(_CC_RAM, key=lambda k: _CC_RAM[k][0]), None)
    except Exception:
        pass


def cc_baca(kata):
    """Return isi cache harian HARI INI (WIB) atau None. Gagal baca = None (scan biasa jalan)."""
    k = _cc_kunci(kata)
    c = _CC_RAM.get(k)
    if c and time.time() - c[0] < 600:
        isi = c[1]
    else:
        try:
            ok, v = setelan_baca(k)
        except Exception:
            ok, v = False, None
        if not ok:
            return None
        isi = _tk_buka(v)
        if isi.get("ik"):
            _CC_RAM[k] = (time.time(), isi)
            while len(_CC_RAM) > 4:
                _CC_RAM.pop(min(_CC_RAM, key=lambda k_: _CC_RAM[k_][0]), None)
    if not isi or not isi.get("ik") or isi.get("hari") != _tgl_wib():
        return None
    return isi


def tk_baca(kata):
    """Return dict kumpulan (bisa kosong). Cache memori 10 mnt; gagal baca = kumpulan kosong (scan tetap jalan)."""
    k = _tk_kunci_niche(kata)
    c = _TK_CACHE.get(k)
    if c and time.time() - c[0] < 600:
        return c[1]
    try:
        ok, v = setelan_baca(k)
    except Exception:
        ok, v = False, None
    v = _tk_buka(v) if ok else {}
    if ok:
        _TK_CACHE[k] = (time.time(), v)
    return v


def tk_gabung(kata, pool, out):
    """Iklan kumpulan yang TIDAK ada di scan kini, masih segar, & lolos ulang saringan relevansi versi sekarang."""
    ada = {str((x.get("detail") or {}).get("archive_id") or "") for x in out}
    batas = time.time() - TOPKUMPUL_HARI * 86400
    hasil = []
    for aid, e in ((pool or {}).get("iklan") or {}).items():
        try:
            if aid in ada or _tk_epoch(e.get("lihat")) < batas or not isinstance(e.get("x"), dict):
                continue
            x = json.loads(json.dumps(e["x"]))
            raw = (x.get("detail") or {}).get("raw") or {}
            if raw.get("snapshot"):   # aturan relevansi terus diperbarui → nilai ulang dgn versi kini
                rel = hitung_relevansi(kata, raw.get("snapshot") or {}, raw, {})
                if rel.get("label") != "Relevan":
                    continue
                x["relevansi"], x["rel_nilai"] = rel, rel.get("nilai")
            if not layak_top(x):
                continue
            x["kumpul"], x["kumpul_lihat"] = True, e.get("lihat")
            x["foto"] = None
            hasil.append(x)
        except Exception:
            continue
    return hasil


_TK_RAW_KUNCI = ("ad_archive_id", "is_active", "page_id", "page_name", "snapshot", "start_date", "end_date",
                 "publisher_platform", "categories", "collation_count", "collation_id")
_TK_SNAP_BUANG = ("page_profile_picture_url", "images", "videos", "extra_images", "extra_videos", "page_profile_uri")


def _tk_ringkas(x):
    """Salinan baris iklan untuk disimpan: tanpa foto base64 (thumb URL tetap), tanpa field turunan kumpulan.
    Data mentah Meta dirampingkan: URL gambar/video/foto profil dibuang (cukup teks utk menilai ulang relevansi)."""
    y = json.loads(json.dumps({k: v for k, v in x.items() if k not in ("foto", "kumpul", "kumpul_lihat", "bertahan")}, default=str))
    det = y.get("detail") or {}
    raw = det.get("raw") or {}
    if raw:
        raw = {k: raw[k] for k in _TK_RAW_KUNCI if k in raw}
        sn = {k: v for k, v in (raw.get("snapshot") or {}).items() if k not in _TK_SNAP_BUANG}
        if isinstance(sn.get("cards"), list):
            sn["cards"] = [{k: v for k, v in c.items() if not re.search(r"image|video|_url$|picture", k) or k == "link_url"}
                           for c in sn["cards"] if isinstance(c, dict)]
        raw["snapshot"] = sn
        det["raw"] = raw
    return y


def _tk_bungkus(v):
    """Kumpulan disimpan terkompres (±5× lebih kecil) → baca/tulis Supabase tetap ringan."""
    return {"v": 2, "z": base64.b64encode(zlib.compress(json.dumps(v, separators=(",", ":")).encode(), 6)).decode()}


def _tk_buka(v):
    if isinstance(v, dict) and v.get("z"):
        try:
            return json.loads(zlib.decompress(base64.b64decode(v["z"])).decode())
        except Exception:
            return {}
    return v if isinstance(v, dict) else {}


# ===== v1.5.4 SENADA BELAJAR: kata senada diurutkan oleh hasil NYATA scan sebelumnya (disimpan di kumpulan niche) =====
# Prior dari audit 92 niche (iklan Relevan baru per permintaan): senada memuat kata utama ±4,7, tanpa kata utama ±3,3.
# Kata yg terbukti nol berkali-kali turun ke belakang; 1 slot terakhir = giliran harian utk kata yg paling jarang dicoba,
# jadi variasi kata berganti antar hari & kumpulan Top 10 7 hari makin beragam (usulan 3: rotasi senada harian).
SENADA_PRIOR_DGN, SENADA_PRIOR_TANPA, SENADA_MAKS_SIMPAN = 4.7, 3.3, 40


def senada_urut(nilai, kandidat, stat=None, slot=8, hari=None):
    q = _norm(nilai or "").strip()
    stat = stat if isinstance(stat, dict) else {}
    hari = hari or _tgl_wib()
    lihat, kd = set(), []
    for w in kandidat:
        k = _norm(w or "").strip()
        if k and k != q and k not in lihat:
            lihat.add(k)
            kd.append(w)
    if len(kd) <= 1:
        return kd

    def rerata(w):
        n, r = (stat.get(_norm(w).strip()) or [0, 0])[:2]
        prior = SENADA_PRIOR_DGN if q and re.search(r"(?<![a-z0-9])" + re.escape(q) + r"(?![a-z0-9])", _norm(w)) else SENADA_PRIOR_TANPA
        return (float(r) + prior) / (float(n) + 1.0)

    acak = lambda w: zlib.crc32((hari + "|" + _norm(w)).encode()) / 2 ** 32   # urutan pengganti seri: berganti tiap hari
    urut = sorted(kd, key=lambda w: (-round(rerata(w), 1), acak(w)))
    if len(urut) > slot:
        dipilih = urut[:slot - 1]
        sisa = urut[slot - 1:]
        coba = min(sisa, key=lambda w: ((stat.get(_norm(w).strip()) or [0])[0], acak(w)))   # giliran: paling jarang dicoba
        urut = dipilih + [coba] + [w for w in sisa if w != coba]
    return urut


def senada_catat(pool_senada, perluasan, out):
    """Statistik baru {kata: [kali_dicoba, total_iklan_relevan_aktif]} dari scan segar (bukan cache)."""
    st = {k: list(v)[:2] for k, v in (pool_senada or {}).items() if isinstance(v, (list, tuple)) and len(v) >= 2}
    p = perluasan or {}
    if p.get("cache") or p.get("cadangan") or p.get("cadangan_berbayar"):
        return None
    dicoba = {_norm(l.get("q") or "").strip() for l in (p.get("log") or []) if l.get("status") == "ok"} & {_norm(w).strip() for w in (p.get("senada") or [])}
    if not dicoba:
        return None
    hit = {}
    for x in out:
        r_ = x.get("relevansi") or {}
        if x.get("aktif") is not False and r_.get("label") == "Relevan" and r_.get("sumber"):
            k = _norm(r_["sumber"]).strip()
            hit[k] = hit.get(k, 0) + 1
    for k in dicoba:
        n, r = st.get(k, [0, 0])
        st[k] = [int(n) + 1, int(r) + hit.get(k, 0)]
    if len(st) > SENADA_MAKS_SIMPAN:
        st = dict(sorted(st.items(), key=lambda kv: -kv[1][0])[:SENADA_MAKS_SIMPAN])
    return st


HALAMAN_MAKS_SIMPAN = 30   # v1.5.4 halaman pengiklan khusus niche yang diingat per niche
HALAMAN_MIN_R, HALAMAN_MIN_RASIO, HALAMAN_ISTIRAHAT_HARI = 3, 0.6, 7


def halaman_catat(pool_hal, perluasan, out):
    """Statistik halaman pengiklan {page_id: [nama, relevan_aktif, total_aktif, tgl]} dari scan segar.
    Hanya halaman yg punya ≥1 iklan Relevan aktif yang diingat; angka = pengamatan TERAKHIR (bukan kumulatif),
    jadi halaman yang berganti produk otomatis turun."""
    p = perluasan or {}
    if p.get("cache") or p.get("cadangan") or p.get("cadangan_berbayar") or not p.get("berhasil"):
        return None
    st = {k: list(v)[:5] for k, v in (pool_hal or {}).items() if isinstance(v, (list, tuple)) and len(v) >= 4}
    hari, kini = _tgl_wib(), {}
    nol = {str(l.get("halaman")) for l in (p.get("log") or []) if l.get("halaman") and l.get("status") == "ok" and not l.get("baru")}
    for x in out:
        if x.get("aktif") is False:
            continue
        pid = str((x.get("detail") or {}).get("page_id") or "").strip()
        if not pid.isdigit():
            continue
        k = kini.setdefault(pid, [str(x.get("halaman") or "")[:60], 0, 0, hari])
        k[2] += 1
        if (x.get("relevansi") or {}).get("label") == "Relevan":
            k[1] += 1
    for pid, v in kini.items():
        if v[1] >= 1:
            lama_nol = (st.get(pid) or [None] * 5)[4:5]
            st[pid] = v + ([hari] if pid in nol else [x for x in lama_nol if x])   # [4] = tgl bor tanpa iklan baru
        elif pid in st:
            st.pop(pid)   # terlihat lagi tanpa satu pun iklan Relevan → lupakan
    if len(st) > HALAMAN_MAKS_SIMPAN:
        st = dict(sorted(st.items(), key=lambda kv: (int(kv[1][1]), kv[1][3]), reverse=True)[:HALAMAN_MAKS_SIMPAN])
    return st


def _hari_selisih(a, b):
    try:
        return abs((datetime.strptime(b[:10], "%Y-%m-%d") - datetime.strptime(a[:10], "%Y-%m-%d")).days)
    except Exception:
        return 999


def halaman_pilih(pool_hal, maks=2, hari=None):
    """Pilih halaman pengiklan KHUSUS niche untuk dibor tanpa kata kunci (uji 16 Okt dashcam: 70mai Indonesia
    → 19 iklan baru, 17 Relevan dari 1 permintaan; halaman campuran (Arvento) → 27 baru tapi hanya 3 Relevan).
    Syarat: ≥3 iklan Relevan aktif & ≥60% iklan aktifnya Relevan. Terbaik selalu + 1 bergiliran harian."""
    hari = hari or _tgl_wib()
    kd = []
    for pid, v in (pool_hal or {}).items():
        try:
            nm, r, t = str(v[0]), int(v[1]), max(1, int(v[2]))
        except Exception:
            continue
        if len(v) >= 5 and v[4] and _hari_selisih(str(v[4]), hari) < HALAMAN_ISTIRAHAT_HARI:
            continue   # dibor tapi semua iklannya sudah tertangkap pencarian kata → istirahat dulu
        if str(pid).isdigit() and r >= HALAMAN_MIN_R and r / t >= HALAMAN_MIN_RASIO:
            kd.append((str(pid), nm, r))
    if not kd or maks <= 0:
        return []
    kd.sort(key=lambda z: (-z[2], z[0]))
    pilih = [kd[0]]
    sisa = sorted(kd[1:6], key=lambda z: zlib.crc32((hari + "|" + z[0]).encode()))
    pilih += sisa[:maks - 1]
    return [(z[0], z[1]) for z in pilih]


def tk_perbarui(kata, out, pool, dari_cache=False, senada_baru=None, halaman_baru=None):
    """Dipanggil di thread latar sesudah scan kata kunci. Best-effort: gagal = kumpulan tidak berubah."""
    try:
        with _tk_kunci:
            kini = _iso_kini()
            pool = json.loads(json.dumps(pool or {}))
            iklan = pool.get("iklan") if isinstance(pool.get("iklan"), dict) else {}
            jejak = _tk_jejak(pool)
            hari_ini = _tgl_wib()
            for x in out:
                aid = str((x.get("detail") or {}).get("archive_id") or "")
                if not aid:
                    continue
                if layak_top(x):
                    lama = iklan.get(aid) or {}
                    iklan[aid] = {"x": _tk_ringkas(x), "lihat": kini, "pertama": lama.get("pertama") or kini,
                                  "n": int(lama.get("n") or 0) + 1}
                    if not dari_cache:   # hasil cache = pengamatan lama, bukan bukti baru iklan masih jalan
                        j = jejak.setdefault(aid, {"p": kini, "h": []})
                        if hari_ini not in j["h"]:
                            j["h"] = sorted(j["h"] + [hari_ini])[-TOPKUMPUL_JEJAK_HARI:]
                else:
                    iklan.pop(aid, None)   # kini mati / bukan Relevan → keluar dari kumpulan
                    jejak.pop(aid, None)   # …dan jejak bertahannya putus
            batas = time.time() - TOPKUMPUL_HARI * 86400
            iklan = {k: v for k, v in iklan.items() if _tk_epoch(v.get("lihat")) >= batas}
            if len(iklan) > TOPKUMPUL_MAKS:
                urut = sorted(iklan.items(), key=lambda kv: (-(((kv[1].get("x") or {}).get("jual")) or 0), kv[1].get("lihat") or ""))
                iklan = dict(urut[:TOPKUMPUL_MAKS])
            batas_j = _tgl_wib(datetime.fromtimestamp(time.time() - TOPKUMPUL_JEJAK_HARI * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            jejak = {k: dict(v, h=[t for t in v["h"] if t >= batas_j]) for k, v in jejak.items()}
            jejak = {k: v for k, v in jejak.items() if v["h"]}
            if len(jejak) > TOPKUMPUL_JEJAK_MAKS:   # simpan yang paling baru terlihat, lalu yang paling banyak harinya
                jejak = dict(sorted(jejak.items(), key=lambda kv: (kv[1]["h"][-1], len(kv[1]["h"])), reverse=True)[:TOPKUMPUL_JEJAK_MAKS])
            scan = [s for s in (pool.get("scan") or []) if _tk_epoch(s) >= batas]
            if not dari_cache:
                scan.append(kini)
            baru = {"v": 1, "niche": kata, "scan": scan[-60:], "iklan": iklan, "jejak": jejak}
            sn_ = senada_baru if isinstance(senada_baru, dict) else pool.get("senada")
            if isinstance(sn_, dict) and sn_:
                baru["senada"] = sn_   # v1.5.4 statistik senada belajar ikut kumpulan (tanpa SQL baru)
            hl_ = halaman_baru if isinstance(halaman_baru, dict) else pool.get("halaman")
            if isinstance(hl_, dict) and hl_:
                baru["halaman"] = hl_   # v1.5.4 halaman pengiklan khusus niche (dibor scan berikutnya)
            if setelan_tulis(_tk_kunci_niche(kata), _tk_bungkus(baru)):
                _TK_CACHE[_tk_kunci_niche(kata)] = (time.time(), baru)
    except Exception:
        pass


def layak_top(x):
    """Syarat iklan masuk Top 10 (dipakai juga oleh kumpulan Top 10 lintas scan)."""
    if x.get("aktif") is False:  # pemenang harus hidup — iklan mati bukan acuan
        return False
    rel_x = x.get("relevansi") or {}
    if rel_x and rel_x.get("label", "Relevan") != "Relevan":   # v1.5.2: Top 10 = etalase → wajib Relevan (dulu ≥15, Longgar ikut)
        return False
    if rel_x.get("cocok") and not any(not any(t in c for t in ("sambil lalu", "halaman niche", "halaman lokal", "senada", "kata terpisah"))
                                      for c in rel_x["cocok"]):
        return False   # v1.5.2: hanya disebut sambil lalu / terangkat konteks halaman → bukan bukti produknya
    return True


def bangun_top_produk(out, kata="", tambahan=None, info=None):
    """Agregasi per PRODUK (bukan per iklan) -> ranking 10 teratas
    berdasarkan penjualan × omzet × lama aktif (Skor Jual).
    v1.5.3: `tambahan` = iklan dari kumpulan scan sebelumnya (idx lanjut setelah `out`, ditandai kumpul);
    `info` (dict) diisi jumlah iklan & produk yang lolos syarat → label jujur "Top 10 dari N iklan"."""
    grup = {}
    n_layak = n_layak_k = 0
    for idx, x in enumerate(list(out) + list(tambahan or [])):
        if not layak_top(x):
            continue
        dari_kumpul = idx >= len(out)
        if dari_kumpul:
            n_layak_k += 1
        else:
            n_layak += 1
        rel_x = x.get("relevansi") or {}
        reln = rel_x.get("nilai", 100)
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
        bt = x.get("bertahan") or None
        if bt and bt.get("hari", 0) > (g.get("bertahan") or {}).get("hari", 0):
            g["bertahan"] = bt
        g["ads"].append({"idx": idx, "umur": x["umur"], "skor": x["skor"],
                         "format": x["format"], "skor_jual": x.get("jual")})
        if bt:
            g["ads"][-1]["bertahan"] = bt.get("hari")
        if dari_kumpul:
            g["ads"][-1]["kumpul"] = True
            g["ads"][-1]["lihat"] = x.get("kumpul_lihat")

    hasil = []
    for g in grup.values():
        baris_tiru = {"umur": g["umur"], "varian": g["varian"],
                      "harga_min": g["harga_min"], "harga_max": g["harga_max"],
                      "terjual_n": g["terjual"],
                      "bertahan_hari": (g.get("bertahan") or {}).get("hari", 0)}
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
        g["rincian_jual"] = sj["rincian"]
        g["unit"] = unit
        g["ads"].sort(key=lambda a: (bool(a.get("kumpul")), -(a["skor_jual"] or 0)))   # iklan scan kini didahulukan (detail terlengkap)
        if all(a.get("kumpul") for a in g["ads"]):
            g["kumpul"] = True   # produk ini HANYA terlihat di scan sebelumnya (tidak muncul di scan kini)
            g["lihat"] = max((a.get("lihat") or "") for a in g["ads"]) or None
        hasil.append(g)
    hasil.sort(key=lambda g: -g["skor_jual"])
    # v1.5.3: maks 2 kartu per MEREK (7 cabang "Rimas Laptop …" tak memenuhi Top 10); kurang dari 10 → isi lagi
    pilih, cadang, per = [], [], {}
    for g in hasil:
        mk = _kunci_merek(g["halaman"], kata)   # v1.5.3: kata niche bukan merek ("Susu Kambing Etawa PrimaGold" ≠ "Susu Etawalin")
        if mk:   # awalan sama = merek sama ("skintific" ~ "skintificoutlet")
            mk = next((k for k in per if k and len(min(k, mk, key=len)) >= 5 and (k.startswith(mk) or mk.startswith(k))), mk)
        if mk and per.get(mk, 0) >= 2:
            cadang.append(g); continue
        per[mk] = per.get(mk, 0) + 1; pilih.append(g)
    if isinstance(info, dict):
        info.update({"iklan_layak": n_layak, "iklan_layak_kumpul": n_layak_k, "produk_layak": len(hasil)})
    return (pilih + cadang)[:10]


MEREK_UMUM = set("toko shop store official rumah pusat grosir jual info spill rekomendasi review produk belanja the my shopee pilihan "
                 "barang promo diskon galeri gudang kedai warung mitra agen distributor supplier reseller jakarta bandung surabaya "
                 "indonesia indo id kak mama bunda mom moms beli racun cek best top super mega "
                 "beauty cosmetics cosmetic kosmetik skincare makeup clinic klinik care".split())


def _kunci_merek(halaman, kata=""):
    buang = set(re.findall(r"[a-z0-9]+", _norm(kata or "")))
    h = re.sub(r"(?<=\b[a-z0-9])\.(?=[a-z0-9])|(?<=[a-z0-9])\.(?=[a-z0-9]\b)", "", _norm(halaman or ""))   # "o.two.o" → "otwoo"
    h = re.sub(r"\b([a-z0-9])\.([a-z0-9]+)\.([a-z0-9])\b", r"\1\2\3", h)
    for t in re.findall(r"[a-z0-9]+", h):
        if len(t) >= 4 and t not in MEREK_UMUM and t not in buang:
            # v1.5.3 "otwooid", "otwooindonesiabeauty" → "otwoo" (akhiran halaman cabang)
            while True:
                t2 = re.sub(r"(indonesia|official|store|shop|beauty|outlet|idn|ind|id)$", "", t)
                if t2 == t or len(t2) < 4:
                    break
                t = t2
            return t
    return None


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


def scan_dengan_antre(nilai, mode, email, segar=False):
    """v1.4.7b: 1 scan berjalan per akun + batas per jam + antre di kunci_scan (bukan `lock` global)."""
    admin = is_admin(email)
    with _kunci_scan_jalan:
        if email in SCAN_JALAN:
            return {"ok": False, "error": "Scan kamu sebelumnya masih berjalan. Tunggu sampai selesai, lalu coba lagi."}
        if not admin and not _boleh_lupa("scan:" + email, SCAN_PER_JAM, 3600):
            return {"ok": False, "error": f"Batas {SCAN_PER_JAM} scan per jam tercapai. Coba lagi nanti supaya Meta tidak memblokir INTAI."}
        SCAN_JALAN.add(email)
    try:
        if not kunci_scan.acquire(timeout=SCAN_ANTRE_DTK):
            return {"ok": False, "error": "Antrean scan sedang penuh (scan otomatis/pengguna lain). Coba lagi beberapa menit lagi."}
        try:
            return jalankan_scan(nilai, mode, pemilik=email, segar=segar)
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}
        finally:
            kunci_scan.release()
    finally:
        with _kunci_scan_jalan:
            SCAN_JALAN.discard(email)


# ===== v1.5.1 JEJAK MESIN META — untuk /admin → Status sistem =====
# Disimpan di intai_setelan["mesin_jejak"] (tanpa SQL baru) supaya tidak hilang saat Render tidur/restart:
#   e   = 150 kejadian terakhir {t, j(enis), k(ata), n, d(etik), ket}
#   rem = rehat_sampai + kena (restart tidak "lupa" sedang ditahan Meta)
#   sc  = pemakaian kredit cadangan hari ini (anggaran harian tetap terjaga walau restart)
MESIN_JEJAK = []
_MJ = {"muat": False, "simpan_t": 0.0}
_mj_kunci = threading.Lock()


def _mj_muat():
    if _MJ["muat"]:
        return
    _MJ["muat"] = True
    try:
        ok, v = setelan_baca("mesin_jejak")
    except Exception:
        ok, v = False, None
    if not ok or not isinstance(v, dict):
        return
    with _mj_kunci:
        MESIN_JEJAK[:0] = [e for e in (v.get("e") or []) if isinstance(e, dict)][-150:]
    rem = v.get("rem") or {}
    try:
        if float(rem.get("rehat_sampai") or 0) > time.time():
            with mesin.REM_KUNCI:
                mesin.REM["rehat_sampai"] = max(mesin.REM["rehat_sampai"], float(rem["rehat_sampai"]))
                mesin.REM["kena"] = max(mesin.REM["kena"], int(rem.get("kena") or 0))
        sc = v.get("sc") or {}
        if sc.get("hari") == mesin.sc_hari_ini():
            with mesin.SC_KUNCI:
                mesin.SC["hari"] = sc["hari"]
                mesin.SC["pakai"] = max(mesin.SC["pakai"], int(sc.get("pakai") or 0))
                if sc.get("sisa") is not None and mesin.SC["sisa"] is None:
                    mesin.SC["sisa"] = sc.get("sisa")
    except (TypeError, ValueError):
        pass


def _mj_simpan():
    with _mj_kunci:
        e = list(MESIN_JEJAK[-150:])
    st = mesin.sc_status()
    try:
        setelan_tulis("mesin_jejak", {"e": e,
                                      "rem": {"rehat_sampai": mesin.REM["rehat_sampai"], "kena": mesin.REM["kena"]},
                                      "sc": {"hari": st["hari"], "pakai": st["pakai_hari_ini"], "sisa": st["sisa_saldo"]}})
    except Exception:
        pass


def jejak_mesin(jenis, kata, n=0, detik=None, ket=""):
    """jenis: meta | cache | cadangan | ditahan | gagal | halaman | halaman_cadangan"""
    _mj_muat()
    with _mj_kunci:
        MESIN_JEJAK.append({"t": _iso_kini(), "j": jenis, "k": str(kata or "")[:40], "n": int(n or 0),
                            "d": detik, "ket": str(ket or "")[:90]})
        del MESIN_JEJAK[:-150]
        segera = jenis in ("ditahan", "cadangan", "halaman_cadangan", "gagal") or time.time() - _MJ["simpan_t"] > 120
        if segera:
            _MJ["simpan_t"] = time.time()
    if segera:   # jaringan di luar kunci, di thread latar → scan tidak melambat
        threading.Thread(target=_mj_simpan, daemon=True, name="jejak-mesin").start()


def mesin_status():
    _mj_muat()
    rem = mesin.rem_status()
    with _mj_kunci:
        e = list(MESIN_JEJAK)
    batas = datetime.now(timezone.utc) - timedelta(hours=24)

    def baru(x):
        try:
            return datetime.fromisoformat(str(x["t"]).replace("Z", "+00:00")) >= batas
        except (ValueError, KeyError):
            return False
    h24 = [x for x in e if baru(x)]
    hit = {}
    for x in h24:
        hit[x["j"]] = hit.get(x["j"], 0) + 1
    meta = [x for x in h24 if x["j"] == "meta" and x.get("d") is not None]
    tahan = [x for x in e if x["j"] == "ditahan"]
    return {"rem": rem,
            "batas": {"per_menit": mesin.REM_PER_MENIT, "per_10menit": mesin.REM_PER_10MENIT,
                      "rehat_menit": mesin.REM_REHAT_DTK // 60, "cache_menit": mesin.CACHE_V2_DTK // 60},
            "sc": mesin.sc_status(),
            "h24": hit,
            "rata_detik": round(sum(x["d"] for x in meta) / len(meta), 1) if meta else None,
            "rata_iklan": round(sum(x["n"] for x in meta) / len(meta)) if meta else None,
            "ditahan_terakhir": tahan[-1]["t"] if tahan else None,
            "jejak": e[-20:][::-1]}


def mesin_pulih():
    with mesin.REM_KUNCI:
        mesin.REM["rehat_sampai"] = 0.0
        mesin.REM["kena"] = 0
    jejak_mesin("pulih", "", ket="rehat diakhiri manual oleh admin")
    threading.Thread(target=_mj_simpan, daemon=True).start()
    return {"ok": True, "rem": mesin.rem_status()}


def sc_uji():
    """Admin: uji kunci cadangan dengan 1 pencarian (1 kredit)."""
    if not mesin.sc_aktif():
        return {"ok": False, "error": "INTAI_SC_KUNCI belum diisi di Environment Render"}
    if mesin._sc_jatah(1) < 1:
        return {"ok": False, "error": "Anggaran cadangan hari ini habis (INTAI_SC_HARIAN)"}
    t0 = time.time()
    try:
        ik, cur = mesin.sc_cari("kopi", "ID")
    except Exception as e:
        with mesin.SC_KUNCI:
            mesin.SC["pakai"] = max(0, mesin.SC["pakai"] - 1)
        return {"ok": False, "error": str(e)[:160]}
    jejak_mesin("cadangan", "uji kunci", len(ik), round(time.time() - t0, 1), "uji dari /admin · 1 kredit")
    return {"ok": True, "iklan": len(ik), "ada_halaman_lanjut": bool(cur), "detik": round(time.time() - t0, 1),
            "sc": mesin.sc_status()}


def jalankan_scan(nilai, mode="cari", pemilik=None, dari_jadwal=False, segar=False):
    """Scan penuh per mode: 'cari' (keyword) atau 'halaman' (intip kompetitor).
    v1.5.4: dari_jadwal → hasil Meta disimpan jadi cache harian; scan user biasa memakai cache itu (kecuali segar)."""
    s = mesin.buka_sesi()
    perluasan = None
    if mode == "halaman":
        nilai = re.sub(r"\D", "", nilai)
        if not nilai:
            return {"ok": False, "error": "ID halaman tidak valid"}
        label = f"halaman {nilai}"
        mentah, lihat_k = [], set()
        t0_h = time.time()
        ditahan_h = bool(mesin.rem_status()["rehat_detik"])   # sedang rehat → Meta tidak disentuh sama sekali
        for media in (() if ditahan_h else ("all", "video", "image")):  # variasi media + ikut iklan mati
            url = ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
                   f"&country=ID&view_all_page_id={quote(nilai)}&search_type=page&media_type={media}")
            try:
                mesin._rem_catat()
                html = ambil_halaman_url(s, url)
                if mesin.ditahan_meta(html):   # v1.5.1: isi dikosongkan Meta → rehat, berhenti
                    mesin._rem_kena()
                    ditahan_h = True
                    break
                for o in mesin.ekstrak_iklan(html):
                    aid = str(o.get("ad_archive_id") or "")
                    if aid and aid not in lihat_k:
                        lihat_k.add(aid)
                        mentah.append(o)
            except Exception:
                pass
            time.sleep(3)
        if mentah:
            jejak_mesin("halaman", nilai, len(mentah), round(time.time() - t0_h, 1))
        elif ditahan_h:
            jejak_mesin("ditahan", nilai, ket="Intip Kompetitor / Pesaing Dipantau")
        if not mentah and mesin.sc_aktif():
            try:
                mentah, kr = mesin.sc_halaman(nilai, "ID")
                if mentah:
                    jejak_mesin("halaman_cadangan", nilai, len(mentah), round(time.time() - t0_h, 1), f"{kr} kredit")
            except Exception as e:
                jejak_mesin("gagal", nilai, ket="cadangan halaman: " + str(e)[:60])
        if not mentah and ditahan_h:
            menit = max(1, round(mesin.rem_status()["rehat_detik"] / 60))
            return {"ok": False, "ditahan": True,
                    "error": f"Meta sedang membatasi pencarian dari server INTAI (bukan kesalahan Anda). "
                             f"Coba lagi sekitar {menit} menit lagi — hasil tidak dikarang selama jeda ini."}
    else:
        label = nilai
        cc = None if (dari_jadwal or segar) else cc_baca(nilai)
        if cc:
            mentah = _copy_dalam(cc["ik"])
            perluasan = dict(cc.get("info") or {}, cache=True, cron=cc.get("t"), umur_cache=int(time.time() - _tk_epoch(cc.get("t"))))
            ditahan = False
        else:
            # v1.6 mesin tajam: kata utama × 4 variasi media + kata senada (kamus sinonim → saran Shopee bila kurang)
            senada = keluarga_sinonim(nilai)[1:]
            # v1.5.4 statistik senada belajar dibaca paralel dgn saran Shopee (kumpulan niche, cache 10 mnt; gagal = prior)
            wadah_sn = []
            th_sn = threading.Thread(target=lambda: wadah_sn.append(tk_baca(nilai)), daemon=True)
            th_sn.start()
            if len(senada) < 8:
                wadah = []
                th = threading.Thread(target=lambda: wadah.extend(shp_saran(nilai)), daemon=True)
                th.start()
                th.join(4)   # saran Shopee hanya pelengkap: maks 4 detik, gagal = diabaikan
                for w in list(wadah):
                    if len(w.split()) > 4 or re.search(r"\b(official|store|shop|toko|termurah|murah|original|ori|cod)\b", w.lower()):
                        continue   # v1.5.4 saran panjang/berbau toko ("dashcam 70mai official store") = hasil Meta sangat sedikit
                    if w.lower() not in [x.lower() for x in senada] and w.lower() != nilai.lower().strip():
                        senada.append(w)
            th_sn.join(3)
            stat_sn = ((wadah_sn[0] if wadah_sn else {}) or {}).get("senada") or {}
            senada = senada_urut(nilai, senada, stat_sn)
            hal_bor = halaman_pilih(((wadah_sn[0] if wadah_sn else {}) or {}).get("halaman"))
            ditahan = False
            try:
                mentah, perluasan = mesin.kumpul_iklan_v2(nilai, "ID", senada[:8], halaman=hal_bor)
            except mesin.MetaMenahan:
                mentah, perluasan, ditahan = [], {}, True
            except Exception as e:
                mentah, perluasan = [], {"galat": str(e)[:80]}
        if dari_jadwal and mentah and not (perluasan or {}).get("cron"):
            threading.Thread(target=cc_simpan, args=(nilai, _copy_dalam(mentah), perluasan), daemon=True, name="cache-harian").start()
        if mentah:
            jejak_mesin("cache" if perluasan.get("cache") else "meta", nilai, len(mentah), perluasan.get("detik"),
                        f"{perluasan.get('berhasil', '?')}/{perluasan.get('permintaan', '?')} pencarian")
        elif ditahan:
            jejak_mesin("ditahan", nilai, ket=f"rehat {max(1, round(mesin.rem_status()['rehat_detik'] / 60))} mnt")
        # v1.5.1: Meta menahan / gagal total → cadangan berbayar (bila kunci diisi & anggaran harian masih ada)
        if not mentah and mesin.sc_aktif():
            try:
                mentah, perluasan = mesin.kumpul_iklan_sc(nilai, "ID", senada[:6])
                jejak_mesin("cadangan", nilai, len(mentah), perluasan.get("detik"),
                            "dari cache" if perluasan.get("cache") else f"{perluasan.get('kredit', 0)} kredit")
            except Exception as e:
                jejak_mesin("gagal", nilai, ket="cadangan: " + str(e)[:70])
        if not mentah and (ditahan or mesin.rem_status()["rehat_detik"]):
            menit = max(1, round(mesin.rem_status()["rehat_detik"] / 60))
            return {"ok": False, "ditahan": True,
                    "error": f"Meta sedang membatasi pencarian dari server INTAI (bukan kesalahan Anda). "
                             f"Coba lagi sekitar {menit} menit lagi — hasil tidak dikarang selama jeda ini."}
        if not mentah:   # cadangan gratis terakhir: cara lama (3 variasi media)
            mentah = mesin.kumpul_iklan(s, nilai, "ID")
            perluasan = dict(perluasan or {}, cadangan=True)
            if not mentah:
                jejak_mesin("gagal", nilai, ket="semua jalur kosong")
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
    pasangan_rel = []
    for b in rows:
        o = by_id.get(str(b["archive_id"]), {})
        if o:
            snap_r = o.get("snapshot") or {}
            rel = hitung_relevansi(nilai if mode == "cari" else "", snap_r, o, b)
            pasangan_rel.append((rel, o))
            if o.get("_sumber"):
                rel["sumber"] = o["_sumber"]
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
    if mode == "cari" and pasangan_rel:
        kategori_minoritas(pasangan_rel, nilai)   # v1.5.3: halaman kategori luar yg nyasar (Scuto di scan kecantikan)
        konteks_halaman(pasangan_rel, nilai)   # v1.6: iklan lemah dari halaman yang jelas niche ikut naik
        for x in out:
            x["rel_nilai"] = (x.get("relevansi") or {}).get("nilai", x.get("rel_nilai"))
    # v1.5.7 hakim AI untuk zona abu-abu: jalan PARALEL dgn gali harga, diterapkan sebelum agregat (maks ±8 dtk)
    wadah_ai, th_ai, t_ai = {}, None, time.time()
    if mode == "cari" and pasangan_rel and ai_aktif() and AI_HAKIM_MODE != "mati":
        _psg = [(dict(r_), o_) for r_, o_ in pasangan_rel]   # salinan: thread tidak menyentuh rel asli
        th_ai = threading.Thread(target=lambda: ai_hakim_relevansi(nilai, _psg, wadah=wadah_ai), daemon=True, name="ai-hakim")
        th_ai.start()
    rows_hidup = [b for b in rows if b.get("aktif") is not False] or rows
    # v1.6: jumlah yang dicatat/ditampilkan = iklan aktif yang NYAMBUNG (Lepas = derau niche luar)
    n_nyambung = len(rows_hidup) if mode != "cari" else sum(
        1 for x in out if x.get("aktif") is not False and (x.get("relevansi") or {}).get("label") != "Lepas")

    # v1.5.3: kumpulan Top 10 lintas scan dibaca PARALEL dgn gali harga (tak menambah waktu scan)
    wadah_tk = []
    th_tk = None
    if mode == "cari":
        th_tk = threading.Thread(target=lambda: wadah_tk.append(tk_baca(label)), daemon=True, name="topkumpul")
        th_tk.start()

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

    if th_ai is not None:   # v1.5.7
        th_ai.join(max(0.3, 9.0 - (time.time() - t_ai)))
        put_ai = {k_: v_ for k_, v_ in list(wadah_ai.items()) if k_ != "__siap"}
        if put_ai:
            ubah_ai = 0
            for rel_, o_ in pasangan_rel:
                v_ = put_ai.get(str(o_.get("ad_archive_id") or o_.get("archive_id") or ""))
                if v_ and AI_HAKIM_BAWAH <= (rel_.get("nilai") or 0) <= AI_HAKIM_ATAS:
                    lab_ = rel_.get("label")
                    _ai_hakim_terapkan(rel_, v_)
                    ubah_ai += rel_.get("label") != lab_
            for x in out:
                x["rel_nilai"] = (x.get("relevansi") or {}).get("nilai", x.get("rel_nilai"))
            n_nyambung = len(rows_hidup) if mode != "cari" else sum(
                1 for x in out if x.get("aktif") is not False and (x.get("relevansi") or {}).get("label") != "Lepas")
            AI_STAT["hakim_terakhir"] = {"kata": label, "dinilai": len(put_ai), "berubah": ubah_ai, "t": _iso_kini(),
                                         "beda": sum(1 for r_, _o in pasangan_rel if (r_.get("ai") or {}).get("usul") not in (None, (r_.get("ai") or {}).get("asal")))}
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
        if x.get("aktif") is False or (x.get("relevansi") or {}).get("label") == "Lepas":
            continue
        aid = str((x.get("detail") or {}).get("archive_id") or "")
        if aid:
            indeks_kini[aid] = {"h": x.get("halaman"), "hr": x.get("harga_min") or 0}
    # v1.4: dicatat ke riwayat tiap pemilik (scan manual = 1 akun; scan otomatis = semua akun yg memintanya)
    daftar_pemilik = [pemilik] if isinstance(pemilik, str) else [p for p in (pemilik or []) if p]
    sejak_lalu = None
    for i_pm, pm in enumerate(daftar_pemilik or [""]):
        try:
            sl = catat_scan(label, mode, n_nyambung, indeks_kini, pm)
        except Exception:
            sl = None
        if i_pm == 0:
            sejak_lalu = sl
    # v1.0 P1: arsipkan semua iklan + pengiklan + potret niche (latar, best-effort)
    ARSIP_STATUS["antri"] = ARSIP_STATUS.get("antri", 0) + 1
    threading.Thread(target=arsipkan_scan, daemon=True, name="arsip",
                     args=(out, label, mode, n_nyambung, ekonomi_niche)).start()
    # v1.6: data mentah Meta untuk iklan Lepas (disembunyikan default, tidak diarsip) tidak dikirim → respons ±40% lebih ringan
    rows_kirim = []
    for x in out:
        if (x.get("relevansi") or {}).get("label") == "Lepas" and (x.get("detail") or {}).get("raw") is not None:
            x = dict(x, detail={k: v for k, v in x["detail"].items() if k != "raw"})
        rows_kirim.append(x)
    # v1.5.3 Top 10: label jujur + kumpulan lintas scan
    pool_tk, rows_kumpul = {}, []
    if th_tk is not None:
        th_tk.join(3)   # biasanya sudah selesai selama gali harga; lambat → dilewati
        pool_tk = wadah_tk[0] if wadah_tk else {}
        try:
            rows_kumpul = tk_gabung(label, pool_tk, out)
        except Exception:
            rows_kumpul = []
        # v1.5.3 "bertahan lintas scan": hari berbeda iklan terlihat aktif (scan kini dihitung hanya bila bukan dari cache)
        segar = not bool((perluasan or {}).get("cache"))
        for x in out:
            if layak_top(x):
                bt = tk_bertahan(pool_tk, (x.get("detail") or {}).get("archive_id"), kini=segar)
                if bt:
                    x["bertahan"] = bt
        for x in rows_kumpul:
            bt = tk_bertahan(pool_tk, (x.get("detail") or {}).get("archive_id"))
            if bt:
                x["bertahan"] = bt
        if wadah_tk:   # hanya bila kumpulan benar-benar terbaca (jangan timpa kumpulan dgn versi kosong)
            threading.Thread(target=tk_perbarui, daemon=True, name="topkumpul-tulis",
                             args=(label, out, pool_tk, bool((perluasan or {}).get("cache"))),
                             kwargs={"senada_baru": senada_catat((pool_tk or {}).get("senada"), perluasan, out),
                                     "halaman_baru": halaman_catat((pool_tk or {}).get("halaman"), perluasan, out)}).start()
    info_top = {}
    top_produk = bangun_top_produk(out, label, rows_kumpul, info_top)
    dipakai_k = {a["idx"] - len(out) for g in top_produk for a in g["ads"] if a.get("kumpul")}
    rows_kumpul_kirim = [dict(x, detail={k: v for k, v in (x.get("detail") or {}).items() if k != "raw"}) if j in dipakai_k else None
                         for j, x in enumerate(rows_kumpul)]   # hanya yang dipakai Top 10 yg dikirim lengkap (respons ringan)
    batas_tk = time.time() - TOPKUMPUL_HARI * 86400
    scan_lalu = [s for s in (pool_tk.get("scan") or []) if _tk_epoch(s) >= batas_tk]
    top_sampel = {
        "iklan_scan": len(out),
        "iklan_aktif_scan": sum(1 for x in out if x.get("aktif") is not False),
        "iklan_layak": info_top.get("iklan_layak", 0),
        "iklan_layak_kumpul": info_top.get("iklan_layak_kumpul", 0),
        "produk_layak": info_top.get("produk_layak", 0),
        "produk_kumpul": sum(1 for g in top_produk if g.get("kumpul")),
        "produk_bertahan": sum(1 for g in top_produk if (g.get("bertahan") or {}).get("hari", 0) >= 2),
        "scan_lalu": len(scan_lalu),
        "sejak": min(scan_lalu) if scan_lalu else None,
        "hari": TOPKUMPUL_HARI,
    }

    return {
        "ok": True, "kata": label, "mode": mode, "jumlah": n_nyambung, "sejak_lalu": sejak_lalu,
        "label_tampil": _nama_dari_rows(out) if mode == "halaman" else None,   # v1.4.2: nama halaman, bukan ID mentah
        "jumlah_mati": sum(1 for b in rows if b.get("aktif") is False),
        "rata2": sum(x["umur"] for x in hidup) // max(len(hidup), 1),
        "tertua": {"nama": tertua_x["halaman"], "hari": tertua_x["umur"]},
        "peta_harga": peta_harga,
        "ekonomi_niche": ekonomi_niche,
        "top_produk": top_produk,
        "top_sampel": top_sampel,
        "rows_kumpul": rows_kumpul_kirim,
        "pemenang": pemenang,
        "acuan": acuan,
        "waktu": waktu,
        "angle": angle,
        "perluasan": perluasan,
        "jumlah_relevan": sum(1 for x in out if x.get("aktif") is not False
                              and (x.get("relevansi") or {}).get("label") == "Relevan"),
        "rows": rows_kirim,
    }


# v1.4.7b: proxy media aman.
#  - host dicek TEPAT (dulu "mengandung fbcdn.net" → fbcdn.net@127.0.0.1 / fbcdn.net.situsjahat.com lolos = SSRF)
#  - hanya https port 443, tanpa user@, redirect diikuti manual & dicek ulang
#  - gambar: maks 8 MB/file, cache LRU maks 48 MB total; isi bukan gambar ditolak
#  - video: TIDAK lagi dimuat utuh ke RAM/cache — dialirkan potongan demi potongan + Range diteruskan ke CDN
_HOST_GAMBAR = ("fbcdn.net", "akamaihd.net", "facebook.com")
_HOST_VIDEO = ("fbcdn.net", "akamaihd.net")
GAMBAR_MAKS = 8 * 1024 * 1024
GAMBAR_CACHE_MAKS = 48 * 1024 * 1024
VIDEO_MAKS = 400 * 1024 * 1024
_kunci_img = threading.Lock()
_img_byte = [0]


def url_media_ok(url, izin):
    try:
        p = urlparse(str(url or ""))
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
            return False
        h = (p.hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return bool(h) and any(h == d or h.endswith("." + d) for d in izin)


def _get_media(url, izin, **kw):
    """GET stream tanpa redirect otomatis; tiap lompatan redirect dicek ulang (maks 3). None = ditolak."""
    for _ in range(4):
        if not url_media_ok(url, izin):
            return None
        r = requests.get(url, allow_redirects=False, stream=True, **kw)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            r.close()
            continue
        return r
    return None


def _jenis_gambar(b, ctype):
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if ctype.startswith("image/") and ctype != "image/svg+xml":
        return ctype
    return None


def ambil_gambar(target):
    with _kunci_img:
        d = img_cache.get(target)
        if d:
            img_cache.move_to_end(target)
            return d
    try:
        r = _get_media(target, _HOST_GAMBAR, timeout=12, headers={
            "User-Agent": mesin.UA, "Referer": "https://www.facebook.com/", "Accept-Encoding": "identity"})
        if r is None:
            return None
        with r:
            if r.status_code != 200 or _int(r.headers.get("Content-Length"), 0) > GAMBAR_MAKS:
                return None
            buf = bytearray()
            for ch in r.iter_content(65536):
                buf += ch
                if len(buf) > GAMBAR_MAKS:
                    return None
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        isi = bytes(buf)
        tipe = _jenis_gambar(isi, ctype) if len(isi) >= 100 else None
        if not tipe:
            return None
        data = (tipe, isi)
        with _kunci_img:
            if target not in img_cache:
                img_cache[target] = data
                _img_byte[0] += len(isi)
                while _img_byte[0] > GAMBAR_CACHE_MAKS and img_cache:
                    _, (_, lama) = img_cache.popitem(last=False)
                    _img_byte[0] -= len(lama)
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
        out_arsip = [x for x in out if (x.get("relevansi") or {}).get("label") != "Lepas"]   # v1.6: derau tidak diarsip
        for x in out_arsip:
            b = baris_arsip_iklan(x, label, mode, kini)
            if b and b["archive_id"] not in lihat:
                lihat.add(b["archive_id"])
                iklan.append(b)
        pengiklan = baris_arsip_pengiklan(out_arsip, label, kini)
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


def _gabung_antrian(st, peserta, pesaing_semua, shp=None):
    """Tambahkan item milik `peserta` ke antrean hari ini (Shopee & niche dulu, lalu pesaing). Return jumlah item baru.
    v1.5.4: `shp` = {email: [item Shopee]} — kata kunci & toko Shopee akun (ringan: 1–2 permintaan, tanpa Meta)."""
    antrian = st.setdefault("antrian", [])
    per_k = {it["k"]: it for it in antrian}
    baru_niche, baru_hal = [], []
    sumber = [(email, _item_akun(email, cfg, pesaing_semua)) for email, cfg in peserta.items()]
    sumber += [(email, [dict(x) for x in its]) for email, its in (shp or {}).items()]
    for email, daftar_it in sumber:
        for it in daftar_it:
            lama = per_k.get(it["k"])
            if lama:
                if email not in lama.setdefault("u", []):
                    lama["u"].append(email)
                continue
            it["u"] = [email]
            per_k[it["k"]] = it
            (baru_niche if it["mode"] in ("cari", "shopee-cari", "shopee-toko", "shopee-flash") else baru_hal).append(it)
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
            if str(it.get("mode") or "").startswith("shopee"):
                h = shp_jalankan_item(it)   # v1.5.4: Shopee tidak memakai jatah/kunci Meta
            else:
                with kunci_scan:   # v1.4.7b: kunci khusus scan (sama dengan /scan) → tidak pernah dua scan bersamaan, situs tetap jalan
                    h = jalankan_scan(it["q"], it["mode"], pemilik=list(it.get("u") or []), dari_jadwal=True)
        except Exception as e:
            h = {"ok": False, "error": str(e)[:160]}
        if h.get("ditahan"):
            # v1.6: Meta sedang membatasi → item TIDAK dihitung gagal; pekerja berhenti, tick cron berikutnya melanjutkan
            with kunci_jadwal:
                st = JADWAL["status"]
                st["item_kini"] = None
                st["ditahan"] = _iso_kini()
                setelan_tulis("jadwal_status", st)
                JADWAL["thread"] = None
            return
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
            time.sleep(4 if str(it.get("mode") or "").startswith("shopee") else 15)   # v1.6: sopan ke Meta Ad Library (1 scan = s/d 12 permintaan)


def jadwal_tick(paksa=False, pemicu="cron", email=None):
    """Cepat: putuskan mulai / tambah / lanjut / tidak, lalu jalankan di thread latar.
    Cron: semua akun yg aktif. paksa ("Jalankan sekarang"): hanya antrean akun `email`."""
    if not _muat_jadwal():
        return {"ok": False, "siap": False, "pesan": "Tabel setelan belum dibuat - jalankan SUPABASE-JADWAL.sql"}
    pesaing_semua = _pesaing_semua()
    try:
        shp = shp_item_cron()   # v1.5.4: kata kunci & toko Shopee akun aktif (dibaca 1×/hari)
    except Exception:
        shp = {}
    with kunci_jadwal:
        kini = datetime.now(WIB)
        hari = kini.strftime("%Y-%m-%d")
        st = JADWAL["status"] or {}
        if paksa:
            peserta = {email: _cfg_akun(email)} if email else {}
            shp = {email: shp.get(email) or []} if email else {}
        else:
            aktif = {e: c for e, c in (JADWAL["cfgs"] or {}).items() if c.get("aktif")}
            if not aktif and not shp:
                return {"ok": True, "aksi": "nonaktif", "pesan": "Scan otomatis nonaktif di semua akun"}
            if kini.hour < JADWAL["jam"]:
                return {"ok": True, "aksi": "menunggu", "pesan": f"Menunggu jam {JADWAL['jam']:02d}:00 WIB"}
            peserta = aktif
        hari_baru = st.get("tanggal") != hari
        if hari_baru:
            st = {"tanggal": hari, "pemicu": pemicu, "mulai": _iso_kini(), "akhir": None,
                  "antrian": [], "hasil": {}, "item_kini": None}
            JADWAL["status"] = st
        n_baru = _gabung_antrian(st, peserta, pesaing_semua, shp)
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
    if not isinstance(d, dict) or not isinstance(d.get("niche", []), list):   # v1.4.7b: dulu teks dihitung per huruf → "Maksimal 3 niche"
        return {"ok": False, "error": "Format jadwal tidak valid (niche harus berupa daftar)."}
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
            "shopee": {"jalur": dict(SHP_JALUR), "aff": shp_aff_aktif(), "per_jam": SHP_PER_JAM},   # v1.5.0
            "mesin": mesin_status(),   # v1.5.1: rem Meta + cadangan berbayar
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


# v1.4.7b: batas percobaan login (dulu tanpa batas → password bisa ditebak beruntun)
LOGIN_GAGAL_AKUN = (8, 900)    # 8x salah per akun / 15 menit
LOGIN_GAGAL_IP = (30, 900)     # 30x salah per IP / 15 menit


_POLA_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,24}")


def email_valid(email):
    e = str(email or "")
    return 6 <= len(e) <= 254 and len(e.split("@")[0]) <= 64 and bool(_POLA_EMAIL.fullmatch(e)) and ".." not in e


def login_terkunci(email, ip):
    """0 = boleh mencoba; >0 = sisa menit terkunci."""
    now = time.time()
    sisa = 0
    with _kunci_lupa:
        for kunci, (maks, detik) in (("lg:" + email, LOGIN_GAGAL_AKUN), ("lgip:" + ip, LOGIN_GAGAL_IP)):
            j = [t for t in LUPA_JEJAK.get(kunci, []) if now - t < detik]
            LUPA_JEJAK[kunci] = j
            if len(j) >= maks:
                sisa = max(sisa, int((j[0] + detik - now) // 60) + 1)
    return sisa


def login_catat(email, ip, berhasil):
    with _kunci_lupa:
        if berhasil:
            LUPA_JEJAK.pop("lg:" + email, None)
            return
        now = time.time()
        for kunci in ("lg:" + email, "lgip:" + ip):
            LUPA_JEJAK.setdefault(kunci, []).append(now)
        if len(LUPA_JEJAK) > 5000:
            for k in list(LUPA_JEJAK)[:2500]:
                LUPA_JEJAK.pop(k, None)


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



# =====================================================================
# v1.5.0 — SCAN SHOPEE
# Jalur (hasil riset, lihat riset/RISET-DATA-SCAN.md):
#   S6/S8/S11  halaman versi pratinjau-link (UA facebookexternalhit) → kartu produk/toko
#   S12        API Flash Sale (UA Chrome)  → historical_sold PASTI + stok
#   saran      search_hint (UA Chrome)     → kata kunci yang diketik pembeli
#   S4         Affiliate Open API resmi    → hanya bila env INTAI_SHOPEE_AFF_ID/SECRET diisi
# Semua jalur tidak resmi KECUALI S4 → wajib gagal dengan tenang (tidak boleh menjatuhkan app).
# =====================================================================
import html as _htmlmod
import hashlib as _hashlib_shp

SHP_UA_BOT = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
SHP_UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SHP_BASIS = "https://shopee.co.id"
SHP_GAMBAR = "https://down-id.img.susercontent.com/file/"
SHP_URUT = {"laris": "sales", "terkait": "relevancy", "baru": "ctime", "murah": "price"}
SHP_URUT_LABEL = {"laris": "Terlaris", "terkait": "Terkait", "baru": "Terbaru", "murah": "Termurah"}
SHP_LENCANA = {   # gambar flag-label di kartu → nama lencana (dicek visual 14 Okt 2026)
    "id-11134258-7r98z-lykpu80ygbvs76": "Mall",
    "id-11134258-7r98r-lyalscj1g30l0b": "Star+",
    "id-11134258-7r98o-lyam4dmlnqcoba": "Star",
    "id-11134258-7r98o-lyaltcbv221q54": "Supermarket",
    "id-11134258-81ztn-mr1eubmw7wg09d": "Apotek",
}
SHP_TTL = {"cari": 30 * 60, "toko": 3 * 3600, "flash": 10 * 60, "saran": 12 * 3600, "nama": 24 * 3600}
SHP_TTL_GAGAL = 3 * 60            # jalur ditutup → jangan dihantam berulang
SHP_PER_JAM = 60                  # permintaan BARU (bukan dari cache) per akun per jam; admin bebas
SHP_CACHE = {}                    # (jenis, kunci) → (waktu, data)
SHP_GAGAL = {}                    # (jenis, kunci) → (waktu, pesan)
SHP_KUOTA = {}                    # email → [waktu, ...]
SHP_JALUR = {}                    # jalur → {ok, ms, waktu, pesan}  (untuk Admin → Status sistem)
_shp_sem = threading.BoundedSemaphore(2)
_shp_lock = threading.Lock()
SHP_AFF_ID = os.environ.get("INTAI_SHOPEE_AFF_ID", "").strip()
SHP_AFF_SECRET = os.environ.get("INTAI_SHOPEE_AFF_SECRET", "").strip()
SHP_AFF_URL = "https://open-api.affiliate.shopee.co.id/graphql"
SHP_FS_KUNCI = "shpfs_riwayat"    # setelan: {"hari": {"YYYY-MM-DD": {itemid: terjual}}} (14 hari)
SHP_FS_HARI = 14


class ShopeeTutup(Exception):
    """Jalur Shopee menolak / berubah format — tampilkan pesan tenang, bukan error 500."""


def _shp_catat(jalur, ok, t0, pesan=""):
    SHP_JALUR[jalur] = {"ok": bool(ok), "ms": int((time.time() - t0) * 1000), "waktu": _iso_kini(),
                        "pesan": str(pesan)[:160]}


def _shp_cache_ambil(jenis, kunci):
    with _shp_lock:
        c = SHP_CACHE.get((jenis, kunci))
        if c and time.time() - c[0] < SHP_TTL[jenis]:
            return c[1]
        g = SHP_GAGAL.get((jenis, kunci))
        if g and time.time() - g[0] < SHP_TTL_GAGAL:
            raise ShopeeTutup(g[1])
    return None


def _shp_cache_simpan(jenis, kunci, data):
    with _shp_lock:
        SHP_CACHE[(jenis, kunci)] = (time.time(), data)
        SHP_GAGAL.pop((jenis, kunci), None)
        if len(SHP_CACHE) > 300:   # buang yang paling tua
            for k, _ in sorted(SHP_CACHE.items(), key=lambda kv: kv[1][0])[:60]:
                SHP_CACHE.pop(k, None)


def _shp_cache_gagal(jenis, kunci, pesan):
    with _shp_lock:
        SHP_GAGAL[(jenis, kunci)] = (time.time(), pesan)


def shp_kuota(email):
    """True bila akun masih boleh memicu permintaan baru ke Shopee."""
    if is_admin(email):
        return True
    kini = time.time()
    with _shp_lock:
        daftar = [t for t in SHP_KUOTA.get(email, []) if kini - t < 3600]
        if len(daftar) >= SHP_PER_JAM:
            SHP_KUOTA[email] = daftar
            return False
        daftar.append(kini)
        SHP_KUOTA[email] = daftar
    return True


def _shp_get(url, bot=True, timeout=20, referer=None):
    if bot:
        h = {"User-Agent": SHP_UA_BOT, "Accept-Language": "id-ID,id;q=0.9"}
    else:
        h = {"User-Agent": SHP_UA_WEB, "Accept": "application/json", "Accept-Language": "id-ID,id;q=0.9",
             "Referer": referer or SHP_BASIS + "/", "X-API-SOURCE": "pc"}
    if not _shp_sem.acquire(timeout=25):
        raise ShopeeTutup("Antrean Shopee sedang penuh - coba lagi sebentar")
    try:
        return requests.get(url, headers=h, timeout=timeout)
    except requests.RequestException as e:
        raise ShopeeTutup("Shopee tidak bisa dihubungi (" + type(e).__name__ + ")")
    finally:
        _shp_sem.release()


def _shp_teks(s):
    s = re.sub(r"<script.*?</script>|<style.*?</style>", "", s, flags=re.S)
    return [x.strip() for x in _htmlmod.unescape(re.sub(r"<[^>]+>", "\x00", s)).split("\x00") if x.strip()]


def shp_angka(t):
    """'428,6RB' → 428600 · '10RB+' → 10000 · '1,2JT' → 1200000 · '109' → 109 · gagal → None."""
    m = re.search(r"(\d+(?:[.,]\d+)*)\s*(RB|JT|rb|jt|K|k|M)?", str(t or ""))
    if not m:
        return None
    a, s = m.group(1), (m.group(2) or "").upper()
    if s:
        a = a.replace(".", "").replace(",", ".") if "," in a else a
        try:
            return int(round(float(a) * (1_000_000 if s in ("JT", "M") else 1000)))
        except ValueError:
            return None
    try:
        return int(re.sub(r"[.,]", "", a))
    except ValueError:
        return None


def shp_kartu(t):
    """Kartu produk dari halaman Shopee versi pratinjau-link → list dict (tanpa tebakan)."""
    out, lihat = [], set()
    for b in t.split('aria-label="Product card"')[1:]:
        m = re.search(r'href="(/[^"]*?-i\.(\d+)\.(\d+))[?"]', b)
        if not m or m.group(3) in lihat:
            continue
        lihat.add(m.group(3))
        b = b[:b.rfind("<")] if "<" in b else b          # buang tag terpotong milik kartu berikutnya
        tk = _shp_teks(b)
        nama = None
        for a in re.findall(r'<img[^>]*alt="([^"]+)"', b):
            a = _htmlmod.unescape(a)
            if a not in ("flag-label", "promotion price", "rating-star", "custom-overlay", "location-icon",
                         "voucher-icon", "promotion-label-icon") and not a.startswith("location-") \
                    and not re.fullmatch(r"-?\d+%|&gt;.*|>.*", a):
                nama = a
                break
        if not nama:   # cadangan: token terpanjang sebelum harga
            i = tk.index("Rp") if "Rp" in tk else len(tk)
            nama = max(tk[:i], key=len, default="")
        nama = re.sub(r"\s+", " ", nama).strip()
        harga = [int(tk[i + 1].replace(".", "")) for i, x in enumerate(tk)
                 if x == "Rp" and i + 1 < len(tk) and re.fullmatch(r"[\d.]+", tk[i + 1])]
        h = harga[0] if harga else None
        coret = harga[1] if len(harga) > 1 and harga[1] > (h or 0) else None
        dsk = next((x for x in tk if re.fullmatch(r"-\d{1,2}%", x)), None)
        diskon = int(dsk[1:-1]) if dsk else (90 if any(re.fullmatch(r">\s*90% off", x) for x in tk) else None)
        if diskon is None and coret and h:
            diskon = int(round((1 - h / coret) * 100))
        rating = next((float(x) for x in tk if re.fullmatch(r"[1-5]\.\d", x)), None)
        sold_t = next((x for x in tk if "terjual" in x.lower()), None)
        kota = re.search(r'aria-label="location-([^"]{1,60})"', b)
        flag = re.search(r'<img[^>]*src="[^"]*/file/([A-Za-z0-9_-]+)"[^>]*alt="flag-label"', b)
        gb = re.search(r'src="(https://down-id\.img\.susercontent\.com/file/[A-Za-z0-9_-]+)', b)
        # label promo = token pendek di antara harga dan rating
        try:
            awal = max(i for i, x in enumerate(tk) if x == "Rp") + 2
        except ValueError:
            awal = 0
        label = []
        for x in tk[awal:]:
            if re.fullmatch(r"[1-5]\.\d", x) or (kota and x == _htmlmod.unescape(kota.group(1))):
                break
            if not re.fullmatch(r"-\d+%|>\s*90% off|\+ ?\d+|Ad|Iklan", x) and "terjual" not in x.lower() \
                    and len(x) <= 34 and x not in label:
                label.append(x)
        out.append({
            "itemid": m.group(3), "shopid": m.group(2), "nama": nama[:180],
            "url": SHP_BASIS + "/product/" + m.group(2) + "/" + m.group(3),
            "gambar": gb.group(1) if gb else "",
            "harga": h, "harga_coret": coret, "diskon": diskon, "rating": rating,
            "terjual_teks": sold_t, "terjual": shp_angka(sold_t) if sold_t else None,
            "kota": _htmlmod.unescape(kota.group(1)) if kota else None,
            "lencana": (SHP_LENCANA.get(flag.group(1), "Lencana") if flag else None),
            "label": label[:4],
            "iklan": ("Ad" in tk or "Iklan" in tk),
        })
    return out


def _shp_halaman_kartu(url, jalur):
    """Ambil halaman versi bot → kartu; SSR kadang kosong → coba ulang 1×."""
    t0 = time.time()
    terakhir = ""
    for coba in range(2):
        r = _shp_get(url, bot=True)
        if r.status_code != 200:
            terakhir = f"HTTP {r.status_code}"
            if r.status_code in (403, 418, 429):
                break
        else:
            k = shp_kartu(r.text)
            if k:
                _shp_catat(jalur, True, t0, f"{len(k)} kartu")
                return k, r.text
            terakhir = "0 kartu (format halaman berubah / SSR kosong)"
        if coba == 0:
            time.sleep(1.2)
    _shp_catat(jalur, False, t0, terakhir)
    raise ShopeeTutup("Shopee sedang tidak mengirim daftar produk (" + terakhir + "). Coba lagi beberapa menit lagi.")


def _shp_kuartil(a, p):
    if not a:
        return None
    a = sorted(a)
    k = (len(a) - 1) * p
    f = int(k)
    c = min(f + 1, len(a) - 1)
    return int(round(a[f] + (a[c] - a[f]) * (k - f)))


def shp_ringkas(prod):
    """Ringkasan JUJUR dari sampel kartu (bukan seluruh pasar)."""
    hg = [p["harga"] for p in prod if p.get("harga")]
    n = len(prod)
    r = {"n": n}
    if hg:
        lo, hi = min(hg), max(hg)
        r["harga"] = {"min": lo, "p25": _shp_kuartil(hg, .25), "median": _shp_kuartil(hg, .5),
                      "p75": _shp_kuartil(hg, .75), "max": hi}
        # histogram 8 kelompok, batas dibulatkan supaya mudah dibaca
        import math
        if hi > lo:
            langkah = (hi - lo) / 8
            pangkat = 10 ** max(0, int(math.log10(max(langkah, 1))))
            langkah = max(pangkat, math.ceil(langkah / pangkat) * pangkat)
            dasar = (lo // pangkat) * pangkat
            ember = []
            for i in range(8):
                a, b = dasar + i * langkah, dasar + (i + 1) * langkah
                ember.append({"dari": int(a), "sampai": int(b),
                              "n": sum(1 for x in hg if a <= x < b or (i == 7 and x >= a))})
                if b > hi:
                    break
            r["histogram"] = ember
    dk = [p["diskon"] for p in prod if p.get("diskon")]
    r["diskon"] = {"persen_produk": round(100 * len(dk) / n) if n else 0,
                   "rata": round(sum(dk) / len(dk)) if dk else 0}
    rt = [p["rating"] for p in prod if p.get("rating")]
    r["rating_rata"] = round(sum(rt) / len(rt), 2) if rt else None
    lc = {}
    for p in prod:
        lc[p.get("lencana") or "Biasa"] = lc.get(p.get("lencana") or "Biasa", 0) + 1
    r["lencana"] = sorted(([k, v] for k, v in lc.items()), key=lambda x: -x[1])
    kt = {}
    for p in prod:
        if p.get("kota"):
            kt[p["kota"]] = kt.get(p["kota"], 0) + 1
    r["kota"] = sorted(([k, v] for k, v in kt.items()), key=lambda x: -x[1])[:6]
    r["kota_n"] = sum(kt.values())
    tk = {}
    for i, p in enumerate(prod):
        d = tk.setdefault(p["shopid"], {"shopid": p["shopid"], "n": 0, "contoh": p["nama"][:70],
                                        "posisi": i + 1, "lencana": p.get("lencana")})
        d["n"] += 1
    r["toko"] = sorted((v for v in tk.values() if v["n"] >= 2), key=lambda x: (-x["n"], x["posisi"]))[:6]
    r["toko_unik"] = len(tk)
    lb = {}
    for p in prod:
        for x in p.get("label") or []:
            lb[x] = lb.get(x, 0) + 1
    r["label"] = sorted(([k, v] for k, v in lb.items()), key=lambda x: -x[1])[:8]
    r["terjual_tampil"] = sum(1 for p in prod if p.get("terjual") is not None)
    return r


def shp_saran(q):
    """Kata kunci yang sedang diketik pembeli (search_hint). Gagal → []."""
    q = q.strip().lower()[:60]
    try:
        c = _shp_cache_ambil("saran", q)
    except ShopeeTutup:
        return []
    if c is not None:
        return c
    t0 = time.time()
    try:
        r = _shp_get(SHP_BASIS + "/api/v4/search/search_hint?keyword=" + quote(q) + "&search_type=0&version=1",
                     bot=False, timeout=12)
        kw = [str(x.get("keyword") or "").strip() for x in (r.json().get("keywords") or [])] if r.status_code == 200 else []
        kw = [k for k in dict.fromkeys(kw) if k and k != q][:10]
        _shp_catat("saran", r.status_code == 200, t0, f"HTTP {r.status_code}, {len(kw)} kata")
    except (ShopeeTutup, ValueError, AttributeError) as e:
        _shp_catat("saran", False, t0, str(e))
        _shp_cache_gagal("saran", q, "saran tidak tersedia")
        return []
    _shp_cache_simpan("saran", q, kw)
    return kw


# ---- Affiliate Open API (resmi) — hanya aktif bila env diisi ----
def shp_aff_aktif():
    return bool(SHP_AFF_ID and SHP_AFF_SECRET)


def shp_aff_kueri(query):
    """POST graphql bertanda tangan SHA256. → (ok, data|pesan)."""
    if not shp_aff_aktif():
        return False, "Affiliate API belum diisi (env INTAI_SHOPEE_AFF_ID / INTAI_SHOPEE_AFF_SECRET)"
    payload = json.dumps({"query": query}, separators=(",", ":"))
    ts = str(int(time.time()))
    sig = _hashlib_shp.sha256((SHP_AFF_ID + ts + payload + SHP_AFF_SECRET).encode()).hexdigest()
    t0 = time.time()
    try:
        r = requests.post(SHP_AFF_URL, data=payload, timeout=20, headers={
            "Content-Type": "application/json",
            "Authorization": f"SHA256 Credential={SHP_AFF_ID},Timestamp={ts},Signature={sig}"})
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        _shp_catat("affiliate", False, t0, type(e).__name__)
        return False, "Affiliate API tidak bisa dihubungi"
    if d.get("errors"):
        e = d["errors"][0] if isinstance(d["errors"], list) and d["errors"] else {}
        kode = (e.get("extensions") or {}).get("code")
        pesan = {10020: "tanda tangan ditolak - cek App ID/Secret", 10030: "batas permintaan Affiliate API tercapai",
                 10035: "akun belum punya akses Open API"}.get(kode, str(e.get("message") or "error")[:100])
        _shp_catat("affiliate", False, t0, f"{kode}: {pesan}")
        return False, f"Affiliate API: {pesan}"
    _shp_catat("affiliate", True, t0, "ok")
    return True, d.get("data") or {}


def shp_aff_cari(q, toko=None):
    """Produk + angka terjual RESMI (field sales). toko=shopId → produk toko itu."""
    filt = f'shopId:{int(toko)},' if toko else f'keyword:{json.dumps(q[:80])},'
    query = ("{productOfferV2(" + filt + "sortType:2,page:1,limit:50){nodes{itemId shopId productName sales "
             "priceMin priceMax ratingStar shopName imageUrl priceDiscountRate commissionRate}}}")
    ok, d = shp_aff_kueri(query)
    if not ok:
        return None, d
    out = []
    for x in ((d.get("productOfferV2") or {}).get("nodes") or []):
        try:
            out.append({"itemid": str(x.get("itemId")), "shopid": str(x.get("shopId")),
                        "nama": str(x.get("productName") or "")[:180], "terjual": _int(x.get("sales")),
                        "harga": _int(float(x.get("priceMin") or 0)), "harga_max": _int(float(x.get("priceMax") or 0)),
                        "rating": round(float(x.get("ratingStar") or 0), 2) or None,
                        "toko": str(x.get("shopName") or "")[:80], "gambar": str(x.get("imageUrl") or ""),
                        "diskon": _int(x.get("priceDiscountRate")), "komisi": x.get("commissionRate"),
                        "url": SHP_BASIS + "/product/" + str(x.get("shopId")) + "/" + str(x.get("itemId"))})
        except (TypeError, ValueError):
            continue
    return out, ""


def shopee_cari(q, urut="laris", email=None):
    q = re.sub(r"\s+", " ", str(q or "")).strip()[:80]
    if len(q) < 2:
        return {"ok": False, "error": "Ketik kata kunci minimal 2 huruf"}
    urut = urut if urut in SHP_URUT else "laris"
    kunci = (q.lower(), urut)
    try:
        c = _shp_cache_ambil("cari", kunci)
    except ShopeeTutup as e:
        return {"ok": False, "tutup": True, "error": str(e)}
    if c is not None:
        return dict(c, cache=True)
    if email and not shp_kuota(email):
        return {"ok": False, "error": f"Batas {SHP_PER_JAM} scan Shopee baru per jam tercapai - hasil yang sudah pernah dibuka tetap bisa dilihat"}
    url = SHP_BASIS + "/search?keyword=" + quote(q) + "&sortBy=" + SHP_URUT[urut] + ("&order=asc" if urut == "murah" else "")
    try:
        prod, _ = _shp_halaman_kartu(url, "cari")
    except ShopeeTutup as e:
        _shp_cache_gagal("cari", kunci, str(e))
        return {"ok": False, "tutup": True, "error": str(e)}
    for i, p in enumerate(prod):
        p["posisi"] = i + 1
    hasil = {"ok": True, "q": q, "urut": urut, "urut_label": SHP_URUT_LABEL[urut], "waktu": _iso_kini(),
             "produk": prod, "ringkas": shp_ringkas(prod), "saran": shp_saran(q),
             "sumber": "Halaman pencarian Shopee (versi pratinjau link) - " + str(len(prod)) + " produk teratas",
             "aff": None, "aff_pesan": ""}
    if shp_aff_aktif():
        a, pesan = shp_aff_cari(q)
        hasil["aff"], hasil["aff_pesan"] = a, pesan
        if a:   # tempelkan angka terjual resmi ke kartu yang sama
            peta = {x["itemid"]: x["terjual"] for x in a}
            for p in prod:
                if p["itemid"] in peta and peta[p["itemid"]] is not None:
                    p["terjual_resmi"] = peta[p["itemid"]]
    _shp_cache_simpan("cari", kunci, hasil)
    return hasil


def _shp_tgl(off=0):
    return (datetime.now(WIB) - timedelta(days=off)).strftime("%Y-%m-%d")


def shp_username_ke_id(nama):
    """'glowfxbeauty' → '573351606' lewat halaman toko versi bot. Gagal → None."""
    nama = nama.strip().strip("/").lower()
    if not re.fullmatch(r"[a-z0-9_.-]{2,40}", nama) or nama in ("search", "shop", "product", "flash_sale", "mall", "m", "list"):
        return None
    try:
        c = _shp_cache_ambil("nama", nama)
    except ShopeeTutup:
        return None
    if c is not None:
        return c
    try:
        r = _shp_get(SHP_BASIS + "/" + quote(nama), bot=True, timeout=15)
    except ShopeeTutup:
        return None
    sid = None
    if r.status_code == 200:
        hit = {}
        for x in re.findall(r'-i\.(\d{3,14})\.\d+', r.text):   # kartu produk milik toko ini = mayoritas
            hit[x] = hit.get(x, 0) + 1
        sid = max(hit, key=hit.get) if hit and max(hit.values()) >= 3 else None
    if sid:
        _shp_cache_simpan("nama", nama, sid)
    else:
        _shp_cache_gagal("nama", nama, "tidak ditemukan")
    return sid


def shopee_toko(shopid, email=None):
    mentah = str(shopid or "").strip()[:200]
    m = re.search(r"/shop/(\d{3,14})", mentah) or re.search(r"-i\.(\d{3,14})\.\d+", mentah) or re.search(r"/product/(\d{3,14})/", mentah)
    if m:
        sid = m.group(1)
    elif re.fullmatch(r"\d{3,14}", mentah):
        sid = mentah
    else:
        nama = re.sub(r"^(https?://)?(www\.)?shopee\.co\.id/", "", mentah).split("?")[0].strip("/")
        sid = shp_username_ke_id(nama) if "/" not in nama else None
        if not sid:
            return {"ok": False, "error": "Toko tidak ditemukan - tempel link toko (shopee.co.id/namatoko), link produk, atau ID toko (angka)"}
    try:
        c = _shp_cache_ambil("toko", sid)
    except ShopeeTutup as e:
        return {"ok": False, "tutup": True, "error": str(e)}
    if c is not None:
        return dict(c, cache=True)
    if email and not shp_kuota(email):
        return {"ok": False, "error": f"Batas {SHP_PER_JAM} permintaan Shopee baru per jam tercapai"}
    t0 = time.time()
    try:
        r = _shp_get(SHP_BASIS + "/shop/" + sid, bot=True)
    except ShopeeTutup as e:
        return {"ok": False, "tutup": True, "error": str(e)}
    if r.status_code != 200:
        _shp_catat("toko", False, t0, f"HTTP {r.status_code}")
        _shp_cache_gagal("toko", sid, f"Profil toko tidak bisa dibuka (HTTP {r.status_code})")
        return {"ok": False, "tutup": True, "error": f"Profil toko tidak bisa dibuka (HTTP {r.status_code})"}
    t = r.text
    org = {}
    for j in re.findall(r'<script[^>]*ld\+json[^>]*>(.*?)</script>', t, re.S):
        try:
            o = json.loads(j)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("@type") == "Organization":
            org = o
            break
    if not org:
        _shp_catat("toko", False, t0, "tanpa data Organization")
        return {"ok": False, "tutup": True, "error": "Format profil toko Shopee berubah - data toko belum bisa dibaca"}
    ag = org.get("aggregateRating") or {}
    pg = re.search(r'([\d.,]+\s*(?:RB|JT|rb|jt)?)\s*(?:<[^>]*>\s*)*Pengikut', t)
    tk = _shp_teks(t.split('aria-label="Product card"')[0])
    live = "LIVE" in tk
    vouc = []
    for i, x in enumerate(tk):
        if re.fullmatch(r"Diskon Rp[\d.,]+\s*(RB|JT)?|Diskon \d+%|Cashback.*|Gratis Ongkir.*", x) and len(vouc) < 6:
            mn = tk[i + 1] if i + 1 < len(tk) and tk[i + 1].startswith("Min.") else ""
            vouc.append((x + (" · " + mn if mn else "")).strip())
    kamp = None
    if live:
        i = tk.index("LIVE")
        kamp = next((x for x in tk[i + 1:i + 4] if len(x) > 6 and not x.isdigit()), None)
    toko = {"shopid": sid, "nama": str(org.get("name") or "")[:100], "url": str(org.get("url") or SHP_BASIS + "/shop/" + sid),
            "logo": str(org.get("image") or ""), "ulasan": _int(ag.get("ratingCount")),
            "rating": round(float(ag.get("ratingValue")), 2) if ag.get("ratingValue") else None,
            "pengikut": shp_angka(pg.group(1)) if pg else None, "pengikut_teks": pg.group(1).strip() if pg else None,
            "live": live, "kampanye_live": kamp, "voucher": vouc}
    toko["username"] = toko["url"].rstrip("/").rsplit("/", 1)[-1] if "/shop/" not in toko["url"] else None
    _shp_catat("toko", True, t0, toko["nama"])
    # 37 produk terlaris toko (S11)
    try:
        prod, _ = _shp_halaman_kartu(SHP_BASIS + "/shop/" + sid + "/search?sortBy=sales", "toko_laris")
        laris_pesan = ""
    except ShopeeTutup as e:
        prod, laris_pesan = [], str(e)
    for i, p in enumerate(prod):
        p["posisi"] = i + 1
    # riwayat harian pengikut & ulasan (untuk pertumbuhan) → setelan shptoko:<id>
    riw = []
    ok, v = setelan_baca("shptoko:" + sid)
    if ok and isinstance(v, list):
        riw = [x for x in v if isinstance(x, list) and len(x) == 3]
    hari = _shp_tgl()
    if toko["ulasan"] or toko["pengikut"]:
        riw = [x for x in riw if x[0] != hari] + [[hari, toko["pengikut"], toko["ulasan"]]]
        riw = sorted(riw)[-90:]
        setelan_tulis("shptoko:" + sid, riw)
    tumbuh = None
    if len(riw) >= 2:
        a, b = riw[0], riw[-1]
        try:
            hr = max(1, (datetime.strptime(b[0], "%Y-%m-%d") - datetime.strptime(a[0], "%Y-%m-%d")).days)
        except ValueError:
            hr = 1
        tumbuh = {"sejak": a[0], "hari": hr,
                  "pengikut": (b[1] - a[1]) if (a[1] is not None and b[1] is not None) else None,
                  "ulasan": (b[2] - a[2]) if (a[2] is not None and b[2] is not None) else None}
    hasil = {"ok": True, "toko": toko, "produk": prod, "laris_pesan": laris_pesan, "ringkas": shp_ringkas(prod) if prod else None,
             "riwayat": riw, "tumbuh": tumbuh, "waktu": _iso_kini(), "aff": None, "aff_pesan": ""}
    if shp_aff_aktif():
        a, pesan = shp_aff_cari("", toko=sid)
        hasil["aff"], hasil["aff_pesan"] = a, pesan
        if a:
            peta = {x["itemid"]: x["terjual"] for x in a}
            for p in prod:
                if peta.get(p["itemid"]) is not None:
                    p["terjual_resmi"] = peta[p["itemid"]]
    _shp_cache_simpan("toko", sid, hasil)
    return hasil


def _shp_fs_riwayat():
    ok, v = setelan_baca(SHP_FS_KUNCI)
    return (v if ok and isinstance(v, dict) and isinstance(v.get("hari"), dict) else {"hari": {}}), ok


def _shp_fs_ambil():
    """Semua item sesi Flash Sale yang sedang berjalan (± 50–120 item). → (sesi, items)"""
    ref = SHP_BASIS + "/flash_sale"
    t0 = time.time()
    r = _shp_get(SHP_BASIS + "/api/v4/flash_sale/get_all_sessions", bot=False, referer=ref, timeout=15)
    try:
        ss = ((r.json() or {}).get("data") or {}).get("sessions") or [] if r.status_code == 200 else []
    except ValueError:
        ss = []
    if not ss:
        _shp_catat("flash", False, t0, f"sesi kosong (HTTP {r.status_code})")
        raise ShopeeTutup(f"Daftar sesi Flash Sale tidak tersedia (HTTP {r.status_code})")
    kini = time.time()
    sesi = next((s for s in ss if (s.get("start_time") or 0) <= kini < (s.get("end_time") or 0)), ss[0])
    pid = sesi.get("promotionid")
    items, lihat = [], set()
    for off in (0, 50, 100):
        r = _shp_get(SHP_BASIS + f"/api/v4/flash_sale/flash_sale_get_items?offset={off}&limit=50&promotionid={pid}"
                     "&sort_soldout=true&need_personalize=true", bot=False, referer=ref, timeout=15)
        try:
            it = ((r.json() or {}).get("data") or {}).get("items") or [] if r.status_code == 200 else []
        except ValueError:
            it = []
        baru = [x for x in it if x.get("itemid") and x["itemid"] not in lihat]
        for x in baru:
            lihat.add(x["itemid"])
        items += baru
        if len(it) < 50 or not baru:
            break
    if not items:
        _shp_catat("flash", False, t0, "0 item")
        raise ShopeeTutup("Item Flash Sale tidak tersedia saat ini")
    _shp_catat("flash", True, t0, f"{len(items)} item")
    jadwal = [{"mulai": s.get("start_time"), "akhir": s.get("end_time"), "promotionid": s.get("promotionid"),
               "berjalan": (s.get("start_time") or 0) <= kini < (s.get("end_time") or 0)} for s in ss]
    return {"promotionid": pid, "mulai": sesi.get("start_time"), "akhir": sesi.get("end_time"), "jadwal": jadwal}, items


def shopee_flash(paksa=False):
    """Radar Flash Sale: terjual PASTI (historical_sold) + delta terhadap potret harian sebelumnya."""
    try:
        c = None if paksa else _shp_cache_ambil("flash", "-")
    except ShopeeTutup as e:
        return {"ok": False, "tutup": True, "error": str(e)}
    if c is not None:
        return dict(c, cache=True)
    try:
        sesi, items = _shp_fs_ambil()
    except ShopeeTutup as e:
        _shp_cache_gagal("flash", "-", str(e))
        return {"ok": False, "tutup": True, "error": str(e)}
    riw, siap = _shp_fs_riwayat()
    hari = _shp_tgl()
    lalu = sorted((d for d in riw["hari"] if d < hari), reverse=True)
    out = []
    for x in items:
        iid = str(x.get("itemid"))
        rc = (x.get("item_rating") or {}).get("rating_counts") or []
        fs_stok, stok = _int(x.get("flash_sale_stock")), _int(x.get("stock"))
        hs = _int(x.get("historical_sold"))
        d = {"itemid": iid, "shopid": str(x.get("shopid")), "nama": str(x.get("name") or "")[:180],
             "gambar": SHP_GAMBAR + str(x.get("image")) if x.get("image") else "",
             "harga": int(round((x.get("price") or 0) / 100000)) or None,
             "harga_coret": int(round((x.get("price_before_discount") or 0) / 100000)) or None,
             "diskon": _int(x.get("raw_discount")), "terjual": hs,
             "stok_fs": fs_stok, "laku_sesi": (fs_stok - stok) if (fs_stok is not None and stok is not None and fs_stok >= stok) else None,
             "rating": round(float((x.get("item_rating") or {}).get("rating_star") or 0), 2) or None,
             "ulasan": _int(rc[0]) if rc else None, "resmi": bool(x.get("is_shop_official")),
             "star": bool(x.get("is_shop_preferred_plus") or x.get("is_shop_preferred")),
             "url": SHP_BASIS + "/product/" + str(x.get("shopid")) + "/" + iid, "delta": None, "delta_hari": None}
        if hs is not None:
            for tgl in lalu:
                v = riw["hari"][tgl].get(iid)
                if v is not None:
                    d["delta"] = hs - v
                    try:
                        d["delta_hari"] = (datetime.strptime(hari, "%Y-%m-%d") - datetime.strptime(tgl, "%Y-%m-%d")).days
                    except ValueError:
                        d["delta_hari"] = None
                    break
        out.append(d)
    # simpan / gabungkan potret hari ini (angka terbaru per item)
    if siap:
        hr = dict(riw["hari"].get(hari) or {})
        for d in out:
            if d["terjual"] is not None:
                hr[d["itemid"]] = d["terjual"]
        riw["hari"][hari] = hr
        for tgl in sorted(riw["hari"])[:-SHP_FS_HARI]:
            riw["hari"].pop(tgl, None)
        setelan_tulis(SHP_FS_KUNCI, riw)
    n_delta = sum(1 for d in out if d["delta"] is not None)
    hasil = {"ok": True, "sesi": sesi, "produk": out, "waktu": _iso_kini(), "potret_hari": sorted(riw["hari"]),
             "n_delta": n_delta, "pembanding": next((t for t in lalu), None), "riwayat_siap": bool(siap)}
    _shp_cache_simpan("flash", "-", hasil)
    threading.Thread(target=shp_fs_kini_simpan, args=(hasil,), daemon=True, name="shopee-fs-kini").start()   # v1.5.4 dasbor
    return hasil


def shopee_potret_latar():
    """Dipanggil dari jadwal_tick: ambil potret Flash Sale 1× per hari tanpa memperlambat cron."""
    riw, siap = _shp_fs_riwayat()
    if not siap or _shp_tgl() in riw["hari"]:
        return
    threading.Thread(target=lambda: shopee_flash(paksa=True), daemon=True, name="shopee-potret").start()


# ======================= DASBOR SHOPEE v1.5.4 =======================
# Per akun di intai_setelan "shpakun:<email>" (terkompres, tanpa SQL baru):
#   {"v":1, "aktif": iso, "q": {"<kata>|<urut>": {"q","urut","lihat","riw":[potret harian ≤14],"prod":[40 teratas terbaru],"saran":[]}},
#    "toko": {shopid: {"nama","logo","lihat","live","voucher","t"}}}
# Cron (jadwal_tick) ikut memindai 3 kata kunci + 3 toko terakhir milik akun yang membuka Shopee/dasbor ≤14 hari terakhir.
SHP_AKUN_MAKS_Q, SHP_AKUN_HARIAN_Q = 6, 3
SHP_AKUN_MAKS_TOKO, SHP_AKUN_HARIAN_TOKO = 8, 3
SHP_AKUN_RIW, SHP_AKUN_AKTIF_HARI = 14, 14
SHP_FS_KINI = "shpfs_kini"           # ringkasan Radar Flash Sale terbaru (untuk dasbor, tanpa memanggil Shopee)
_shp_akun_lock = threading.Lock()


def _shp_akun_baca(email):
    try:
        ok, v = setelan_baca("shpakun:" + email)
    except Exception:
        ok, v = False, None
    v = _tk_buka(v) if ok else {}
    v = v if isinstance(v, dict) else {}
    v.setdefault("q", {})
    v.setdefault("toko", {})
    return v, ok


def _shp_akun_tulis(email, v):
    v["v"] = 1
    return setelan_tulis("shpakun:" + email, _tk_bungkus(v))


def _shp_potret(h):
    rk, pr = h.get("ringkas") or {}, h.get("produk") or []
    n = rk.get("n") or len(pr)
    lc = {k: c for k, c in (rk.get("lencana") or [])}
    tk = (rk.get("toko") or [None])[0]
    hg = rk.get("harga") or {}
    return {"t": h.get("waktu") or _iso_kini(), "hari": _shp_tgl(), "n": n,
            "harga": {k: hg.get(k) for k in ("min", "p25", "median", "p75", "max")} if hg else None,
            "diskon": (rk.get("diskon") or {}).get("persen_produk"), "diskon_rata": (rk.get("diskon") or {}).get("rata"),
            "mall": round(100 * lc.get("Mall", 0) / n) if n else 0, "toko_unik": rk.get("toko_unik"),
            "rating": rk.get("rating_rata"), "iklan": round(100 * sum(1 for p in pr if p.get("iklan")) / len(pr)) if pr else 0,
            "dom": {"shopid": str(tk.get("shopid")), "n": tk.get("n"), "contoh": str(tk.get("contoh") or "")[:70],
                    "lencana": tk.get("lencana")} if tk else None,
            "ids": [str(p.get("itemid")) for p in pr if p.get("itemid")][:40]}


def shp_akun_catat_cari(emails, h, oto=False):
    """Simpan potret hasil Scan Shopee ke akun (1 potret per hari WIB; yg terbaru menimpa hari yg sama)."""
    if not isinstance(h, dict) or not h.get("ok") or not h.get("produk"):
        return
    emails = [e for e in ([emails] if isinstance(emails, str) else (emails or [])) if e]
    for email in emails:
        try:
            with _shp_akun_lock:
                v, ok = _shp_akun_baca(email)
                if not ok and supa_aktif():
                    continue   # tabel setelan tak terbaca → jangan timpa dengan versi kosong
                kunci = str(h.get("q") or "").lower() + "|" + str(h.get("urut") or "laris")
                e = v["q"].get(kunci) or {"q": h.get("q"), "urut": h.get("urut") or "laris", "riw": []}
                p = _shp_potret(h)
                p["oto"] = bool(oto)
                riw = [x for x in (e.get("riw") or []) if isinstance(x, dict) and x.get("hari") != p["hari"]]
                e["riw"] = (riw + [p])[-SHP_AKUN_RIW:]
                e["prod"] = [[str(x.get("itemid")), str(x.get("shopid")), str(x.get("nama") or "")[:90], x.get("harga"),
                              x.get("gambar") or "", x.get("lencana") or ("Mall" if x.get("resmi") else "")]
                             for x in (h.get("produk") or [])[:40] if x.get("itemid")]
                e["saran"] = list(h.get("saran") or [])[:8]
                if not oto:
                    e["lihat"] = _iso_kini()
                    v["aktif"] = _iso_kini()
                e.setdefault("lihat", _iso_kini())
                v["q"][kunci] = e
                if len(v["q"]) > SHP_AKUN_MAKS_Q:
                    v["q"] = dict(sorted(v["q"].items(), key=lambda kv: kv[1].get("lihat") or "", reverse=True)[:SHP_AKUN_MAKS_Q])
                _shp_akun_tulis(email, v)
        except Exception:
            pass


def shp_akun_catat_toko(emails, h, oto=False):
    if not isinstance(h, dict) or not h.get("ok") or not h.get("toko"):
        return
    t = h["toko"]
    sid = str(t.get("shopid") or "")
    if not sid:
        return
    emails = [e for e in ([emails] if isinstance(emails, str) else (emails or [])) if e]
    for email in emails:
        try:
            with _shp_akun_lock:
                v, ok = _shp_akun_baca(email)
                if not ok and supa_aktif():
                    continue
                lama = v["toko"].get(sid) or {}
                v["toko"][sid] = {"nama": str(t.get("nama") or lama.get("nama") or "")[:80], "logo": str(t.get("logo") or "")[:300],
                                  "live": bool(t.get("live")), "voucher": list(t.get("voucher") or [])[:2], "t": h.get("waktu") or _iso_kini(),
                                  "lihat": lama.get("lihat") if oto and lama.get("lihat") else _iso_kini()}
                if not oto:
                    v["aktif"] = _iso_kini()
                if len(v["toko"]) > SHP_AKUN_MAKS_TOKO:
                    v["toko"] = dict(sorted(v["toko"].items(), key=lambda kv: kv[1].get("lihat") or "", reverse=True)[:SHP_AKUN_MAKS_TOKO])
                _shp_akun_tulis(email, v)
        except Exception:
            pass


def shp_fs_kini_simpan(hasil):
    """Dipanggil sesudah Radar Flash Sale segar → ringkasan kecil untuk dasbor."""
    try:
        pr = [p for p in (hasil.get("produk") or []) if p.get("terjual") is not None]
        ringkas = lambda p: {k: p.get(k) for k in ("itemid", "nama", "gambar", "harga", "harga_coret", "diskon", "terjual",
                                                    "delta", "delta_hari", "laku_sesi", "url", "resmi")}
        naik = sorted([p for p in pr if p.get("delta")], key=lambda p: -p["delta"])[:6]
        laris = sorted(pr, key=lambda p: -(p.get("terjual") or 0))[:6]
        setelan_tulis(SHP_FS_KINI, {"t": hasil.get("waktu"), "n": len(hasil.get("produk") or []), "akhir": (hasil.get("sesi") or {}).get("akhir"),
                                    "pembanding": hasil.get("pembanding"), "naik": [ringkas(p) for p in naik],
                                    "laris": [ringkas(p) for p in laris]})
    except Exception:
        pass


def _shp_toko_tumbuh(sid):
    try:
        ok, r = setelan_baca("shptoko:" + sid)
    except Exception:
        ok, r = False, None
    riw = sorted(x for x in (r if ok and isinstance(r, list) else []) if isinstance(x, list) and len(x) == 3)
    if not riw:
        return None
    b = riw[-1]

    def selisih(a):
        if not a:
            return None
        try:
            hr = (datetime.strptime(b[0], "%Y-%m-%d") - datetime.strptime(a[0], "%Y-%m-%d")).days
        except ValueError:
            return None
        return {"hari": hr, "sejak": a[0],
                "pengikut": (b[1] - a[1]) if (a[1] is not None and b[1] is not None) else None,
                "ulasan": (b[2] - a[2]) if (a[2] is not None and b[2] is not None) else None}
    try:
        batas = (datetime.strptime(b[0], "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    except ValueError:
        batas = ""
    a7 = next((x for x in reversed(riw[:-1]) if x[0] <= batas), riw[0] if len(riw) >= 2 else None)
    return {"pengikut": b[1], "ulasan": b[2], "hari": b[0], "titik": len(riw),
            "d1": selisih(riw[-2]) if len(riw) >= 2 else None, "d7": selisih(a7) if a7 and a7 is not b else None}


def _shp_ringkas_potret(p):
    return {k: p.get(k) for k in ("t", "hari", "n", "harga", "diskon", "diskon_rata", "mall", "toko_unik", "rating", "iklan", "dom", "oto")} if p else None


def shp_dasbor(email):
    v, ok = _shp_akun_baca(email)
    if not ok and supa_aktif():
        return {"ok": False, "error": "Data Shopee akunmu belum bisa dibaca. Coba muat ulang."}
    try:   # tandai akun aktif (cron hanya memindai akun aktif) — tulis maks 1×/hari
        if _tgl_wib(v.get("aktif")) != _tgl_wib() and (v["q"] or v["toko"]):
            with _shp_akun_lock:
                v2, ok2 = _shp_akun_baca(email)
                if ok2 or not supa_aktif():
                    v2["aktif"] = _iso_kini()
                    _shp_akun_tulis(email, v2)
    except Exception:
        pass
    kata = []
    urut_q = sorted(v["q"].values(), key=lambda e: e.get("lihat") or "", reverse=True)
    for i, e in enumerate(urut_q[:4]):
        riw = [x for x in (e.get("riw") or []) if isinstance(x, dict)]
        if not riw:
            continue
        a, b = riw[-1], (riw[-2] if len(riw) >= 2 else None)
        prod = e.get("prod") or []
        mk = lambda x, pos: {"itemid": x[0], "shopid": x[1], "nama": x[2], "harga": x[3], "gambar": x[4], "lencana": x[5] if len(x) > 5 else "",
                             "posisi": pos, "url": SHP_BASIS + "/product/" + str(x[1]) + "/" + str(x[0])}
        baru = []
        if b and b.get("ids"):
            lama = set(b["ids"])
            baru = [mk(x, j + 1) for j, x in enumerate(prod) if x[0] not in lama]
        keluar = len(set(b.get("ids") or []) - {x[0] for x in prod}) if b else 0
        kata.append({"q": e.get("q"), "urut": e.get("urut") or "laris", "harian": i < SHP_AKUN_HARIAN_Q,
                     "kini": _shp_ringkas_potret(a), "lalu": _shp_ringkas_potret(b),
                     "baru": baru[:6], "n_baru": len(baru), "n_keluar": keluar,
                     "teratas": [mk(x, j + 1) for j, x in enumerate(prod[:3])],
                     "seri": [[x.get("hari"), (x.get("harga") or {}).get("median")] for x in riw if (x.get("harga") or {}).get("median")],
                     "saran": (e.get("saran") or [])[:8]})
    toko_urut = sorted(v["toko"].items(), key=lambda kv: kv[1].get("lihat") or "", reverse=True)[:6]
    tumbuh = {}

    def ambil(sid):
        tumbuh[sid] = _shp_toko_tumbuh(sid)
    th = [threading.Thread(target=ambil, args=(sid,), daemon=True) for sid, _ in toko_urut]
    for t in th:
        t.start()
    for t in th:
        t.join(6)
    toko = [dict(t, shopid=sid, harian=j < SHP_AKUN_HARIAN_TOKO, tumbuh=tumbuh.get(sid)) for j, (sid, t) in enumerate(toko_urut)]
    fs = None
    try:
        ok_f, f = setelan_baca(SHP_FS_KINI)
        fs = f if ok_f and isinstance(f, dict) else None
    except Exception:
        fs = None
    jam = JADWAL.get("jam", 5)
    return {"ok": True, "kata": kata, "toko": toko, "flash": fs, "jam": jam,
            "jumlah": {"kata": len(v["q"]), "toko": len(v["toko"]),
                       "produk": sum((k["kini"] or {}).get("n") or 0 for k in kata)},
            "harian_maks": {"kata": SHP_AKUN_HARIAN_Q, "toko": SHP_AKUN_HARIAN_TOKO}}


def shp_item_cron():
    """{email: [item jadwal Shopee]} untuk akun yang aktif ≤14 hari. Dibaca sekali per hari (berat: semua akun)."""
    hari = _shp_tgl()
    c = JADWAL.get("shp_cache")
    if c and c[0] == hari:
        return c[1]
    out = {}
    try:
        ok, semua = setelan_awalan("shpakun:")
    except Exception:
        ok, semua = False, {}
    if ok:
        batas = datetime.now(timezone.utc) - timedelta(days=SHP_AKUN_AKTIF_HARI)
        batas_iso = batas.strftime("%Y-%m-%dT%H:%M:%SZ")
        for k, raw in (semua or {}).items():
            email = k.split(":", 1)[1] if ":" in k else ""
            v = _tk_buka(raw)
            if not email or not isinstance(v, dict) or str(v.get("aktif") or "") < batas_iso:
                continue
            its = []
            for e in sorted((v.get("q") or {}).values(), key=lambda e: e.get("lihat") or "", reverse=True)[:SHP_AKUN_HARIAN_Q]:
                q, urut = str(e.get("q") or ""), str(e.get("urut") or "laris")
                if q:
                    its.append({"k": "shopee:" + q.lower() + "|" + urut, "mode": "shopee-cari", "q": q, "urut": urut, "nama": "🛍 Shopee: " + q})
            for sid, t in sorted((v.get("toko") or {}).items(), key=lambda kv: kv[1].get("lihat") or "", reverse=True)[:SHP_AKUN_HARIAN_TOKO]:
                its.append({"k": "shopeetoko:" + sid, "mode": "shopee-toko", "q": sid, "nama": "🛍 Toko Shopee: " + (t.get("nama") or sid)})
            if its:   # Radar Flash Sale 1×/hari (kunci sama → digabung jadi 1 item) → "naik sejak kemarin" tetap terisi
                its.append({"k": "shopee:flash", "mode": "shopee-flash", "q": "-", "nama": "⚡ Radar Flash Sale Shopee"})
                out[email] = its
        JADWAL["shp_cache"] = (hari, out)
    return out


def shp_jalankan_item(it):
    """Pekerja jadwal: 1 item Shopee. Return dict mirip hasil scan (ok, jumlah, error)."""
    u = list(it.get("u") or [])
    if it.get("mode") == "shopee-flash":
        h = shopee_flash()
        return {"ok": bool(h.get("ok")), "jumlah": len(h.get("produk") or []), "error": h.get("error")}
    if it.get("mode") == "shopee-cari":
        h = shopee_cari(it["q"], it.get("urut") or "laris", None)
        if h.get("ok"):
            shp_akun_catat_cari(u, h, oto=True)
        return {"ok": bool(h.get("ok")), "jumlah": len(h.get("produk") or []), "error": h.get("error")}
    h = shopee_toko(it["q"], None)
    if h.get("ok"):
        shp_akun_catat_toko(u, h, oto=True)
    return {"ok": bool(h.get("ok")), "jumlah": len(h.get("produk") or []), "error": h.get("error")}


# ===== v1.5.5 AFFILIATOR & VIDEO (tab terpisah di Scan Shopee) =====
# Sumber: arsip iklan Meta INTAI (intai_iklan) yang berlink Shopee — tanpa tabel/SQL baru.
#   link `s.shopee.co.id/<kode>` dibuka (301) → /opaanlp/<shopid>/<itemid>?utm_medium=affiliates&utm_source=an_<id_affiliator>
#   `an_redir?origin_link=` = affiliate · utm_source=facebook-ca / utm_medium=seller = iklan resmi penjual · link langsung = penjual/brand
# Hasil buka short link disimpan permanen di setelan "shplink_peta" (terkompres) → tiap link dibuka sekali saja.
AFV_KOLOM = "archive_id,page_id,halaman,niche,aktif,mulai,umur,format,cta,hook,teks,link,thumb,terakhir_terlihat"
AFV_CACHE = {"t": 0.0, "versi": None, "rows": None, "ok": False}
kunci_afv = threading.Lock()
AFV_LINK = {"peta": None, "t_simpan": 0.0}
kunci_afv_link = threading.Lock()
AFV_LINK_MAKS = 6000          # entri short link yang diingat
AFV_BUKA_PER_PANGGIL = 40     # short link baru yang dibuka per permintaan (sisanya di permintaan berikut)
AFV_PENDEK = ("s.shopee.co.id", "shope.ee", "shp.ee", "id.shp.ee")
_RE_SHP_URL = re.compile(r"https?://(?:[a-z0-9-]+\.)*(?:shopee\.co\.id|shope\.ee|shp\.ee)/[^\s\"'<>)\]\\]+", re.I)
_AFV_PERINGKAT = {"aff": 3, "penjual": 2, "langsung": 1, "belum": 0, "mati": -1}


def _afv_baris():
    """Iklan arsip yang menyebut Shopee di link/teks. Return (ok, rows). Cache 5 menit / sampai arsip bertambah."""
    versi = ARSIP_STATUS.get("terakhir_waktu")
    with kunci_afv:
        if AFV_CACHE["rows"] is not None and AFV_CACHE["versi"] == versi and time.time() - AFV_CACHE["t"] < 300:
            return AFV_CACHE["ok"], AFV_CACHE["rows"]
        rows, ok = [], True
        try:
            if supa_aktif():
                filt = "&or=(link.ilike.*shope*,teks.ilike.*shope*)"
                for off in range(0, 20000, 1000):
                    o, r = supa("GET", "intai_iklan", params=f"?select={AFV_KOLOM}{filt}&order=archive_id.asc&limit=1000&offset={off}")
                    if not o or not isinstance(r, list):
                        ok = bool(rows)
                        break
                    rows += r
                    if len(r) < 1000:
                        break
            else:
                with kunci_arsip:
                    _muat_arsip_lokal()
                    rows = [dict(v) for v in ARSIP_LOKAL["iklan"].values()
                            if re.search(r"shope", str(v.get("link") or "") + " " + str(v.get("teks") or ""), re.I)]
        except Exception:
            ok = bool(rows)
        AFV_CACHE.update({"t": time.time(), "versi": versi, "rows": rows, "ok": ok})
        return ok, rows


def _afv_urls(r):
    teks = str(r.get("link") or "") + "\n" + str(r.get("teks") or "").replace("\\n", "\n")
    out = []
    for u in _RE_SHP_URL.findall(teks):
        u = u.rstrip(".,;:!?…*_~")
        if u not in out:
            out.append(u)
    return out[:6]


def _afv_urai(u, kedalaman=0):
    """URL Shopee → {jenis: aff|penjual|langsung|pendek, shopid, itemid, aff, sub, kode, host}."""
    try:
        p = urlparse(u)
    except Exception:
        return {"jenis": "langsung"}
    h = (p.hostname or "").lower()
    q = parse_qs(p.query)
    jalur = p.path or ""
    if "an_redir" in jalur:
        asal = q.get("origin_link", [""])[0]
        d = _afv_urai(unquote(asal), kedalaman + 1) if (asal and kedalaman < 2) else {}
        d["jenis"] = "aff"
        d["aff"] = d.get("aff") or ("an_" + q["affiliate_id"][0] if q.get("affiliate_id") else "")
        return d
    kode = jalur.strip("/")
    if h in AFV_PENDEK and kode and "/" not in kode:
        return {"jenis": "pendek", "kode": kode[:40], "host": h}
    d = {"jenis": "langsung"}
    m = re.search(r"/(?:product|opaanlp)/(\d+)/(\d+)", jalur) or re.search(r"-i\.(\d+)\.(\d+)", jalur)
    if m:
        d.update(shopid=m.group(1), itemid=m.group(2))
    else:
        m2 = re.search(r"/shop/(\d+)", jalur)
        if m2:
            d["shopid"] = m2.group(1)
    med = (q.get("utm_medium", [""])[0] or "").lower()
    src = q.get("utm_source", [""])[0] or ""
    mmp = q.get("mmp_pid", [""])[0] or ""
    if med == "affiliates" or src.startswith("an_") or mmp.startswith("an_") or "/opaanlp/" in jalur:
        d.update(jenis="aff", aff=(src if src.startswith("an_") else mmp if mmp.startswith("an_") else ""),
                 sub=(q.get("utm_content", [""])[0] or "").strip("-")[:60])
    elif src == "facebook-ca" or med == "seller":
        d["jenis"] = "penjual"
    return d


_RE_AFV_KREDIT = re.compile(r"^\W*(credit|cr\b|cr\s*:|video\s*by|vid\s*by|by\s*(tiktok|tt|ig|instagram|yt|youtube)\b|sumber|source|repost|ig\s*:|tiktok\s*:|tt\s*:|instagram\s*:|yt\s*:|youtube\s*:|fb\s*:|facebook\s*:|ig\s*@|tiktok\s*@)", re.I)
_RE_AFV_AJAKAN = re.compile(r"^\W*(cek|beli|link|promo|klik|order|checkout|dapatkan|dapetin|ambil|grab|pesan|shop|langsung|buruan|yuk|info)\b.{0,40}"
                            r"(di ?sini|disini|👇|👉|⬇|bawah|keranjang|shopee|kak)", re.I)


# v1.5.5b: kalimat pendek berisi ajakan/penunjuk di MANA PUN ('belinya disini👉', 'Yang di spill', 'Pembelian Klik 👉') bukan hook
_RE_AFV_CTA_DALAM = re.compile(r"(👇|👉|☝|⬇|⤵|🔗|🛒|\bdi ?sini\b|\bdisini\b|\bspill\w*|\bklik\w*|\bclick\b|\blink\w*|\bkeranjang\b|"
                               r"\bcheck ?out\b|\bco\b|\border\s+(sekarang|di)\b|\bbeli(nya)?\s+(sekarang|di)\b|\bbelanja\s+(sekarang|di)\b|\bpembelian\b|\bcek\b|\bcekidot\b|\bbio\b|"
                               r"\bkomen\w*|\bcomment\b|\bdm\b|\bwa\b|\bwhatsapp\b|➡)", re.I)
# ajakan kuat → ditolak berapa pun panjangnya
_RE_AFV_CTA_KUAT = re.compile(r"(\bklik\w*\s+(link|keranjang|tombol|di ?sini|disini|tautan)|\bkeranjang\s+(kuning|oranye|orange|di\s+(pojok|bawah|kiri|kanan))|"
                              r"\bpojok\s+kiri\s+bawah|\blink\s+(di|ada\s+di|produk|pembelian|dibawah|di\s?bawah|bio)\b|\bbeli(nya)?\s+(di ?sini|disini|di\s+link)|"
                              r"\bcek\s+(keranjang|link)|\bcara\s+(pesan|order|beli)\w*)", re.I)


_RE_AFV_EKOR_CTA = re.compile(r"\s*\b(beli(nya)?|cek|order|klik|pesan|belanja|checkout|dapatkan)\b[^.!?]{0,20}?(di ?sini|disini|di\s+link|link\w*)\W*$", re.I)
_RE_AFV_EKOR_PANAH = re.compile(r"[\s:=>\-👇👉👆☝⬇⤵➡🫵\U0001F3FB-\U0001F3FF\uFE0F]+$")


def _afv_bersih(b):
    """Pangkas ekor ajakan ('Tirai Cantik Beli Disini⤵️' → 'Tirai Cantik'). Return (teks, dipangkas)."""
    c = _RE_AFV_EKOR_CTA.sub("", b)
    dipangkas = c != b
    c2 = _RE_AFV_EKOR_PANAH.sub("", c).strip() if dipangkas else c.strip()
    return (c2 or b), dipangkas


def _afv_bukan_hook(b, min_kata=2):
    """True bila baris lebih mirip ajakan klik / kredit / tagar / harga daripada kalimat pembuka."""
    if "{{" in b or "}}" in b:   # placeholder katalog ({{product.name}})
        return True
    kata = re.findall(r"[^\W\d_]{3,}", b)
    if len(kata) < min_kata:
        return True
    tok = b.split()
    if tok and sum(1 for t in tok if t.startswith("#") or t.startswith("@")) * 2 >= len(tok):
        return True
    if _RE_AFV_KREDIT.search(b):
        return True
    if _RE_AFV_AJAKAN.search(b) and len(b) < 60:
        return True
    if _RE_AFV_CTA_KUAT.search(b):
        return True
    # kalimat pendek (≤ 6 kata bermakna) yang memuat penunjuk/ajakan di mana pun
    if len(kata) <= 6 and len(b) < 50 and _RE_AFV_CTA_DALAM.search(b):
        return True
    return False


def _afv_potong(b, n=140):
    """Potong di batas kata + elipsis supaya hook tidak berakhir di tengah kata ('bebera')."""
    b = str(b or "").strip()
    if len(b) <= n:
        return b
    c = b[:n]
    sp = c.rfind(" ")
    if sp >= n * 0.6:
        c = c[:sp]
    return c.rstrip(" ,;:-–—") + "…"


def _afv_hook_dari(teks, cadangan=""):
    """Baris pembuka yang layak jadi hook: bukan link, bukan ajakan klik ('Cek di sini 👇'), cukup panjang."""
    for baris in str(teks or "").replace("\\n", "\n").splitlines():
        b = re.sub(r"https?://\S+", "", baris)
        b = re.sub(r"^\s*mall\s*inline\s*badge\s*", "", b, flags=re.I).strip(" -–—•*|")   # sisa label kartu katalog
        b, dipangkas = _afv_bersih(b)
        if len(b) < 10 or len(re.sub(r"[\W\d_]+", "", b)) < 8:
            continue
        if _afv_bukan_hook(b, 3 if dipangkas else 2):
            continue
        return _afv_potong(b)
    c = re.sub(r"https?://\S+", "", str(cadangan or "")).strip()
    return _afv_potong(c) if len(c) >= 12 and not _afv_bukan_hook(c) else ""


def _afv_peta():
    with kunci_afv_link:
        if AFV_LINK["peta"] is None:
            try:
                ok, v = setelan_baca("shplink_peta")
                AFV_LINK["peta"] = _tk_buka(v) if (ok and v) else {}
            except Exception:
                AFV_LINK["peta"] = {}
        return AFV_LINK["peta"]


def _afv_buka_satu(kode, host):
    """Buka 1 short link (tanpa mengikuti redirect). Return ringkasan atau None (gagal → dicoba lain kali)."""
    try:
        r = requests.get(f"https://{host}/{quote(kode)}", allow_redirects=False, timeout=5,
                         headers={"User-Agent": SHP_UA_WEB, "Accept-Language": "id-ID,id;q=0.9"})
        loc = r.headers.get("location") or ""
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            d = _afv_urai(urljoin(f"https://{host}/", loc))
            if d.get("jenis") == "pendek":
                return None
            return {"j": d.get("jenis") or "langsung", "s": d.get("shopid", ""), "i": d.get("itemid", ""),
                    "a": d.get("aff", ""), "u": d.get("sub", ""), "t": int(time.time())}
        if r.status_code in (404, 410):
            return {"j": "mati", "t": int(time.time())}
    except Exception:
        pass
    return None


def _afv_simpan_peta():
    try:
        with kunci_afv_link:
            peta = dict(AFV_LINK["peta"] or {})
        if len(peta) > AFV_LINK_MAKS:
            peta = dict(sorted(peta.items(), key=lambda kv: -int((kv[1] or {}).get("t") or 0))[:AFV_LINK_MAKS])
            with kunci_afv_link:
                AFV_LINK["peta"] = peta
        setelan_tulis("shplink_peta", _tk_bungkus(peta))
        AFV_LINK["t_simpan"] = time.time()
    except Exception:
        pass


def afv_analisis(q):
    """Tab Affiliator & Video: produk yang diiklankan affiliator di Meta (A1), kepadatan affiliator (A2), bank hook (V1)."""
    q = re.sub(r"\s+", " ", str(q or "")).strip()[:80]
    ok, rows = _afv_baris()
    try:   # v1.5.6: + iklan dari Scan Affiliator (disimpan terpisah); arsip menang bila archive_id sama
        sc = _afvs_muat()["rows"]
        if sc:
            ada = {str(r.get("archive_id")) for r in rows}
            rows = list(rows) + [r for a_, r in sc.items() if a_ not in ada]
    except Exception:
        pass
    tok = [t for t in re.split(r"\s+", _norm(q)) if len(t) >= 2] if q else []

    def cocok(r):
        if not tok:
            return True
        hay = _norm(" ".join(str(r.get(k) or "") for k in ("niche", "hook", "teks", "halaman")))
        return all(t in hay for t in tok)

    pilih = [r for r in rows if cocok(r)]
    peta = _afv_peta()
    per, perlu = [], {}
    for r in pilih:
        ur = [_afv_urai(u) for u in _afv_urls(r)]
        if not ur:
            continue
        per.append((r, ur))
        for d in ur:
            if d.get("jenis") == "pendek" and d["kode"] not in peta:
                perlu.setdefault(d["kode"], d["host"])
    dibuka = 0
    if perlu:
        antre = list(perlu.items())[:AFV_BUKA_PER_PANGGIL]
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                hasil = list(ex.map(lambda kh: (kh[0], _afv_buka_satu(kh[0], kh[1])), antre))
        except Exception:
            hasil = []
        baru = {k: v for k, v in hasil if v}
        if baru:
            with kunci_afv_link:
                AFV_LINK["peta"].update(baru)
            dibuka = len(baru)
            threading.Thread(target=_afv_simpan_peta, daemon=True, name="afv-peta").start()
    hitung = {"aff": 0, "penjual": 0, "langsung": 0, "belum": 0}
    produk, hook = {}, {}
    peran_ai = _ai_cache("ai_peran") if ai_aktif() else {}   # v1.5.7 putusan AI: link langsung dari affiliator?
    calon_ai, ai_aff, ai_jual = [], 0, 0
    for r, ur in per:
        jenis, tgt = None, {}
        for d in ur:
            if d.get("jenis") == "pendek":
                m = peta.get(d["kode"])
                dd = {"jenis": "belum"} if not m else {"jenis": m.get("j"), "shopid": m.get("s"), "itemid": m.get("i"),
                                                        "aff": m.get("a"), "sub": m.get("u")}
            else:
                dd = d
            if jenis is None or _AFV_PERINGKAT.get(dd.get("jenis"), -1) > _AFV_PERINGKAT.get(jenis, -1):
                jenis, tgt = dd.get("jenis"), dd
        if jenis in hitung:
            hitung[jenis] += 1
        umur = _int(r.get("umur"), 0) or 0
        aktif = r.get("aktif") is not False
        hk = _afv_hook_dari(r.get("teks"), r.get("hook"))
        if len(hk) >= 12:
            kh = re.sub(r"\W+", " ", _norm(hk)).strip()[:80]
            lama = hook.get(kh)
            calon = {"hook": hk[:140], "halaman": r.get("halaman"), "umur": umur, "aktif": aktif,
                     "aid": r.get("archive_id"), "jenis": jenis}
            if not lama or (aktif, umur) > (lama["aktif"], lama["umur"]):
                hook[kh] = calon
        dugaan = False
        if jenis == "langsung" and r.get("archive_id"):   # v1.5.7
            pa = peran_ai.get(str(r.get("archive_id")))
            if pa is None:
                if len(calon_ai) < 60:
                    calon_ai.append((str(r.get("archive_id")), " | ".join(x for x in [
                        "Halaman: " + str(r.get("halaman") or ""), "Teks: " + re.sub(r"\s+", " ", str(r.get("teks") or r.get("hook") or ""))[:260],
                        "Link: " + (_afv_urls(r)[0][:90] if _afv_urls(r) else "")] if x)))
            elif pa.get("p") == "affiliator" and (pa.get("y") or 0) >= 80:
                ai_aff += 1
                dugaan = True
            elif pa.get("p") == "penjual" and (pa.get("y") or 0) >= 70:
                ai_jual += 1
        if not (jenis == "aff" or dugaan) or not tgt.get("itemid"):
            continue
        k = f"{tgt.get('shopid')}.{tgt.get('itemid')}"
        p = produk.setdefault(k, {"shopid": tgt.get("shopid"), "itemid": tgt.get("itemid"), "_aff": set(), "_hal": {},
                                  "iklan": 0, "aktif": 0, "umur_maks": 0, "_niche": set(), "_hook": [], "_sub": set(),
                                  "aid": [], "thumb": None, "_teks": "", "_scan": 0, "_ai": 0})
        p["iklan"] += 1
        p["_ai"] += 1 if dugaan else 0
        p["_scan"] += 1 if r.get("_scan") else 0
        p["aktif"] += 1 if aktif else 0
        p["umur_maks"] = max(p["umur_maks"], umur)
        p["_aff"].add(tgt.get("aff") or ("hal:" + str(r.get("page_id") or r.get("halaman") or "")))
        if r.get("halaman"):
            p["_hal"][r.get("halaman")] = max(p["_hal"].get(r.get("halaman"), 0), umur)
        if r.get("niche"):
            p["_niche"].add(r.get("niche"))
        if hk and hk not in p["_hook"]:
            p["_hook"].append(hk[:140])
        if tgt.get("sub"):
            p["_sub"].add(tgt.get("sub"))
        if r.get("archive_id") and len(p["aid"]) < 6:
            p["aid"].append(r.get("archive_id"))
        if not p["thumb"] and str(r.get("thumb") or "").startswith("http"):
            p["thumb"] = r.get("thumb")
        if len(str(r.get("teks") or "")) > len(p["_teks"]):
            p["_teks"] = str(r.get("teks") or "")[:300]
    daftar = []
    for p in produk.values():
        n = len([a for a in p["_aff"] if a and a != "hal:"]) or 1
        daftar.append({
            "shopid": p["shopid"], "itemid": p["itemid"],
            "url": SHP_BASIS + "/product/" + str(p["shopid"]) + "/" + str(p["itemid"]),
            "affiliator": n, "kepadatan": ("sepi" if n <= 1 else "sedang" if n <= 4 else "padat"),
            "iklan": p["iklan"], "aktif": p["aktif"], "umur_maks": p["umur_maks"],
            "halaman": [h for h, _ in sorted(p["_hal"].items(), key=lambda x: -x[1])][:5],
            "niche": sorted(p["_niche"])[:4], "hook": p["_hook"][:4], "tag": sorted(p["_sub"])[:4],
            "aid": p["aid"], "thumb": p["thumb"], "teks": p["_teks"],
            "dari_scan": p["_scan"], "dari_ai": p["_ai"],
        })
    daftar.sort(key=lambda x: (-x["affiliator"], -x["aktif"], -x["umur_maks"], -x["iklan"]))
    AFV_AI_CALON[q] = calon_ai
    while len(AFV_AI_CALON) > 100:
        AFV_AI_CALON.pop(next(iter(AFV_AI_CALON)))
    hooks = sorted(hook.values(), key=lambda x: (not x["aktif"], -x["umur"]))[:14]
    sisa = len([1 for _, ur in per for d in ur if d.get("jenis") == "pendek" and d["kode"] not in peta])
    return {"ok": True, "arsip_ok": bool(ok), "q": q, "produk": daftar[:40], "jumlah_produk": len(daftar),
            "hook": hooks, "hitung": hitung, "iklan_shopee": len(per), "arsip_shopee": len(rows),
            "dibuka": dibuka, "belum_dibuka": sisa, "jam": _iso_kini(),
            "iklan_scan": sum(1 for r, _ in per if r.get("_scan")), "scan_kw": afvs_status(q) if q else None,
            "ai_aktif": ai_aktif(), "ai_peran": {"affiliator": ai_aff, "penjual": ai_jual, "belum": len(calon_ai)}}


# ===== v1.5.6 SCAN AFFILIATOR (Meta) — hasil DISIMPAN TERPISAH dari arsip Scan Meta =====
# Kenapa terpisah: Scan Meta dirancang untuk penjual (iklan "Lepas" dibuang, kata kunci polos). Iklan affiliator
# (halaman "Pusat Promo", "Rekomendasi Belanja") sering berlabel Lepas → tidak masuk arsip. Scan ini memakai mesin
# pengambil yang SAMA (kumpul_iklan_v2: jatah Meta, rehat, antrean, batas per jam tetap berlaku), tapi:
#   · kata kunci bergaya affiliator ("<kw> shopee", "racun shopee <kw>", "spill <kw>") + kata polos,
#   · satu-satunya saringan = menyebut Shopee (link/teks/kartu) + memuat kata kunci,
#   · disimpan di intai_setelan["afvscan"] (terkompres) → TIDAK mengotori Top 10 / Tren / Pesaing niche penjual.
AFVS = {"data": None, "t_simpan": 0.0}
kunci_afvs = threading.Lock()
AFVS_MAKS_BARIS = 2500        # total iklan affiliator yang disimpan (kata kunci terlama dibuang dulu)
AFVS_SEGAR_DTK = 6 * 3600     # kata yang sama discan ulang paling cepat 6 jam (kecuali admin)


def _afvs_muat():
    with kunci_afvs:
        if AFVS["data"] is None:
            try:
                ok, v = setelan_baca("afvscan")
                d = _tk_buka(v) if (ok and v) else {}
            except Exception:
                d = {}
            AFVS["data"] = {"kw": dict(d.get("kw") or {}), "rows": dict(d.get("rows") or {})}
        return AFVS["data"]


def _afvs_simpan():
    try:
        with kunci_afvs:
            d = AFVS["data"] or {"kw": {}, "rows": {}}
            if len(d["rows"]) > AFVS_MAKS_BARIS:   # buang kata kunci terlama sampai muat
                for kw, _ in sorted(d["kw"].items(), key=lambda kv: kv[1].get("t") or ""):
                    if len(d["rows"]) <= AFVS_MAKS_BARIS:
                        break
                    for aid in d["kw"][kw].get("aid") or []:
                        r = d["rows"].get(aid)
                        if r and r.get("niche") == kw:
                            d["rows"].pop(aid, None)
                    d["kw"].pop(kw, None)
            salinan = {"kw": dict(d["kw"]), "rows": dict(d["rows"])}
        setelan_tulis("afvscan", _tk_bungkus(salinan))
        AFVS["t_simpan"] = time.time()
    except Exception:
        pass


def _afvs_kunci(q):
    return re.sub(r"\s+", " ", _norm(q)).strip()[:60]


def _afvs_baris_dari_mentah(o, kw, tok):
    """Iklan mentah Meta → baris berformat arsip (kolom AFV_KOLOM). None bila tidak menyebut Shopee / kata kunci."""
    snap = o.get("snapshot") or {}
    bag, links = [], []
    for k in ("title", "body", "caption", "link_description"):
        t = mesin._teks(snap, k)
        if t and t not in bag:
            bag.append(t)
    if snap.get("link_url"):
        links.append(str(snap.get("link_url")))
    for c in (snap.get("cards") or [])[:10]:
        if not isinstance(c, dict):
            continue
        if c.get("link_url") and str(c["link_url"]) not in links:
            links.append(str(c["link_url"]))
        for k in ("title", "body"):
            t = c.get(k)
            t = t.get("text") if isinstance(t, dict) else t
            if t and str(t) not in bag and len(bag) < 8:
                bag.append(str(t))
    teks = "\n".join(bag)
    link = " ".join(links)
    if not re.search(r"shope", link + " " + teks, re.I):
        return None
    hay = _norm(teks + " " + str(o.get("page_name") or "") + " " + link)
    if tok and not all(t in hay for t in tok):
        return None
    aid = str(o.get("ad_archive_id") or "")
    if not aid:
        return None
    umur, mulai = 0, None
    try:
        st = int(o.get("start_date") or 0)
        if st:
            mulai = datetime.fromtimestamp(st, tz=timezone.utc).strftime("%Y-%m-%d")
            umur = max(0, (datetime.now(timezone.utc) - datetime.fromtimestamp(st, tz=timezone.utc)).days)
    except Exception:
        pass
    return {"archive_id": aid, "page_id": str(o.get("page_id") or ""), "halaman": o.get("page_name") or "",
            "niche": kw, "aktif": bool(o.get("is_active")), "mulai": mulai, "umur": umur,
            "hook": _hook(teks), "teks": teks[:700], "link": link[:600], "thumb": None,
            "terakhir_terlihat": _iso_kini(), "_scan": True}


def afvs_status(q):
    kw = _afvs_kunci(q)
    if not kw:
        return None
    d = _afvs_muat()
    m = d["kw"].get(kw)
    return {"kw": kw, "t": m.get("t"), "iklan_meta": m.get("meta"), "shopee": len(m.get("aid") or [])} if m else None


def afv_scan(q, email):
    """Scan iklan affiliator untuk 1 kata kunci (dipanggil di bawah antrean scan)."""
    kw = _afvs_kunci(q)
    if len(kw) < 2:
        return {"ok": False, "error": "Ketik kata kunci produk minimal 2 huruf."}
    d = _afvs_muat()
    lama = d["kw"].get(kw)
    if lama and not is_admin(email) and time.time() - _tk_epoch(lama.get("t")) < AFVS_SEGAR_DTK:
        r = afv_analisis(q)
        r["scan"] = {"cache": True, "t": lama.get("t"), "iklan_meta": lama.get("meta"), "shopee": len(lama.get("aid") or []),
                     "baru": 0, "pesan": "Kata ini sudah discan dalam 6 jam terakhir — hasilnya dipakai ulang supaya Meta tidak memblokir INTAI."}
        return r
    tok = [t for t in kw.split() if len(t) >= 2]
    utama = kw + " shopee"
    senada = [kw, "racun shopee " + kw, "spill " + kw, kw + " link"]
    t0 = time.time()
    try:
        mentah, info = mesin.kumpul_iklan_v2(utama, "ID", senada, batas_waktu=40)
    except mesin.MetaMenahan:
        menit = max(1, round(mesin.rem_status()["rehat_detik"] / 60))
        jejak_mesin("ditahan", "afv:" + kw, ket=f"rehat {menit} mnt")
        return {"ok": False, "ditahan": True,
                "error": f"Meta sedang membatasi pencarian dari server INTAI (bukan kesalahan kamu). Coba lagi sekitar {menit} menit lagi."}
    except Exception as e:
        return {"ok": False, "error": "Scan affiliator gagal: " + str(e)[:100]}
    rows = []
    for o in mentah or []:
        b = _afvs_baris_dari_mentah(o, kw, tok)
        if b:
            rows.append(b)
    jejak_mesin("afv", kw, len(mentah or []), round(time.time() - t0, 1), f"{len(rows)} berlink Shopee")
    _, arsip = _afv_baris()
    di_arsip = {str(r.get("archive_id")) for r in arsip}
    baru = sum(1 for b in rows if b["archive_id"] not in di_arsip)
    with kunci_afvs:
        for b in rows:
            d["rows"][b["archive_id"]] = b
        d["kw"][kw] = {"t": _iso_kini(), "meta": len(mentah or []), "aid": [b["archive_id"] for b in rows][:400]}
    threading.Thread(target=_afvs_simpan, daemon=True, name="afvs-simpan").start()
    r = afv_analisis(q)
    r["scan"] = {"cache": bool((info or {}).get("cache")), "t": d["kw"][kw]["t"], "iklan_meta": len(mentah or []),
                 "shopee": len(rows), "baru": baru, "detik": round(time.time() - t0, 1)}
    return r


def afv_scan_antre(q, email):
    """Sama seperti scan_dengan_antre: 1 scan per akun, ikut batas scan per jam, antre di kunci_scan."""
    admin = is_admin(email)
    with _kunci_scan_jalan:
        if email in SCAN_JALAN:
            return {"ok": False, "error": "Scan kamu sebelumnya masih berjalan. Tunggu sampai selesai, lalu coba lagi."}
        if not admin and not _boleh_lupa("scan:" + email, SCAN_PER_JAM, 3600):
            return {"ok": False, "error": f"Batas {SCAN_PER_JAM} scan per jam tercapai. Coba lagi nanti supaya Meta tidak memblokir INTAI."}
        SCAN_JALAN.add(email)
    try:
        if not kunci_scan.acquire(timeout=SCAN_ANTRE_DTK):
            return {"ok": False, "error": "Antrean scan sedang penuh (scan otomatis/pengguna lain). Coba lagi beberapa menit lagi."}
        try:
            return afv_scan(q, email)
        except Exception as e:
            return {"ok": False, "error": "Scan affiliator gagal: " + str(e)[:200]}
        finally:
            kunci_scan.release()
    finally:
        with _kunci_scan_jalan:
            SCAN_JALAN.discard(email)


# ===== v1.5.6 SHOPEE VIDEO (tagar) — halaman SEO publik sv.shopee.co.id/web/hashtag/<nama>, tanpa login =====
# Isi per tagar: statistik tagar (total tayangan & jumlah video) + ±30 video (terbaru utk tagar biasa) lengkap dengan
# penonton/suka/komentar, kreator, caption, dan PRODUK YANG DITAG (nama, harga, terjual). Tidak resmi → gagal = tenang.
SV_BASIS = "https://sv.shopee.co.id"
SV_CACHE = OrderedDict()
kunci_sv = threading.Lock()
SV_CACHE_DTK = 30 * 60
SV_UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
_RE_SV_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _sv_tag_nama(s):
    return re.sub(r"[^0-9a-z]", "", _norm(s))[:40]


def _sv_gbr(img):
    img = str(img or "").strip()
    if not img:
        return ""
    if img.startswith("http"):
        return img
    return "https://down-id.img.susercontent.com/file/" + re.sub(r"_tn$", "", img)


def _sv_ambil_tagar(nama):
    """1 halaman tagar → dict ringkas, atau {'galat': ...}. Cache 30 menit per tagar."""
    nama = _sv_tag_nama(nama)
    if len(nama) < 2:
        return {"galat": "nama tagar kosong"}
    with kunci_sv:
        c = SV_CACHE.get(nama)
        if c and time.time() - c[0] < SV_CACHE_DTK:
            return c[1]
    try:
        r = requests.get(f"{SV_BASIS}/web/hashtag/{quote(nama)}", timeout=12,
                         headers={"User-Agent": SV_UA, "Accept-Language": "id-ID,id;q=0.9", "Accept": "text/html"})
        if r.status_code != 200:
            return {"galat": f"HTTP {r.status_code}"}
        m = _RE_SV_DATA.search(r.text)
        if not m:
            return {"galat": "halaman tanpa data (mungkin diblokir)"}
        pp = (json.loads(m.group(1)).get("props") or {}).get("pageProps") or {}
        hs = pp.get("hashtag") or {}
        det = ((hs.get("detail") or {}).get("hashtag")) or {}
        lst = ((hs.get("hashtagVideos") or {}).get("list")) or []
    except Exception as e:
        return {"galat": str(e)[:80]}
    video = []
    for x in lst[:40]:
        me, co = x.get("meta") or {}, x.get("content") or {}
        ci = me.get("countInfo") or {}
        pr = co.get("products") or {}
        produk = []
        anc = pr.get("anchorProduct") if isinstance(pr, dict) else None
        lst_p = (pr.get("itemList") if isinstance(pr, dict) else None) or []
        for p in ([anc] if anc else []) + list(lst_p):
            if not isinstance(p, dict) or not p.get("itemId"):
                continue
            if any(str(q_["itemid"]) == str(p.get("itemId")) for q_ in produk):
                continue
            dp = p.get("itemCardDisplayPrice") or {}
            harga = dp.get("price") or p.get("price")
            coret = dp.get("strikethroughPrice") or p.get("priceBeforeDiscount")
            produk.append({"shopid": p.get("shopId"), "itemid": p.get("itemId"), "nama": p.get("name") or "",
                           "harga": int(harga / 100000) if harga else None,
                           "harga_coret": int(coret / 100000) if coret and coret > 0 else None,
                           "diskon": dp.get("discount") or None, "terjual": p.get("sold"),
                           "gambar": _sv_gbr(p.get("image"))})
        cap = str(co.get("caption") or "")
        vd = co.get("video") or {}
        video.append({"id": me.get("postId"), "kreator": me.get("userName") or "", "nama": me.get("shopeeNickName") or "",
                      "centang": bool(me.get("isVerified")), "tayang": _int(ci.get("views"), 0) or 0,
                      "suka": _int(ci.get("likes"), 0) or 0, "komen": _int(ci.get("comments"), 0) or 0,
                      "unggah": int((me.get("ctime") or 0) / 1000) or None, "caption": cap[:400],
                      "durasi": round((vd.get("duration") or 0) / 1000) or None, "cover": vd.get("cover") or "",
                      "url": f"{SV_BASIS}/share-video/{quote(str(me.get('postId') or ''), safe='')}",
                      "produk": produk[:6], "jml_produk": (pr.get("count") if isinstance(pr, dict) else None) or len(produk),
                      "tagar": [t.lower() for t in re.findall(r"#([0-9A-Za-z_]{2,40})", cap)][:15]})
    hasil = {"nama": det.get("content") or nama, "id": det.get("hashtagId"), "resmi": bool(det.get("isOfficial")),
             "tayang": _int(det.get("viewCount"), 0) or 0, "video_total": _int(det.get("postCount"), 0) or 0,
             "video": video, "jam": _iso_kini()}
    with kunci_sv:
        SV_CACHE[nama] = (time.time(), hasil)
        while len(SV_CACHE) > 200:
            SV_CACHE.popitem(last=False)
    return hasil


def sv_tagar(q):
    """Shopee Video untuk 1 kata kunci: tagar utama + 2 tagar terkait yang paling sering muncul di caption video."""
    q = re.sub(r"\s+", " ", str(q or "")).strip()[:60]
    utama = _sv_tag_nama(q)
    if len(utama) < 2:
        return {"ok": False, "error": "Ketik kata kunci produk minimal 2 huruf."}
    t0 = time.time()
    tg = [_sv_ambil_tagar(utama)]
    if tg[0].get("galat"):
        return {"ok": False, "q": q, "error": "Shopee Video tidak bisa dibaca saat ini (" + tg[0]["galat"] + "). Coba lagi nanti."}
    tok = [t for t in _norm(q).split() if len(t) >= 3] or [utama[:4]]
    hit = {}
    for v in tg[0]["video"]:
        for t in set(v["tagar"]):
            if t != utama and any(k in t for k in tok):
                hit[t] = hit.get(t, 0) + 1
    terkait = [t for t, n in sorted(hit.items(), key=lambda kv: -kv[1]) if n >= 2][:6]
    if terkait:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            for h in ex.map(_sv_ambil_tagar, terkait[:2]):
                if not h.get("galat") and h.get("video"):
                    tg.append(h)
    video, lihat = [], set()
    for h in tg:
        for v in h["video"]:
            if v["id"] and v["id"] not in lihat:
                lihat.add(v["id"])
                video.append(dict(v, dari=h["nama"]))
    produk = {}
    for v in video:
        for p in v["produk"]:
            k = f"{p['shopid']}.{p['itemid']}"
            a = produk.setdefault(k, dict(p, video=0, kreator=set(), tayang=0, suka=0,
                                          url=f"{SHP_BASIS}/product/{p['shopid']}/{p['itemid']}"))
            if not a.get("nama") and p.get("nama"):   # produk tambahan hanya ber-ID → isi dari video yg menjadikannya produk utama
                a.update({k_: p.get(k_) for k_ in ("nama", "harga", "harga_coret", "diskon", "terjual", "gambar")})
            a["video"] += 1
            a["kreator"].add(v["kreator"])
            a["tayang"] += v["tayang"]
            a["suka"] += v["suka"]
    pr = []
    for a in produk.values():
        a = dict(a, kreator=len(a["kreator"]))
        pr.append(a)
    pr.sort(key=lambda a: (-a["kreator"], not a.get("nama"), -a["tayang"], -(a.get("terjual") or 0)))
    wkt = [v["unggah"] for v in video if v["unggah"]]

    def laju(h):   # video per jam di sampel tagar (tagar biasa = unggahan terbaru) → ukuran kepadatan kreator
        w = sorted(v["unggah"] for v in h["video"] if v["unggah"])
        if len(w) < 5 or h["resmi"]:
            return None
        jam = (w[-1] - w[0]) / 3600
        return round(len(w) / jam, 1) if jam >= 0.05 else None
    return {"ok": True, "q": q,
            "tagar": [{"nama": h["nama"], "resmi": h["resmi"], "tayang": h["tayang"], "video_total": h["video_total"],
                       "rata": round(h["tayang"] / h["video_total"]) if h["video_total"] else None,
                       "sampel": len(h["video"]), "per_jam": laju(h)} for h in tg],
            "terkait": terkait, "video": sorted(video, key=lambda v: -v["tayang"])[:60], "jumlah_video": len(video),
            "produk": pr[:40], "jumlah_produk": len(pr),
            "video_berproduk": sum(1 for v in video if v["produk"]),
            "rentang": [min(wkt), max(wkt)] if wkt else None, "detik": round(time.time() - t0, 1), "jam": _iso_kini()}


# ===== v1.5.7 AI (OPSIONAL) — asisten, BUKAN sumber angka =====
# Prinsip: angka (tayangan, terjual, umur iklan, jumlah affiliator) tetap 100% dari Meta/Shopee.
# AI hanya MENILAI (relevansi abu-abu, peran pengiklan, sudut jualan) & MENULIS (naskah). Tanpa kunci / kuota
# habis / galat → semua fitur kembali ke mesin aturan (tidak ada yang gagal, scan tidak melambat).
# Kunci disimpan di Environment Render: INTAI_GEMINI_KUNCI (utama), INTAI_GROQ_KUNCI (cadangan).
AI_GEMINI = (os.environ.get("INTAI_GEMINI_KUNCI") or os.environ.get("GEMINI_API_KEY") or "").strip()
AI_GROQ = (os.environ.get("INTAI_GROQ_KUNCI") or os.environ.get("GROQ_API_KEY") or "").strip()
AI_POLL = os.environ.get("INTAI_AI_POLLINATIONS") == "1"   # HANYA untuk uji lokal (tanpa kunci, tidak andal)
AI_MODEL_GEMINI = [m for m in dict.fromkeys([os.environ.get("INTAI_GEMINI_MODEL") or "", "gemini-flash-lite-latest",
                                             "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-2.5-flash"]) if m]
AI_MODEL_GROQ = [m for m in dict.fromkeys([os.environ.get("INTAI_GROQ_MODEL") or "", "openai/gpt-oss-120b",
                                           "openai/gpt-oss-20b"]) if m]   # v1.5.8b: llama-3.x dimatikan Groq 16 Agu 2026
AI_RPM = {"gemini": int(os.environ.get("INTAI_AI_RPM") or 10), "groq": 25, "poll": 2}
AI_HARIAN_SISTEM = int(os.environ.get("INTAI_AI_HARIAN") or 700)    # hakim relevansi + peran (otomatis) per hari
AI_PER_AKUN = int(os.environ.get("INTAI_AI_PER_AKUN") or 30)        # naskah + sudut (diminta user) per akun per hari
kunci_ai = threading.Lock()
AI_STAT = {"hari": "", "sistem": 0, "akun": {}, "n": {}, "gagal": 0, "galat": None, "galat_t": None,
           "terakhir": None, "terakhir_t": None}
_AI_JENDELA = {"gemini": [], "groq": [], "poll": []}
_AI_MODEL_MATI = set()
_AI_REHAT = {}   # penyedia → epoch boleh dicoba lagi (429 / kuota habis)


_AI_DAFTAR = {}   # v1.5.8b: penyedia → (epoch, [model aktif di akun ini]) — model dipensiunkan tak lagi mematikan AI


def _ai_daftar_model(nama):
    c = _AI_DAFTAR.get(nama)
    if c and time.time() - c[0] < 6 * 3600:
        return c[1]
    hasil = None
    try:
        if nama == "gemini" and AI_GEMINI:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                             headers={"x-goog-api-key": AI_GEMINI}, timeout=10)
            if r.status_code == 200:
                ms = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
                      if "generateContent" in (m.get("supportedGenerationMethods") or [])]
                ms = [m for m in ms if "flash" in m and not re.search(r"image|tts|audio|live|embed|thinking|exp|computer|robot", m)]

                def skor(m):   # flash-lite dulu (jatah harian terbesar), lalu flash; yang stabil sebelum preview
                    return (0 if "lite" in m else 1, 1 if "preview" in m else 0, 0 if "latest" in m else 1,
                            -float((re.search(r"(\d+(?:\.\d+)?)", m) or [0, "0"])[1]))
                hasil = sorted(ms, key=skor)[:6]
        elif nama == "groq" and AI_GROQ:
            r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": "Bearer " + AI_GROQ}, timeout=10)
            if r.status_code == 200:
                ids = [m.get("id", "") for m in r.json().get("data", []) if m.get("active", True)]
                ids = [i for i in ids if not re.search(r"whisper|guard|tts|orpheus|playai|distil|compound|embed|vision|prompt", i, re.I)]
                pilih = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
                hasil = [i for i in pilih if i in ids] + sorted(i for i in ids if i not in pilih)[:4]
    except Exception:
        hasil = None
    _AI_DAFTAR[nama] = (time.time() if hasil else time.time() - 6 * 3600 + 600, hasil or [])   # gagal → coba lagi 10 mnt
    return hasil or []


def _ai_urutan(nama, bawaan):
    env = os.environ.get("INTAI_GEMINI_MODEL" if nama == "gemini" else "INTAI_GROQ_MODEL") or ""
    daftar = _ai_daftar_model(nama)
    if daftar:   # model bawaan yang masih aktif tetap didahulukan, lalu sisa daftar resmi
        return [m for m in dict.fromkeys([env] + [m for m in bawaan if m in daftar] + daftar) if m]
    return bawaan


def ai_aktif():
    return bool(AI_GEMINI or AI_GROQ or AI_POLL)


def _ai_hari():
    h = datetime.now(WIB).strftime("%Y-%m-%d")
    if AI_STAT["hari"] != h:
        AI_STAT.update(hari=h, sistem=0, akun={}, n={}, gagal=0)
    return h


def ai_jatah(email, tugas):
    """(boleh, pesan). Tugas user (naskah/sudut) dibatasi per akun; tugas sistem dibatasi global."""
    with kunci_ai:
        _ai_hari()
        if tugas in ("hakim", "peran"):
            return (AI_STAT["sistem"] < AI_HARIAN_SISTEM), "Jatah AI otomatis hari ini habis."
        if email and is_admin(email):
            return True, ""
        n = AI_STAT["akun"].get(email or "", 0)
        return (n < AI_PER_AKUN), f"Jatah AI kamu hari ini ({AI_PER_AKUN}×) sudah habis — besok terisi lagi. Versi tanpa AI tetap bisa dipakai."


def _ai_hitung(email, tugas, penyedia):
    with kunci_ai:
        _ai_hari()
        if tugas in ("hakim", "peran"):
            AI_STAT["sistem"] += 1
        else:
            AI_STAT["akun"][email or ""] = AI_STAT["akun"].get(email or "", 0) + 1
        AI_STAT["n"][penyedia] = AI_STAT["n"].get(penyedia, 0) + 1
        AI_STAT["terakhir"], AI_STAT["terakhir_t"] = penyedia, _iso_kini()


def _ai_slot(p, tunggu_maks=3.0):
    """Batas permintaan/menit per penyedia (free tier). Penuh → tunggu sebentar, lalu menyerah."""
    akhir = time.time() + tunggu_maks
    while True:
        with kunci_ai:
            kini = time.time()
            if _AI_REHAT.get(p, 0) > kini:
                return False
            j = _AI_JENDELA[p] = [t for t in _AI_JENDELA[p] if kini - t < 60]
            if len(j) < AI_RPM.get(p, 10):
                j.append(kini)
                return True
        if time.time() >= akhir:
            return False
        time.sleep(0.4)


def _ai_json(teks):
    t = str(teks or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
    for a, b in (("{", "}"), ("[", "]")):
        i, j = t.find(a), t.rfind(b)
        if i >= 0 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None


def _ai_gemini(sistem, prompt, maks, suhu, batas):
    galat = None
    for m in _ai_urutan("gemini", AI_MODEL_GEMINI):
        if m in _AI_MODEL_MATI:
            continue
        gc = {"temperature": suhu, "maxOutputTokens": maks, "responseMimeType": "application/json",
              "thinkingConfig": {"thinkingBudget": 0}}
        body = {"systemInstruction": {"parts": [{"text": sistem}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gc}
        r, sibuk = None, 0
        for _ in range(3):
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                              headers={"x-goog-api-key": AI_GEMINI, "Content-Type": "application/json"},
                              json=body, timeout=batas)
            if r.status_code == 400 and "think" in r.text.lower() and "thinkingConfig" in gc:
                gc.pop("thinkingConfig", None)   # model baru menolak thinkingBudget → kirim ulang tanpa itu
                continue
            if r.status_code in (500, 503) and not sibuk:   # v1.5.8b: server Google penuh (sementara) → tunggu, ulang sekali
                sibuk = 1
                time.sleep(1.5)
                continue
            break
        if r.status_code == 404:
            _AI_MODEL_MATI.add(m)
            continue
        if r.status_code == 429:
            _AI_REHAT["gemini"] = time.time() + 60
            return None, "Gemini: kuota/menit habis (429)"
        if r.status_code in (401, 403):
            return None, f"Gemini: kunci ditolak ({r.status_code})"
        if r.status_code == 400 and "api key" in r.text.lower():
            return None, "Gemini: kunci tidak valid (400)"
        if r.status_code != 200:
            galat = f"Gemini {m}: HTTP {r.status_code} {r.text[:80]}"
            continue
        try:
            d = r.json()
            teks = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
            return teks, "gemini:" + m
        except Exception:
            galat = f"Gemini {m}: jawaban kosong"
            continue
    return None, galat or "Gemini: tidak ada model yang tersedia"


def _ai_openai(url, kunci, models, sistem, prompt, maks, suhu, batas, nama, json_mode=True):
    galat = None
    for m in models:
        if (nama + m) in _AI_MODEL_MATI:
            continue
        body = {"model": m, "temperature": suhu, "max_tokens": maks,
                "messages": [{"role": "system", "content": sistem}, {"role": "user", "content": prompt}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if nama == "groq" and "gpt-oss" in m:   # v1.5.8b: model penalar → nalar singkat, token nalar ikut dihitung
            body.update(reasoning_effort="low", max_tokens=maks + 600)
        if nama == "poll":   # uji lokal: model penalar, batas 1.500 token
            body.update(reasoning_effort="low", max_tokens=min(maks, 1500), seed=secrets.randbelow(10 ** 6))   # seed: lewati cache jawaban
        hd = {"Content-Type": "application/json"}
        if kunci:
            hd["Authorization"] = "Bearer " + kunci
        r = requests.post(url, headers=hd, json=body, timeout=batas)
        if r.status_code == 400 and "response_format" in body and re.search(r"response_format|json", r.text, re.I) \
                and not re.search(r"decommission|not found|does not exist", r.text, re.I):
            body.pop("response_format", None)   # model tak mendukung mode JSON → minta JSON lewat prompt
            body["messages"][-1]["content"] += "\n\nBalas HANYA JSON valid."
            r = requests.post(url, headers=hd, json=body, timeout=batas)
        if r.status_code == 404 or (r.status_code == 400 and re.search(r"decommission|model_not_found|does not exist|not supported", r.text, re.I)):
            _AI_MODEL_MATI.add(nama + m)
            continue
        if r.status_code == 429:
            _AI_REHAT[nama] = time.time() + 60
            return None, f"{nama}: kuota/menit habis (429)"
        if r.status_code in (401, 403):
            return None, f"{nama}: kunci ditolak ({r.status_code})"
        if r.status_code != 200:
            galat = f"{nama} {m}: HTTP {r.status_code} {r.text[:80]}"
            continue
        try:
            return r.json()["choices"][0]["message"]["content"], f"{nama}:{m}"
        except Exception:
            galat = f"{nama} {m}: jawaban kosong"
    return None, galat or f"{nama}: tidak ada model"


def ai_panggil(sistem, prompt, tugas="umum", email=None, maks=1500, suhu=0.3, batas=20, tunggu_slot=3.0):
    """→ (objek_json | None, info). Urutan: Gemini → Groq → (Pollinations bila diizinkan untuk uji)."""
    if not ai_aktif():
        return None, {"penyedia": None, "error": "AI belum diaktifkan (kunci belum diisi admin)."}
    boleh, pesan = ai_jatah(email, tugas)
    if not boleh:
        return None, {"penyedia": None, "error": pesan, "jatah": True}
    calon = []
    if AI_GEMINI:
        calon.append(("gemini", lambda: _ai_gemini(sistem, prompt, maks, suhu, batas)))
    if AI_GROQ:
        calon.append(("groq", lambda: _ai_openai("https://api.groq.com/openai/v1/chat/completions", AI_GROQ,
                                                 _ai_urutan("groq", AI_MODEL_GROQ), sistem, prompt, maks, suhu, batas, "groq")))
    if AI_POLL:
        calon.append(("poll", lambda: _ai_openai("https://text.pollinations.ai/openai", "", ["openai"],
                                                 sistem, prompt + "\n\nBalas HANYA JSON valid.", maks, suhu,
                                                 max(batas, 90), "poll", json_mode=False)))
    galat = []
    for nama, f in calon:
        if not _ai_slot(nama, tunggu_slot):
            galat.append(nama + ": sedang penuh")
            continue
        try:
            teks, info = f()
        except Exception as e:
            teks, info = None, f"{nama}: {str(e)[:80]}"
        if teks:
            obj = _ai_json(teks)
            if obj is not None:
                _ai_hitung(email, tugas, info)
                return obj, {"penyedia": info}
            info = f"{nama}: jawaban bukan JSON"
        galat.append(info)
    with kunci_ai:
        AI_STAT["gagal"] += 1
        AI_STAT["galat"], AI_STAT["galat_t"] = " · ".join(str(g) for g in galat)[:300], _iso_kini()
    return None, {"penyedia": None, "error": "AI sedang tidak bisa dipakai (" + (" · ".join(str(g) for g in galat)[:240] if galat else "?") + ")."}


def ai_status(email=None):
    with kunci_ai:
        _ai_hari()
        d = {"ok": True, "aktif": ai_aktif(), "jatah_akun": AI_PER_AKUN,
             "terpakai_akun": AI_STAT["akun"].get(email or "", 0)}
        if email and is_admin(email):
            d.update(penyedia=[p for p, ada in (("Gemini", AI_GEMINI), ("Groq", AI_GROQ), ("Pollinations (uji)", AI_POLL)) if ada],
                     model_gemini=[m for m in _ai_urutan("gemini", AI_MODEL_GEMINI) if m not in _AI_MODEL_MATI] if AI_GEMINI else [],
                     model_groq=[m for m in _ai_urutan("groq", AI_MODEL_GROQ) if "groq" + m not in _AI_MODEL_MATI] if AI_GROQ else [],
                     hari=AI_STAT["hari"], sistem=AI_STAT["sistem"], batas_sistem=AI_HARIAN_SISTEM,
                     per_penyedia=dict(AI_STAT["n"]), gagal=AI_STAT["gagal"], galat=AI_STAT["galat"],
                     galat_t=AI_STAT["galat_t"], terakhir=AI_STAT["terakhir"], terakhir_t=AI_STAT["terakhir_t"],
                     akun_hari_ini=len(AI_STAT["akun"]), hakim_terakhir=AI_STAT.get("hakim_terakhir"),
                     hakim_mode=AI_HAKIM_MODE)
        return d


# --- cache penilaian AI (disimpan terkompres di intai_setelan; tanpa SQL baru) ---
AI_CACHE = {"ai_relevan": None, "ai_peran": None}
_AI_CACHE_KOTOR = set()
kunci_ai_cache = threading.Lock()
AI_CACHE_MAKS = 5000


def _ai_cache(nama):
    with kunci_ai_cache:
        if AI_CACHE[nama] is None:
            try:
                ok, v = setelan_baca(nama)
                AI_CACHE[nama] = OrderedDict(_tk_buka(v) if (ok and v) else {})
            except Exception:
                AI_CACHE[nama] = OrderedDict()
        return AI_CACHE[nama]


def _ai_cache_isi(nama, isi):
    c = _ai_cache(nama)
    with kunci_ai_cache:
        for k, v in isi.items():
            c[k] = v
            c.move_to_end(k)
        while len(c) > AI_CACHE_MAKS:
            c.popitem(last=False)
        _AI_CACHE_KOTOR.add(nama)
    threading.Thread(target=_ai_cache_simpan, daemon=True, name="ai-cache").start()


def _ai_cache_simpan():
    time.sleep(2)   # gabungkan beberapa penulisan beruntun
    with kunci_ai_cache:
        kotor = list(_AI_CACHE_KOTOR)
        _AI_CACHE_KOTOR.clear()
        salinan = {n: dict(AI_CACHE[n] or {}) for n in kotor}
    for n, v in salinan.items():
        try:
            setelan_tulis(n, _tk_bungkus(v))
        except Exception:
            pass


def _ai_ringkas_iklan(snap, o, n=320):
    snap = snap or {}
    judul = re.sub(r"\s+", " ", bersih_tpl(_t(snap, "title"))).strip()
    body = re.sub(r"\s+", " ", bersih_tpl(_t(snap, "body"))).strip()
    ld = re.sub(r"\s+", " ", bersih_tpl(_t(snap, "link_description"))).strip()
    kartu = " / ".join(re.sub(r"\s+", " ", str(c.get("title") or "")).strip()
                       for c in (snap.get("cards") or [])[:3] if isinstance(c, dict) and c.get("title"))
    hal = o.get("page_name") or snap.get("page_name") or ""
    kat = ", ".join(str(x) for x in (snap.get("page_categories") or o.get("categories") or [])[:2])
    dom = urlparse(str(snap.get("link_url") or "")).netloc.replace("www.", "")
    bag = [f"Halaman: {hal}" + (f" ({kat})" if kat else "")]
    if judul:
        bag.append("Judul: " + judul[:100])
    if kartu:
        bag.append("Kartu: " + kartu[:140])
    if body:
        bag.append("Teks: " + body[:n])
    if ld:
        bag.append("Deskripsi link: " + ld[:100])
    if dom:
        bag.append("Link: " + dom)
    return " | ".join(bag)


# --- (1) HAKIM RELEVANSI untuk zona abu-abu (skor aturan 15–59) ---
AI_HAKIM_BAWAH, AI_HAKIM_ATAS, AI_HAKIM_MAKS = 15, 59, 36
# "bayangan" (bawaan): putusan AI hanya DITAMPILKAN sebagai pendapat kedua — label/angka tetap dari aturan.
# Alasan: uji 100 kasus emas zona abu-abu (16 Okt, model uji gpt-oss-20b) → aturan 98/100, aturan+AI 77/100.
# Admin boleh menyetel INTAI_AI_HAKIM=ubah HANYA bila /api/admin/ai-uji membuktikan AI (Gemini) ≥ aturan.
AI_HAKIM_MODE = (os.environ.get("INTAI_AI_HAKIM") or "bayangan").strip().lower()
if AI_HAKIM_MODE not in ("bayangan", "ubah", "mati"):
    AI_HAKIM_MODE = "bayangan"
# 100 kasus emas zona abu-abu (dari riset/mesin/uji_kasus.py): [kata, judul, teks, halaman, kategori, link, deskripsi, label_benar]
AI_EMAS = json.loads(r'''[["hijab","Helm Hijab Full Visor","helm nyaman","Toko X",null,"",null,"Longgar"],["kopi","Meja Teras Coffee Table","kayu jati","Toko X",null,"",null,"Longgar"],["kopi","Mesin Kopi Espresso","Rp2jt","Toko X",null,"",null,"Longgar"],["kopi","Pemanis untuk teh, kopi, matcha","0 kalori Rp50rb","Toko X",null,"",null,"Longgar"],["sepatu","Rak Sepatu 5 Susun","Rp99rb","Toko X",null,"",null,"Longgar"],["tas","Bukan tas biasa","ini dompet Rp59rb","Toko X",null,"",null,"Longgar"],["tas","Bikin tas makin gemes","promo Rp35rb","Toko X",["Burgers"],"",null,"Longgar"],["lampu tidur","Lampu Tidur Aesthetic","Rp80rb","Toko X",["Bank"],"",null,"Longgar"],["kecantikan","Perawatan kecantikan kendaraan kamu","coating Rp500rb","Toko X",["Servis Otomotif"],"",null,"Longgar"],["skincare","Skincare Glowing BPOM","Rp99rb","Toko X",["Servis Otomotif"],"",null,"Longgar"],["kaos","Jersey custom 4 jam","BONUS KAOS KAKI untuk timmu Rp99rb","Toko X",null,"",null,"Longgar"],["kaos","Bali Tee Shop","Original Bali t-shirt, cotton, made in Bali. Visit our shop in Kuta","Toko X",null,"",null,"Relevan"],["kaos","Pemutih Kaos Putih","bikin kaos putih kinclong lagi, Rp15rb","Toko Y",null,"",null,"Longgar"],["madu","","jom madu asli dari habib, whatsapp kami","Toko X",null,"",null,"Longgar"],["rak sepatu","Portable Dehumidifier Ravelle","Lemari lembap? Rak sepatu mulai bau apek? Pakai dehumidifier ini Rp300rb. Cocok di lemari, rak sepatu, kabinet","Toko X",null,"",null,"Longgar"],["rak sepatu","","Rak dinding minimalis #raksepatu Rp99rb","Toko X",null,"",null,"Relevan"],["helm","","Toko helm balap di Jepang dekat Haneda","ヘルメット 東京",null,"",null,"Longgar"],["helm","","Kadang yang bikin capek itu bukan karena aktivitasnya padat. Tapi karena di sela-sela aktivitas selalu ada momen benerin hijab, mulai dari habis pakai helm, kena angin","Toko X",["Clothing (Brand)"],"",null,"Longgar"],["kosmetik","Foundation Matte 24 Jam","Coverage tinggi, shade cocok kulit sawo matang Rp89rb","Toko X",null,"",null,"Relevan"],["kosmetik","inspirasi kamar aesthethic","Rak Laci Serbaguna Lemari Plastik – Kotak Penyimpanan Pakaian, Kosmetik & Aksesoris Rp89rb","Toko X",null,"",null,"Longgar"],["kemeja","Hem Batik Pria","Hem lengan panjang katun Rp120rb","Toko X",null,"",null,"Relevan"],["hp","","{COD} HP Mainan Musik Anak Lampu 45 Tombol Rp35rb","Toko X",null,"",null,"Longgar"],["hp","","Pakai maps di mobil jadi lebih nyaman tanpa harus pegang HP terus Rp35rb","Toko X",null,"",null,"Longgar"],["hp","","Numpuk foto di galeri? Cetak jadi album, sekarang HP lega Rp50rb","Idphotobook",null,"",null,"Longgar"],["laptop","","BAGSMART Mini Backpack Laptop 9.7Inch ransel mini Rp199rb","Toko X",null,"",null,"Longgar"],["laptop","Feel Like Yourself Again","I came to on the floor of a HyVee in Topeka, Kansas. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it. I opened my laptop and read about it.  checkout now","Toko X",null,"",null,"Longgar"],["popok","Efektif Cegah & Redakan Ruam Popok","Ruam bikin si Kecil rewel? Expert Care Diaper Rash Cream Zinc Oxide Rp45rb","Toko X",null,"",null,"Longgar"],["popok","","Ruam popok iritasi vs jamur. Lindungi kulit bayi dengan Kinderen Diaper Rash Cream","Toko X",null,"",null,"Longgar"],["kasur","","Air kotor jadi bukti, kasur yang kelihatan bersih bisa menipu! Untung ada Heloklin","Heloklin Indonesia",["Perawatan di Rumah"],"",null,"Longgar"],["kasur","Hydro Vacuum","Hydro Vacuum membersihkan debu tungau di kasur, gorden, bantal","Toko Y",["Household supplies"],"",null,"Longgar"],["kursi","","Ready sarung kursi Futura ketat putih 100pcs Wa 0895","Sahrul konveksi",null,"",null,"Longgar"],["meja","","EDON Air Cooler Mini viral! Cocok buat: ✔️ Kamar kecil / kos ✔️ Meja kerja / meja belajar. Udah banyak yang pakai di kamar & meja kerja! Rp99rb #aircooler","Toko X",null,"",null,"Longgar"],["meja","","Open Order Taplak Meja Bulat bahan lotto tebal Rp35rb","Sahrul konveksi",null,"",null,"Longgar"],["meja","Dracaena Fragrans Aesthetic","Tanaman Hias Meja Aesthetic Bambu Hoki Rp45rb","Toko X",null,"",null,"Longgar"],["meja","","Siap-siap kenalan sama warna baru yang bakal bikin meja kerja kamu makin aesthetic #laptoplifestyle","Axioo Indonesia",["Phone/Tablet"],"",null,"Longgar"],["sambal","","Crispy Fried Duck. Golden crunch, sambal kick. #tanahresto","TANAH Restaurant",["Restaurant"],"",null,"Longgar"],["sambal","","Tumpeng Tampah untuk 10 orang. Isiannya nasi kuning, ayam goreng, telur balado, perkedel dan sambal","Tumpeng25rb",null,"",null,"Longgar"],["sambal","","Pilihan menunya hari ini: Ikan Gurame Panggang, Udang Sambal Lado, Cumi Goreng","Lemburkuringjakarta",["Diner"],"",null,"Longgar"],["sambal","","Crispy Fried Duck. Golden crunch, sambal kick. #tanahresto #restolombok","TANAH Restaurant",["Restaurant"],"",null,"Longgar"],["teh","","Alhamdulillah, bahagia dapet tanggapan positif dari Teh Vira & A Aldi, kasur sudah sampai Rp2jt","Supermarket Mebel",null,"",null,"Longgar"],["teh","Solusi untuk Bau Mulut yang Sering Kambuh","Permen teh hijau penyegar napas, praktis dibawa ke mana saja. Order sekarang gratis ongkir!","Permen Nafas Segar Teh Hijau",["Health/Beauty"],"",null,"Longgar"],["jam tangan","Smart Watch Pria","Watch AMOLED 1ATM strap logam Rp299rb","Toko X",null,"",null,"Relevan"],["panci","","Cairan Ajaib Pembersih Kerak Panci & Wajan Rp25rb","Toko X",null,"",null,"Longgar"],["celana","","Bunda, waktunya cek stok popok si kecil! Sweety Silver Pants – Popok Bayi Celana Jumbo Pack. Lagi cari popok celana bayi? Model celana praktis #popokcelana","Toko X",null,"",null,"Longgar"],["celana","Quick Dry & Breathable Running Pants","Dengan bahan anti sobek dan jahitan kuat, Running Pants ini memang dibuat untuk pria aktif yang jogging rutin & suka outdoor.","Elfs Active",["Outdoor & Sporting Goods Company"],"",null,"Relevan"],["obat lambung","Lambung Tenang, Hidup Lebih Nyaman!","Sudah coba banyak cara tapi masih nyeri ulu hati, asam lambung naik. Asam lambung kambuh lagi? Coba ramuan alami ini","JSR Store",["Herbal Medicine Store"],"",null,"Relevan"],["obat lambung","Diskusi bareng yuk","Sop Subarashi dapat membantu perkembangan anak autis. Pemesanan produk dan Konsultasi, DM kami. #sopsubarashi #autisme #anakautis #autis #autoimun #gerd #maag #asamlambung #ginjal #diabetes #hati #nyerisendi #otak #tulang #hipertensi","Konnichiwa.AFCAllstars",["Food & Drink"],"",null,"Longgar"],["obat batuk","","Batuk berdahak tak kunjung reda? Sirup herbal pereda batuk untuk keluarga, batuk kering juga bisa","Herbal Nusantara",null,"",null,"Relevan"],["sunscreen","Serum Brightening Glow","Serum niacinamide untuk mencerahkan, toner pH balance, paket skincare lengkap","Glow Beauty",null,"",null,"Longgar"],["sunscreen","Set Pemutih Tabir Surya Vitamin C","SPF 50+ PA+++ perlindungan maksimal dari sinar UV, Rp89rb","Vibrant Glamour Indonesia",null,"",null,"Relevan"],["sprei","Seprai Katun Jepang Anti Luntur","Bed cover dan seprai ukuran 160x200 lembut adem","Rumah Tidur",null,"",null,"Relevan"],["rice cooker","Magic Com 3in1 Hemat Listrik","Penanak nasi serbaguna, bisa kukus dan masak","Dapur Hemat",null,"",null,"Relevan"],["rice cooker","Paket Bali 3D2N","Liburan hemat ke Bali, hotel + rice cooker di kamar","Bali Trip",["Tour Agency"],"",null,"Longgar"],["rice cooker","Connector sesuai standar global","Holland Plug untuk rice cooker, setrika, dispenser. Diproduksi lokal, standar SNI","PT Kabel X",["Telecommunication company"],"",null,"Longgar"],["kaos kaki","Kaus Kaki Anti Bau","Socks katun premium isi 6 pasang","Sock Store",null,"",null,"Relevan"],["kopi diet","","Kapok minuman diet yang rasanya kayak obat? Coba yang ini, enak dan bikin kenyang","Curvie Coffee",["Grocery Store"],"",null,"Longgar"],["kopi diet","","Lakukan diet intermitten fasting dengan minum kopi ini, badan tidak lemes","Herbal Life",["Health/beauty"],"",null,"Relevan"],["kopi diet","Promo Spesial Pagi","Menu sarapan lengkap, kopi susu gula aren, roti bakar, nasi goreng. Buat yang lagi diet juga ada salad","Warung Pagi",["Café"],"",null,"Longgar"],["serum wajah","Glowing nggak putus","Scarlett Body Serum si wangi tahan lama bikin kulit badan cerah","Scarlett",null,"",null,"Longgar"],["serum wajah","Brightening Cream Wajah","Krim untuk kulit wajah kusam, moisturizer ringan","Skin Lab",null,"",null,"Longgar"],["serum wajah","Serum Emas Anti Flek Hitam","Serum untuk flek dan noda hitam","dr. Shindy",null,"",null,"Relevan"],["serum wajah","Checkout Sekarang!","SERUM AZARINE CUMA 30 RIBU AN","AzarineOfficial",["Health/beauty"],"",null,"Relevan"],["cokelat","","Emang boleh softlens coklat senatural ini? Yes, tipe Muted Choco emang natural banget! Perfect buat kamu yang mau pake softlens ke sekolah #softlenscoklat #softlensnatural","Lens by Seoul",["Health/Beauty"],"",null,"Longgar"],["cokelat","Vitamin Lengkap, Tulang Kuat","Tablet kunyah yang mengandung Kalsium dan Vitamin D dengan rasa coklat favorit anak-anak! #Fitkom","Fitkom Ranger Indonesia",null,"",null,"Longgar"],["cokelat","","Cobain MAIJI Roti Mentega 60Gr! Tersedia 4 varian favorit: Coklat, Cheese, Strawberry & Blueberry. #roti #coklat","Rekomendasi Belanja",null,"",null,"Longgar"],["dompet","Shopee Indonesia | Belanja Online Terlengkap & Terpercaya","Padahal ada solusi namanya tas..  Tapi bukan cewek namanya kalau gak gini. Bawa kunci mobil, earphone, card holder, jepitan, kacamata.. #Hanasui #HanasuiPowerSerum","Skincare Hanasui",["Health & Beauty"],"",null,"Longgar"],["casing hp","","Glow up HP kamu dengan case ungu glitter nan mewah ini! Tampilannya super aesthetic, bahan tebal, order sekarang","Calista case is here",["Shopping & Retail"],"",null,"Relevan"],["casing hp","","Softcase. Stylish. Support MagSafe. Paika Case ngasih perlindungan maksimal buat iPhone kamu","Wandercase Official Indonesia",["Accessories"],"",null,"Relevan"],["casing hp","","Tangan kamu bakal lebih rileks dengan adanya sandaran bintang magnetik di casing pink polkadot ini. Ready stock! #CasingHP #CaseLucu https://s.shopee.co.id/6L4h75Vmws","Badranaya Web Studio",null,"",null,"Relevan"],["casing hp","","iPhone 13 128gb all operator 5jutaan aja. Free adaptor ugreen softcase antigores dan iCloud. Stok terbatas!","AsA Store Kisaran",null,"",null,"Longgar"],["sepeda","","GA SEMUA BAHAN ALAM AMPUH Kalau diolah dengan Benar, awet mudanya sampe cucu cicit #Flekhitam #pencerah #cycling #running #cyclist #sepeda #jalankaki #lari #runner","Puji Sopandi - Natural Product Development",["Kesehatan & Kecantikan"],"",null,"Longgar"],["sepeda","1 HARI TELAH BERLALU!!!","Viona by Bonita bikin heboh di PRJ Surabaya! Promonya pecah: Buy 1 Get 1. Menangkan kulkas, mesin cuci, kompor, sepeda listrik, dispenser, dan puluhan hadiah menarik","Viona by Bonita",["Product/service"],"",null,"Longgar"],["sepeda","BISA BAYAR DI TEMPAT (COD)","Sering merasa pegal saat bersepeda jarak jauh? Ganti sadel sepeda Anda. Busa foam empuk dan desain ergonomis","Toko Serbaguna Plus",["Business"],"",null,"Longgar"],["minyak kayu putih","Jual Jompo Keychain by Habibie Craft Gantungan kunci freshcare fresh care kayu putih lucu kekinian viral imut sakura pink bag Charm tas hiasan | Shopee Indonesia","Rekomendasi bag charm edisi lucu","Promo Tertawa Saja",["Product/service"],"",null,"Longgar"],["baju anak","","Review skincare anak viral udah pake ini aja bund Body Lotion: https://s.shopee.co.id/x Body Wash: https://s.shopee.co.id/y Detergen Baju Anak: https://s.shopee.co.id/z","Info Produk Diskon Setiap Hari",["Business"],"",null,"Longgar"],["masker wajah","Beli ZALA Gummy, GRATIS Masker Wajah! (Stok Terbatas)","Pusing atasi FLEK HITAM membandel? ZALA Gummy Collagen hadir dengan formula premium 2400mg Collagen.","Gerai Beauty",["Topic"],"",null,"Longgar"],["masker wajah","","Penggunaan face shield bisa lebih membantu pencegahan Covid-19. Gunakanlah face shield bersama dengan masker, pastikan pas dengan wajah. #ProtokolKesehatan","Gugus Tugas Covid-19 Provinsi NTT",["Public Service"],"",null,"Longgar"],["kerudung","Promo Modal Viscose Polos Hanya 14.000-an!","STOP SCROLLING! Cari supplier bahan hijab & pashmina premium yang ramah budget produksi? Modal Viscose jawabannya!","V-TEX",["Business"],"",null,"Longgar"],["kerudung","Bros Korea Premium Motif Bunga Mewah Elegan Kebaya Hijab Jilbab Jas - GEUM GE01B022","Dibuat dengan detail rapi, cocok untuk acara formal.","geum.jewelry",["Jewelry/watches"],"",null,"Longgar"],["kerudung","","Bros Korea Premium Motif Bunga Mewah Elegan Kebaya Hijab Jilbab Jas - GEUM GE01B022\n\nDibuat dengan bahan premium secara handmade dan dilapisi Emas 18K.\n\n#aksesoriswanita #broshijab #brosjilbab #brospremium","geum.jewelry",["Profile"],"",null,"Longgar"],["stroller","Diaper bag favorit modern parents","Stroller hook terpisah bisa ketinggalan atau hilang. Dengan diaper bag Papamama Clio yang punya built-in stroller hook, nggak perlu khawatir.","Papamama",["Baby goods/kids goods"],"",null,"Longgar"],["stroller","Bayi Anti Gerah di Stroller","Quokka Stroller Pad bahan cooling, bayi adem di stroller seharian. Diskon 25%.","Quokka Baby Indonesia",["Baby goods/kids goods"],"",null,"Longgar"],["bantal","","Quick Clean Hydro Vacuum membersihkan rumah mulai dari (kasur, lantai, gorden, furniture, bantal) jadi bebas tungau. Order sekarang gratis ongkir.","Quick Clean Indonesia",["Home goods store"],"",null,"Longgar"],["alat pancing","Pancing Set Lengkap","Pancing set lengkap joran + reel + senar, siap mancing. COD.","Toko Mancing",["Sporting goods store"],"",null,"Relevan"],["alat pancing","","Beli di sini: https://s.shopee.co.id/4Vd8n3eL9s #setpancing #alatpancing #mancingmania #hobimancing #pemancingindonesia #perlengkapanmancing #mancingikan #hobioutdoor #racunbelanja #barangunik","Cuan Harian",["Shopping & retail"],"",null,"Relevan"],["kamera","","Harga Ambyar https://s.shopee.co.id/x Mini Action Camera Portable 4K anti air, cocok buat vlog.","Sinta Herlina",["Personal blog"],"",null,"Relevan"],["mukena","Udah Gak Perlu Buang Mukenah yang Berjamur Lagi","Mukenah udah bertahun-tahun dipakai dan gak bisa dihilangkan jamurnya? tenang ada penghilang noda kain dari Klinbos. Yuk checkout sekarang!","Klinbos Indonesia",["Household supplies"],"",null,"Longgar"],["mukena","DIDIK ANAK DENGAN BAIK SEKARANG","CARA MENDIDIK ANAK ALA NABI Dapatkan promo terbatas Pembayaran COD Gratis Ongkir","Artra Branding",["Business"],"https://mahajemen.orderonline.id/page/buku-parenting","Mukenah Lipat Viral. Dapatkan promo terbatas","Longgar"],["jaket","UNIQLO Indonesia","Yuk, eksplor pilihan Jaket yang sesuai dengan gayamu di aplikasi UNIQLO. Klik di sini.","Uniqlo Indonesia",["Retail company"],"http://itunes.apple.com/app/id867569998",null,"Longgar"],["sandal","Sweat-Absorbing Sandals","Hot days, sweaty feet? These sweat-absorbing sandals help keep every step feeling dry, fresh, and comfortable.","VIVAIA",["Website"],"https://www.vivaia.com/campaign/Comfortable-Walking-Platform-Sandals",null,"Longgar"],["vitamin anak","","Belanja di sini: https://s.shopee.co.id/9zx26IWKx6\n#VitaminAnak #ProdukAnak #IbuDanAnak #KidsDaily #ShopeeFinds #RacunShopee #FYP","Spill Murce",["Brand"],"",null,"Relevan"],["earphone","","GG EZ Dance GG YE-Z\n#Trend #itzy #ggez #idols #dancechallenge #fyp #earphone #viral","Asha",["Artist"],"",null,"Longgar"],["parfum","Parfum Mobil Aroma Kopi","pengharum kabin tahan 30 hari Rp25rb","Toko Y",null,"",null,"Longgar"],["pupuk","","Third FJD installation in Lampung featuring the FJD AT2 Auto Steer System for more precise fertilizer application. #FJDynamics #SmartFarming","FJDynamics AgTech",["Business"],"",null,"Longgar"],["makanan kucing","","Anabul susah makan? Saatnya kasih Mix 30PCS Chiyo Creamy Cat Strip yang bikin si meong lahap! 100% daging asli","Rekomendasi Belanja",null,"",null,"Relevan"],["obat jerawat","IPL Acne 199rb","JERAWAT BANDEL? IPL ACNE SOLUSINYA! Capek sama jerawat? Saatnya coba IPL ACNE di SOZO Clinic! Hanya 199RB","Sozo Skin Clinic",["Health/beauty"],"",null,"Longgar"],["lipstik","","Bibir item tapi cari lipen yg bisa cover? Susah cari liptint yang pas, pake lipmatte malah kering. Untungnya ketemu Cleviant Serum Tint, 1x swipe langsung nutup","Cléviant",null,"",null,"Relevan"],["lipstik","Fix Lipstick Sultan","Baru tau kalo ada lipstick harganya bisa tembus segini, pantesan jadi idaman banyak orang","Spill By Mowii",["Software"],"https://s.shopee.co.id/113dF7ymDk",null,"Relevan"],["s","n","e","a","k","r","s","e"],["s","n","e","a","k","r","s","e"]]''')
AI_HAKIM_SISTEM = (
    "Kamu penilai iklan untuk alat riset jualan di Indonesia. Untuk setiap iklan, tentukan hubungannya dengan "
    "NICHE yang dicari pengguna. Ikuti definisi ini dengan ketat:\n"
    "- \"inti\": produk UTAMA yang dijual iklan ini jelas adalah niche itu sendiri (nama produknya = niche atau jenis "
    "dari niche). Contoh niche 'madu': 'Madu Hutan Asli 1kg'.\n"
    "- \"sebut\": kata niche merujuk ke benda niche yang sebenarnya, tetapi yang dijual produk LAIN yang berkaitan "
    "(aksesori, perawatan, wadah, pelengkap, bonus, dipakai bersama), ATAU iklan terlalu umum untuk dipastikan. "
    "Contoh niche 'kasur': 'vacuum pembersih kasur'; niche 'hp': 'holder hp di mobil'. JIKA RAGU, PILIH 'sebut'.\n"
    "- \"lepas\": kata niche TIDAK merujuk ke benda niche sama sekali: idiom/kiasan ('bikin dompet jebol'), nama "
    "orang/merek/tempat, rasa/warna (niche 'cokelat' pada 'softlens coklat'), cerita/novel, donasi, berita, "
    "lowongan, atau properti.\n"
    "Nilai 'yakin' 0-100. Balas JSON: "
    "{\"hasil\":[{\"id\":<nomor>,\"kelas\":\"inti|sebut|lepas\",\"yakin\":<0-100>,\"alasan\":\"<maks 8 kata>\"}]}")


def _ai_hakim_label(lab0, kelas, yakin):
    """Label yang AKAN dipakai bila putusan AI diterapkan (zona abu-abu saja)."""
    if kelas == "inti" and yakin >= 70 and lab0 == "Longgar":
        return "Relevan"
    if kelas == "lepas" and yakin >= 80 and lab0 in ("Longgar", "Relevan"):
        return "Lepas"
    if kelas == "sebut" and yakin >= 80 and lab0 == "Relevan":
        return "Longgar"
    return lab0


def _ai_hakim_terapkan(rel, v, mode=None):
    """mode 'bayangan': hanya catat pendapat AI (rel['ai']); 'ubah': label ikut berubah & tercatat di 'cocok'."""
    mode = mode or AI_HAKIM_MODE
    kelas, yakin = v.get("k"), _int(v.get("y"), 0) or 0
    n0, lab0 = rel.get("nilai", 0), rel.get("label")
    usul = _ai_hakim_label(lab0, kelas, yakin)
    rel["ai"] = {"kelas": kelas, "yakin": yakin, "alasan": v.get("a") or "", "asal": lab0, "usul": usul, "mode": mode}
    if mode != "ubah":
        return
    if kelas == "inti" and yakin >= 70 and lab0 == "Longgar":
        rel.update(nilai=max(n0, 45), label="Relevan")
    elif kelas == "lepas" and yakin >= 80 and lab0 in ("Longgar", "Relevan"):
        rel.update(nilai=min(n0, 10), label="Lepas")
    elif kelas == "sebut" and yakin >= 80 and lab0 == "Relevan":
        rel.update(nilai=min(n0, 35), label="Longgar")
    if rel.get("label") != lab0:
        rel.setdefault("cocok", []).append(f"🤖 AI: {kelas} ({yakin}%) — {lab0}→{rel['label']}")


def ai_hakim_relevansi(kata, pasangan, batas=8.0, wadah=None):
    """pasangan: [(rel, o)] semua iklan scan. Return {aid: putusan} (dipakai di thread terpisah, TIDAK mengubah rel).
    wadah (dict) diisi bertahap: putusan dari cache LANGSUNG, putusan baru menyusul → pemanggil yg berhenti menunggu
    tetap mendapat putusan cache walau panggilan AI baru lambat."""
    kata = _norm(kata).strip()
    putusan = wadah if wadah is not None else {}
    if not kata or not ai_aktif():
        return putusan
    c = _ai_cache("ai_relevan")
    calon = []
    for rel, o in pasangan:
        aid = str(o.get("ad_archive_id") or o.get("archive_id") or "")
        if not aid or not (AI_HAKIM_BAWAH <= (rel.get("nilai") or 0) <= AI_HAKIM_ATAS):
            continue
        k = kata + "|" + aid
        if k in c:
            putusan[aid] = c[k]
        elif len(calon) < AI_HAKIM_MAKS:
            calon.append((aid, _ai_ringkas_iklan(o.get("snapshot") or {}, o, 260)))
    putusan["__siap"] = True   # cache sudah masuk
    if calon:
        daftar = "\n".join(f"{i + 1}. {t}" for i, (_, t) in enumerate(calon))
        obj, _info = ai_panggil(AI_HAKIM_SISTEM, f"NICHE: \"{kata}\"\n\nIKLAN:\n{daftar}", tugas="hakim",
                                maks=60 * len(calon) + 200, suhu=0.0, batas=batas, tunggu_slot=1.0)
        baru = {}
        for h in ((obj or {}).get("hasil") or []) if isinstance(obj, dict) else []:
            try:
                i = int(h.get("id")) - 1
            except Exception:
                continue
            if 0 <= i < len(calon) and h.get("kelas") in ("inti", "sebut", "lepas"):
                v = {"k": h["kelas"], "y": max(0, min(100, _int(h.get("yakin"), 0) or 0)),
                     "a": str(h.get("alasan") or "")[:60]}
                putusan[calon[i][0]] = v
                baru[kata + "|" + calon[i][0]] = v
        if baru:
            _ai_cache_isi("ai_relevan", baru)
    return putusan


def ai_uji_emas(email):
    """Admin: jalankan 100 kasus emas zona abu-abu ke AI yang terpasang → bandingkan aturan vs aturan+AI."""
    if not ai_aktif():
        return {"ok": False, "error": "AI belum diaktifkan (isi INTAI_GEMINI_KUNCI di Environment Render)."}
    sis = AI_HAKIM_SISTEM.replace("NICHE yang dicari pengguna", "NICHE yang tertulis di tiap iklan")
    kasus = []
    for k, ti, bo, pg, kat, lk, ld, exp in AI_EMAS:
        sn = {"title": ti, "body": {"text": bo}, "page_name": pg, "link_url": lk, "link_description": ld}
        if kat:
            sn["page_categories"] = kat
        rel = hitung_relevansi(k, sn, {"page_name": pg}, {})
        kasus.append({"k": k, "sn": sn, "pg": pg, "exp": exp, "rule": rel["label"]})
    t0, dijawab, penyedia, galat = time.time(), 0, None, None
    for b in range(0, len(kasus), 25):
        part = kasus[b:b + 25]
        daftar = "\n".join(f"{i + 1}. NICHE \"{c['k']}\" → {_ai_ringkas_iklan(c['sn'], {'page_name': c['pg']}, 260)}" for i, c in enumerate(part))
        obj, info = ai_panggil(sis, "IKLAN:\n" + daftar, tugas="hakim", maks=2200, suhu=0.0, batas=40, tunggu_slot=8)
        penyedia = info.get("penyedia") or penyedia
        galat = info.get("error") or galat
        for h in ((obj or {}).get("hasil") or []) if isinstance(obj, dict) else []:
            try:
                i = int(h.get("id")) - 1
            except Exception:
                continue
            if 0 <= i < len(part) and h.get("kelas") in ("inti", "sebut", "lepas"):
                part[i]["ai"] = _ai_hakim_label(part[i]["rule"], h["kelas"], _int(h.get("yakin"), 0) or 0)
                dijawab += 1
    benar_aturan = sum(c["rule"] == c["exp"] for c in kasus)
    benar_ai = sum(c.get("ai", c["rule"]) == c["exp"] for c in kasus)
    ubah = [c for c in kasus if c.get("ai", c["rule"]) != c["rule"]]
    return {"ok": True, "kasus": len(kasus), "dijawab": dijawab, "benar_aturan": benar_aturan, "benar_ai": benar_ai,
            "diubah_ai": len(ubah), "diubah_benar": sum(c["ai"] == c["exp"] for c in ubah), "penyedia": penyedia,
            "galat": galat if dijawab < len(kasus) else None, "detik": round(time.time() - t0, 1), "mode": AI_HAKIM_MODE,
            "saran": ("AI lebih baik dari aturan — boleh setel INTAI_AI_HAKIM=ubah." if benar_ai > benar_aturan and dijawab >= 90
                      else "Biarkan mode bayangan: AI belum terbukti lebih baik dari aturan."),
            "contoh_salah": [{"niche": c["k"], "iklan": (c["sn"]["title"] or c["sn"]["body"]["text"])[:70],
                              "aturan": c["rule"], "ai": c["ai"], "benar": c["exp"]} for c in ubah if c["ai"] != c["exp"]][:12]}


# --- (4) PERAN PENGIKLAN: iklan link-langsung-ke-toko → dari affiliator atau penjual? ---
AI_PERAN_SISTEM = (
    "Kamu menilai iklan Facebook/Instagram yang menautkan produk Shopee. Tentukan siapa pengiklannya:\n"
    "- \"affiliator\": halaman konten/review/racun/rekomendasi, kreator, akun 'spill', 'racun shopee', 'review jujur', "
    "atau menawarkan banyak produk dari toko berbeda — tujuannya komisi, bukan pemilik barang.\n"
    "- \"penjual\": toko/merek pemilik barang (nama halaman = nama toko/merek, menyebut 'toko kami', 'official', "
    "'produksi sendiri', 'reseller dicari', garansi dari toko).\n"
    "- \"ragu\": tidak bisa dipastikan dari teks.\n"
    "Balas JSON: {\"hasil\":[{\"id\":<nomor>,\"peran\":\"affiliator|penjual|ragu\",\"yakin\":<0-100>,\"alasan\":\"<maks 8 kata>\"}]}")
AFV_AI_CALON = {}


def ai_peran(q, email):
    q = re.sub(r"\s+", " ", str(q or "")).strip()[:80]
    if not ai_aktif():
        return {"ok": False, "error": "AI belum diaktifkan."}
    if q not in AFV_AI_CALON:
        afv_analisis(q)
    calon = [x for x in AFV_AI_CALON.get(q, []) if x[0] not in _ai_cache("ai_peran")][:30]
    if not calon:
        return {"ok": True, "dinilai": 0, "sisa": 0}
    daftar = "\n".join(f"{i + 1}. {t}" for i, (_, t) in enumerate(calon))
    obj, info = ai_panggil(AI_PERAN_SISTEM, f"IKLAN:\n{daftar}", tugas="peran", email=email,
                           maks=60 * len(calon) + 200, suhu=0.0, batas=20)
    if obj is None:
        return {"ok": False, "error": info.get("error")}
    baru = {}
    for h in (obj.get("hasil") or []) if isinstance(obj, dict) else []:
        try:
            i = int(h.get("id")) - 1
        except Exception:
            continue
        if 0 <= i < len(calon) and h.get("peran") in ("affiliator", "penjual", "ragu"):
            baru[calon[i][0]] = {"p": h["peran"], "y": max(0, min(100, _int(h.get("yakin"), 0) or 0)),
                                 "a": str(h.get("alasan") or "")[:60]}
    for aid, _ in calon:   # yang tidak dijawab ditandai ragu agar tidak ditanya berulang-ulang
        baru.setdefault(aid, {"p": "ragu", "y": 0, "a": ""})
    _ai_cache_isi("ai_peran", baru)
    return {"ok": True, "dinilai": len(baru), "affiliator": sum(1 for v in baru.values() if v["p"] == "affiliator"),
            "penjual": sum(1 for v in baru.values() if v["p"] == "penjual"), "penyedia": info.get("penyedia")}


# --- (3) SUDUT JUALAN iklan pesaing ---
AI_SUDUT = OrderedDict([
    ("masalah", "Masalah → solusi"), ("promo", "Promo / harga"), ("testimoni", "Testimoni / bukti"),
    ("demo", "Demo / cara pakai"), ("urgensi", "Urgensi / stok terbatas"), ("edukasi", "Edukasi / fakta"),
    ("gaya", "Gaya hidup / emosi"), ("lain", "Lainnya")])
_SUDUT_ATURAN = [
    ("urgensi", r"\b(terbatas|hari ini saja|hanya hari ini|sisa \d+|stok (tinggal|menipis|terbatas)|buruan|segera|last call|flash sale|jam terakhir|kesempatan terakhir)\b"),
    ("promo", r"\b(diskon|promo|gratis ongkir|free ongkir|cashback|potongan|beli \d+ gratis|bonus|murah|hemat|harga spesial|cuma rp|hanya rp|voucher|\d+\s?%)"),
    ("testimoni", r"\b(testi|testimoni|review|ulasan|kata mereka|pelanggan kami|sudah terbukti|terjual \d|rating|bintang 5|pengalaman (saya|aku|gue))\b"),
    ("masalah", r"(\?|\b(capek|susah|ribet|bingung|masalah|kesel|kesal|takut|khawatir|bau|gerah|jerawat|kusam|rontok|sakit|pegal|boros|lelah|stres)\b)"),
    ("demo", r"\b(cara pakai|cara menggunakan|tutorial|lihat (hasilnya|video)|tonton|demo|begini caranya|langkah)\b"),
    ("edukasi", r"\b(tahukah|fakta|manfaat|kandungan|tips|ternyata|penelitian|dokter|ahli|bpom|halal)\b"),
]


def _sudut_aturan(teks):
    t = _norm(teks)
    for k, rx in _SUDUT_ATURAN:
        if re.search(rx, t):
            return k
    return "lain"


AI_SUDUT_SISTEM = (
    "Kamu analis iklan. Untuk setiap iklan, pilih SATU sudut jualan utama (cara iklan membujuk), dari daftar: "
    + ", ".join(f"\"{k}\" ({v})" for k, v in AI_SUDUT.items()) +
    ". Lalu tulis 'pola' = ringkasan gaya pembuka iklan itu, maks 8 kata, bahasa Indonesia, tanpa angka. "
    "Balas JSON: {\"hasil\":[{\"id\":<nomor>,\"sudut\":\"<kode>\",\"pola\":\"...\"}]}")
AI_SUDUT_CACHE = OrderedDict()


def ai_sudut(email, d):
    kata = re.sub(r"\s+", " ", str(d.get("kata") or "")).strip()[:80]
    iklan = [x for x in (d.get("iklan") or []) if isinstance(x, dict) and str(x.get("teks") or "").strip()][:40]
    if len(iklan) < 3:
        return {"ok": False, "error": "Butuh minimal 3 iklan bertulisan untuk dibedah."}
    rows = [{"id": str(x.get("id") or i)[:40], "teks": re.sub(r"\s+", " ", str(x.get("teks")))[:300],
             "umur": max(0, _int(x.get("umur"), 0) or 0), "halaman": str(x.get("halaman") or "")[:60]} for i, x in enumerate(iklan)]
    # v1.5.8: mode banding bebas (mis. TikTok: CTR tinggi vs lainnya) → 'kuat' dipetakan ke umur 30/0 agar logika sama
    lbl_a, lbl_b = "Iklan awet (≥30 hari", "iklan baru (<30 hari"
    if any("kuat" in x for x in iklan):
        for r, x in zip(rows, iklan):
            r["umur"] = 30 if x.get("kuat") else 0
        lbl_a = str(d.get("label_kuat") or "Iklan kuat")[:40] + " ("
        lbl_b = str(d.get("label_lemah") or "lainnya")[:40] + " ("
        lbl_a, lbl_b = lbl_a.rstrip(" ("), lbl_b.rstrip(" (")
        lbl_a, lbl_b = lbl_a + " (", lbl_b + " ("
    kunci = hashlib.sha1((kata + lbl_a + "|" + "|".join(r["id"] + r["teks"][:40] for r in rows)).encode()).hexdigest()
    if kunci in AI_SUDUT_CACHE:
        return dict(AI_SUDUT_CACHE[kunci], cache=True)
    hasil, sumber, info = {}, "aturan", {}
    if ai_aktif():
        daftar = "\n".join(f"{i + 1}. [{r['halaman']}] {r['teks']}" for i, r in enumerate(rows))
        obj, info = ai_panggil(AI_SUDUT_SISTEM, f"Niche: {kata or '-'}\n\nIKLAN:\n{daftar}", tugas="sudut",
                               email=email, maks=50 * len(rows) + 200, suhu=0.1, batas=25)
        for h in ((obj or {}).get("hasil") or []) if isinstance(obj, dict) else []:
            try:
                i = int(h.get("id")) - 1
            except Exception:
                continue
            if 0 <= i < len(rows) and h.get("sudut") in AI_SUDUT:
                hasil[i] = (h["sudut"], re.sub(r"\s+", " ", re.sub(r"\d", "", str(h.get("pola") or ""))).strip(" ,")[:70])
        if hasil:
            sumber = "ai"
    for i, r in enumerate(rows):   # tak dijawab AI / AI mati → aturan kata kunci
        if i not in hasil:
            hasil[i] = (_sudut_aturan(r["teks"]), "")
    per = [{"id": r["id"], "sudut": hasil[i][0], "pola": hasil[i][1], "umur": r["umur"], "halaman": r["halaman"],
            "teks": r["teks"][:140], "ai": sumber == "ai"} for i, r in enumerate(rows)]
    n = len(per)
    lama = [p for p in per if p["umur"] >= 30]
    dist = []
    for k, lbl in AI_SUDUT.items():
        a = [p for p in per if p["sudut"] == k]
        if not a:
            continue
        b = [p for p in lama if p["sudut"] == k]
        dist.append({"kode": k, "label": lbl, "jumlah": len(a), "persen": round(100 * len(a) / n),
                     "lama": len(b), "persen_lama": round(100 * len(b) / len(lama)) if lama else None,
                     "contoh": [{"teks": p["teks"], "halaman": p["halaman"], "umur": p["umur"], "pola": p["pola"]}
                                for p in sorted(a, key=lambda z: -z["umur"])[:3]]})
    dist.sort(key=lambda z: (-z["jumlah"], -(z["lama"] or 0)))
    # ringkasan dihitung INTAI dari hitungan (bukan karangan AI): iklan awet (≥30 hari) vs iklan baru (<30 hari)
    baru_ = [p for p in per if p["umur"] < 30]
    for z in dist:
        b_ = sum(1 for p in baru_ if p["sudut"] == z["kode"])
        z["baru"], z["persen_baru"] = b_, (round(100 * b_ / len(baru_)) if baru_ else None)
    ring = f"Dari {n} iklan, sudut terbanyak: {dist[0]['label']} ({dist[0]['persen']}%)."
    if len(lama) >= 3 and len(baru_) >= 3:
        tl = max(dist, key=lambda z: (z["lama"], z["jumlah"]))
        tb = max(dist, key=lambda z: (z["baru"], z["jumlah"]))
        sep = ", " if lbl_a.endswith("hari") else ""
        ring += (f" {lbl_a}{sep}{len(lama)}): paling banyak {tl['label']} ({tl['persen_lama']}%)"
                 f" · {lbl_b}{', ' if lbl_b.endswith('hari') else ''}{len(baru_)}): {tb['label']} ({tb['persen_baru']}%).")
        if tl["kode"] != tb["kode"]:
            ring += (f" Sudut “{tl['label']}” lebih sering bertahan lama — layak dicoba duluan." if lbl_a.startswith("Iklan awet")
                     else f" Sudut “{tl['label']}” lebih sering muncul di kelompok {lbl_a.rstrip(' (')} — layak dicoba duluan.")
    else:
        ring += (" Iklan awet & iklan baru belum cukup (<3 masing-masing) untuk dibandingkan." if lbl_a.startswith("Iklan awet")
                 else " Kelompok pembanding belum cukup (<3 masing-masing) untuk dibandingkan.")
    out = {"ok": True, "kata": kata, "jumlah": n, "jumlah_lama": len(lama), "sudut": dist, "ringkas": ring,
           "label_a": lbl_a.rstrip(" (") + (")" if "hari" in lbl_a else ""), "label_b": lbl_b.rstrip(" (") + (")" if "hari" in lbl_b else ""),
           "sumber": sumber, "penyedia": info.get("penyedia"),
           "catatan": None if sumber == "ai" else (info.get("error") or "AI belum aktif") + " — dipakai pengelompokan kata kunci 🔵."}
    if sumber == "ai":
        AI_SUDUT_CACHE[kunci] = out
        while len(AI_SUDUT_CACHE) > 200:
            AI_SUDUT_CACHE.popitem(last=False)
    return out


# --- (2) NASKAH VIDEO AI ---
AI_NASKAH_SISTEM = (
    "Kamu penulis naskah video pendek (Shopee Video / Reels / TikTok, 15-30 detik) untuk affiliator & penjual "
    "Indonesia. Gaya bahasa santai, natural, seperti kreator sungguhan.\n"
    "ATURAN KERAS:\n"
    "1. JANGAN mengarang angka, harga, diskon, jumlah terjual, rating, klaim kesehatan/medis, atau sertifikasi yang "
    "tidak ada di FAKTA. Boleh menyebut angka HANYA yang tertulis di FAKTA.\n"
    "2. 'hook' = kalimat pembuka 3 detik pertama yang membuat orang berhenti scroll: pertanyaan, situasi, masalah, "
    "atau kejutan. Hook BUKAN ajakan (jangan 'cek sekarang', 'klik link', 'checkout', 'beli', 'yuk', 'buruan').\n"
    "3. Tiap varian memakai sudut BERBEDA (mis. masalah→solusi, demo, testimoni pribadi, perbandingan, gaya hidup).\n"
    "4. Ajakan (CTA) hanya di 'penutup'.\n"
    "Balas JSON: {\"varian\":[{\"sudut\":\"...\",\"hook\":\"...\",\"adegan\":[{\"detik\":\"0-3\",\"visual\":\"...\","
    "\"ucapan\":\"...\"}],\"penutup\":\"...\",\"caption\":\"...\"}]} — 3 varian, masing-masing 3-5 adegan, caption "
    "maks 150 karakter tanpa tagar.")


def _ai_angka_boleh(fakta_teks):
    return set(re.sub(r"[.,]", "", m) for m in re.findall(r"\d[\d.,]*", fakta_teks))


def _ai_saring_angka(teks, boleh):
    """Kalimat yang memuat ANGKA apa pun (harga, %, watt, mAh, jam, level, terjual…) yang TIDAK ada di fakta produk /
    hook nyata dibuang — AI tidak boleh mengarang spesifikasi atau klaim."""
    out = []
    for k in re.split(r"(?<=[.!?])\s+|\n+", str(teks or "")):
        angka = [re.sub(r"[.,]", "", m) for m in re.findall(r"\d[\d.,]*", k)]
        if all(a in boleh for a in angka):
            out.append(k)
    return " ".join(x.strip() for x in out if x.strip()).strip()


def ai_naskah(email, d):
    nama = re.sub(r"\s+", " ", str(d.get("nama") or "")).strip()[:160]
    if not nama:
        return {"ok": False, "error": "Nama produk kosong."}
    fakta = [f"Produk: {nama}"]
    if d.get("harga"):
        fakta.append(f"Harga: Rp{_int(d.get('harga'), 0):,}".replace(",", "."))
    if d.get("diskon"):
        fakta.append(f"Diskon: {re.sub(r'[^0-9]', '', str(d.get('diskon')))}%")
    if d.get("terjual"):
        fakta.append(f"Terjual: {str(d.get('terjual'))[:30]}")
    if d.get("rating"):
        fakta.append(f"Rating: {str(d.get('rating'))[:6]}")
    if d.get("niche"):
        fakta.append(f"Niche: {str(d.get('niche'))[:60]}")
    hooks = [re.sub(r"\s+", " ", str(h))[:140] for h in (d.get("hooks") or []) if str(h).strip()][:6]
    fakta_teks = "\n".join(fakta)
    prompt = "FAKTA (hanya ini yang boleh disebut):\n" + fakta_teks
    if hooks:
        prompt += "\n\nHOOK NYATA dari iklan/video yang sudah tayang di niche ini (jadikan inspirasi gaya, jangan disalin mentah):\n" + \
                  "\n".join("- " + h for h in hooks)
    prompt += f"\n\nPlatform: {str(d.get('platform') or 'Shopee Video')[:30]}. Tulis 3 varian."
    obj, info = ai_panggil(AI_NASKAH_SISTEM, prompt, tugas="naskah", email=email, maks=2200, suhu=0.8, batas=30)
    if obj is None:
        return {"ok": False, "error": info.get("error"), "jatah": info.get("jatah", False)}
    boleh = _ai_angka_boleh(fakta_teks + "\n" + "\n".join(hooks))   # angka dari fakta produk + hook nyata saja
    varian, dibuang = [], 0
    for v in (obj.get("varian") or [])[:4] if isinstance(obj, dict) else []:
        if not isinstance(v, dict):
            continue
        hook = _ai_saring_angka(re.sub(r"\s+", " ", str(v.get("hook") or "")).strip(), boleh)[:160]
        if len(hook) < 10 or _afv_bukan_hook(hook):
            dibuang += 1
            continue
        adegan = []
        for a in (v.get("adegan") or [])[:6]:
            if not isinstance(a, dict):
                continue
            uc = _ai_saring_angka(str(a.get("ucapan") or ""), boleh)[:240]
            vi = _ai_saring_angka(str(a.get("visual") or ""), boleh)[:200]
            if uc or vi:
                adegan.append({"detik": str(a.get("detik") or "")[:10], "visual": vi, "ucapan": uc})
        varian.append({"sudut": str(v.get("sudut") or "")[:40], "hook": hook, "adegan": adegan,
                       "penutup": _ai_saring_angka(str(v.get("penutup") or ""), boleh)[:200],
                       "caption": _ai_saring_angka(str(v.get("caption") or ""), boleh)[:200]})
    if not varian:
        return {"ok": False, "error": "Jawaban AI tidak lolos penyaring (hook berupa ajakan / angka karangan). Coba lagi."}
    st = ai_status(email)
    return {"ok": True, "varian": varian, "dibuang": dibuang, "penyedia": info.get("penyedia"),
            "sisa": None if is_admin(email) else max(0, AI_PER_AKUN - st["terpakai_akun"])}


# ===== v1.5.8 SCAN TIKTOK (jalur publik, tanpa login) =====
# T1 Creative Center Top Ads (tanda tangan anonim seperti situs resminya) · T7 profil · T8 embed (13 video) · T9 detail video.
# Semua di-cache supaya IP Render tidak dibanjiri; endpoint tidak resmi → gagal dengan tenang (pesan jelas, tanpa data palsu).
TT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
TT_CC = "https://ads.tiktok.com/creative_radar_api/v1/"
TT_SESI = {"web": None, "web_t": 0.0, "uid": secrets.token_hex(16)}
TT_CACHE = OrderedDict()
kunci_tt = threading.Lock()
TT_INDUK_ID = {
    10000000000: "Pendidikan", 11000000000: "Kendaraan & Transportasi", 12000000000: "Bayi, Anak & Ibu",
    13000000000: "Keuangan", 14000000000: "Kecantikan & Perawatan Diri", 15000000000: "Teknologi & Elektronik",
    16000000000: "Peralatan Rumah (Elektronik)", 17000000000: "Travel", 18000000000: "Produk Rumah Tangga",
    19000000000: "Hewan Peliharaan", 20000000000: "Aplikasi", 21000000000: "Perkakas & Perbaikan Rumah",
    22000000000: "Fashion & Aksesori", 23000000000: "Berita & Hiburan", 24000000000: "Layanan Bisnis",
    25000000000: "Game", 26000000000: "Layanan & Jasa", 27000000000: "Makanan & Minuman", 28000000000: "Olahraga & Outdoor",
    29000000000: "Kesehatan", 30000000000: "E-Commerce (non-aplikasi)"}
TT_TUJUAN = {"product_sales": "Penjualan produk", "conversion": "Konversi", "traffic": "Traffic ke situs",
             "app_install": "Instal aplikasi", "reach": "Jangkauan", "video_view": "Tayangan video",
             "lead_generation": "Kumpulkan prospek", "community_interaction": "Interaksi komunitas",
             "app_promotion": "Promosi aplikasi", "web_conversions": "Konversi situs", "catalog_sales": "Penjualan katalog"}
TT_BUDGET = {0: "rendah", 1: "sedang", 2: "tinggi"}
TT_URUT = {"for_you": "for_you", "like": "like", "ctr": "ctr", "impression": "impression"}


def _tt_cache(kunci, ttl, fungsi):
    kini = time.time()
    with kunci_tt:
        c = TT_CACHE.get(kunci)
        if c and kini - c[0] < ttl:
            return c[1]
    v = fungsi()
    with kunci_tt:
        TT_CACHE[kunci] = (kini, v)
        TT_CACHE.move_to_end(kunci)
        while len(TT_CACHE) > 400:
            TT_CACHE.popitem(last=False)
    return v


def _tt_webid(segar=False):
    if not segar and TT_SESI["web"] and time.time() - TT_SESI["web_t"] < 6 * 3600:
        return TT_SESI["web"]
    r = requests.post("https://mcs-sg.tiktokv.com/v1/user/webid", timeout=10, json={
        "app_id": 345918, "url": "https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en",
        "user_agent": TT_UA, "referer": "", "user_unique_id": ""})
    TT_SESI["web"], TT_SESI["web_t"] = str(r.json()["web_id"]), time.time()
    return TT_SESI["web"]


def _tt_tanda(uid, web):
    ts = int(time.time())
    h = hashlib.md5(f"A7B&9z#1G6$2K@8M!3-{web or uid}-{ts}".encode()).hexdigest()
    s = "".join(format(int(h[i], 16) ^ int(h[i + 16], 16), "x") for i in range(16))
    return {"anonymous-user-id": uid, "timestamp": str(ts), "user-sign": s, "web-id": str(web)}


def tt_cc(jalur, ttl=1800):
    """GET Creative Center bertanda tangan → data (dict). Galat sementara (50000) dicoba ulang sekali dgn web_id baru."""
    def ambil():
        galat = None
        for coba in range(2):
            web = _tt_webid(segar=coba > 0)
            r = requests.get(TT_CC + jalur, timeout=15,
                             headers={"User-Agent": TT_UA, "lang": "en", **_tt_tanda(TT_SESI["uid"], web)})
            try:
                j = r.json()
            except Exception:
                galat = f"HTTP {r.status_code}"
                continue
            if j.get("code") == 0:
                return j.get("data") or {}
            galat = f"{j.get('code')} {j.get('msg')}"
            time.sleep(0.8)
        raise RuntimeError("TikTok Creative Center menolak (" + str(galat) + ")")
    return _tt_cache("cc:" + jalur, ttl, ambil)


def tt_filter():
    d = tt_cc("top_ads/v2/filters?country_code=ID", ttl=24 * 3600)
    sub = {}
    for x in d.get("industry") or []:
        if x.get("parent_id"):
            sub.setdefault(x["parent_id"], []).append({"id": str(x["id"]), "nama": x.get("value") or ""})
    induk = [{"id": str(i), "nama": n, "sub": sorted(sub.get(i, []), key=lambda z: z["nama"])}
             for i, n in sorted(TT_INDUK_ID.items(), key=lambda kv: kv[1])]
    return {"ok": True, "induk": induk}


def _tt_label_industri(kunci):
    try:
        i = int(str(kunci or "").replace("label_", ""))
    except Exception:
        return ""
    ind = int(str(i)[:2] + "0" * 9) if len(str(i)) == 11 else 0
    nama_sub = ""
    try:
        for x in (tt_cc("top_ads/v2/filters?country_code=ID", ttl=24 * 3600).get("industry") or []):
            if x.get("id") == i:
                nama_sub = x.get("value") or ""
                break
    except Exception:
        pass
    return (TT_INDUK_ID.get(ind, "") + (" · " + nama_sub if nama_sub and i != ind else "")).strip(" ·")


def _tt_tujuan(k):
    k = str(k or "").replace("campaign_objective_", "")
    return TT_TUJUAN.get(k, k.replace("_", " ").capitalize())


def _tt_judul(t):
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    tagar = re.findall(r"#([^\s#]+)", t)
    bersih = re.sub(r"#[^\s#]+", "", t)
    bersih = re.sub(r"@[\w.]+", lambda m: m.group(0), bersih)
    return re.sub(r"\s+", " ", bersih).strip(" .·-"), [x.lower() for x in tagar][:8]


def tt_iklan(ind="", per="30", urut="for_you", bhs="id", hal="1"):
    per = per if str(per) in ("7", "30", "180") else "30"
    urut = TT_URUT.get(str(urut), "for_you")
    hal = max(1, min(25, _int(hal, 1) or 1))
    q = f"period={per}&page={hal}&limit=20&order_by={urut}&country_code=ID"
    ind = re.sub(r"[^0-9]", "", str(ind or ""))[:12]
    if ind:
        q += "&industry=" + ind
    if bhs in ("id", "en", "ms"):
        q += "&ad_language=" + bhs
    d = tt_cc("top_ads/v2/list?" + q)
    out = []
    for i, m in enumerate(d.get("materials") or []):
        vi = m.get("video_info") or {}
        judul, tagar = _tt_judul(m.get("ad_title"))
        out.append({"id": str(m.get("id") or ""), "judul": judul or "(tanpa teks)", "tagar": tagar,
                    "merek": m.get("brand_name") or "", "like": _int(m.get("like"), 0) or 0,
                    "ctr": m.get("ctr"), "budget": TT_BUDGET.get(m.get("cost")), "budget_n": m.get("cost"),
                    "tujuan": _tt_tujuan(m.get("objective_key")), "kategori": _tt_label_industri(m.get("industry_key")),
                    "cover": vi.get("cover") or "", "video": (vi.get("video_url") or {}).get("720p") or "",
                    "durasi": round(float(vi.get("duration") or 0)), "cari": bool(m.get("is_search")),
                    "peringkat": (hal - 1) * 20 + i + 1})
    pg = d.get("pagination") or {}
    return {"ok": True, "iklan": out, "total": pg.get("total_count"), "ada_lagi": bool(pg.get("has_more")),
            "halaman": hal, "periode": per, "urut": urut, "bahasa": bhs, "industri": ind,
            "industri_label": TT_INDUK_ID.get(int(ind), "") if ind and int(ind) in TT_INDUK_ID else (_tt_label_industri(ind) if ind else ""),
            "jam": _iso_kini()}


def tt_iklan_detail(mid):
    mid = re.sub(r"[^0-9]", "", str(mid or ""))[:25]
    if not mid:
        return {"ok": False, "error": "ID iklan kosong"}
    d = tt_cc("top_ads/v2/detail?material_id=" + mid, ttl=6 * 3600)
    vi = d.get("video_info") or {}
    kk = d.get("keyword_list") or []
    return {"ok": True, "id": mid, "komentar": d.get("comment"), "share": d.get("share"), "like": d.get("like"),
            "landing": d.get("landing_page") or "", "kata_kunci": [str(k) for k in kk][:12] if isinstance(kk, list) else [],
            "sumber": d.get("source") or "", "suara": bool(d.get("voice_over")), "sorotan": d.get("highlight_text") or "",
            "tujuan": [_tt_tujuan(o.get("label")) for o in (d.get("objectives") or []) if isinstance(o, dict)][:4],
            "negara": len(d.get("country_code") or []), "video": (vi.get("video_url") or {}).get("720p") or "",
            "durasi": round(float(vi.get("duration") or 0))}


def _tt_rehidrasi(html):
    m = re.search(r'id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S)
    return (json.loads(m.group(1)).get("__DEFAULT_SCOPE__") or {}) if m else {}


def _tt_nama(u):
    u = str(u or "").strip()
    m = re.search(r"tiktok\.com/@([\w.]+)", u)
    u = m.group(1) if m else u.lstrip("@").split("/")[0].split("?")[0]
    return u.lower() if re.fullmatch(r"[\w.]{2,30}", u or "") else ""


def _tt_video_detail(nama, vid):
    try:
        r = requests.get(f"https://www.tiktok.com/@{nama}/video/{vid}", timeout=10,
                         headers={"User-Agent": TT_UA, "Accept-Language": "id-ID,id;q=0.9"})
        d = _tt_rehidrasi(r.text).get("webapp.video-detail") or {}
        it = (d.get("itemInfo") or {}).get("itemStruct") or {}
        st = it.get("stats") or {}
        if not it:
            return None
        return {"like": _int(st.get("diggCount"), None), "komentar": _int(st.get("commentCount"), None),
                "share": _int(st.get("shareCount"), None), "simpan": _int(st.get("collectCount"), None),
                "tayang": _int(st.get("playCount"), None), "unggah": _int(it.get("createTime"), None),
                "durasi": _int((it.get("video") or {}).get("duration"), None), "iklan": bool(it.get("isAd")),
                "musik": ((it.get("music") or {}).get("title") or "")[:60],
                "toko": bool(it.get("anchors")) or "shop" in json.dumps(it.get("anchors") or "").lower()}
    except Exception:
        return None


def _tt_diag(r, isi=""):
    try:
        return f"HTTP {r.status_code} · {len(r.text) // 1024} KB · {r.url.split('?')[0][-45:]}" + (f" · {isi}" if isi else "")
    except Exception:
        return "?"


def _tt_embed_parse(html, nama):
    m = re.search(r'id="__FRONTITY_CONNECT_STATE__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return {}
    data = ((json.loads(m.group(1)).get("source") or {}).get("data") or {})
    if f"/embed/@{nama}" in data:
        return data[f"/embed/@{nama}"] or {}
    for k, v in data.items():   # kunci beda huruf besar/kecil → ambil yang berisi userInfo
        if isinstance(v, dict) and v.get("userInfo"):
            return v
    return {}


def _tt_profil_embed(nama):
    """v1.5.8b: profil + embed dgn sesi ber-cookie; bila kosong → pemanasan beranda (ttwid) lalu ulang sekali.
    Mengembalikan (profil, embed, diag) — diag dipakai tombol uji admin untuk melihat apa yang diterima server."""
    diag = {}
    s = requests.Session()
    s.headers.update({"User-Agent": TT_UA, "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
                      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    prof, emb = {}, {}
    for coba in range(2):
        if coba:
            try:
                r0 = s.get("https://www.tiktok.com/", timeout=10)
                diag["beranda"] = _tt_diag(r0, "cookie: " + (",".join(sorted(s.cookies.keys()))[:60] or "-"))
            except Exception as e:
                diag["beranda"] = "galat " + str(e)[:60]
        if not prof.get("userInfo") and prof.get("statusCode") != 10221:
            try:
                r = s.get(f"https://www.tiktok.com/@{nama}" + ("?lang=id-ID" if coba else ""), timeout=12)
                sc = _tt_rehidrasi(r.text)
                prof = sc.get("webapp.user-detail") or {}
                diag["profil" + str(coba + 1)] = _tt_diag(r, ("statusCode " + str(prof.get("statusCode")) if prof else
                                                             "tanpa user-detail; scope: " + ",".join(list(sc)[:4]) if sc else
                                                             "tanpa data rehidrasi" + (" (captcha/verifikasi)" if re.search(r"captcha|verify|wafchallenge", r.text[:30000], re.I) else "")))
            except Exception as e:
                diag["profil" + str(coba + 1)] = "galat " + str(e)[:80]
        if not emb.get("userInfo"):
            try:
                r = s.get(f"https://www.tiktok.com/embed/@{nama}", timeout=12)
                emb = _tt_embed_parse(r.text, nama)
                diag["embed" + str(coba + 1)] = _tt_diag(r, f"{len(emb.get('videoList') or [])} video" if emb.get("userInfo") else
                                                         ("ada FRONTITY tapi tanpa userInfo" if "__FRONTITY_CONNECT_STATE__" in r.text else "tanpa FRONTITY"))
            except Exception as e:
                diag["embed" + str(coba + 1)] = "galat " + str(e)[:80]
        if (prof.get("userInfo") or prof.get("statusCode") == 10221) and emb.get("userInfo"):
            break
    return prof, emb, diag


# ===== v1.5.10 AKUN TIKTOK LEWAT JINA READER =====
# IP Render diblokir TikTok → halaman dibaca server Jina (format HTML mentah agar JSON di dalam halaman ikut).
# Jina sering memblokir akses anonim ke "www.tiktok.com" (penyalahgunaan orang lain) → coba "tiktok.com" dulu, lalu www.
def _tt_jina_html(jalan, timeout=40):
    """jalan = '/@nama' dsb. Mengembalikan (html, diag)."""
    hd = {"X-Timeout": "25", "X-Return-Format": "html"}
    kunci = (os.environ.get("INTAI_JINA_KUNCI") or os.environ.get("JINA_API_KEY") or "").strip()
    if kunci:
        hd["Authorization"] = "Bearer " + kunci
    diag = []
    for dom in ("https://tiktok.com", "https://www.tiktok.com"):
        try:
            r = requests.get("https://r.jina.ai/" + dom + jalan, headers=hd, timeout=timeout)
        except Exception as e:
            diag.append(dom[8:] + ": galat " + str(e)[:50])
            continue
        if r.status_code == 200 and len(r.text) > 5000:
            diag.append(dom[8:] + f": HTTP 200 · {len(r.text) // 1024} KB")
            return r.text, " | ".join(diag)
        try:
            pesan = (r.json().get("name") or r.json().get("message") or "")[:60]
        except Exception:
            pesan = r.text[:60].replace("\n", " ")
        diag.append(dom[8:] + f": HTTP {r.status_code} {pesan}")
        if r.status_code == 429:
            break
    return "", " | ".join(diag)


def _tt_video_detail_jina(nama, vid):
    try:
        html, _ = _tt_jina_html(f"/@{nama}/video/{vid}", timeout=35)
        d = _tt_rehidrasi(html).get("webapp.video-detail") or {}
        it = (d.get("itemInfo") or {}).get("itemStruct") or {}
        st = it.get("stats") or {}
        if not it:
            return None
        return {"like": _int(st.get("diggCount"), None), "komentar": _int(st.get("commentCount"), None),
                "share": _int(st.get("shareCount"), None), "simpan": _int(st.get("collectCount"), None),
                "tayang": _int(st.get("playCount"), None), "unggah": _int(it.get("createTime"), None),
                "durasi": _int((it.get("video") or {}).get("duration"), None), "iklan": bool(it.get("isAd")),
                "musik": ((it.get("music") or {}).get("title") or "")[:60],
                "toko": bool(it.get("anchors")) or "shop" in json.dumps(it.get("anchors") or "").lower()}
    except Exception:
        return None


def _tt_profil_jina(nama):
    """(profil, embed, diag) lewat Jina — profil & embed diambil paralel."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fp = ex.submit(_tt_jina_html, f"/@{nama}")
        fe = ex.submit(_tt_jina_html, f"/embed/@{nama}")
        hp, dp = fp.result()
        he, de = fe.result()
    prof = _tt_rehidrasi(hp).get("webapp.user-detail") or {} if hp else {}
    emb = _tt_embed_parse(he, nama) if he else {}
    return prof, emb, {"jina_profil": dp + (" · ada data akun" if prof.get("userInfo") else ""),
                       "jina_embed": de + (f" · {len(emb.get('videoList') or [])} video" if emb.get("userInfo") else "")}


def tt_akun(u):
    nama = _tt_nama(u)
    if not nama:
        return {"ok": False, "error": "Tulis nama akun TikTok (mis. @somethincofficial) atau tempel link profilnya."}

    def ambil():
        prof, emb, diag = _tt_profil_embed(nama)
        jalur = "langsung"
        if not prof.get("userInfo") and not emb.get("userInfo") and prof.get("statusCode") != 10221:
            p2, e2, d2 = _tt_profil_jina(nama)
            diag.update(d2)
            if p2.get("userInfo") or e2.get("userInfo") or p2.get("statusCode") == 10221:
                prof, emb, jalur = p2, e2, "jina"
        if prof.get("statusCode") == 10221 or (not prof.get("userInfo") and not emb.get("userInfo")):
            if prof.get("statusCode") == 10221:
                return {"ok": False, "error": f"Akun @{nama} tidak ditemukan di TikTok."}
            return {"ok": False, "error": "TikTok tidak memberikan data akun saat ini (mungkin dibatasi). Coba lagi nanti.", "diag": diag,
                    "blokir": True, "nama": nama}
        ui = (prof.get("userInfo") or {})
        us, st, eu = ui.get("user") or {}, ui.get("stats") or {}, emb.get("userInfo") or {}
        cu = us.get("commerceUserInfo") or {}
        akun = {"nama": us.get("uniqueId") or eu.get("uniqueId") or nama, "tampil": us.get("nickname") or eu.get("nickname") or nama,
                "centang": bool(us.get("verified") if us else eu.get("verified")),
                "bio": (us.get("signature") or eu.get("signature") or "")[:300],
                "link_bio": ((us.get("bioLink") or {}).get("link") or "")[:120],
                "avatar": us.get("avatarThumb") or eu.get("avatarThumbUrl") or "",
                "pengikut": _int(st.get("followerCount") or eu.get("followerCount"), None),
                "mengikuti": _int(st.get("followingCount") or eu.get("followingCount"), None),
                "like": _int(st.get("heartCount") or eu.get("heartCount"), None),
                "video_total": _int(st.get("videoCount"), None),
                "penjual": bool(us.get("ttSeller")), "bisnis": bool(cu.get("commerceUser")),
                "kategori": cu.get("category") or "", "organisasi": bool(us.get("isOrganization")),
                "dibuat": _int(us.get("createTime"), None), "privat": bool(us.get("privateAccount") or eu.get("privateAccount"))}
        vl = [v for v in (emb.get("videoList") or []) if isinstance(v, dict) and v.get("id")][:13]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            fdet = _tt_video_detail_jina if jalur == "jina" else _tt_video_detail
            det = list(ex.map(lambda v: fdet(akun["nama"], v["id"]), vl))
        video = []
        for v, dd in zip(vl, det):
            dd = dd or {}
            judul, tagar = _tt_judul(v.get("desc"))
            video.append({"id": str(v["id"]), "caption": judul, "tagar": tagar, "cover": v.get("coverUrl") or "",
                          "tayang": dd.get("tayang") if dd.get("tayang") is not None else _int(v.get("playCount"), None),
                          "like": dd.get("like"), "komentar": dd.get("komentar"), "share": dd.get("share"),
                          "simpan": dd.get("simpan"), "unggah": dd.get("unggah"), "durasi": dd.get("durasi"),
                          "iklan": dd.get("iklan"), "musik": dd.get("musik") or "", "detail": bool(dd),
                          "url": f"https://www.tiktok.com/@{akun['nama']}/video/{v['id']}"})
        video.sort(key=lambda x: -(x["unggah"] or 0))   # embed = campuran disematkan/populer/baru → urut tanggal
        # angka turunan (🔵 dihitung INTAI dari video sampel)
        ty = sorted(x["tayang"] for x in video if x["tayang"] is not None)
        er = [(x["like"] + (x["komentar"] or 0) + (x["share"] or 0)) / x["tayang"] for x in video
              if x["like"] is not None and x["tayang"]]
        ung = sorted(x["unggah"] for x in video if x["unggah"])
        ring = {"sampel": len(video), "median_tayang": ty[len(ty) // 2] if ty else None,
                "tayang_maks": ty[-1] if ty else None,
                "interaksi": round(100 * sorted(er)[len(er) // 2], 2) if er else None,
                "dipromosikan": sum(1 for x in video if x["iklan"]), "dicek_iklan": sum(1 for x in video if x["detail"]),
                "terbaru_hari": int((time.time() - ung[-1]) // 86400) if ung else None,
                "per_bulan": round(akun["video_total"] / max((time.time() - akun["dibuat"]) / 2629800, 1), 1)
                if akun.get("video_total") and akun.get("dibuat") else None}
        return {"ok": True, "akun": akun, "video": video, "ringkas": ring, "jam": _iso_kini(), "jalur": jalur}
    hasil = _tt_cache("akun:" + nama, 1800, ambil)
    if not hasil.get("ok"):   # galat jangan di-cache lama
        with kunci_tt:
            TT_CACHE.pop("akun:" + nama, None)
    return hasil


# ===== v1.5.9 TOKOPEDIA TERLARIS (lewat Jina Reader) =====
# Render & sandbox diblokir Tokopedia (timeout) → halaman cari dibaca oleh server Jina Reader (r.jina.ai) lalu diurai di sini.
# Tanpa kunci: 20 permintaan/menit per IP (cukup, karena hasil di-cache 6 jam). Kunci opsional: INTAI_JINA_KUNCI.
JINA_KUNCI = (os.environ.get("INTAI_JINA_KUNCI") or os.environ.get("JINA_API_KEY") or "").strip()
TOKPED_URUT = {"laris": "5", "sesuai": "23"}
TOKPED_URUT_LBL = {"laris": "Terlaris", "sesuai": "Paling sesuai"}
_KOTA_RE = re.compile(r"\s((?:Kab\.|Kota|Kabupaten)\s.+|Jakarta\s(?:Selatan|Barat|Timur|Utara|Pusat)|Tangerang Selatan|Bandung Barat|"
                      r"Banda Aceh|Bandar Lampung|Pangkal Pinang|Tanjung Pinang|Tanjung Balai|Pematang Siantar|Padang Sidempuan|"
                      r"Kota Baru|Labuhan Batu|Tebing Tinggi|Gunung Sitoli|[A-Z][A-Za-z.-]+)$")


def _tokped_angka_terjual(t):
    """'100rb+' → 100000 · '1,2rb+' → 1200 · '30+' → 30 · '4' → 4 (batas bawah, sesuai pembulatan Tokopedia)."""
    m = re.match(r"([\d.,]+)\s*(rb|jt)?", str(t or "").lower())
    if not m:
        return None
    try:
        v = float(m.group(1).replace(".", "").replace(",", ".")) if m.group(2) else float(m.group(1).replace(".", "").replace(",", ""))
    except Exception:
        return None
    return int(v * (1000 if m.group(2) == "rb" else 1000000 if m.group(2) == "jt" else 1))


def _tokped_rp(t):
    try:
        return int(str(t).replace("Rp", "").replace(".", "").strip())
    except Exception:
        return None


def _tokped_urai(md):
    out, lihat = [], set()
    for par in re.split(r"\n\s*\n", md or ""):
        par = par.strip()
        if not par.startswith("[") or "](https://www.tokopedia.com/" not in par or "product-image" not in par:
            continue
        mlink = re.search(r"\]\((https://www\.tokopedia\.com/[^)\s]+)\)\s*$", par)
        if not mlink:
            continue
        link = mlink.group(1).split("?")[0]
        mimg = re.search(r"product-image\]\((https://[^)\s]+)\)", par)
        teks = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", par[1:mlink.start()])
        teks = re.sub(r"\s+", " ", teks).strip()
        mrp = list(re.finditer(r"Rp[\d.]+", teks))
        if not mrp or link in lihat:
            continue
        lihat.add(link)
        kepala = teks[:mrp[0].start()].strip()
        md_ = re.match(r"(\d{1,2})%\s+", kepala)
        diskon = int(md_.group(1)) if md_ else None
        nama = kepala[md_.end():].strip() if md_ else kepala
        harga = _tokped_rp(mrp[0].group(0))
        coret = None
        if len(mrp) > 1 and teks[mrp[0].end():mrp[1].start()].strip() == "":
            coret = _tokped_rp(mrp[1].group(0))
        ekor = teks[(mrp[1] if coret else mrp[0]).end():].strip()
        mt = re.search(r"(?:(\d\.\d)\s+)?([\d.,]+\s*(?:rb|jt)?\+?)\s*terjual\s*(.*)$", ekor, re.I)
        rating, terjual, sisa, label = None, None, "", ekor
        if mt:
            rating = float(mt.group(1)) if mt.group(1) else None
            terjual = re.sub(r"\s+", "", mt.group(2))
            sisa = mt.group(3).strip()
            label = ekor[:mt.start()].strip()
        else:
            mr = re.search(r"(\d\.\d)\s+(.*)$", ekor)
            if mr:
                rating, sisa, label = float(mr.group(1)), mr.group(2).strip(), ekor[:mr.start()].strip()
        toko, kota = sisa, ""
        mk = _KOTA_RE.search(" " + sisa)
        if mk and mk.start() > 0:
            kota, toko = mk.group(1).strip(), (" " + sisa)[:mk.start()].strip()
        lbl = [x for x in ("Bisa COD", "Pakai Bonus", "Cashback", "Gratis Ongkir", "Hemat s.d \\d+%") if re.search(x, label)]
        lbl = [re.search(x, label).group(0) for x in lbl]
        slug = link.split("tokopedia.com/")[1].split("/")[0]
        out.append({"posisi": len(out) + 1, "nama": nama[:200], "harga": harga, "harga_coret": coret, "diskon": diskon,
                    "rating": rating, "terjual_teks": terjual, "terjual_min": _tokped_angka_terjual(terjual),
                    "toko": toko[:80] or slug, "toko_slug": slug, "kota": kota[:40], "label": lbl[:4],
                    "gambar": mimg.group(1) if mimg else "", "url": link,
                    "resmi": bool(re.search(r"official|resmi", toko, re.I))})
    return out


def tokped_cari(q, urut="laris"):
    q = re.sub(r"\s+", " ", str(q or "")).strip()[:80]
    urut = urut if urut in TOKPED_URUT else "laris"
    if len(q) < 2:
        return {"ok": False, "error": "Ketik kata kunci produk minimal 2 huruf."}

    def ambil():
        url = "https://www.tokopedia.com/search?q=" + requests.utils.quote(q) + "&ob=" + TOKPED_URUT[urut]
        hd = {"X-Timeout": "30", "X-Remove-Overlay": "true"}
        if JINA_KUNCI:
            hd["Authorization"] = "Bearer " + JINA_KUNCI
        galat = None
        t0 = time.time()
        for coba in range(2):
            if coba and time.time() - t0 > 35:   # jaga total < ±80 dtk (proxy Render memutus di 100 dtk)
                break
            try:
                r = requests.get("https://r.jina.ai/" + url, headers=hd, timeout=40)
            except Exception as e:
                galat = "Pembaca halaman tidak menjawab (" + str(e)[:60] + ")"
                continue
            if r.status_code == 429:
                galat = "Batas baca per menit sedang penuh — coba lagi 1 menit lagi."
                break
            if r.status_code != 200:
                try:
                    galat = (r.json().get("readableMessage") or r.json().get("message") or "")[:160]
                except Exception:
                    galat = f"HTTP {r.status_code}"
                galat = "Pembaca halaman menolak (" + (galat or str(r.status_code)) + ")"
                break
            produk = _tokped_urai(r.text)
            if produk:
                return {"ok": True, "produk": produk}
            galat = "Halaman Tokopedia terbaca tapi daftar produknya kosong (mungkin halaman verifikasi)."
            time.sleep(1.5)
        raise RuntimeError(galat or "Tokopedia belum terbaca")

    hasil = _tt_cache(f"tokped:{urut}:{q.lower()}", 6 * 3600, ambil)
    pr = hasil["produk"]
    hg = sorted(p["harga"] for p in pr if p["harga"])
    toko = {}
    for p in pr:
        t = toko.setdefault(p["toko_slug"], {"toko": p["toko"], "slug": p["toko_slug"], "kota": p["kota"], "produk": 0, "terjual_min": 0})
        t["produk"] += 1
        t["terjual_min"] += p["terjual_min"] or 0
    dom = sorted(toko.values(), key=lambda t: (-t["produk"], -t["terjual_min"]))[:5]
    laris = max(pr, key=lambda p: p["terjual_min"] or 0)
    return {"ok": True, "q": q, "urut": urut, "urut_label": TOKPED_URUT_LBL[urut], "produk": pr,
            "ringkas": {"jumlah": len(pr), "harga": {"min": hg[0], "median": hg[len(hg) // 2], "max": hg[-1]} if hg else None,
                        "terlaris": {"nama": laris["nama"], "terjual_teks": laris["terjual_teks"]} if laris.get("terjual_min") else None,
                        "diskon": sum(1 for p in pr if p["diskon"]), "toko": dom, "resmi": sum(1 for p in pr if p["resmi"])},
            "jam": _iso_kini(), "kunci": bool(JINA_KUNCI)}


def tt_uji():
    """Admin: cek tiap jalur TikTok (+ Tokopedia) dari server ini. v1.5.10: semua baris jalan paralel (proxy Render putus di 100 dtk)."""
    tugas = []

    def uji(nama, f):
        tugas.append((nama, f))

    def _jalan(nf):
        nama, f = nf
        t0 = time.time()
        try:
            return {"jalur": nama, "ok": True, "ket": f(), "ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            return {"jalur": nama, "ok": False, "ket": str(e)[:700], "ms": int((time.time() - t0) * 1000)}
    with kunci_tt:
        for k in [k for k in TT_CACHE if k.startswith(("cc:top_ads/v2/list?period=7&page=1&limit=20&order_by=like", "akun:somethincofficial"))]:
            TT_CACHE.pop(k, None)
    uji("Creative Center: Top Ads", lambda: f"{len(tt_iklan(per='7', urut='like', bhs='id')['iklan'])} iklan")

    def akun():
        a = tt_akun("somethincofficial")
        if not a.get("ok"):
            raise RuntimeError(str(a.get("error")) + " | " + " · ".join(f"{k}: {v}" for k, v in (a.get("diag") or {}).items()))
        return (f"{a['akun']['pengikut']} pengikut · {len(a['video'])} video · {a['ringkas']['dicek_iklan']} detail video · jalur "
                + ("Jina Reader (cadangan)" if a.get("jalur") == "jina" else "langsung"))
    uji("Profil + video akun", akun)

    def oembed():
        r = requests.get("https://www.tiktok.com/oembed?url=https://www.tiktok.com/@somethincofficial", timeout=10, headers={"User-Agent": TT_UA})
        return _tt_diag(r, "author: " + str((r.json() if r.status_code == 200 else {}).get("author_name")))
    uji("TikTok oEmbed (resmi)", oembed)

    def tokped_jina():
        with kunci_tt:
            TT_CACHE.pop("tokped:laris:madu anak", None)
        d = tokped_cari("madu anak", "laris")
        if not d.get("ok"):
            raise RuntimeError(d.get("error"))
        t = d["ringkas"]["terlaris"] or {}
        return f"{len(d['produk'])} produk · terlaris: {t.get('nama', '-')[:40]} ({t.get('terjual_teks', '-')} terjual)" + (" · kunci Jina" if d["kunci"] else " · tanpa kunci")
    uji("Tokopedia lewat Jina Reader", tokped_jina)

    def akun_jina():
        prof, emb, diag = _tt_profil_jina("somethincofficial")
        ui = (prof.get("userInfo") or {})
        st, eu = ui.get("stats") or {}, emb.get("userInfo") or {}
        if not ui and not eu:
            raise RuntimeError("tidak terbaca — " + diag.get("jina_profil", "") + " ‖ " + diag.get("jina_embed", ""))
        vl = emb.get("videoList") or []
        dd = _tt_video_detail_jina("somethincofficial", vl[0]["id"]) if vl else None
        kunci = bool((os.environ.get("INTAI_JINA_KUNCI") or os.environ.get("JINA_API_KEY") or "").strip())
        pg = f"{_int(st.get('followerCount') or eu.get('followerCount'), 0):,}".replace(",", ".")
        return (f"pengikut {pg} · {_int(st.get('videoCount'), 0) or '?'} video total · "
                f"{len(vl)} video sampel · detail video: " + (f"like {dd['like']}, iklan {'ya' if dd['iklan'] else 'tidak'}" if dd else "gagal") +
                (" · kunci Jina" if kunci else " · tanpa kunci"))
    uji("TikTok akun lewat Jina Reader", akun_jina)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tugas)) as ex:
        hasil = list(ex.map(_jalan, tugas))
    return {"ok": True, "hasil": hasil}


def shopee_uji():
    """Admin → Status sistem: uji semua jalur Shopee langsung dari server ini."""
    hasil = []

    def uji(nama, fn):
        t0 = time.time()
        try:
            ket = fn()
            hasil.append({"jalur": nama, "ok": True, "ms": int((time.time() - t0) * 1000), "ket": ket})
        except Exception as e:   # sengaja lebar: ini alat diagnosa
            hasil.append({"jalur": nama, "ok": False, "ms": int((time.time() - t0) * 1000), "ket": str(e)[:160]})

    uji("Pencarian (kartu produk)", lambda: f"{len(_shp_halaman_kartu(SHP_BASIS + '/search?keyword=madu&sortBy=sales', 'cari')[0])} kartu")
    uji("Terlaris toko", lambda: f"{len(_shp_halaman_kartu(SHP_BASIS + '/shop/150222332/search?sortBy=sales', 'toko_laris')[0])} kartu")
    uji("Flash Sale", lambda: f"{len(_shp_fs_ambil()[1])} item")

    def _saran():
        with _shp_lock:
            SHP_CACHE.pop(("saran", "madu"), None)
            SHP_GAGAL.pop(("saran", "madu"), None)
        k = shp_saran("madu")
        if not k:
            raise ShopeeTutup("0 saran")
        return f"{len(k)} kata kunci"
    uji("Saran kata kunci", _saran)

    def _aff():
        if not shp_aff_aktif():
            raise ShopeeTutup("belum diisi (env INTAI_SHOPEE_AFF_ID / INTAI_SHOPEE_AFF_SECRET)")
        a, p = shp_aff_cari("madu")
        if a is None:
            raise ShopeeTutup(p)
        return f"{len(a)} produk dengan angka terjual resmi"
    uji("Affiliate Open API (resmi)", _aff)
    return {"ok": True, "hasil": hasil, "waktu": _iso_kini()}


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

    def kirim_video(self, target):
        """v1.4.7b: alirkan video dari CDN Meta (tanpa simpan di RAM). Range dari browser diteruskan apa adanya
        → CDN yang memotong (termasuk "bytes=-500" yang dulu salah dilayani)."""
        hdr = {"User-Agent": mesin.UA, "Referer": "https://www.facebook.com/", "Accept-Encoding": "identity"}
        rng = (self.headers.get("Range") or "").strip()
        if re.fullmatch(r"bytes=(\d+-\d*|-\d+)", rng):
            hdr["Range"] = rng
        r = None
        try:
            r = _get_media(target, _HOST_VIDEO, timeout=(10, 30), headers=hdr) if target else None
        except Exception:
            r = None
        if r is None or r.status_code not in (200, 206, 416):
            if r is not None:
                r.close()
            self.tulis(404, b"Gagal memuat video", "text/plain")
            return
        with r:
            ctype = (r.headers.get("Content-Type") or "video/mp4").split(";")[0].strip().lower()
            if ctype.startswith("text/") or "html" in ctype or "json" in ctype or "xml" in ctype:
                self.tulis(404, b"Gagal memuat video", "text/plain")
                return
            if not ctype.startswith("video/"):
                ctype = "video/mp4"
            self.send_response(r.status_code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            for k in ("Content-Length", "Content-Range"):
                if r.headers.get(k):
                    self.send_header(k, r.headers[k])
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            terkirim = 0
            try:
                for ch in r.iter_content(65536):
                    self.wfile.write(ch)
                    terkirim += len(ch)
                    if terkirim > VIDEO_MAKS:
                        break
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def ip_klien(self):
        """v1.4.7b: IP asli pengguna. Di Render: True-Client-IP (ditimpa Render) / CF-Connecting-IP (pemalsuan ditolak
        Cloudflare). X-Forwarded-For paling kiri BISA DIPALSUKAN → hanya cadangan saat jalan lokal."""
        for h in ("True-Client-IP", "CF-Connecting-IP"):
            v = (self.headers.get(h) or "").strip()
            if v and len(v) <= 64:
                return v
        xff = [x.strip() for x in (self.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
        return xff[0][:64] if xff else self.client_address[0]

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
        elif u.path == "/api/shopee/cari":   # v1.5.0
            prm = parse_qs(u.query)
            h_ = shopee_cari(prm.get("q", [""])[0], prm.get("urut", ["laris"])[0], self.email)
            if h_.get("ok") and self.email:   # v1.5.4 dasbor Shopee: potret per akun (latar, gagal = diabaikan)
                threading.Thread(target=shp_akun_catat_cari, args=(self.email, h_), daemon=True).start()
            self.json_ok(h_)
        elif u.path == "/api/shopee/toko":
            h_ = shopee_toko(parse_qs(u.query).get("id", [""])[0], self.email)
            if h_.get("ok") and self.email:
                threading.Thread(target=shp_akun_catat_toko, args=(self.email, h_), daemon=True).start()
            self.json_ok(h_)
        elif u.path == "/api/shopee/dasbor":   # v1.5.4
            self.json_ok(shp_dasbor(self.email) if self.email else {"ok": False, "error": "Silakan masuk dulu"})
        elif u.path == "/api/shopee/flash":
            self.json_ok(shopee_flash())
        elif u.path == "/api/shopee/afv/scan":   # v1.5.6 Scan Affiliator (Meta, disimpan terpisah)
            try:
                self.json_ok(afv_scan_antre(parse_qs(u.query).get("q", [""])[0], self.email))
            except Exception as e_:
                self.json_ok({"ok": False, "error": "Scan affiliator gagal: " + str(e_)[:120]})
        elif u.path == "/api/shopee/video":   # v1.5.6 Shopee Video per tagar (halaman SEO publik)
            try:
                self.json_ok(sv_tagar(parse_qs(u.query).get("q", [""])[0]))
            except Exception as e_:
                self.json_ok({"ok": False, "error": "Shopee Video gagal dibaca: " + str(e_)[:120]})
        elif u.path.startswith("/api/tiktok/"):   # v1.5.8 Scan TikTok
            qs = parse_qs(u.query)
            g = lambda k, d="": qs.get(k, [d])[0]
            try:
                if u.path == "/api/tiktok/filter":
                    self.json_ok(tt_filter())
                elif u.path == "/api/tiktok/iklan":
                    self.json_ok(tt_iklan(g("ind"), g("per", "30"), g("urut", "for_you"), g("bhs", "id"), g("hal", "1")))
                elif u.path == "/api/tiktok/detail":
                    self.json_ok(tt_iklan_detail(g("id")))
                elif u.path == "/api/tiktok/akun":
                    self.json_ok(tt_akun(g("u")))
                else:
                    self.tulis(404, json.dumps({"ok": False, "error": "Tidak ditemukan"}), "application/json")
            except Exception as e_:
                self.json_ok({"ok": False, "error": "TikTok tidak bisa dibaca saat ini: " + str(e_)[:140]})
        elif u.path == "/api/tokped/cari":   # v1.5.9
            qs = parse_qs(u.query)
            try:
                self.json_ok(tokped_cari(qs.get("q", [""])[0], qs.get("urut", ["laris"])[0]))
            except Exception as e_:
                self.json_ok({"ok": False, "error": str(e_)[:200]})
        elif u.path == "/api/admin/tiktok-uji":   # v1.5.8
            if not is_admin(self.email):
                self.tulis(403, json.dumps({"ok": False, "error": "Khusus admin"}), "application/json")
            else:
                self.json_ok(tt_uji())
        elif u.path == "/api/ai/status":   # v1.5.7
            self.json_ok(ai_status(self.email))
        elif u.path == "/api/ai/peran":   # v1.5.7 tebak peran pengiklan link-langsung (affiliator/penjual)
            try:
                self.json_ok(ai_peran(parse_qs(u.query).get("q", [""])[0], self.email))
            except Exception as e_:
                self.json_ok({"ok": False, "error": "AI peran gagal: " + str(e_)[:120]})
        elif u.path == "/api/shopee/afv":   # v1.5.5 Affiliator & Video
            try:
                self.json_ok(afv_analisis(parse_qs(u.query).get("q", [""])[0]))
            except Exception as e_:
                self.json_ok({"ok": False, "error": "Analisis affiliator gagal: " + str(e_)[:120]})
        elif u.path == "/api/admin/ai-uji":   # v1.5.7
            if not is_admin(self.email):
                self.tulis(403, json.dumps({"ok": False, "error": "Khusus admin"}), "application/json")
            else:
                self.json_ok(ai_uji_emas(self.email))
        elif u.path == "/api/admin/shopee-uji":
            if not is_admin(self.email):
                self.tulis(403, json.dumps({"ok": False, "error": "Khusus admin"}), "application/json")
            else:
                self.json_ok(shopee_uji())
        elif u.path in ("/api/admin/mesin", "/api/admin/mesin-pulih", "/api/admin/sc-uji"):   # v1.5.1
            if not is_admin(self.email):
                self.tulis(403, json.dumps({"ok": False, "error": "Khusus admin"}), "application/json")
            elif u.path == "/api/admin/mesin":
                self.json_ok(dict(mesin_status(), ok=True))
            elif u.path == "/api/admin/mesin-pulih":
                self.json_ok(mesin_pulih())
            else:
                self.json_ok(sc_uji())
        elif u.path == "/api/jadwal":
            self.tulis(200, json.dumps(jadwal_publik(self.email), ensure_ascii=False), "application/json")
        elif u.path == "/api/jadwal/tick":
            kunci = parse_qs(u.query).get("kunci", [""])[0]
            if JADWAL_KUNCI and not hmac.compare_digest(kunci.encode(), JADWAL_KUNCI.encode()):
                self.tulis(403, json.dumps({"ok": False, "pesan": "kunci salah"}), "application/json")
                return
            try:   # v1.5.0: potret Flash Sale Shopee 1×/hari (thread latar, tidak memperlambat cron)
                shopee_potret_latar()
            except Exception as e:
                print("[INTAI SHOPEE] potret latar gagal:", type(e).__name__)
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
            nilai = nilai[:200]
            hasil = scan_dengan_antre(nilai, mode, self.email, qs.get("segar", [""])[0] == "1")
            self.tulis(200, json.dumps(hasil, ensure_ascii=False), "application/json")
        elif u.path == "/img":
            target = parse_qs(u.query).get("u", [""])[0]
            data = ambil_gambar(target) if target else None
            if data:
                self.tulis(200, data[1], data[0])
            else:
                self.tulis(404, b"Gagal memuat gambar", "text/plain")
        elif u.path == "/vid":
            self.kirim_video(parse_qs(u.query).get("u", [""])[0])
        else:
            self.tulis(404, "Not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        if not self.gerbang(u.path, ("/api/login", "/api/register", "/api/logout", "/api/lupa", "/api/reset")):
            return
        if u.path == "/api/lupa":   # v1.4.5
            d = self.baca_json()
            ip = self.ip_klien()
            self.json_ok(lupa_password(d.get("email"), ip, basis_url(self)))
            return
        if u.path == "/api/reset":   # v1.4.5
            d = self.baca_json()
            ip = self.ip_klien()
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
            email = str(d.get("email") or "").strip().lower()[:254]
            pw = str(d.get("password") or "")[:200]
            ip = self.ip_klien()
            tunggu = login_terkunci(email, ip)
            if tunggu:
                self.json_ok({"ok": False, "error": f"Terlalu banyak percobaan masuk. Coba lagi dalam {tunggu} menit, atau pakai Lupa password."})
                return
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
                login_catat(email, ip, True)
                self.tulis(200, json.dumps({"ok": True, "token": tok, "nama": nama, "email": email}),
                           "application/json", [self.cookie_sesi(tok, SESI_HARI * 86400)])
            else:
                login_catat(email, ip, False)
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
            if not nama or len(pw) < 6:
                self.tulis(200, json.dumps({"ok": False, "error": "Data belum lengkap (password min. 6 karakter)"}),
                           "application/json")
                return
            if not email_valid(email):   # v1.4.7b: dulu email berisi HTML / 300 karakter diterima
                self.json_ok({"ok": False, "error": "Format email tidak valid. Contoh: nama@gmail.com"})
                return
            if len(nama) > 60 or len(pw) > 200:
                self.json_ok({"ok": False, "error": "Nama maksimal 60 karakter, password maksimal 200 karakter."})
                return
            h_pw = hash_pw(pw)  # sekali saja (salt acak) — v1.4.7b: di LUAR lock (PBKDF2 ±0,1 dtk)
            with lock:
                if email in AKUN_DB:
                    self.tulis(200, json.dumps({"ok": False, "error": "Email sudah terdaftar di mesin ini"}),
                               "application/json")
                    return
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
        elif u.path in ("/api/ai/naskah", "/api/ai/sudut"):   # v1.5.7
            try:
                d_ = self.baca_json()
                self.json_ok(ai_naskah(self.email, d_) if u.path.endswith("naskah") else ai_sudut(self.email, d_))
            except Exception as e_:
                self.json_ok({"ok": False, "error": "AI gagal: " + str(e_)[:120]})
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
