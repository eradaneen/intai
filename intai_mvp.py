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


def _buka_url(s, url):
    """Buka URL Ad Library + lewati tantangan anti-bot + pulangkan HTML.
    (dari intai_mvp.py asli kamar sebelah)"""
    r = s.get(url, timeout=30)
    for _ in range(5):
        if r.status_code == 200 and len(r.content) > 50000:
            return r.text
        if r.status_code == 403 and "__rd_verify" in r.text:
            m = PAT_VERIFY.search(r.text)
            if m:
                try:
                    s.post("https://www.facebook.com" + m.group(1), timeout=25)
                except Exception:
                    pass
        time.sleep(2)
        r = s.get(url, timeout=30)
    return r.text


def kumpul_iklan(s, kata_kunci, negara="ID"):
    """B5+B6: tarik dari 3 variasi filter media + active_status=all (ikut iklan mati),
    gabung & buang duplikat. Best-effort — satu variasi gagal = dilewati.
    Catatan: GraphQL pagination dibatasi rate-limit Meta (code 1675004),
    jadi volume dinaikkan lewat variasi filter HTML yang aman.
    (dari intai_mvp.py asli kamar sebelah)"""
    hasil, lihat = [], set()
    for media in ("all", "video", "image"):
        url = ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
               f"&country={negara}&q={quote(kata_kunci)}"
               f"&search_type=keyword_unordered&media_type={media}")
        try:
            html = _buka_url(s, url)
            for o in ekstrak_iklan(html):
                aid = str(o.get("ad_archive_id") or "")
                if aid and aid not in lihat:
                    lihat.add(aid)
                    hasil.append(o)
        except Exception:
            pass
        time.sleep(3)
    return hasil


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
            "aktif": bool(o.get("is_active")),  # dari intai_mvp.py asli (kamar sebelah)
        })
    return rows


def hitung_skor(rows):
    """Skor Cuan 0-100 = umur(50) + varian(25) + funnel(15) + video(10).
    v0.8: komponen varian = jumlah iklan halaman yg sama dlm scan (rumus intai_mvp.py ASLI);
    ambang MENANG/NAIK di app di-tuning dgn rumus ini (median Cuan ±66).
    varian_batch (collation_count) tetap disimpan utk tampilan & Skor Jual."""
    per_hal = {}
    for r in rows:
        per_hal[r.get("halaman")] = per_hal.get(r.get("halaman"), 0) + 1
    hasil = []
    for r in rows:
        umur = r.get("umur_hari") or 0
        r["varian_halaman"] = per_hal.get(r.get("halaman"), 1)
        varian = r["varian_halaman"]
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


# ===== v1.6 mesin tajam: banyak "halaman pertama" berbeda (paralel, ada batas waktu, ada rem) =====
# Meta hanya memberi halaman pertama (±30 iklan) untuk pengunjung tanpa login — halaman lanjutan
# (GraphQL AdLibrarySearchPaginationQuery) ditolak "Rate limit exceeded 1675004" bahkan di browser asli.
# Jadi data ditambah dengan kata kunci utama × variasi media + kata kunci senada.
# REM: bila IP kebanyakan permintaan, Meta TIDAK error — ia kirim halaman normal dengan
#      "search_results_connection":{"count":2761,"edges":[]} (jumlah ada, isi kosong). Itu dideteksi → rehat.
import copy as _copy
import threading as _th

MEDIA_UTAMA = ("all", "video", "image", "meme")
REM = {"jejak": [], "rehat_sampai": 0.0, "kena": 0}
REM_KUNCI = _th.Lock()
REM_PER_MENIT = 24          # jatah permintaan Meta per 60 dtk untuk SELURUH server (±2 scan penuh)
REM_PER_10MENIT = 90         # ±7 scan penuh per 10 menit; selebihnya otomatis mode hemat (3 permintaan)
REM_REHAT_DTK = 15 * 60     # rehat sesudah ketahuan dibatasi (naik 2× tiap kena lagi, maks 60 mnt)
CACHE_V2 = {}               # (kata, negara) -> (t, hasil, info)
CACHE_V2_DTK = 20 * 60


class MetaMenahan(RuntimeError):
    """Meta sedang membatasi IP server ini (isi pencarian dikosongkan)."""


def rem_status():
    with REM_KUNCI:
        kini = time.time()
        REM["jejak"] = [t for t in REM["jejak"] if kini - t < 600]
        return {"rehat_detik": max(0, int(REM["rehat_sampai"] - kini)),
                "menit_ini": sum(1 for t in REM["jejak"] if kini - t < 60),
                "sepuluh_menit": len(REM["jejak"]), "kena": REM["kena"]}


