#include <Arduino.h>
#include <HTTPClient.h>
#include <SD.h>
#include <SPI.h>
#include <WiFi.h>
#include <Wire.h>
#include <Adafruit_BNO08x.h>
#include "detect.h"

// Road condition device: ESP32 + BNO085 + GT-U7 GPS + microSD.
// Measures the road on board with detect.h (the same math as pipeline.py), writes every severe hit
// and 50 m roughness window to a queue on the SD card, and uploads the queue over WiFi when it can.
// Raw IMU and GPS stay on the card, in the logger's CSV format, so a drive drops into data/raw/drives.
// BOOT button (or "replay" on serial): replay a logged drive from the card through the same detector.
//
// SD card: /config.txt     ssid=, pass=, url=http://<laptop>:8000/live, replay=/replay/<drive>, speed=8
//          /queue.jsonl    events, one JSON line each; /queue.pos is how much of it the server has
//          /drives/        raw IMU + GPS per drive
// Serial: key=value sets and saves a config line; replay, stop, status.

const int PIN_SDA = 21, PIN_SCL = 22;
const int PIN_GPS_RX = 16, PIN_GPS_TX = 17;
const int PIN_SCK = 18, PIN_MISO = 19, PIN_MOSI = 23, PIN_CS = 5;
const int LED = 2, BUTTON = 0;
const uint32_t PERIOD_US = 10000;   // 100 Hz
const float G = 9.80665f;
const long TZ_OFFSET_S = -4 * 3600;  // ponytail: EDT, fixed; drive names are local time until Nov 1

// One sample, as the logger sent it over BLE, plus the tick time
struct __attribute__((packed)) Sample {
  uint32_t ms;
  uint16_t seq;
  int16_t qw, qi, qj, qk;  // game rotation vector, x 16384
  int16_t ax, ay, az;      // body-frame accel, gravity included, hundredths of m/s^2
  int16_t gx, gy, gz;      // calibrated gyro, mrad/s
  int16_t fwd, lat, vert;  // road frame, gravity removed, hundredths of m/s^2
  uint8_t accAccuracy;
};

Adafruit_BNO08x bno(-1);
QueueHandle_t samples;
volatile uint32_t dropped = 0;
detect::Detector det;

struct Config { String ssid, pass, url, replay = "/replay/drive"; float speed = 8; } cfg;

// ---------- sampler: core 1, the only code that touches I2C (from project 16) ----------

void toRoad(const float q[4], const float a[3], float out[3]) {
  float r = q[0], i = q[1], j = q[2], k = q[3];
  float R[3][3] = {
      {1 - 2 * (j * j + k * k), 2 * (i * j - r * k), 2 * (i * k + r * j)},
      {2 * (i * j + r * k), 1 - 2 * (i * i + k * k), 2 * (j * k - r * i)},
      {2 * (i * k - r * j), 2 * (j * k + r * i), 1 - 2 * (i * i + j * j)},
  };
  float w[3];
  for (int row = 0; row < 3; row++) w[row] = R[row][0] * a[0] + R[row][1] * a[1] + R[row][2] * a[2];
  float fx = R[0][0], fy = R[1][0], n = sqrtf(fx * fx + fy * fy);
  if (n < 1e-3f) { fx = 1; fy = 0; } else { fx /= n; fy /= n; }
  out[0] = w[0] * fx + w[1] * fy;
  out[1] = -w[0] * fy + w[1] * fx;
  out[2] = w[2] - G;
}

void enableReports() {
  bno.enableReport(SH2_GAME_ROTATION_VECTOR, PERIOD_US);  // no magnetometer: a steel car body throws off heading
  bno.enableReport(SH2_ACCELEROMETER, PERIOD_US);
  bno.enableReport(SH2_GYROSCOPE_CALIBRATED, PERIOD_US);
}

int16_t clamp16(float v) { return (int16_t)constrain(lroundf(v), -32768L, 32767L); }

