// Laptop build of the device's detector, for check.py: replays one drive, prints hits and windows.
//   check.exe data/raw/drives/drive_20260928_203615
#include <stdio.h>
#include <string>
#include "src/detect.h"

static detect::Detector det;

int main(int argc, char **argv) {
  if (argc < 2) return 2;
  std::string stem = argv[1];
  FILE *imu = fopen((stem + "_imu.csv").c_str(), "r"), *gps = fopen((stem + "_gps.csv").c_str(), "r");
  if (!imu || !gps) return 1;
  static char a[512], g[512];
  fgets(a, sizeof a, imu);
  int ct = detect::column(a, "t_ms"), cs = detect::column(a, "seq"), cl = detect::column(a, "lat"), cv = detect::column(a, "vert");
  fgets(g, sizeof g, gps);
  int gt = detect::column(g, "t_ms"), gla = detect::column(g, "lat"), glo = detect::column(g, "lon"), gs = detect::column(g, "speed_mps");
  bool more = fgets(g, sizeof g, gps) != nullptr;

  det.onHit = [](const detect::Hit &h) {
    printf("hit %.2f %.6f %.6f %.1f %.2f %.2f %.2f %.2f\n", h.t, h.lat, h.lon, h.heading, h.severity, h.vert, h.lat_g, h.speed);
  };
  det.onWindow = [](const detect::Window &w) {
    printf("win %.2f %.4f %.4f %.4f %.2f %.6f %.6f\n", w.t, w.rough, w.rough_lat, w.rn, w.speed, w.path[0][0], w.path[0][1]);
  };
  det.reset();
  while (fgets(a, sizeof a, imu)) {
    double t = detect::field(a, ct);
    for (; more && detect::field(g, gt) <= t; more = fgets(g, sizeof g, gps) != nullptr) {
      double la = detect::field(g, gla), lo = detect::field(g, glo);
      if (la == la && lo == lo) det.fix(int32_t(detect::field(g, gt)), la, lo, float(detect::field(g, gs)));
    }
    det.sample(int32_t(t), int32_t(detect::field(a, cs)), float(detect::field(a, cl)), float(detect::field(a, cv)));
  }
  det.finish();
  return 0;
}
