// Using avoidance sensor OR tracking sensor...
// SEN -> ARD:
// GND -> GND
//  +  -> 5V
// S/OUT -> Digital 2

const int PIN_AVOID = 2;
const int OBSTACLE_ACTIVE = LOW;   // change to HIGH if your sensor is inverted
const int STABLE_COUNT_REQUIRED = 3;
const unsigned long PERIOD_MS = 100;
const unsigned long HEARTBEAT_PERIOD_MS = 5000;   // Arduino sends every 5 seconds
const char* DEVICE_ID = "frank-arduino-01";
const char* RULE_NAME = "avoidance_hysteresis_v1";

unsigned long event_id = 0;
unsigned long last_sample_ms = 0;
unsigned long last_heartbeat_ms = 0;

bool confirmed_blocked = false;
int last_raw = -1;
int stable_count = 0;

void setup() {
  Serial.begin(115200);
  pinMode(PIN_AVOID, INPUT);
}

void loop() {
  unsigned long now = millis();
  if (now - last_sample_ms < PERIOD_MS) return;
  last_sample_ms = now;

  int raw = digitalRead(PIN_AVOID);
  bool blocked_now = (raw == OBSTACLE_ACTIVE);

  if (raw == last_raw) {
    stable_count++;
  } else {
    stable_count = 1;
    last_raw = raw;
  }

  if (stable_count >= STABLE_COUNT_REQUIRED && blocked_now != confirmed_blocked) {
    confirmed_blocked = blocked_now;
    event_id++;

    Serial.print("{\"device_id\":\"");
    Serial.print(DEVICE_ID);
    Serial.print("\",\"ts_ms\":");
    Serial.print(now);
    Serial.print(",\"event_id\":");
    Serial.print(event_id);
    Serial.print(",\"event_type\":\"");
    Serial.print(confirmed_blocked ? "obstacle_detected" : "obstacle_cleared");
    Serial.print("\",\"sensor\":\"avoidance\",\"value\":");
    Serial.print(confirmed_blocked ? 1 : 0);
    Serial.print(",\"rule\":\"");
    Serial.print(RULE_NAME);
    Serial.print("\"}");
    Serial.println();
  } else {
    // Send heartbeat if no state change, to indicate we're alive
    if (now - last_heartbeat_ms >= HEARTBEAT_PERIOD_MS) {
      Serial.print("{\"device_id\":\"");
      Serial.print(DEVICE_ID);
      Serial.print("\",\"ts_ms\":");
      Serial.print(now);
      Serial.print(",\"event_type\":\"heartbeat\"");
      Serial.print(",\"sensor\":\"avoidance\"");
      Serial.print(",\"status\":\"alive\"");
      Serial.print(",\"value\":");
      Serial.print(confirmed_blocked ? 1 : 0);
      Serial.print("}");
      Serial.println();
      last_heartbeat_ms = now;
    }
  }
}