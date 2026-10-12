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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote

import requests
import intai_mvp as mesin

PORT = int(os.environ.get("PORT") or 8765)   # env PORT utk deploy (HF Spaces/Render set otomatis)
VERSI = "1.0"
FOTO_MAKS = 12
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BERKAS_DATA = os.path.join(BASE_DIR, "intai_data.json")
lock = threading.RLock()   # reentrant: jalankan_scan dipanggil di bawah lock, catat_scan ikut lock
img_cache = {}
url_cache = {}
AKUN_DB = {}    # email -> {"nama": str, "hash": sha256}
TOKENS = {}     # token -> email
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
  const kol = ["status","halaman","format","funnel","relevansi_label","relevansi_nilai","skor_cuan","skor_jual","umur_hari","varian","mulai","akhir","harga_min","harga_max","est_terjual_min","est_terjual_max","cta","platform","link","teks_ringkas"];
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
            cards.append({"title": c.get("title") or "", "body": c.get("body") or "",
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
        "title": teks(snap, "title"),
        "body": teks(snap, "body"),
        "caption": teks(snap, "caption"),
        "link_desc": teks(snap, "link_description"),
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


def jalankan_scan(nilai, mode="cari"):
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
        teks_bersih = re.sub(r"\{\{[^}]*\}\}", "…", b["teks"] or "")
        teks_bersih = re.sub(r"\s+", " ", teks_bersih).strip()
        umur, tgl = b["umur_hari"], b["tgl_mulai"]
        if umur is None or umur < 0 or umur > 3650:
            umur, tgl = 0, "?"
        out.append({
            "aktif": b.get("aktif") is not False,
            "halaman": b["halaman"], "teks": teks_bersih, "format": b["format"],
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
            label_produk = re.sub(r"\{\{[^}]*\}\}", "…", det.get("title") or x["teks"][:30] or "")
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
    try:
        sejak_lalu = catat_scan(label, mode, len(rows_hidup), indeks_kini)
    except Exception:
        sejak_lalu = None
    # v1.0 P1: arsipkan semua iklan + pengiklan + potret niche (latar, best-effort)
    threading.Thread(target=arsipkan_scan, daemon=True, name="arsip",
                     args=(out, label, mode, len(rows_hidup), ekonomi_niche)).start()

    return {
        "ok": True, "kata": label, "jumlah": len(rows_hidup), "sejak_lalu": sejak_lalu,
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
            "last_seen": [{"kata": k[0], "mode": k[1], "ids": v}
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
                    LAST_SEEN[(e["kata"], e["mode"])] = ke_indeks(e.get("ids"))
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
                    params="?select=kata,mode,waktu,jumlah,baru,hilang&order=id.asc&limit=1000")
    if ok and isinstance(rows, list) and rows:
        with lock:
            RIWAYAT.clear()
            for r in rows:
                RIWAYAT.append({k: r.get(k) for k in ("kata", "mode", "waktu", "jumlah", "baru", "hilang")})
    else:
        terapkan_lokal()
        if lokal.get("riwayat"):
            supa("POST", "intai_riwayat", payload=lokal["riwayat"])  # seed massal

    # --- last_seen ---
    ok, ls = supa("GET", "intai_last_seen", params="?select=kata,mode,ids&limit=1000")
    if ok and isinstance(ls, list) and ls:
        with lock:
            LAST_SEEN.clear()
            for e in ls:
                try:
                    LAST_SEEN[(e["kata"], e["mode"])] = ke_indeks(e.get("ids"))
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


def catat_scan(label, mode, jumlah, indeks):
    """Catat hasil scan ke riwayat mesin (sumber data dashboard /api/ringkasan).
    delta baru/hilang dihitung dari perbandingan archive_id vs scan sebelumnya
    dengan kata/mode yang sama. v0.8: indeks = {archive_id: {"h", "hr"}} →
    return `sejak_lalu` (nama yg baru/hilang + harga yg berubah) atau None (scan pertama)."""
    indeks = ke_indeks(indeks)
    with lock:
        kunci = (label, mode)
        prev = LAST_SEEN.get(kunci)
        waktu_lalu = jumlah_lalu = None
        for r in reversed(RIWAYAT):
            if r.get("kata") == label and r.get("mode") == mode:
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
            "jumlah": jumlah, "baru": baru, "hilang": hilang,
        })
        if len(RIWAYAT) > 500:
            del RIWAYAT[:len(RIWAYAT) - 500]
    simpan_data()
    if supa_aktif():
        supa("POST", "intai_riwayat", payload={
            "kata": label, "mode": mode, "waktu": RIWAYAT[-1]["waktu"] if RIWAYAT else "",
            "jumlah": jumlah, "baru": baru, "hilang": hilang})
        supa("POST", "intai_last_seen", payload={"kata": label, "mode": mode, "ids": indeks},
             prefer="return=minimal,resolution=merge-duplicates")
    return sejak_lalu


# ======================= ARSIP v1.0 (P1): database yang tumbuh =======================
# Tiap scan: semua iklan (aktif & mati) + pengiklan + potret angka niche disimpan.
# Supabase bila terpasang (tabel: SUPABASE-ARSIP.sql), kalau tidak → intai_arsip.json lokal.
# Best-effort & di thread latar: arsip gagal TIDAK boleh menggagalkan scan.
BERKAS_ARSIP = os.path.join(BASE_DIR, "intai_arsip.json")
ARSIP_MAKS_LOKAL = 20000
kunci_arsip = threading.Lock()
ARSIP_LOKAL = {"dimuat": False, "iklan": {}, "pengiklan": {}, "snapshot": []}
ARSIP_STATUS = {"terakhir_ok": None, "terakhir_galat": "", "terakhir_waktu": None}
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


def baris_arsip_iklan(x, label, mode, kini):
    """1 iklan hasil scan → 1 baris intai_iklan (kunci & tipe SELALU sama: syarat upsert massal PostgREST)."""
    det = x.get("detail") or {}
    aid = str(det.get("archive_id") or "").strip()
    if not aid:
        return None
    tgl = str(x.get("tgl") or det.get("mulai") or "")
    teks = det.get("body") or det.get("title") or x.get("teks") or ""
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
        ARSIP_LOKAL["iklan"] = d.get("iklan") or {}
        ARSIP_LOKAL["pengiklan"] = d.get("pengiklan") or {}
        ARSIP_LOKAL["snapshot"] = d.get("snapshot") or []
    except Exception:
        pass


def _simpan_arsip_lokal(iklan, pengiklan, snap, kini):
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
    if len(ARSIP_LOKAL["iklan"]) > ARSIP_MAKS_LOKAL:  # buang yg paling lama tak terlihat
        urut = sorted(ARSIP_LOKAL["iklan"].items(), key=lambda kv: kv[1].get("terakhir_terlihat") or "")
        ARSIP_LOKAL["iklan"] = dict(urut[-ARSIP_MAKS_LOKAL:])
    try:
        with open(BERKAS_ARSIP + ".tmp", "w", encoding="utf-8") as f:
            json.dump({k: ARSIP_LOKAL[k] for k in ("iklan", "pengiklan", "snapshot")}, f, ensure_ascii=False)
        os.replace(BERKAS_ARSIP + ".tmp", BERKAS_ARSIP)
        return True, ""
    except Exception as e:
        return False, str(e)[:120]


def arsipkan_scan(out, label, mode, jumlah, ekonomi_niche):
    """Dipanggil di thread latar sesudah scan. Return (ok, galat)."""
    kini = _iso_kini()
    try:
        iklan, lihat = [], set()
        for x in out:
            b = baris_arsip_iklan(x, label, mode, kini)
            if b and b["archive_id"] not in lihat:
                lihat.add(b["archive_id"])
                iklan.append(b)
        pengiklan = baris_arsip_pengiklan(out, label, kini)
        snap = ringkas_snapshot(out, label, mode, jumlah, ekonomi_niche, kini)
        if mode == "halaman":
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
                for i in range(0, len(iklan), 200):
                    ok, d = supa("POST", "intai_iklan", payload=iklan[i:i + 200], prefer=gabung)
                    if not ok:
                        galat.append("iklan: " + str(d)[:160])
                        break
                if pengiklan:
                    ok, d = supa("POST", "intai_pengiklan", payload=pengiklan, prefer=gabung)
                    if not ok:
                        galat.append("pengiklan: " + str(d)[:160])
                ok, d = supa("POST", "intai_snapshot", payload=snap)
                if not ok:
                    galat.append("snapshot: " + str(d)[:160])
                hasil = (not galat, "; ".join(galat))
            else:
                hasil = _simpan_arsip_lokal(iklan, pengiklan, snap, kini)
    except Exception as e:
        hasil = (False, "arsip: " + str(e)[:160])
    ARSIP_STATUS.update({"terakhir_ok": hasil[0], "terakhir_galat": hasil[1], "terakhir_waktu": kini})
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
    else:
        with kunci_arsip:
            _muat_arsip_lokal()
            ik = ARSIP_LOKAL["iklan"]
            sn = ARSIP_LOKAL["snapshot"]
            d = {"ok": True, "sumber": "lokal", "siap": True, "iklan": len(ik),
                 "iklan_aktif": sum(1 for v in ik.values() if v.get("aktif")),
                 "pengiklan": len(ARSIP_LOKAL["pengiklan"]), "snapshot": len(sn),
                 "sejak": (sn[0].get("waktu") if sn else None),
                 "niche": len({x.get("niche") for x in sn if x.get("mode") != "halaman"})}
    d["status_terakhir"] = dict(ARSIP_STATUS)
    _ARSIP_CACHE.update({"t": time.time(), "data": d})
    return d


def ringkasan_api():
    """Struktur persis kontrak dashboard: niche/total_scan/total_iklan/delta/
    terakhir/daftar[{kata,mode,scan[]}] (total_iklan = jumlah scan TERAKHIR per niche)."""
    with lock:
        grup = {}
        for r in RIWAYAT:
            g = grup.setdefault(r["kata"], {"kata": r["kata"], "mode": r["mode"], "scan": []})
            g["mode"] = r["mode"]
            g["scan"].append({k: r[k] for k in ("waktu", "jumlah", "baru", "hilang")})
        daftar = list(grup.values())
        total_iklan = sum(g["scan"][-1]["jumlah"] for g in daftar)
        terakhir = dict(RIWAYAT[-1]) if RIWAYAT else None
        return {
            "ok": True, "niche": len(daftar), "total_scan": len(RIWAYAT),
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def tulis(self, kode, isi, tipe):
        self.send_response(kode)
        self.send_header("Content-Type", tipe)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")  # preview iframe (origin null)
        self.end_headers()
        self.wfile.write(isi if isinstance(isi, bytes) else isi.encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def baca_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html", "/dashboard", "/meta", "/shopee", "/tiktok"):
            self.tulis(200, muat_halaman(), "text/html; charset=utf-8")
        elif u.path == "/api/versi":
            self.tulis(200, json.dumps({"ok": True, "v": VERSI}), "application/json")
        elif u.path == "/api/me":
            tok = parse_qs(u.query).get("token", [""])[0]
            with lock:
                email = TOKENS.get(tok)
                akun = AKUN_DB.get(email) if email else None
            if akun:
                self.tulis(200, json.dumps({"ok": True, "nama": akun["nama"], "email": email}),
                           "application/json")
            else:
                self.tulis(200, json.dumps({"ok": False}), "application/json")
        elif u.path == "/api/arsip":
            self.tulis(200, json.dumps(statistik_arsip(), ensure_ascii=False), "application/json")
        elif u.path == "/api/ringkasan":
            self.tulis(200, json.dumps(ringkasan_api(), ensure_ascii=False), "application/json")
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
                    hasil = jalankan_scan(nilai, mode)
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
        if u.path == "/api/login":
            d = self.baca_json()
            email = str(d.get("email") or "").strip().lower()
            pw = str(d.get("password") or "")
            with lock:
                akun = dict(AKUN_DB.get(email) or {})
            oke, upgrade = cek_pw(pw, akun.get("hash")) if akun else (False, False)  # hashing di luar lock
            if oke:
                with lock:
                    tok = secrets.token_hex(16)
                    TOKENS[tok] = email
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
            if oke:
                self.tulis(200, json.dumps({"ok": True, "token": tok, "nama": nama, "email": email}),
                           "application/json")
            else:
                self.tulis(200, json.dumps({"ok": False, "error": "Email atau password salah"}),
                           "application/json")
        elif u.path == "/api/register":
            d = self.baca_json()
            nama = str(d.get("nama") or "").strip()
            email = str(d.get("email") or "").strip().lower()
            pw = str(d.get("password") or "")
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
                tok = secrets.token_hex(16)
                TOKENS[tok] = email
            simpan_data()
            if supa_aktif():
                supa("POST", "intai_akun",
                     payload={"email": email, "nama": nama, "hash": h_pw},
                     prefer="return=minimal,resolution=merge-duplicates")
            self.tulis(200, json.dumps({"ok": True, "token": tok, "nama": nama, "email": email}),
                       "application/json")
        elif u.path == "/api/logout":
            d = self.baca_json()
            with lock:
                TOKENS.pop(str(d.get("token") or ""), None)
            self.tulis(200, json.dumps({"ok": True}), "application/json")
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
