// UDYAM-TWIN Wokwi virtual machine: settings. Nothing here is secret.
// Credentials, if your broker needs them, go in secrets.h (see secrets.example.h); never commit that file.
#pragma once

// Identity: shown on the dashboard. This is a virtual device in the Wokwi simulator, not a real machine.
#define MACHINE_ID "machine-001"
#define DATA_SOURCE "WOKWI_SIMULATOR"
#define SCHEMA "udyam.wokwi.v1"

// Wi-Fi: Wokwi's simulated open access point.
#define WIFI_SSID "Wokwi-GUEST"
#define WIFI_PASSWORD ""
#define WIFI_CHANNEL 6

// MQTT. Free Wokwi reaches the internet only, so the broker must be public or cloud-hosted.
// Default: shiftr.io's public broker. Its login "public"/"public" is a published shared login, not a secret.
// Anyone can read or publish on a public broker (shiftr.io even shows traffic on its website): change TOPIC_PREFIX to
// something unique to you, and set the same prefix on the backend (UDYAM_WOKWI_TOPIC_PREFIX).
// Alternative: "broker.hivemq.com" with an empty login (in our tests on 26 Oct 2026 it often refused or ignored clients).
#define MQTT_HOST "public.cloud.shiftr.io"
#define MQTT_PORT 1883
// A private broker's login goes in secrets.h instead (it takes precedence; see secrets.example.h).
#ifndef MQTT_USER
#define MQTT_USER "public"
#define MQTT_PASS "public"
#endif
#define TOPIC_PREFIX "udyam-twin-7e4f53cb/virtual-machines"  // unique to this project; must match UDYAM_WOKWI_TOPIC_PREFIX in .env
// Telemetry: <TOPIC_PREFIX>/<MACHINE_ID>/telemetry; status (retained online/offline, last will): .../status

// How often to publish (ms). Can also be changed at run time from the serial monitor: "interval 5000".
#define PUBLISH_INTERVAL_MS 2000
