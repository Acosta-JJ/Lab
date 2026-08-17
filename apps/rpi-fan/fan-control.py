"""Fan control for the Raspberry Pi 5 Active Cooler under Talos.

Talos runs the mainline kernel, which does not ship the pwm-rp1 driver, so
Linux never registers a cooling device and the fan never spins. This drives the
RP1 PWM hardware directly through the chip's PCI BAR.

Register layout and offsets are taken verbatim from Sung-jin Hong's script
(0BSD, https://gist.github.com/serialx/d3213768026b15222ec8b86d92c06819), which
derives them from the kernel sources pwm-rp1.c, clk-rp1.c and rp1.dtsi. Do not
"tidy" the constants: the RP1 also hosts ethernet and USB, and a wrong offset
writes into a peripheral this cluster depends on.

Remove this once mainline ships pwm-rp1 — the kernel driver and this would then
fight over the same peripheral.
"""

import mmap
import os
import signal
import struct
import sys
import time

RESOURCE1 = "/host-sys/bus/pci/devices/0002:01:00.0/resource1"
THERMAL = "/host-sys/class/thermal/thermal_zone0/temp"
INTERVAL = 10

# Set FAN_SPEED to pin the fan at a fixed percentage and ignore the curve.
# Unset it to go back to temperature-driven control.
FIXED_SPEED = os.environ.get("FAN_SPEED")

# Raspberry Pi's own fan curve: (temperature, speed %). Below the first entry
# the fan is off. HYSTERESIS keeps it from oscillating around a threshold.
CURVE = [(50, 30), (60, 50), (67, 70), (75, 100)]
HYSTERESIS = 5

# --- RP1 clock controller (base 0x18000) ---
CLK_PWM1_CTRL = 0x18084
CLK_PWM1_DIV_INT = 0x18088
CLK_PWM1_DIV_FRAC = 0x1808C
CLK_PWM1_SEL = 0x18090
CLK_CTRL_ENABLE = 1 << 11
AUXSRC_XOSC = 2

# --- RP1 GPIO (base 0xd0000). Pin 45, bank 2, local pin 11, FUNCSEL 0 = pwm1 ---
GPIO45_CTRL = 0xD0000 + 0x8000 + 11 * 8 + 4
PWM_FUNCSEL = 0

# --- RP1 PWM1 (base 0x9c000), channel 3 ---
PWM1 = 0x9C000
CH = 3
GLOB_CTRL = PWM1 + 0x000
CHAN_CTRL = PWM1 + 0x014 + CH * 16
RANGE_REG = PWM1 + 0x018 + CH * 16
DUTY_REG = PWM1 + 0x020 + CH * 16

# 50 MHz clock, 20ns per tick, 41566ns period as the device tree specifies.
RANGE_TICKS = 41566 // 20

fd = os.open(RESOURCE1, os.O_RDWR | os.O_SYNC)
mm = mmap.mmap(fd, 4 * 1024 * 1024, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)


def read32(offset):
    return struct.unpack_from("<I", mm, offset)[0]


def write32(offset, val):
    struct.pack_into("<I", mm, offset, val)


def setup():
    """Point the PWM1 clock at the 50 MHz oscillator and put GPIO45 in PWM mode."""
    clk_ctrl = read32(CLK_PWM1_CTRL)
    write32(CLK_PWM1_CTRL, clk_ctrl & ~CLK_CTRL_ENABLE)
    write32(CLK_PWM1_DIV_INT, 1)
    write32(CLK_PWM1_DIV_FRAC, 0)

    clk_ctrl = read32(CLK_PWM1_CTRL)
    clk_ctrl &= ~0x3E1
    clk_ctrl |= AUXSRC_XOSC << 5
    clk_ctrl |= 1
    clk_ctrl |= CLK_CTRL_ENABLE
    write32(CLK_PWM1_CTRL, clk_ctrl)
    write32(CLK_PWM1_SEL, 1 << 1)

    ctrl = read32(GPIO45_CTRL)
    if (ctrl & 0x1F) != PWM_FUNCSEL:
        write32(GPIO45_CTRL, (ctrl & ~0x1F) | PWM_FUNCSEL)

    # BIT(8) FIFO_POP_MASK | BIT(3) POLARITY_INV | BIT(0) M/S mode.
    # Inverted polarity means duty 0 leaves the output low, i.e. fan off.
    write32(CHAN_CTRL, 0x109)
    write32(RANGE_REG, RANGE_TICKS)

    glob = read32(GLOB_CTRL)
    write32(GLOB_CTRL, glob | (1 << CH))


def set_speed(percent):
    write32(DUTY_REG, RANGE_TICKS * percent // 100)
    glob = read32(GLOB_CTRL)
    write32(GLOB_CTRL, glob | (1 << 31))


def temperature():
    with open(THERMAL) as f:
        return int(f.read().strip()) / 1000


def target_speed(temp, current):
    speed = 0
    for threshold, value in CURVE:
        if temp >= threshold:
            speed = value
    # Only step down once the temperature has dropped clear of the threshold.
    if speed < current:
        for threshold, value in CURVE:
            if value == current and temp > threshold - HYSTERESIS:
                return current
    return speed


def on_exit(signum, frame):
    """Leave the fan at full speed. If this process dies the hardware keeps its
    last duty cycle, so the safe thing to leave behind is maximum cooling."""
    print("shutting down, setting fan to 100%", flush=True)
    try:
        set_speed(100)
    finally:
        sys.exit(0)


signal.signal(signal.SIGTERM, on_exit)
signal.signal(signal.SIGINT, on_exit)

setup()

if FIXED_SPEED is not None:
    speed = max(0, min(100, int(FIXED_SPEED)))
    set_speed(speed)
    print(f"fan pinned at {speed}%, temperature curve disabled", flush=True)
    while True:
        # Rewrite periodically so the fan recovers if anything else disturbs the
        # PWM registers, and log the temperature for the dashboards.
        set_speed(speed)
        try:
            print(f"{temperature():.1f}C, fan held at {speed}%", flush=True)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(60)

print(f"fan control started, curve={CURVE} hysteresis={HYSTERESIS}C", flush=True)

current = 100
set_speed(current)

while True:
    try:
        temp = temperature()
    except Exception as exc:  # noqa: BLE001 - never stop cooling on a read error
        print(f"temperature read failed ({exc}), forcing 100%", flush=True)
        set_speed(100)
        current = 100
        time.sleep(INTERVAL)
        continue

    speed = target_speed(temp, current)
    if speed != current:
        print(f"{temp:.1f}C -> fan {speed}%", flush=True)
        set_speed(speed)
        current = speed
    time.sleep(INTERVAL)
