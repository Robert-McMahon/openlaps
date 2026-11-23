//! `openlaps-link`: one QUIC connection between the pit and the car.
//!
//! The car's telemetry reaches the pit two ways. The *stream* is a NATS
//! leafnode connection: ordered, gap-free, and as late as the radio is slow.
//! The *live link* is small UDP frames of whatever just changed, for gauges
//! (docs/WIRE_FORMAT.md -> Live link). Sent as two separate flows they share
//! the radio's queue, and a stream that is busy catching up fills that queue
//! ahead of the live frames, which then arrive seconds late.
//!
//! This program puts both inside one QUIC connection, so one sender decides
//! what goes first. Live frames travel as QUIC datagrams: unreliable, sent
//! ahead of everything else, dropped rather than queued when they cannot
//! go. The leafnode's TCP connection is carried in a QUIC stream that takes
//! whatever capacity is left. Neither NATS nor the live link knows: each
//! talks to a local port.
//!
//! ```text
//!   pit                                                   car
//!   NATS leaf --tcp--> [link pit] ==== QUIC ==== [link car] --tcp--> NATS :7422
//!   live-decoder -udp->     "      (datagrams)        "     --udp--> agent :7423
//! ```
//!
//! The pit dials, as it does for the leafnode itself, and authenticates with
//! the same certificates (mutual TLS against the deployment's CA).

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use bytes::Bytes;
use quinn::crypto::rustls::{QuicClientConfig, QuicServerConfig};
use quinn::{Connection, Endpoint, TransportConfig, VarInt};
use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use tokio::net::{TcpListener, TcpStream, UdpSocket};

mod pacing;

const ALPN: &[u8] = b"openlaps-link/1";
/// How long a silent path is trusted. The pit redials after this.
const IDLE_TIMEOUT: Duration = Duration::from_secs(8);
const KEEP_ALIVE: Duration = Duration::from_secs(2);
/// Live frames waiting to be sent. Small on purpose: when the link cannot
/// take them, the oldest are dropped, and a newer frame supersedes them.
const DATAGRAM_SEND_BUFFER: usize = 4 * 1024;
const STATS_EVERY: Duration = Duration::from_secs(30);
const STREAM_MARKER: u8 = 1;

struct Args {
    mode: String,
    cert: PathBuf,
    key: PathBuf,
    ca: PathBuf,
    /// car: QUIC listen address. pit: unused.
    listen: SocketAddr,
    /// pit: the car's QUIC addresses, tried together; first to answer wins.
    car: Vec<String>,
    /// pit: the name the car's certificate must carry.
    server_name: String,
    /// car: where the leafnode listener is. pit: where NATS dials us.
    tcp: SocketAddr,
    /// car: the agent's live port. pit: where the live-decoder sends HELLOs.
    udp: SocketAddr,
    /// Queueing delay the stream may add to the path before it backs off.
    target_delay: Duration,
}

fn parse_args() -> Result<Args> {
    let mut argv = std::env::args().skip(1);
    let mode = argv
        .next()
        .ok_or_else(|| anyhow!("usage: openlaps-link car|pit [--option value]..."))?;
    let car_side = match mode.as_str() {
        "car" => true,
        "pit" => false,
        other => bail!("unknown mode {other:?}: expected car or pit"),
    };
    let mut args = Args {
        mode,
        cert: PathBuf::new(),
        key: PathBuf::new(),
        ca: PathBuf::new(),
        listen: "0.0.0.0:7424".parse()?,
        car: Vec::new(),
        server_name: String::new(),
        tcp: if car_side {
            "127.0.0.1:7422"
        } else {
            "0.0.0.0:7422"
        }
        .parse()?,
        udp: if car_side {
            "127.0.0.1:7423"
        } else {
            "0.0.0.0:7423"
        }
        .parse()?,
        target_delay: Duration::from_millis(150),
    };
    while let Some(flag) = argv.next() {
        // Both `--option value` and `--option=value`.
        let (flag, value) = match flag.split_once('=') {
            Some((flag, value)) => (flag.to_string(), value.to_string()),
            None => {
                let value = argv.next().ok_or_else(|| anyhow!("{flag} needs a value"))?;
                (flag, value)
            }
        };
        match flag.as_str() {
            "--cert" => args.cert = value.into(),
            "--key" => args.key = value.into(),
            "--ca" => args.ca = value.into(),
            "--listen" => args.listen = value.parse().context("--listen")?,
            "--car" => args.car.extend(
                value
                    .split(',')
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty()),
            ),
            "--server-name" => args.server_name = value,
            "--tcp" => args.tcp = value.parse().context("--tcp")?,
            "--udp" => args.udp = value.parse().context("--udp")?,
            "--target-delay-ms" => {
                args.target_delay =
                    Duration::from_millis(value.parse().context("--target-delay-ms")?)
            }
            other => bail!("unknown option {other}"),
        }
    }
    for (name, path) in [
        ("--cert", &args.cert),
        ("--key", &args.key),
        ("--ca", &args.ca),
    ] {
        if path.as_os_str().is_empty() {
            bail!("{name} is required");
        }
    }
    if !car_side && (args.car.is_empty() || args.server_name.is_empty()) {
        bail!("pit mode needs --car host:port[,host:port] and --server-name");
    }
    Ok(args)
}

