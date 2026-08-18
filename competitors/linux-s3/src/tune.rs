use std::io;
use std::os::unix::io::RawFd;

#[derive(Clone, Copy, Debug)]
pub struct Tuning {
    pub pin_cores: Option<usize>,
    /// `SO_RCVBUF`. Also disables receive-window autotuning, which is the
    /// point: smoltcp has a fixed 4 MiB ring and none to disable.
    pub rcvbuf: Option<usize>,
    /// `SO_BUSY_POLL`, in microseconds.
    pub busy_poll: Option<u32>,
    /// Re-armed before every read: the kernel clears it after each recv.
    pub quickack: bool,
}

impl Tuning {
    pub fn none() -> Self {
        Self { pin_cores: None, rcvbuf: None, busy_poll: None, quickack: false }
    }
}

pub fn nproc() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
}

/// Errors are returned, not ignored: a parity run that silently failed to pin
/// is a wrong measurement, not a slow one.
pub fn pin_to(core: usize) -> io::Result<()> {
    unsafe {
        let mut set: libc::cpu_set_t = std::mem::zeroed();
        libc::CPU_ZERO(&mut set);
        libc::CPU_SET(core, &mut set);
        if libc::sched_setaffinity(0, std::mem::size_of::<libc::cpu_set_t>(), &set) != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

fn setsockopt_i32(fd: RawFd, level: i32, name: i32, value: i32) -> io::Result<()> {
    let rc = unsafe {
        libc::setsockopt(
            fd,
            level,
            name,
            &value as *const i32 as *const libc::c_void,
            std::mem::size_of::<i32>() as libc::socklen_t,
        )
    };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// The kernel doubles what you ask for, so reading it back proves nothing.
pub fn set_rcvbuf(fd: RawFd, bytes: usize) -> io::Result<()> {
    setsockopt_i32(fd, libc::SOL_SOCKET, libc::SO_RCVBUF, bytes as i32)
}

/// Historically `CAP_NET_ADMIN`-gated (❓5); `instance.py` sets the
/// `net.core.busy_poll` sysctl as a fallback.
pub fn set_busy_poll(fd: RawFd, usec: u32) -> io::Result<()> {
    setsockopt_i32(fd, libc::SOL_SOCKET, libc::SO_BUSY_POLL, usec as i32)
}

/// Matches `sock.set_ack_delay(None)` on the smoltcp side.
pub fn set_quickack(fd: RawFd) -> io::Result<()> {
    setsockopt_i32(fd, libc::IPPROTO_TCP, libc::TCP_QUICKACK, 1)
}
