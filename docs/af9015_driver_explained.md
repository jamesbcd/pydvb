# AF9015 Linux Driver — Function-by-Function Explanation

File: `drivers/media/usb/dvb-usb-v2/af9015.c`
Purpose: Linux kernel driver for the **Afatech AF9015**, a chip used in USB DVB-T (terrestrial digital TV) receiver dongles. It handles talking to the chip over USB, loading its firmware, reading its configuration, and wiring up the demodulator, tuner, and remote control.

---

## 1. Low-level USB command layer

### `af9015_ctrl_msg()`
The heart of the driver. Every other function that talks to the hardware goes through this one. It packages a request (`req_t`) — a command byte, an I2C address, a memory address, and any data — into the exact byte format the AF9015 expects, sends it over USB, waits for the reply, checks a status byte, and copies read data back. It also does bounds-checking so no buffer overflows happen.

### `af9015_write_reg_i2c()`
A convenience wrapper: builds a "write one byte to this I2C address/register" request and calls `af9015_ctrl_msg()`. Picks the correct address length depending on whether the target is one of the two demodulators.

### `af9015_read_reg_i2c()`
Same idea as above, but for reading a single byte from an I2C register.

---

## 2. I2C bus emulation

The AF9015's USB interface can be used as a virtual I2C bus, since the demodulator and tuner sit behind I2C internally.

### `af9015_i2c_xfer()`
Implements the standard Linux `i2c_algorithm` transfer function. It looks at the incoming I2C message(s) — write-only, write-then-read, or read-only — figures out the register address/length from the message bytes, and translates it into the right `af9015_ctrl_msg()` call. Also handles the tricky case of **two demodulators sharing the same I2C address**, which the AF9015 disambiguates via its own gating logic rather than the address itself.

### `af9015_i2c_func()`
Trivial: reports back to the I2C core which capabilities this bus supports (basic I2C, no SMBus extras).

### `af9015_i2c_algo` (struct, not a function)
Bundles the two functions above into the structure the I2C core expects.

---

## 3. Device identification & firmware

### `af9015_identify_state()`
Asks the device "are you cold (just plugged in, no firmware) or warm (firmware already loaded)?" by sending a `GET_CONFIG` request and checking the reply byte.

### `af9015_download_firmware()`
Uploads the firmware image to the device in chunks (since a single USB control transfer has a maximum payload size), computes a checksum as it goes, and finally sends a `BOOT` command to make the device start running the uploaded code.

### `af9015_copy_firmware()`
Used only on dual-tuner devices. The firmware only needs to be downloaded to the "master" demodulator; this function copies it internally from the master to the second ("slave") demodulator over I2C, temporarily speeding up the I2C clock to make the copy faster, then boots it and polls a status register until the second demodulator reports it's running (or times out/fails).

---

## 4. EEPROM & configuration reading

### `af9015_eeprom_hash()`
Reads the entire on-board EEPROM (256 bytes) one byte at a time and computes a hash/checksum of its contents. This hash is later used to auto-detect which remote-control layout the device ships with, since many rebranded devices don't reliably declare it elsewhere.

### `af9015_read_config()`
Reads all the important settings out of EEPROM: IR remote mode, whether it's a single- or dual-tuner device, crystal frequency, intermediate frequency, tuner chip ID, etc. It uses the tuner ID to decide device-specific parameters like spectrum inversion. It also contains a manual override for a couple of AVerMedia devices known to ship with bad/incorrect EEPROM data.

### `af9015_get_stream_config()`
Tells the USB streaming layer what buffer size to use for the video/data stream, adjusting it if the device is connected via USB Full Speed instead of High Speed.

### `af9015_get_adapter_count()`
Simply reports whether this device exposes one or two DVB adapters (i.e., single or dual tuner).

---

## 5. Streaming control

