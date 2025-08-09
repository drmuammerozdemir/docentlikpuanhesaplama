# -*- coding: utf-8 -*-
"""
Doçentlik Puan Hesaplayıcı (Sağlık Bilimleri – TABLO 10 / 2025)
----------------------------------------------------------------
Bu Streamlit uygulaması, ÜAK 2025 "Tablo 10. Sağlık Bilimleri Temel Alanı" kurallarını
esas alarak puan hesaplaması yapar. (Kural özeti kod içinde sabit olarak tanımlıdır.)

Özellikler:
- Kullanıcı girişi (kayıt/oturum açma) – sqlite + PBKDF2 ile parola hash
- Ziyaret sayaç (site-wide)
- Yayın/etkinlik kayıtlarını kullanıcı bazında kaydetme (sqlite)
- Uluslararası Makale (Madde 1), Ulusal Makale (Madde 2), Lisansüstü Tezden Üretilmiş Yayın (Madde 3)
  için puan motoru (yazar payı kuralları dâhil)
- Madde 3 üst sınırları (max 20 puan; g-h toplam max 5) uygulanır
- Basit uygunluk denetimleri (minimum şartlara dair özet rapor)
- Excel/CSV dışa aktarma

Notlar:
- Bu araç ÜAK'ın resmi yazılımı değildir. Resmi metin: "2025 doçentlik başvuru şartları.pdf"
- Geliştirme için: `pip install streamlit`
- Çalıştırma: `streamlit run docentlik_puan_app.py`
"""

import os
import io
import json
import math
import hashlib
import sqlite3
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any

import pandas as pd
import streamlit as st

###########################
# Config & DB
###########################
APP_TITLE = "Doçentlik Puan Hesaplayıcı – Sağlık Bilimleri (2025)"
DB_PATH = os.environ.get("DOCENTLIK_DB", "docentlik_app.db")
HASH_ITER = 200_000

st.set_page_config(page_title=APP_TITLE, layout="wide")
st.title(APP_TITLE)

@st.cache_resource
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

conn = get_conn()

###########################
# DB Schema
###########################
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT,
        salt BLOB NOT NULL,
        pwd_hash BLOB NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
)

conn.execute(
    """
    CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """
)

conn.execute(
    """
    CREATE TABLE IF NOT EXISTS site_counter (
        id INTEGER PRIMARY KEY CHECK (id=1),
        visits INTEGER NOT NULL
    );
    """
)

# init counter row
cur = conn.execute("SELECT visits FROM site_counter WHERE id=1")
row = cur.fetchone()
if row is None:
    conn.execute("INSERT INTO site_counter (id, visits) VALUES (1, 0)")
    conn.commit()

# increment counter once per session
if "_counter_incremented" not in st.session_state:
    conn.execute("UPDATE site_counter SET visits = visits + 1 WHERE id=1")
    conn.commit()
    st.session_state["_counter_incremented"] = True

visits = conn.execute("SELECT visits FROM site_counter WHERE id=1").fetchone()[0]
st.caption(f"Toplam ziyaret: {visits}")

###########################
# Auth Helpers
###########################
def hash_password(password: str, salt: Optional[bytes] = None):
    if salt is None:
        salt = os.urandom(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, HASH_ITER)
    return salt, pwd_hash

def register_user(username: str, email: str, password: str) -> Optional[str]:
    try:
        salt, pwd_hash = hash_password(password)
        conn.execute(
            "INSERT INTO users (username, email, salt, pwd_hash) VALUES (?, ?, ?, ?)",
            (username, email, salt, pwd_hash),
        )
        conn.commit()
        return None
    except sqlite3.IntegrityError as e:
        return "Kullanıcı adı zaten mevcut."

def verify_user(username: str, password: str) -> Optional[int]:
    cur = conn.execute("SELECT id, salt, pwd_hash FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    if not row:
        return None
    uid, salt, pwd_hash = row
    test_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, HASH_ITER)
    if test_hash == pwd_hash:
        return uid
    return None

