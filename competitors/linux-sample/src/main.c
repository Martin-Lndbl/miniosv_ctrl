// What sampling costs on Linux: the baseline for apps/bench/pmc-sample.
//
// Same workload, frequencies and schema, so both sides land on one axis.
// Overhead is against the same workload unsampled in the same process, never
// an absolute time against miniOSv's.
//
// Deliberately generous to Linux: PERF_SAMPLE_IP only (no callchain, matching
// the miniOSv handler), a ring big enough that nothing drains it mid-run (perf
// record pays for a consumer thread; this does not), precise_ip = 0, and
// exclude_kernel = 0 since a unikernel has no such split.
//
// Stdlib only, static: it runs on a bare AL2023 instance with no toolchain.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/perf_event.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#ifndef PMC_REPS
#define PMC_REPS 5
#endif
// These have to equal apps/bench/pmc-sample/config.hh, not merely resemble it.
// A budget is wall time per measurement, so a mismatch means the two arms of
// the comparison run for different lengths and their noise floors are not the
// same size: at 200ms against miniOSv's 2s this side collected 19 samples at
// 99Hz where miniOSv collected 196, and the low-frequency rows came back with
// negative overhead because the difference was smaller than the noise.
//
// The cap tracks the budget for the reason the miniOSv config records: if it
// binds first, the clamp rather than the budget decides how long a rep lasts.
#ifndef PMC_BUDGET_NS
#define PMC_BUDGET_NS 2000000000.0
#endif
#ifndef PMC_PROBE_ITERS
#define PMC_PROBE_ITERS 32
#endif
#ifndef PMC_MIN_ITERS
#define PMC_MIN_ITERS 32ull
#endif
#ifndef PMC_MAX_ITERS
#define PMC_MAX_ITERS 20000000000ull
#endif
#ifndef PMC_WORK
#define PMC_WORK 64
#endif
#ifndef PMC_CALIBRATE_MS
#define PMC_CALIBRATE_MS 50
#endif

// 2^n data pages after the header page. 4096 pages = 16 MiB, enough that the
// highest frequency here never wraps inside one measurement.
#ifndef PMC_RING_PAGES
#define PMC_RING_PAGES 4096
#endif

static const int freqs[] = {99, 997, 4000, 10000, 50000};

static volatile uint64_t sink;

static long perf_event_open(struct perf_event_attr *attr, pid_t pid, int cpu,
                            int group_fd, unsigned long flags) {
  return syscall(SYS_perf_event_open, attr, pid, cpu, group_fd, flags);
}

static double now_ns(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}

// The workload, identical to the miniOSv side: a dependent LCG chain, linear
// in `work` and independent of cache and branch predictor.
static uint64_t spin(uint64_t work, uint64_t x) {
  for (uint64_t i = 0; i < work; ++i) {
    x = x * 6364136223846793005ull + 1442695040888963407ull;
    __asm__ volatile("" : "+r"(x));
  }
  return x;
}

static int cmp_double(const void *a, const void *b) {
  double x = *(const double *)a, y = *(const double *)b;
  return (x > y) - (x < y);
}

static double median(double *v, int n) {
  if (n <= 0)
    return 0;
  qsort(v, (size_t)n, sizeof(*v), cmp_double);
  return n % 2 ? v[n / 2] : 0.5 * (v[n / 2 - 1] + v[n / 2]);
}

// ns per unit of work, over `iters` units.
static double run_units(uint64_t iters) {
  uint64_t x = 1;
  double t0 = now_ns();
  for (uint64_t i = 0; i < iters; ++i)
    x = spin(PMC_WORK, x);
  double t1 = now_ns();
  sink = x;
  return (t1 - t0) / (double)iters;
}

