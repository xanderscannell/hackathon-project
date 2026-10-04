// Severe hits and 50 m roughness windows, computed as samples arrive.
// A port of pipeline.py (find_hits and the windows in process()): same thresholds and the same
// centered windows as its pandas rollings, so the device and the backend find the same hits on
// the same drive. Plain C++, no Arduino, so device/check.py can run it on the laptop too.
#pragma once
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <algorithm>

namespace detect {

const float MIN_SPEED = 3.0f, REF_SPEED = 20, SPEED_EXP = 0.31f, HIT_MS2 = 4.5f, HIT_RATIO = 3;
const double WINDOW_M = 50;
const double DEG = 3.14159265358979323846 / 180;  // not PI: Arduino.h defines that
const double LAT0 = 42.2, LON0 = -83.3, KX = cos(LAT0 * DEG) * 111320, KY = 110540;  // pipeline.to_xy
// pandas rolling(w, center=True) covers [i - w/2, i + w/2 - 1]
const int HP_B = 50, HP_F = 49;    // 1 s mean: high-pass
const int BG_B = 250, BG_F = 249;  // 5 s median: the surrounding road
const int DN_B = 5, DN_F = 4;      // 0.1 s: at least 3 candidates
const int HIT_LEN = 100;           // a hit is described by its first second
const int HEAD = 100;              // heading from the 2 s around a hit
const int N = 1024;                // ring: holds the ~400 samples of look-back and look-ahead
const int NFIX = 64;
const int PATH = 16;

struct Hit { float t; double lat, lon; float heading, severity, vert, lat_g, speed; };
struct Window { float t; int npath; double path[PATH + 1][2]; float rough, rough_lat, rn, speed; };

inline float speedNorm(float x, float speed) { return x * powf(REF_SPEED / std::max(speed, MIN_SPEED), SPEED_EXP); }

class Detector {
 public:
  void (*onHit)(const Hit &) = nullptr;
  void (*onWindow)(const Window &) = nullptr;

  void reset() {
    n_ = s1_ = s2_ = s3_ = 0;
    nfix_ = 0;
    hits_ = 0;
    dist_ = 0;
    win_ = -1;
    wn_ = 0;
  }

  // A GPS fix on the same clock as the samples. Fixes must arrive in time order.
  void fix(int32_t t, double lat, double lon, float speed) {
    Fix &f = fixes_[nfix_++ % NFIX];
    f.t = t; f.lat = lat; f.lon = lon; f.speed = speed;
  }

  // One 100 Hz sample: road-frame lateral and vertical acceleration (gravity removed), m/s^2.
  void sample(int32_t t, int32_t seq, float lat, float vert) {
    int k = n_ % N;
    t_[k] = t; seq_[k] = seq; lat_[k] = lat; vert_[k] = vert;
    n_++;
    run(false);
  }

  // End of a drive: the last samples get truncated windows, as pandas does at the end of a series.
  void finish() {
    run(true);
    flushWindow();
  }

 private:
  struct Fix { int32_t t; double lat, lon; float speed; };
  Fix fixes_[NFIX];
  long nfix_;
  int32_t t_[N], seq_[N];
  float lat_[N], vert_[N], vhp_[N], lhp_[N], mag_[N], spd_[N];
  bool cand_[N];
  float scratch_[BG_B + BG_F + 1];
  long n_, s1_, s2_, s3_, hits_;  // samples in; next sample for each stage
  int32_t lastHitT_;
  double dist_;
  long win_;
  // the open window
  int wn_, npath_, stride_;
  int32_t wlast_;
  float wt_;
  double wspeed_, wv2_, wl2_;
  double path_[PATH][2];

  // np.interp over the fixes: clamps outside them; no fix yet means stopped, nowhere
  void at(int32_t t, double &lat, double &lon, float &speed) const {
    long lo = nfix_ > NFIX ? nfix_ - NFIX : 0;
    if (nfix_ == 0) { lat = lon = NAN; speed = 0; return; }
    const Fix *b = &fixes_[(nfix_ - 1) % NFIX];
    if (t >= b->t) { lat = b->lat; lon = b->lon; speed = b->speed; return; }
    for (long i = nfix_ - 2; i >= lo; i--) {
      const Fix *a = &fixes_[i % NFIX];
      if (t >= a->t) {
        double f = double(t - a->t) / (b->t - a->t);
        lat = a->lat + f * (b->lat - a->lat);
        lon = a->lon + f * (b->lon - a->lon);
        speed = a->speed + f * (b->speed - a->speed);
        return;
      }
      b = a;
    }
    lat = b->lat; lon = b->lon; speed = b->speed;
  }

  void run(bool end) {
    for (; s1_ < n_ && (end || s1_ + HP_F < n_); s1_++) highpass(s1_);
    for (; s2_ < s1_ && (end || s2_ + BG_F < s1_); s2_++) candidate(s2_);
    for (; s3_ < s2_ && (end || (s3_ + DN_F < s2_ && s3_ + HIT_LEN <= s1_ && s3_ + HEAD < n_)); s3_++) describe(s3_);
  }

  // high-pass: minus the centered 1 s mean, which removes slow body motion and steady cornering
  void highpass(long i) {
    long a = std::max(0L, i - HP_B), b = std::min(n_ - 1, i + HP_F);
    double sv = 0, sl = 0;
    for (long j = a; j <= b; j++) { sv += vert_[j % N]; sl += lat_[j % N]; }
    int k = i % N;
    vhp_[k] = vert_[k] - float(sv / (b - a + 1));
    lhp_[k] = lat_[k] - float(sl / (b - a + 1));
    mag_[k] = hypotf(vhp_[k], lhp_[k]);
  }