###########################
# Rule Engine (TABLO 10)
###########################
# Author share rule:
# - 1 author -> full
# - 2 authors -> major: 0.8*full, second: 0.5*full
# - 3+ authors -> major: 0.5*full, others share remaining 0.5 equally
# - If no major designated (2+ authors) -> equal share

def author_share(total_points: float, n_authors: int, is_major: Optional[bool], major_specified: bool) -> float:
    if n_authors <= 1:
        return total_points
    if n_authors == 2:
        if major_specified and is_major:
            return total_points * 0.8
        if major_specified and not is_major:
            return total_points * 0.5
        # not specified -> equal split
        return total_points / 2.0
    # 3+
    if major_specified and is_major:
        return total_points * 0.5
    if major_specified and not is_major:
        return (total_points * 0.5) / (n_authors - 1)
    # not specified -> equal split
    return total_points / float(n_authors)

# POINT TABLES
INTL_ARTICLE_POINTS = {
    # Madde 1
    "SCIE_Q1": 30,
    "SCIE_Q2": 20,
    "SCIE_Q3": 15,
    "SCIE_Q4": 10,
    "AHCI": 20,
    "ESCI_SCOPUS": 10,
    "OTHER_INTL_INDEXED": 5,
    "LETTER_NOTE_ABSTRACT_REVIEW": 3,  # only if in a/b/c/d scope
    "SCIE_CASE_REPORT": 5,
}

THESIS_DERIVED_POINTS = {
    # Madde 3
    "SCIE_SSCI_AHCI": 20,
    "ESCI_SCOPUS": 10,
    "OTHER_INTL_INDEXED": 5,
    "TR_DIZIN": 8,
    "BKCI_BOOK": 20,
    "BKCI_CHAPTER": 10,
    "OTHER_BOOK": 5,
    "OTHER_BOOK_CHAPTER": 3,
    "CONF_CPCI": 3,
    "CONF_OTHER": 2,
}

NATIONAL_ARTICLE_POINTS = {
    # Madde 2
    "TR_DIZIN": 10,
    "OTHER_REFEREED": 4,
    "LETTER_NOTE_ABSTRACT_REVIEW": 2,
}

# Caps & constraints for Madde 3
THESIS_CAP_TOTAL = 20
THESIS_GH_CAP = 5  # g+h combined cap

###########################
# Data Models
###########################
@dataclass
class Record:
    category: str  # one of: M1_INTL, M2_NATL, M3_THESIS
    payload: Dict[str, Any]

    def to_json(self):
        return json.dumps({"category": self.category, "payload": self.payload}, ensure_ascii=False)

###########################
# Calculation helpers
###########################

def calc_m1_points(item: Dict[str, Any]) -> float:
    """Madde 1 – Uluslararası makale puanı (tezden üretilmemiş olmalı)."""
    base_key = item.get("base_key")
    total = INTL_ARTICLE_POINTS.get(base_key, 0)
    n = int(item.get("n_authors", 1))
    major_specified = bool(item.get("major_specified", True))
    is_major = bool(item.get("is_major", True))
    return author_share(total, n, is_major, major_specified)

def calc_m2_points(item: Dict[str, Any]) -> float:
    base_key = item.get("base_key")
    total = NATIONAL_ARTICLE_POINTS.get(base_key, 0)
    n = int(item.get("n_authors", 1))
    # Madde 2'de diğer yayınlarda toplam puan eşit bölünür (metin öyle diyor),
    # ancak makale özelindeki kuralı, M1 ile tutarlı olacak şekilde uyguluyoruz.
    # (TR Dizin/diğer hakemli makalelerde başlıca yazar vurgusu min koşul için önemlidir.)
    major_specified = bool(item.get("major_specified", True))
    is_major = bool(item.get("is_major", True))
    return author_share(total, n, is_major, major_specified)

