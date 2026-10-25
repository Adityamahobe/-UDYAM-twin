// UDYAM-TWIN virtual machine for the Wokwi simulator (ESP32).
//
// Publishes timestamped JSON telemetry over MQTT for the UDYAM-TWIN dashboard's Real Data mode.
// Everything here is VIRTUAL: Wokwi's simulated sensors and software models. It does not measure any real machine.
//
// Where each value comes from (also sent in every message, under "value_sources"):
//   temp_c             Wokwi DHT22 virtual sensor (drag its temperature in the simulator); +15 °C in the OVERHEAT scenario (software)
//   rpm                Wokwi potentiometer "SPEED" as a speed setpoint 0–3000 rpm, plus software noise (no encoder model exists)
//   vibration_rms_mm_s software model: baseline + speed term + the RMS deviation of the Wokwi MPU6050 virtual accelerometer
//                      from 1 g (tilt or set its acceleration in the simulator) + a bearing-wear term in BEARING_WEAR
//   current_a          software model from speed and the "LOAD" potentiometer (Wokwi has no current-sensor model)
//
// Scenarios (press the button, or type in the serial monitor): normal, high_load, bearing_wear, overheat, sensor_dropout.
// Serial commands: "scenario <name>", "interval <ms>", "status".

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <DHTesp.h>
#include <Adafruit_MPU6050.h>
#include <Wire.h>
#include <time.h>

#if __has_include("secrets.h")
#include "secrets.h"  // a private broker's login (git-ignored); overrides the public default in config.h
#endif
#include "config.h"

// Pins (see diagram.json)
const int PIN_DHT = 15;
const int PIN_SPEED_POT = 34;
const int PIN_LOAD_POT = 35;
const int PIN_BUTTON = 4;
const int PIN_FAULT_LED = 26;

const char* SCENARIOS[] = {"normal", "high_load", "bearing_wear", "overheat", "sensor_dropout"};
const int N_SCENARIOS = 5;
// The fault each scenario simulates (null for normal / high load). A label of the simulated condition, not a verified fault.
const char* SCENARIO_FAULT[] = {nullptr, nullptr, "bearing_wear", "overheating", "temperature_sensor_dropout"};

WiFiClient net;
PubSubClient mqtt(net);
DHTesp dht;
Adafruit_MPU6050 mpu;
bool mpuOk = false;

int scenario = 0;
unsigned long intervalMs = PUBLISH_INTERVAL_MS;
unsigned long lastPublish = 0, lastSample = 0, lastReconnect = 0, lastButton = 0;
unsigned long seq = 0;
double accDevSq = 0;  // sum of (|a| - g)^2 since the last publish
int accN = 0;
String topicTelemetry, topicStatus, clientId;

float noise(float amp) { return amp * ((float)random(-1000, 1001) / 1000.0f); }

void connectWifi() {
  Serial.print("Wi-Fi: connecting to " WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD, WIFI_CHANNEL);
  while (WiFi.status() != WL_CONNECTED) { delay(250); Serial.print("."); }
  Serial.print(" connected, IP "); Serial.println(WiFi.localIP());
  configTime(0, 0, "pool.ntp.org", "time.google.com");  // UTC timestamps for the messages
}

bool connectMqtt() {
  Serial.printf("MQTT: connecting to %s:%d as %s ... ", MQTT_HOST, MQTT_PORT, clientId.c_str());
  const char* user = strlen(MQTT_USER) ? MQTT_USER : nullptr;
  const char* pass = strlen(MQTT_PASS) ? MQTT_PASS : nullptr;
  // Last will: the broker publishes "offline" (retained) if this device disappears without saying goodbye.
  if (mqtt.connect(clientId.c_str(), user, pass, topicStatus.c_str(), 1, true, "offline")) {
    Serial.println("connected");
    mqtt.publish(topicStatus.c_str(), "online", true);
    return true;
  }
  Serial.printf("failed, state %d (retrying)\n", mqtt.state());
  return false;
}

bool utcNow(char* out, size_t n) {
  time_t now = time(nullptr);
  if (now < 1700000000) return false;  // NTP not synchronised yet
  struct tm t;
  gmtime_r(&now, &t);
  strftime(out, n, "%Y-%m-%dT%H:%M:%SZ", &t);
  return true;
}

void sampleAccelerometer() {
  if (!mpuOk) return;
  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);
  double mag = sqrt(a.acceleration.x * a.acceleration.x + a.acceleration.y * a.acceleration.y + a.acceleration.z * a.acceleration.z);
  double dev = mag - 9.80665;
  accDevSq += dev * dev;
  accN++;
}

