// anyblob-s3: the smoltcp-s3 workload driven through AnyBlob (Durner, Leis,
// Neumann, VLDB 2023), the io_uring download manager Umbra reads S3 with.
//
// Same shape as apps/bench/smoltcp-s3 and competitors/linux-s3: W worker
// threads, C requests in flight per worker, K blocks of B bytes per worker
// taken from one object with the same wrap-around ranges. W threads each run
// one AnyBlob TaskedSendReceiver daemon (its own io_uring, its own
// connection cache), every request is a ranged GET the library builds, and
// the library's own callback path validates and recycles each response. The
// summary lines are the ones scripts/bench/runner.py parses, so the rows land
// next to the other two stacks' in one CSV.
//
// What is AnyBlob's and what is ours: connection handling, TLS (OpenSSL),
// HTTP parsing, retries, the throughput-based resolver and the receive loop
// are the library, untouched. Ours is only the request list, the buffer pool
// handed to it before the clock starts, and the accounting.
//
// Runtime configuration, from the environment (see scripts/instance.py):
//   AWS_BUCKET, AWS_REGION, AWS_BUCKET_SIZE   the object, 10G by default
//   BENCH_WORKERS, BENCH_CONNS_PER_WORKER    W and C (C is capped at 128 by
//                                            the library, per daemon)
//   BENCH_BLOCK_SIZE, BENCH_BLOCKS           B and K (K=0 means K=C)
//   BENCH_SCHEME                             https (default) or http
//   BENCH_CHUNK                              recv size per io_uring op; the
//                                            library's default is 64 KiB
//   BENCH_PATH                               object key, /blob.bin
//   BENCH_URL                                any https://host/key instead of
//                                            the bucket (local smoke test)
//   BENCH_IFACE                              NIC whose rx_bytes the sampler
//                                            reads for the WIRE lines
//   AWS_TARGET_IP                            echoed; the pin itself is an
//                                            /etc/hosts entry the instance
//                                            script writes

#include "cloud/provider.hpp"
#include "network/tasked_send_receiver.hpp"
#include "network/transaction.hpp"
#include "utils/data_vector.hpp"
#include "utils/timer.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <dirent.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

using namespace std;
namespace ab = anyblob;
using clk = chrono::steady_clock;

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

static string env(const char* k, const string& d = "") {
    const char* v = getenv(k);
    return (v && *v) ? string(v) : d;
}

// Same acceptance as the other two stacks: digits, then K/M/G/T.
static uint64_t parseSize(const string& s, uint64_t d) {
    if (s.empty()) return d;
    char* end = nullptr;
    unsigned long long v = strtoull(s.c_str(), &end, 10);
    if (end == s.c_str()) return d;
    switch (*end) {
        case 'K': case 'k': return v << 10;
        case 'M': case 'm': return v << 20;
        case 'G': case 'g': return v << 30;
        case 'T': case 't': return v << 40;
        default: return v;
    }
}

struct Config {
    unsigned workers = 16;
    unsigned conns = 24;
    uint64_t block = 128ull << 20;
    uint64_t blocks = 0;  // per worker; 0 = conns
    uint64_t object = 10ull << 30;
    uint64_t chunk = 64ull << 10;
    bool https = true;
    string bucket, region, path, url, iface, targetIp;

    uint64_t blocksPerWorker() const { return blocks ? blocks : conns; }
    uint64_t requests() const { return uint64_t(workers) * blocksPerWorker(); }
};

static Config load() {
    Config c;
    c.workers = unsigned(parseSize(env("BENCH_WORKERS"), 16));
    c.conns = unsigned(parseSize(env("BENCH_CONNS_PER_WORKER"), 24));
    c.block = parseSize(env("BENCH_BLOCK_SIZE"), 128ull << 20);
    c.blocks = parseSize(env("BENCH_BLOCKS"), 0);
    c.object = parseSize(env("AWS_BUCKET_SIZE"), 10ull << 30);
    c.chunk = parseSize(env("BENCH_CHUNK"), 64ull << 10);
    c.https = env("BENCH_SCHEME", "https") != "http";
    c.bucket = env("AWS_BUCKET");
    c.region = env("AWS_REGION");
    c.path = env("BENCH_PATH", "/blob.bin");
    c.url = env("BENCH_URL");
    c.iface = env("BENCH_IFACE");
    c.targetIp = env("AWS_TARGET_IP");
    return c;
}

