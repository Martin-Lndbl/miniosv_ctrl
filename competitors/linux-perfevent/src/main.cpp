// What Viktor Leis's PerfEvent.hpp costs on Linux, against the miniOSv port
// of the same API in include/osv/perf.hh.
//
// Both wrap the identical hashtable workload in the header's own start/stop
// and report the added wall-clock time. The region is swept over its duration,
// because the two costs have different shapes: a fixed price per instrumented
// region, versus multiplexing, which the kernel pays on a timer for as long as
// the region runs.
//
// PerfEvent.hpp registers seven events by default; miniOSv's port registers
// four. Comparing those directly would compare event counts, not headers, so
// the list is pruned after construction to exactly miniOSv's set -- cycles,
// instructions, cache-misses, branch-misses -- which fits the counters on
// every machine here and so is never multiplexed. mult_pct is reported anyway,
// as the check that it really wasn't.
//
// Stdlib only, static: it runs on a bare AL2023 instance with no toolchain.

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <string>
#include <unistd.h>
#include <vector>

#include "PerfEvent.hpp"
#include "hashtable.hh"

#ifndef PMC_REPS
#define PMC_REPS 5
#endif

// miniOSv's PerfEvent default set, in its order. Trimmed to the counters the
// machine granted: c7g.large has 2, so it runs the first two.
#ifndef PMC_EVENTS
#define PMC_EVENTS 2
#endif
// The same two the miniOSv side registers, in the same order. "cycle" is
// gone deliberately: it lands on a fixed-function counter rather than a
// general one, so it neither exercises the same hardware nor costs one of the
// two general counters Graviton grants.
static const char *keep[] = {"instr", "br-miss"};

// Keys inserted and probed per region. Kept small: the added cost is tens of
// microseconds, so a region of milliseconds buries it under the workload's own
// jitter -- measured, and the largest points came back as noise.
static const size_t works[] = {500, 2000, 8000, 32000, 128000};

static volatile uint64_t sink;

// Keep only miniOSv's events. The constructor has already opened every fd, so
// the rest are closed here rather than never opened -- that way this stays
// Leis's header, driven through its own API, not a reimplementation.
static void prune(PerfEvent &e) {
  std::vector<PerfEvent::event> kept;
  for (int i = 0; i < PMC_EVENTS; ++i)
    for (auto &ev : e.events)
      if (ev.name == keep[i])
        kept.push_back(ev);
  for (auto &ev : e.events) {
    bool k = false;
    for (auto &x : kept)
      k |= x.name == ev.name;
    if (!k && ev.fd != -1)
      close(ev.fd);
  }
  e.events = kept;
}

static double now_ns() {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}



int main() {
  setvbuf(stdout, NULL, _IOLBF, 0);

  // Touch every page once, so the first timed region is not the only one
  // paying for first-touch faults on a 16 MiB BSS array.
  ht::clear();

  printf("pmc-perfevent: os=linux header=PerfEvent.hpp reps=%d ht_mib=%zu\n",
         PMC_REPS, (ht::size * sizeof(uint64_t)) >> 20);
  // One row per repetition, not a median: dispersion has to survive into the
  // CSV, and a single boot's median hides the spread between boots.
  printf("#PERFEVENT,rep,keys,base_ns,instr_ns,delta_ns,mult_pct,events\n");

  for (size_t wi = 0; wi < sizeof(works) / sizeof(works[0]); ++wi) {
    size_t n = works[wi];
    for (int r = 0; r < PMC_REPS; ++r) {
      // Interleaved so drift over the run lands on both arms.
      ht::clear();
      double t0 = now_ns();
      sink = ht::work(n, 1 + r);
      double t1 = now_ns();

      PerfEvent e;
      prune(e);
      ht::clear();
      double t2 = now_ns();
      e.startCounters();
      sink = ht::work(n, 1 + r);
      e.stopCounters();
      double t3 = now_ns();

      unsigned events = (unsigned)e.events.size();
      // time_running/time_enabled over the registered events: below 100% the
      // kernel was rotating counters and the values it reports are estimates.
      double running = 0, enabled = 0;
      for (auto &ev : e.events) {
        running += (double)(ev.data.time_running - ev.prev.time_running);
        enabled += (double)(ev.data.time_enabled - ev.prev.time_enabled);
      }
      double mult = enabled > 0 ? 100.0 * running / enabled : 0;

      printf("PERFEVENT,%d,%zu,%.0f,%.0f,%.0f,%.1f,%u\n", r, n, t1 - t0,
             t3 - t2, (t3 - t2) - (t1 - t0), mult, events);
    }
  }

  printf("pmc-perfevent: done\n");
  return 0;
}
