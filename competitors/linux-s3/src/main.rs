mod tune;

use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::os::unix::io::AsRawFd;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Barrier};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rustls::client::{ClientConfig, UnbufferedClientConnection};
use rustls::pki_types::{ServerName, UnixTime};
use rustls::time_provider::TimeProvider;
use rustls::unbuffered::{ConnectionState, UnbufferedStatus};
use rustls::RootCertStore;

use tune::Tuning;

// Compile-time on both sides, so the two cannot drift through configuration.

const TARGET_PORT: u16 = 443;
/// `BENCH_SCHEME=http` dials this and drops TLS entirely. Rows from the two
/// schemes measure different things and do not belong in one CSV.
const TARGET_PORT_PLAIN: u16 = 80;
/// `lib.rs:795`. `BENCH_PATH` overrides it for a local smoke test against an
/// arbitrary HTTPS host; unset in every real run.
const DEFAULT_TARGET_PATH: &str = "/blob.bin";
/// `lib.rs:891`. Sized to hold pipelined records without reallocating.
const TLS_BUF_CAP: usize = 256 * 1024;
/// `lib.rs:932`. One attempt: Linux's default SYN retry would grind for ~127 s
/// and give the two stacks incomparable failure semantics (§3.5).
const SYN_TIMEOUT: Duration = Duration::from_secs(5);
/// No counterpart in `lib.rs` (which has `ITER_BUDGET`). A connection that
/// stops producing bytes would otherwise hang the run and bill for it.
const STALL_TIMEOUT: Duration = Duration::from_secs(60);
/// `lib.rs:1169`. Fixed, not autotuned — the parity run's SO_RCVBUF matches it.
const RX_BUF_BYTES: usize = 4 * 1024 * 1024;

// Config. Runtime here, build-time on the unikernel (§3.7, §5): the guest has
// neither an environment nor a resolver. Every value is echoed at startup so a
// mismatch between the arms is on the record.

/// IEC suffixes so `AWS_BUCKET_SIZE="10G"` parses. Same acceptance as
/// `lib.rs:317`, so both stacks read `.env` identically: leading digits, then
/// the first non-digit picks the multiplier, and anything else is a default.
fn parse_size(s: Option<String>, default: u64) -> u64 {
    let Some(s) = s else { return default };
    let digits: String = s.chars().take_while(char::is_ascii_digit).collect();
    let Ok(v) = digits.parse::<u64>() else { return default };
    match s[digits.len()..].chars().next() {
        None => v,
        Some('K' | 'k') => v << 10,
        Some('M' | 'm') => v << 20,
        Some('G' | 'g') => v << 30,
        Some('T' | 't') => v << 40,
        Some('B' | 'b' | 'i' | 'I') => v,
        _ => default,
    }
}

fn parse_bool(s: Option<String>, default: bool) -> bool {
    match s.as_deref().and_then(|s| s.bytes().next()) {
        Some(b'1' | b't' | b'T' | b'y' | b'Y') => true,
        Some(b'0' | b'f' | b'F' | b'n' | b'N') => false,
        _ => default,
    }
}

fn env(k: &str) -> Option<String> {
    std::env::var(k).ok().filter(|v| !v.is_empty())
}

struct Config {
    workers: usize,
    conns_per_worker: usize,
    object_size: u64,
    block_size: u64,
    stub: bool,
    /// No TLS, port 80.
    plain: bool,
    target_ip: Ipv4Addr,
    host: String,
    path: String,
    tuning: Tuning,
    mode: String,
}

