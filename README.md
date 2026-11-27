# << COMING SOON >>

## FaReFlash — Firmware for Fantic ESP32-C3 BLE/CAN Gateway Modules

![Target](https://img.shields.io/badge/Target-ESP32--C3%20%28RISC--V%29-blue.svg)
![Bus](https://img.shields.io/badge/CAN--Bus-500%20kbit%2Fs%20ISO%2015765--2-orange.svg)
![Tested](https://img.shields.io/badge/Tested%20on-OEM%20V1.1%20only-yellow.svg)
![Status](https://img.shields.io/badge/Status-Hobby%20Project%20%2F%20Alpha-red.svg)

**FaReFlash** is a personal hobby project providing an independent, clean-room replacement firmware binary (`.bin`) for the ESP32-C3-based Bluetooth/CAN gateway module (*e-Conn Micro Fantic*, Part No. `30513` / `V1391005`) used in Fantic motorcycles.

It maintains full protocol compatibility with existing BLE service `0xE550` apps (such as [Fantic Analyzer](https://github.com/MarioLange1983/FanticAnalyzer)) while unlocking the unused hardware capabilities of the ESP32-C3 — including a local Wi-Fi diagnostic cockpit, live CAN bus monitoring with an interactive Bit-Finder, extended OBD-II live telemetry, BLE fault-code clearing, and Over-The-Air (OTA) updates.

> [!IMPORTANT]
> **Hobby Project & Independence Notice:**
> * This is a **non-commercial hobby project**, provided **"AS IS"** with **no warranty** — **use entirely at your own risk!**
> * FaReFlash is **not affiliated with, endorsed by, or supported by Fantic Motor S.p.A. or e-Shock S.r.l.**
> * **Firmware Compatibility Warning:** FaReFlash has so far **only been tested on modules running stock OEM firmware `V1.1`**. Newer modules shipped with **`V2.03`** (16-bit protocol) have **not been tested** and may use different security/bootloader protections — flashing on `V2.03` is currently unverified and needs further investigation.

---
<p align="center">
  <img src="https://github.com/user-attachments/assets/2889a4c7-240d-4ad5-9c6b-6b5420a28e54" width="450">
  <img src="https://github.com/user-attachments/assets/141ef37c-ea3e-4d5f-a5dc-3f183fc37f55" width="450">
</p>

<p align="center">
  <img src="https://github.com/user-attachments/assets/906c0d80-632f-4998-a0ac-75c39e1ab211" width="450">
  <img src="https://github.com/user-attachments/assets/194614de-f811-41b7-bf7d-eba55bda9368" width="450">
</p>

<p align="center">
  <img src="https://github.com/user-attachments/assets/e413e117-32c1-4aa7-b965-77e166b8ab7d" width="450">
  <img src="https://github.com/user-attachments/assets/e5bb1de6-05cd-4654-b886-b868ae6d2e18" width="450">
</p>

---

## Table of Contents

- [Why FaReFlash? (Comparison & Advantages)](#why-fareflash-comparison--advantages)
- [Feature List & How It Is Implemented](#feature-list--how-it-is-implemented)
- [Key Features](#key-features)
- [Supported Hardware & Vehicle Profiles](#supported-hardware--vehicle-profiles)
- [How to Install FaReFlash on Your Module](#how-to-install-fareflash-on-your-module)
  - [Important Warning: Sealed Module & One-Way Risk](#important-warning-sealed-module--one-way-risk)
  - [Initial Installation via CAN-Bus](#initial-installation-via-can-bus)
  - [Updating an Existing FaReFlash Installation (Web-OTA)](#updating-an-existing-fareflash-installation-web-ota)
  - [Returning to Stock Firmware](#returning-to-stock-firmware)
- [First Boot & Web Cockpit](#first-boot--web-cockpit)
- [Risks, Limitations & Safety Warnings](#risks-limitations--safety-warnings)
- [FaReFlash Custom BLE DIDs (`0xEE00` Namespace)](#fareflash-custom-ble-dids-0xee00-namespace)
- [Disclaimer & Regulatory Compliance](#disclaimer--regulatory-compliance)
- [Contact](#-contact)

---

## Why FaReFlash? (Comparison & Advantages)

The OEM gateway hardware is built around an **Espressif ESP32-C3-MINI-1** (160 MHz RISC-V, 400 KB SRAM, 4 MB Flash, Wi-Fi + BLE 5.0) paired with a **TI TCAN1146** Selective-Wake CAN transceiver. On stock firmware, several of these hardware capabilities remain unused.

| Feature / Capability | Stock OEM Firmware (`V1.1`) | FaReFlash (Clean-Room) |
| :--- | :---: | :---: |
| **BLE Telemetry Service (`0xE550`)** | ✅ Supported | ✅ 100% compatible |
| **Clear ECU Fault Codes (DTCs) via BLE (`0x14`)** | ❌ Not supported (CAN only) | ✅ Supported via BLE & Web UI |
| **Profile 1 (`XMF`/`XEF 125`) Gear, Load & Odometer** | ⚠️ Partial (`0xFF` / `0xFFFFFFFF`) | ✅ Fully populated |
| **OBD-II Mode `0x01` Live Sensors (MAP, IAT, Timing)** | ❌ Not supported | ✅ Polled & streamed (`0xEE20`–`0xEE24`) |
| **Gateway Health & Chip Temperature Monitoring** | ❌ CAN heartbeat only | ✅ Exposed via BLE (`0xEE11`) & Web UI |
| **Gateway 12-bit ADC Supply Voltage (`0x6F0`)** | ✅ CAN heartbeat only | ✅ 100% `0x6F0` parity + Web UI + `< 12.0 V` CAN flasher stop |
| **Autonomous Blackbox (Freeze-Frames, Battery & Engine Stats)** | ❌ Not available | ✅ 8-slot Freeze-Frames, Cranking/Charging check & lifetime stats (`0xEE12`/`0xEE13`) |
| **Built-in Wi-Fi SoftAP & Browser Cockpit** | ❌ Wi-Fi unused | ✅ Local Web UI (`192.168.4.1` / `fareflash.local`) |
| **On-Demand Wi-Fi with Thermal & Brownout Guard** | ❌ Not applicable | ✅ Default OFF, 13 dBm cap, 62/68 °C & 11.2 V guard, 10-min auto-off (`0xFD0E`) |
| **Live CAN Monitor, Trace & Interactive Bit-Finder** | ❌ Not available | ✅ Built into Web UI (`.csv` / `.log` export) |
| **Wireless CAN-over-Wi-Fi Bridge (`SavvyCAN / GVRET`)** | ❌ Not available | ✅ TCP Port 23 (`🔌 Bridge` tab), Read-Only default + optional TX toggle |
| **End-User OTA Updates (Web & BLE)** | ❌ CAN-bus only | ✅ Web-OTA via browser (~4 s) & BLE-OTA |
| **ISO-TP / UDS Bounds Checking** | ⚠️ Limited FF length validation | ✅ Strict length & bounds checks |
| **Selective-Wake Power-Off** | ✅ Supported | ✅ Supported + active-BLE sleep guard |
| **NVS Backup & Restore (`nvs_es`)** | ❌ Not available | ✅ 1-click download/restore in Web UI |

---

## Feature List & How It Is Implemented

Below is a technical overview of all core FaReFlash features and how they are engineered inside the ESP-IDF firmware (100% clean-room implementation on ESP32-C3):

| Feature | How It Is Implemented |
| :--- | :--- |
| **1. OEM-Compatible BLE Gateway (`0xE550`)** | Built on Espressif Bluedroid GATT Server exposing characteristics `0xE5C0`–`0xE5C4`, CRC-8 OpenSafety (`0x2F`) framing, CRC-16/MODBUS Security Access (`0x27`), dynamic C3 telemetry (`0xFD10`) and C4 DTC streaming (`0xFD11`), plus shortened 2.0 s link supervision timeout for instant reconnect after app restarts. |
| **2. Autonomous Blackbox (`0xEE12` / `0xEE13`)** | Runs at 5 Hz inside the gateway heartbeat loop. **Cranking & Charging Analyzer** detects starter voltage drops (`< 11.4 V` while engine starts), measures minimum cranking voltage, and tracks alternator charging voltage above 2,500 RPM (`OK / Weak / Low`). **Lifetime Engine Stats** accumulate engine run time, cold starts, peak RPM, and max coolant temp purely in RAM and flush to NVS only on sleep/shutdown or every 30 min of engine operation. **Freeze-Frame Ring** captures an 8-slot snapshot (RPM, speed, coolant, TPS, gear, `Ubatt`, chip temp, odometer, timestamp) on every newly appearing ECU DTC (`0x59 0x02`) or critical event (over-temp / brownout). Strictly stored in ESP-IDF's default `nvs` partition (`"blackbox"` namespace) — OEM `nvs_es` is never touched — with deduplication (existing DTC updates counter/timestamp in place), 60 s cooldown, and max 5 flash commits per ignition cycle to protect flash endurance. |
| **3. Wireless CAN-Bridge (`SavvyCAN / GVRET`)** | Optional TCP server on **Port 23** implementing the binary **GVRET** protocol (`0xE7` / `0xF1`). Disabled by default (`0 B` RAM/CPU overhead). When enabled in the Web Cockpit's **`🔌 Bridge`** tab, a dedicated FreeRTOS task and 64-frame non-blocking ring queue stream live TWAI frames to desktop tools like **SavvyCAN**. Starts **Read-Only** by default; CAN transmission requires an explicit expert toggle (auto-resets to Read-Only on disconnect/disable). Auto-stops when Wi-Fi shuts down or after 10 minutes without a TCP client. |
| **4. On-Demand Wi-Fi SoftAP + Thermal & Brownout Guard (`0xFD0E`)** | SoftAP is **OFF by default at boot** to keep heat and current draw minimal in the potted housing. Activated on demand from **Fantic Analyzer** (`0x2E 0xFD0E 0x01`) or CAN (`--wifi-mode on`) with a 10-minute inactivity auto-off timer. A 1 Hz supervisor caps TX power to **13.0 dBm**, throttles to **8.5 dBm** at `≥ 62 °C`, and shuts down Wi-Fi with lockout (`NRC 0x22`) at `≥ 68 °C` (`76 °C` during active Web-OTA; release `≤ 60 °C`) or when OEM `GPIO0` ADC `Ubatt < 11.2 V` for `≥ 2 s` (release `≥ 11.8 V`). |
| **5. Browser Diagnostic Cockpit & Bit-Finder** | Embedded single-page HTML/JS app served via `esp_http_server` with mDNS + UDP DNS fallback (`http://fareflash.local` / `192.168.4.1`) in 4 languages (`DE / EN / FR / ES`). The **CAN Monitor** feeds a lock-free ring buffer polled by the browser; Matrix, Trace (50k frames), `.csv`/`.log` export, and the **SavvyCAN-style `🔍 Bit-Finder`** (3-second baseline noise masking) execute entirely client-side in JavaScript for zero extra MCU load. |
| **6. Extended OBD-II Mode `0x01` Telemetry (`0xEE20`–`0xEE24`)** | A lightweight round-robin poller queries standard OBD-II PIDs (`0x0B` MAP, `0x0D` Speed, `0x0E` Timing Advance, `0x0F` IAT, `0x04`/`0x05`/`0x0C`/`0x11`/`0x42`) on `0x7DF → 0x7E8` at low bus load, caching values with a valid-PID bitmask (`0xEE20`) that can be subscribed directly inside the existing `0xE5C3` BLE live stream. |
| **7. Gateway Health, ADC Voltage & Internal DTCs (`0x6F0` / `0xEE11`)** | Samples the ESP32-C3 internal temperature sensor and the OEM voltage divider on `ADC1_CH0` (`GPIO0`, 12-bit curve-fitting calibration) to populate the 200 ms `0x6F0` CAN heartbeat (`5 mV/LSB`) and expose chip temperature, peak temperature, and all 6 OEM NVS DTC slots via `0xEE11` and Web UI. |
| **8. Dual-Slot OTA (Web, BLE & CAN) + Safety Guards** | Supports firmware updates over **HTTP Web-OTA** (~4 s), **BLE-OTA** (non-blocking `OTA_WITH_SEQUENTIAL_WRITES` erase so BLE never drops), and **CAN UDS** (`0x780 → 0x785`). Validates the ESP32-C3 image header before touching flash, preserves `nvs_es` (serial & calibration), and `fantic_can_flasher.py` aborts CAN flashing below **`12.0 V`** supply voltage (`0x6F0`). |
| **9. Hardened CAN/ISO-TP & Selective-Wake Sleep Guard** | Enforces strict ISO 15765-2 frame length/bounds checks and ISO 14229-1 functional NRC suppression on `0x7DF`. Drives the **TI TCAN1146** SBC over SPI2 for selective-wake power-off (`0xFD07 = 1`) after 10 s of CAN silence while inhibiting shutdown whenever a BLE client is actively connected. |

---

## Key Features

### 1. Full Protocol & App Compatibility
* Implements the complete BLE GATT service `0xE550` (`0xE5C0`–`0xE5C4`), standard CRC-8 OpenSafety framing, standard CRC-16/MODBUS-based Security Access (`0x27`), Live Telemetry Stream (`0xE5C3`), and Diagnostic/DTC Stream (`0xE5C4`).
* Works out-of-the-box with **Fantic Analyzer** (which automatically detects FaReFlash, displays the yellow lightning indicator in the top bar, and enables DTC clearing, Gateway Health, and extended OBD-II live data).
* Preserves your module's existing serial number, calibration data, and vehicle profile stored in `nvs_es`.

### 2. Built-in Wi-Fi Diagnostic Cockpit (DE / EN / FR / ES)
* Accessible via any smartphone or laptop browser at **`http://192.168.4.1`** or **`http://fareflash.local`** (built-in mDNS responder).
* **Dashboard Tab:** Live RPM, Speedo/Tacho (`km/h`), Gear, Throttle (`%`), Engine Load (`%`), Coolant Temp (`°C`), Battery Voltage (`V`), Odometer, Sidestand, plus OBD-II MAP (`kPa`), IAT (`°C`), and Ignition Timing (`° BTDC`).
* **Diagnostics Tab:** Live view of both Motorcycle ECU DTCs and internal Gateway DTCs, with 1-click fault memory reset.
* **CAN Monitor & Interactive `🔍 Bit-Finder` Tab:**
  * **Matrix Mode:** Real-time per-ID view with byte-change highlighting and cycle time (`Δt ms`).
  * **Trace Mode:** Chronological raw CAN frame log (up to 50,000 frames in browser memory) with ID filtering (`310, 500, 780-7DF, !6F0`) and export to SavvyCAN `.csv` or `candump .log`.
  * **Interactive `🔍 Bit-Finder` (SavvyCAN-style):** Runs 100% in your browser (zero extra load on the module). Click **Baseline (3s)** hands-off to automatically mask noisy bits (alive counters, CRCs, RPM jitter), then operate any lever or switch (clutch, brake, gear, turn signal) to highlight the exact changing byte and bit (`0→1` green, `1→0` red, toggled yellow) in real time.
* **Firmware & Config Tabs:** Dual-slot Web-OTA updater with binary header validation, NVS backup/restore, vehicle profile selection, and Wi-Fi SSID/password customization.
* **On-Demand Wi-Fi with Thermal & Brownout Protection:** To keep the sealed OEM module cool and prevent voltage-regulator brownouts, the Wi-Fi SoftAP is **OFF by default** at boot and activated on demand from **Fantic Analyzer** (Module card switch via `0xFD0E`) or over CAN (`--wifi-mode on`), with a 10-minute inactivity auto-off timer. TX power is capped at **13 dBm** (`8.5 dBm` thermal derating at `≥ 62 °C`), with automatic cutoff at **`≥ 68 °C`** (cooldown to `≤ 60 °C`; `76 °C` emergency cutoff during active Web-OTA) and undervoltage protection below **`11.2 V`** (release at `≥ 11.8 V`).

### 3. Hardened CAN/ISO-TP Stack & Smart Power Management
* Strict ISO 15765-2 length and bounds checking prevents memory corruption or unexpected reboots on malformed bus traffic.
* Proper ISO 14229-1 functional NRC suppression on `0x7DF` so external OBD-II testers never conflict with the gateway.
* Automatic power-off after ignition off (CAN silence), while automatically keeping the module awake if a BLE client is actively connected.

---

## Supported Hardware & Vehicle Profiles

### Hardware
* **Module:** `e-Conn Micro Fantic` (Part No. `30513` / `V1391005`), Hardware RevC (`CUM`), unknown 4-pin waterproof JST-style connector with slotted sides (`+12V`, `GND`, `CAN_H`, `CAN_L`).
* **Stock Firmware Requirement:** **Verified on `V1.1` only.** Modules with **`V2.03`** are untested and may be protected (needs investigation).

### Vehicle Profiles (`DID 0x0001` / `0xFD03`)

| Profile ID | VIN Prefix | Motorcycle Platform | Wakeup CAN ID |
| :---: | :--- | :--- | :---: |
| **`1`** (`0x0001`) | `ZFMFA13...` | Fantic XMF 125 / XEF 125 | `0x310` |
| **`4`** (`0x0004`) | `ZFMCA50...` | Fantic Caballero 500 (Scrambler, Deluxe, Rally, Flat Track, Explorer) | `0x260` |
| **`5`** (`0x0005`) | `ZFMCA13...` / `ZFMCA14...` | Fantic Caballero 125 (MY24 / MY25) | `0x260` |
| **`6`** (`0x0006`) | — | Reserved Combustion Profile (same CAN layout as 4/5) | `0x260` |
| **`11` / `12`** | `ZFMEM01...` / `ZFMEM02...` | Fantic Electric / Issimo Platform | `0x2F2` |

*(Note: Motorcycles with integrated Bluetooth in their TFT dashboard — such as the Caballero 700 — do **not** use this external gateway module.)*

---

## How to Install FaReFlash on Your Module

### Important Warning: Sealed Module & One-Way Risk

> [!CAUTION]
> **No Full Flash Backup Possible — Potential One-Way Operation:**
> The OEM module is permanently sealed in epoxy resin. There is **no external USB/UART port** and **no way to create a full flash backup** of the stock firmware before flashing.
>
> Initial installation on a sealed OEM module is **only possible over the CAN bus** using `fantic_can_flasher.py`. Although CAN-OTA writes into the `ota_0`/`ota_1` partition (leaving `factory` and `nvs_es` untouched), **if anything goes wrong during flashing or if the module fails to boot, there is no non-destructive recovery path.** Proceed only if you accept that flashing a sealed module can be a **one-way ticket**.

---

### Initial Installation via CAN-Bus

The module's stock `V1.1` firmware supports physical CAN-bus UDS flashing on `0x780 -> 0x785` (500 kbit/s), which writes the FaReFlash `.bin` directly into the inactive OTA partition (`ota_0` / `ota_1`).

#### Required Hardware
* A Linux PC with a SocketCAN USB-to-CAN adapter — tested and recommended: **DSD TECH SH-C31A USB-to-CAN Adapter** (`gs_usb` driver built into Linux, integrated switchable `120 Ω` bus termination).
* **In-Vehicle Connection via 6-Pin Euro 5 OBD (Only Safe & Recommended Method — No Module Removal):**
  * Connect directly to the motorcycle's **red 6-pin Euro 5 / Euro 5+ OBD diagnostic connector (ISO 19689)** using a simple 6-pin motorcycle OBD adapter (`CAN_H`, `CAN_L`, `GND`). Because the gateway module sits on the same 500 kbit/s CAN bus as the diagnostic port, **you do not need to remove the module from the bike** — and the motorcycle's ECUs keep the CAN bus continuously awake throughout the flash.
  * Turn **Ignition ON, Engine OFF** and **connect a 12 V battery charger** (the flasher enforces a **≥ 12.0 V** supply-voltage safety check via `0x6F0`; disconnect any BLE apps or other OBD scanners).
  * Set the **`120 Ω` termination switch on the DSD TECH SH-C31A to `OFF`** (the motorcycle wiring harness is already terminated).
* **Why Bench Flashing Is Not Practical / Not Recommended:**
  * The OEM module uses an **unknown 4-pin waterproof JST-style connector with slotted sides** (standard `JST 04R-JWPF-VSLE` plugs do **not** fit). Poking loose DuPont jumper wires directly into the tightly spaced pins carries a high risk of short-circuiting `+12V` to `GND` or `CAN` — **always flash cleanly in-vehicle via the 6-pin OBD diagnostic connector**.

<p align="center">
  <img src="https://github.com/user-attachments/assets/1e9f823f-3e8e-4eb1-bab5-6fd89ecaaa8a" alt="EURO5 to SH-C31A cable harness" width="300">
</p>

#### Steps
1. Bring up the SocketCAN interface at **500 kbit/s**:
   ```bash
   sudo ip link set can0 down
   sudo ip link set can0 up type can bitrate 500000
   ```
2. *(Recommended first)* Run a **non-destructive pre-flight check** to verify your connection, supply voltage (`Ubatt`), firmware version, and vehicle profile without modifying flash:
   ```bash
   python3 fantic_can_flasher.py --iface can0 --info
   ```
3. Run the Python CAN flasher with the pre-built FaReFlash `.bin` file:
   ```bash
   python3 fantic_can_flasher.py --iface can0 FaReFlash_v.0.0.56-alpha.bin
   ```
4. **Automated Safe Flash Workflow:** The script automatically handles every step:
   * **Bus & Wakeup Detection:** Passively checks whether the module is connected to a live motorcycle harness (with active ECU wakeup PIDs such as `0x260`/`0x310`/`0x2F2`). On a bench setup without ECU traffic, it automatically starts a background wakeup/keep-alive sender so the module stays awake throughout the entire flash.
   * **Module Info & Pre-Flight Verification:** Displays Firmware Version (`0xF194`/`0xF195`), Serial Number (`0xF18C`), HW Revision (`0xF193`), Vehicle Profile (`0xFD03`), Active Partition (`0xFD0B`), Gateway DTC count, and EOL backup status, verifies that flashing can be started safely (automatically blocking untested `V2.03` modules unless `--force` is explicitly passed), and asks for explicit confirmation before touching flash.
   * **Backup & Flash:** Validates the `.bin` file (`<= 1 MB`, ESP32-C3 header, XOR checksum & appended SHA-256 digest), backs up Serial (`0xF18C`), HW-Rev (`0xF193`), and EOL (`0xFD00`), clears pre-flash DTCs (`0x14 FF FF FF`), enters Programming Session (`0x10 0x02`), unlocks Security Access, erases the OTA slot (`0x31 0x01 0xFD00`, with automatic recovery reset if a stale OTA state exists), transfers the firmware via ISO-TP (`0x34` / `0x36` / `0x37`), validates the image (`0x31 0x01 0xFD02`), and reboots (`0x11 0x01`).
   * **Post-Flash Verification:** Waits for reboot completion, confirms that the active boot partition (`0xFD0B`) switched to the newly flashed slot (detecting silent boot rollbacks), displays the running firmware version, verifies that Serial Number & HW Revision are intact (**safely restoring them via `0x2E 0xFD00` only if altered and a valid 159-byte EOL backup exists**), and clears post-flash DTCs.

<p align="center">
  <img src="https://github.com/user-attachments/assets/1ae132b6-d3c7-4ee0-a47e-661a47cc5221" width="450">
</p>

---

### Updating an Existing FaReFlash Installation (Web-OTA)

Once FaReFlash is installed on the module, future updates require **no CAN adapter or cables** and take ~10 seconds directly from your browser:

1. Connect your smartphone or laptop to the module's Wi-Fi (`FaReFlash-<SERIAL>`).
2. Open **`http://192.168.4.1`** → **`🚀 Firmware`** tab.
3. Select the new `FaReFlash_<version>.bin` file, keep Target set to **Standard: Next free update slot**, and click **Install Firmware**.
4. The module validates the image header before erasing, writes the inactive OTA slot, and reboots automatically.

---

### Returning to Stock Firmware

Because initial CAN-bus flashing writes to `ota_0`/`ota_1`, your original OEM `V1.1` firmware remains stored in the `factory` partition. As long as FaReFlash boots and the Web Cockpit is reachable, you can switch the active boot partition back to **`factory (1)`** in the **`ℹ️ Module Info`** tab.

> [!CAUTION]
> **Never select "Overwrite Factory memory" in Web-OTA on a sealed OEM module** — overwriting the `factory` partition permanently destroys your stock fallback with no way to restore it.

---

## First Boot & Web Cockpit

1. Power on the motorcycle ignition (or 12 V bench supply).
2. **Bluetooth:** The module continues advertising as `FanticCON-<SERIAL>` (unchanged, so existing BLE pairings continue working).
3. **Wi-Fi SoftAP:**
   * **SSID:** `FaReFlash-<SERIAL>` (e.g., `FaReFlash-154204`)
   * **Default Password:** `fantic<SERIAL>` (WPA2-PSK, e.g., `fantic154204`)
   * **URL:** `http://192.168.4.1` or `http://fareflash.local`
4. **Recommended first step:** Open the **`ℹ️ Module Info`** tab and click **`💾 NVS Backup`** to download a copy of your module's serial number and calibration data.
5. **Security & Power Tip:** Change the default Wi-Fi password in **`⚙️ Config`**, or disable the Wi-Fi SoftAP in **`⚙️ Config`** if you only use Bluetooth telemetry.

---

## Risks, Limitations & Safety Warnings

> [!CAUTION]
> **Read carefully before flashing any vehicle electronics.**

1. **No Backup / Potential One-Way Operation:**
   * Sealed OEM modules cannot be backed up via UART/JTAG beforehand. A power loss, unstable CAN connection, or overwriting the `factory` partition can permanently brick the module with no non-destructive recovery path.
2. **Untested on Stock `V2.03` Modules:**
   * Only stock `V1.1` modules have been tested. Flashing `V2.03` modules is untested and may fail or behave unexpectedly.
3. **Power Consumption (Wi-Fi SoftAP):**
   * The Wi-Fi SoftAP draws more current than BLE alone while the ignition is on. If you only need Bluetooth telemetry, you can disable the Wi-Fi SoftAP in **`⚙️ Config`**.
4. **CAN-Bus Integrity:**
   * The module is connected to the motorcycle's primary 500 kbit/s CAN bus (shared with Engine ECU, Dashboard, and ABS). Experimental third-party firmware can never be guaranteed 100% bug-free — never flash or run experimental tests while riding.
5. **Rider Distraction:**
   * **Never interact with the Web Cockpit, Bit-Finder, or OTA uploader while operating a motorcycle.** Perform all diagnostic and configuration tasks only while safely parked.
6. **Warranty & Road Legality:**
   * Installing third-party firmware on an OEM electronic module **will void the manufacturer's warranty** for that component (and potentially related electrical claims). Depending on your jurisdiction (e.g., EU Whole Vehicle Type-Approval / German StVZO), modifying vehicle-connected electronics may affect road legality. You are solely responsible for compliance with local laws and regulations.

---

## FaReFlash Custom BLE DIDs (`0xEE00` Namespace)

All standard OEM DIDs (`0x0001`–`0x002C`, `0xE501`–`0xE506`) remain unchanged. FaReFlash adds an optional, backwards-compatible extension namespace:

| DID | Size | Access | Description |
| :--- | :---: | :---: | :--- |
| **`0xFD0E`** | 4 B / 1 B | Read (`0x22`) / Write (`0x2E`) | On-Demand Wi-Fi SoftAP Control & Thermal/Brownout Guard (`[running, persistent, guard_flags, idle_rem_min]`; Write `0=OFF`, `1=On-Demand`, `2=Persistent`) |
| **`0xEE01`** | Variable | Read (`0x22`) | FaReFlash Identity & Version string |
| **`0xEE10`** | Variable | Read (`0x22`) | Wi-Fi SoftAP status & active SSID |
| **`0xEE11`** | 37 B | Read (`0x22`) / Clear (`0x31 0x01 0xEE11`) | Gateway Health: Chip Temp (`0.1 °C`), Peak Temp (`0.1 °C`), Temp Status, 6 × 5-byte internal NVS DTC slots |
| **`0xEE12`** | 32 B | Read (`0x22`) | Autonomous Blackbox Stats: Ignition cycles, engine run seconds, cold starts, peak RPM, max coolant temp, last/min cranking voltage (`mV`), avg/max charging voltage (`mV`), battery & charging health flags, unseen Freeze-Frame count |
| **`0xEE13`** | `1 + N×22 B` | Read (`0x22`) / Routine (`0x31 0x01 0xEE13`) | Autonomous Blackbox Freeze-Frame Ring (up to 8 deduplicated snapshots: trigger type, source, 3-byte DTC, occurrence count, RPM, speed, coolant, TPS, gear, `Ubatt`, chip temp, odometer, uptime). Routine `opt=0x01` marks unseen as read; `opt=0xFF` clears ring |
| **`0xEE20`** | 2 B | Read / Stream (`0xE5C3`) | OBD-II Mode `0x01` Valid-PID Bitmask |
| **`0xEE21`** | 1 B | Read / Stream (`0xE5C3`) | OBD-II Manifold Absolute Pressure (MAP, `kPa`) |
| **`0xEE22`** | 1 B | Read / Stream (`0xE5C3`) | OBD-II Vehicle Speed (`km/h`) |
| **`0xEE23`** | 1 B | Read / Stream (`0xE5C3`) | OBD-II Intake Air Temperature (IAT, signed `°C`) |
| **`0xEE24`** | 2 B | Read / Stream (`0xE5C3`) | OBD-II Ignition Timing Advance (signed `0.1 ° BTDC`) |

---

## Disclaimer & Regulatory Compliance

* **Hobby Project / No Warranty ("AS-IS"):** This firmware binary and documentation are a private hobby project provided **"AS IS"**, without warranty of any kind, express or implied, including but not limited to merchantability, fitness for a particular purpose, and non-infringement. In no event shall the author or contributors be liable for any claim, damages, bricked hardware, vehicle malfunction, or other liability arising from its use.
* **AI-Assisted Development:** Artificial Intelligence (AI) tools were used during the development of this project to assist with protocol analysis, firmware implementation, tooling, and documentation.
* **Clean-Room & Interoperability Statement:** This firmware was developed independently from scratch based exclusively on externally observable bus and BLE protocol behavior on lawfully owned hardware (interoperability research under EU Directive 2009/24/EC Art. 6, German § 69e UrhG, and § 3 Abs. 1 Nr. 2 GeschGehG). No vendor trade secrets, proprietary firmware binaries, or copyrighted vendor files are included or distributed.
* **Trademarks:** *Fantic*, *Caballero*, *XMF*, *XEF*, *Issimo*, and *e-Shock* are registered trademarks of their respective owners and are referenced solely for compatibility identification.

---

## 📬 Contact

If you want to get in touch, please open an issue in this repository and leave your email address. I will get back to you!