void sampler(void *) {
  float q[4] = {1, 0, 0, 0}, a[3] = {0, 0, G}, g[3] = {0, 0, 0};
  uint8_t accAccuracy = 0;
  uint16_t seq = 0;
  TickType_t lastWake = xTaskGetTickCount();
  for (;;) {
    vTaskDelayUntil(&lastWake, pdMS_TO_TICKS(PERIOD_US / 1000));
    if (bno.wasReset()) enableReports();
    sh2_SensorValue_t e;
    while (bno.getSensorEvent(&e)) {
      if (e.sensorId == SH2_GAME_ROTATION_VECTOR) {
        q[0] = e.un.gameRotationVector.real; q[1] = e.un.gameRotationVector.i;
        q[2] = e.un.gameRotationVector.j; q[3] = e.un.gameRotationVector.k;
      } else if (e.sensorId == SH2_ACCELEROMETER) {
        a[0] = e.un.accelerometer.x; a[1] = e.un.accelerometer.y; a[2] = e.un.accelerometer.z;
        accAccuracy = e.status & 0x03;
      } else if (e.sensorId == SH2_GYROSCOPE_CALIBRATED) {
        g[0] = e.un.gyroscope.x; g[1] = e.un.gyroscope.y; g[2] = e.un.gyroscope.z;
      }
    }
    float road[3];
    toRoad(q, a, road);
    Sample s = {millis(), seq++,
                clamp16(q[0] * 16384), clamp16(q[1] * 16384), clamp16(q[2] * 16384), clamp16(q[3] * 16384),
                clamp16(a[0] * 100), clamp16(a[1] * 100), clamp16(a[2] * 100),
                clamp16(g[0] * 1000), clamp16(g[1] * 1000), clamp16(g[2] * 1000),
                clamp16(road[0] * 100), clamp16(road[1] * 100), clamp16(road[2] * 100), accAccuracy};
    if (xQueueSend(samples, &s, 0) != pdTRUE) dropped++;  // full: drop, never block the sampler
  }
}

// ---------- SD output: buffered, flushed every second so a power cut loses at most that ----------

struct Out {
  File f;
  char buf[4096];
  size_t n = 0;
  void drain() { if (n && f) f.write((uint8_t *)buf, n); n = 0; }
  void flush() { drain(); if (f) f.flush(); }
  void printf(const char *fmt, ...) {
    char line[1024];
    va_list ap;
    va_start(ap, fmt);
    int len = vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    len = min(len, (int)sizeof line - 1);
    if (n + len > sizeof buf) drain();
    memcpy(buf + n, line, len);
    n += len;
  }
};
Out imuLog, gpsLog, queue;

void tick() {
  static uint32_t last = 0;
  if (millis() - last < 1000) return;
  last = millis();
  imuLog.flush(); gpsLog.flush(); queue.flush();
}

// ---------- events: what leaves the device ----------

char driveId[40] = "", liveId[40] = "";
uint32_t runId = 0, events = 0, hitCount = 0;

void beginRun(const char *id) {
  strlcpy(driveId, id, sizeof driveId);
  runId = esp_random();  // a replayed drive keeps its name, but every run is new to the server
  events = hitCount = 0;
  det.reset();
}

void emitHit(const detect::Hit &h) {
  hitCount++;
  queue.printf("{\"run\":%lu,\"drive\":\"%s\",\"n\":%lu,\"k\":\"hit\",\"t\":%.2f,\"lat\":%.6f,\"lon\":%.6f,"
               "\"heading\":%.1f,\"severity\":%.1f,\"vert\":%.1f,\"lat_g\":%.1f,\"speed\":%.1f}\n",
               runId, driveId, events++, h.t, h.lat, h.lon, h.heading, h.severity, h.vert, h.lat_g, h.speed);
}

void emitWindow(const detect::Window &w) {
  char path[detect::PATH * 26 + 32];
  int p = 0;
  for (int j = 0; j < w.npath; j++)
    p += snprintf(path + p, sizeof path - p, "%s[%.6f,%.6f]", j ? "," : "", w.path[j][0], w.path[j][1]);
  queue.printf("{\"run\":%lu,\"drive\":\"%s\",\"n\":%lu,\"k\":\"win\",\"t\":%.2f,\"rough\":%.3f,\"rough_lat\":%.3f,"
               "\"rn\":%.3f,\"speed\":%.1f,\"path\":[%s]}\n",
               runId, driveId, events++, w.t, w.rough, w.rough_lat, w.rn, w.speed, path);
}

// ---------- GPS: GT-U7 on UART2, RMC + GGA at 5 Hz (from the 00/gt-u7 smoke test) ----------