  void candidate(long i) {
    long a = std::max(0L, i - BG_B), b = std::min(s1_ - 1, i + BG_F);
    int m = 0;
    for (long j = a; j <= b; j++) scratch_[m++] = mag_[j % N];
    std::nth_element(scratch_, scratch_ + m / 2, scratch_ + m);
    float bg = scratch_[m / 2];
    if (m % 2 == 0) bg = (bg + *std::max_element(scratch_, scratch_ + m / 2)) / 2;
    int k = i % N;
    double lat, lon;
    at(t_[k], lat, lon, spd_[k]);
    cand_[k] = spd_[k] >= MIN_SPEED && speedNorm(mag_[k], spd_[k]) > HIT_MS2 && mag_[k] > HIT_RATIO * bg;
  }

  void describe(long i) {
    int k = i % N;
    long a = std::max(0L, i - DN_B), b = std::min(s2_ - 1, i + DN_F);
    int dense = 0;
    for (long j = a; j <= b; j++) dense += cand_[j % N];
    if (cand_[k] && dense >= 3 && (!hits_ || t_[k] - lastHitT_ > 1000)) {
      hits_++;
      lastHitT_ = t_[k];
      if (onHit) onHit(hit(i));
    }
    // the window this sample falls in, by distance travelled; seq steps clipped as in pipeline.py
    int32_t dseq = i ? std::min(std::max(seq_[k] - seq_[(i - 1) % N], 0), 10) : 0;
    dist_ += spd_[k] * dseq * 0.01;
    long w = long(floor(dist_ / WINDOW_M));
    if (w != win_) { flushWindow(); win_ = w; }
    if (spd_[k] >= MIN_SPEED) addToWindow(k);
  }

  Hit hit(long i) {
    int k = i % N;
    Hit h;
    h.t = t_[k] / 1000.0f;
    float spd;
    at(t_[k], h.lat, h.lon, spd);
    h.speed = spd_[k];
    float mx = 0, v = 0, l = 0;
    for (long j = i, e = std::min(i + HIT_LEN, s1_); j < e; j++) {
      int q = j % N;
      mx = std::max(mx, mag_[q]); v = std::max(v, fabsf(vhp_[q])); l = std::max(l, fabsf(lhp_[q]));
    }
    h.severity = speedNorm(mx, spd_[k]);
    h.vert = v; h.lat_g = l;
    double la, lo, lb, lob;
    at(t_[std::max(i - HEAD, 0L) % N], la, lo, spd);
    at(t_[std::min(i + HEAD, n_ - 1) % N], lb, lob, spd);
    float hd = float(atan2((lob - lo) * KX, (lb - la) * KY) / DEG);
    h.heading = fmodf(hd + 360, 360);
    return h;
  }

  // Path points: every stride-th moving sample; when full, drop every other point and double the stride.
  void addToWindow(int k) {
    if (wn_ == 0) { wt_ = t_[k] / 1000.0f; wspeed_ = wv2_ = wl2_ = 0; npath_ = 0; stride_ = 1; }
    wspeed_ += spd_[k]; wv2_ += vhp_[k] * vhp_[k]; wl2_ += lhp_[k] * lhp_[k];
    if (wn_ % stride_ == 0) {
      if (npath_ == PATH) {
        for (int j = 0; j < PATH / 2; j++) { path_[j][0] = path_[2 * j][0]; path_[j][1] = path_[2 * j][1]; }
        npath_ = PATH / 2;
        stride_ *= 2;
      }
      if (wn_ % stride_ == 0) { float s; at(t_[k], path_[npath_][0], path_[npath_][1], s); npath_++; }
    }
    wlast_ = t_[k];
    wn_++;
  }

  void flushWindow() {
    if (wn_ >= 100 && onWindow) {  // under 1 s of moving data is not a window
      Window w;
      w.t = wt_;
      for (int j = 0; j < npath_; j++) { w.path[j][0] = path_[j][0]; w.path[j][1] = path_[j][1]; }
      float s;
      at(wlast_, w.path[npath_][0], w.path[npath_][1], s);
      w.npath = npath_ + 1;
      w.rough = sqrt(wv2_ / wn_);
      w.rough_lat = sqrt(wl2_ / wn_);
      w.speed = wspeed_ / wn_;
      w.rn = speedNorm(w.rough, w.speed);
      onWindow(w);
    }
    wn_ = 0;
  }
};

// Replaying a logged drive (the logger's _imu.csv and _gps.csv, as in data/raw/drives)
inline int column(const char *header, const char *name) {
  size_t n = strlen(name);
  for (int col = 0; header; col++, header = strchr(header, ',') ? strchr(header, ',') + 1 : nullptr)
    if (!strncmp(header, name, n) && strchr(",\r\n", header[n])) return col;  // strchr also matches the '\0'
  return -1;
}

inline double field(const char *line, int col) {  // NAN when empty or missing
  for (int c = 0; c < col; c++) {
    while (*line && *line != ',') line++;
    if (!*line) return NAN;
    line++;
  }
  char *end;
  double v = strtod(line, &end);
  return end == line ? NAN : v;
}

}  // namespace detect
