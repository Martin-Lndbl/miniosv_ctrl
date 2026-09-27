/*
 * netphase.so: where a request's time goes, read off the socket syscalls.
 *
 * LD_PRELOADed into the stock DuckDB CLI, so no rebuild of DuckDB or httpfs.
 * Every send and receive on a connected TCP socket is stamped. On one
 * connection an exchange is: send(s), then receive(s), then the next send.
 *
 *   ttfb   the last send before the first receive -> that receive returning:
 *          the server's turnaround, S3's own latency plus a round trip
 *   body   first receive -> last receive of the exchange: the body on the wire
 *   gap    last receive -> the next send on the same socket: DuckDB and the
 *          client between two requests on this connection
 *
 * The first exchange on a port-443 connection is the TLS handshake and is
 * counted apart (hs_*). A TLS 1.3 server may push session tickets before the
 * first response on a fresh connection; that first request's ttfb then reads
 * a round trip short. Reused connections, the large majority, are exact.
 *
 * Results go to $NETPHASE_OUT at exit, one key=value per line. Times in ns.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

#define MAXFD 65536
#define HB 512 /* 1 ms buckets; the last one is everything past 511 ms */

enum { IDLE = 0, SENT = 1, RECEIVING = 2 };

struct slot {
    uint8_t sock, phase, tls;
    uint32_t exchanges;
    uint64_t t_send, t_first, t_last, bytes;
};
static struct slot slots[MAXFD];

struct agg {
    atomic_uint_fast64_t n, ttfb, body, gap, gap_n, bytes;
    atomic_uint_fast64_t h_ttfb[HB], h_body[HB];
};
static struct agg req, hs;
static atomic_uint_fast64_t t_first_send, t_last_recv, conns;

static ssize_t (*real_read)(int, void *, size_t);
static ssize_t (*real_write)(int, const void *, size_t);
static ssize_t (*real_recv)(int, void *, size_t, int);
static ssize_t (*real_send)(int, const void *, size_t, int);
static ssize_t (*real_recvfrom)(int, void *, size_t, int, struct sockaddr *, socklen_t *);
static ssize_t (*real_sendto)(int, const void *, size_t, int, const struct sockaddr *, socklen_t);
static ssize_t (*real_recvmsg)(int, struct msghdr *, int);
static ssize_t (*real_sendmsg)(int, const struct msghdr *, int);
static ssize_t (*real_readv)(int, const struct iovec *, int);
static ssize_t (*real_writev)(int, const struct iovec *, int);
static int (*real_connect)(int, const struct sockaddr *, socklen_t);
static int (*real_close)(int);

static uint64_t now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void hist(atomic_uint_fast64_t *h, uint64_t ns)
{
    uint64_t ms = ns / 1000000;
    atomic_fetch_add(&h[ms < HB ? ms : HB - 1], 1);
}

static void close_exchange(struct slot *s, uint64_t t_next, int has_next)
{
    if (s->phase != RECEIVING) {
        return; /* a send with nothing received yet folds into the same exchange */
    }
    struct agg *a = (s->exchanges == 0 && s->tls) ? &hs : &req;
    uint64_t ttfb = s->t_first - s->t_send, body = s->t_last - s->t_first;
    atomic_fetch_add(&a->n, 1);
    atomic_fetch_add(&a->ttfb, ttfb);
    atomic_fetch_add(&a->body, body);
    atomic_fetch_add(&a->bytes, s->bytes);
    hist(a->h_ttfb, ttfb);
    hist(a->h_body, body);
    if (has_next) {
        atomic_fetch_add(&a->gap, t_next - s->t_last);
        atomic_fetch_add(&a->gap_n, 1);
    }
    s->exchanges++;
    s->phase = IDLE;
    s->bytes = 0;
}

static void on_send(int fd)
{
    if (fd < 0 || fd >= MAXFD || !slots[fd].sock) {
        return;
    }
    struct slot *s = &slots[fd];
    uint64_t t = now();
    close_exchange(s, t, 1);
    s->t_send = t; /* the last send before the response starts */
    s->phase = SENT;
    uint64_t z = 0;
    atomic_compare_exchange_strong(&t_first_send, &z, t);
}

static void on_recv(int fd, ssize_t n)
{
    if (fd < 0 || fd >= MAXFD || !slots[fd].sock || n <= 0) {
        return;
    }
    struct slot *s = &slots[fd];
    if (s->phase == IDLE) {
        return; /* nothing asked for: a session ticket, or noise */
    }
    uint64_t t = now();
    if (s->phase == SENT) {
        s->t_first = t;
        s->phase = RECEIVING;
    }
    s->t_last = t;
    s->bytes += (uint64_t)n;
    uint64_t cur = atomic_load(&t_last_recv);
    while (t > cur && !atomic_compare_exchange_weak(&t_last_recv, &cur, t)) {
    }
}

int connect(int fd, const struct sockaddr *addr, socklen_t len)
{
    int rc = real_connect(fd, addr, len);
    if (fd >= 0 && fd < MAXFD && addr) {
        int port = -1;
        if (addr->sa_family == AF_INET) {
            port = ntohs(((const struct sockaddr_in *)addr)->sin_port);
        } else if (addr->sa_family == AF_INET6) {
            port = ntohs(((const struct sockaddr_in6 *)addr)->sin6_port);
        }
        if (port >= 0) {
            struct slot *s = &slots[fd];
            memset(s, 0, sizeof *s);
            s->sock = 1;
            s->tls = port == 443;
            atomic_fetch_add(&conns, 1);
        }
    }
    return rc;
}