fn load_config() -> Result<Config, String> {
    let workers = parse_size(env("BENCH_WORKERS"), 8) as usize;
    let conns_per_worker = parse_size(env("BENCH_CONNS_PER_WORKER"), 24) as usize;
    let object_size = parse_size(env("AWS_BUCKET_SIZE"), 10 * 1024 * 1024 * 1024);
    let block_size = parse_size(env("BENCH_BLOCK_SIZE"), 64 * 1024 * 1024);
    let mut stub = parse_bool(env("BENCH_TLS_STUB"), false);
    // Rejected rather than defaulted: a misspelt scheme would silently run TLS
    // and produce a row labelled https, which is a wrong measurement.
    let plain = match env("BENCH_SCHEME").as_deref().unwrap_or("https") {
        "http" => true,
        "https" => false,
        other => return Err(format!("BENCH_SCHEME must be http or https, not {other:?}")),
    };
    if plain && stub {
        // No record layer, so there is no ciphertext to count instead.
        println!("note: BENCH_TLS_STUB ignored under BENCH_SCHEME=http");
        stub = false;
    }

    // BENCH_HOST/BENCH_PATH are for the local smoke test only; unset in every
    // real run, where the endpoint comes from the same `.env` the bench uses.
    let host = match env("BENCH_HOST") {
        Some(h) => h,
        None => {
            let bucket = env("AWS_BUCKET").ok_or("AWS_BUCKET is unset")?;
            let region = env("AWS_REGION").ok_or("AWS_REGION is unset")?;
            format!("{bucket}.s3.{region}.amazonaws.com")
        }
    };
    // Resolved per run by the driver and handed to both stacks (§9.9). No DNS
    // here, which is also what keeps getaddrinfo out of a static glibc.
    let target_ip: Ipv4Addr = env("AWS_TARGET_IP")
        .ok_or("AWS_TARGET_IP is unset")?
        .parse()
        .map_err(|_| "AWS_TARGET_IP is malformed".to_string())?;

    if object_size == 0 || block_size == 0 || conns_per_worker == 0 || workers == 0 {
        return Err("BENCH_WORKERS, BENCH_CONNS_PER_WORKER, BENCH_BLOCK_SIZE and \
                    AWS_BUCKET_SIZE must be nonzero"
            .into());
    }

    let path = env("BENCH_PATH").unwrap_or_else(|| DEFAULT_TARGET_PATH.into());
    let mode = env("MODE").unwrap_or_else(|| "stock".into());
    // The unikernel's core budget, not the machine's: it pins one worker per
    // queue and the device gives it 8 (§7). Both restricted arms take it.
    let pin = Some(parse_size(env("PIN_CORES"), workers as u64) as usize);
    let tuning = match mode.as_str() {
        // Same cores as the unikernel, but every Linux feature kept: jumbo,
        // GRO, delayed ACK, receive autotuning. Resources matched, not
        // functionality removed.
        "capped" => Tuning { pin_cores: pin, ..Tuning::none() },
        "parity" => Tuning {
            pin_cores: pin,
            rcvbuf: Some(parse_size(env("RCVBUF"), RX_BUF_BYTES as u64) as usize),
            busy_poll: Some(parse_size(env("BUSY_POLL"), 50) as u32),
            quickack: parse_bool(env("QUICKACK"), true),
        },
        _ => Tuning::none(),
    };

    Ok(Config { workers, conns_per_worker, object_size, block_size, stub, plain,
                target_ip, host, path, tuning, mode })
}

// Byte-identical to `lib.rs:797-806`, User-Agent included: an honest string
// would be a difference on the wire.

fn build_range_request(host: &str, path: &str, start: u64, end_inclusive: u64) -> Vec<u8> {
    format!(
        "GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: minidpdk-smoltcp/0.1\r\n\
         Range: bytes={start}-{end_inclusive}\r\nConnection: close\r\n\r\n"
    )
    .into_bytes()
}

/// Verbatim from `lib.rs:943-963`. Body bytes only: the HTTP header is
/// application data to TLS, so counting raw decrypted length overcounts by one
/// header per connection. The CRLFCRLF terminator can straddle records.
fn count_body(bytes: &mut u64, headers_done: &mut bool, hdr_state: &mut u8, data: &[u8]) {
    if *headers_done {
        *bytes += data.len() as u64;
        return;
    }
    for (i, &b) in data.iter().enumerate() {
        *hdr_state = match (*hdr_state, b) {
            (0, b'\r') => 1,
            (1, b'\n') => 2,
            (2, b'\r') => 3,
            (3, b'\n') => 4,
            (_, b'\r') => 1,
            _ => 0,
        };
        if *hdr_state == 4 {
            *headers_done = true;
            *bytes += (data.len() - (i + 1)) as u64;
            return;
        }
    }
}

// Same provider, versions and unbuffered API as the bench; only the clock
// differs, because the bench's comes from the OSv shim.