fn load_certs(path: &PathBuf) -> Result<Vec<CertificateDer<'static>>> {
    let mut reader = std::io::BufReader::new(
        std::fs::File::open(path).with_context(|| format!("{}", path.display()))?,
    );
    let certs = rustls_pemfile::certs(&mut reader).collect::<std::result::Result<Vec<_>, _>>()?;
    if certs.is_empty() {
        bail!("{}: no certificate found", path.display());
    }
    Ok(certs)
}

fn load_key(path: &PathBuf) -> Result<PrivateKeyDer<'static>> {
    let mut reader = std::io::BufReader::new(
        std::fs::File::open(path).with_context(|| format!("{}", path.display()))?,
    );
    rustls_pemfile::private_key(&mut reader)?
        .ok_or_else(|| anyhow!("{}: no private key found", path.display()))
}

fn roots(path: &PathBuf) -> Result<Arc<rustls::RootCertStore>> {
    let mut store = rustls::RootCertStore::empty();
    for cert in load_certs(path)? {
        store.add(cert)?;
    }
    Ok(Arc::new(store))
}

fn transport(args: &Args) -> Result<Arc<TransportConfig>> {
    let mut config = TransportConfig::default();
    config.max_idle_timeout(Some(IDLE_TIMEOUT.try_into()?));
    config.keep_alive_interval(Some(KEEP_ALIVE));
    config.datagram_send_buffer_size(DATAGRAM_SEND_BUFFER);
    config.datagram_receive_buffer_size(Some(64 * 1024));
    // The whole point: the stream must not build a queue on the radio that
    // live frames then wait behind. See pacing.rs.
    config.congestion_controller_factory(Arc::new(pacing::LowDelayConfig::new(args.target_delay)));
    Ok(Arc::new(config))
}

static DATAGRAMS_OUT: AtomicU64 = AtomicU64::new(0);
static DATAGRAMS_IN: AtomicU64 = AtomicU64::new(0);
static DATAGRAMS_REFUSED: AtomicU64 = AtomicU64::new(0);

fn send_datagram(conn: &Connection, data: &[u8]) {
    match conn.send_datagram(Bytes::copy_from_slice(data)) {
        Ok(()) => {
            DATAGRAMS_OUT.fetch_add(1, Ordering::Relaxed);
        }
        // Too large for the path, or the peer has gone: a live frame is
        // never worth more than dropping it.
        Err(_) => {
            DATAGRAMS_REFUSED.fetch_add(1, Ordering::Relaxed);
        }
    }
}

async fn report(conn: Connection, label: &'static str) {
    let mut every = tokio::time::interval(STATS_EVERY);
    every.tick().await;
    loop {
        every.tick().await;
        let stats = conn.stats();
        println!(
            "{label}: rtt {:.0} ms, window {} B, {} packets lost of {}, stream {} kB out {} kB in, live {} out {} in {} refused",
            stats.path.rtt.as_secs_f64() * 1000.0,
            stats.path.cwnd,
            stats.path.lost_packets,
            stats.path.sent_packets,
            stats.udp_tx.bytes / 1000,
            stats.udp_rx.bytes / 1000,
            DATAGRAMS_OUT.load(Ordering::Relaxed),
            DATAGRAMS_IN.load(Ordering::Relaxed),
            DATAGRAMS_REFUSED.load(Ordering::Relaxed),
        );
    }
}

/// Join one QUIC stream to one TCP connection until either side ends.
async fn splice(send: quinn::SendStream, recv: quinn::RecvStream, mut tcp: TcpStream) {
    let _ = tcp.set_nodelay(true);
    let mut quic = tokio::io::join(recv, send);
    let _ = tokio::io::copy_bidirectional(&mut tcp, &mut quic).await;
}

// -- car ---------------------------------------------------------------------