int close(int fd)
{
    if (fd >= 0 && fd < MAXFD && slots[fd].sock) {
        close_exchange(&slots[fd], 0, 0);
        slots[fd].sock = 0;
    }
    return real_close(fd);
}

ssize_t read(int fd, void *b, size_t n) { ssize_t r = real_read(fd, b, n); on_recv(fd, r); return r; }
ssize_t recv(int fd, void *b, size_t n, int f) { ssize_t r = real_recv(fd, b, n, f); on_recv(fd, r); return r; }
ssize_t recvfrom(int fd, void *b, size_t n, int f, struct sockaddr *a, socklen_t *l)
{ ssize_t r = real_recvfrom(fd, b, n, f, a, l); on_recv(fd, r); return r; }
ssize_t recvmsg(int fd, struct msghdr *m, int f) { ssize_t r = real_recvmsg(fd, m, f); on_recv(fd, r); return r; }
ssize_t readv(int fd, const struct iovec *v, int c) { ssize_t r = real_readv(fd, v, c); on_recv(fd, r); return r; }

ssize_t write(int fd, const void *b, size_t n) { on_send(fd); return real_write(fd, b, n); }
ssize_t send(int fd, const void *b, size_t n, int f) { on_send(fd); return real_send(fd, b, n, f); }
ssize_t sendto(int fd, const void *b, size_t n, int f, const struct sockaddr *a, socklen_t l)
{ on_send(fd); return real_sendto(fd, b, n, f, a, l); }
ssize_t sendmsg(int fd, const struct msghdr *m, int f) { on_send(fd); return real_sendmsg(fd, m, f); }
ssize_t writev(int fd, const struct iovec *v, int c) { on_send(fd); return real_writev(fd, v, c); }

__attribute__((constructor)) static void init(void)
{
    real_read = dlsym(RTLD_NEXT, "read");
    real_write = dlsym(RTLD_NEXT, "write");
    real_recv = dlsym(RTLD_NEXT, "recv");
    real_send = dlsym(RTLD_NEXT, "send");
    real_recvfrom = dlsym(RTLD_NEXT, "recvfrom");
    real_sendto = dlsym(RTLD_NEXT, "sendto");
    real_recvmsg = dlsym(RTLD_NEXT, "recvmsg");
    real_sendmsg = dlsym(RTLD_NEXT, "sendmsg");
    real_readv = dlsym(RTLD_NEXT, "readv");
    real_writev = dlsym(RTLD_NEXT, "writev");
    real_connect = dlsym(RTLD_NEXT, "connect");
    real_close = dlsym(RTLD_NEXT, "close");
}

static int put(int fd, char *buf, size_t cap, size_t *len, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf + *len, cap - *len, fmt, ap);
    va_end(ap);
    if (n < 0) {
        return -1;
    }
    *len += (size_t)n;
    if (*len > cap - 4096) {
        real_write(fd, buf, *len);
        *len = 0;
    }
    return 0;
}

static void dump_agg(int fd, char *buf, size_t cap, size_t *len, const char *p, struct agg *a)
{
    put(fd, buf, cap, len, "%s_n=%lu\n%s_ttfb_ns=%lu\n%s_body_ns=%lu\n%s_gap_ns=%lu\n%s_gap_n=%lu\n%s_bytes=%lu\n",
        p, (unsigned long)a->n, p, (unsigned long)a->ttfb, p, (unsigned long)a->body,
        p, (unsigned long)a->gap, p, (unsigned long)a->gap_n, p, (unsigned long)a->bytes);
    put(fd, buf, cap, len, "%s_hist_ttfb_ms=", p);
    for (int i = 0; i < HB; i++) {
        put(fd, buf, cap, len, "%s%lu", i ? "," : "", (unsigned long)a->h_ttfb[i]);
    }
    put(fd, buf, cap, len, "\n%s_hist_body_ms=", p);
    for (int i = 0; i < HB; i++) {
        put(fd, buf, cap, len, "%s%lu", i ? "," : "", (unsigned long)a->h_body[i]);
    }
    put(fd, buf, cap, len, "\n");
}

__attribute__((destructor)) static void fini(void)
{
    const char *out = getenv("NETPHASE_OUT");
    /* A wrapper that inherited LD_PRELOAD but never opened a socket (timeout,
     * a shell) exits after the process that did and would truncate its file. */
    if (!out || atomic_load(&conns) == 0) {
        return;
    }
    /* Exchanges still open at exit: count what they have. */
    for (int fd = 0; fd < MAXFD; fd++) {
        if (slots[fd].sock) {
            close_exchange(&slots[fd], 0, 0);
        }
    }
    int fd = open(out, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        return;
    }
    static char buf[1 << 16];
    size_t len = 0;
    uint64_t fs = atomic_load(&t_first_send), lr = atomic_load(&t_last_recv);
    put(fd, buf, sizeof buf, &len, "conns=%lu\nwindow_ns=%lu\n", (unsigned long)conns,
        (unsigned long)(lr > fs ? lr - fs : 0));
    dump_agg(fd, buf, sizeof buf, &len, "req", &req);
    dump_agg(fd, buf, sizeof buf, &len, "hs", &hs);
    if (len) {
        real_write(fd, buf, len);
    }
    real_close(fd);
}