#[derive(Debug)]
struct StdTimeProvider;

impl TimeProvider for StdTimeProvider {
    fn current_time(&self) -> Option<UnixTime> {
        Some(UnixTime::since_unix_epoch(
            SystemTime::now().duration_since(UNIX_EPOCH).ok()?,
        ))
    }
}

fn make_client_config() -> Arc<ClientConfig> {
    let mut roots = RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    let cfg = ClientConfig::builder_with_details(
        Arc::new(rustls_rustcrypto::provider()),
        Arc::new(StdTimeProvider),
    )
    .with_safe_default_protocol_versions()
    .expect("rustls: default protocol versions")
    .with_root_certificates(roots)
    .with_no_client_auth();
    Arc::new(cfg)
}

// The smoltcp side runs 24 of these as state machines under one `iface.poll()`;
// here each is a thread blocking on `read` — the threading-model half of the
// delta (§1).

#[derive(Default)]
struct Outcome {
    worker: usize,
    src_port: u16,
    bytes_received: u64,
    expected: u64,
    closed_cleanly: bool,
    /// Never established. Counted in `conns_total`, never in `conns_clean`,
    /// matching a smoltcp connection stuck in SynSent (§3.5).
    failed: bool,
    setup_ms: u64,
    /// Nanoseconds since the process epoch, so the worker clock can span
    /// "all connects issued" -> "last of the C done" (§3.6).
    finish_ns: u64,
    /// Epoch-relative moment the TLS handshake finished. `lib.rs:1327` takes
    /// the same instant; the max over a worker's connections is a wall clock
    /// that can be subtracted from elapsed, which setup_ms cannot.
    handshake_ns: u64,
}

/// Everything every connection shares. Threads borrow one of these instead of
/// receiving nine cloned arguments each.
struct Job {
    tls: Arc<ClientConfig>,
    server_name: ServerName<'static>,
    host: String,
    path: String,
    target: SocketAddr,
    object_size: u64,
    block_size: u64,
    stub: bool,
    /// `pump_plain` instead of `pump`.
    plain: bool,
    tuning: Tuning,
    epoch: Instant,
}

fn run_conn(job: &Job, worker: usize, block: u64, barrier: &Barrier,
            worker_start_ns: &AtomicU64) -> Outcome {
    let Job { object_size, block_size, stub, tuning, epoch, target, .. } = *job;
    // Blocks wrap within the object, so distinct connections read distinct
    // offsets rather than replaying one hot range. `lib.rs:1218-1225`.
    let stride = object_size.saturating_sub(block_size) + 1;
    let start = if stride == 0 { 0 } else { block.wrapping_mul(block_size) % stride };
    let end = start + block_size - 1;
    let request = build_range_request(&job.host, &job.path, start, end);

    let mut out = Outcome { worker, expected: end - start + 1, ..Outcome::default() };

    if let Some(cores) = tuning.pin_cores {
        if let Err(e) = tune::pin_to(worker % cores) {
            println!("FAIL: worker {worker}: sched_setaffinity: {e}");
        }
    }

    // Constructed before the barrier, because the smoltcp side constructs its
    // sockets and `Conn`s before starting the worker clock (§3.6). None when
    // plain: no state machine to build.
    let tls = (!job.plain)
        .then(|| UnbufferedClientConnection::new(job.tls.clone(), job.server_name.clone()));

    // The worker clock starts here: after setup, before the connects are
    // issued, so it excludes construction but includes SYN, establishment and
    // the TLS handshake — exactly what `lib.rs:1264` measures.
    //
    // Reached on every path, including the failure above: this barrier expects
    // all C of the worker's threads, so one of them returning early would hang
    // the other C-1 for the lifetime of the process.
    if barrier.wait().is_leader() {
        worker_start_ns.store(epoch.elapsed().as_nanos() as u64, Ordering::Relaxed);
    }

    let mut tls = match tls {
        None => None,
        Some(Ok(c)) => Some(c),
        Some(Err(e)) => {
            println!("FAIL: rustls new: {e:?}");
            out.failed = true;
            out.finish_ns = epoch.elapsed().as_nanos() as u64;
            return out;
        }
    };

    let t_connect = Instant::now();
    let sock = match TcpStream::connect_timeout(&target, SYN_TIMEOUT) {
        Ok(s) => s,
        Err(e) => {
            out.setup_ms = t_connect.elapsed().as_millis() as u64;
            // Mirrors `lib.rs:998`, including the wording, so one parser reads
            // both logs.
            println!(
                "FAIL: q{worker} SYN timeout on port ? after {} ms — no SYN-ACK ({e})",
                SYN_TIMEOUT.as_millis()
            );
            out.failed = true;
            out.finish_ns = epoch.elapsed().as_nanos() as u64;
            return out;
        }
    };
    out.setup_ms = t_connect.elapsed().as_millis() as u64;
    out.src_port = sock.local_addr().map(|a| a.port()).unwrap_or(0);

    let fd = sock.as_raw_fd();
    if let Some(bytes) = tuning.rcvbuf {
        if let Err(e) = tune::set_rcvbuf(fd, bytes) {
            println!("FAIL: q{worker}: SO_RCVBUF: {e}");
        }
    }
    if let Some(usec) = tuning.busy_poll {
        if let Err(e) = tune::set_busy_poll(fd, usec) {
            // ❓5: historically CAP_NET_ADMIN-gated. Logged rather than fatal;
            // instance.py sets the sysctl fallback and records which path won.
            println!("FAIL: q{worker}: SO_BUSY_POLL: {e} (falling back to sysctl)");
        }
    }
    let _ = sock.set_read_timeout(Some(STALL_TIMEOUT));

    match tls.as_mut() {
        Some(tls) => pump(&sock, tls, &request, stub, tuning, worker, epoch, &mut out),
        None => pump_plain(&sock, &request, tuning, worker, epoch, &mut out),
    }
    out.finish_ns = epoch.elapsed().as_nanos() as u64;
    out
}