async fn run_car(args: Args) -> Result<()> {
    let verifier = rustls::server::WebPkiClientVerifier::builder(roots(&args.ca)?).build()?;
    let mut tls = rustls::ServerConfig::builder()
        .with_client_cert_verifier(verifier)
        .with_single_cert(load_certs(&args.cert)?, load_key(&args.key)?)?;
    tls.alpn_protocols = vec![ALPN.to_vec()];
    let mut server = quinn::ServerConfig::with_crypto(Arc::new(QuicServerConfig::try_from(tls)?));
    server.transport_config(transport(&args)?);
    let endpoint = Endpoint::server(server, args.listen)?;
    println!(
        "car: listening on udp {}; stream to {}, live to {}",
        args.listen, args.tcp, args.udp
    );

    while let Some(incoming) = endpoint.accept().await {
        let (tcp, udp) = (args.tcp, args.udp);
        tokio::spawn(async move {
            let conn = match incoming.await {
                Ok(conn) => conn,
                Err(err) => {
                    println!("car: handshake failed: {err}");
                    return;
                }
            };
            println!("car: pit connected from {}", conn.remote_address());
            let reporter = tokio::spawn(report(conn.clone(), "car"));
            let reason = serve_car(&conn, tcp, udp).await;
            reporter.abort();
            println!("car: pit at {} gone: {reason}", conn.remote_address());
        });
    }
    Ok(())
}

async fn serve_car(conn: &Connection, tcp: SocketAddr, udp_target: SocketAddr) -> String {
    // One local socket per pit connection: the agent answers HELLOs to the
    // address they came from, which is this socket.
    let udp = match UdpSocket::bind("127.0.0.1:0").await {
        Ok(socket) => socket,
        Err(err) => return format!("cannot open a local socket: {err}"),
    };
    let mut buf = vec![0u8; 2048];
    loop {
        tokio::select! {
            stream = conn.accept_bi() => match stream {
                Ok((send, mut recv)) => {
                    tokio::spawn(async move {
                        // Swallow the opener's marker byte (see serve_pit).
                        let mut marker = [0u8; 1];
                        if recv.read_exact(&mut marker).await.is_err() {
                            return;
                        }
                        match TcpStream::connect(tcp).await {
                            Ok(local) => splice(send, recv, local).await,
                            Err(err) => println!("car: cannot reach {tcp}: {err}"),
                        }
                    });
                }
                Err(err) => return err.to_string(),
            },
            datagram = conn.read_datagram() => match datagram {
                Ok(data) => {
                    DATAGRAMS_IN.fetch_add(1, Ordering::Relaxed);
                    let _ = udp.send_to(&data, udp_target).await;
                }
                Err(err) => return err.to_string(),
            },
            received = udp.recv_from(&mut buf) => {
                if let Ok((len, _)) = received {
                    send_datagram(conn, &buf[..len]);
                }
            }
        }
    }
}

// -- pit ---------------------------------------------------------------------

async fn dial(endpoint: &Endpoint, args: &Args) -> Result<Connection> {
    // Every address at once: whichever path answers first is the one in use.
    let mut attempts = tokio::task::JoinSet::new();
    for target in &args.car {
        let (endpoint, target, name) = (endpoint.clone(), target.clone(), args.server_name.clone());
        attempts.spawn(async move {
            let addr = tokio::net::lookup_host(&target)
                .await?
                .find(SocketAddr::is_ipv4)
                .ok_or_else(|| anyhow!("{target}: no IPv4 address"))?;
            let conn = tokio::time::timeout(Duration::from_secs(5), endpoint.connect(addr, &name)?)
                .await
                .map_err(|_| anyhow!("{target}: no answer"))??;
            Ok::<_, anyhow::Error>(conn)
        });
    }
    let mut last = anyhow!("no car address configured");
    while let Some(joined) = attempts.join_next().await {
        match joined? {
            Ok(conn) => return Ok(conn),
            Err(err) => last = err,
        }
    }
    Err(last)
}

/// What the pit's local ports are joined to right now.
type Tunnel = tokio::sync::watch::Receiver<Option<Connection>>;

