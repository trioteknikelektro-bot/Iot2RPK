/*
  ESP32 Smart Home — Ciseeng, Kab. Bogor
  AES-256-GCM Encrypted
  DHT11 (Suhu/Kelembaban) + MQ-2 (Asap)
  Kipas GPIO 2 (built-in) + Lampu GPIO 4 (eksternal)

  Library yang dibutuhkan (Library Manager Arduino IDE):
  1. DHT sensor library       — by Adafruit
  2. Adafruit Unified Sensor  — by Adafruit
  3. ArduinoJson              — by Benoit Blanchon
  (AES-GCM pakai mbedTLS yang sudah built-in di ESP32)

  Cara install:
  Arduino IDE → Sketch → Include Library → Manage Libraries
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <mbedtls/gcm.h>   // AES-GCM sudah built-in di ESP32 via mbedTLS
#include <mbedtls/entropy.h>
#include <mbedtls/ctr_drbg.h>

// ============================================================
// KONFIGURASI — EDIT BAGIAN INI
// ============================================================
const char* WIFI_SSID     = "stl";      // Ganti dengan WiFi kamu
const char* WIFI_PASSWORD = "12345678";   // Ganti dengan password WiFi
const char* SERVER_IP     = "192.168.92.30";         // IP laptop/PC server
const int   SERVER_PORT   = 5000;
// Lokasi: Ciseeng, Kab. Bogor (-6.3328, 106.8312)
// Cuaca dan jadwal sholat diproses di server.py

// ============================================================
// PIN
// ============================================================
#define LED_PIN    2      // GPIO 2 = Kipas (built-in LED, tidak perlu resistor)
#define LAMPU_PIN  4      // GPIO 4 = Lampu (LED eksternal + resistor 220Ω)
#define DHT_PIN    15     // GPIO 14 → DHT11
#define MQ2_PIN    13     // GPIO 34 → MQ-2 (analog input)
#define DHT_TYPE   DHT11

// ============================================================
// INTERVAL
// ============================================================
#define SENSOR_INTERVAL   10000   // kirim sensor tiap 10 detik
#define POLLING_INTERVAL   3000   // polling perintah LED tiap 3 detik

// ============================================================
// AES-256-GCM KEY — HARUS SAMA PERSIS DENGAN server.py
// ============================================================
const uint8_t AES_KEY[32] = {
  0x9d, 0xbc, 0x8c, 0x1c, 0x94, 0x32, 0xa8, 0x2a,
  0xf7, 0x84, 0xa9, 0x52, 0x59, 0x2a, 0x90, 0x8e,
  0x72, 0x89, 0x60, 0x18, 0xb7, 0xd1, 0xc6, 0xf6,
  0x1f, 0x8e, 0xef, 0x51, 0x8d, 0x42, 0x6a, 0xb0
};

// ============================================================
// BASE64 ENCODE (untuk kirim nonce & ciphertext via JSON)
// ============================================================
static const char b64chars[] =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

String base64Encode(const uint8_t* data, size_t len) {
  String out = "";
  int val = 0, bits = -6;
  for (size_t i = 0; i < len; i++) {
    val = (val << 8) + data[i];
    bits += 8;
    while (bits >= 0) {
      out += b64chars[(val >> bits) & 0x3F];
      bits -= 6;
    }
  }
  if (bits > -6) out += b64chars[((val << 8) >> (bits + 8)) & 0x3F];
  while (out.length() % 4) out += '=';
  return out;
}

// ============================================================
// AES-256-GCM ENCRYPT
// Input : plaintext (JSON string)
// Output: mengisi nonce_out (12 bytes) dan cipher_out
//         return panjang cipher (plaintext + 16 byte tag)
// ============================================================
int aesGcmEncrypt(
  const uint8_t* plaintext, size_t plainLen,
  uint8_t* nonce_out,        // harus buffer 12 byte
  uint8_t* cipher_out        // harus buffer plainLen + 16 byte
) {
  // Generate random nonce 12 byte
  esp_fill_random(nonce_out, 12);

  mbedtls_gcm_context gcm;
  mbedtls_gcm_init(&gcm);

  int ret = mbedtls_gcm_setkey(&gcm, MBEDTLS_CIPHER_ID_AES, AES_KEY, 256);
  if (ret != 0) {
    Serial.printf("❌ GCM setkey error: -0x%04X\n", -ret);
    mbedtls_gcm_free(&gcm);
    return -1;
  }

  uint8_t tag[16];

  ret = mbedtls_gcm_crypt_and_tag(
    &gcm,
    MBEDTLS_GCM_ENCRYPT,
    plainLen,
    nonce_out, 12,   // nonce
    nullptr, 0,      // additional data (tidak dipakai)
    plaintext,
    cipher_out,
    16, tag
  );

  if (ret != 0) {
    Serial.printf("❌ GCM encrypt error: -0x%04X\n", -ret);
    mbedtls_gcm_free(&gcm);
    return -1;
  }

  // Tempel tag di belakang ciphertext (sesuai konvensi Python cryptography)
  memcpy(cipher_out + plainLen, tag, 16);

  mbedtls_gcm_free(&gcm);
  return plainLen + 16;
}

// ============================================================
// OBJECTS & STATE
// ============================================================
DHT dht(DHT_PIN, DHT_TYPE);

bool          ledState       = false;   // kipas
bool          lampuState     = false;   // lampu
unsigned long lastSensorTime  = 0;
unsigned long lastPollingTime = 0;

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(115200);
  Serial.println("\n======================================");
  Serial.println("  ESP32 Smart Home — Ciseeng, Kab. Bogor");
  Serial.println("======================================");
  delay(200);

  Serial.println("========================================");
  Serial.println("🚀 ESP32 Smart Home — AES-256-GCM");
  Serial.println("========================================");

  // Setup pin
  pinMode(LED_PIN,   OUTPUT);
  pinMode(LAMPU_PIN, OUTPUT);
  digitalWrite(LED_PIN,   LOW);  // Kipas mati saat startup
  digitalWrite(LAMPU_PIN, LOW);  // Lampu mati saat startup

  // Init DHT
  dht.begin();

  // Konek WiFi
  Serial.print("📡 Konek ke WiFi: ");
  Serial.println(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempt = 0;
  while (WiFi.status() != WL_CONNECTED && attempt < 30) {
    delay(500);
    Serial.print(".");
    attempt++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n✅ WiFi Terhubung!");
    Serial.print("📍 IP ESP32: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\n❌ WiFi gagal! Restart...");
    ESP.restart();
  }

  // Test kedua LED saat startup (kedip 3x tanda siap)
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_PIN,   HIGH);
    digitalWrite(LAMPU_PIN, HIGH); delay(150);
    digitalWrite(LED_PIN,   LOW);
    digitalWrite(LAMPU_PIN, LOW);  delay(150);
  }

  Serial.println("========================================");
  Serial.println("✅ Siap! Enkripsi AES-256-GCM aktif.");
  Serial.println("========================================");
}

// ============================================================
// LOOP
// ============================================================
void loop() {
  // Reconnect WiFi jika putus
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("⚠️  WiFi putus, reconnecting...");
    WiFi.reconnect();
    delay(3000);
    return;
  }

  unsigned long now = millis();

  // 1. Kirim data sensor (terenkripsi)
  if (now - lastSensorTime >= SENSOR_INTERVAL) {
    lastSensorTime = now;
    sendSensorData();
  }

  // 2. Polling perintah LED dari server
  if (now - lastPollingTime >= POLLING_INTERVAL) {
    lastPollingTime = now;
    pollLedState();
  }

  delay(100);
}

// ============================================================
// KIRIM DATA SENSOR (TERENKRIPSI AES-256-GCM)
// ============================================================
void sendSensorData() {
  float temp  = dht.readTemperature();
  float humid = dht.readHumidity();
  int   smoke = analogRead(MQ2_PIN);

  if (isnan(temp) || isnan(humid)) {
    Serial.println("⚠️  DHT11 gagal baca, skip");
    return;
  }

  Serial.printf("📊 Sensor: %.1f°C | %.1f%% | Smoke:%d\n", temp, humid, smoke);

  // 1. Buat JSON plaintext
  StaticJsonDocument<256> doc;
  doc["device_id"]   = "ESP32_SMART_HOME";
  doc["temperature"] = round(temp * 10.0) / 10.0;
  doc["humidity"]    = round(humid * 10.0) / 10.0;
  doc["smoke"]       = smoke;

  String plaintext;
  serializeJson(doc, plaintext);

  Serial.println("🔑 Mengenkripsi data...");

  // 2. Enkripsi
  size_t  plainLen  = plaintext.length();
  uint8_t nonce[12];
  uint8_t* cipher  = new uint8_t[plainLen + 16];

  int cipherLen = aesGcmEncrypt(
    (const uint8_t*)plaintext.c_str(), plainLen,
    nonce, cipher
  );

  if (cipherLen < 0) {
    Serial.println("❌ Enkripsi gagal!");
    delete[] cipher;
    return;
  }

  // 3. Base64 encode nonce & ciphertext
  String nonceB64  = base64Encode(nonce,   12);
  String cipherB64 = base64Encode(cipher, cipherLen);
  delete[] cipher;

  // 4. Buat JSON terenkripsi
  StaticJsonDocument<512> encDoc;
  encDoc["encrypted_data"] = cipherB64;
  encDoc["nonce"]          = nonceB64;

  String encPayload;
  serializeJson(encDoc, encPayload);

  // 5. Kirim ke server
  String url = String("http://") + SERVER_IP + ":" + SERVER_PORT + "/api/sensor";

  HTTPClient http;
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(8000);

  int code = http.POST(encPayload);

  if (code == 200) {
    Serial.println("✅ Data terenkripsi berhasil dikirim!");
  } else if (code > 0) {
    Serial.printf("❌ Server error: HTTP %d\n", code);
    Serial.println(http.getString());
  } else {
    Serial.printf("❌ Koneksi gagal: %s\n", http.errorToString(code).c_str());
  }

  http.end();
}

// ============================================================
// POLLING STATE LED DARI SERVER (tidak dienkripsi, hanya GET)
// ============================================================
void pollLedState() {
  String url = String("http://") + SERVER_IP + ":" + SERVER_PORT + "/api/state";

  HTTPClient http;
  http.begin(url);
  http.setTimeout(3000);

  int code = http.GET();

  if (code == 200) {
    String payload = http.getString();

    StaticJsonDocument<256> doc;
    DeserializationError err = deserializeJson(doc, payload);

    if (!err) {
      bool serverLed   = doc["led"].as<bool>();
      bool serverLampu = doc["lampu"].as<bool>();
      const char* byWho      = doc["updated_by"]       | "unknown";
      const char* lampuByWho = doc["lampu_updated_by"] | "unknown";

      // Update kipas
      if (serverLed != ledState) {
        ledState = serverLed;
        digitalWrite(LED_PIN, ledState ? HIGH : LOW);
        Serial.printf("[KIPAS] %s — oleh: %s\n", ledState ? "ON" : "OFF", byWho);
      }

      // Update lampu
      if (serverLampu != lampuState) {
        lampuState = serverLampu;
        digitalWrite(LAMPU_PIN, lampuState ? HIGH : LOW);
        Serial.printf("[LAMPU] %s — oleh: %s\n", lampuState ? "ON" : "OFF", lampuByWho);
      }
    }
  }
  // Kalau gagal/timeout → diam saja, coba lagi nanti

  http.end();
}
