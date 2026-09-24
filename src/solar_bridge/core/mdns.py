"""Raw mDNS announcer for the Fronius-SE service types the Wattpilot scans for."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import struct
import subprocess

_LOGGER = logging.getLogger(__name__)

MDNS_ADDR = "224.0.0.251"
MDNS_ADDR6 = "ff02::fb"
MDNS_PORT = 5353

FRONIUS_SE_INVERTER_TYPE = "_Fronius-SE-Inverter._tcp.local."
FRONIUS_SE_METER_TYPE = "_Fronius-SE-SmartMeter._tcp.local."


# DNS record types
_TYPE_A = 1
_TYPE_AAAA = 28
_TYPE_PTR = 12
_TYPE_TXT = 16
_TYPE_SRV = 33

# DNS classes
_CLASS_IN = 0x0001
_CLASS_IN_FLUSH = 0x8001  # cache-flush bit

# mDNS response flags: QR=1, Opcode=0, AA=1
_FLAGS_RESPONSE = 0x8400


def _get_local_ip() -> str:
    """Get the local IP address of this machine."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _get_link_local_ipv6(iface: str) -> bytes | None:
    """Return the 16-byte packed link-local IPv6 address for the given interface."""
    try:
        result = subprocess.run(
            ["ip", "-6", "addr", "show", "dev", iface, "scope", "link"],
            capture_output=True, text=True, timeout=2,
        )
        match = re.search(r"inet6\s+(fe80[^/\s]+)", result.stdout)
        if match:
            addr = match.group(1).split("%")[0]
            return socket.inet_pton(socket.AF_INET6, addr)
    except Exception:
        pass
    # Fallback: getaddrinfo
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
            addr = info[4][0]
            if addr.lower().startswith("fe80"):
                return socket.inet_pton(socket.AF_INET6, addr.split("%")[0])
    except Exception:
        pass
    return None


def _get_multicast_iface() -> str:
    """Return the interface name used to reach the mDNS multicast group."""
    try:
        result = subprocess.run(
            ["ip", "route", "get", "224.0.0.251"],
            capture_output=True, text=True, timeout=2,
        )
        match = re.search(r"\bdev\s+(\S+)", result.stdout)
        return match.group(1) if match else "eth0"
    except Exception:
        return "eth0"


# ── DNS packet helpers ────────────────────────────────────────────────────────

def _encode_name(name: str) -> bytes:
    """Encode a DNS name (e.g. 'foo._tcp.local.') to wire format."""
    result = b""
    for label in name.rstrip(".").split("."):
        enc = label.encode()
        result += bytes([len(enc)]) + enc
    return result + b"\x00"


def _rr(name: bytes, rtype: int, rclass: int, ttl: int, rdata: bytes) -> bytes:
    """Build a single DNS resource record."""
    return name + struct.pack(">HHIH", rtype, rclass, ttl, len(rdata)) + rdata


def _txt_rdata(entries: list[str]) -> bytes:
    """Encode TXT RDATA: each string is length-prefixed."""
    result = b""
    for entry in entries:
        enc = entry.encode()
        result += bytes([len(enc)]) + enc
    return result


def _build_packet(
    instance_name: str,
    service_type: str,
    hostname: str,
    port: int,
    ip_bytes: bytes,
    txt: bytes,
    *,
    use_ipv6: bool = False,
    ip6_bytes: bytes | None = None,
) -> bytes:
    """Build a complete mDNS announcement packet for one service type.

    PTR in answers (ANCOUNT=1), SRV+TXT+A/AAAA in additionals (ARCOUNT=3).
    When use_ipv6=True and ip6_bytes is provided, the A record is replaced
    with an AAAA record containing the link-local IPv6 address.
    """
    svc = _encode_name(service_type)
    inst = _encode_name(f"{instance_name}.{service_type}")
    host = _encode_name(hostname)

    ptr = _rr(svc, _TYPE_PTR, _CLASS_IN, 4500, inst)
    srv = _rr(inst, _TYPE_SRV, _CLASS_IN_FLUSH, 120, struct.pack(">HHH", 0, 0, port) + host)
    txt_rr = _rr(inst, _TYPE_TXT, _CLASS_IN_FLUSH, 4500, txt)

    if use_ipv6 and ip6_bytes is not None:
        addr_rr = _rr(host, _TYPE_AAAA, _CLASS_IN_FLUSH, 120, ip6_bytes)
    else:
        addr_rr = _rr(host, _TYPE_A, _CLASS_IN_FLUSH, 120, ip_bytes)

    # ID=0, flags, QDCOUNT=0, ANCOUNT=1, NSCOUNT=0, ARCOUNT=3
    header = struct.pack(">HHHHHH", 0, _FLAGS_RESPONSE, 0, 1, 0, 3)
    return header + ptr + srv + txt_rr + addr_rr


# ── Raw mDNS announcer (send-only, source port 5353) ─────────────────────────

