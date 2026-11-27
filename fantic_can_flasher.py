#!/usr/bin/env python3
"""
Fantic e-Shock Gateway Module CAN UDS Firmware Flasher
Communicates directly with the Gateway module via SocketCAN (e.g. can0).

Works both:
- In-vehicle via the 6-pin Euro 5/5+ motorcycle OBD connector (detects live bus & wakeup traffic)
- On the bench via the 4-pin module connector (automatically sends wakeup & keep-alive frames)

Safe workflow:
0. Validate ESP32-C3 binary header & size (<= 1 MB)
1. Passive CAN bus sniff -> Detect in-vehicle harness vs. bench & start Wakeup/Keep-Alive if needed
2. Read all Gateway module info (FW, Serial, HW-Rev, Profile, Partition, DTCs, EOL) & Pre-Flight Check
3. User confirmation before touching flash (skippable via --yes, or dry-run via --check-only)
4. Backup Serial (0xF18C), HW-Rev (0xF193) & EOL (0xFD00) + Pre-flash Clear DTCs (0x14 FF FF FF)
5. Programming Session (0x10 0x02) + Security Access Level 1 (0x27 0x35/0x36)
6. RoutineControl Start OTA (0x31 0x01 0xFD 0x00 0x01 0x00) with automatic recovery reset retry
7. RequestDownload (0x34) -> Address 0x00010000, dynamic maxBlockSize
8. TransferData loop (0x36 <seq> <block>) with ISO-TP FlowControl & NRC 0x78 handling
9. RequestTransferExit (0x37) + RoutineControl Finalize OTA (0x31 0x01 0xFD 0x02 0x01 0x00)
10. ECUReset (0x11 0x01) -> Verify active boot partition (0xFD0B) & heartbeat (0x6F0)
11. Post-flash Serial/NVS verification & automatic restore (0x2E 0xFD00 via Level 2) + Post-flash DTC Clear
"""

import argparse
import errno
import hashlib
import os
import select
import sys
import time
import struct
import socket
import threading

CAN_INTERFACE = "can0"
CAN_ID_REQ = 0x780
CAN_ID_RESP = 0x785
CAN_ID_HEARTBEAT = 0x6F0

# Known TCAN1146 Selective-Wake CAN IDs across Fantic platforms
WAKEUP_IDS = {
    0x260: "Caballero 125/500 (Profile 4/5/6)",
    0x310: "XMF / XEF 125 (Profile 1)",
    0x2F2: "Electric / Issimo (Profile 11/12)",
}

PROFILE_INFO = {
    1: ("XMF 125 / XEF 125", 0x310),
    4: ("Caballero 500", 0x260),
    5: ("Caballero 125", 0x260),
    6: ("Combustion Reserved", 0x260),
    11: ("Electric / Issimo (11)", 0x2F2),
    12: ("Electric / Issimo (12)", 0x2F2),
}

PARTITION_NAMES = {
    0: "factory (0)",
    1: "factory (1)",
    2: "ota_0 (2)",
    3: "ota_1 (3)",
}

POLY_LEVEL1 = 0x35564443
POLY_LEVEL2 = 0xE5ACAAE5
DEFAULT_BLOCK_SIZE = 128
MIN_FLASH_VOLTAGE_V = 12.0
MAX_FLASH_VOLTAGE_V = 15.5
MAX_PARTITION_SIZE = 1048576  # 1 MB (factory / ota_0 / ota_1)


class C:
    """Zero-dependency ANSI terminal colors (auto-disabled for non-TTY / NO_COLOR / --no-color)."""

    RESET = ""
    BOLD = ""
    DIM = ""
    RED = ""
    GREEN = ""
    YELLOW = ""
    BLUE = ""
    CYAN = ""

    @classmethod
    def configure(cls, enabled: bool) -> None:
        if enabled:
            cls.RESET = "\033[0m"
            cls.BOLD = "\033[1m"
            cls.DIM = "\033[2m"
            cls.RED = "\033[31m"
            cls.GREEN = "\033[32m"
            cls.YELLOW = "\033[33m"
            cls.BLUE = "\033[34m"
            cls.CYAN = "\033[36m"
        else:
            cls.RESET = cls.BOLD = cls.DIM = ""
            cls.RED = cls.GREEN = cls.YELLOW = cls.BLUE = cls.CYAN = ""

    @classmethod
    def ok(cls, text: str) -> str:
        return f"{cls.GREEN}{cls.BOLD}{text}{cls.RESET}"

    @classmethod
    def warn(cls, text: str) -> str:
        return f"{cls.YELLOW}{cls.BOLD}{text}{cls.RESET}"

    @classmethod
    def err(cls, text: str) -> str:
        return f"{cls.RED}{cls.BOLD}{text}{cls.RESET}"

    @classmethod
    def hdr(cls, text: str) -> str:
        return f"{cls.CYAN}{cls.BOLD}{text}{cls.RESET}"

    @classmethod
    def dim(cls, text: str) -> str:
        return f"{cls.DIM}{text}{cls.RESET}"


C.configure(sys.stdout.isatty() and "NO_COLOR" not in os.environ)


def compute_key(seed: int, poly: int) -> int:
    key = seed
    for _ in range(35):
        if key & 0x80000000:
            key = ((key << 1) ^ poly) & 0xFFFFFFFF
        else:
            key = (key << 1) & 0xFFFFFFFF
    return key


def parse_ascii_field(raw: bytes, max_len: int = 32) -> str:
    trimmed = raw[:max_len].split(b"\x00", 1)[0]
    if not trimmed:
        return ""
    if not (0x21 <= trimmed[0] <= 0x7E):
        return ""
    return "".join(chr(b) for b in trimmed if 0x20 <= b <= 0x7E).strip()


def validate_esp32c3_image(data: bytes) -> dict:
    if len(data) < 24:
        raise RuntimeError(f"Firmware file too small ({len(data)} bytes)!")
    if len(data) > MAX_PARTITION_SIZE:
        raise RuntimeError(
            f"Firmware size ({len(data)} bytes) exceeds 1 MB partition limit ({MAX_PARTITION_SIZE} bytes)!"
        )
    magic = data[0]
    segment_count = data[1]
    chip_id = struct.unpack_from("<H", data, 12)[0]
    hash_appended = data[23]

    if magic != 0xE9:
        raise RuntimeError(f"Invalid ESP32 app image magic (expected 0xE9, got 0x{magic:02X})!")
    if chip_id != 0x0005:
        raise RuntimeError(f"Wrong chip target in image header (expected ESP32-C3 = 0x0005, got 0x{chip_id:04X})!")
    if segment_count == 0 or segment_count > 16:
        raise RuntimeError(f"Invalid ESP32 segment count ({segment_count})!")

    # Walk all segments to verify XOR checksum, exact image end, and optional SHA-256 digest
    offset = 24
    xor_checksum = 0xEF
    for idx in range(segment_count):
        if offset + 8 > len(data):
            raise RuntimeError(f"Truncated ESP32 image at segment {idx} header!")
        _load_addr, seg_len = struct.unpack_from("<II", data, offset)
        offset += 8
        if offset + seg_len > len(data):
            raise RuntimeError(f"Truncated ESP32 image at segment {idx} data ({seg_len} bytes)!")
        for b in data[offset : offset + seg_len]:
            xor_checksum ^= b
        offset += seg_len

    # Checksum byte sits at the last byte of the 16-byte aligned block following the last segment
    padded_end = (offset + 16) & ~15
    if padded_end > len(data):
        raise RuntimeError("Truncated ESP32 image before XOR checksum byte!")
    stored_xor = data[padded_end - 1]
    if stored_xor != xor_checksum:
        raise RuntimeError(
            f"ESP32 image XOR checksum mismatch (expected 0x{xor_checksum:02X}, got 0x{stored_xor:02X})!"
        )

    sha256_verified = False
    sha256_short = ""
    exact_size = padded_end
    if hash_appended == 1:
        if padded_end + 32 > len(data):
            raise RuntimeError("Truncated ESP32 image before appended 32-byte SHA-256 digest!")
        stored_sha = data[padded_end : padded_end + 32]
        calc_sha = hashlib.sha256(data[:padded_end]).digest()
        if stored_sha != calc_sha:
            raise RuntimeError(
                f"ESP32 image SHA-256 integrity check FAILED! "
                f"(expected {calc_sha.hex()[:16]}..., got {stored_sha.hex()[:16]}...)"
            )
        sha256_verified = True
        sha256_short = calc_sha.hex()[:16]
        exact_size = padded_end + 32

    # Try to read ESP-IDF esp_app_desc_t (starts at offset 0x20 if standard image)
    app_ver = ""
    app_name = ""
    if len(data) >= 0x20 + 80:
        desc_magic = struct.unpack_from("<I", data, 0x20)[0]
        if desc_magic == 0xABCD5432:
            app_ver = parse_ascii_field(data[0x20 + 16 : 0x20 + 48], 32)
            app_name = parse_ascii_field(data[0x20 + 48 : 0x20 + 80], 32)

    return {
        "size": len(data),
        "exact_size": exact_size,
        "magic": magic,
        "chip_id": chip_id,
        "app_name": app_name,
        "app_version": app_ver,
        "sha256_verified": sha256_verified,
        "sha256_short": sha256_short,
    }


