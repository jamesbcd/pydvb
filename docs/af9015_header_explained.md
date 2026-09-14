# AF9015 Linux Driver — Header File (`af9015.h`) Explained

File: `drivers/media/usb/dvb-usb-v2/af9015.h`

Unlike `af9015.c`, this file has **no functions** — it's the "shape" file: includes, constants, and data-structure definitions that `af9015.c` (and nothing else) relies on. Think of it as the shared vocabulary the driver's code is written in.

---

## 1. Includes

```c
#include <linux/hash.h>
#include <linux/regmap.h>
#include "dvb_usb.h"
#include "af9013.h"
#include "dvb-pll.h"
#include "mt2060.h"
#include "qt1010.h"
#include "tda18271.h"
#include "mxl5005s.h"
#include "mc44s803.h"
#include "tda18218.h"
#include "mxl5007t.h"
```
Pulls in:
- `linux/hash.h`, `linux/regmap.h` — generic kernel helpers (hashing utilities, the register-map framework used for talking to the chip).
- `dvb_usb.h` — the generic DVB-over-USB framework this driver plugs into.
- `af9013.h` — the driver for the **demodulator** chip actually used alongside the AF9015 (the AF9015 is mainly a USB bridge; AF9013 does the real demodulation).
- The rest (`dvb-pll`, `mt2060`, `qt1010`, `tda18271`, `mxl5005s`, `mc44s803`, `tda18218`, `mxl5007t`) are drivers for the **various tuner chips** that different AF9015-based USB sticks might use. Since manufacturers paired the AF9015 with different tuners, the driver needs to know about all of them and pick the right one at runtime.

---

## 2. Firmware filename

```c
#define AF9015_FIRMWARE "dvb-usb-af9015.fw"
```
The name of the firmware blob (loaded via `af9015_download_firmware()` in the .c file) that the kernel's firmware loader looks for, typically under `/lib/firmware/`.

---

## 3. Fixed I2C addresses and USB timeout

```c
#define AF9015_I2C_EEPROM  0x50
#define AF9015_I2C_DEMOD   0x1c
#define AF9015_USB_TIMEOUT 2000
```
- `AF9015_I2C_EEPROM` — the I2C address where the device's configuration EEPROM lives.
- `AF9015_I2C_DEMOD` — the default I2C address of the (first) demodulator chip.
- `AF9015_USB_TIMEOUT` — how many milliseconds to wait before giving up on a USB control transfer.

---

## 4. EEPROM memory map

```c
#define AF9015_EEPROM_IR_MODE        0x18
#define AF9015_EEPROM_IR_REMOTE_TYPE 0x34
#define AF9015_EEPROM_TS_MODE        0x31
#define AF9015_EEPROM_DEMOD2_I2C     0x32

#define AF9015_EEPROM_SAW_BW1        0x35
#define AF9015_EEPROM_XTAL_TYPE1     0x36
#define AF9015_EEPROM_SPEC_INV1      0x37
#define AF9015_EEPROM_IF1L           0x38
#define AF9015_EEPROM_IF1H           0x39
#define AF9015_EEPROM_MT2060_IF1L    0x3a
#define AF9015_EEPROM_MT2060_IF1H    0x3b
#define AF9015_EEPROM_TUNER_ID1      0x3c

#define AF9015_EEPROM_SAW_BW2        0x45
#define AF9015_EEPROM_XTAL_TYPE2     0x46
#define AF9015_EEPROM_SPEC_INV2      0x47
#define AF9015_EEPROM_IF2L           0x48
#define AF9015_EEPROM_IF2H           0x49
#define AF9015_EEPROM_MT2060_IF2L    0x4a
#define AF9015_EEPROM_MT2060_IF2H    0x4b
#define AF9015_EEPROM_TUNER_ID2      0x4c

#define AF9015_EEPROM_OFFSET (AF9015_EEPROM_SAW_BW2 - AF9015_EEPROM_SAW_BW1)
```
Every one of these is a byte offset into the device's EEPROM, which `af9015_read_config()` (in the .c file) reads to figure out how the device is wired up:
- **IR settings** — which remote-control mode/type the device uses.
- **TS mode** — whether it's a single- or dual-tuner device.
- **A "1" set** (`...1`) — filter bandwidth, crystal type, spectrum inversion, intermediate frequency (high/low byte), MT2060-specific IF value, and tuner chip ID, all for the **first** tuner/demodulator.
- **A "2" set** (`...2`) — the exact same fields, but for the **second** tuner/demodulator on dual-tuner devices.
- **`AF9015_EEPROM_OFFSET`** — rather than duplicating all the read logic, the driver computes the constant gap between the "1" and "2" blocks (here, `0x45 - 0x35 = 0x10`) and just adds that offset when reading the second demodulator's settings — so one block of code in the .c file can serve both.