class RawMDNSAnnouncer:
    """Announces Fronius-SE service types via raw UDP multicast on IPv4 and IPv6.

    Bypasses zeroconf's 15-byte label limit by building DNS packets directly.
    Sends from UDP port 5353 (the Wattpilot drops mDNS responses from any other
    source port), shared with HA's zeroconf via SO_REUSEPORT. Send-only: the
    announcement is repeated every second instead of answering queries.
    """

    def __init__(self, name: str, port: int, serial: str, system_name: str | None = None) -> None:
        self._name = name
        self._port = port
        self._serial = serial
        self._system_name = system_name or name
        self._sock: socket.socket | None = None
        self._sock6: socket.socket | None = None
        self._iface: str = "eth0"
        self._packets: list[bytes] = []    # IPv4 packets (A record)
        self._packets6: list[bytes] = []   # IPv6 packets (AAAA record)
        self._task: asyncio.Task | None = None

    def _make_txt(self) -> bytes:
        """Build TXT RDATA matching the Fronius device mDNS format.

        Two TXT strings:
          FSED-DID=V 1|P JSON|PFC 2
          00=<DeviceMeta JSON>   (fits in a single chunk at this size)
        Each string is length-prefixed: len(key=value) + key=value.
        """
        info = json.dumps(
            {
                "DeviceMeta": {
                    "Device-Information": {
                        "Systemname": self._system_name,
                        "CommonName": f"pilot-0.5e-{self._system_name}",
                        "UniqueID": f"240.{self._system_name}",
                        "DeviceSerialNumber": self._serial,
                        "Manufacturer": "Fronius",
                        "SoftwareBundleVersion": "3.4.0-102",
                    },
                    "ZeroconfMetaVersion": "1.0",
                },
            },
            separators=(",", ":"),
        )
        chunk = 250  # value bytes per TXT string; "00=" prefix = 253 total, within 255 limit
        entries = ["FSED-DID=V 1|P JSON|PFC 2"]
        for i, start in enumerate(range(0, len(info), chunk)):
            entries.append(f"{i:02d}={info[start : start + chunk]}")
        return _txt_rdata(entries)

    async def async_start(self) -> None:
        """Create send-only multicast sockets (IPv4 + IPv6) and start announcing."""
        local_ip = _get_local_ip()
        ip_bytes = socket.inet_aton(local_ip)
        hostname = f"{self._name}.local."
        txt = self._make_txt()

        # Determine outbound interface and link-local IPv6 address
        loop = asyncio.get_running_loop()
        self._iface = await loop.run_in_executor(None, _get_multicast_iface)
        ip6_bytes = await loop.run_in_executor(None, _get_link_local_ipv6, self._iface)

        # ── Build per-protocol packet lists ───────────────────────────────
        self._packets = [
            _build_packet(self._name, svc, hostname, self._port, ip_bytes, txt)
            for svc in (FRONIUS_SE_INVERTER_TYPE, FRONIUS_SE_METER_TYPE)
        ]
        if ip6_bytes is not None:
            self._packets6 = [
                _build_packet(
                    self._name, svc, hostname, self._port, ip_bytes, txt,
                    use_ipv6=True, ip6_bytes=ip6_bytes,
                )
                for svc in (FRONIUS_SE_INVERTER_TYPE, FRONIUS_SE_METER_TYPE)
            ]
            _LOGGER.info(
                "RawMDNSAnnouncer: link-local IPv6 = %s (iface=%s)",
                socket.inet_ntop(socket.AF_INET6, ip6_bytes),
                self._iface,
            )
        else:
            self._packets6 = self._packets  # fallback: send IPv4 packets on IPv6 socket
            _LOGGER.info(
                "RawMDNSAnnouncer: no link-local IPv6 found for iface=%s, AAAA fallback to A",
                self._iface,
            )

        # ── IPv4 socket ───────────────────────────────────────────────────
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
            sock.bind((local_ip, MDNS_PORT))
        except OSError:
            sock.close()
            raise
        self._sock = sock

        # ── IPv6 socket ───────────────────────────────────────────────────
        sock6 = None
        try:
            sock6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
            sock6.bind(("::", MDNS_PORT))
            self._sock6 = sock6
            _LOGGER.info(
                "RawMDNSAnnouncer IPv6 bound on [::]:5353, iface=%s", self._iface
            )
        except Exception as err:
            _LOGGER.warning("RawMDNSAnnouncer IPv6 socket failed (non-fatal): %s", err)
            if sock6 is not None:
                sock6.close()
            self._sock6 = None

        self._task = asyncio.create_task(self._announce_loop())
        _LOGGER.info(
            "RawMDNSAnnouncer IPv4 bound to %s:5353, iface=%s, sending to 224.0.0.251:5353",
            local_ip, self._iface,
        )
        _LOGGER.info(
            "Raw mDNS: announcing '%s' at %s:%d for %s and %s",
            self._name,
            local_ip,
            self._port,
            FRONIUS_SE_INVERTER_TYPE,
            FRONIUS_SE_METER_TYPE,
        )

    async def _announce_loop(self) -> None:
        iface_idx = 0
        try:
            iface_idx = socket.if_nametoindex(self._iface)
        except Exception:
            pass

        while True:
            try:
                if self._sock is not None:
                    for pkt in self._packets:
                        try:
                            self._sock.sendto(pkt, (MDNS_ADDR, MDNS_PORT))
                        except Exception as err:
                            _LOGGER.debug("IPv4 mDNS send error: %s", err)
                if self._sock6 is not None:
                    for pkt in self._packets6:
                        try:
                            self._sock6.sendto(pkt, (MDNS_ADDR6, MDNS_PORT, 0, iface_idx))
                        except Exception as err:
                            _LOGGER.debug("IPv6 mDNS send error: %s", err)
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.warning("mDNS announce error (will retry): %s", e)
                await asyncio.sleep(5)

    async def async_stop(self) -> None:
        """Cancel the announce loop and close sockets."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._sock:
            self._sock.close()
            self._sock = None
        if self._sock6:
            self._sock6.close()
            self._sock6 = None
        _LOGGER.info("Raw mDNS announcer stopped")