def _rem_catat():
    with REM_KUNCI:
        REM["jejak"].append(time.time())


def _rem_kena():
    with REM_KUNCI:
        if REM["rehat_sampai"] > time.time():
            return   # permintaan paralel yang ditolak bersamaan = 1 kejadian, bukan 3
        REM["kena"] += 1
        lama = min(REM_REHAT_DTK * (2 ** (REM["kena"] - 1)), 3600)
        REM["rehat_sampai"] = max(REM["rehat_sampai"], time.time() + lama)


def _rem_pulih():
    with REM_KUNCI:
        REM["kena"] = 0


def _rem_jatah():
    """Berapa permintaan boleh dipakai scan ini (0 = sedang rehat)."""
    st = rem_status()
    if st["rehat_detik"]:
        return 0
    return max(0, min(REM_PER_MENIT - st["menit_ini"], REM_PER_10MENIT - st["sepuluh_menit"]))


def ditahan_meta(html):
    return bool(re.search(r'"search_results_connection":\{"count":[1-9]\d*,"edges":\[\]', html or ""))


def _ambil_kartu(s, q, media, negara="ID"):
    url = ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
           f"&country={negara}&q={quote(q)}&search_type=keyword_unordered&media_type={media}")
    _rem_catat()
    html = _buka_url(s, url)
    if len(html) < 50000:
        raise RuntimeError("halaman pendek / ditolak")
    if ditahan_meta(html):
        _rem_kena()
        raise MetaMenahan("Meta membatasi sementara")
    return ekstrak_iklan(html)


def rencana_v2(kata_kunci, senada, jatah):
    """Urutan prioritas: data paling banyak per permintaan dulu."""
    penuh = ([(kata_kunci, "all"), (kata_kunci, "video"), (kata_kunci, "image")]
             + [(w, "all") for w in senada[:3]] + [(kata_kunci, "meme")]
             + [(w, "all") for w in senada[3:6]] + [(w, "video") for w in senada[:2]])
    return penuh[:max(0, min(jatah, len(penuh)))]


