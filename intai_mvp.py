#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
INTAI MVP — mesin pengikis Meta Ad Library
===========================================
Rekonstruksi dari spesifikasi kontrak API yang dipakai intai_app.py v0.5
(asli: C:\\Users\\hp\\Downloads\\intai-paket\\intai\\intai_mvp.py).

Dipakai oleh intai_app.py sebagai modul `mesin`:
  UA, buka_sesi(), ambil_halaman(s, kata, negara), ekstrak_iklan(html),
  rapihkan(mentah), hitung_skor(rows), main() (CLI).
"""

import argparse
import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
         "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]

DOMAIN_MP = ("shopee", "tokopedia", "lazada", "bukalapak", "blibli",
             "shope.ee", "tiktok.com/shop")
DOMAIN_WA = ("wa.me", "api.whatsapp.com", "whatsapp.com", "wa.link")

PAT_ARCHIVE_ID = re.compile(r'"ad_archive_id"\s*:\s*"?(\d+)"?')
PAT_VERIFY = re.compile(r"fetch\('(/__rd_verify_[^']+)'")


def buka_sesi():
    """Session browser-like + cookie dasar dari halaman depan FB."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.facebook.com/",
    })
    try:
        s.get("https://www.facebook.com/", timeout=15)
    except Exception:
        pass
    return s


def ambil_halaman(s, kata, negara="ID"):
    """GET halaman hasil Ad Library utk kata kunci; lolos challenge anti-bot.
    Return (html, url)."""
    url = ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
           f"&country={negara}&q={quote(kata)}&search_type=keyword_unordered&media_type=all")
    r = s.get(url, timeout=30)
    for _ in range(6):
        if r.status_code == 200 and len(r.content) > 50000:
            return r.text, url
        if r.status_code == 403 and "__rd_verify" in r.text:
            m = PAT_VERIFY.search(r.text)
            if m:
                try:
                    s.post("https://www.facebook.com" + m.group(1), timeout=25)
                except Exception:
                    pass
        time.sleep(2)
        r = s.get(url, timeout=30)
    return r.text, url


def objek_json_dari(html, pos):
    """Ambil objek JSON lengkap yang mengandung posisi ini: cari '{' mundur,
    maju sampai brace seimbang, lalu json.loads dengan guard."""
    mundur = html.rfind("{", 0, pos)
    if mundur < 0:
        return None
    dalam = 0
    dalam_str = False
    esc = False
    for i in range(mundur, min(mundur + 300000, len(html))):
        c = html[i]
        if dalam_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                dalam_str = False
            continue
        if c == '"':
            dalam_str = True
        elif c == "{":
            dalam += 1
        elif c == "}":
            dalam -= 1
            if dalam == 0:
                potongan = html[mundur:i + 1]
                try:
                    obj = json.loads(potongan)
                    if isinstance(obj, dict):
                        return obj
                except Exception:
                    return None
    return None


def ekstrak_iklan(html):
    """Kumpulkan objek iklan unik dari HTML Ad Library via ad_archive_id."""
    lihat, hasil = set(), []
    for m in PAT_ARCHIVE_ID.finditer(html):
        aid = m.group(1)
        if aid in lihat:
            continue
        lihat.add(aid)
        obj = objek_json_dari(html, m.start())
        if isinstance(obj, dict) and obj.get("snapshot"):
            hasil.append(obj)
    return hasil


def _teks(snap, kunci):
    v = snap.get(kunci) or ""
    if isinstance(v, dict):
        v = v.get("text") or ""
    return v if isinstance(v, str) else str(v)


def _klasifikasi_funnel(link, cta):
    u = (link or "").lower()
    cta = (cta or "").lower()
    if any(d in u for d in DOMAIN_WA) or "whatsapp" in cta:
        return "CTWA/WA"
    if any(d in u for d in DOMAIN_MP):
        return "Marketplace"
    if u.startswith("http"):
        return "LP/Website"
    return "Tanpa tautan"