// Same sizing rule as the miniOSv side, so both spend equal wall time.
//
// Load-bearing rather than incidental: sizing once means the sampled window
// and the timed window are the same window, so samples/secs is a rate and not
// a ratio of two different intervals. The miniOSv side briefly sized itself in
// three timed loops while the sampler was armed across all of them and
// reported delivered_pct near 200%. Both arms have to agree on this rule for
// the column to be comparable at all.
static uint64_t sized_iters(void) {
  double per = run_units(PMC_PROBE_ITERS);
  if (per <= 0)
    per = 1.0;
  uint64_t n = (uint64_t)(PMC_BUDGET_NS / per);
  if (n < PMC_MIN_ITERS)
    n = PMC_MIN_ITERS;
  if (n > PMC_MAX_ITERS)
    n = PMC_MAX_ITERS;
  return n;
}

// Core clock: a frequency becomes a period only once this is known, and both
// systems must measure it the same way for the periods to match.
static double cpu_hz(void) {
  struct perf_event_attr attr;
  memset(&attr, 0, sizeof(attr));
  attr.type = PERF_TYPE_HARDWARE;
  attr.size = sizeof(attr);
  attr.config = PERF_COUNT_HW_CPU_CYCLES;
  attr.disabled = 1;
  attr.exclude_hv = 1;

  int fd = (int)perf_event_open(&attr, 0, -1, -1, 0);
  if (fd < 0)
    return 0;

  ioctl(fd, PERF_EVENT_IOC_RESET, 0);
  ioctl(fd, PERF_EVENT_IOC_ENABLE, 0);
  double t0 = now_ns();
  while (now_ns() - t0 < PMC_CALIBRATE_MS * 1e6)
    __asm__ volatile("" ::: "memory");
  double t1 = now_ns();
  ioctl(fd, PERF_EVENT_IOC_DISABLE, 0);

  uint64_t cycles = 0;
  if (read(fd, &cycles, sizeof(cycles)) != (ssize_t)sizeof(cycles))
    cycles = 0;
  close(fd);

  double s = (t1 - t0) / 1e9;
  return (s > 0 && cycles) ? (double)cycles / s : 0;
}

struct sampler {
  int fd;
  void *base;
  size_t len;
};

static int sampler_open(struct sampler *s, uint64_t period) {
  struct perf_event_attr attr;
  memset(&attr, 0, sizeof(attr));
  attr.type = PERF_TYPE_HARDWARE;
  attr.size = sizeof(attr);
  attr.config = PERF_COUNT_HW_CPU_CYCLES;
  attr.sample_period = period;
  attr.sample_type = PERF_SAMPLE_IP;
  attr.disabled = 1;
  attr.exclude_hv = 1;
  // Not exclude_kernel: that would hide work miniOSv is charged for.
  attr.precise_ip = 0;
  attr.wakeup_events = 0;

  s->fd = (int)perf_event_open(&attr, 0, -1, -1, 0);
  if (s->fd < 0)
    return -1;

  long page = sysconf(_SC_PAGESIZE);
  s->len = (size_t)(PMC_RING_PAGES + 1) * (size_t)page;
  s->base = mmap(NULL, s->len, PROT_READ | PROT_WRITE, MAP_SHARED, s->fd, 0);
  if (s->base == MAP_FAILED) {
    close(s->fd);
    s->fd = -1;
    return -1;
  }
  return 0;
}

// Counted after the workload, never during: draining is a consumer's cost.
//
// Throttle records are counted, not skipped. Linux caps sampling at
// perf_cpu_time_max_percent (25% of cpu time by default); past that the
// kernel lowers the effective rate and emits PERF_RECORD_THROTTLE. Without
// this the resulting shortfall looks identical to the sampler failing to keep
// up, and the two mean opposite things -- one of them is Linux working as
// designed. Measured at 50kHz: 40.7% overhead predicts 71% delivery via the
// wall-clock stretch alone, but only 32.6% arrived, and this column is what
// says whether the throttle is the difference.
static uint64_t sampler_count(struct sampler *s, uint64_t *throttles) {
  struct perf_event_mmap_page *meta = s->base;
  long page = sysconf(_SC_PAGESIZE);
  uint64_t head = __atomic_load_n(&meta->data_head, __ATOMIC_ACQUIRE);
  uint64_t tail = meta->data_tail;
  uint64_t size = (uint64_t)PMC_RING_PAGES * (uint64_t)page;
  char *data = (char *)s->base + page;

  uint64_t n = 0, thr = 0;
  // Wrapped: records are gone, so this is a floor. delivered_pct shows it.
  if (head - tail > size)
    tail = head - size;
  while (tail < head) {
    struct perf_event_header *h =
        (struct perf_event_header *)(data + (tail % size));
    if (h->size == 0)
      break;
    if (h->type == PERF_RECORD_SAMPLE)
      ++n;
    else if (h->type == PERF_RECORD_THROTTLE)
      ++thr;
    tail += h->size;
  }
  __atomic_store_n(&meta->data_tail, head, __ATOMIC_RELEASE);
  if (throttles)
    *throttles = thr;
  return n;
}