class WakeupKeeper:
    """Background keep-alive sender for bench flashing when no motorcycle ECU is on the bus."""

    def __init__(self, interface: str, wake_ids: list[int], interval: float = 0.25):
        self.interface = interface
        self._lock = threading.Lock()
        self._wake_ids = list(wake_ids)
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def set_wake_ids(self, wake_ids: list[int]) -> None:
        with self._lock:
            self._wake_ids = list(wake_ids)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        # TX-only socket: disable RX delivery so the kernel does not queue unused frames
        try:
            self._sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, b"")
        except OSError:
            pass
        self._sock.bind((self.interface,))
        self._sock.settimeout(0.2)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="can-wakeup-keeper", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        payload = bytes(8)
        while not self._stop.is_set():
            with self._lock:
                ids = list(self._wake_ids)
                sock = self._sock
            for can_id in ids:
                if self._stop.is_set() or sock is None:
                    break
                try:
                    frame = struct.pack("=IB3x8s", can_id, 8, payload)
                    sock.send(frame)
                except OSError:
                    # e.g. ENOBUFS while waiting for module ACK during boot
                    self._stop.wait(0.1)
                self._stop.wait(0.015)
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass


class CanFlasher:
    def __init__(self, interface: str = CAN_INTERFACE):
        self.interface = interface
        self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        # Enable CAN error frames so we can detect missing-ACK / BUS-OFF on bench setups
        try:
            err_mask = 0x1FFFFFFF  # CAN_ERR_MASK
            self.sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_ERR_FILTER, struct.pack("=I", err_mask))
        except OSError:
            pass
        try:
            self.sock.bind((self.interface,))
        except OSError as e:
            self.sock.close()
            raise RuntimeError(
                f"Cannot open SocketCAN interface '{self.interface}': {e}\n"
                f"Bring it up first with:\n"
                f"  sudo ip link set {self.interface} down\n"
                f"  sudo ip link set {self.interface} up type can bitrate 500000"
            ) from e
        self.sock.settimeout(0.1)
        self.ack_error_seen = False
        self.bus_off_seen = False
        # Filter RX in the Linux kernel to Gateway frames (0x785 + 0x6F0) by default so
        # high-rate in-vehicle bus traffic (~1000 fps) cannot stall UDS/drain() or overflow RX.
        self.set_gateway_rx_filter(True)

    def set_gateway_rx_filter(self, enable: bool = True):
        """Restrict SocketCAN RX in kernel space to 0x785 (UDS Resp) and 0x6F0 (Heartbeat)."""
        try:
            if enable:
                # Match exact 11-bit standard data frames (EFF=0, RTR=0)
                mask = 0xC00007FF  # CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_SFF_MASK
                flt = struct.pack("=IIII", CAN_ID_RESP, mask, CAN_ID_HEARTBEAT, mask)
            else:
                flt = struct.pack("=II", 0, 0)
            self.sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, flt)
        except OSError:
            pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def send_frame(self, can_id: int, data: bytes):
        can_frame = struct.pack("=IB3x8s", can_id & 0x7FF, 8, data[:8].ljust(8, b"\x55"))
        deadline = time.monotonic() + 0.25
        while True:
            try:
                self.sock.send(can_frame)
                return
            except OSError as e:
                if e.errno in (errno.ENOBUFS, errno.EAGAIN, errno.EWOULDBLOCK) and time.monotonic() < deadline:
                    time.sleep(0.005)
                    continue
                if e.errno == errno.ENETDOWN:
                    raise RuntimeError(
                        f"SocketCAN interface '{self.interface}' is DOWN! Bring it up with:\n"
                        f"  sudo ip link set {self.interface} up type can bitrate 500000"
                    ) from e
                raise

    def _check_error_frame(self, raw_can_id: int) -> bool:
        if raw_can_id & 0x20000000:  # CAN_ERR_FLAG
            if raw_can_id & 0x00000020:  # CAN_ERR_ACK
                self.ack_error_seen = True
            if raw_can_id & 0x00000040:  # CAN_ERR_BUSOFF
                self.bus_off_seen = True
            return True
        return False

    def recv_frame(self, target_id: int, timeout: float = 2.0) -> bytes | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw_frame = self.sock.recv(16)
                if len(raw_frame) < 16:
                    continue
                raw_can_id, can_dlc = struct.unpack("=IB3x", raw_frame[:8])
                if self._check_error_frame(raw_can_id):
                    continue
                if raw_can_id & 0xC0000000:  # Ignore EFF (29-bit) & RTR frames
                    continue
                can_id = raw_can_id & 0x7FF
                if can_id == target_id:
                    dlc = min(can_dlc, 8)
                    return raw_frame[8 : 8 + dlc]
            except socket.timeout:
                continue
        return None

    def sniff_bus(self, duration: float = 1.5) -> dict:
        """Passively listen on the CAN bus to detect vehicle harness traffic & wakeup PIDs."""
        seen_counts: dict[int, int] = {}
        last_hb: bytes | None = None
        self.set_gateway_rx_filter(False)
        self.drain()
        deadline = time.monotonic() + duration
        try:
            while time.monotonic() < deadline:
                try:
                    raw_frame = self.sock.recv(16)
                    if len(raw_frame) < 16:
                        continue
                    raw_can_id, can_dlc = struct.unpack("=IB3x", raw_frame[:8])
                    if self._check_error_frame(raw_can_id):
                        continue
                    if raw_can_id & 0xC0000000:  # Ignore EFF (29-bit) & RTR frames
                        continue
                    can_id = raw_can_id & 0x7FF
                    seen_counts[can_id] = seen_counts.get(can_id, 0) + 1
                    if can_id == CAN_ID_HEARTBEAT:
                        dlc = min(can_dlc, 8)
                        last_hb = raw_frame[8 : 8 + dlc]
                except socket.timeout:
                    continue
        finally:
            self.set_gateway_rx_filter(True)
            self.drain()

        wakeup_found = [wid for wid in WAKEUP_IDS if wid in seen_counts]
        other_ecu_ids = [
            cid for cid in seen_counts if cid not in (CAN_ID_HEARTBEAT, CAN_ID_REQ, CAN_ID_RESP)
        ]
        return {
            "total_frames": sum(seen_counts.values()),
            "seen_counts": seen_counts,
            "heartbeat": last_hb,
            "wakeup_found": wakeup_found,
            "other_ecu_ids": sorted(other_ecu_ids),
            "in_vehicle": bool(wakeup_found) or len(other_ecu_ids) >= 2,
        }

    def drain(self, max_frames: int = 4096):
        """Truly non-blocking drain of pending RX frames (avoids CPython settimeout() select wait)."""
        for _ in range(max_frames):
            r, _, _ = select.select([self.sock], [], [], 0.0)
            if not r:
                break
            try:
                self.sock.recv(16, socket.MSG_DONTWAIT)
            except (BlockingIOError, socket.timeout):
                break
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                break

    def _wait_flow_control(self, timeout: float = 1.5) -> tuple[int, float]:
        """Wait for ISO-TP Flow Control (0x30 CTS) and return (block_size, st_min_sec)."""
        fc_deadline = time.monotonic() + timeout
        wait_count = 0
        while time.monotonic() < fc_deadline:
            fc = self.recv_frame(CAN_ID_RESP, timeout=max(0.05, fc_deadline - time.monotonic()))
            if not fc or len(fc) < 3 or (fc[0] & 0xF0) != 0x30:
                continue
            fs = fc[0] & 0x0F
            if fs == 0x01:  # WAIT
                wait_count += 1
                if wait_count > 10:
                    raise RuntimeError("Gateway exceeded max ISO-TP FlowControl WAIT count (10)")
                fc_deadline = time.monotonic() + timeout
                continue
            if fs == 0x02:  # OVERFLOW
                raise RuntimeError("Gateway returned ISO-TP FlowControl OVERFLOW (0x32)")
            if fs == 0x00:  # ContinueToSend
                block_size = fc[1]
                st_min_byte = fc[2]
                if st_min_byte <= 0x7F:
                    st_min = st_min_byte / 1000.0
                elif 0xF1 <= st_min_byte <= 0xF9:
                    st_min = (st_min_byte - 0xF0) / 10000.0
                else:
                    st_min = 0.003
                if st_min < 0.002:
                    st_min = 0.002
                return block_size, st_min
        raise RuntimeError("Timeout waiting for ISO-TP Flow Control (0x30) from Gateway")

    def send_isotp(self, payload: bytes):
        length = len(payload)
        if length == 0 or length > 4095:
            raise ValueError(f"Invalid ISO-TP payload length: {length}")
        if length <= 7:
            self.send_frame(CAN_ID_REQ, bytes([length]) + payload)
            return

        # First Frame (FF)
        ff = bytes([0x10 | ((length >> 8) & 0x0F), length & 0xFF]) + payload[:6]
        self.send_frame(CAN_ID_REQ, ff)

        block_size, st_min = self._wait_flow_control(timeout=1.5)

        # Consecutive Frames (CF) honoring BlockSize (BS) & STmin
        offset = 6
        sn = 1
        sent_in_block = 0
        while offset < length:
            if block_size > 0 and sent_in_block >= block_size:
                block_size, st_min = self._wait_flow_control(timeout=1.5)
                sent_in_block = 0
            chunk = payload[offset : offset + 7]
            cf = bytes([0x20 | (sn & 0x0F)]) + chunk
            time.sleep(st_min)
            self.send_frame(CAN_ID_REQ, cf)
            offset += len(chunk)
            sn = (sn + 1) & 0x0F
            sent_in_block += 1

    def recv_isotp(self, timeout: float = 2.0) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.recv_frame(CAN_ID_RESP, timeout=max(0.05, deadline - time.monotonic()))
            if not frame:
                raise TimeoutError("Timeout waiting for ISO-TP response")

            pci_type = frame[0] >> 4
            if pci_type == 0:  # Single Frame
                length = frame[0] & 0x0F
                if length == 0 or length > 7 or (1 + length) > len(frame):
                    continue
                return frame[1 : 1 + length]
            if pci_type == 1:  # First Frame
                if len(frame) < 8:
                    continue
                total_len = ((frame[0] & 0x0F) << 8) | frame[1]
                if total_len < 8 or total_len > 4095:
                    raise RuntimeError(f"Invalid ISO-TP First Frame length: {total_len}")
                data = bytearray(frame[2:8])
                self.send_frame(CAN_ID_REQ, bytes([0x30, 0x00, 0x00, 0x55, 0x55, 0x55, 0x55, 0x55]))
                expected_sn = 1
                cf_deadline = time.monotonic() + max(timeout, 5.0)
                while len(data) < total_len:
                    rem = cf_deadline - time.monotonic()
                    if rem <= 0:
                        raise TimeoutError("Overall timeout waiting for ISO-TP consecutive frames")
                    cf = self.recv_frame(CAN_ID_RESP, timeout=min(2.0, rem))
                    if not cf:
                        raise TimeoutError("Timeout waiting for ISO-TP consecutive frame")
                    if (cf[0] >> 4) != 2:
                        continue
                    sn = cf[0] & 0x0F
                    if sn != expected_sn:
                        raise RuntimeError(f"ISO-TP SN mismatch: expected {expected_sn}, got {sn}")
                    expected_sn = (expected_sn + 1) & 0x0F
                    needed = min(total_len - len(data), 7)
                    if len(cf) < 1 + needed:
                        raise RuntimeError(f"Truncated ISO-TP CF (expected >= {1 + needed}B, got {len(cf)}B)")
                    data.extend(cf[1 : 1 + needed])
                return bytes(data[:total_len])
            if pci_type == 3:
                continue
        raise TimeoutError("Timeout waiting for ISO-TP response")

    def execute_uds(self, payload: bytes, timeout: float = 2.0) -> bytes:
        req_sid = payload[0]
        expected_pos_sid = (req_sid + 0x40) & 0xFF
        self.drain()
        self.send_isotp(payload)

        deadline = time.monotonic() + timeout
        pending_count = 0
        while time.monotonic() < deadline:
            resp = self.recv_isotp(timeout=max(0.1, deadline - time.monotonic()))
            if not resp:
                raise TimeoutError("Empty UDS response")
            if resp[0] == 0x7F and len(resp) >= 3:
                sid, nrc = resp[1], resp[2]
                if sid != req_sid:
                    # Ignore stale NRC belonging to a different/earlier SID
                    continue
                if nrc == 0x78:  # ResponsePending
                    pending_count += 1
                    if pending_count > 25:
                        raise RuntimeError(f"UDS SID 0x{req_sid:02X} exceeded max NRC 0x78 ResponsePending count")
                    deadline = time.monotonic() + max(timeout, 5.0)
                    continue
                raise RuntimeError(f"UDS NRC 0x{nrc:02X} for SID 0x{sid:02X}")
            if resp[0] != expected_pos_sid:
                # Ignore stale response from an earlier request
                continue
            return resp
        raise TimeoutError(f"Timeout waiting for UDS response to SID 0x{req_sid:02X}")

    def read_did(self, did: int, timeout: float = 1.5) -> bytes | None:
        try:
            resp = self.execute_uds(bytes([0x22, (did >> 8) & 0xFF, did & 0xFF]), timeout=timeout)
            if len(resp) >= 3 and resp[0] == 0x62 and resp[1] == ((did >> 8) & 0xFF) and resp[2] == (did & 0xFF):
                return resp[3:]
        except Exception:
            pass
        return None

    def write_did(self, did: int, data: bytes, timeout: float = 3.0) -> bool:
        resp = self.execute_uds(bytes([0x2E, (did >> 8) & 0xFF, did & 0xFF]) + data, timeout=timeout)
        return len(resp) >= 3 and resp[0] == 0x6E and resp[1] == ((did >> 8) & 0xFF) and resp[2] == (did & 0xFF)

    def read_dtc_count(self) -> int | None:
        try:
            resp = self.execute_uds(bytes([0x19, 0x01, 0xFF]), timeout=1.2)
            if len(resp) >= 6 and resp[0] == 0x59 and resp[1] == 0x01:
                return struct.unpack(">H", resp[4:6])[0]
        except Exception:
            pass
        return None

    def unlock_security(self, seed_sub: int, poly: int, session: int = 0x02) -> None:
        resp = self.execute_uds(bytes([0x10, session]), timeout=2.0)
        if len(resp) < 2 or resp[0] != 0x50 or resp[1] != session:
            raise RuntimeError(f"Failed to enter session 0x{session:02X}: {resp.hex()}")

        resp = self.execute_uds(bytes([0x27, seed_sub]), timeout=2.0)
        if len(resp) < 6 or resp[0] != 0x67 or resp[1] != seed_sub:
            raise RuntimeError(f"Security seed 0x{seed_sub:02X} failed: {resp.hex()}")

        seed = struct.unpack(">I", resp[2:6])[0]
        if seed == 0:
            print(f" Security Level 0x{seed_sub:02X} already unlocked (Seed = 0).")
            return

        key = compute_key(seed, poly)
        print(f" Seed: 0x{seed:08X} -> Key: 0x{key:08X}")
        key_sub = seed_sub + 1
        resp = self.execute_uds(bytes([0x27, key_sub]) + struct.pack(">I", key), timeout=2.0)
        if len(resp) < 2 or resp[0] != 0x67 or resp[1] != key_sub:
            raise RuntimeError(f"Security key 0x{key_sub:02X} rejected: {resp.hex()}")

    def clear_dtcs(self) -> bool:
        try:
            self.execute_uds(bytes([0x10, 0x03]), timeout=1.0)
        except Exception:
            pass
        try:
            resp = self.execute_uds(bytes([0x14, 0xFF, 0xFF, 0xFF]), timeout=1.5)
            return len(resp) >= 1 and resp[0] == 0x54
        except Exception:
            return False

    def hard_reset(self) -> None:
        try:
            self.execute_uds(bytes([0x11, 0x01]), timeout=1.0)
        except Exception:
            pass

    def read_heartbeat(self, timeout: float = 2.0) -> bytes | None:
        return self.recv_frame(CAN_ID_HEARTBEAT, timeout=timeout)


