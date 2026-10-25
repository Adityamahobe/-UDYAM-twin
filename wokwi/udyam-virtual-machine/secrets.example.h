// Copy to secrets.h only for a private broker with its own login (the default public broker uses public/public).
// secrets.h is git-ignored. In Wokwi, files in a public project are visible to anyone with the link:
// keep the project private, or use a broker account made only for this demo.
#pragma once
#define MQTT_USER "your-username"
#define MQTT_PASS "your-password"