def kumpul_iklan_v2(kata_kunci, negara="ID", senada=(), batas_waktu=40, jalur=3):
    """Return (iklan_mentah, info). Tiap iklan diberi '_sumber' = kata senada yang menemukannya ('' = kata utama).
    Rem: rehat → MetaMenahan; jatah menipis → paket diperkecil; gagal 3× beruntun → sisa dilewati."""
    import concurrent.futures as _cf
    t0 = time.time()
    kata_kunci = kata_kunci.strip()
    senada = [w for w in senada if w and w.lower() != kata_kunci.lower()][:6]
    kunci = (kata_kunci.lower(), negara)
    c = CACHE_V2.get(kunci)
    if c and time.time() - c[0] < CACHE_V2_DTK:
        info = dict(c[2], cache=True, umur_cache=int(time.time() - c[0]))
        return _copy.deepcopy(c[1]), info
    jatah = _rem_jatah()
    if jatah <= 0:
        raise MetaMenahan("rehat")
    tugas = rencana_v2(kata_kunci, senada, jatah if jatah >= 6 else 3)
    jalur = max(1, min(jalur, len(tugas)))
    with _cf.ThreadPoolExecutor(jalur) as ex:
        sesi = list(ex.map(lambda _: buka_sesi(), range(jalur)))
    hasil, lihat, log = [], set(), []
    gagal = [0]
    ditahan = [False]

    def kerja(i, q, media):
        if time.time() - t0 > batas_waktu or gagal[0] >= 3 or ditahan[0]:
            return q, media, None, "dilewati"
        try:
            ik = _ambil_kartu(sesi[i % jalur], q, media, negara)
            gagal[0] = 0
            return q, media, ik, "ok"
        except MetaMenahan:
            ditahan[0] = True
            return q, media, None, "ditahan"
        except Exception as e:
            gagal[0] += 1
            return q, media, None, str(e)[:60]

    with _cf.ThreadPoolExecutor(jalur) as ex:
        futs = [ex.submit(kerja, i, q, m) for i, (q, m) in enumerate(tugas)]
        for f in _cf.as_completed(futs):
            q, media, ik, st = f.result()
            baru = 0
            for o in ik or []:
                aid = str(o.get("ad_archive_id") or "")
                if aid and aid not in lihat:
                    lihat.add(aid)
                    o["_sumber"] = "" if q.lower() == kata_kunci.lower() else q
                    hasil.append(o)
                    baru += 1
            log.append({"q": q, "media": media, "n": len(ik or []), "baru": baru, "status": st})
    ok = sum(1 for l in log if l["status"] == "ok")
    dipakai = sorted({q for q, _ in tugas if q.lower() != kata_kunci.lower()}, key=senada.index)
    info = {"detik": round(time.time() - t0, 1), "senada": dipakai, "permintaan": len(tugas),
            "berhasil": ok, "ditahan": ditahan[0], "log": log}
    if not hasil and ditahan[0]:
        raise MetaMenahan("Meta membatasi sementara")
    if ok and not ditahan[0]:
        _rem_pulih()
    if hasil and not ditahan[0] and ok >= max(1, len(tugas) // 2):
        CACHE_V2[kunci] = (time.time(), _copy.deepcopy(hasil), info)
        for k in [k for k, v in CACHE_V2.items() if time.time() - v[0] > CACHE_V2_DTK]:
            CACHE_V2.pop(k, None)
        while len(CACHE_V2) > 12:   # RAM Render terbatas: buang yang paling tua
            CACHE_V2.pop(min(CACHE_V2, key=lambda k: CACHE_V2[k][0]), None)
    return hasil, info


# ===== v1.5.1 CADANGAN BERBAYAR: ScrapeCreators (aktif hanya bila env INTAI_SC_KUNCI diisi) =====
# Dipakai HANYA saat Meta menahan IP server / pengumpul gratis gagal total. Isi data = Ad Library Meta yang sama
# (objek mentah identik: ad_archive_id, snapshot, start_date…), jadi saringan & skor INTAI tetap berlaku.
# Biaya: 1 kredit per permintaan (±30 iklan). Rem anggaran: INTAI_SC_HARIAN kredit/hari (default 200).
SC_URL = "https://api.scrapecreators.com/v1/facebook/adLibrary"
SC = {"kunci": (os.environ.get("INTAI_SC_KUNCI") or "").strip(),
      "harian": int(os.environ.get("INTAI_SC_HARIAN") or 200),
      "hari": "", "pakai": 0, "sisa": None, "galat": "", "t_galat": None, "t_ok": None}
SC_KUNCI = _th.Lock()


def sc_aktif():
    return bool(SC["kunci"])


def sc_hari_ini():
    return datetime.fromtimestamp(time.time() + 7 * 3600, tz=timezone.utc).strftime("%Y-%m-%d")   # WIB


def sc_status():
    with SC_KUNCI:
        if SC["hari"] != sc_hari_ini():
            SC["hari"], SC["pakai"] = sc_hari_ini(), 0
        return {"aktif": sc_aktif(), "harian": SC["harian"], "pakai_hari_ini": SC["pakai"], "hari": SC["hari"],
                "sisa_saldo": SC["sisa"], "galat": SC["galat"], "t_galat": SC["t_galat"], "t_ok": SC["t_ok"]}


def _sc_jatah(n):
    """Pesan n kredit dari anggaran harian. Return jumlah yang boleh dipakai (0..n)."""
    with SC_KUNCI:
        if SC["hari"] != sc_hari_ini():
            SC["hari"], SC["pakai"] = sc_hari_ini(), 0
        boleh = max(0, min(n, SC["harian"] - SC["pakai"]))
        SC["pakai"] += boleh
        return boleh


def _sc_get(jalur, prm):
    if not sc_aktif():
        raise RuntimeError("cadangan belum diatur")
    r = requests.get(SC_URL + jalur, params=prm, headers={"x-api-key": SC["kunci"]}, timeout=45)
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code != 200 or not d.get("success", True):
        pesan = str(d.get("message") or d.get("error") or f"HTTP {r.status_code}")[:120]
        with SC_KUNCI:
            SC["galat"], SC["t_galat"] = pesan, datetime.now(timezone.utc).isoformat()[:19]
        raise RuntimeError(pesan)
    with SC_KUNCI:
        if d.get("credits_remaining") is not None:
            SC["sisa"] = d.get("credits_remaining")
        SC["galat"], SC["t_ok"] = "", datetime.now(timezone.utc).isoformat()[:19]
    return d


def _sc_daftar(d):
    return [o for o in (d.get("searchResults") or d.get("results") or []) if isinstance(o, dict) and o.get("snapshot")]


def sc_cari(q, negara="ID", media="ALL", cursor=None):
    prm = {"query": q, "country": negara, "status": "ALL", "media_type": media, "search_type": "keyword_unordered"}
    if cursor:
        prm["cursor"] = cursor
    d = _sc_get("/search/ads", prm)
    return _sc_daftar(d), d.get("cursor")


def kumpul_iklan_sc(kata_kunci, negara="ID", senada=(), maks=6):
    """Cadangan: kata utama 3 halaman (cursor — halaman lanjutan yang tak bisa diambil gratis) + 3 kata senada.
    Return (iklan, info) dengan format sama seperti kumpul_iklan_v2."""
    import concurrent.futures as _cf
    t0 = time.time()
    kata_kunci = kata_kunci.strip()
    senada = [w for w in senada if w and w.lower() != kata_kunci.lower()][:3]
    kunci = (kata_kunci.lower(), negara)
    c = CACHE_V2.get(kunci)
    if c and time.time() - c[0] < CACHE_V2_DTK:
        return _copy.deepcopy(c[1]), dict(c[2], cache=True, umur_cache=int(time.time() - c[0]))
    kredit = _sc_jatah(maks)
    if kredit <= 0:
        raise RuntimeError("anggaran cadangan hari ini habis")
    halaman_utama = min(3, kredit)                      # kata utama didahulukan (6 kredit → 3 halaman + 3 senada)
    senada = senada[:kredit - halaman_utama]
    hasil, lihat, log = [], set(), []
    dipakai = [0]

    def rantai_utama():
        out, cur = [], None
        for i in range(halaman_utama):
            try:
                ik, cur = sc_cari(kata_kunci, negara, "ALL", cur)
                dipakai[0] += 1
                out.append((kata_kunci, f"hal {i + 1}", ik, "ok"))
            except Exception as e:
                out.append((kata_kunci, f"hal {i + 1}", None, str(e)[:60]))
                break
            if not cur:
                break
        return out

    def satu(w):
        try:
            ik, _ = sc_cari(w, negara, "ALL")
            dipakai[0] += 1
            return [(w, "hal 1", ik, "ok")]
        except Exception as e:
            return [(w, "hal 1", None, str(e)[:60])]

    with _cf.ThreadPoolExecutor(1 + len(senada)) as ex:
        futs = [ex.submit(rantai_utama)] + [ex.submit(satu, w) for w in senada]
        for f in _cf.as_completed(futs):
            for q, ket, ik, st in f.result():
                baru = 0
                for o in ik or []:
                    aid = str(o.get("ad_archive_id") or "")
                    if aid and aid not in lihat:
                        lihat.add(aid)
                        o["_sumber"] = "" if q.lower() == kata_kunci.lower() else q
                        hasil.append(o)
                        baru += 1
                log.append({"q": q, "media": ket, "n": len(ik or []), "baru": baru, "status": st})
    with SC_KUNCI:   # kredit yang dipesan tapi tidak terpakai dikembalikan ke anggaran
        SC["pakai"] = max(0, SC["pakai"] - (kredit - dipakai[0]))
    ok = sum(1 for l in log if l["status"] == "ok")
    info = {"detik": round(time.time() - t0, 1), "senada": senada, "permintaan": len(log), "berhasil": ok,
            "kredit": dipakai[0], "cadangan_berbayar": True, "log": log}
    if hasil and ok:
        CACHE_V2[kunci] = (time.time(), _copy.deepcopy(hasil), info)
    return hasil, info


def sc_halaman(page_id, negara="ID", maks=2):
    """Cadangan untuk Intip Kompetitor / Pesaing Dipantau: semua iklan satu halaman (≤2 kredit)."""
    kredit = _sc_jatah(maks)
    out, lihat, cur, dipakai = [], set(), None, 0
    for _ in range(kredit):
        prm = {"pageId": page_id, "country": negara, "status": "ALL"}
        if cur:
            prm["cursor"] = cur
        try:
            d = _sc_get("/company/ads", prm)
        except Exception:
            break
        dipakai += 1
        for o in _sc_daftar(d):
            aid = str(o.get("ad_archive_id") or "")
            if aid and aid not in lihat:
                lihat.add(aid)
                out.append(o)
        cur = d.get("cursor")
        if not cur:
            break
    with SC_KUNCI:
        SC["pakai"] = max(0, SC["pakai"] - (kredit - dipakai))
    return out, dipakai