async fn run_pit(args: Args) -> Result<()> {
    let mut tls = rustls::ClientConfig::builder()
        .with_root_certificates(roots(&args.ca)?)
        .with_client_auth_cert(load_certs(&args.cert)?, load_key(&args.key)?)?;
    tls.alpn_protocols = vec![ALPN.to_vec()];
    let mut client = quinn::ClientConfig::new(Arc::new(QuicClientConfig::try_from(tls)?));
    client.transport_config(transport(&args)?);
    let mut endpoint = Endpoint::client("0.0.0.0:0".parse()?)?;
    endpoint.set_default_client_config(client);

    let listener = TcpListener::bind(args.tcp)
        .await
        .with_context(|| format!("tcp {}", args.tcp))?;
    let udp = Arc::new(
        UdpSocket::bind(args.udp)
            .await
            .with_context(|| format!("udp {}", args.udp))?,
    );
    println!(
        "pit: stream on tcp {}, live on udp {}, car at {:?}",
        args.tcp, args.udp, args.car
    );

    let (tunnel_tx, tunnel) = tokio::sync::watch::channel(None::<Connection>);
    let live_peer = Arc::new(std::sync::Mutex::new(None::<SocketAddr>));
    tokio::spawn(accept_streams(listener, tunnel.clone()));
    tokio::spawn(forward_live(udp.clone(), tunnel.clone(), live_peer.clone()));
    tokio::spawn(watch_network());

    let mut failures = 0u32;
    loop {
        let conn = match dial(&endpoint, &args).await {
            Ok(conn) => conn,
            Err(err) => {
                if failures == 0 {
                    println!("pit: cannot reach the car's tunnel: {err}");
                }
                failures += 1;
                tokio::time::sleep(Duration::from_secs(1)).await;
                continue;
            }
        };
        failures = 0;
        println!("pit: connected to the car at {}", conn.remote_address());
        let _ = tunnel_tx.send(Some(conn.clone()));
        let reporter = tokio::spawn(report(conn.clone(), "pit"));
        let reason = loop {
            match conn.read_datagram().await {
                Ok(data) => {
                    DATAGRAMS_IN.fetch_add(1, Ordering::Relaxed);
                    let peer = *live_peer.lock().unwrap();
                    if let Some(peer) = peer {
                        let _ = udp.send_to(&data, peer).await;
                    }
                }
                Err(err) => break err.to_string(),
            }
        };
        let _ = tunnel_tx.send(None);
        reporter.abort();
        conn.close(VarInt::from_u32(0), b"");
        println!("pit: car connection lost: {reason}");
    }
}

/// Every TCP connection to the local stream port is carried in the tunnel.
/// While there is no tunnel the connection is closed at once: there is no
/// other way to the car from here, and NATS redials every second, so it is
/// in the tunnel within a second of there being one. That also means it
/// does not matter which of NATS and this program started first.
async fn accept_streams(listener: TcpListener, tunnel: Tunnel) {
    loop {
        let Ok((local, _)) = listener.accept().await else {
            tokio::time::sleep(Duration::from_millis(100)).await;
            continue;
        };
        let Some(conn) = tunnel.borrow().clone() else {
            continue; // dropping `local` closes it
        };
        tokio::spawn(async move {
            if let Ok((mut send, recv)) = conn.open_bi().await {
                // A QUIC stream does not exist for the far end until
                // something is written to it, and on a leafnode connection
                // the car speaks first.
                if send.write_all(&[STREAM_MARKER]).await.is_ok() {
                    splice(send, recv, local).await;
                }
            }
        });
    }
}

/// HELLOs from the live-decoder go to the car through whatever tunnel there
/// is; with none they are dropped, and the live-decoder turns to its backup
/// address by itself.
async fn forward_live(
    udp: Arc<UdpSocket>,
    tunnel: Tunnel,
    live_peer: Arc<std::sync::Mutex<Option<SocketAddr>>>,
) {
    let mut buf = vec![0u8; 2048];
    loop {
        let Ok((len, peer)) = udp.recv_from(&mut buf).await else {
            tokio::time::sleep(Duration::from_millis(100)).await;
            continue;
        };
        *live_peer.lock().unwrap() = Some(peer);
        let conn = tunnel.borrow().clone();
        if let Some(conn) = conn {
            send_datagram(&conn, &buf[..len]);
        }
    }
}

/// In the pit stack this program shares the NATS container's network. If
/// that container is restarted its network goes away and this process is
/// left holding sockets on a dead one, connected to nothing for ever. Exit
/// instead, so the restart policy brings it back on the new network.
async fn watch_network() {
    let mut missing = 0;
    loop {
        tokio::time::sleep(Duration::from_secs(5)).await;
        let has_interface = std::fs::read_dir("/sys/class/net")
            .map(|entries| entries.flatten().any(|entry| entry.file_name() != "lo"))
            .unwrap_or(true);
        missing = if has_interface { 0 } else { missing + 1 };
        if missing >= 2 {
            println!("pit: no network interface left; exiting to be restarted");
            std::process::exit(1);
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    rustls::crypto::ring::default_provider()
        .install_default()
        .map_err(|_| anyhow!("cannot install the TLS provider"))?;
    let args = parse_args()?;
    let run = async {
        if args.mode == "car" {
            run_car(args).await
        } else {
            run_pit(args).await
        }
    };
    tokio::select! {
        result = run => result,
        _ = tokio::signal::ctrl_c() => Ok(()),
    }
}