static void sampler_close(struct sampler *s) {
  if (s->base && s->base != MAP_FAILED)
    munmap(s->base, s->len);
  if (s->fd >= 0)
    close(s->fd);
  s->base = NULL;
  s->fd = -1;
}

int main(void) {
  setvbuf(stdout, NULL, _IOLBF, 0);

  double hz = cpu_hz();
  if (hz <= 0) {
    printf("pmc-sample: no usable PMU here -- perf_event_open failed (%s). "
           "On EC2 the vPMU must be exposed and perf_event_paranoid low "
           "enough.\n",
           strerror(errno));
    return 1;
  }

#if defined(__x86_64__)
  const char *arch = "x86_64";
#else
  const char *arch = "aarch64";
#endif

  printf("pmc-sample: os=linux arch=%s vendor=%s cpu_mhz=%.1f work=%d reps=%d "
         "budget_ms=%.0f\n",
         arch, "unknown", hz / 1e6, PMC_WORK, PMC_REPS, PMC_BUDGET_NS / 1e6);

  uint64_t iters = sized_iters();

  // The same measurement twice, unsampled: an overhead below this is not
  // evidence of an overhead.
  double control[PMC_REPS];
  for (int r = 0; r < PMC_REPS; ++r) {
    double a = run_units(iters);
    double b = run_units(iters);
    control[r] = a > 0 ? 100.0 * (b - a) / a : 0.0;
    if (control[r] < 0)
      control[r] = -control[r];
  }
  printf("pmc-sample: noise_floor_pct=%.3f\n", median(control, PMC_REPS));

  // One row per repetition, not a median: dispersion has to survive into the
  // CSV. delivered_pct is the validity gate: requested is not delivered.
  printf("#SAMPLE,rep,freq_hz,period_events,base_ns,sampled_ns,samples,"
         "samples_per_s,delivered_pct,dead,throttles\n");

  for (size_t fi = 0; fi < sizeof(freqs) / sizeof(freqs[0]); ++fi) {
    int f = freqs[fi];
    uint64_t period = (uint64_t)(hz / f);

    for (int r = 0; r < PMC_REPS; ++r) {
      // Interleaved so drift over the run lands on both arms.
      double b = run_units(iters);

      struct sampler s = {.fd = -1};
      if (sampler_open(&s, period) < 0) {
        printf("pmc-sample: perf_event_open failed at %d Hz: %s\n", f,
               strerror(errno));
        break;
      }
      ioctl(s.fd, PERF_EVENT_IOC_RESET, 0);
      ioctl(s.fd, PERF_EVENT_IOC_ENABLE, 0);
      double sv = run_units(iters);
      ioctl(s.fd, PERF_EVENT_IOC_DISABLE, 0);
      uint64_t throttles = 0;
      uint64_t samples = sampler_count(&s, &throttles);
      sampler_close(&s);

      int dead = (sv <= 0 || samples == 0);
      double secs = dead ? 0 : sv * (double)iters / 1e9;
      double sps = secs > 0 ? (double)samples / secs : 0;
      printf("SAMPLE,%d,%d,%llu,%.4f,%.4f,%llu,%.1f,%.1f,%d,%llu\n", r, f,
             (unsigned long long)period, b, sv,
             (unsigned long long)samples, sps, 100.0 * sps / f, dead,
             (unsigned long long)throttles);
    }
  }

  printf("pmc-sample: done\n");
  return 0;
}
