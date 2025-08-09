
import os
import json
import sqlite3
import hashlib
import datetime as dt
from dataclasses import dataclass
from typing import Dict, Any, Tuple, List

import streamlit as st

# =========================
# Config
# =========================
DB_PATH = os.environ.get("DOCENT_DB_PATH", "/tmp/docentlik.db")  # <-- cloud-safe default
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")  # change in production!

APP_TITLE = "Doçentlik Puan Hesaplayıcı — Sağlık Bilimleri (2025)"
APP_FOOTER = "© 2025 — Örnek Uygulama (Tablo 10 - Sağlık Bilimleri). Lütfen resmi tablolarla doğrulayın."

# =========================
# Helpers
# =========================
def sha256_hash(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

@st.cache_resource
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            payload TEXT NOT NULL,
            total REAL NOT NULL,
            breakdown TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        )
    """)
    # visit counter row
    cur.execute("INSERT OR IGNORE INTO stats(key, value) VALUES ('visits', 0)")
    conn.commit()

    # bootstrap admin if not exists
    cur.execute("SELECT * FROM users WHERE username=?", (ADMIN_USER,))
    if cur.fetchone() is None:
        salt = os.urandom(16).hex()
        ph = sha256_hash(ADMIN_PASS, salt)
        cur.execute("""INSERT INTO users(username, password_hash, salt, is_admin, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (ADMIN_USER, ph, salt, 1, dt.datetime.utcnow().isoformat()))
        conn.commit()

def increment_visit():
    conn = get_conn()
    conn.execute("UPDATE stats SET value = value + 1 WHERE key='visits'")
    conn.commit()

def get_visits() -> int:
    conn = get_conn()
    row = conn.execute("SELECT value FROM stats WHERE key='visits'").fetchone()
    return int(row["value"]) if row else 0

def register_user(username: str, password: str) -> Tuple[bool, str]:
    conn = get_conn()
    try:
        salt = os.urandom(16).hex()
        ph = sha256_hash(password, salt)
        conn.execute("""INSERT INTO users(username, password_hash, salt, is_admin, created_at)
                        VALUES (?, ?, ?, 0, ?)""", (username, ph, salt, dt.datetime.utcnow().isoformat()))
        conn.commit()
        return True, "Kayıt başarılı."
    except sqlite3.IntegrityError:
        return False, "Bu kullanıcı adı zaten mevcut."

