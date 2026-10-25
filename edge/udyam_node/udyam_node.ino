// UDYAM-TWIN LIVE sensor node (ESP32 DevKit)
//
// DS18B20 (housing temp) + MPU6050 (vibration) + ACS712 (current) → IoT hub over HTTP.
// Read-only: the node never receives commands.
//
// Libraries (Arduino Library Manager): OneWire, DallasTemperature, Adafruit MPU6050.
// Register the device on http://<hub>:8000/iot first, then paste its token below.

#include <WiFi.h>
#include <HTTPClient.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Adafruit_MPU6050.h>
#include <math.h>

// ---- configure ---------------------------------------------------------------
const char* WIFI_SSID   = "your-wifi";
const char* WIFI_PASS   = "your-pass";
const char* HUB_URL     = "http://192.168.1.10:8000/api/iot/telemetry";
const char* DEVICE_ID   = "ESP32-01";
const char* DEVICE_TOKEN = "paste-token-from-hub";

const int PIN_DS18B20 = 4;
const int PIN_ACS712  = 34;          // ADC1 only (ADC2 is unusable with Wi-Fi)
const float ACS_MV_PER_A = 185.0;    // 5 A module; 100 for 20 A, 66 for 30 A
const float ACS_DIVIDER = 2.0;       // ADC mV × this = sensor mV (2.0 for a 10k/10k divider on the 5 V output)
const float VIB_FREQ_HZ = 25.0;      // dominant shaft frequency used to turn accel RMS into velocity RMS
const unsigned long PERIOD_MS = 500; // matches the twin's 500 ms tick
// ------------------------------------------------------------------------------

OneWire oneWire(PIN_DS18B20);
DallasTemperature ds(&oneWire);
Adafruit_MPU6050 mpu;
bool mpuOk = false;

float readVibrationMmS() {
  // 200 samples at ~1 kHz; RMS of accel magnitude with the mean (gravity) removed.
  const int N = 200;
  static float buf[N];
  float mean = 0;
  for (int i = 0; i < N; i++) {
    sensors_event_t a, g, t;
    mpu.getEvent(&a, &g, &t);
    buf[i] = sqrtf(a.acceleration.x * a.acceleration.x + a.acceleration.y * a.acceleration.y + a.acceleration.z * a.acceleration.z);
    mean += buf[i];
    delayMicroseconds(1000);
  }
  mean /= N;
  float sq = 0;
  for (int i = 0; i < N; i++) sq += (buf[i] - mean) * (buf[i] - mean);
  float aRms = sqrtf(sq / N);                        // m/s²
  return aRms / (2.0f * PI * VIB_FREQ_HZ) * 1000.0f; // ≈ mm/s at the dominant frequency
}

float readCurrentA() {
  // RMS around the measured midpoint over ~100 ms (5 mains cycles at 50 Hz).
  const int N = 400;
  long sum = 0;
  static int raw[N];
  for (int i = 0; i < N; i++) { raw[i] = analogReadMilliVolts(PIN_ACS712); sum += raw[i]; delayMicroseconds(250); }
  float mid = (float)sum / N;
  float sq = 0;
  for (int i = 0; i < N; i++) { float d = (raw[i] - mid) * ACS_DIVIDER; sq += d * d; }
  return sqrtf(sq / N) / ACS_MV_PER_A;
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.printf("\nWi-Fi OK %s\n", WiFi.localIP().toString().c_str());
}

void setup() {
  Serial.begin(115200);
  ds.begin();
  ds.setWaitForConversion(false);
  ds.requestTemperatures();
  mpuOk = mpu.begin();
  if (mpuOk) mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
  analogSetPinAttenuation(PIN_ACS712, ADC_11db);
  connectWifi();
}

void loop() {
  unsigned long start = millis();
  if (WiFi.status() != WL_CONNECTED) connectWifi();

  float tempC = ds.getTempCByIndex(0);
  ds.requestTemperatures();                 // start next conversion; read it next loop
  bool tempOk = tempC != DEVICE_DISCONNECTED_C;

  char body[256];
  char vib[16] = "null", cur[16] = "null", tmp[16] = "null";
  if (tempOk) snprintf(tmp, sizeof tmp, "%.4f", tempC);
  if (mpuOk) snprintf(vib, sizeof vib, "%.3f", readVibrationMmS());
  snprintf(cur, sizeof cur, "%.3f", readCurrentA());
  snprintf(body, sizeof body,
           "{\"temp_c\":%s,\"vibration_rms_mm_s\":%s,\"current_a\":%s,\"rssi\":%d,\"fw\":\"0.1\"}",
           tmp, vib, cur, WiFi.RSSI());

  HTTPClient http;
  http.begin(HUB_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Id", DEVICE_ID);
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  int code = http.POST(body);
  if (code != 200) Serial.printf("hub %d: %s\n", code, http.getString().c_str());
  http.end();

  long wait = (long)PERIOD_MS - (long)(millis() - start);
  if (wait > 0) delay(wait);
}