void sendUbx(uint8_t cls, uint8_t id, const uint8_t *payload, uint16_t len) {
  uint8_t hdr[6] = {0xB5, 0x62, cls, id, (uint8_t)len, (uint8_t)(len >> 8)};
  uint8_t a = 0, b = 0;
  for (int i = 2; i < 6; i++) { a += hdr[i]; b += a; }
  for (int i = 0; i < len; i++) { a += payload[i]; b += a; }
  Serial2.write(hdr, 6);
  if (len) Serial2.write(payload, len);
  Serial2.write(a);
  Serial2.write(b);
}

// Sentences off before the rate goes up: the full set at 5 Hz saturates 9600 baud. Not saved on the module.
void gpsFast() {
  for (uint8_t id : {0x01, 0x02, 0x03, 0x05}) {  // GLL, GSA, GSV, VTG
    uint8_t m[3] = {0xF0, id, 0};
    sendUbx(0x06, 0x01, m, 3);
  }
  uint8_t r[6] = {200, 0, 1, 0, 1, 0};  // 200 ms, navRate 1, GPS time
  sendUbx(0x06, 0x08, r, 6);
}

double nmeaDeg(const char *v, const char *hemi) {
  if (!*v) return NAN;
  double x = atof(v);
  int d = (int)(x / 100);
  double deg = d + (x - d * 100) / 60;
  return (*hemi == 'S' || *hemi == 'W') ? -deg : deg;
}

bool fixOk = false, replaying = false;
int sats = 0;
float altM = NAN;
uint32_t logStart = 0;
double lastLat, lastLon;

void startLiveDrive(const char *date, const char *utc) {
  struct tm tm = {};
  tm.tm_mday = (date[0] - '0') * 10 + date[1] - '0';
  tm.tm_mon = (date[2] - '0') * 10 + date[3] - '0' - 1;
  tm.tm_year = 100 + (date[4] - '0') * 10 + date[5] - '0';
  tm.tm_hour = (utc[0] - '0') * 10 + utc[1] - '0';
  tm.tm_min = (utc[2] - '0') * 10 + utc[3] - '0';
  tm.tm_sec = (utc[4] - '0') * 10 + utc[5] - '0';
  time_t local = mktime(&tm) + TZ_OFFSET_S;  // no TZ set: mktime is UTC
  gmtime_r(&local, &tm);
  strftime(liveId, sizeof liveId, "drive_%Y%m%d_%H%M%S", &tm);
  String base = String("/drives/") + liveId;
  imuLog.f = SD.open(base + "_imu.csv", FILE_WRITE);
  gpsLog.f = SD.open(base + "_gps.csv", FILE_WRITE);
  File m = SD.open(base + "_markers.csv", FILE_WRITE);  // fleet units have no marker button; pipeline.py wants the file
  m.print("t_ms,count\n");
  m.close();
  imuLog.printf("t_ms,seq,qw,qi,qj,qk,ax,ay,az,gx,gy,gz,fwd,lat,vert,acc_accuracy\n");
  gpsLog.printf("t_ms,fix_t_ms,utc_ms,lat,lon,speed_mps,bearing_deg,accuracy_m,speed_accuracy_mps,alt_m\n");
  logStart = millis();
  beginRun(liveId);
  Serial.printf("first fix: logging %s\n", base.c_str());
}

int splitFields(char *s, char *f[], int maxF) {  // keeps empty fields, unlike strtok
  int n = 0;
  f[n++] = s;
  for (char *p = s; *p && n < maxF; p++)
    if (*p == ',') { *p = 0; f[n++] = p + 1; }
  return n;
}

void handleNmea(char *line) {
  char *star = strchr(line, '*');
  if (!star || strlen(star) < 3) return;
  uint8_t x = 0;
  for (char *p = line + 1; p < star; p++) x ^= *p;
  if (x != strtoul(star + 1, nullptr, 16)) return;
  *star = 0;
  if (strlen(line) < 6) return;
  const char *type = line + 3;  // skip "$GP" / "$GN"
  char *f[20];
  int n = splitFields(line, f, 20);
  if (!strncmp(type, "GGA", 3) && n >= 10) {
    sats = atoi(f[7]);
    altM = *f[9] ? atof(f[9]) : NAN;
  } else if (!strncmp(type, "RMC", 3) && n >= 10) {
    bool ok = f[2][0] == 'A' && strlen(f[1]) >= 6 && strlen(f[9]) == 6;
    if (!ok) {
      // lost the fix: stopped, as far as detection knows; never hold the last speed while parked
      if (fixOk && *liveId) det.fix(millis() - logStart, lastLat, lastLon, 0);
      fixOk = false;
      return;
    }
    if (!*liveId) startLiveDrive(f[9], f[1]);
    fixOk = true;
    int32_t t = millis() - logStart;
    lastLat = nmeaDeg(f[3], f[4]);
    lastLon = nmeaDeg(f[5], f[6]);
    float mps = atof(f[7]) * 0.514444f, course = *f[8] ? atof(f[8]) : NAN;
    det.fix(t, lastLat, lastLon, mps);
    gpsLog.printf("%ld,%ld,,%.8f,%.8f,%.2f,%.1f,,,%.1f\n", t, t, lastLat, lastLon, mps, course, altM);
  }
}