// lib.rs `block_range`: blocks wrap around the object with a stride that
// keeps every range inside it. Returned as [start, end) for AnyBlob.
static pair<uint64_t, uint64_t> blockRange(const Config& c, uint64_t block) {
    uint64_t stride = (c.object > c.block ? c.object - c.block : 0) + 1;
    uint64_t start = stride == 0 ? 0 : (block * c.block) % stride;
    return {start, start + c.block};
}

// ---------------------------------------------------------------------------
// Accounting
// ---------------------------------------------------------------------------

struct WorkerStat {
    atomic<uint64_t> bytes{0};
    atomic<uint64_t> done{0};
    atomic<uint64_t> lastFinishNs{0};
    char pad[64 - 3 * sizeof(atomic<uint64_t>)];
};

static thread_local unsigned tlWorker = 0;

struct Dist {
    uint64_t n = 0, avg = 0, p50 = 0, p90 = 0, p99 = 0, max = 0;
    static Dist of(vector<uint64_t> v) {
        Dist d;
        if (v.empty()) return d;
        sort(v.begin(), v.end());
        d.n = v.size();
        uint64_t sum = 0;
        for (auto x : v) sum += x;
        d.avg = sum / d.n;
        auto at = [&](double q) { return v[min(v.size() - 1, size_t(q * double(v.size())))]; };
        d.p50 = at(0.5);
        d.p90 = at(0.9);
        d.p99 = at(0.99);
        d.max = v.back();
        return d;
    }
};

// The NIC's byte counter every 10 ms from a thread of its own: one clock, one
// counter, the same instrument the miniOSv bench reads off its device.
struct Sampler {
    string path;
    atomic<bool> run{true};
    vector<pair<uint64_t, uint64_t>> samples;  // (ns since epoch, rx_bytes)
    thread th;
    clk::time_point epoch;

    static string pick(const string& want) {
        if (!want.empty()) return want;
        // First interface that is not loopback and has a carrier.
        string best;
        if (DIR* d = opendir("/sys/class/net")) {
            while (dirent* e = readdir(d)) {
                string n = e->d_name;
                if (n == "." || n == ".." || n == "lo") continue;
                ifstream f("/sys/class/net/" + n + "/operstate");
                string st;
                f >> st;
                if (st == "up" && (best.empty() || n < best)) best = n;
            }
            closedir(d);
        }
        return best;
    }

    uint64_t read() const {
        ifstream f(path);
        uint64_t v = 0;
        f >> v;
        return v;
    }

    void start(const string& iface, clk::time_point ep) {
        epoch = ep;
        if (iface.empty()) return;
        path = "/sys/class/net/" + iface + "/statistics/rx_bytes";
        if (!ifstream(path)) { path.clear(); return; }
        samples.reserve(1 << 16);
        th = thread([this] {
            while (run.load(memory_order_relaxed)) {
                auto t = clk::now();
                samples.emplace_back(uint64_t(chrono::duration_cast<chrono::nanoseconds>(t - epoch).count()), read());
                this_thread::sleep_until(t + chrono::milliseconds(10));
            }
        });
    }

    void stop() {
        run = false;
        if (th.joinable()) th.join();
    }
};

static double gbps(uint64_t bytes, double s) { return double(bytes) * 8.0 / 1e9 / max(s, 1e-9); }

// ---------------------------------------------------------------------------