def calc_m3_points(items: List[Dict[str, Any]]) -> (float, float, float):
    """Madde 3 toplam puanı, g-h alt limitlerini gözeterek.
    Returns: (total_after_caps, subtotal, gh_subtotal)
    """
    subtotal = 0.0
    gh_subtotal = 0.0
    for it in items:
        key = it.get("base_key")
        total = THESIS_DERIVED_POINTS.get(key, 0)
        n = int(it.get("n_authors", 1))
        major_specified = bool(it.get("major_specified", True))
        is_major = bool(it.get("is_major", True))
        pts = author_share(total, n, is_major, major_specified)
        subtotal += pts
        if key in ("OTHER_BOOK", "OTHER_BOOK_CHAPTER"):
            gh_subtotal += pts

    # apply g-h cap
    over_gh = max(0.0, gh_subtotal - THESIS_GH_CAP)
    total_after_gh = subtotal - over_gh
    # apply total cap
    total_after_caps = min(total_after_gh, THESIS_CAP_TOTAL)
    return total_after_caps, subtotal, gh_subtotal

###########################
# UI – Auth
###########################
with st.sidebar:
    st.subheader("Giriş / Kayıt")
    if "user_id" not in st.session_state:
        mode = st.radio("Seçim", ["Giriş", "Kayıt ol"], horizontal=True)
        if mode == "Giriş":
            u = st.text_input("Kullanıcı adı")
            p = st.text_input("Parola", type="password")
            if st.button("Giriş yap"):
                uid = verify_user(u, p)
                if uid:
                    st.session_state["user_id"] = uid
                    st.session_state["username"] = u
                    st.success(f"Hoş geldiniz, {u}!")
                else:
                    st.error("Kullanıcı adı / parola hatalı.")
        else:
            u = st.text_input("Kullanıcı adı")
            e = st.text_input("E-posta (opsiyonel)")
            p1 = st.text_input("Parola", type="password")
            p2 = st.text_input("Parola (tekrar)", type="password")
            if st.button("Kayıt ol"):
                if not u or not p1:
                    st.error("Kullanıcı adı ve parola gerekli.")
                elif p1 != p2:
                    st.error("Parolalar uyuşmuyor.")
                else:
                    err = register_user(u, e, p1)
                    if err:
                        st.error(err)
                    else:
                        st.success("Kayıt başarılı. Şimdi giriş yapabilirsiniz.")
    else:
        st.success(f"Oturum açık: {st.session_state['username']}")
        if st.button("Çıkış yap"):
            for k in ("user_id", "username"):
                st.session_state.pop(k, None)
            st.experimental_rerun()

###########################
# Helper: persist/load records
###########################

def save_record(user_id: int, rec: Record):
    conn.execute(
        "INSERT INTO records (user_id, category, payload) VALUES (?, ?, ?)",
        (user_id, rec.category, json.dumps(rec.payload, ensure_ascii=False)),
    )
    conn.commit()


def load_records(user_id: int) -> pd.DataFrame:
    cur = conn.execute("SELECT id, category, payload, created_at FROM records WHERE user_id=? ORDER BY id DESC", (user_id,))
    rows = cur.fetchall()
    data = []
    for rid, cat, payload, created in rows:
        try:
            p = json.loads(payload)
        except Exception:
            p = {"raw": payload}
        data.append({"id": rid, "category": cat, **p, "created_at": created})
    return pd.DataFrame(data)

###########################
# UI – Main (requires auth)
###########################
if "user_id" not in st.session_state:
    st.info("Devam etmek için lütfen oturum açın veya kayıt olun.")
    st.stop()

uid = st.session_state["user_id"]

TAB = st.tabs([
    "Puan Hesaplayıcı",
    "Kayıtlarım",
    "İçe/Dışa Aktar",
    "Uygunluk Özeti",
    "Hakkında"
])