def extract_heartbeat_ubatt_v(hb: bytes | None) -> float | None:
    """Decode Gateway ADC1_CH0 supply voltage from 0x6F0 Bytes 2..3 (12-bit, 5 mV/LSB)."""
    if not hb or len(hb) < 5 or (hb[3] & 0x0F) != 0:
        return None
    ubatt_12 = (hb[2] << 4) | ((hb[3] >> 4) & 0x0F)
    return ubatt_12 * 0.005


def parse_heartbeat(hb: bytes) -> str:
    if len(hb) < 5:
        return f"Raw: {hb.hex()}"
    alive = (hb[0] >> 4) & 0x0F
    err_flags = (hb[0] >> 2) & 0x03
    model_id = hb[1]
    ubatt_12 = (hb[2] << 4) | ((hb[3] >> 4) & 0x0F)
    ubatt_v = ubatt_12 * 0.005
    vin_state = (hb[4] >> 4) & 0x0F
    ble_detail = hb[4] & 0x0F
    vin_map = {0: "Wait", 1: "OK", 2: "Mismatch"}
    ble_map = {0: "Off(Cal)", 1: "Off", 2: "Connected", 3: "Streaming", 4: "Ready", 5: "Idle/Standby"}
    vin_str = vin_map.get(vin_state, str(vin_state))
    ble_str = ble_map.get(ble_detail, str(ble_detail))
    return (
        f"Model: {model_id}, Ubatt: {ubatt_v:.2f}V, BLE: {ble_str}, "
        f"VIN: {vin_str}, Faults: 0x{err_flags:X}, Alive: {alive}"
    )