def authenticate(username: str, password: str) -> Tuple[bool, Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        return False, {}
    salt = row["salt"]
    ph = sha256_hash(password, salt)
    if ph == row["password_hash"]:
        return True, {"username": row["username"], "is_admin": bool(row["is_admin"])}
    return False, {}

def list_users() -> List[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("SELECT id, username, is_admin, created_at FROM users ORDER BY id").fetchall()
    return rows

def set_admin(username: str, is_admin: bool):
    conn = get_conn()
    conn.execute("UPDATE users SET is_admin=? WHERE username=?", (1 if is_admin else 0, username))
    conn.commit()

def reset_password(username: str, new_password: str):
    conn = get_conn()
    salt = os.urandom(16).hex()
    ph = sha256_hash(new_password, salt)
    conn.execute("UPDATE users SET password_hash=?, salt=? WHERE username=?", (ph, salt, username))
    conn.commit()

def save_record(owner: str, payload: Dict[str, Any], total: float, breakdown: Dict[str, Any]):
    conn = get_conn()
    conn.execute("""INSERT INTO records(owner, payload, total, breakdown, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                 (owner, json.dumps(payload, ensure_ascii=False), total, json.dumps(breakdown, ensure_ascii=False),
                  dt.datetime.utcnow().isoformat()))
    conn.commit()

def list_records(owner: str=None) -> List[sqlite3.Row]:
    conn = get_conn()
    if owner is None:
        rows = conn.execute("SELECT * FROM records ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM records WHERE owner=? ORDER BY id DESC", (owner,)).fetchall()
    return rows

def delete_record(record_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM records WHERE id=?", (record_id,))
    conn.commit()

# =========================
# Point Rules (Tablo 10 - 2025)
# =========================
@dataclass
class Totals:
    total: float
    total_excluding_thesis: float
    checks: Dict[str, Any]
    breakdown: Dict[str, Any]

def cap(value: float, maxv: float) -> float:
    return min(value, maxv)

def article_share(points: float, num_authors: int, role: str, has_primary_author: bool=True) -> float:
    """
    role: 'primary', 'second' (only for 2 authors), 'other', 'equal' (no primary indicated)
    """
    if num_authors <= 0:
        return 0.0
    if num_authors == 1:
        return points
    if not has_primary_author:
        return points / num_authors
    if num_authors == 2:
        if role == "primary":
            return points * 0.8
        else:
            return points * 0.5
    # 3+ authors
    if role == "primary":
        return points * 0.5
    else:
        return (points * 0.5) / (num_authors - 1)

def compute_points(data: Dict[str, Any]) -> Totals:
    """
    data: see previous message for structure
    """
    # --- Article base points (Madde 1 & 2) ---
    base_map = {
        "Q1": 30, "Q2": 20, "Q3": 15, "Q4": 10,
        "AHCI": 20, "ESCI": 10, "OTHER_INT": 5,
        "TRDIZIN": 10, "OTHER_NAT": 4, "LETTER": 3, "CASE": 5
    }

    # --- Thesis publications (Madde 3) ---
    thesis_map = {
        "SCIE_SSCI_AHCI": 20, "ESCI_SCOPUS": 10, "OTHER_INT": 5, "TRDIZIN": 8,
        "BKCI_BOOK": 20, "BKCI_CHAPTER": 10, "OTHER_BOOK": 5, "OTHER_BOOK_CH": 3,
        "CPCI": 3, "OTHER_CONF": 2
    }

    # Articles (non-thesis)
    total_articles = 0.0
    total_articles_details = []
    count_1a_primary_after = 0
    total_1a_points_after = 0.0

    for a in data.get("articles", []):
        t = a["type"]
        pts = base_map.get(t, 0)
        share = article_share(pts, a["num_authors"], a["role"], a.get("has_primary", True))
        total_articles += share
        total_articles_details.append((t, pts, share, a["num_authors"], a["role"]))
        if t in ["Q1","Q2","Q3","Q4"] and a["role"] == "primary" and data.get("after_degree", True):
            count_1a_primary_after += 1
            total_1a_points_after += share

    # National article condition (Madde 2)
    nat_primary_count = 0
    nat_trdizin_count = 0
    nat_pub_count = 0
    for a in data.get("articles", []):
        if a["type"] in ["TRDIZIN", "OTHER_NAT"]:
            nat_pub_count += 1
            if a["type"] == "TRDIZIN":
                nat_trdizin_count += 1
            if a["role"] == "primary":
                nat_primary_count += 1

    # Thesis publications (Madde 3)
    thesis_total_share = 0.0
    thesis_any_ah_to_h = False
    thesis_details = []
    for tpub in data.get("thesis_articles", []):
        t = tpub["type"]
        pts = thesis_map.get(t, 0)
        share = article_share(pts, tpub["num_authors"], tpub["role"], tpub.get("has_primary", True))
        thesis_total_share += share
        thesis_details.append((t, pts, share, tpub["num_authors"], tpub["role"]))
        if t in ["SCIE_SSCI_AHCI","ESCI_SCOPUS","OTHER_INT","TRDIZIN","BKCI_BOOK","BKCI_CHAPTER","OTHER_BOOK","OTHER_BOOK_CH"]:
            thesis_any_ah_to_h = True
    thesis_total_capped = cap(thesis_total_share, 20.0)

    # Citations (Madde 5) — max 10
    c = data.get("citations", {})
    c_points_capped = cap(c.get("wos_scopus", 0)*3 + c.get("bkci", 0)*2 + c.get("trdizin", 0)*2 + c.get("other", 0)*1, 10.0)

    # Supervisions (Madde 6) — max 10
    s = data.get("supervisions", {})
    s_points_capped = cap(s.get("phd",0)*5 + s.get("ms",0)*3 + s.get("phd_as_second",0)*2.5 + s.get("ms_as_second",0)*1.5, 10.0)

    # Projects (Madde 7) — max 20
    p = data.get("projects", {})
    p_points_capped = cap(p.get("eu_tubitak_coord",0)*15 + p.get("eu_tubitak_researcher",0)*10 + p.get("eu_tubitak_advisor",0)*5 +
                          p.get("intl_project_any",0)*10 + p.get("public_private_rnd",0)*5 + p.get("bap_coord",0)*3, 20.0)

    # Meetings (Madde 8) — max 10
    m = data.get("meetings", {})
    m_points_capped = cap(m.get("cpci",0)*5 + m.get("other",0)*3, 10.0)

    # Education (Madde 9) — min 2, max 6
    edu = data.get("education", {})
    edu_points = 0.0
    if edu.get("semester_mode", 0) >= 4: edu_points += 2
    if edu.get("year_mode", 0) >= 2: edu_points += 2
    if edu.get("has_2yr_faculty", False): edu_points += 2
    edu_points_capped = cap(edu_points, 6.0)

    # Patents (Madde 10)
    pat = data.get("patents", {})
    def safe_div(x, n): return (x / n) if n and n>0 else 0.0
    pat_points = 0.0
    pat_points += safe_div(20*pat.get("intl",0), max(1, pat.get("intl_inventors",1)))
    pat_points += safe_div(10*pat.get("national",0), max(1, pat.get("national_inventors",1)))
    pat_points += safe_div(5*pat.get("utility",0), max(1, pat.get("utility_inventors",1)))
    pat_points += safe_div(2*pat.get("app",0), max(1, pat.get("app_inventors",1)))

    # Awards (Madde 11) — max 25
    aw = data.get("awards", {})
    aw_points_capped = cap( (aw.get("yok_phd",0)+aw.get("yok_high",0)+aw.get("tubitak_science",0)+
                             aw.get("tubitak_encour",0)+aw.get("tuba_gebip",0)+aw.get("tuba_tesep",0)) * 25, 25.0)

    # Editor (Madde 12) — max 4
    ed = data.get("editor", {})
    ed_points_capped = cap(ed.get("wos_scopus",0)*2 + ed.get("bkci_scopus_book",0)*1 + ed.get("trdizin",0)*1, 4.0)

    # Other (Madde 13) — max 10
    oth = data.get("other", {})
    other_points_capped = cap( (5 if oth.get("hindex5", False) else 0) + (5 if oth.get("top300_6m", False) else 0), 10.0)

    total_excl_thesis = (total_articles + c_points_capped + s_points_capped + p_points_capped +
                         m_points_capped + edu_points_capped + pat_points + aw_points_capped +
                         ed_points_capped + other_points_capped)

    total_all = total_excl_thesis + thesis_total_capped

    checks = {
        "overall_min_100": total_all >= 100.0,
        "min_90_after_excl_thesis": total_excl_thesis >= 90.0,
        "1a_primary_at_least3_and_40pts": (count_1a_primary_after >= 3 and total_1a_points_after >= 40.0),
        "2_national_at_least3_with2_trdizin_and_2_primary": (nat_pub_count >= 3 and nat_trdizin_count >= 2 and nat_primary_count >= 2),
        "3_thesis_at_least_one_from_a_h": thesis_any_ah_to_h,
        "5_citation_min5_after": c_points_capped >= 5.0,
        "8_meeting_min5_after": m_points_capped >= 5.0,
        "9_education_min2": edu_points_capped >= 2.0
    }

    breakdown = {
        "1_2_articles_total_share": round(total_articles, 4),
        "3_thesis_share_capped20": round(thesis_total_capped, 4),
        "5_citations_capped10": round(c_points_capped, 4),
        "6_supervisions_capped10": round(s_points_capped, 4),
        "7_projects_capped20": round(p_points_capped, 4),
        "8_meetings_capped10": round(m_points_capped, 4),
        "9_education_capped6": round(edu_points_capped, 4),
        "10_patents": round(pat_points, 4),
        "11_awards_capped25": round(aw_points_capped, 4),
        "12_editor_capped4": round(ed_points_capped, 4),
        "13_other_capped10": round(other_points_capped, 4),
        "TOTAL_EXCLUDING_THESIS": round(total_excl_thesis, 4),
        "TOTAL_ALL": round(total_all, 4),
    }
    return Totals(total=total_all, total_excluding_thesis=total_excl_thesis, checks=checks, breakdown=breakdown)

# =========================
# UI
# =========================
def login_ui():
    st.markdown("### Giriş / Kayıt")
    tabs = st.tabs(["Giriş", "Kayıt Ol"])
    with tabs[0]:
        u = st.text_input("Kullanıcı adı", key="login_user")
        p = st.text_input("Şifre", type="password", key="login_pass")
        if st.button("Giriş"):
            ok, info = authenticate(u, p)
            if ok:
                st.session_state["user"] = info
                st.success(f"Hoş geldiniz, {info['username']}!")
                st.experimental_rerun()
            else:
                st.error("Kullanıcı adı veya şifre hatalı.")
    with tabs[1]:
        u = st.text_input("Yeni kullanıcı adı", key="reg_user")
        p1 = st.text_input("Şifre", type="password", key="reg_p1")
        p2 = st.text_input("Şifre (tekrar)", type="password", key="reg_p2")
        if st.button("Kayıt Ol"):
            if not u or not p1:
                st.warning("Kullanıcı adı ve şifre gerekli.")
            elif p1 != p2:
                st.error("Şifreler eşleşmiyor.")
            else:
                ok, msg = register_user(u, p1)
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

def article_entry(label: str, thesis: bool=False) -> List[Dict[str, Any]]:
    st.markdown(f"#### {label}")
    rows = st.number_input("Kaç kayıt gireceksiniz?", min_value=0, max_value=200, value=0, step=1, key=f"{label}_rows")
    data = []
    for i in range(rows):
        with st.expander(f"{label} #{i+1}", expanded=False):
            if not thesis:
                type_opt = st.selectbox(
                    "Tür",
                    ["Q1","Q2","Q3","Q4","AHCI","ESCI","OTHER_INT","TRDIZIN","OTHER_NAT","LETTER","CASE"],
                    key=f"{label}_type_{i}"
                )
            else:
                type_opt = st.selectbox(
                    "Tür (Tezden Üretilmiş Yayın)",
                    ["SCIE_SSCI_AHCI","ESCI_SCOPUS","OTHER_INT","TRDIZIN","BKCI_BOOK","BKCI_CHAPTER","OTHER_BOOK","OTHER_BOOK_CH","CPCI","OTHER_CONF"],
                    key=f"{label}_type_{i}"
                )
            num_auth = st.number_input("Yazar sayısı", min_value=1, value=1, step=1, key=f"{label}_num_{i}")
            has_pri = st.checkbox("Başlıca yazar belirtilmiş", value=True, key=f"{label}_haspri_{i}")
            role = st.selectbox("Sizdeki rol", ["primary","second","other","equal"], key=f"{label}_role_{i}")
            data.append({"type": type_opt, "num_authors": int(num_auth), "has_primary": has_pri, "role": role})
    return data

def citations_entry():
    st.markdown("#### 5) Atıflar")
    w = st.number_input("SCIE/SSCI/AHCI/ESCI/Scopus kapsamındaki atıf sayısı", min_value=0, value=0)
    b = st.number_input("BKCI kapsamındaki kitapta atıf sayısı", min_value=0, value=0)
    t = st.number_input("TR Dizin kapsamındaki dergide atıf sayısı", min_value=0, value=0)
    o = st.number_input("Diğer uluslararası/ulusal atıf sayısı", min_value=0, value=0)
    return {"wos_scopus": int(w), "bkci": int(b), "trdizin": int(t), "other": int(o)}

def supervisions_entry():
    st.markdown("#### 6) Lisansüstü Tez Danışmanlığı")
    phd = st.number_input("Tamamlanmış Doktora tezi (asıl danışman)", min_value=0, value=0)
    ms = st.number_input("Tamamlanmış Yüksek lisans tezi (asıl danışman)", min_value=0, value=0)
    phd2 = st.number_input("Tamamlanmış Doktora tezi (ikinci danışman)", min_value=0, value=0)
    ms2 = st.number_input("Tamamlanmış Yüksek lisans tezi (ikinci danışman)", min_value=0, value=0)
    return {"phd": int(phd), "ms": int(ms), "phd_as_second": int(phd2), "ms_as_second": int(ms2)}

def projects_entry():
    st.markdown("#### 7) Bilimsel Araştırma Projeleri")
    eu_c = st.number_input("AB/TÜBİTAK — Koordinatör/Yürütücü", min_value=0, value=0)
    eu_r = st.number_input("AB/TÜBİTAK — Araştırmacı", min_value=0, value=0)
    eu_d = st.number_input("AB/TÜBİTAK — Danışman", min_value=0, value=0)
    intl = st.number_input("Uluslararası destekli proje (yür./arş./dan.)", min_value=0, value=0)
    pp = st.number_input("Kamu/özel Ar-Ge/Ür-Ge projesi (yür./arş./dan.)", min_value=0, value=0)
    bap = st.number_input("Üniversite BAP (tez/uzmanlık projeleri hariç) — Yürütücü", min_value=0, value=0)
    return {"eu_tubitak_coord": int(eu_c), "eu_tubitak_researcher": int(eu_r), "eu_tubitak_advisor": int(eu_d),
            "intl_project_any": int(intl), "public_private_rnd": int(pp), "bap_coord": int(bap)}

def meetings_entry():
    st.markdown("#### 8) Bilimsel Toplantı")
    cpci = st.number_input("Uluslararası toplantı — CPCI kayıtlı", min_value=0, value=0)
    other = st.number_input("Diğer uluslararası/ulusal toplantı", min_value=0, value=0)
    return {"cpci": int(cpci), "other": int(other)}

def education_entry():
    st.markdown("#### 9) Eğitim-Öğretim")
    sem = st.number_input("Dönemlik programlarda **farklı yarıyıl** sayısı (>=4 ise 2 puan)", min_value=0, value=0)
    yr = st.number_input("Yıllık programlarda **farklı yıl** sayısı (>=2 ise 2 puan)", min_value=0, value=0)
    has2 = st.checkbox("Uzmanlıktan sonra ≥2 yıl kadrolu öğretim elemanı (otomatik 2 puan)", value=False)
    return {"semester_mode": int(sem), "year_mode": int(yr), "has_2yr_faculty": bool(has2)}

def patents_entry():
    st.markdown("#### 10) Patent / Faydalı Model")
    intl = st.number_input("Tescilli uluslararası patent sayısı", min_value=0, value=0)
    intl_inv = st.number_input("Uluslararası patent başına mucit sayısı (ortalama)", min_value=1, value=1)
    nat = st.number_input("Tescilli ulusal patent sayısı", min_value=0, value=0)
    nat_inv = st.number_input("Ulusal patent başına mucit sayısı (ortalama)", min_value=1, value=1)
    uti = st.number_input("Tescilli faydalı model sayısı", min_value=0, value=0)
    uti_inv = st.number_input("Faydalı model başına mucit sayısı (ortalama)", min_value=1, value=1)
    app = st.number_input("Kişisel patent başvurusu sayısı", min_value=0, value=0)
    app_inv = st.number_input("Patent başvurusu başına mucit sayısı (ortalama)", min_value=1, value=1)
    return {"intl": int(intl), "national": int(nat), "utility": int(uti), "app": int(app),
            "intl_inventors": int(intl_inv), "national_inventors": int(nat_inv),
            "utility_inventors": int(uti_inv), "app_inventors": int(app_inv)}

def awards_entry():
    st.markdown("#### 11) Ödüller")
    yok_phd = st.number_input("YÖK Yılın Doktora Tezi Ödülü", min_value=0, value=0)
    yok_high = st.number_input("YÖK Üstün Başarı Ödülü", min_value=0, value=0)
    tub_sci = st.number_input("TÜBİTAK Bilim Ödülü", min_value=0, value=0)
    tub_enc = st.number_input("TÜBİTAK Teşvik Ödülü", min_value=0, value=0)
    tuba_g = st.number_input("TÜBA GEBİP Ödülü", min_value=0, value=0)
    tuba_t = st.number_input("TÜBA TESEP Ödülü", min_value=0, value=0)
    return {"yok_phd": int(yok_phd), "yok_high": int(yok_high), "tubitak_science": int(tub_sci),
            "tubitak_encour": int(tub_enc), "tuba_gebip": int(tuba_g), "tuba_tesep": int(tuba_t)}

def editor_entry():
    st.markdown("#### 12) Editörlük")
    w = st.number_input("SCIE/SSCI/AHCI/ESCI/Scopus kapsamındaki dergide editörlük", min_value=0, value=0)
    b = st.number_input("BKCI/Scopus kapsamındaki kitapta editörlük", min_value=0, value=0)
    t = st.number_input("TR Dizin kapsamındaki dergide editörlük", min_value=0, value=0)
    return {"wos_scopus": int(w), "bkci_scopus_book": int(b), "trdizin": int(t)}

def other_entry():
    st.markdown("#### 13) Diğer")
    h5 = st.checkbox("Web of Science h-indeksi ≥ 5", value=False)
    top300 = st.checkbox("İlk 300 üniversitede ≥6 ay yurt dışı (kesintisiz) araştırma/öğretim", value=False)
    return {"hindex5": bool(h5), "top300_6m": bool(top300)}

# -------- compute_points is above --------
# (imported earlier in this file)

def admin_panel():
    st.markdown("## 🔐 Admin Paneli")
    st.info("Admin yetkisi, ADMIN_USER ile giriş yapan kullanıcıya atanır (varsayılan: admin/admin). Lütfen üretimde değiştirin.")
    visits = get_visits()
    st.metric("Toplam ziyaret", visits)

    st.subheader("Kullanıcılar")
    users = list_users()
    cols = st.columns([2,1,2,2,2])
    cols[0].markdown("**Kullanıcı adı**")
    cols[1].markdown("**Admin?**")
    cols[2].markdown("**Oluşturulma**")
    cols[3].markdown("**Admin ata/kaldır**")
    cols[4].markdown("**Şifre sıfırla**")
    for u in users:
        c = st.columns([2,1,2,2,2])
        c[0].write(u["username"])
        c[1].write("Evet" if u["is_admin"] else "Hayır")
        c[2].write(u["created_at"])
        with c[3]:
            if st.button(("Admin Kaldır" if u["is_admin"] else "Admin Yap"), key=f"adm_{u['id']}"):
                set_admin(u["username"], not bool(u["is_admin"]))
                st.success("Güncellendi.")
                st.experimental_rerun()
        with c[4]:
            newp = st.text_input("Yeni şifre", key=f"np_{u['id']}", type="password")
            if st.button("Sıfırla", key=f"rp_{u['id']}"):
                if newp:
                    reset_password(u["username"], newp)
                    st.success("Şifre güncellendi.")
                else:
                    st.warning("Şifre boş olamaz.")

    st.subheader("Kayıtlar")
    recs = list_records()
    st.write(f"Toplam kayıt: {len(recs)}")
    # export
    if st.button("Kayıtları JSON olarak indir"):
        js = [dict(r) for r in recs]
        st.download_button("JSON indir", json.dumps(js, ensure_ascii=False, indent=2), file_name="records.json")
    for r in recs:
        with st.expander(f"#{r['id']} • {r['owner']} • {r['created_at']} • Toplam: {r['total']}"):
            st.json(json.loads(r["payload"]))
            st.json(json.loads(r["breakdown"]))
            if st.button("Bu kaydı sil", key=f"del_{r['id']}"):
                delete_record(r["id"])
                st.success("Silindi.")
                st.experimental_rerun()

def main():
    st.set_page_config(page_title=APP_TITLE, page_icon="🧮", layout="wide")
    st.title(APP_TITLE)
    st.caption(APP_FOOTER)

    init_db()
    if "visited" not in st.session_state:
        increment_visit()
        st.session_state["visited"] = True

    # Auth
    user = st.session_state.get("user")
    if not user:
        # quick shortcut login for admin (dev environments)
        if st.sidebar.button("Admin olarak otomatik giriş"):
            ok, info = authenticate(ADMIN_USER, ADMIN_PASS)
            if ok:
                st.session_state["user"] = info
                st.experimental_rerun()
        # otherwise normal login UI
        login_ui()
        st.stop()

    st.sidebar.write(f"👤 Kullanıcı: **{user['username']}** {'(Admin)' if user['is_admin'] else ''}")
    if st.sidebar.button("Çıkış"):
        st.session_state.pop("user", None)
        st.experimental_rerun()

    tabs = st.tabs(["Puan Hesaplayıcı", "Kayıtlarım", "Hakkında"] + (["Admin"] if user["is_admin"] else []))

    with tabs[0]:
        st.markdown("### 1) Yayın Bilgileri (Tez dışı)")
        after_degree = st.checkbox("Bu yayınların tamamı uzmanlık/doktora SONRASI mı?", value=True)
        articles = article_entry("Uluslararası/Ulusal Makaleler (Tez dışı)")

        st.markdown("---")
        st.markdown("### 2) Tezden Üretilmiş Yayınlar (Madde 3)")
        thesis_articles = article_entry("Tez Yayınları", thesis=True)

        st.markdown("---")
        citations = citations_entry()
        st.markdown("---")
        superv = supervisions_entry()
        st.markdown("---")
        projects = projects_entry()
        st.markdown("---")
        meetings = meetings_entry()
        st.markdown("---")
        edu = education_entry()
        st.markdown("---")
        pat = patents_entry()
        st.markdown("---")
        aw = awards_entry()
        st.markdown("---")
        ed = editor_entry()
        st.markdown("---")
        oth = other_entry()

        if st.button("Hesapla"):
            payload = {
                "after_degree": after_degree,
                "articles": articles,
                "thesis_articles": thesis_articles,
                "citations": citations,
                "supervisions": superv,
                "projects": projects,
                "meetings": meetings,
                "education": edu,
                "patents": pat,
                "awards": aw,
                "editor": ed,
                "other": oth
            }
            totals = compute_points(payload)
            st.subheader("💡 Sonuçlar")
            st.metric("Toplam (Tüm Kalemler)", f"{totals.total:.2f}")
            st.metric("Toplam (Tez yayınları hariç)", f"{totals.total_excluding_thesis:.2f}")
            st.write("**Kontroller** (yeşil = sağlandı):")
            for k, v in totals.checks.items():
                st.write(f"- {'✅' if v else '❌'} {k}")
            st.subheader("Döküm")
            st.json(totals.breakdown)

            if st.button("Kaydet"):
                save_record(owner=user["username"], payload=payload, total=totals.total, breakdown=totals.breakdown)
                st.success("Kayıt edildi.")

    with tabs[1]:
        st.markdown("### Kayıtlarım")
        recs = list_records(owner=user["username"])
        st.write(f"Toplam kendi kaydınız: {len(recs)}")
        for r in recs:
            with st.expander(f"#{r['id']} • {r['created_at']} • Toplam: {r['total']}"):
                st.json(json.loads(r["payload"]))
                st.json(json.loads(r["breakdown"]))
                if st.button("Sil", key=f"mydel_{r['id']}"):
                    delete_record(r["id"])
                    st.success("Silindi.")
                    st.experimental_rerun()
        if recs:
            js = [dict(r) for r in recs]
            st.download_button("Kayıtları JSON indir", json.dumps(js, ensure_ascii=False, indent=2),
                               file_name="kayitlarim.json")

    with tabs[2]:
        st.markdown("### Hakkında")
        st.write("""
Bu uygulama, 2025 Sağlık Bilimleri **Tablo 10** esas alınarak hazırlanmış bir puan hesaplayıcıdır.
- 1–2: Uluslararası/Ulusal makaleler (tez dışı)
- 3: Lisansüstü tezlerden üretilmiş yayınlar (max 20)
- 5–13: Diğer kalemler (atıf, danışmanlık, proje, toplantı, eğitim-öğretim, patent, ödül, editörlük, diğer)
**ÖNEMLİ:** Asgari koşullar ve “sonra/önce” ayrımları kullanıcının doğru veri girişi ile sağlanır.
""")

    if user["is_admin"] and len(tabs) >= 4:
        with tabs[3]:
            admin_panel()

if __name__ == "__main__":
    main()