###########################
# Tab: Puan Hesaplayıcı
###########################
with TAB[0]:
    st.subheader("Yayın/Etkinlik Ekle")
    cat = st.selectbox(
        "Kategori",
        [
            ("M1_INTL", "Uluslararası Makale (Madde 1) – Tezden üretilmemiş"),
            ("M2_NATL", "Ulusal Makale (Madde 2) – Tezden üretilmemiş"),
            ("M3_THESIS", "Lisansüstü Tezden Üretilmiş Yayın (Madde 3)"),
        ],
        format_func=lambda x: x[1],
    )

    cat_key = cat[0]

    if cat_key == "M1_INTL":
        base_key = st.selectbox(
            "Dergi/Tip",
            [
                ("SCIE_Q1", "SCIE/SSCI – Q1 (30)"),
                ("SCIE_Q2", "SCIE/SSCI – Q2 (20)"),
                ("SCIE_Q3", "SCIE/SSCI – Q3 (15)"),
                ("SCIE_Q4", "SCIE/SSCI – Q4 (10)"),
                ("AHCI", "AHCI (20)"),
                ("ESCI_SCOPUS", "ESCI/Scopus (10)"),
                ("OTHER_INTL_INDEXED", "Diğer uluslararası indeksli (5)"),
                ("LETTER_NOTE_ABSTRACT_REVIEW", "Editöre mektup/araştırma notu/özet/kitap kritiği (3)"),
                ("SCIE_CASE_REPORT", "SCIE vaka takdimi (5)"),
            ],
            format_func=lambda x: x[1],
        )
        n_authors = st.number_input("Yazar sayısı", min_value=1, step=1, value=1)
        major_specified = st.checkbox("Başlıca yazar belirtilmiş", value=True)
        is_major = st.checkbox("Ben başlıca yazarıyım", value=True)
        title = st.text_input("Eser başlığı (opsiyonel)")
        if st.button("Ekle (Madde 1)"):
            item = {
                "base_key": base_key[0],
                "n_authors": int(n_authors),
                "major_specified": bool(major_specified),
                "is_major": bool(is_major),
                "title": title,
            }
            save_record(uid, Record(category="M1_INTL", payload=item))
            st.success("Kaydedildi.")

    elif cat_key == "M2_NATL":
        base_key = st.selectbox(
            "Dergi/Tip",
            [
                ("TR_DIZIN", "TR Dizin (10)"),
                ("OTHER_REFEREED", "Diğer hakemli (4)"),
                ("LETTER_NOTE_ABSTRACT_REVIEW", "Editöre mektup/araştırma notu/özet/kitap kritiği (2)"),
            ],
            format_func=lambda x: x[1],
        )
        n_authors = st.number_input("Yazar sayısı", min_value=1, step=1, value=1)
        major_specified = st.checkbox("Başlıca yazar belirtilmiş", value=True)
        is_major = st.checkbox("Ben başlıca yazarıyım", value=True)
        title = st.text_input("Eser başlığı (opsiyonel)")
        if st.button("Ekle (Madde 2)"):
            item = {
                "base_key": base_key[0],
                "n_authors": int(n_authors),
                "major_specified": bool(major_specified),
                "is_major": bool(is_major),
                "title": title,
            }
            save_record(uid, Record(category="M2_NATL", payload=item))
            st.success("Kaydedildi.")

    else:  # M3_THESIS
        base_key = st.selectbox(
            "Yayın türü",
            [
                ("SCIE_SSCI_AHCI", "SCIE/SSCI/AHCI makale (20)"),
                ("ESCI_SCOPUS", "ESCI/Scopus makale (10)"),
                ("OTHER_INTL_INDEXED", "Diğer uluslararası indeksli makale (5)"),
                ("TR_DIZIN", "TR Dizin makale (8)"),
                ("BKCI_BOOK", "BKCI kitap (20)"),
                ("BKCI_CHAPTER", "BKCI kitap bölümü (10)"),
                ("OTHER_BOOK", "Diğer uluslararası/ulusal kitap (5) [g]"),
                ("OTHER_BOOK_CHAPTER", "Diğer uluslararası/ulusal kitap bölümü (3) [h]"),
                ("CONF_CPCI", "Uluslararası toplantı tam metin/özet CPCI'da (3)"),
                ("CONF_OTHER", "Diğer uluslararası/ulusal toplantı tam metin/özet (2)"),
            ],
            format_func=lambda x: x[1],
        )
        n_authors = st.number_input("Yazar sayısı", min_value=1, step=1, value=1)
        major_specified = st.checkbox("Başlıca yazar belirtilmiş", value=True)
        is_major = st.checkbox("Ben başlıca yazarıyım", value=True)
        title = st.text_input("Eser başlığı (opsiyonel)")
        if st.button("Ekle (Madde 3)"):
            item = {
                "base_key": base_key[0],
                "n_authors": int(n_authors),
                "major_specified": bool(major_specified),
                "is_major": bool(is_major),
                "title": title,
            }
            save_record(uid, Record(category="M3_THESIS", payload=item))
            st.success("Kaydedildi.")

    st.divider()

    # On-the-fly total
    df_user = load_records(uid)
    st.subheader("Anlık Toplam")
    total_m1 = 0.0
    total_m2 = 0.0
    m3_items: List[Dict[str, Any]] = []

    for _, r in df_user.iterrows():
        cat = r.get("category")
        payload = {k: r.get(k) for k in r.index if k not in ("id", "category", "created_at")}
        if cat == "M1_INTL":
            total_m1 += calc_m1_points(payload)
        elif cat == "M2_NATL":
            total_m2 += calc_m2_points(payload)
        elif cat == "M3_THESIS":
            m3_items.append(payload)

    m3_total, m3_subtotal, m3_gh = calc_m3_points(m3_items)

    grand_total = total_m1 + total_m2 + m3_total

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Madde 1 (Uluslararası)", f"{total_m1:.2f}")
    c2.metric("Madde 2 (Ulusal)", f"{total_m2:.2f}")
    c3.metric("Madde 3 (Tezden) – Net", f"{m3_total:.2f}", help=f"Ham: {m3_subtotal:.2f} | g+h: {m3_gh:.2f} (g+h max {THESIS_GH_CAP}) | Madde3 max {THESIS_CAP_TOTAL}")
    c4.metric("GENEL TOPLAM", f"{grand_total:.2f}")