### `af9015_streaming_ctrl()`
Turns the transport-stream (TV data) flow on or off for a given adapter. On first use it configures USB endpoint buffer sizes and stream-interface settings; after that, it flips hardware bits to enable/disable/reset the relevant USB endpoint (there's separate logic for adapter 0 vs. adapter 1, since dual-tuner models use two endpoints).

---

## 6. Frontend/tuner callback wrappers (locking)

The AF9015 firmware apparently misbehaves if I2C operations interrupt certain critical demodulator/tuner operations. So the driver wraps the demodulator and tuner driver's own callback functions with a mutex, saving the "real" function pointer and substituting its own locking wrapper:

- **`af9015_af9013_set_frontend()`** — locks, calls the real `set_frontend`, unlocks.
- **`af9015_af9013_read_status()`** — locks, calls the real `read_status`, unlocks.
- **`af9015_af9013_init()`** — locks, calls the real demodulator `init`, unlocks.
- **`af9015_af9013_sleep()`** — locks, calls the real demodulator `sleep`, unlocks.
- **`af9015_tuner_init()`** — locks, calls the real tuner `init`, unlocks.
- **`af9015_tuner_sleep()`** — locks, calls the real tuner `sleep`, unlocks.

Each just protects the underlying driver call with `state->fe_mutex` so it can't be interrupted by a concurrent I2C access (e.g. remote-control polling).

---

## 7. Attaching sub-drivers (demodulator, tuner)

### `af9015_af9013_frontend_attach()`
Sets up demodulator-specific parameters (TS mode, GPIO usage, API version) for whichever adapter is being initialized. For the second adapter on dual-tuner devices, it triggers `af9015_copy_firmware()` to get the second demodulator running. Then it "probes" (loads) the separate `af9013` demodulator driver module via I2C, retrieves its `dvb_frontend` handle, and installs the locking wrappers from section 6 over its callbacks.

### `af9015_frontend_detach()`
The cleanup counterpart — releases the I2C demodulator module that was loaded by the attach function above.

### `af9015_tuner_attach()`
Looks at which physical tuner chip this device has (read earlier from EEPROM) and calls the matching tuner driver's `_attach()` function — supporting MT2060, QT1010, TDA18271, TDA18218, MXL5003/5005/5007T, ENV77H11D5(dvb-pll), or MC44S803. Also installs the tuner locking wrappers (section 6) if the attached tuner driver supports init/sleep.

Several `static struct ..._config` blocks just above this function (e.g. `af9015_mt2060_config`, `af9015_qt1010_config`, etc.) aren't functions — they're the hardware-specific configuration structs (I2C addresses, IF frequencies, etc.) each tuner driver needs, and are just data passed into the attach calls.

---

## 8. PID filtering

### `af9015_pid_filter_ctrl()`
Enables or disables hardware PID filtering (a feature that lets the demodulator drop unwanted transport-stream packets before they reach the PC, saving bandwidth). It locks the mutex and forwards the call into the demodulator driver's own PID-filter-control function.

### `af9015_pid_filter()`
Adds or removes a specific PID (packet identifier, i.e. a particular TV channel/stream) from the filter list, again just forwarding into the demodulator driver under the mutex.

---

## 9. Device init

### `af9015_init()`
Runs once per device at startup. Initializes the mutex used throughout, and writes a "canary" byte to a register that the remote-control polling logic later checks to detect whether a new key has been pressed.

---

## 10. Remote control (IR) handling

*(Only compiled in if `CONFIG_RC_CORE` is enabled.)*

### `af9015_rc_setup_match()`
A small helper: given an ID number and a lookup table, returns the matching remote-control keymap name, or `NULL` if not found.

### `af9015_rc_query()`
Called periodically by the RC core to poll for a keypress. Reads 17 status/data bytes from the device, checks a "canary" byte to see whether a *new* key event happened since the last poll, decides whether it's a repeat of the previous key, decodes the scancode according to which NEC variant (plain NEC, extended NEC, or 32-bit NEC) the bit pattern matches, and reports the keypress (or repeat) to the Linux input/RC subsystem. Also has basic error-tolerance so one bad poll doesn't kill RC polling entirely.

### `af9015_get_rc_config()`
Figures out which remote-control keymap to load for this particular device, trying — in order — a manually specified module parameter, a match against the EEPROM hash computed earlier, and finally a fallback based on the USB "manufacturer" string for older devices. Falls back to an empty map if nothing matches. Also sets which IR protocols are supported (NEC variants) and the polling interval.

*(`af9015_rc_setup_modparam[]` and `af9015_rc_setup_hashes[]` are just static lookup tables used by the two functions above, not functions themselves.)*

---

## 11. Regmap glue

The driver uses the kernel's generic `regmap` framework (used for register-style device access) as a thin layer over its own USB commands.

### `af9015_regmap_write()`
Translates a generic regmap "write these bytes to this register" call into a `WRITE_MEMORY` `af9015_ctrl_msg()` request.

### `af9015_regmap_read()`
Same idea for reads: translates a regmap read request into a `READ_MEMORY` `af9015_ctrl_msg()` request.

---

## 12. Device lifecycle

### `af9015_probe()`
Called when the device is first plugged in and matched. Reads the USB "manufacturer" string to disambiguate a case where one particular TerraTec USB ID is shared by two different, incompatible chipsets (AF9015 vs. IT9135) — bailing out if it detects the wrong chip. Then sets up the `regmap` instance (wiring in the read/write functions from section 11) that the rest of the driver will use.

### `af9015_disconnect()`
Cleanup when the device is unplugged or the driver unloads: tears down the regmap instance created in `probe()`.

---

## 13. Driver registration (data structures, not functions)

- **`af9015_props`** — the big properties struct that tells the generic `dvb-usb-v2` framework how to drive this specific device: which functions above to call for firmware download, config reading, frontend/tuner attach, streaming control, PID filtering, remote control, etc.
- **`af9015_id_table[]`** — the list of USB vendor/product IDs this driver claims to support (i.e., all the different rebranded dongles built around the AF9015).
- **`af9015_usb_driver`** — the standard Linux `usb_driver` struct tying the ID table and probe/disconnect functions into the USB core.
- **`module_usb_driver(af9015_usb_driver)`** — a kernel macro that auto-generates the module's `init`/`exit` functions to register/unregister the USB driver.

---

## Big picture

Reading top to bottom, the file layers up like this:

1. **Raw USB protocol** (`ctrl_msg`) →
2. **I2C emulation over that protocol** (`i2c_xfer`) →
3. **Firmware upload and EEPROM/config parsing** →
4. **Streaming control** →
5. **Locking wrappers so nothing interrupts the firmware mid-operation** →
6. **Attaching the actual demodulator and tuner drivers** (PID filtering, RC, regmap) →
7. **Standard Linux USB driver registration glue** tying it all together.