def read_module_info(flasher: CanFlasher, hb: bytes | None) -> dict:
    """Read comprehensive Gateway identification & diagnostic info before flashing."""
    # Reset to Default Session first
    uds_ok = False
    try:
        resp = flasher.execute_uds(bytes([0x10, 0x01]), timeout=1.5)
        uds_ok = len(resp) >= 2 and resp[0] == 0x50
    except Exception:
        pass
    time.sleep(0.05)

    info: dict = {
        "uds_ok": uds_ok,
        "heartbeat": hb,
        "serial": "",
        "hw_rev": "",
        "sw_num": "",
        "sw_ver": "",
        "prog_date": "",
        "fareflash_id": "",
        "profile_id": None,
        "profile_name": "unknown",
        "partition_id": None,
        "partition_name": "unknown",
        "dtc_count": None,
        "eol": None,
    }

    if not uds_ok:
        return info

    raw_ser = flasher.read_did(0xF18C, timeout=1.0)
    if raw_ser:
        info["serial"] = parse_ascii_field(raw_ser, 20)[:6]

    raw_hw = flasher.read_did(0xF193, timeout=1.0)
    if raw_hw:
        info["hw_rev"] = parse_ascii_field(raw_hw, 20)

    raw_swn = flasher.read_did(0xF194, timeout=1.0)
    if raw_swn:
        info["sw_num"] = parse_ascii_field(raw_swn, 32)

    raw_swv = flasher.read_did(0xF195, timeout=1.0)
    if raw_swv:
        info["sw_ver"] = parse_ascii_field(raw_swv, 32)

    raw_date = flasher.read_did(0xF199, timeout=1.0)
    if raw_date:
        info["prog_date"] = parse_ascii_field(raw_date, 20)

    raw_ff = flasher.read_did(0xEE01, timeout=0.8)
    if raw_ff:
        info["fareflash_id"] = parse_ascii_field(raw_ff, 48)

    raw_prof = flasher.read_did(0xFD03, timeout=1.0)
    if raw_prof and len(raw_prof) >= 2:
        pid = struct.unpack(">H", raw_prof[:2])[0]
        info["profile_id"] = pid
        info["profile_name"] = PROFILE_INFO.get(pid, (f"Profile {pid}", 0x260))[0]
    elif hb and len(hb) >= 2 and hb[1] > 0:
        pid = hb[1]
        info["profile_id"] = pid
        info["profile_name"] = PROFILE_INFO.get(pid, (f"Profile {pid}", 0x260))[0]

    raw_part = flasher.read_did(0xFD0B, timeout=1.0)
    if raw_part and len(raw_part) >= 1:
        info["partition_id"] = raw_part[0]
        info["partition_name"] = PARTITION_NAMES.get(raw_part[0], f"Partition {raw_part[0]}")

    # Switch to Extended Session for EOL backup (0xFD00) & DTC count (0x19 0x01)
    try:
        flasher.execute_uds(bytes([0x10, 0x03]), timeout=1.0)
    except Exception:
        pass

    raw_eol = flasher.read_did(0xFD00, timeout=2.0)
    if raw_eol and len(raw_eol) >= 159:
        info["eol"] = raw_eol[:159]
        if not info["serial"]:
            info["serial"] = parse_ascii_field(info["eol"][14:34], 20)[:6]

    info["dtc_count"] = flasher.read_dtc_count()
    return info