###########################
# Tab: Kayıtlarım
###########################
with TAB[1]:
    st.subheader("Kayıtlarım")
    df = load_records(uid)
    if df.empty:
        st.info("Henüz kayıt yok.")
    else:
        # pretty view
        view_df = df.copy()
        view_df["payload"] = view_df.apply(lambda r: json.dumps({k: r[k] for k in r.index if k not in ("id","category","payload","created_at")}, ensure_ascii=False), axis=1)
        show_cols = ["id", "category", "title", "base_key", "n_authors", "major_specified", "is_major", "created_at"]
        show_cols = [c for c in show_cols if c in view_df.columns]
        st.dataframe(view_df[show_cols], use_container_width=True)

        sel = st.multiselect("Silinecek kayıt id'leri", view_df["id"].tolist())
        if st.button("Seçilenleri sil") and sel:
            conn.executemany("DELETE FROM records WHERE id=? AND user_id=?", [(rid, uid) for rid in sel])
            conn.commit()
            st.success("Silindi.")
            st.experimental_rerun()

###########################
# Tab: İçe/Dışa Aktar
###########################
with TAB[2]:
    st.subheader("Dışa aktar (CSV/JSON)")
    df = load_records(uid)
    if not df.empty:
        csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("CSV indir", data=csv_bytes, file_name="kayitlarim.csv", mime="text/csv")
        json_bytes = df.to_json(orient="records", force_ascii=False).encode("utf-8")
        st.download_button("JSON indir", data=json_bytes, file_name="kayitlarim.json", mime="application/json")
    else:
        st.info("Dışa aktarılacak kayıt yok.")

    st.subheader("İçe aktar (CSV)")
    up = st.file_uploader("CSV yükle (daha önce bu uygulamadan indirilmiş format)", type=["csv"])
    if up is not None:
        try:
            imp = pd.read_csv(up)
            # basic sanity
            needed = {"category", "base_key", "n_authors"}
            if not needed.issubset(set(imp.columns)):
                st.error(f"CSV kolonları eksik. Gerekli: {needed}")
            else:
                for _, r in imp.iterrows():
                    payload = {k: r[k] for k in imp.columns if k not in ("id", "created_at")}
                    save_record(uid, Record(category=str(r["category"]), payload=payload))
                st.success("Kayıtlar içe aktarıldı.")
        except Exception as e:
            st.error(f"Hata: {e}")