void publishTelemetry() {
  const char* sc = SCENARIOS[scenario];
  // Speed setpoint from the SPEED potentiometer; high load slows the spindle slightly.
  float rpmSet = analogRead(PIN_SPEED_POT) / 4095.0f * 3000.0f;
  float rpm = rpmSet * (scenario == 1 ? 0.96f : 1.0f);
  rpm = rpm > 5 ? rpm + noise(6) : 0;
  float load = analogRead(PIN_LOAD_POT) / 4095.0f * 1.2f;  // 0–120 % of nominal load
  if (scenario == 1) load *= 1.35f;
  float speed = rpm / 1500.0f;

  // Current: software model (idle current + speed × load term), amps.
  float current = rpm > 0 ? 1.2f + 6.5f * speed * load + noise(0.08f) : 0.4f + noise(0.02f);

  // Vibration: software model on top of the virtual accelerometer.
  float accRms = accN ? sqrt(accDevSq / accN) : 0;  // m/s²
  accDevSq = 0; accN = 0;
  float vib = rpm > 0 ? 0.8f + 0.25f * speed * speed + 0.5f * accRms : 0.2f;
  if (scenario == 2) vib += 4.0f * speed;  // bearing wear grows with speed
  vib = constrain(vib + noise(0.05f), 0.0f, 50.0f);

  // Temperature: the DHT22 virtual sensor; +15 °C (software) when overheating; no value on sensor dropout.
  TempAndHumidity th = dht.getTempAndHumidity();
  bool tempOk = dht.getStatus() == 0 && scenario != 4;
  float temp = th.temperature + (scenario == 3 ? 15.0f : 0.0f);

  JsonDocument doc;
  doc["schema"] = SCHEMA;
  doc["source"] = DATA_SOURCE;
  doc["virtual"] = true;
  doc["machine_id"] = MACHINE_ID;
  doc["seq"] = ++seq;
  char ts[24];
  if (utcNow(ts, sizeof ts)) doc["ts"] = ts; else doc["ts"] = nullptr;
  doc["uptime_ms"] = millis();
  doc["interval_ms"] = intervalMs;
  doc["rpm"] = round(rpm * 10) / 10.0;
  if (tempOk) doc["temp_c"] = round(temp * 100) / 100.0; else doc["temp_c"] = nullptr;
  doc["vibration_rms_mm_s"] = round(vib * 100) / 100.0;
  doc["current_a"] = round(current * 100) / 100.0;
  doc["scenario"] = sc;
  if (SCENARIO_FAULT[scenario]) doc["simulated_fault"] = SCENARIO_FAULT[scenario]; else doc["simulated_fault"] = nullptr;
  JsonObject src = doc["value_sources"].to<JsonObject>();
  src["temp_c"] = scenario == 3 ? "wokwi DHT22 virtual sensor + software offset" : "wokwi DHT22 virtual sensor";
  src["rpm"] = "wokwi potentiometer setpoint + software";
  src["vibration_rms_mm_s"] = mpuOk ? "software model + wokwi MPU6050 virtual accelerometer" : "software model";
  src["current_a"] = "software model";

  char buf[640];
  size_t n = serializeJson(doc, buf, sizeof buf);
  bool ok = mqtt.publish(topicTelemetry.c_str(), (const uint8_t*)buf, n, false);
  char tempText[12];
  if (tempOk) snprintf(tempText, sizeof tempText, "%.1f", temp); else strcpy(tempText, "null");
  Serial.printf("%s #%lu %s rpm=%.0f temp=%s vib=%.2f I=%.2f\n", ok ? "sent" : "SEND FAILED", seq, sc, rpm,
                tempText, vib, current);
}

void setScenario(int i) {
  scenario = (i % N_SCENARIOS + N_SCENARIOS) % N_SCENARIOS;
  digitalWrite(PIN_FAULT_LED, scenario == 0 ? LOW : HIGH);
  Serial.printf("scenario: %s\n", SCENARIOS[scenario]);
}

void handleSerial() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.startsWith("scenario ")) {
    String name = line.substring(9);
    for (int i = 0; i < N_SCENARIOS; i++) if (name == SCENARIOS[i]) { setScenario(i); return; }
    Serial.println("unknown scenario: normal | high_load | bearing_wear | overheat | sensor_dropout");
  } else if (line.startsWith("interval ")) {
    long ms = line.substring(9).toInt();
    if (ms >= 500 && ms <= 60000) { intervalMs = ms; Serial.printf("interval: %ld ms\n", ms); }
    else Serial.println("interval must be 500–60000 ms");
  } else if (line == "status") {
    Serial.printf("wifi=%d mqtt=%d scenario=%s interval=%lu seq=%lu topic=%s\n", WiFi.status() == WL_CONNECTED,
                  mqtt.connected(), SCENARIOS[scenario], intervalMs, seq, topicTelemetry.c_str());
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_FAULT_LED, OUTPUT);
  randomSeed(esp_random());
  dht.setup(PIN_DHT, DHTesp::DHT22);
  Wire.begin(21, 22);
  mpuOk = mpu.begin();
  Serial.println(mpuOk ? "MPU6050 virtual accelerometer ready" : "MPU6050 not found: vibration uses the software model only");

  topicTelemetry = String(TOPIC_PREFIX) + "/" + MACHINE_ID + "/telemetry";
  topicStatus = String(TOPIC_PREFIX) + "/" + MACHINE_ID + "/status";
  clientId = String("udyam-wokwi-") + MACHINE_ID + "-" + String((uint32_t)esp_random(), HEX);
  Serial.println("UDYAM-TWIN virtual machine (Wokwi simulator): " MACHINE_ID);
  Serial.println("telemetry topic: " + topicTelemetry);

  connectWifi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(768);
  connectMqtt();
  setScenario(0);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();
  if (!mqtt.connected() && millis() - lastReconnect > 3000) { lastReconnect = millis(); connectMqtt(); }
  mqtt.loop();
  handleSerial();

  if (digitalRead(PIN_BUTTON) == LOW && millis() - lastButton > 400) { lastButton = millis(); setScenario(scenario + 1); }
  if (millis() - lastSample >= 20) { lastSample = millis(); sampleAccelerometer(); }
  if (mqtt.connected() && millis() - lastPublish >= intervalMs) { lastPublish = millis(); publishTelemetry(); }
}