def print_preflight_summary(
    img_info: dict | None,
    bus_info: dict,
    mod_info: dict,
    keeper_active: bool,
    wake_ids: list[int],
) -> tuple[bool, list[str], bool]:
    """Print complete module & bus overview and evaluate whether flashing is safe to start."""
    sep_eq = C.hdr("=" * 68)
    sep_dash = C.dim("-" * 68)
    print(f"\n{sep_eq}")
    print(C.hdr("  GATEWAY MODULE OVERVIEW & PRE-FLIGHT SAFETY CHECK"))
    print(sep_eq)

    # Bus status
    if bus_info["in_vehicle"]:
        wids = ", ".join(f"0x{w:03X}" for w in bus_info["wakeup_found"]) or "none"
        frame_cnt = bus_info["total_frames"]
        other_ids = ", ".join(f"0x{c:03X}" for c in bus_info["other_ecu_ids"][:8]) or "none"
        print(f"  CAN Bus Mode      : {C.ok('IN-VEHICLE HARNESS')} {C.dim(f'(live bus: {frame_cnt} frames / 1.5s)')}")
        print(f"  Wakeup PIDs Seen  : {C.CYAN}{wids}{C.RESET}")
        print(f"  Other ECU IDs     : {C.dim(other_ids)}")
    else:
        wids = ", ".join(f"0x{w:03X}" for w in wake_ids)
        print(f"  CAN Bus Mode      : {C.warn('BENCH / STANDALONE')} {C.dim('(no vehicle ECU traffic)')}")
        ka_str = C.ok(f"ACTIVE ({wids} @ 2 Hz)") if keeper_active else C.warn("OFF")
        print(f"  Auto Keep-Alive   : {ka_str}")

    print(sep_dash)
    is_fareflash = bool(mod_info["fareflash_id"])
    fw_display = mod_info["fareflash_id"] or mod_info["sw_num"] or mod_info["sw_ver"] or "<unknown>"
    if mod_info["sw_ver"] and mod_info["sw_ver"] not in fw_display:
        fw_display += f" ({mod_info['sw_ver']})"

    uds_str = C.ok("OK (responding)") if mod_info["uds_ok"] else C.err("NO RESPONSE")
    print(f"  UDS 0x780->0x785  : {uds_str}")
    hb_str = parse_heartbeat(mod_info["heartbeat"]) if mod_info["heartbeat"] else C.warn("NOT SEEN")
    print(f"  Heartbeat (0x6F0) : {hb_str}")
    print(f"  Firmware Version  : {C.BOLD}{fw_display}{C.RESET}")
    print(f"  Build/Prog Date   : {mod_info['prog_date'] or '<n/a>'}")
    print(f"  Serial (0xF18C)   : {C.CYAN}{mod_info['serial'] or '<none>'}{C.RESET}")
    print(f"  HW-Rev (0xF193)   : {mod_info['hw_rev'] or '<none>'}")
    prof_str = (
        f"{mod_info['profile_id']} ({mod_info['profile_name']})"
        if mod_info["profile_id"] is not None
        else "<unknown>"
    )
    print(f"  Vehicle Profile   : {prof_str}")
    print(f"  Active Partition  : {C.BOLD}{mod_info['partition_name']}{C.RESET}")
    dtc_val = mod_info["dtc_count"]
    dtc_str = C.ok("0") if dtc_val == 0 else (C.warn(str(dtc_val)) if dtc_val is not None else "<n/a>")
    print(f"  Gateway DTCs      : {dtc_str}")
    eol_str = C.ok("OK (159 bytes backed up)") if mod_info["eol"] else C.warn("NOT AVAILABLE")
    print(f"  EOL Backup 0xFD00 : {eol_str}")

    if img_info:
        print(sep_dash)
        desc = f"{img_info['app_name']} {img_info['app_version']}".strip() or "ESP32-C3 App"
        sha_txt = C.ok(f"SHA-256 OK ({img_info['sha256_short']}...)") if img_info.get("sha256_verified") else C.ok("XOR OK")
        print(f"  Target Binary     : {C.BOLD}{desc}{C.RESET} ({img_info['size']} bytes, ESP32-C3, {sha_txt})")

    print(sep_dash)
    print(f"  {C.BOLD}Pre-Flight Verification:{C.RESET}")

    blockers: list[str] = []
    warnings: list[str] = []

    if img_info:
        if img_info.get("sha256_verified"):
            print(f"   {C.ok('[OK]')}   Firmware image valid (ESP32-C3, XOR + SHA-256 verified, <= 1 MB)")
        else:
            print(f"   {C.ok('[OK]')}   Firmware image header & XOR checksum valid (ESP32-C3, <= 1 MB)")

    if mod_info["uds_ok"]:
        print(f"   {C.ok('[OK]')}   Gateway responds on UDS 0x780 -> 0x785")
    else:
        print(f"   {C.err('[FAIL]')} Gateway does NOT respond on UDS 0x780 -> 0x785")
        blockers.append("Gateway not reachable over CAN UDS (check wiring, 12V power, 500 kbit/s, 120 Ohm termination).")

    low_voltage = False
    ubatt_v = extract_heartbeat_ubatt_v(mod_info["heartbeat"])

    if mod_info["heartbeat"]:
        print(f"   {C.ok('[OK]')}   Gateway 0x6F0 heartbeat active")
        if ubatt_v is not None:
            if ubatt_v < MIN_FLASH_VOLTAGE_V:
                low_voltage = True
                if ubatt_v < 5.0:
                    print(
                        f"   {C.err('[FAIL]')} Gateway supply voltage too low ({ubatt_v:.2f} V < {MIN_FLASH_VOLTAGE_V:.1f} V — "
                        "bare dev-board without GPIO0 divider? Use --force)"
                    )
                else:
                    print(
                        f"   {C.err('[FAIL]')} Gateway supply voltage too low ({ubatt_v:.2f} V < {MIN_FLASH_VOLTAGE_V:.1f} V — "
                        "connect a 12V battery charger! Use --force to override)"
                    )
                warnings.append(f"Gateway supply voltage too low ({ubatt_v:.2f} V < {MIN_FLASH_VOLTAGE_V:.1f} V).")
            elif ubatt_v > MAX_FLASH_VOLTAGE_V:
                print(
                    f"   {C.warn('[WARN]')} Gateway supply voltage high ({ubatt_v:.2f} V > {MAX_FLASH_VOLTAGE_V:.1f} V — "
                    "check power supply!)"
                )
                warnings.append(f"Gateway supply voltage high ({ubatt_v:.2f} V).")
            else:
                print(f"   {C.ok('[OK]')}   Gateway supply voltage OK ({ubatt_v:.2f} V >= {MIN_FLASH_VOLTAGE_V:.1f} V)")
        else:
            print(f"   {C.warn('[WARN]')} Could not decode 0x6F0 ADC Ubatt — verify >= 12.0V power manually")
        if len(mod_info["heartbeat"]) >= 5 and (mod_info["heartbeat"][4] & 0x0F) in (2, 3):
            print(f"   {C.warn('[WARN]')} A BLE app is currently connected — disconnect BLE before CAN flashing!")
            warnings.append("BLE client connected during CAN flash.")
    else:
        print(f"   {C.warn('[WARN]')} No 0x6F0 heartbeat seen yet")
        warnings.append("No 0x6F0 heartbeat received.")

    if bus_info["in_vehicle"] and bus_info["wakeup_found"]:
        print(f"   {C.ok('[OK]')}   Vehicle wakeup PID active (module stays powered during flash)")
    elif keeper_active:
        print(f"   {C.ok('[OK]')}   Automatic bench wakeup/keep-alive active (module stays powered during flash)")
    else:
        print(f"   {C.warn('[WARN]')} Neither vehicle wakeup PID nor keep-alive active (module may sleep after 10s!)")
        warnings.append("No wakeup/keep-alive active.")

    sw_tokens = [mod_info["sw_num"].strip().lower(), mod_info["sw_ver"].strip().lower()]
    fw_combined = f"{mod_info['sw_num']} {mod_info['sw_ver']} {mod_info['fareflash_id']}".lower()
    is_v203 = ("2.03" in fw_combined) or any(tok.startswith("v2.") or tok.startswith("2.") for tok in sw_tokens if tok)
    is_v11 = any(tok in ("v1.1", "1.1", "v1.10") for tok in sw_tokens)

    if is_v203:
        print(f"   {C.err('[WARN]')} Stock V2.03 firmware detected — UNTESTED & may be protected! (--force required)")
        warnings.append("Stock V2.03 is untested and may be protected (flashing requires --force at your own risk!).")
    elif is_fareflash or is_v11:
        print(f"   {C.ok('[OK]')}   Compatible firmware detected (OEM V1.1 or FaReFlash)")
    else:
        print(f"   {C.warn('[WARN]')} Unknown firmware version — verify compatibility before proceeding")
        warnings.append(f"Unrecognized firmware version string: '{fw_display}'.")

    if mod_info["serial"] and mod_info["eol"]:
        print(f"   {C.ok('[OK]')}   Serial ('{mod_info['serial']}') & 159-byte EOL block backed up in RAM")
    elif mod_info["serial"]:
        print(f"   {C.warn('[WARN]')} Serial ('{mod_info['serial']}') read, but 0xFD00 EOL block not readable")
        warnings.append("0xFD00 EOL block could not be read.")
    else:
        print(f"   {C.warn('[WARN]')} Serial number could not be read before flash")
        warnings.append("Serial number not readable before flash.")

    if blockers:
        print(f"\n  {C.err('STATUS: NOT SAFE TO FLASH!')}")
        for b in blockers:
            print(f"   {C.err('-> BLOCKER:')} {b}")
    elif low_voltage:
        print(f"\n  {C.err(f'STATUS: LOW SUPPLY VOLTAGE ({ubatt_v:.2f} V < {MIN_FLASH_VOLTAGE_V:.1f} V) — BLOCKED UNLESS --force IS SET!')}")
    elif is_v203:
        print(f"\n  {C.err('STATUS: UNTESTED V2.03 MODULE — BLOCKED UNLESS --force IS SET!')}")
    elif warnings:
        print(f"\n  {C.warn('STATUS: READY WITH WARNINGS (review warnings above before continuing)')}")
    else:
        print(f"\n  {C.ok('STATUS: ALL CHECKS PASSED — SAFE TO START FLASH!')}")

    print(sep_eq)
    return (len(blockers) == 0), warnings, is_v203, low_voltage, ubatt_v