###########################
# Tab: Uygunluk Özeti (Basit kontroller)
###########################
with TAB[3]:
    st.subheader("Uygunluk Özeti – (Basit denetim)")
    df = load_records(uid)
    if df.empty:
        st.info("Kayıt yok.")
    else:
        # Madde 3: en az bir yayın şartı
        has_m3_any = (df["category"] == "M3_THESIS").any()
        # M3 totals
        m3_items = []
        for _, r in df[df["category"] == "M3_THESIS"].iterrows():
            payload = {k: r.get(k) for k in r.index if k not in ("id", "category", "created_at")}
            m3_items.append(payload)
        m3_total, m3_subtotal, m3_gh = calc_m3_points(m3_items)

        # M1 requirement: a bendinden (SCIE/SSCI Q1–Q4) en az 40 puan ve en az 3 makalede başlıca yazar olmak
        m1_major_count = 0
        m1_major_points = 0.0
        for _, r in df[df["category"] == "M1_INTL"].iterrows():
            bk = r.get("base_key")
            if bk in ("SCIE_Q1", "SCIE_Q2", "SCIE_Q3", "SCIE_Q4"):
                payload = {k: r.get(k) for k in r.index if k not in ("id", "category", "created_at")}
                pts = calc_m1_points(payload)
                m1_major_points += pts
                if r.get("is_major") in (True, "True", 1, "1"):
                    m1_major_count += 1

        # M2 requirement: en az 3 yayın; ikisi TR Dizin; bu yayınlardan en az ikisi başlıca yazar
        m2_rows = df[df["category"] == "M2_NATL"]
        m2_count = len(m2_rows)
        m2_trd_count = int((m2_rows.get("base_key") == "TR_DIZIN").sum()) if not m2_rows.empty else 0
        m2_major_count = int((m2_rows.get("is_major") == True).sum()) if not m2_rows.empty else 0

        # Education min 2 puan – bu uygulamada eğitim modülü yok, uyarı notu veriyoruz
        st.write("**Not:** Eğitim-Öğretim (Madde 9) ve diğer bazı kalemler bu sürümde hesaplanmıyor; lütfen bu kalemleri ayrıca kontrol edin.")

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("#### Madde 3 – Tezden Üretilmiş Yayın")
            st.write(f"- En az bir yayın var mı? **{'Evet' if has_m3_any else 'Hayır'}**")
            st.write(f"- Madde 3 Net Toplam: **{m3_total:.2f}** (Ham: {m3_subtotal:.2f}; g+h: {m3_gh:.2f} – sınır {THESIS_GH_CAP})")
        with col2:
            st.markdown("#### Madde 1 – SCIE/SSCI (a bendi) Başlıca Yazar Şartı")
            st.write(f"- Başlıca yazar olduğunuz SCIE/SSCI (Q1–Q4) makale sayısı: **{m1_major_count}** (gereken ≥ 3)")
            st.write(f"- Bu makalelerden elde edilen toplam puan: **{m1_major_points:.2f}** (gereken ≥ 40)")

        st.markdown("#### Madde 2 – Ulusal Makale Şartı")
        st.write(f"- Ulusal makale sayısı: **{m2_count}** (gereken ≥ 3; ikisi TR Dizin olmalı)")
        st.write(f"- TR Dizin sayısı: **{m2_trd_count}** (gereken ≥ 2)")
        st.write(f"- Başlıca yazar olduğunuz ulusal makale sayısı: **{m2_major_count}** (gereken ≥ 2)")

###########################
# Tab: Hakkında
###########################
with TAB[4]:
    st.subheader("Hakkında")
    st.markdown(
        """
        Bu araç, ÜAK 2025 Tablo 10 – Sağlık Bilimleri Temel Alanı puan kurallarını baz alır ve
        kullanıcıların kişisel hesaplamalarını kolaylaştırmak amacıyla hazırlanmıştır. Resmî bir araç değildir.

        **Geri Bildirim:** Eksik gördüğünüz maddeler (Atıf, Proje, Eğitim, Patent vb.) için ek modüller eklenebilir.
        Lütfen ihtiyaçlarınızı iletin.
        """
    )