uint32_t gpsBytes = 0;

void readGps() {
  static char line[128];
  static size_t len = 0;
  while (Serial2.available()) {
    char c = Serial2.read();
    gpsBytes++;
    if (c == '$') len = 0;  // a new sentence always restarts
    if (c == '\r' || c == '\n') {
      line[len] = 0;
      if (len) handleNmea(line);
      len = 0;
    } else if (len < sizeof line - 1) {
      line[len++] = c;
    }
  }
}

// ---------- live: the sampler's samples, after the first fix ----------

uint32_t received = 0;

void live(const Sample &s) {
  if (!*liveId || s.ms < logStart) return;
  int32_t t = s.ms - logStart;
  imuLog.printf("%ld,%u,%.4f,%.4f,%.4f,%.4f,%.2f,%.2f,%.2f,%.3f,%.3f,%.3f,%.2f,%.2f,%.2f,%u\n", t, s.seq,
                s.qw / 16384.0f, s.qi / 16384.0f, s.qj / 16384.0f, s.qk / 16384.0f,
                s.ax / 100.0f, s.ay / 100.0f, s.az / 100.0f, s.gx / 1000.0f, s.gy / 1000.0f, s.gz / 1000.0f,
                s.fwd / 100.0f, s.lat / 100.0f, s.vert / 100.0f, s.accAccuracy);
  det.sample(t, s.seq, s.lat / 100.0f, s.vert / 100.0f);
}

// ---------- replay: a logged drive from the card, through the same detector ----------

struct Lines {  // a file, line by line, read in blocks: byte-at-a-time SD reads are slow
  File f;
  char buf[2048], line[512];
  size_t n = 0, pos = 0;
  bool open(const String &path) { f = SD.open(path); n = pos = 0; return f; }
  bool next() {
    size_t len = 0;
    for (;;) {
      if (pos == n) {
        n = f.read((uint8_t *)buf, sizeof buf);
        pos = 0;
        if (!n) { line[len] = 0; return len > 0; }
      }
      char c = buf[pos++];
      if (c == '\n') { line[len] = 0; return true; }
      if (len < sizeof line - 1) line[len++] = c;
    }
  }
};

volatile bool replayAsked = false, stopAsked = false;

void replay() {
  static Lines imu, gps;
  String stem = cfg.replay;
  stopAsked = false;
  if (!imu.open(stem + "_imu.csv") || !gps.open(stem + "_gps.csv") || !imu.next() || !gps.next()) {
    Serial.printf("replay: can't read %s_imu.csv and %s_gps.csv\n", stem.c_str(), stem.c_str());
    imu.f.close(); gps.f.close();
    return;
  }
  using detect::column;
  using detect::field;
  int ct = column(imu.line, "t_ms"), cs = column(imu.line, "seq"), cl = column(imu.line, "lat"), cv = column(imu.line, "vert");
  int gt = column(gps.line, "t_ms"), gla = column(gps.line, "lat"), glo = column(gps.line, "lon"), gs = column(gps.line, "speed_mps");
  bool more = gps.next();
  beginRun(stem.substring(stem.lastIndexOf('/') + 1).c_str());
  replaying = true;
  Serial.printf("replay %s at %gx, run %lu\n", driveId, cfg.speed, runId);
  uint32_t wall0 = millis(), n = 0;
  double t0 = NAN;
  while (!stopAsked && imu.next()) {
    double t = field(imu.line, ct);
    for (; more && field(gps.line, gt) <= t; more = gps.next()) {  // fixes go in as their time comes, as live
      double la = field(gps.line, gla), lo = field(gps.line, glo);
      if (la == la && lo == lo) det.fix(int32_t(field(gps.line, gt)), la, lo, float(field(gps.line, gs)));
    }
    det.sample(int32_t(t), int32_t(field(imu.line, cs)), float(field(imu.line, cl)), float(field(imu.line, cv)));
    if (t0 != t0) t0 = t;
    if (++n % 100 == 0) {  // every second of drive: keep pace, keep the card flushed
      int32_t ahead = int32_t((t - t0) / cfg.speed) - int32_t(millis() - wall0);
      if (ahead > 0) vTaskDelay(pdMS_TO_TICKS(ahead));
      xQueueReset(samples);  // live samples are not wanted while replaying
      tick();
    }
  }
  det.finish();
  queue.flush();
  Serial.printf("replay %s: %lu samples in %.1f s, %lu hits%s\n", driveId, n, (millis() - wall0) / 1000.0f, hitCount,
                stopAsked ? " (stopped)" : "");
  imu.f.close(); gps.f.close();
  replaying = stopAsked = false;
  if (*liveId) beginRun(liveId);  // back to live: same drive, new run
  else *driveId = 0;
}

