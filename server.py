"""
IoT Smart Home Server — Ciseeng, Kab. Bogor
- Flask + SQLite + Gemini AI
- AES-256-GCM Encryption
- DHT11 (suhu/kelembaban) + MQ-2 (asap)
- Kipas GPIO 2 (built-in LED) + Lampu GPIO 4
- Cuaca: Open-Meteo (lat -6.3328, lon 106.8312)
- Jadwal Sholat: MyQuran API (Kab. Bogor)
- Telegram: teks, voice, perintah kipas & lampu
- Web Dashboard: monitoring + AI chat + jadwal sholat
- Notifikasi otomatis dengan cooldown anti-spam
Server IP: 192.168.92.30
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from google import genai
import requests
import sqlite3
import os
import threading
import time
from datetime import datetime, timedelta
import json
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

# ============================================================
# KONFIGURASI
# ============================================================
TELEGRAM_BOT_TOKEN = "8455077735:AAFUibm5q_kDQPOPg6iB_pGgslY8mTiscn8"
TELEGRAM_CHAT_ID   = "1793496453"
GEMINI_API_KEY     = "AIzaSyCWdLw3OZzFTTKb9iHB4kBbVD6gzBLJ4jw"
SERVER_IP          = "192.168.92.30"
SERVER_PORT        = 5000
DB_PATH            = 'data/sensor.db'

TEMP_MAX           = 35
TEMP_MIN           = 15
HUMID_MAX          = 80
HUMID_MIN          = 30
SMOKE_WARNING      = 600
SMOKE_CRITICAL     = 1200
COOLDOWN_SECONDS   = 300

AES_KEY = bytes.fromhex("9dbc8c1c9432a82af784a952592a908e72896018b7d1c6f61f8eef518d426ab0")

# Lokasi Ciseeng untuk Open-Meteo
LATITUDE       = -6.3328
LONGITUDE      = 106.8312
KOTA_SHOLAT_ID = 501       # Kab. Bogor — MyQuran API

# ============================================================
# STATE LED
# ============================================================
device_state = {
    "led":            False,   # kipas (GPIO 2)
    "lampu":          False,   # lampu (GPIO 4)
    "updated_at":     None,
    "updated_by":     None,
    "lampu_updated_at": None,
    "lampu_updated_by": None
}
state_lock   = threading.Lock()

def set_led(value: bool, source: str = 'unknown'):
    with state_lock:
        device_state['led']        = value
        device_state['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        device_state['updated_by'] = source
    print(f"💡 LED → {'ON' if value else 'OFF'} (oleh: {source})")

def get_led() -> bool:
    with state_lock:
        return device_state['led']

def set_lampu(value: bool, source: str = 'unknown'):
    with state_lock:
        device_state['lampu']          = value
        device_state['lampu_updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        device_state['lampu_updated_by'] = source
    print(f"💡 LAMPU → {'ON' if value else 'OFF'} (oleh: {source})")

def get_lampu() -> bool:
    with state_lock:
        return device_state['lampu']

# ============================================================
# COOLDOWN ALERT
# ============================================================
_last_alert_time = {}
_alert_lock      = threading.Lock()

def can_send_alert(key):
    with _alert_lock:
        last = _last_alert_time.get(key)
        if last is None or (datetime.now() - last).total_seconds() >= COOLDOWN_SECONDS:
            _last_alert_time[key] = datetime.now()
            return True
        return False

def reset_alert_cooldown(key):
    with _alert_lock:
        _last_alert_time.pop(key, None)

# ============================================================
# PRINT HEADER
# ============================================================
print("=" * 60)
print("🏠 IoT Smart Home Server — Ciseeng, Kab. Bogor")
print("=" * 60)
print(f"📡 Server    : {SERVER_IP}:{SERVER_PORT}")
print(f"💬 Telegram  : {TELEGRAM_CHAT_ID}")
print(f"🌀 Kipas     : GPIO 2 (built-in LED)")
print(f"💡 Lampu     : GPIO 4 (LED eksternal)")
print(f"🌍 Lokasi    : lat {LATITUDE}, lon {LONGITUDE}")
print(f"🕌 Kota Sholat: Kab. Bogor (ID {KOTA_SHOLAT_ID})")
print("=" * 60)

# ============================================================
# GEMINI AI
# ============================================================
try:
    ai_client  = genai.Client(api_key=GEMINI_API_KEY)
    MODEL_NAME = "gemini-2.5-flash"
    print(f"✅ Gemini AI ready — {MODEL_NAME}")
except Exception as e:
    print(f"❌ Gemini error: {e}")
    exit(1)

# ============================================================
# FLASK
# ============================================================
app = Flask(__name__, static_folder='static')
CORS(app)
os.makedirs('data',   exist_ok=True)
os.makedirs('static', exist_ok=True)

# ============================================================
# DATABASE
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn   = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            temperature REAL NOT NULL,
            humidity REAL NOT NULL,
            smoke INTEGER NOT NULL DEFAULT 0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            severity TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS control_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL,
            action TEXT NOT NULL,
            source TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Migrasi kolom smoke
    cursor.execute("PRAGMA table_info(readings)")
    cols = [r['name'] for r in cursor.fetchall()]
    if 'smoke' not in cols:
        cursor.execute("ALTER TABLE readings ADD COLUMN smoke INTEGER NOT NULL DEFAULT 0")
        print("✅ Migrasi: kolom smoke ditambahkan")
    conn.commit()
    conn.close()
    print("✅ Database siap!")

init_db()

def db_insert_reading(device_id, temperature, humidity, smoke=0):
    try:
        conn = get_db()
        conn.execute(
            'INSERT INTO readings (device_id, temperature, humidity, smoke) VALUES (?, ?, ?, ?)',
            (device_id, temperature, humidity, smoke)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ DB insert error: {e}")
        return False

def db_insert_alert(message, severity):
    try:
        conn = get_db()
        conn.execute('INSERT INTO alerts (message, severity) VALUES (?, ?)', (message, severity))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ DB alert error: {e}")

def db_insert_control_log(device, action, source):
    try:
        conn = get_db()
        conn.execute(
            'INSERT INTO control_log (device, action, source) VALUES (?, ?, ?)',
            (device, action, source)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ DB control log error: {e}")

def db_get_latest():
    try:
        conn   = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM readings ORDER BY timestamp DESC LIMIT 1')
        row    = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"❌ DB latest error: {e}")
        return None

def db_get_stats(hours=24):
    try:
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        conn   = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT temperature, humidity, smoke FROM readings WHERE timestamp >= ?',
            (cutoff,)
        )
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return None
        temps  = [r['temperature'] for r in rows]
        humids = [r['humidity']    for r in rows]
        smokes = [r['smoke']       for r in rows]
        return {
            'count': len(rows),
            'temperature': {'avg': round(sum(temps)/len(temps),1),  'min': round(min(temps),1),  'max': round(max(temps),1)},
            'humidity':    {'avg': round(sum(humids)/len(humids),1), 'min': round(min(humids),1), 'max': round(max(humids),1)},
            'smoke':       {'avg': round(sum(smokes)/len(smokes),1), 'min': min(smokes),          'max': max(smokes)}
        }
    except Exception as e:
        print(f"❌ DB stats error: {e}")
        return None

def db_get_recent_alerts(limit=20):
    try:
        conn   = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM alerts ORDER BY timestamp DESC LIMIT ?', (limit,))
        rows   = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return []

def db_get_control_log(limit=20):
    try:
        conn   = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM control_log ORDER BY timestamp DESC LIMIT ?', (limit,))
        rows   = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return []

# ============================================================
# TELEGRAM HELPER
# ============================================================
def send_telegram(message, chat_id=None):
    if not chat_id:
        chat_id = TELEGRAM_CHAT_ID
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'}
        resp    = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"✅ Telegram → {message[:60]}...")
            return True
        print(f"❌ Telegram gagal: {resp.status_code}")
        return False
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return False

def download_telegram_file(file_id):
    """Download file voice dari Telegram."""
    try:
        url       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
        resp      = requests.get(url, timeout=10)
        file_path = resp.json()['result']['file_path']
        dl_url    = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        return requests.get(dl_url, timeout=15).content
    except Exception as e:
        print(f"❌ Download file error: {e}")
        return None

# ============================================================
# PARSE PERINTAH LED — kata kunci natural bahasa Indonesia
# ============================================================
def parse_device_command(text: str):
    """
    Deteksi perintah perangkat menggunakan Gemini AI.
    Return dict: {"device": "kipas"/"lampu"/None, "action": "on"/"off"/None}
    """
    try:
        prompt = (
            "Dari teks berikut, tentukan perintah untuk perangkat smart home.\n"
            "Perangkat yang ada: kipas (fan), lampu (light).\n"
            "Jawab HANYA dalam format: device:action\n"
            "Contoh: kipas:on | lampu:off | none:none\n"
            "Jangan tambahkan kata lain apapun.\n\n"
            f"Teks: {text}"
        )
        resp = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=[{"role": "user", "parts": [{"text": prompt}]}]
        )
        result = resp.text.strip().lower().replace(" ", "")
        print(f"🔍 parse_device_command({text!r}) → {result!r}")

        if ':' in result:
            parts  = result.split(':')
            device = parts[0].strip()
            action = parts[1].strip()
            if device in ['kipas', 'fan', 'led'] and action in ['on', 'off']:
                return {'device': 'kipas', 'action': action}
            elif device in ['lampu', 'light', 'lamp'] and action in ['on', 'off']:
                return {'device': 'lampu', 'action': action}
        return {'device': None, 'action': None}

    except Exception as e:
        print(f"⚠️  parse_device_command fallback ke keyword: {e}")
        t = text.lower().strip()
        # Deteksi action
        on_kw  = ['nyalakan', 'hidupkan', 'nyala', 'hidup', 'aktifkan', 'on']
        off_kw = ['matikan', 'padamkan', 'mati', 'padam', 'nonaktifkan', 'off']
        action = None
        if any(kw in t for kw in off_kw): action = 'off'
        elif any(kw in t for kw in on_kw): action = 'on'
        if not action:
            return {'device': None, 'action': None}
        # Deteksi device
        if 'kipas' in t or 'fan' in t:
            return {'device': 'kipas', 'action': action}
        elif 'lampu' in t or 'lamp' in t or 'cahaya' in t or 'light' in t:
            return {'device': 'lampu', 'action': action}
        return {'device': None, 'action': None}

# Alias untuk backward compatibility
def parse_led_command(text: str):
    result = parse_device_command(text)
    if result['device'] == 'kipas':
        return result['action']
    return None

# ============================================================
# CUACA OPEN-METEO
# ============================================================
_weather_cache = {'data': None, 'updated_at': None}
_weather_lock  = threading.Lock()

def fetch_weather_data():
    """Fetch cuaca dari Open-Meteo, cache 10 menit."""
    with _weather_lock:
        now = datetime.now()
        if (_weather_cache['data'] and _weather_cache['updated_at'] and
                (now - _weather_cache['updated_at']).total_seconds() < 600):
            return _weather_cache['data']
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={LATITUDE}&longitude={LONGITUDE}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            f"weather_code,wind_speed_10m,precipitation,uv_index,is_day,cloud_cover"
            f"&daily=weather_code,temperature_2m_max,temperature_2m_min,"
            f"precipitation_probability_max"
            f"&timezone=Asia%2FJakarta&forecast_days=3"
        )
        r    = requests.get(url, timeout=10)
        data = r.json()
        with _weather_lock:
            _weather_cache['data']       = data
            _weather_cache['updated_at'] = datetime.now()
        print(f"🌤️  Cuaca update: {data['current']['temperature_2m']}°C, kode {data['current']['weather_code']}")
        return data
    except Exception as e:
        print(f"⚠️  Fetch cuaca gagal: {e}")
        with _weather_lock:
            return _weather_cache['data']  # return cache lama jika ada

WEATHER_CODE_ID = {
    0:'Cerah', 1:'Hampir Cerah', 2:'Berawan Sebagian', 3:'Mendung',
    45:'Berkabut', 48:'Berkabut', 51:'Gerimis Ringan', 53:'Gerimis',
    55:'Gerimis Lebat', 61:'Hujan Ringan', 63:'Hujan', 65:'Hujan Lebat',
    71:'Salju Ringan', 80:'Hujan Lokal', 81:'Hujan Lokal Sedang',
    82:'Hujan Lokal Lebat', 95:'Badai Petir', 99:'Badai Petir Besar'
}

# ============================================================
# JADWAL SHOLAT — MyQuran API
# ============================================================
_sholat_cache = {'data': None, 'date': None}
_sholat_lock  = threading.Lock()

def fetch_sholat_data():
    """Fetch jadwal sholat dari MyQuran, cache harian."""
    today = datetime.now().strftime('%Y-%m-%d')
    with _sholat_lock:
        if _sholat_cache['data'] and _sholat_cache['date'] == today:
            return _sholat_cache['data']
    try:
        now = datetime.now()
        url = (
            f"https://api.myquran.com/v2/sholat/jadwal/{KOTA_SHOLAT_ID}"
            f"/{now.year}/{now.month:02d}/{now.day:02d}"
        )
        r    = requests.get(url, timeout=10)
        data = r.json()
        if data.get('status'):
            jadwal = data['data']['jadwal']
            with _sholat_lock:
                _sholat_cache['data'] = jadwal
                _sholat_cache['date'] = today
            print(f"🕌 Jadwal sholat update: Subuh {jadwal.get('subuh','--')}, Maghrib {jadwal.get('maghrib','--')}")
            return jadwal
        return None
    except Exception as e:
        print(f"⚠️  Fetch sholat gagal: {e}")
        with _sholat_lock:
            return _sholat_cache['data']

def get_next_sholat():
    """Dapatkan waktu sholat berikutnya."""
    jadwal = fetch_sholat_data()
    if not jadwal:
        return None, None
    now_min = datetime.now().hour * 60 + datetime.now().minute
    waktu_list = {
        'Subuh':   jadwal.get('subuh', ''),
        'Dzuhur':  jadwal.get('dzuhur', ''),
        'Ashar':   jadwal.get('ashar', ''),
        'Maghrib': jadwal.get('maghrib', ''),
        'Isya':    jadwal.get('isya', '')
    }
    next_name, next_time, next_min = None, None, 9999
    for nama, waktu in waktu_list.items():
        if not waktu: continue
        h, m = map(int, waktu.split(':'))
        tm   = h * 60 + m
        if tm > now_min and tm < next_min:
            next_min, next_name, next_time = tm, nama, waktu
    return next_name, next_time

# ============================================================
# BANGUN KONTEKS SENSOR UNTUK AI
# ============================================================
def build_context():
    """Buat string konteks kondisi sensor + LED untuk dikirim ke AI."""
    latest = db_get_latest()
    stats  = db_get_stats(24)
    kipas = '🌀 ON (menyala)' if get_led()   else '⬛ OFF (mati)'
    lampu = '💡 ON (menyala)' if get_lampu() else '⬛ OFF (mati)'

    ctx = f"Status kipas: {kipas}. Status lampu: {lampu}.\n"

    if latest:
        t = latest['temperature']
        h = latest['humidity']
        s = latest['smoke']
        t_status = "TERLALU TINGGI" if t > TEMP_MAX else "TERLALU RENDAH" if t < TEMP_MIN else "normal"
        h_status = "TERLALU TINGGI" if h > HUMID_MAX else "TERLALU RENDAH" if h < HUMID_MIN else "normal"
        s_status = "KRITIS" if s > SMOKE_CRITICAL else "PERHATIAN" if s > SMOKE_WARNING else "aman"
        ctx += (
            f"Sensor dalam ruangan — Suhu: {t}°C ({t_status}), "
            f"Kelembaban: {h}% ({h_status}), "
            f"Asap MQ-2: {s} ({s_status}).\n"
        )
    else:
        ctx += "Belum ada data sensor.\n"

    if stats:
        ctx += (
            f"Rata-rata 24 jam — Suhu: {stats['temperature']['avg']}°C, "
            f"Kelembaban: {stats['humidity']['avg']}%, "
            f"Asap: {stats['smoke']['avg']}.\n"
        )

    # Tambah data cuaca luar
    try:
        wx = fetch_weather_data()
        if wx:
            c    = wx['current']
            code = c.get('weather_code', 0)
            desc = WEATHER_CODE_ID.get(code, f"kode {code}")
            ctx += (
                f"Cuaca luar ruangan (Ciseeng, Kab. Bogor) — "
                f"Suhu: {c['temperature_2m']}°C (terasa {c['apparent_temperature']}°C), "
                f"Kondisi: {desc}, "
                f"Kelembaban luar: {c['relative_humidity_2m']}%, "
                f"Angin: {c['wind_speed_10m']} km/jam, "
                f"Hujan: {c['precipitation']} mm, "
                f"UV index: {c['uv_index']}.\n"
            )
    except Exception as e:
        ctx += f"Data cuaca tidak tersedia.\n"

    # Tambah jadwal sholat
    try:
        jadwal = fetch_sholat_data()
        if jadwal:
            next_name, next_time = get_next_sholat()
            ctx += (
                f"Jadwal sholat hari ini (Kab. Bogor) — "
                f"Subuh: {jadwal.get('subuh','--')}, "
                f"Dzuhur: {jadwal.get('dzuhur','--')}, "
                f"Ashar: {jadwal.get('ashar','--')}, "
                f"Maghrib: {jadwal.get('maghrib','--')}, "
                f"Isya: {jadwal.get('isya','--')}. "
            )
            if next_name:
                ctx += f"Waktu sholat berikutnya: {next_name} pukul {next_time}.\n"
    except Exception as e:
        ctx += "Jadwal sholat tidak tersedia.\n"

    return ctx

# ============================================================
# PROSES PESAN VIA GEMINI AI (teks)
# ============================================================
def ask_gemini(user_text: str, context: str) -> str:
    """Kirim pertanyaan ke Gemini dengan konteks sistem."""
    prompt = (
        "Kamu adalah asisten smart home yang ramah dan informatif untuk rumah di Ciseeng, Kab. Bogor. "
        "Kamu tahu kondisi sensor dalam ruangan, cuaca luar dari Open-Meteo, dan jadwal sholat hari ini. "
        "Jawab dalam Bahasa Indonesia dengan emoji yang sesuai. Jawab singkat, padat, dan jelas.\n\n"
        f"Konteks sistem saat ini:\n{context}\n\n"
        f"Pesan pengguna: {user_text}\n\n"
        "Jawaban:"
    )
    resp = ai_client.models.generate_content(
        model=MODEL_NAME,
        contents=[{"role": "user", "parts": [{"text": prompt}]}]
    )
    return resp.text

# ============================================================
# PROSES VOICE MESSAGE — transkripsi + AI + perintah lampu
# ============================================================
def process_voice_message(file_id, chat_id):
    """
    Alur:
    1. Download file voice dari Telegram
    2. Kirim audio ke Gemini untuk transkripsi + deteksi perintah
    3. Jika ada perintah lampu → eksekusi
    4. Jawab dengan AI (seperti chat teks biasa)
    """
    send_telegram("🎤 Memproses pesan suara...", chat_id)

    audio_bytes = download_telegram_file(file_id)
    if not audio_bytes:
        send_telegram("❌ Gagal mengunduh pesan suara. Coba lagi.", chat_id)
        return

    # ── LANGKAH 1: Transkripsi audio via Gemini ──────────────
    try:
        audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')

        transkripsi_prompt = (
            "Transkripsikan pesan suara Bahasa Indonesia ini menjadi teks. "
            "Kembalikan HANYA teks transkripsinya saja, tanpa penjelasan apapun."
        )

        transkripsi_resp = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=[{
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "audio/ogg",
                            "data": audio_b64
                        }
                    },
                    {"text": transkripsi_prompt}
                ]
            }]
        )
        transkripsi = transkripsi_resp.text.strip()
        print(f"🎤 Transkripsi: {transkripsi}")

    except Exception as e:
        print(f"❌ Transkripsi error: {e}")
        # Fallback: kalau audio tidak bisa diproses Gemini
        send_telegram(
            "❌ Maaf, pesan suara tidak bisa ditranskripsikan.\n"
            "Kemungkinan format audio tidak didukung.\n"
            "Coba kirim pesan <b>teks</b> saja ya.",
            chat_id
        )
        return

    # ── LANGKAH 2: Cek perintah perangkat dari transkripsi ───
    cmd = parse_device_command(transkripsi)
    action_text = None

    if cmd['device'] == 'kipas' and cmd['action'] == 'on':
        set_led(True, 'telegram_voice')
        db_insert_control_log('kipas', 'ON', 'telegram_voice')
        action_text = "🌀 <b>Kipas dinyalakan!</b>"
        print("🌀 Voice command: Kipas ON")
    elif cmd['device'] == 'kipas' and cmd['action'] == 'off':
        set_led(False, 'telegram_voice')
        db_insert_control_log('kipas', 'OFF', 'telegram_voice')
        action_text = "🌀 <b>Kipas dimatikan!</b>"
        print("🌀 Voice command: Kipas OFF")
    elif cmd['device'] == 'lampu' and cmd['action'] == 'on':
        set_lampu(True, 'telegram_voice')
        db_insert_control_log('lampu', 'ON', 'telegram_voice')
        action_text = "💡 <b>Lampu dinyalakan!</b>"
        print("💡 Voice command: Lampu ON")
    elif cmd['device'] == 'lampu' and cmd['action'] == 'off':
        set_lampu(False, 'telegram_voice')
        db_insert_control_log('lampu', 'OFF', 'telegram_voice')
        action_text = "💡 <b>Lampu dimatikan!</b>"
        print("💡 Voice command: Lampu OFF")

    # ── LANGKAH 3: Jawab dengan AI (seperti chat teks) ───────
    try:
        context   = build_context()
        ai_answer = ask_gemini(transkripsi, context)
    except Exception as e:
        print(f"❌ AI error: {e}")
        ai_answer = "Maaf, AI tidak bisa menjawab saat ini."

    # ── LANGKAH 4: Kirim balasan ──────────────────────────────
    reply = f"🎤 <b>Pesan suara</b>\n📝 <i>\"{transkripsi}\"</i>\n"
    if action_text:
        reply += f"\n{action_text} ✅\n"
    reply += f"\n🤖 {ai_answer}"

    send_telegram(reply, chat_id)

# ============================================================
# ALERT OTOMATIS
# ============================================================
def process_alerts(temperature, humidity, smoke):
    alerts_fired = []
    now_str = datetime.now().strftime('%H:%M:%S')

    # Suhu tinggi → kipas otomatis ON
    if temperature > TEMP_MAX:
        # Nyalakan kipas otomatis
        if not get_led():
            set_led(True, 'auto_suhu')
            db_insert_control_log('kipas', 'ON', 'auto_suhu')
            print(f"🌀 Kipas otomatis ON — suhu {temperature}°C")
        if can_send_alert('temp_high'):
            send_telegram(
                f"🚨 <b>SUHU TERLALU TINGGI</b>\n"
                f"Suhu: <b>{temperature}°C</b>\n"
                f"🌀 Kipas otomatis dinyalakan!\n"
                f"Waktu: {now_str}"
            )
            db_insert_alert(f"Suhu tinggi: {temperature}°C — kipas ON otomatis", 'critical')
            alerts_fired.append('temp_high')
    else:
        if 'temp_high' in _last_alert_time:
            reset_alert_cooldown('temp_high')
            # Matikan kipas otomatis saat suhu normal (hanya jika dinyalakan otomatis)
            if get_led() and device_state.get('updated_by') == 'auto_suhu':
                set_led(False, 'auto_suhu')
                db_insert_control_log('kipas', 'OFF', 'auto_suhu')
                send_telegram(
                    f"✅ <b>Suhu kembali normal</b>: {temperature}°C\n"
                    f"🌀 Kipas otomatis dimatikan."
                )
                db_insert_alert(f"Suhu normal: {temperature}°C — kipas OFF otomatis", 'info')
            else:
                send_telegram(f"✅ <b>Suhu kembali normal</b>: {temperature}°C")
                db_insert_alert(f"Suhu normal: {temperature}°C", 'info')

    # Suhu rendah
    if temperature < TEMP_MIN:
        if can_send_alert('temp_low'):
            send_telegram(f"❄️ <b>SUHU TERLALU RENDAH</b>\nSuhu: <b>{temperature}°C</b>\nWaktu: {now_str}")
            db_insert_alert(f"Suhu rendah: {temperature}°C", 'warning')
            alerts_fired.append('temp_low')
    else:
        if 'temp_low' in _last_alert_time:
            reset_alert_cooldown('temp_low')
            send_telegram(f"✅ <b>Suhu kembali normal</b>: {temperature}°C")

    # Kelembaban tinggi
    if humidity > HUMID_MAX:
        if can_send_alert('humid_high'):
            send_telegram(f"💧 <b>KELEMBABAN TINGGI</b>\nKelembaban: <b>{humidity}%</b>\nWaktu: {now_str}")
            db_insert_alert(f"Kelembaban tinggi: {humidity}%", 'warning')
            alerts_fired.append('humid_high')
    else:
        if 'humid_high' in _last_alert_time:
            reset_alert_cooldown('humid_high')
            send_telegram(f"✅ <b>Kelembaban kembali normal</b>: {humidity}%")

    # Kelembaban rendah
    if humidity < HUMID_MIN:
        if can_send_alert('humid_low'):
            send_telegram(f"🌵 <b>KELEMBABAN RENDAH</b>\nKelembaban: <b>{humidity}%</b>\nWaktu: {now_str}")
            db_insert_alert(f"Kelembaban rendah: {humidity}%", 'warning')
            alerts_fired.append('humid_low')
    else:
        if 'humid_low' in _last_alert_time:
            reset_alert_cooldown('humid_low')
            send_telegram(f"✅ <b>Kelembaban kembali normal</b>: {humidity}%")

    # Asap kritis → kipas ON otomatis (sirkulasi udara)
    if smoke > SMOKE_CRITICAL:
        if not get_led():
            set_led(True, 'auto_asap')
            db_insert_control_log('kipas', 'ON', 'auto_asap')
            print(f"🌀 Kipas otomatis ON — asap kritis {smoke}")
        if can_send_alert('smoke_critical'):
            send_telegram(
                f"🔥 <b>ASAP KRITIS!</b>\n"
                f"MQ-2: <b>{smoke}</b>\n"
                f"⚠️ Periksa ruangan!\n"
                f"🌀 Kipas otomatis dinyalakan untuk sirkulasi!\n"
                f"Waktu: {now_str}"
            )
            db_insert_alert(f"Asap kritis: {smoke} — kipas ON otomatis", 'critical')
            alerts_fired.append('smoke_critical')
    else:
        if 'smoke_critical' in _last_alert_time:
            reset_alert_cooldown('smoke_critical')
            send_telegram(f"✅ <b>Asap kembali aman</b>: {smoke}")

    # Asap warning
    if SMOKE_WARNING < smoke <= SMOKE_CRITICAL:
        if can_send_alert('smoke_warning'):
            send_telegram(f"⚠️ <b>Indikasi Asap</b>\nMQ-2: <b>{smoke}</b>\nWaktu: {now_str}")
            db_insert_alert(f"Asap warning: {smoke}", 'warning')
            alerts_fired.append('smoke_warning')
    else:
        if 'smoke_warning' in _last_alert_time:
            reset_alert_cooldown('smoke_warning')

    return alerts_fired

# ============================================================
# FLASK ROUTES
# ============================================================

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

# ── Sensor data dari ESP32 ────────────────────────────────
@app.route('/api/sensor', methods=['POST'])
def receive_sensor():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'status': 'error', 'message': 'No JSON'}), 400

        # Dekripsi AES-256-GCM
        if 'encrypted_data' in data and 'nonce' in data:
            try:
                encrypted = base64.b64decode(data['encrypted_data'])
                nonce     = base64.b64decode(data['nonce'])
                aesgcm    = AESGCM(AES_KEY)
                plaintext = aesgcm.decrypt(nonce, encrypted, None)
                data      = json.loads(plaintext.decode('utf-8'))
                print("✅ Data decrypted")
            except InvalidTag:
                return jsonify({'status': 'error', 'message': 'Invalid encryption'}), 400
            except Exception as e:
                return jsonify({'status': 'error', 'message': f'Decryption failed: {e}'}), 400

        device_id   = data.get('device_id', 'ESP32_SMART_HOME')
        temperature = float(data.get('temperature', 0))
        humidity    = float(data.get('humidity', 0))
        smoke       = int(data.get('smoke', 0))

        if not (-40 <= temperature <= 80) or not (0 <= humidity <= 100):
            return jsonify({'status': 'error', 'message': 'Sensor data out of range'}), 400

        db_insert_reading(device_id, temperature, humidity, smoke)
        print(f"📊 {temperature}°C | {humidity}% | Smoke:{smoke}")

        alerts_fired = process_alerts(temperature, humidity, smoke)

        return jsonify({
            'status': 'success',
            'temperature': temperature,
            'humidity':    humidity,
            'smoke':       smoke,
            'alerts':      alerts_fired
        }), 200

    except Exception as e:
        print(f"❌ /api/sensor error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ── API Weather data ─────────────────────────────────────
@app.route('/api/weather', methods=['GET'])
def api_weather():
    """Return cuaca luar dari Open-Meteo."""
    try:
        data = fetch_weather_data()
        if not data:
            return jsonify({'status': 'error', 'message': 'Data cuaca tidak tersedia'}), 503
        return jsonify({'status': 'success', 'data': data})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ── API Jadwal Sholat ─────────────────────────────────────
@app.route('/api/sholat', methods=['GET'])
def api_sholat():
    """Return jadwal sholat hari ini dari MyQuran."""
    try:
        jadwal = fetch_sholat_data()
        if not jadwal:
            return jsonify({'status': 'error', 'message': 'Jadwal sholat tidak tersedia'}), 503
        next_name, next_time = get_next_sholat()
        return jsonify({
            'status': 'success',
            'data': {
                'jadwal':     jadwal,
                'next_name':  next_name,
                'next_time':  next_time,
                'kota_id':    KOTA_SHOLAT_ID,
                'lokasi':     'Ciseeng, Kab. Bogor'
            }
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ── ESP32 polling state LED ───────────────────────────────
@app.route('/api/state', methods=['GET'])
def api_state():
    with state_lock:
        return jsonify({
            'status':           'success',
            'led':              device_state['led'],
            'lampu':            device_state['lampu'],
            'updated_at':       device_state['updated_at'],
            'updated_by':       device_state['updated_by'],
            'lampu_updated_at': device_state['lampu_updated_at'],
            'lampu_updated_by': device_state['lampu_updated_by']
        })

# ── Kontrol perangkat dari web ───────────────────────────
@app.route('/api/control', methods=['POST'])
def api_control():
    """
    Body: {"device": "led"/"lampu", "action": "on"/"off", "source": "web"}
    """
    try:
        data   = request.get_json()
        device = data.get('device', '').lower()
        action = data.get('action', '').lower()
        source = data.get('source', 'web')

        if device not in ['led', 'lampu']:
            return jsonify({'status': 'error', 'message': 'Device tidak dikenal'}), 400
        if action not in ['on', 'off']:
            return jsonify({'status': 'error', 'message': 'Action harus on atau off'}), 400

        value = (action == 'on')

        if device == 'led':
            set_led(value, source)
            db_insert_control_log('kipas', action.upper(), source)
            msg = f"Kipas berhasil {'dinyalakan' if value else 'dimatikan'}"
        else:
            set_lampu(value, source)
            db_insert_control_log('lampu', action.upper(), source)
            msg = f"Lampu berhasil {'dinyalakan' if value else 'dimatikan'}"

        return jsonify({
            'status':  'success',
            'message': msg,
            'led':     get_led(),
            'lampu':   get_lampu()
        })

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ── Chat AI dari web ──────────────────────────────────────
@app.route('/api/chat', methods=['POST'])
def api_chat():
    """
    Terima pesan teks dari web, proses perintah lampu jika ada,
    lalu jawab dengan AI.
    Body: {"message": "teks pengguna"}
    """
    try:
        data    = request.get_json()
        message = data.get('message', '').strip()
        if not message:
            return jsonify({'status': 'error', 'message': 'Pesan kosong'}), 400

        # Cek perintah perangkat (kipas / lampu)
        cmd          = parse_device_command(message)
        action_taken = None

        if cmd['device'] == 'kipas' and cmd['action'] == 'on':
            set_led(True, 'web_chat')
            db_insert_control_log('kipas', 'ON', 'web_chat')
            action_taken = '🌀 Kipas dinyalakan'
        elif cmd['device'] == 'kipas' and cmd['action'] == 'off':
            set_led(False, 'web_chat')
            db_insert_control_log('kipas', 'OFF', 'web_chat')
            action_taken = '🌀 Kipas dimatikan'
        elif cmd['device'] == 'lampu' and cmd['action'] == 'on':
            set_lampu(True, 'web_chat')
            db_insert_control_log('lampu', 'ON', 'web_chat')
            action_taken = '💡 Lampu dinyalakan'
        elif cmd['device'] == 'lampu' and cmd['action'] == 'off':
            set_lampu(False, 'web_chat')
            db_insert_control_log('lampu', 'OFF', 'web_chat')
            action_taken = '💡 Lampu dimatikan'

        # Jawab dengan AI
        context   = build_context()
        ai_answer = ask_gemini(message, context)

        return jsonify({
            'status':       'success',
            'reply':        ai_answer,
            'action_taken': action_taken,
            'led':          get_led(),
            'lampu':        get_lampu()
        })

    except Exception as e:
        print(f"❌ /api/chat error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ── Endpoint data ─────────────────────────────────────────
@app.route('/api/latest', methods=['GET'])
def api_latest():
    reading = db_get_latest()
    if reading:
        return jsonify({'status': 'success', 'data': reading})
    return jsonify({'status': 'error', 'message': 'Belum ada data'}), 404

@app.route('/api/statistics', methods=['GET'])
def api_statistics():
    hours = int(request.args.get('hours', 24))
    stats = db_get_stats(hours)
    if stats:
        return jsonify({'status': 'success', 'data': stats})
    return jsonify({'status': 'error', 'message': 'Belum ada data'}), 404

@app.route('/api/alerts', methods=['GET'])
def api_alerts():
    limit  = int(request.args.get('limit', 20))
    alerts = db_get_recent_alerts(limit)
    return jsonify({'status': 'success', 'data': alerts})

@app.route('/api/history', methods=['GET'])
def api_history():
    try:
        limit  = int(request.args.get('limit', 50))
        conn   = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT temperature, humidity, smoke, timestamp FROM readings ORDER BY timestamp DESC LIMIT ?',
            (limit,)
        )
        rows = cursor.fetchall()
        conn.close()
        return jsonify({'status': 'success', 'data': [dict(r) for r in reversed(rows)]})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/control-log', methods=['GET'])
def api_control_log():
    limit = int(request.args.get('limit', 20))
    logs  = db_get_control_log(limit)
    return jsonify({'status': 'success', 'data': logs})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status':   'running',
        'led':      get_led(),
        'database': DB_PATH,
        'ai':       MODEL_NAME,
        'time':     datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })

# ============================================================
# TELEGRAM BOT LISTENER
# ============================================================
def telegram_bot_listener():
    print("🤖 Telegram bot listener started...")

    send_telegram(
        "🏠 <b>Smart Home Monitor Online!</b>\n\n"
        "<b>Perintah sensor:</b>\n"
        "/suhu — Data sensor terkini\n"
        "/stats — Statistik 24 jam\n"
        "/alerts — Alert terakhir\n\n"
        "<b>Perintah lampu:</b>\n"
        "/lampu on — Nyalakan lampu\n"
        "/lampu off — Matikan lampu\n"
        "/status — Status perangkat\n\n"
        "<b>Natural language:</b>\n"
        "\"tolong nyalakan lampu\"\n"
        "\"suhu sekarang berapa?\"\n\n"
        "<b>Voice:</b> Kirim pesan suara 🎤\n"
        "(transkripsi otomatis + dijawab AI)\n\n"
        "Powered by Gemini AI 🤖"
    )

    last_update_id = 0

    while True:
        try:
            url    = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {'offset': last_update_id + 1, 'timeout': 5}
            resp   = requests.get(url, params=params, timeout=10)
            result = resp.json()

            if not result.get('ok'):
                time.sleep(5)
                continue

            for update in result.get('result', []):
                last_update_id = update['update_id']
                if 'message' not in update:
                    continue

                msg     = update['message']
                chat_id = str(msg['chat']['id'])

                # ── VOICE MESSAGE ─────────────────────────────
                # Proses voice: transkripsi + cek perintah lampu + jawab AI
                if 'voice' in msg:
                    file_id = msg['voice']['file_id']
                    t = threading.Thread(
                        target=process_voice_message,
                        args=(file_id, chat_id),
                        daemon=True
                    )
                    t.start()
                    continue

                text = msg.get('text', '').strip()
                if not text:
                    continue

                print(f"💬 [{chat_id}] {text}")

                # ── /start /help ──────────────────────────────
                if text in ['/start', '/help']:
                    send_telegram(
                        "🤖 <b>Smart Home Assistant</b>\n\n"
                        "<b>Sensor:</b>\n"
                        "/suhu — Data terkini\n"
                        "/stats — Statistik 24 jam\n"
                        "/alerts — Alert terakhir\n\n"
                        "<b>Kipas (GPIO 2):</b>\n"
                        "/kipas on — Nyalakan kipas\n"
                        "/kipas off — Matikan kipas\n\n"
                        "<b>Lampu (GPIO 4):</b>\n"
                        "/lampu on — Nyalakan lampu\n"
                        "/lampu off — Matikan lampu\n\n"
                        "/status — Status semua perangkat\n\n"
                        "<b>Teks/Voice bebas:</b>\n"
                        "Tanya apa saja atau perintah natural 🎤",
                        chat_id
                    )

                # ── /kipas on ─────────────────────────────────
                elif text.lower() in ['/kipas on', '/kipas_on', '/led on', '/on']:
                    set_led(True, 'telegram')
                    db_insert_control_log('kipas', 'ON', 'telegram')
                    send_telegram(
                        "🌀 <b>Kipas dinyalakan!</b> ✅\n"
                        f"Waktu: {datetime.now().strftime('%H:%M:%S')}",
                        chat_id
                    )

                # ── /kipas off ────────────────────────────────
                elif text.lower() in ['/kipas off', '/kipas_off', '/led off', '/off']:
                    set_led(False, 'telegram')
                    db_insert_control_log('kipas', 'OFF', 'telegram')
                    send_telegram(
                        "🌀 <b>Kipas dimatikan!</b> ✅\n"
                        f"Waktu: {datetime.now().strftime('%H:%M:%S')}",
                        chat_id
                    )

                # ── /lampu on ─────────────────────────────────
                elif text.lower() in ['/lampu on', '/lampu_on']:
                    set_lampu(True, 'telegram')
                    db_insert_control_log('lampu', 'ON', 'telegram')
                    send_telegram(
                        "💡 <b>Lampu dinyalakan!</b> ✅\n"
                        f"Waktu: {datetime.now().strftime('%H:%M:%S')}",
                        chat_id
                    )

                # ── /lampu off ────────────────────────────────
                elif text.lower() in ['/lampu off', '/lampu_off']:
                    set_lampu(False, 'telegram')
                    db_insert_control_log('lampu', 'OFF', 'telegram')
                    send_telegram(
                        "💡 <b>Lampu dimatikan!</b> ✅\n"
                        f"Waktu: {datetime.now().strftime('%H:%M:%S')}",
                        chat_id
                    )

                # ── /status ───────────────────────────────────
                elif text == '/status':
                    kipas_icon = "🌀 ON" if get_led()   else "⬛ OFF"
                    lampu_icon = "💡 ON" if get_lampu() else "⬛ OFF"
                    with state_lock:
                        k_updated = device_state.get('updated_at', '--')
                        k_by      = device_state.get('updated_by', '--')
                        l_updated = device_state.get('lampu_updated_at', '--')
                        l_by      = device_state.get('lampu_updated_by', '--')
                    send_telegram(
                        f"📟 <b>STATUS PERANGKAT</b>\n"
                        f"──────────────────────\n"
                        f"🌀 Kipas : <b>{kipas_icon}</b>\n"
                        f"   Diubah: {k_updated} ({k_by})\n"
                        f"──────────────────────\n"
                        f"💡 Lampu : <b>{lampu_icon}</b>\n"
                        f"   Diubah: {l_updated} ({l_by})",
                        chat_id
                    )

                # ── /suhu ─────────────────────────────────────
                elif text == '/suhu':
                    d = db_get_latest()
                    if d:
                        t_icon = "🚨" if d['temperature'] > TEMP_MAX else "❄️" if d['temperature'] < TEMP_MIN else "✅"
                        h_icon = "💧" if d['humidity'] > HUMID_MAX  else "🌵" if d['humidity'] < HUMID_MIN  else "✅"
                        s_icon = "🔥" if d['smoke'] > SMOKE_CRITICAL else "⚠️" if d['smoke'] > SMOKE_WARNING else "✅"
                        reply  = (
                            f"🌡️ <b>DATA SENSOR TERKINI</b>\n"
                            f"─────────────────────\n"
                            f"{t_icon} Suhu      : <b>{d['temperature']}°C</b>\n"
                            f"{h_icon} Kelembaban: <b>{d['humidity']}%</b>\n"
                            f"{s_icon} Asap MQ-2 : <b>{d['smoke']}</b>\n"
                            f"─────────────────────\n"
                            f"📟 Device: {d['device_id']}\n"
                            f"🕐 Waktu : {d['timestamp']}"
                        )
                    else:
                        reply = "❌ Belum ada data sensor."
                    send_telegram(reply, chat_id)

                # ── /stats ────────────────────────────────────
                elif text == '/stats':
                    s = db_get_stats(24)
                    if s:
                        reply = (
                            f"📊 <b>STATISTIK 24 JAM</b>\n"
                            f"─────────────────────\n"
                            f"🌡️ Suhu   : {s['temperature']['avg']}°C "
                            f"(min {s['temperature']['min']} / max {s['temperature']['max']})\n"
                            f"💧 Lembab : {s['humidity']['avg']}% "
                            f"(min {s['humidity']['min']} / max {s['humidity']['max']})\n"
                            f"🔥 Asap   : avg {s['smoke']['avg']} / max {s['smoke']['max']}\n"
                            f"📈 Total  : {s['count']} data"
                        )
                    else:
                        reply = "❌ Belum ada data statistik."
                    send_telegram(reply, chat_id)

                # ── /alerts ───────────────────────────────────
                elif text == '/alerts':
                    recent = db_get_recent_alerts(10)
                    if recent:
                        lines = ["🔔 <b>10 ALERT TERAKHIR</b>"]
                        for a in recent:
                            icon = "🚨" if a['severity'] == 'critical' else "⚠️" if a['severity'] == 'warning' else "✅"
                            lines.append(f"{icon} {a['timestamp']}\n    {a['message']}")
                        reply = "\n".join(lines)
                    else:
                        reply = "✅ Tidak ada alert."
                    send_telegram(reply, chat_id)

                # ── TEKS BEBAS: cek perintah lampu dulu, lalu AI ──
                else:
                    cmd = parse_device_command(text)

                    if cmd['device'] == 'kipas' and cmd['action'] == 'on':
                        set_led(True, 'telegram')
                        db_insert_control_log('kipas', 'ON', 'telegram')
                        send_telegram("🌀 <b>Kipas dinyalakan!</b> ✅\n"
                            f"Waktu: {datetime.now().strftime('%H:%M:%S')}", chat_id)

                    elif cmd['device'] == 'kipas' and cmd['action'] == 'off':
                        set_led(False, 'telegram')
                        db_insert_control_log('kipas', 'OFF', 'telegram')
                        send_telegram("🌀 <b>Kipas dimatikan!</b> ✅\n"
                            f"Waktu: {datetime.now().strftime('%H:%M:%S')}", chat_id)

                    elif cmd['device'] == 'lampu' and cmd['action'] == 'on':
                        set_lampu(True, 'telegram')
                        db_insert_control_log('lampu', 'ON', 'telegram')
                        send_telegram("💡 <b>Lampu dinyalakan!</b> ✅\n"
                            f"Waktu: {datetime.now().strftime('%H:%M:%S')}", chat_id)

                    elif cmd['device'] == 'lampu' and cmd['action'] == 'off':
                        set_lampu(False, 'telegram')
                        db_insert_control_log('lampu', 'OFF', 'telegram')
                        send_telegram("💡 <b>Lampu dimatikan!</b> ✅\n"
                            f"Waktu: {datetime.now().strftime('%H:%M:%S')}", chat_id)

                    else:
                        # Bukan perintah perangkat → jawab dengan AI
                        send_telegram("⏳ Menganalisis...", chat_id)
                        try:
                            context   = build_context()
                            ai_answer = ask_gemini(text, context)
                            send_telegram(ai_answer, chat_id)
                        except Exception as e:
                            print(f"❌ AI error: {e}")
                            send_telegram(f"❌ AI error: {str(e)}", chat_id)

        except Exception as e:
            print(f"❌ Telegram listener error: {e}")

        time.sleep(1)

# ============================================================
# MAIN
# ============================================================
def weather_prefetch_loop():
    """Fetch cuaca setiap 10 menit di background."""
    time.sleep(3)  # delay singkat saat startup
    while True:
        try:
            fetch_weather_data()
        except Exception as e:
            print(f"⚠️  Weather prefetch: {e}")
        time.sleep(600)

def sholat_prefetch_loop():
    """Fetch jadwal sholat tiap jam di background."""
    time.sleep(5)
    while True:
        try:
            fetch_sholat_data()
        except Exception as e:
            print(f"⚠️  Sholat prefetch: {e}")
        time.sleep(3600)

if __name__ == '__main__':
    # Telegram bot listener
    t = threading.Thread(target=telegram_bot_listener, daemon=True)
    t.start()

    # Background fetch cuaca (Open-Meteo)
    tw = threading.Thread(target=weather_prefetch_loop, daemon=True)
    tw.start()

    # Background fetch jadwal sholat (MyQuran)
    ts = threading.Thread(target=sholat_prefetch_loop, daemon=True)
    ts.start()

    print("\n" + "=" * 60)
    print("✅ Server berjalan! — Ciseeng, Kab. Bogor")
    print("=" * 60)
    print(f"🌐 Dashboard : http://{SERVER_IP}:{SERVER_PORT}/")
    print(f"📡 Sensor    : POST /api/sensor")
    print(f"🔄 ESP32 Poll: GET  /api/state")
    print(f"🎛️  Kontrol   : POST /api/control")
    print(f"💬 Chat AI   : POST /api/chat")
    print(f"📊 Latest    : GET  /api/latest")
    print(f"📈 Stats     : GET  /api/statistics")
    print(f"🔔 Alerts    : GET  /api/alerts")
    print(f"🌤️  Cuaca     : GET  /api/weather")
    print(f"🕌 Sholat    : GET  /api/sholat")
    print("=" * 60)
    print("Ctrl+C untuk stop\n")

    app.run(host='0.0.0.0', port=SERVER_PORT, debug=False, use_reloader=False)