def rapihkan(mentah):
    """Ubah list objek mentah -> baris datar dengan kunci standar."""
    rows = []
    for o in mentah:
        snap = o.get("snapshot") or {}
        link = snap.get("link_url") or ""
        cta_type = str(snap.get("cta_type") or "")
        fmt_raw = str(snap.get("display_format") or "").upper()
        if "VIDEO" in fmt_raw:
            fmt = "Video"
        elif "CAROUSEL" in fmt_raw or "DPA" in fmt_raw or "DCO" in fmt_raw:
            fmt = "CAROUSEL" if "CAROUSEL" in fmt_raw else fmt_raw
        elif "IMAGE" in fmt_raw:
            fmt = "IMAGE"
        else:
            fmt = fmt_raw or "?"
        start = o.get("start_date")
        umur = None
        tgl = "?"
        try:
            if start:
                d0 = datetime.fromtimestamp(int(start), tz=timezone.utc)
                tgl = d0.strftime("%Y-%m-%d")
                umur = (datetime.now(timezone.utc) - d0).days
        except Exception:
            pass
        platform = ", ".join(
            p.replace("AUDIENCE_NETWORK", "AN") for p in (o.get("publisher_platform") or []))
        rows.append({
            "halaman": o.get("page_name") or "(tanpa nama)",
            "teks": " ".join(t for t in (_teks(snap, "title"), _teks(snap, "body"),
                                         _teks(snap, "caption")) if t)[:300],
            "format": fmt,
            "funnel": _klasifikasi_funnel(link, cta_type),
            "link": link,
            "tgl_mulai": tgl,
            "umur_hari": umur if umur is not None and umur >= 0 else 0,
            "varian_batch": o.get("collation_count") or 1,
            "platform": platform,
            "archive_id": o.get("ad_archive_id"),
        })
    return rows


def hitung_skor(rows):
    """Skor Cuan 0-100 = umur(50) + varian(25) + funnel(15) + video(10)."""
    hasil = []
    for r in rows:
        umur = r.get("umur_hari") or 0
        varian = r.get("varian_batch") or 0
        skor = (min(umur / 60, 1) * 50
                + min(varian / 10, 1) * 25
                + {"CTWA/WA": 15, "Marketplace": 10, "LP/Website": 5}.get(r["funnel"], 0)
                + (10 if r["format"] == "Video" else 4))
        r["skor"] = round(skor)
        hasil.append(r)
    hasil.sort(key=lambda x: -x["skor"])
    return hasil


def main():
    ap = argparse.ArgumentParser(description="INTAI — scanner iklan Meta Ad Library")
    ap.add_argument("kata", help="kata kunci pencarian")
    ap.add_argument("--negara", default="ID", help="kode negara (default: ID)")
    ap.add_argument("--top", type=int, default=20, help="jumlah baris teratas")
    ap.add_argument("--debug", action="store_true", help="simpan HTML mentah")
    a = ap.parse_args()

    print(f"[INTAI] memindai: {a.kata!r} (negara: {a.negara}) …")
    s = buka_sesi()
    html, url = ambil_halaman(s, a.kata, a.negara)
    if len(html) < 50000:
        print("[INTAI] GAGAL: Ad Library tidak merespons / diblokir (coba lagi nanti)")
        if a.debug:
            os.makedirs("hasil", exist_ok=True)
            with open("hasil/debug.html", "w", encoding="utf-8") as f:
                f.write(html)
        return 1
    if a.debug:
        os.makedirs("hasil", exist_ok=True)
        with open("hasil/debug.html", "w", encoding="utf-8") as f:
            f.write(html)

    mentah = ekstrak_iklan(html)
    rows = hitung_skor(rapihkan(mentah))
    if not rows:
        print("[INTAI] 0 iklan ditemukan.")
        return 0

    print(f"[INTAI] {len(rows)} iklan aktif ditemukan\n")
    print(f"{'#':<3} {'SKOR':<5} {'UMUR':<5} {'FUNNEL':<13} {'FORMAT':<9} HALAMAN")
    for i, r in enumerate(rows[:a.top], 1):
        print(f"{i:<3} {r['skor']:<5} {r['umur_hari']}h   {r['funnel']:<13} "
              f"{r['format']:<9} {r['halaman'][:44]}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    slug = re.sub(r"\W+", "-", a.kata.lower()).strip("-")[:40] or "scan"
    os.makedirs("hasil", exist_ok=True)
    csv_path = f"hasil/intai_{slug}_{stamp}.csv"
    json_path = f"hasil/intai_{slug}_{stamp}.json"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"\n[INTAI] tersimpan: {csv_path} · {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