// ---------- worker: core 0, everything that touches the card ----------

void worker(void *) {
  det.onHit = emitHit;
  det.onWindow = emitWindow;
  for (;;) {
    if (replayAsked) { replayAsked = false; replay(); }
    Sample s;
    if (xQueueReceive(samples, &s, pdMS_TO_TICKS(10)) == pdTRUE) {
      do { received++; live(s); } while (xQueueReceive(samples, &s, 0) == pdTRUE);
    }
    readGps();
    tick();
  }
}

// ---------- upload: loop(), core 1 below the sampler ----------

uint32_t sent = 0, lastUploadMs = 0;
int lastCode = 0;

void savePos() {
  File f = SD.open("/queue.pos", FILE_WRITE);
  f.print(sent);
  f.close();
}

void upload() {
  static uint32_t last = 0;
  if (millis() - last < 1000 || WiFi.status() != WL_CONNECTED || !cfg.url.length()) return;
  last = millis();
  static char body[8192];
  File q = SD.open("/queue.jsonl");
  size_t size = q ? q.size() : 0, n = 0;
  if (size > sent) {
    q.seek(sent);
    n = q.read((uint8_t *)body, min(sizeof body, size - sent));
  }
  if (q) q.close();
  while (n && body[n - 1] != '\n') n--;  // whole lines only; the rest goes next time
  if (!n) return;
  HTTPClient http;
  http.begin(cfg.url);
  http.setTimeout(3000);
  http.addHeader("Content-Type", "application/x-ndjson");
  lastCode = http.POST((uint8_t *)body, n);
  http.end();
  if (lastCode != 200) return;
  sent += n;
  savePos();
  lastUploadMs = millis();
  if (size > sent) last = 0;  // more waiting: send it now
}

// ---------- config, serial, button ----------

bool setting(String line) {
  int eq = line.indexOf('=');
  if (eq < 1) return false;
  String k = line.substring(0, eq), v = line.substring(eq + 1);
  k.trim(); v.trim();
  if (k == "ssid") cfg.ssid = v;
  else if (k == "pass") cfg.pass = v;
  else if (k == "url") cfg.url = v;
  else if (k == "replay") cfg.replay = v;
  else if (k == "speed") cfg.speed = max(0.1f, v.toFloat());
  else return false;
  return true;
}

void saveConfig() {
  File f = SD.open("/config.txt", FILE_WRITE);
  f.printf("ssid=%s\npass=%s\nurl=%s\nreplay=%s\nspeed=%g\n", cfg.ssid.c_str(), cfg.pass.c_str(), cfg.url.c_str(),
           cfg.replay.c_str(), cfg.speed);
  f.close();
}

void startWifi() {
  if (!cfg.ssid.length()) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str());
}