def flash_firmware(
    image_path: str | None,
    interface: str = CAN_INTERFACE,
    skip_nvs_restore: bool = False,
    check_only: bool = False,
    assume_yes: bool = False,
    forced_wake_id: int | None = None,
    force_unsupported: bool = False,
):
    print(C.hdr("=== Fantic e-Shock CAN UDS Firmware Flasher ==="))
    print(f"Interface:        {C.CYAN}{interface}{C.RESET}")
    if image_path:
        print(f"Target Binary:    {C.BOLD}{os.path.basename(image_path)}{C.RESET}")
    if check_only:
        print(f"Mode:             {C.warn('CHECK-ONLY')} {C.dim('(read module info & verify bus, no flashing)')}")

    firmware_data: bytes | None = None
    img_info: dict | None = None

    if not check_only:
        assert image_path is not None
        with open(image_path, "rb") as f:
            firmware_data = f.read()

        img_info = validate_esp32c3_image(firmware_data)

        # Trim trailing 0xFF flash padding cleanly using exact ESP32 image end if a 1 MB partition dump is supplied
        if len(firmware_data) == MAX_PARTITION_SIZE and img_info["exact_size"] < len(firmware_data):
            trimmed = firmware_data[: img_info["exact_size"]]
            print(C.dim(f"Full 1 MB dump trimmed cleanly to exact image size: {len(trimmed)} bytes"))
            firmware_data = trimmed
            img_info["size"] = len(firmware_data)

    flasher = CanFlasher(interface)
    keeper: WakeupKeeper | None = None
    active_wake_ids: list[int] = [forced_wake_id] if forced_wake_id else list(WAKEUP_IDS.keys())
    ota_started = False
    boot_switched = False

    try:
        # ------------------------------------------------------------------
        # Step 0: Passive CAN bus detection & Wakeup/Keep-Alive handling
        # ------------------------------------------------------------------
        print(f"\n{C.hdr('[Step 0]')} Sniffing CAN bus (1.5s) to detect vehicle harness & wakeup traffic...")
        bus_info = flasher.sniff_bus(duration=1.5)
        hb = bus_info["heartbeat"]

        if bus_info["in_vehicle"] and bus_info["wakeup_found"]:
            wids = ", ".join(f"0x{w:03X}" for w in bus_info["wakeup_found"])
            print(f" {C.ok('In-vehicle CAN bus active')} ({bus_info['total_frames']} frames, wakeup PID: {C.CYAN}{wids}{C.RESET}).")
            print(C.dim(" Leaving wakeup control to the motorcycle ECU (no manual wakeup frames sent)."))
        else:
            if bus_info["total_frames"] == 0:
                print(f" {C.warn('CAN bus is silent')} {C.dim('(bench setup or module sleeping).')}")
            else:
                frame_cnt = bus_info["total_frames"]
                print(f" {C.warn('No motorcycle wakeup PID seen')} {C.dim(f'({frame_cnt} frames on bus).')}")

            if forced_wake_id is not None:
                candidate_ids = [forced_wake_id]
            else:
                # Try all known Fantic Selective-Wake IDs one by one to auto-detect the right one
                candidate_ids = [0x260, 0x310, 0x2F2]

            if not hb:
                matched_wake_id: int | None = None
                for wid in candidate_ids:
                    desc = WAKEUP_IDS.get(wid, "Custom")
                    print(f" Probing wakeup ID {C.CYAN}0x{wid:03X}{C.RESET} {C.dim(f'({desc})')}...")
                    # TCAN1146 Selective-Wake (ISO 11898-2) needs an initial bus event to wake
                    # its internal oscillator, followed by a valid matching frame (WUF) -> send 3-frame burst
                    for _ in range(3):
                        try:
                            flasher.send_frame(wid, bytes(8))
                        except OSError:
                            pass
                        time.sleep(0.08)
                    # Wait up to 4.5s for TCAN1146 INH power-on + ESP32-C3 cold boot + 0x6F0 heartbeat
                    hb = flasher.read_heartbeat(timeout=4.5)
                    if hb:
                        matched_wake_id = wid
                        print(f" {C.ok(f'Gateway woke up on 0x{wid:03X}!')} {parse_heartbeat(hb)}")
                        time.sleep(0.5)
                        break

                if matched_wake_id is not None:
                    active_wake_ids = [matched_wake_id]
                else:
                    print(f" {C.warn('Warning:')} No 0x6F0 heartbeat seen after wakeup probe (probing UDS 0x780 anyway)...")
                    if flasher.bus_off_seen or flasher.ack_error_seen or not forced_wake_id:
                        print(
                            f" {C.warn('Hint:')} On a standalone bench with a sleeping module (no other CAN node for ACK),\n"
                            "       the CAN adapter's TX mailbox stalls on the first non-matching wakeup ID\n"
                            "       unless SocketCAN one-shot mode is enabled:\n"
                            f"         {C.CYAN}sudo ip link set {interface} down{C.RESET}\n"
                            f"         {C.CYAN}sudo ip link set {interface} up type can bitrate 500000 one-shot on{C.RESET}\n"
                            "       Alternatively, power-cycle 12V on the module or pass --wake-id 0x260 / 0x310 / 0x2F2."
                        )

            keeper = WakeupKeeper(interface, active_wake_ids, interval=0.5)
            keeper.start()
            wids_str = ", ".join(f"0x{w:03X}" for w in active_wake_ids)
            print(f" {C.ok('Keep-Alive started')} ({C.CYAN}{wids_str}{C.RESET} @ 2 Hz).")

        # ------------------------------------------------------------------
        # Step 1: Read all module infos & run Pre-Flight Safety Check
        # ------------------------------------------------------------------
        print(f"\n{C.hdr('[Step 1]')} Reading Gateway module info & backing up Serial/EOL data...")
        mod_info = read_module_info(flasher, hb)

        # Narrow bench keep-alive to the module's exact profile wakeup ID once known
        if keeper and not forced_wake_id and mod_info["profile_id"] in PROFILE_INFO:
            exact_wid = PROFILE_INFO[mod_info["profile_id"]][1]
            active_wake_ids = [exact_wid]
            keeper.set_wake_ids(active_wake_ids)

        safe_to_flash, _warnings, is_v203, low_voltage, ubatt_v = print_preflight_summary(
            img_info=img_info,
            bus_info=bus_info,
            mod_info=mod_info,
            keeper_active=(keeper is not None),
            wake_ids=active_wake_ids,
        )

        if check_only:
            sys.exit(0 if (safe_to_flash and not low_voltage) else 1)

        if not safe_to_flash:
            print(f"\n{C.err('Aborting:')} Pre-flight check failed. Module flash was NOT modified.")
            sys.exit(1)

        if low_voltage and not force_unsupported:
            print(
                f"\n{C.err('Aborting:')} Gateway supply voltage ({ubatt_v:.2f} V) is below {MIN_FLASH_VOLTAGE_V:.1f} V.\n"
                "          Connect a 12V battery charger before flashing to prevent a bricked ECU.\n"
                "          Module flash was NOT modified. Pass --force only on a USB dev-board or if you accept the risk."
            )
            sys.exit(1)

        if is_v203 and not force_unsupported:
            print(
                f"\n{C.err('Aborting:')} Stock V2.03 firmware is UNTESTED and may be protected.\n"
                "          Module flash was NOT modified. Pass --force only if you explicitly accept the risk."
            )
            sys.exit(1)

        if not assume_yes or is_v203 or low_voltage:
            if is_v203:
                prompt = f"\n{C.err('[DANGER: UNTESTED V2.03]')} Really proceed with flashing? [y/N]: "
            elif low_voltage:
                prompt = f"\n{C.err(f'[DANGER: LOW VOLTAGE {ubatt_v:.2f} V]')} Really proceed with flashing? [y/N]: "
            else:
                prompt = f"\n{C.warn('Start flashing now?')} Ensure stable 12V power! [y/N]: "
            answer = input(prompt).strip().lower()
            if answer not in ("y", "yes"):
                print(C.warn("Aborted by user. Module flash was NOT modified."))
                sys.exit(0)

        pre_partition_id: int | None = mod_info["partition_id"]
        saved_serial: str = mod_info["serial"]
        saved_hw_rev: str = mod_info["hw_rev"]
        saved_eol: bytes | None = mod_info["eol"]

        if flasher.clear_dtcs():
            print(f"\n {C.ok('Pre-flash Gateway DTCs cleared')} (0x14 FF FF FF).")
        else:
            print(f"\n {C.dim('Pre-flash DTC clear skipped/not acknowledged.')}")
        time.sleep(0.05)

        # ------------------------------------------------------------------
        # Step 2: Programming Session (0x10 0x02) + Security Level 1
        # ------------------------------------------------------------------
        print(f"\n{C.hdr('[Step 2]')} Entering Programming Session (0x10 0x02) & Unlocking Security Level 1...")
        flasher.unlock_security(0x35, POLY_LEVEL1, session=0x02)
        print(f" {C.ok('Security Level 1 unlocked!')}")
        time.sleep(0.05)

        # ------------------------------------------------------------------
        # Step 3: Routine 0xFD00 (Start OTA / Partition Erase) with recovery retry
        # ------------------------------------------------------------------
        print(f"\n{C.hdr('[Step 3]')} Routine 0xFD00: Starting OTA & erasing inactive OTA partition...")
        ota_started = True
        t0 = time.monotonic()
        ota_start_req = bytes([0x31, 0x01, 0xFD, 0x00, 0x01, 0x00])
        try:
            resp = flasher.execute_uds(ota_start_req, timeout=12.0)
            ok = resp[0] == 0x71 and resp[1] == 0x01 and resp[2:4] == b"\xFD\x00" and (len(resp) < 5 or resp[4] == 0x00)
        except Exception as e:
            print(f" {C.warn('Initial 0xFD00 attempt failed')} ({e}). Rebooting Gateway to clear sticky OTA state...")
            ok = False

        if not ok:
            flasher.hard_reset()
            time.sleep(2.2)
            flasher.unlock_security(0x35, POLY_LEVEL1, session=0x02)
            time.sleep(0.05)
            t0 = time.monotonic()
            resp = flasher.execute_uds(ota_start_req, timeout=12.0)
            if resp[0] != 0x71 or resp[1] != 0x01 or resp[2:4] != b"\xFD\x00" or (len(resp) >= 5 and resp[4] != 0x00):
                raise RuntimeError(f"Routine 0xFD00 failed after recovery reset: {resp.hex()}")

        print(f" {C.ok(f'Routine 0xFD00 completed in {time.monotonic() - t0:.2f}s!')}")
        time.sleep(0.08)

        # ------------------------------------------------------------------
        # Step 4: RequestDownload (0x34)
        # ------------------------------------------------------------------
        assert firmware_data is not None
        total_len = len(firmware_data)
        addr = 0x00010000
        req_dl = bytes([0x34, 0x00, 0x44]) + struct.pack(">II", addr, total_len)
        print(f"\n{C.hdr('[Step 4]')} RequestDownload (0x34): Addr=0x{addr:08X}, Size={total_len} bytes {C.dim('(may erase flash, up to ~10s)')}...")
        resp = flasher.execute_uds(req_dl, timeout=15.0)
        if resp[0] != 0x74:
            raise RuntimeError(f"RequestDownload failed: {resp.hex()}")

        max_block_len = struct.unpack(">H", resp[2:4])[0] if len(resp) >= 4 else DEFAULT_BLOCK_SIZE + 2
        block_size = (max_block_len - 2) if 16 <= max_block_len <= 514 else DEFAULT_BLOCK_SIZE
        print(f" {C.ok('RequestDownload accepted!')} maxBlockLength={max_block_len} -> payload={block_size} bytes/block")

        # ------------------------------------------------------------------
        # Step 5: TransferData (0x36) loop
        # ------------------------------------------------------------------
        total_blocks = (total_len + block_size - 1) // block_size
        print(f"\n{C.hdr('[Step 5]')} Transferring {total_len} bytes in {total_blocks} blocks...")
        t_transfer = time.monotonic()
        block_seq = 1

        for i in range(total_blocks):
            chunk = firmware_data[i * block_size : (i + 1) * block_size]
            payload = bytes([0x36, block_seq & 0xFF]) + chunk
            resp = flasher.execute_uds(payload, timeout=8.0)
            if len(resp) < 2 or resp[0] != 0x76 or resp[1] != (block_seq & 0xFF):
                raise RuntimeError(f"TransferData failed at block {i + 1} (seq {block_seq}): {resp.hex()}")
            block_seq = (block_seq + 1) & 0xFF

            if (i + 1) % 100 == 0 or (i + 1) == total_blocks:
                bytes_sent = min((i + 1) * block_size, total_len)
                pct = (i + 1) * 100.0 / total_blocks
                elapsed = max(0.01, time.monotonic() - t_transfer)
                rate = bytes_sent / (elapsed * 1024.0)
                filled = int(pct // 5)
                bar = f"{C.GREEN}{'#' * filled}{C.DIM}{'-' * (20 - filled)}{C.RESET}"
                sys.stdout.write(
                    f"\r [{bar}] {C.BOLD}{pct:5.1f}%{C.RESET} ({i + 1}/{total_blocks}) - {C.CYAN}{rate:.1f} KB/s{C.RESET} - {elapsed:.1f}s"
                )
                sys.stdout.flush()

        print(f"\n {C.ok(f'All {total_blocks} blocks transferred in {time.monotonic() - t_transfer:.1f}s!')}")

        # ------------------------------------------------------------------
        # Step 6 & 7: RequestTransferExit (0x37) + Routine 0xFD02 (Finalize)
        # ------------------------------------------------------------------
        print(f"\n{C.hdr('[Step 6]')} RequestTransferExit (0x37)...")
        resp = flasher.execute_uds(bytes([0x37]), timeout=2.0)
        if resp[0] != 0x77:
            raise RuntimeError(f"RequestTransferExit failed: {resp.hex()}")
        print(f" {C.ok('Transfer exit accepted!')}")

        print(f"\n{C.hdr('[Step 7]')} Routine 0xFD02: Validating image & switching boot partition...")
        t0 = time.monotonic()
        resp = flasher.execute_uds(bytes([0x31, 0x01, 0xFD, 0x02, 0x01, 0x00]), timeout=12.0)
        if resp[0] != 0x71 or resp[1] != 0x01 or resp[2:4] != b"\xFD\x02":
            raise RuntimeError(f"Routine 0xFD02 failed: {resp.hex()}")
        if len(resp) >= 5 and resp[4] != 0x00:
            raise RuntimeError(f"Routine 0xFD02 validation error: status 0x{resp[4]:02X}")
        boot_switched = True
        print(f" {C.ok(f'Image validated & boot partition updated in {time.monotonic() - t0:.2f}s!')}")

        # ------------------------------------------------------------------
        # Step 8: Reboot & verify active partition (0xFD0B) + new firmware
        # ------------------------------------------------------------------
        print(f"\n{C.hdr('[Step 8]')} Rebooting Gateway (0x11 0x01) & waiting for boot...")
        flasher.hard_reset()
        time.sleep(2.0)

        post_uds_ok = False
        boot_deadline = time.monotonic() + 6.0
        while time.monotonic() < boot_deadline:
            try:
                resp = flasher.execute_uds(bytes([0x10, 0x01]), timeout=0.8)
                if len(resp) >= 2 and resp[0] == 0x50:
                    post_uds_ok = True
                    break
            except Exception:
                time.sleep(0.3)

        post_part_id: int | None = None
        active_part_str = "unknown"
        post_fw_str = "<unknown>"
        post_is_fareflash = False

        if post_uds_ok:
            for _ in range(3):
                raw_part = flasher.read_did(0xFD0B, timeout=0.8)
                if raw_part and len(raw_part) >= 1 and raw_part[0] > 0:
                    post_part_id = raw_part[0]
                    active_part_str = PARTITION_NAMES.get(post_part_id, f"Partition {post_part_id}")
                    break
                time.sleep(0.3)

            raw_post_ff = flasher.read_did(0xEE01, timeout=0.8)
            if raw_post_ff:
                post_fw_str = parse_ascii_field(raw_post_ff, 48)
                post_is_fareflash = bool(post_fw_str)
            if not post_is_fareflash:
                raw_post_sw = flasher.read_did(0xF194, timeout=0.8)
                if raw_post_sw:
                    post_fw_str = parse_ascii_field(raw_post_sw, 32) or "<unknown>"

        hb = flasher.read_heartbeat(timeout=3.0)
        print(f" Post-Boot UDS (0x780):     {C.ok('OK') if post_uds_ok else C.err('NO RESPONSE')}")
        print(f" Booted Partition (0xFD0B): {C.BOLD}{active_part_str}{C.RESET}")
        print(f" Running Firmware:          {C.CYAN}{C.BOLD}{post_fw_str}{C.RESET}")
        if hb:
            print(f" Heartbeat (0x6F0):         {parse_heartbeat(hb)}")

        partition_rollback = (
            pre_partition_id is not None and post_part_id is not None and post_part_id == pre_partition_id
        )
        if partition_rollback:
            print(
                f"\n{C.warn(f'[!] WARNING: Active partition ({active_part_str}) did NOT change after reboot!')}\n"
                "             The bootloader may have rolled back to the previous slot."
            )

        # ------------------------------------------------------------------
        # Step 9: Post-flash Serial/NVS verification & safe restore + DTC clear
        # ------------------------------------------------------------------
        print(f"\n{C.hdr('[Step 9]')} Post-flash Serial/NVS verification & DTC cleanup...")
        if not post_uds_ok:
            print(f" {C.warn('Warning:')} Skipping Serial/NVS check & DTC cleanup because Gateway did not answer UDS after reboot.")
        else:
            post_ser_raw = flasher.read_did(0xF18C, timeout=1.0)
            post_ser = parse_ascii_field(post_ser_raw, 20)[:6] if post_ser_raw else ""
            post_hw_raw = flasher.read_did(0xF193, timeout=1.0)
            post_hw = parse_ascii_field(post_hw_raw, 20) if post_hw_raw else ""

            serial_differs = bool(saved_serial) and (post_ser != saved_serial)
            if not skip_nvs_restore and (serial_differs or (saved_eol and (not post_ser or not post_hw))):
                print(
                    f" {C.warn('Serial/HW mismatch after flash')} (Post='{post_ser}'/'{post_hw}', Saved='{saved_serial}'/'{saved_hw_rev}') -> Restoring EOL (0xFD00)..."
                )
                try:
                    eol_base: bytes | None = saved_eol
                    if eol_base is None:
                        try:
                            flasher.execute_uds(bytes([0x10, 0x03]), timeout=1.0)
                        except Exception:
                            pass
                        cur_eol = flasher.read_did(0xFD00, timeout=2.0)
                        if cur_eol and len(cur_eol) >= 159:
                            eol_base = cur_eol[:159]

                    if eol_base is None:
                        print(
                            f" {C.warn('Warning:')} Refusing to write 0xFD00 because no valid 159-byte EOL base block is available "
                            "(avoiding zeroing calibration/HW data)."
                        )
                    else:
                        eol_to_write = bytearray(eol_base)
                        if saved_serial:
                            eol_to_write[14:34] = saved_serial.encode("ascii").ljust(20, b"\x00")[:20]

                        flasher.unlock_security(0x69, POLY_LEVEL2, session=0x02)
                        if flasher.write_did(0xFD00, bytes(eol_to_write), timeout=3.0):
                            print(f" {C.ok(f'EOL block (0xFD00) restored with Serial {saved_serial!r}!')} Rebooting...")
                            flasher.hard_reset()
                            time.sleep(2.2)
                        else:
                            print(f" {C.warn('Warning:')} Failed to restore 0xFD00!")
                except Exception as e:
                    print(f" {C.warn('Warning:')} Post-flash NVS restore failed: {e}")
            else:
                print(f" {C.ok('Serial/NVS intact')} (Serial='{post_ser or '<none>'}', HW-Rev='{post_hw or '<none>'}').")

            if flasher.clear_dtcs():
                print(f" {C.ok('Post-flash Gateway DTCs cleared')} (0x14 FF FF FF).")
            else:
                print(f" {C.dim('Post-flash DTC clear skipped/not acknowledged.')}")

        if not post_uds_ok or partition_rollback:
            print(f"\n{C.warn('[!] FLASH FINISHED WITH WARNINGS — verify active partition & firmware above!')}")
            sys.exit(2)

        print(f"\n{C.ok(f'*** SUCCESS! Gateway flashed and running {post_fw_str} on {active_part_str} ***')}")

    except KeyboardInterrupt:
        print(f"\n{C.warn('[-] Interrupted by user.')}")
        if ota_started:
            print(" Sending HardReset (0x11 0x01) to clear Gateway OTA state...")
            flasher.hard_reset()
            if not boot_switched:
                print(f" {C.ok('Safe:')} Boot partition was NOT switched yet; the previous firmware remains intact.")
        sys.exit(130)
    except Exception as e:
        print(f"\n{C.err(f'[-] ERROR: {e}')}")
        if ota_started:
            print(" Sending HardReset (0x11 0x01) to clear any sticky Gateway OTA state...")
            flasher.hard_reset()
            if not boot_switched:
                print(f" {C.ok('Safe:')} Boot partition was NOT switched yet; the previous firmware remains intact. You can retry flashing.")
        sys.exit(1)
    finally:
        if keeper:
            keeper.stop()
        flasher.close()


def main():
    examples = """Examples:
  1. Safe Pre-Flight Check (read module info, Ubatt, Serial & EOL without flashing):
       python3 fantic_can_flasher.py --info

  2. Flash FaReFlash firmware over CAN (interactive confirmation, >= 12.0V required):
       python3 fantic_can_flasher.py FaReFlash_v.0.0.54-alpha.bin

  3. Control FaReFlash Wi-Fi SoftAP via CAN (without smartphone):
       python3 fantic_can_flasher.py --wifi-mode on        # On-Demand (10-min auto-off)
       python3 fantic_can_flasher.py --wifi-mode off       # Turn SoftAP off
       python3 fantic_can_flasher.py --wifi-mode persist   # Keep ON across reboots (bench)

  4. Bench / Advanced options:
       python3 fantic_can_flasher.py --iface can0 --wake-id 0x260 FaReFlash.bin
       python3 fantic_can_flasher.py --force FaReFlash.bin # Override <12V (USB dev-board) or V2.03
"""

    parser = argparse.ArgumentParser(
        prog="fantic_can_flasher.py",
        usage="%(prog)s [OPTIONS] [FIRMWARE.bin]",
        description="Fantic e-Shock / FaReFlash CAN-Bus UDS Firmware Flasher & Service Tool (500 kbit/s)",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    grp_act = parser.add_argument_group("Firmware & Actions")
    grp_act.add_argument("image", nargs="?", metavar="FIRMWARE.bin", help="Path to FaReFlash .bin firmware file")
    grp_act.add_argument("-f", "--file", dest="file_opt", metavar="PATH", help="Alternative flag for the .bin file")
    grp_act.add_argument(
        "--check-only",
        "--info",
        dest="check_only",
        action="store_true",
        help="Read module info, 0x6F0 Ubatt, partition & EOL backup without flashing",
    )
    grp_act.add_argument(
        "--wifi-mode",
        choices=("off", "on", "persist"),
        default=None,
        help="Control FaReFlash Wi-Fi SoftAP via CAN 0xFD0E (off, on=10m on-demand, persist)",
    )

    grp_can = parser.add_argument_group("CAN Bus & Wakeup")
    grp_can.add_argument("-i", "--iface", default=CAN_INTERFACE, help="SocketCAN interface (default: can0)")
    grp_can.add_argument(
        "--wake-id",
        type=lambda x: int(x, 0),
        default=None,
        metavar="ID",
        help="Explicit bench wakeup CAN ID (e.g. 0x260, 0x310, 0x2F2)",
    )

    grp_adv = parser.add_argument_group("Safety & Advanced")
    grp_adv.add_argument("-y", "--yes", action="store_true", help="Skip interactive confirmation prompt")
    grp_adv.add_argument(
        "--force",
        action="store_true",
        help="Override safety blocks (< 12.0V supply voltage or untested V2.03) — at your own risk",
    )
    grp_adv.add_argument("--skip-nvs-restore", action="store_true", help="Do not restore Serial/EOL after flash")
    grp_adv.add_argument("--no-color", action="store_true", help="Disable ANSI colored terminal output")

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    if args.no_color:
        C.configure(False)

    if args.wifi_mode is not None:
        mode_map = {"off": 0, "on": 1, "persist": 2}
        mval = mode_map[args.wifi_mode]
        flasher = CanFlasher(args.iface)
        try:
            flasher.unlock_security(0x35, POLY_LEVEL1, session=0x03)
            ok = flasher.write_did(0xFD0E, bytes([mval]), timeout=2.0)
            time.sleep(0.35)
            st = flasher.read_did(0xFD0E, timeout=1.5)
            if ok:
                print(f"{C.ok('[OK]')} Wi-Fi SoftAP mode set to '{args.wifi_mode}' ({mval}).")
            else:
                print(f"{C.err('[FAIL]')} Wi-Fi SoftAP request rejected (thermal/undervoltage lockout or unsupported).")
            if st and len(st) >= 4:
                print(
                    f"     0xFD0E Status: running={st[0]}, persistent={st[1]}, "
                    f"guard_flags=0x{st[2]:02X}, idle_rem_min={st[3]}"
                )
            sys.exit(0 if ok else 1)
        finally:
            flasher.close()

    img = args.file_opt or args.image
    if not args.check_only and not img:
        parser.error("Please provide a firmware .bin file, or use --info / --wifi-mode.")

    flash_firmware(
        img,
        interface=args.iface,
        skip_nvs_restore=args.skip_nvs_restore,
        check_only=args.check_only,
        assume_yes=args.yes,
        forced_wake_id=args.wake_id,
        force_unsupported=args.force,
    )


if __name__ == "__main__":
    main()