/// `pump` with the record layer removed. Same request bytes, same header
/// strip, same clean-close rule.
fn pump_plain(
    sock: &TcpStream,
    request: &[u8],
    tuning: Tuning,
    worker: usize,
    epoch: Instant,
    out: &mut Outcome,
) {
    let mut buf = vec![0u8; TLS_BUF_CAP];
    let mut sock = sock;
    let mut headers_done = false;
    let mut hdr_state = 0u8;

    if let Err(e) = sock.write_all(request) {
        println!("FAIL: q{worker}: write: {e}");
        return;
    }
    // Stands in for "handshake finished": what TRANSFER excludes as setup.
    out.handshake_ns = epoch.elapsed().as_nanos() as u64;

    loop {
        if tuning.quickack {
            let _ = tune::set_quickack(sock.as_raw_fd());
        }
        match sock.read(&mut buf) {
            Ok(0) => {
                out.closed_cleanly = true;
                return;
            }
            Ok(n) => count_body(
                &mut out.bytes_received,
                &mut headers_done,
                &mut hdr_state,
                &buf[..n],
            ),
            Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => {
                println!("FAIL: q{worker}: read: {e}");   // incl. STALL_TIMEOUT
                return;
            }
        }
    }
}

/// The rustls unbuffered state machine, arm for arm from `lib.rs:1028-1091`.
/// Only the direction of control differs: smoltcp's pump is one step of a
/// rotation, this one owns the loop and blocks on `read` when it needs more.
fn pump(
    sock: &TcpStream,
    tls: &mut UnbufferedClientConnection,
    request: &[u8],
    stub: bool,
    tuning: Tuning,
    worker: usize,
    epoch: Instant,
    out: &mut Outcome,
) {
    let mut incoming: Vec<u8> = Vec::with_capacity(TLS_BUF_CAP);
    let mut outgoing: Vec<u8> = Vec::with_capacity(TLS_BUF_CAP);
    let mut buf = vec![0u8; TLS_BUF_CAP];
    let mut sock = sock;

    let mut handshake_done = false;
    let mut request_queued = false;
    let mut headers_done = false;
    let mut hdr_state = 0u8;
    let mut eof = false;
    let mut failed = false;

    loop {
        // --- advance the TLS state machine over whatever is buffered --------
        let mut progress = true;
        while progress {
            progress = false;
            let UnbufferedStatus { discard, state } = tls.process_tls_records(&mut incoming);
            let st = match state {
                Ok(st) => st,
                Err(e) => {
                    println!("FAIL: tls: {e:?}");
                    failed = true;
                    break;
                }
            };
            match st {
                ConnectionState::ReadTraffic(mut rt) => {
                    // `lib.rs` sets progress unconditionally; it can, because
                    // its outer loop is a poll loop. Ours would spin on an
                    // empty buffer.
                    let mut got_record = false;
                    while let Some(rec) = rt.next_record() {
                        match rec {
                            Ok(rec) => {
                                got_record = true;
                                count_body(
                                    &mut out.bytes_received,
                                    &mut headers_done,
                                    &mut hdr_state,
                                    rec.payload,
                                );
                            }
                            Err(e) => {
                                println!("FAIL: tls record: {e:?}");
                                failed = true;
                                break;
                            }
                        }
                    }
                    progress = got_record;
                }
                ConnectionState::EncodeTlsData(mut et) => {
                    let head = outgoing.len();
                    // `lib.rs` resizes to TLS_BUF_CAP absolute, which would
                    // panic if `head` exceeded it. Same bytes, no latent panic.
                    outgoing.resize(head + TLS_BUF_CAP, 0);
                    match et.encode(&mut outgoing[head..]) {
                        Ok(n) => {
                            outgoing.truncate(head + n);
                            progress = true;
                        }
                        Err(e) => {
                            println!("FAIL: tls encode: {e:?}");
                            failed = true;
                            break;
                        }
                    }
                }
                ConnectionState::TransmitTlsData(tt) => {
                    tt.done();
                    progress = true;
                }
                ConnectionState::WriteTraffic(mut wt) => {
                    if !handshake_done {
                        out.handshake_ns = epoch.elapsed().as_nanos() as u64;
                    }
                    handshake_done = true;
                    if !request_queued {
                        let head = outgoing.len();
                        outgoing.resize(head + request.len() + 128, 0);
                        match wt.encrypt(request, &mut outgoing[head..]) {
                            Ok(n) => {
                                outgoing.truncate(head + n);
                                request_queued = true;
                                progress = true;
                            }
                            Err(e) => {
                                println!("FAIL: tls encrypt: {e:?}");
                                failed = true;
                                break;
                            }
                        }
                    }
                }
                _ => {}
            }
            incoming.drain(..discard);
        }

        // --- flush whatever the state machine produced ----------------------
        if !outgoing.is_empty() {
            if let Err(e) = sock.write_all(&outgoing) {
                println!("FAIL: q{worker}: write: {e}");
                failed = true;
            }
            outgoing.clear();
        }

        if failed {
            break;
        }
        if eof {
            // smoltcp requires closed *and* drained, because a FIN can arrive
            // with data still buffered (`lib.rs:1083-1091`). read() == 0 is the
            // same point: the kernel returns 0 only after delivering the tail.
            out.closed_cleanly = handshake_done && request_queued;
            break;
        }

        // --- wait for more ---------------------------------------------------
        if tuning.quickack {
            // The kernel clears TCP_QUICKACK after each recv, so it is re-armed
            // per read rather than set once (§7).
            let _ = tune::set_quickack(sock.as_raw_fd());
        }
        match sock.read(&mut buf) {
            Ok(0) => eof = true,
            Ok(n) => {
                if stub && handshake_done && request_queued {
                    // Stub mode tallies ciphertext and never touches the
                    // record layer (`lib.rs:1023-1026`), so the count carries
                    // TLS framing and the header — §3.2 on the weaker COMPLETE.
                    out.bytes_received += n as u64;
                } else {
                    incoming.extend_from_slice(&buf[..n]);
                }
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => {
                // Includes STALL_TIMEOUT expiring. Either way the connection
                // is abandoned unclean, which the completeness check catches.
                println!("FAIL: q{worker}: read: {e}");
                break;
            }
        }
    }
}

// Mirrors `osv_app_main` (`lib.rs:1374-1554`) line for line in what it prints:
// scripts/bench/*/bench.py parses these and builds the CSV from them (§4).

fn main() {
    let cfg = match load_config() {
        Ok(c) => c,
        Err(e) => {
            println!("FAIL: {e} — run `just setup competitors/linux-s3`");
            std::process::exit(1);
        }
    };

    let port = if cfg.plain { TARGET_PORT_PLAIN } else { TARGET_PORT };
    println!(
        "bench: {} workers x {} conns x {} MiB block, tls_stub={} scheme={}",
        cfg.workers,
        cfg.conns_per_worker,
        cfg.block_size / (1024 * 1024),
        cfg.stub,
        if cfg.plain { "http" } else { "https" }
    );
    let o = cfg.target_ip.octets();
    println!(
        "target: {}.{}.{}.{}:{} {}",
        o[0], o[1], o[2], o[3], port, cfg.host
    );
    // `q<N>` is the RSS queue on the smoltcp side, only a worker index here.
    // The shape is kept so the logs diff; this line stops it being a claim.
    println!(
        "note: q<N> is the worker index; this stack does not steer by RSS. \
         mode={} pin={:?} rcvbuf={:?} busy_poll={:?} quickack={} cores={}",
        cfg.mode,
        cfg.tuning.pin_cores,
        cfg.tuning.rcvbuf,
        cfg.tuning.busy_poll,
        cfg.tuning.quickack,
        tune::nproc()
    );

    let server_name = match ServerName::try_from(cfg.host.clone()) {
        Ok(n) => n,
        Err(_) => {
            println!("FAIL: invalid ServerName");
            std::process::exit(1);
        }
    };
    let job = Job {
        tls: make_client_config(),
        server_name,
        host: cfg.host.clone(),
        path: cfg.path.clone(),
        target: SocketAddr::new(IpAddr::V4(cfg.target_ip), port),
        object_size: cfg.object_size,
        block_size: cfg.block_size,
        stub: cfg.stub,
        plain: cfg.plain,
        tuning: cfg.tuning,
        epoch: Instant::now(),
    };
    let barriers: Vec<Barrier> =
        (0..cfg.workers).map(|_| Barrier::new(cfg.conns_per_worker)).collect();
    let worker_start: Vec<AtomicU64> =
        (0..cfg.workers).map(|_| AtomicU64::new(0)).collect();

    // The AGGREGATE clock spans spawn -> join of all workers (`lib.rs:1447`).
    // Scoped threads so they can borrow `job` and the barriers rather than each
    // taking its own Arc of everything. No join-error arm: the release profile
    // aborts on panic, so a panicking connection takes the process with it.
    let overall = Instant::now();
    let outcomes: Vec<Outcome> = std::thread::scope(|s| {
        let handles: Vec<_> = (0..cfg.workers)
            .flat_map(|w| (0..cfg.conns_per_worker).map(move |i| (w, i)))
            .map(|(w, i)| {
                // `first_block = worker * CONNS_PER_WORKER` (`lib.rs:1437`).
                let block = (w * cfg.conns_per_worker + i) as u64;
                let (job, barrier, ws) = (&job, &barriers[w], &worker_start[w]);
                s.spawn(move || run_conn(job, w, block, barrier, ws))
            })
            .collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    let overall_s = overall.elapsed().as_secs_f64();

    // Bytes never requested, surfaced rather than left a silent gap
    // (`lib.rs:1290-1295`). Capped: a healthy run prints none, and a run that
    // prints many is already discarded — the summary carries the count.
    const UNCLEAN_SHOWN: usize = 16;
    let unclean: Vec<&Outcome> = outcomes.iter().filter(|c| !c.closed_cleanly).collect();
    for c in unclean.iter().take(UNCLEAN_SHOWN) {
        println!(
            "q{}: conn on port {} did not close cleanly ({} of {} B)",
            c.worker, c.src_port, c.bytes_received, c.expected
        );
    }
    if unclean.len() > UNCLEAN_SHOWN {
        println!(
            "... and {} more unclean connections (not listed)",
            unclean.len() - UNCLEAN_SHOWN
        );
    }

    let total_b: u64 = outcomes.iter().map(|c| c.bytes_received).sum();
    let total_expected: u64 = outcomes.iter().map(|c| c.expected).sum();
    let setup_ms: u64 = outcomes.iter().map(|c| c.setup_ms).sum();
    let conns_total = outcomes.len() as u64;
    let conns_clean = outcomes.iter().filter(|c| c.closed_cleanly).count() as u64;
    let failed = outcomes.iter().filter(|c| c.failed).count() as u64;

    for w in 0..cfg.workers {
        let mine = || outcomes.iter().filter(|c| c.worker == w);
        let b: u64 = mine().map(|c| c.bytes_received).sum();
        let start_ns = worker_start[w].load(Ordering::Relaxed);
        let e = mine().map(|c| c.finish_ns).max().unwrap_or(start_ns)
            .saturating_sub(start_ns) as f64 / 1e9;
        println!("worker {w} (q{w}): {b} B / {e:.3} s  ({:.1} MB/s)",
                 (b as f64 / 1e6) / e.max(1e-9));
    }

    println!();
    println!(
        "AGGREGATE: {:.1} MiB in {:.3} s => {:.1} MB/s, {:.3} Gbps",
        total_b as f64 / (1024.0 * 1024.0),
        overall_s,
        total_b as f64 / 1e6 / overall_s.max(1e-9),
        total_b as f64 * 8.0 / 1e9 / overall_s.max(1e-9)
    );

    // AGGREGATE is what a fetch costs end to end; this is what the stack
    // sustains once connections exist. Not `worker_start`: that barrier sits
    // *before* the connects, so it only covers thread startup and would
    // subtract ~0.01 s instead of the connect and handshake. The last
    // handshake to finish is the same instant `lib.rs:1327` takes, and workers
    // overlap, so max not sum.
    let setup_s = outcomes
        .iter()
        .map(|c| c.handshake_ns)
        .max()
        .unwrap_or(0) as f64
        / 1e9;
    let transfer_s = (overall_s - setup_s).max(1e-9);
    println!(
        "TRANSFER: {:.1} MiB in {:.3} s => {:.1} MB/s, {:.3} Gbps (setup {:.3} s excluded)",
        total_b as f64 / (1024.0 * 1024.0),
        transfer_s,
        total_b as f64 / 1e6 / transfer_s,
        total_b as f64 * 8.0 / 1e9 / transfer_s,
        setup_s
    );

    let ranges_ok = conns_clean == conns_total && conns_total > 0;
    let planned_conns = (cfg.workers * cfg.conns_per_worker) as u64;
    let covered = conns_total == planned_conns;

    println!();
    println!("connections   : {conns_clean}/{conns_total} closed cleanly, {failed} failed");
    // Structurally zero here: the kernel picks source ports and there is no
    // RSS model to be wrong about. Printed so the validity gate is shared (§4).
    println!("syn retries   : 0 (expected 0)");
    println!("misrouted rx  : 0 packets dropped (expected 0)");
    println!("tx drops      : 0 no-mbuf, 0 ring-full (expected 0)");
    println!(
        "setup         : {} ms total, {:.1} ms/conn",
        setup_ms,
        setup_ms as f64 / conns_total.max(1) as f64
    );
    println!(
        "blocks        : {}/{} of {} MiB requested ({} bytes)",
        conns_total,
        planned_conns,
        cfg.block_size / (1024 * 1024),
        total_expected
    );

    if cfg.stub {
        let overhead = total_b as f64 - total_expected as f64;
        println!(
            "bytes         : {} on the wire (ciphertext, {:+.2}%)",
            total_b,
            overhead * 100.0 / total_expected.max(1) as f64
        );
    } else {
        println!("bytes         : {total_b} plaintext body");
    }

    let bytes_ok = if cfg.stub {
        total_b >= total_expected
    } else {
        total_b == total_expected
    };

    if ranges_ok && covered && bytes_ok {
        if cfg.stub {
            println!(
                "COMPLETE: all {conns_total} blocks fetched \
                 (set BENCH_TLS_STUB=0 for a byte-exact check)"
            );
        } else {
            println!("COMPLETE: {total_b} bytes, byte-exact");
        }
    } else {
        println!(
            "INCOMPLETE: {} connections abandoned, {} blocks unrequested, {} bytes {}",
            conns_total - conns_clean,
            planned_conns.saturating_sub(conns_total),
            if total_b > total_expected {
                total_b - total_expected
            } else {
                total_expected - total_b
            },
            if total_b > total_expected { "over" } else { "short" }
        );
        std::process::exit(1);
    }
}