void status() {
  static uint32_t lastReceived = 0, lastBytes = 0, lastMs = 0;
  uint32_t now = millis();
  float secs = (now - lastMs) / 1000.0f;  // the first time: since boot
  float rate = (received - lastReceived) / secs, gpsRate = (gpsBytes - lastBytes) / secs;
  lastReceived = received; lastBytes = gpsBytes; lastMs = now;
  File q = SD.open("/queue.jsonl");
  uint32_t unsent = q ? q.size() - min((uint32_t)q.size(), sent) : 0;
  if (q) q.close();
  Serial.printf("%s sats %d, GPS %.0f B/s%s | %s %s, %lu hits | IMU %.0f/s, dropped %lu | queue %lu B unsent | wifi %s",
                fixOk ? "FIX" : "no fix", sats, gpsRate, gpsRate || replaying ? "" : " (check GPS TX -> GPIO 16)", replaying ? "replay" : "drive", *driveId ? driveId : "(waits for a fix)",
                hitCount, rate, dropped, unsent, WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str() : "down");
  if (lastCode) Serial.printf(", last upload %d, %lu s ago", lastCode, (now - lastUploadMs) / 1000);
  Serial.println();
}

void serialCommand(String line) {
  line.trim();
  if (line == "replay") replayAsked = true;
  else if (line == "stop") stopAsked = true;
  else if (line == "status") status();
  else if (setting(line)) {
    saveConfig();
    if (line.startsWith("ssid") || line.startsWith("pass")) startWifi();
    Serial.printf("saved %s\n", line.c_str());
  } else Serial.println("? key=value (ssid pass url replay speed), replay, stop, status");
}

// BOOT reads near-threshold while held (projects 14/15): vote per 10 ms window, 3 agreeing windows.
bool buttonPressed() {
  static uint32_t windowStart = millis(), reads = 0, lowReads = 0;
  static bool stable = false;
  static uint8_t agree = 0;
  reads++;
  lowReads += digitalRead(BUTTON) == LOW;
  if (millis() - windowStart < 10) return false;
  bool vote = lowReads * 2 > reads;
  windowStart = millis();
  reads = lowReads = 0;
  agree = vote != stable ? agree + 1 : 0;
  if (agree < 3) return false;
  stable = vote;
  agree = 0;
  return stable;
}

void setup() {
  Serial.begin(115200);
  pinMode(LED, OUTPUT);
  pinMode(BUTTON, INPUT_PULLUP);
  delay(500);
  Serial.println("\nroad device");

  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CS);
  while (!SD.begin(PIN_CS, SPI, 20000000)) {  // the card is the source of truth: nothing runs without it
    Serial.println("SD card not mounted: check the card and CS 5, SCK 18, MISO 19, MOSI 23");
    digitalWrite(LED, !digitalRead(LED));
    delay(200);
  }
  File f = SD.open("/config.txt");
  while (f && f.available()) setting(f.readStringUntil('\n'));
  if (f) f.close();
  else saveConfig();  // first boot: write the defaults so there is a file to edit
  SD.mkdir("/drives");
  f = SD.open("/queue.pos");
  sent = f ? f.readString().toInt() : 0;
  if (f) f.close();
  f = SD.open("/queue.jsonl");
  if (!f || f.size() < sent) { sent = 0; savePos(); }  // queue gone or replaced: start over
  if (f) f.close();
  queue.f = SD.open("/queue.jsonl", FILE_APPEND);
  Serial.printf("config: ssid '%s', url '%s', replay '%s' at %gx; %lu bytes already uploaded\n", cfg.ssid.c_str(),
                cfg.url.c_str(), cfg.replay.c_str(), cfg.speed, sent);

  Serial2.setRxBufferSize(1024);
  Serial2.begin(9600, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
  delay(100);
  gpsFast();

  Wire.begin(PIN_SDA, PIN_SCL, 400000);
  while (!bno.begin_I2C(0x4B, &Wire)) {
    Serial.println("BNO085 not found at 0x4B, retrying");
    delay(1000);
  }
  enableReports();

  startWifi();
  samples = xQueueCreate(256, sizeof(Sample));  // 2.5 s: covers the worst SD write stall
  xTaskCreatePinnedToCore(sampler, "sampler", 4096, nullptr, 5, nullptr, 1);
  xTaskCreatePinnedToCore(worker, "worker", 8192, nullptr, 2, nullptr, 0);
}

void loop() {
  static String line;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { serialCommand(line); line = ""; }
    else line += c;
  }
  if (buttonPressed()) {
    if (replaying) stopAsked = true;
    else replayAsked = true;
  }
  static uint32_t lastStatus = 0;
  if (millis() - lastStatus > 5000) { lastStatus = millis(); status(); }
  upload();
  // solid: GPS fix; slow blink: no fix; fast blink: replaying
  digitalWrite(LED, replaying ? (millis() / 100) % 2 : fixOk || (millis() / 500) % 2);
  delay(1);
}