int main() {
    Config c = load();
    if (c.url.empty() && (c.bucket.empty() || c.region.empty())) {
        printf("FAIL: AWS_BUCKET and AWS_REGION (or BENCH_URL) must be set\n");
        return 1;
    }
    if (c.workers == 0 || c.conns == 0 || c.block == 0 || c.object == 0) {
        printf("FAIL: BENCH_WORKERS, BENCH_CONNS_PER_WORKER, BENCH_BLOCK_SIZE and AWS_BUCKET_SIZE must be nonzero\n");
        return 1;
    }
    // W x C sockets plus the library's cache: the soft limit under
    // cloud-init is 1024.
    rlimit rl{};
    if (getrlimit(RLIMIT_NOFILE, &rl) == 0 && rl.rlim_cur < rl.rlim_max) {
        rl.rlim_cur = rl.rlim_max;
        setrlimit(RLIMIT_NOFILE, &rl);
    }

    const unsigned perDaemon = min<unsigned>(c.conns, ab::network::TaskedSendReceiverGroup::maxConcurrentRequests);
    const uint64_t total = c.requests();
    const string host = c.url.empty() ? c.bucket + ".s3." + c.region + ".amazonaws.com" : c.url;

    printf("bench: %u workers x %u conns x %llu MiB block, tls_stub=false scheme=%s blocks=%llu chunk=%llu stack=anyblob\n",
           c.workers, c.conns, (unsigned long long)(c.block >> 20), c.https ? "https" : "http",
           (unsigned long long)c.blocksPerWorker(), (unsigned long long)c.chunk);
    printf("target: %s:%u %s\n", c.targetIp.empty() ? "0.0.0.0" : c.targetIp.c_str(), c.https ? 443u : 80u, host.c_str());
    printf("note: q<N> is the worker index; AnyBlob runs one io_uring daemon per worker, "
           "requests come from one shared queue, and %u of the %u asked-for requests per worker fly at once (library cap %u). "
           "cores=%ld\n",
           perDaemon, c.conns, ab::network::TaskedSendReceiverGroup::maxConcurrentRequests, sysconf(_SC_NPROCESSORS_ONLN));
    fflush(stdout);

    // The group: shared submission queue, shared buffer pool, one daemon per
    // handle. Sized as the paper's benchmark sizes it.
    ab::network::TaskedSendReceiverGroup group(unsigned(c.chunk), total * 2 + 16, 0);
    group.setConcurrentRequests(perDaemon);
    vector<unique_ptr<ab::network::TaskedSendReceiverHandle>> handles;
    for (unsigned i = 0; i < c.workers; i++)
        handles.push_back(make_unique<ab::network::TaskedSendReceiverHandle>(group.getHandle()));

    // Anonymous: the bucket policy lets the VPC endpoint read unsigned, the
    // way the other two stacks read it. No IAM, no IMDS, no signing.
    unique_ptr<ab::cloud::Provider> provider;
    string key;
    try {
        if (c.url.empty()) {
            provider = ab::cloud::Provider::makeAnonymousProvider("s3://" + c.bucket + ":" + c.region + "/", c.https);
            key = c.path.substr(c.path.find_first_not_of('/'));
        } else {
            // https://host/key -> the plain HTTP(S) provider, Host = host.
            auto info = ab::cloud::Provider::getRemoteInfo(c.url);
            provider = ab::cloud::Provider::makeAnonymousProvider(c.url, c.https);
            key = info.key;
        }
    } catch (exception& e) {
        printf("FAIL: provider: %s\n", e.what());
        return 1;
    }

    // One buffer per slot, plus one spare per worker, allocated and touched
    // before the clock: a database hands its download manager a pool; it does
    // not page-fault 48 GiB inside the measurement.
    {
        uint64_t n = uint64_t(c.workers) * perDaemon + c.workers;
        uint64_t cap = c.block + c.chunk + (64 << 10);
        auto t0 = clk::now();
        for (uint64_t i = 0; i < n; i++) {
            auto buf = make_unique<ab::utils::DataVector<uint8_t>>(cap);  // value-initialised: touched
            handles.back()->get()->reuse(move(buf));
        }
        printf("pool: %llu buffers x %llu MiB, %.2f s to allocate and touch\n", (unsigned long long)n,
               (unsigned long long)(cap >> 20), chrono::duration<double>(clk::now() - t0).count());
    }

    // Per-request bookkeeping; traceId indexes it.
    vector<ab::utils::TimingHelper> timings(total);
    for (auto& h : handles) h->get()->setTimings(&timings);
    vector<WorkerStat> stats(c.workers);
    atomic<uint64_t> finished{0}, ok{0}, badStatus{0}, firstBad{0}, shortBody{0}, failed{0};
    atomic<uint64_t> firstIdleNs{0};
    vector<uint64_t> expected(total);
    // Which code a whole-object GET returns; a ranged one must say 206.
    const uint64_t wantCode = 206;
    clk::time_point epoch;
    mutex errMutex;
    unsigned errShown = 0;

    auto callback = [&](ab::network::MessageResult& r) {
        auto now = clk::now();
        // Every block is the same size, so the body check needs no trace id.
        bool good = r.success();
        uint64_t size = good ? r.getSize() : 0;
        uint64_t code = r.getResponseCodeNumber();
        auto& st = stats[tlWorker];
        st.bytes.fetch_add(size, memory_order_relaxed);
        st.done.fetch_add(1, memory_order_relaxed);
        st.lastFinishNs.store(uint64_t(chrono::duration_cast<chrono::nanoseconds>(now - epoch).count()), memory_order_relaxed);
        if (!good) {
            failed.fetch_add(1, memory_order_relaxed);
            lock_guard<mutex> lg(errMutex);
            if (errShown++ < 8)
                printf("q%u: request failed: failure_code=%u http=%llu\n", tlWorker, r.getFailureCode(), (unsigned long long)code);
        } else if (code != wantCode) {
            badStatus.fetch_add(1, memory_order_relaxed);
            uint64_t z = 0;
            firstBad.compare_exchange_strong(z, code);
        } else if (size != c.block) {
            shortBody.fetch_add(1, memory_order_relaxed);
            lock_guard<mutex> lg(errMutex);
            if (errShown++ < 8)
                printf("q%u: body %llu B, wanted %llu\n", tlWorker, (unsigned long long)size, (unsigned long long)c.block);
        } else {
            ok.fetch_add(1, memory_order_relaxed);
        }
        // Back to the pool before the daemon looks for its next request.
        if (r.owned())
            handles.back()->get()->reuse(r.moveDataVector());
        uint64_t f = finished.fetch_add(1, memory_order_acq_rel) + 1;
        // The moment the queue can no longer keep every slot busy.
        if (total - f < uint64_t(c.workers) * perDaemon) {
            uint64_t z = 0;
            firstIdleNs.compare_exchange_strong(z, uint64_t(chrono::duration_cast<chrono::nanoseconds>(now - epoch).count()));
        }
    };

    // The request list: worker w owns blocks [w*K, (w+1)*K), the same
    // assignment as lib.rs, built through the library's own Transaction so
    // the headers are AnyBlob's. Queued before the clock starts.
    vector<ab::network::Transaction> txns(c.workers);
    for (unsigned w = 0; w < c.workers; w++) {
        txns[w].setProvider(provider.get());
        for (uint64_t k = 0; k < c.blocksPerWorker(); k++) {
            uint64_t id = uint64_t(w) * c.blocksPerWorker() + k;
            auto range = blockRange(c, id);
            expected[id] = range.second - range.first;
            if (!txns[w].getObjectRequest(callback, key, range, nullptr, 0, id)) {
                printf("FAIL: could not build request %llu\n", (unsigned long long)id);
                return 1;
            }
        }
    }
    for (auto& t : txns)
        if (!t.processAsync(group)) {
            printf("FAIL: submission queue refused the requests\n");
            return 1;
        }

    Sampler sampler;
    rusage ru0{}, ru1{};
    getrusage(RUSAGE_SELF, &ru0);
    epoch = clk::now();
    sampler.start(Sampler::pick(c.iface), epoch);

    vector<thread> threads;
    for (unsigned w = 0; w < c.workers; w++)
        threads.emplace_back([&, w] {
            tlWorker = w;
            try {
                handles[w]->process(false);  // daemon: until stop()
            } catch (exception& e) {
                lock_guard<mutex> lg(errMutex);
                printf("FAIL: q%u: daemon: %s\n", w, e.what());
            }
        });

    // Same wait the paper's harness uses.
    while (finished.load(memory_order_acquire) < total) usleep(100);
    auto endTp = clk::now();
    for (auto& h : handles) h->stop();
    for (auto& t : threads) t.join();
    sampler.stop();
    getrusage(RUSAGE_SELF, &ru1);

    const double overallS = chrono::duration<double>(endTp - epoch).count();
    uint64_t totalB = 0;
    for (unsigned w = 0; w < c.workers; w++) {
        auto b = stats[w].bytes.load();
        double e = double(stats[w].lastFinishNs.load()) / 1e9;
        totalB += b;
        printf("worker %u (q%u): %llu B / %.3f s  (%.1f MB/s) %llu requests\n", w, w, (unsigned long long)b, e,
               double(b) / 1e6 / max(e, 1e-9), (unsigned long long)stats[w].done.load());
    }
    uint64_t totalExpected = 0;
    for (auto e : expected) totalExpected += e;

    printf("\nAGGREGATE: %.1f MiB in %.3f s => %.1f MB/s, %.3f Gbps\n", double(totalB) / (1 << 20), overallS,
           double(totalB) / 1e6 / max(overallS, 1e-9), gbps(totalB, overallS));

    // Setup is not separable here: the library dials inside its request
    // state machine and reuses sockets across requests, so TRANSFER equals
    // AGGREGATE and the connection count comes from the kernel's
    // Tcp:ActiveOpens delta the instance script prints.
    printf("TRANSFER: %.1f MiB in %.3f s => %.1f MB/s, %.3f Gbps (setup 0.000 s excluded)\n", double(totalB) / (1 << 20),
           overallS, double(totalB) / 1e6 / max(overallS, 1e-9), gbps(totalB, overallS));

    // Per-request latencies from the library's own timing hooks.
    {
        vector<uint64_t> ttfb, wire;
        ttfb.reserve(total);
        wire.reserve(total);
        for (auto& t : timings) {
            if (t.finish.time_since_epoch().count() == 0 || t.start.time_since_epoch().count() == 0) continue;
            wire.push_back(uint64_t(chrono::duration_cast<chrono::microseconds>(t.finish - t.start).count()));
            if (t.recieve.time_since_epoch().count() != 0 && t.recieve >= t.start)
                ttfb.push_back(uint64_t(chrono::duration_cast<chrono::microseconds>(t.recieve - t.start).count()));
        }
        auto tw = Dist::of(wire), tt = Dist::of(ttfb);
        printf("REQ STATS     : n=%llu ttfb_us_avg=%llu ttfb_us_p50=%llu wire_us_avg=%llu wire_us_p50=%llu wire_us_p90=%llu wire_us_p99=%llu wire_us_max=%llu\n",
               (unsigned long long)tw.n, (unsigned long long)tt.avg, (unsigned long long)tt.p50, (unsigned long long)tw.avg,
               (unsigned long long)tw.p50, (unsigned long long)tw.p90, (unsigned long long)tw.p99, (unsigned long long)tw.max);
    }

    // CPU: what the whole process burned, daemons included, over the run.
    {
        auto secs = [](timeval a, timeval b) { return double(b.tv_sec - a.tv_sec) + double(b.tv_usec - a.tv_usec) / 1e6; };
        double us = secs(ru0.ru_utime, ru1.ru_utime), sy = secs(ru0.ru_stime, ru1.ru_stime);
        printf("CPU STATS     : user_s=%.2f sys_s=%.2f total_s=%.2f cores_avg=%.2f over %.3f s (%.1f MB per cpu-second)\n", us, sy, us + sy,
               (us + sy) / max(overallS, 1e-9), overallS, double(totalB) / 1e6 / max(us + sy, 1e-9));
    }

    // The wire, from the NIC's counter.
    if (!sampler.samples.empty() && sampler.samples.size() > 1) {
        auto& s = sampler.samples;
        uint64_t b0 = s.front().second, b1 = s.back().second;
        double span = double(s.back().first - s.front().first) / 1e9;
        printf("WIRE: %.3f Gbps of frames over the run (%llu bytes received)\n", gbps(b1 - b0, span), (unsigned long long)(b1 - b0));
        uint64_t idle = firstIdleNs.load();
        if (idle == 0) idle = uint64_t(chrono::duration_cast<chrono::nanoseconds>(endTp - epoch).count());
        auto at = [&](uint64_t t) -> const pair<uint64_t, uint64_t>* {
            for (auto& p : s)
                if (p.first >= t) return &p;
            return nullptr;
        };
        auto p0 = at(0), p1 = at(idle);
        if (p0 && p1 && p1->first > p0->first)
            printf("WIRE STEADY: %.3f Gbps of frames from %.3f s to %.3f s, every slot busy (%llu B)\n",
                   gbps(p1->second - p0->second, double(p1->first - p0->first) / 1e9), double(p0->first) / 1e9,
                   double(p1->first) / 1e9, (unsigned long long)(p1->second - p0->second));
        else
            printf("WIRE STEADY: no window in which every slot was busy\n");
        double best = 0;
        size_t j = 0;
        for (size_t i = 0; i < s.size(); i++) {
            while (j < s.size() && s[j].first < s[i].first + 1000000000ull) j++;
            if (j < s.size()) best = max(best, gbps(s[j].second - s[i].second, double(s[j].first - s[i].first) / 1e9));
        }
        printf("WIRE PEAK: %.3f Gbps of frames over the best second\n", best);
        double tailMs = double(uint64_t(chrono::duration_cast<chrono::nanoseconds>(endTp - epoch).count()) - idle) / 1e6;
        printf("TAIL STATS    : idle_max_ms=%.1f idle_avg_ms=%.1f\n", tailMs, tailMs);
    } else {
        printf("WIRE: no interface to sample (set BENCH_IFACE)\n");
    }

    uint64_t nOk = ok.load(), nFail = failed.load(), nBad = badStatus.load(), nShort = shortBody.load();
    printf("\nconnections   : %llu/%llu closed cleanly, %llu failed\n", (unsigned long long)nOk, (unsigned long long)total,
           (unsigned long long)nFail);
    printf("  (requests, not sockets: AnyBlob keeps its connections and reuses them; Tcp:ActiveOpens below counts the dials)\n");
    printf("misrouted rx  : 0 packets dropped (expected 0)\n");
    printf("tx drops      : 0 no-mbuf, 0 ring-full (expected 0)\n");
    printf("http status   : %llu non-206 responses (expected 0)%s\n", (unsigned long long)nBad,
           nBad ? (firstBad.load() == 503 ? " - 503 SlowDown, S3 is throttling" : " - see first code below") : "");
    if (nBad) printf("first bad code: %llu\n", (unsigned long long)firstBad.load());
    printf("response heads: %llu did not match the range requested (expected 0)\n", (unsigned long long)nShort);
    printf("SETUP STATS   : conns=0 failed=%llu us_avg=0 us_p50=0 us_p90=0 us_max=0 (handshakes happen inside the library; not timed)\n",
           (unsigned long long)nFail);
    printf("DIAL STATS    : dials=0 us_avg=0 us_p50=0 us_p90=0 us_max=0 loop_ms=0.0\n");
    printf("blocks        : %llu/%llu of %llu MiB requested (%llu bytes)\n", (unsigned long long)total, (unsigned long long)total,
           (unsigned long long)(c.block >> 20), (unsigned long long)totalExpected);
    printf("bytes         : %llu plaintext body\n", (unsigned long long)totalB);

    if (nOk == total && totalB == totalExpected && nFail == 0 && nBad == 0 && nShort == 0) {
        printf("COMPLETE: %llu bytes, byte-exact\n", (unsigned long long)totalB);
        return 0;
    }
    printf("INCOMPLETE: %llu requests failed, %llu bad status, %llu short bodies, %llu bytes %s\n", (unsigned long long)nFail,
           (unsigned long long)nBad, (unsigned long long)nShort,
           (unsigned long long)(totalB > totalExpected ? totalB - totalExpected : totalExpected - totalB),
           totalB > totalExpected ? "over" : "short");
    return 1;
}