---

## 5. `struct req_t` — the USB command "envelope"

```c
struct req_t {
	u8  cmd;       /* [0] */
	/*  seq */     /* [1] */
	u8  i2c_addr;  /* [2] */
	u16 addr;      /* [3|4] */
	u8  mbox;      /* [5] */
	u8  addr_len;  /* [6] */
	u8  data_len;  /* [7] */
	u8  *data;
};
```
This is the struct every USB request to the device gets packed into before `af9015_ctrl_msg()` sends it. Each field maps to a specific byte position in the actual USB packet (noted in the comments):
- `cmd` — which operation to perform (see the `af9015_cmd` enum below).
- `i2c_addr` — the I2C address to target, if relevant.
- `addr` — a 16-bit register/memory address.
- `mbox` — an extra addressing byte the chip protocol uses ("mailbox").
- `addr_len` — how many bytes of `addr` are actually meaningful (0, 1, 2, or 3).
- `data_len` — how many bytes of payload data are being sent or expected back.
- `data` — pointer to the actual payload buffer.

---

## 6. `enum af9015_cmd` — command codes

```c
enum af9015_cmd {
	GET_CONFIG           = 0x10,
	DOWNLOAD_FIRMWARE    = 0x11,
	BOOT                 = 0x13,
	READ_MEMORY          = 0x20,
	WRITE_MEMORY         = 0x21,
	READ_WRITE_I2C       = 0x22,
	COPY_FIRMWARE        = 0x23,
	RECONNECT_USB        = 0x5a,
	WRITE_VIRTUAL_MEMORY = 0x26,
	GET_IR_CODE          = 0x27,
	READ_I2C,
	WRITE_I2C,
};
```
The numeric command codes the AF9015's own on-chip firmware understands over USB. Each corresponds directly to an operation you saw in `af9015.c`'s `af9015_ctrl_msg()` switch statement: ask for cold/warm state (`GET_CONFIG`), push firmware bytes (`DOWNLOAD_FIRMWARE`), start running firmware (`BOOT`), read/write raw device memory, do an I2C transaction, copy firmware between demodulators, force a USB re-enumeration, write to "virtual" memory regions, or read an IR code. `READ_I2C`/`WRITE_I2C` have no explicit numbers — they just auto-increment from `GET_IR_CODE`'s value (0x27, 0x28) since the driver internally remaps them onto `READ_WRITE_I2C` (0x22) with a direction bit before sending, rather than using these raw values.

---

## 7. `enum af9015_ir_mode` — IR receiver modes

```c
enum af9015_ir_mode {
	AF9015_IR_MODE_DISABLED = 0,
	AF9015_IR_MODE_HID,
	AF9015_IR_MODE_RLC,
	AF9015_IR_MODE_RC6,
	AF9015_IR_MODE_POLLING, /* just guess */
};
```
The possible ways the device's built-in infrared receiver can be configured to report keypresses: turned off, acting as a USB HID device, using "RLC" encoding, RC6 protocol, or plain polling. The driver reads this value out of EEPROM (`AF9015_EEPROM_IR_MODE`) to decide whether/how to set up remote-control support.

---

## 8. `struct af9015_state` — the driver's per-device memory

```c
#define BUF_LEN 63
struct af9015_state {
	struct regmap *regmap;
	u8 buf[BUF_LEN]; /* bulk USB control message */
	u8 ir_mode;
	u8 rc_repeat;
	u32 rc_keycode;
	u8 rc_last[4];
	bool rc_failed;
	u8 dual_mode;
	u8 seq; /* packet sequence number */
	u16 mt2060_if1[2];
	u16 firmware_size;
	u16 firmware_checksum;
	u32 eeprom_sum;
	struct af9013_platform_data af9013_pdata[2];
	struct i2c_client *demod_i2c_client[2];
	u8 af9013_i2c_addr[2];
	bool usb_ts_if_configured[2];

	/* for demod callback override */
	int (*set_frontend[2]) (struct dvb_frontend *fe);
	int (*read_status[2]) (struct dvb_frontend *fe, enum fe_status *status);
	int (*init[2]) (struct dvb_frontend *fe);
	int (*sleep[2]) (struct dvb_frontend *fe);
	int (*tuner_init[2]) (struct dvb_frontend *fe);
	int (*tuner_sleep[2]) (struct dvb_frontend *fe);
	struct mutex fe_mutex;
};
```
This is the single "everything about this particular plugged-in device" struct — one instance exists per AF9015 device and gets passed around throughout `af9015.c`. Field by field:

- **`regmap`** — handle to the regmap instance set up in `af9015_probe()`, used for simple register-style reads/writes.
- **`buf[BUF_LEN]`** — a reusable scratch buffer (63 bytes, the USB control-transfer payload limit) that `af9015_ctrl_msg()` uses to build outgoing packets and receive incoming ones.
- **`ir_mode`** — the IR mode read from EEPROM (one of the `af9015_ir_mode` values).
- **`rc_repeat`, `rc_last[4]`, `rc_failed`, `rc_keycode`** — bookkeeping for the remote-control polling logic: the last-seen repeat counter, the last decoded key bytes (to detect repeats), whether the last poll failed, and the most recently decoded scancode.
- **`dual_mode`** — whether this device has one or two tuners/demodulators (read from EEPROM's TS mode byte).
- **`seq`** — a sequence number incremented on every USB command, used by the chip to match requests with replies.
- **`mt2060_if1[2]`** — the MT2060 tuner's intermediate frequency value, stored separately for each of the (up to two) tuners, since it's read from EEPROM per-tuner.
- **`firmware_size`, `firmware_checksum`** — the size and checksum of the uploaded firmware, remembered so they can be passed along when copying firmware to a second demodulator.
- **`eeprom_sum`** — the hash of the whole EEPROM computed by `af9015_eeprom_hash()`, used later to auto-guess the remote-control layout.
- **`af9013_pdata[2]`** — the configuration struct handed to the AF9013 demodulator driver, one per possible tuner/demodulator.
- **`demod_i2c_client[2]`** — handles to the I2C client instances created when each demodulator was "probed"/attached, needed later to detach them cleanly.
- **`af9013_i2c_addr[2]`** — the actual I2C address of each demodulator (since dual-tuner devices use two different addresses).
- **`usb_ts_if_configured[2]`** — tracks whether the USB/TS streaming interface has already been set up for each adapter, so `af9015_streaming_ctrl()` only does that setup once.
- **The six function-pointer arrays** (`set_frontend`, `read_status`, `init`, `sleep`, `tuner_init`, `tuner_sleep`) — this is where the driver stashes the *original* demodulator/tuner callback functions before overwriting them with its own locking wrappers (explained in the .c file walkthrough). Having one slot per adapter (`[2]`) lets each of up to two demodulators/tuners keep its own saved originals.
- **`fe_mutex`** — the mutex all those locking wrapper functions use to make sure the firmware isn't interrupted mid-operation.

---

## 9. `enum af9015_remote` — known remote-control layouts

```c
enum af9015_remote {
	AF9015_REMOTE_NONE                    = 0,
	AF9015_REMOTE_A_LINK_DTU_M,
	AF9015_REMOTE_MSI_DIGIVOX_MINI_II_V3,
	AF9015_REMOTE_MYGICTV_U718,
	AF9015_REMOTE_DIGITTRADE_DVB_T,
	AF9015_REMOTE_AVERMEDIA_KS,
};
```
A short catalog of specific remote-control layouts known to ship with various AF9015-based products (A-Link DTU-m, MSI Digivox Mini II v3, MyGica TV U718, Digittrade DVB-T, AVerMedia KS). These values are used both as the module-parameter option numbers (`remote=N`) and as lookup keys in the `af9015_rc_setup_modparam[]` table back in the .c file, to select the matching Linux RC keymap.

---

## Big picture

Where `af9015.c` is *behavior* (what the driver does), `af9015.h` is *vocabulary* (what the driver's data looks like):

- **Includes** → what other chip drivers this one depends on.
- **`#define`s** → fixed addresses and EEPROM byte offsets — essentially a map of the device's configuration memory.
- **`req_t`** → the shape of a single USB command.
- **`af9015_cmd`** → what commands mean.
- **`af9015_ir_mode` / `af9015_remote`** → small classification enums for remote-control handling.
- **`af9015_state`** → the one big "state bag" that ties everything together for a given plugged-in device.
