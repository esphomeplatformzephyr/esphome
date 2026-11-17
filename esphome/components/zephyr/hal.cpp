#ifdef USE_ZEPHYR

#include "esphome/core/defines.h"
#include "esphome/core/hal.h"

#include <cstdio>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/sys/reboot.h>
#if defined(USE_OTA_MCUBOOT) && !defined(USE_OTA_ROLLBACK)
#include <zephyr/dfu/mcuboot.h>
#endif
#ifdef USE_LOGGER
#include "esphome/components/logger/logger.h"
#endif

#ifdef USE_ZEPHYR_DWT_MICROS
#include <cmsis_core.h>
#endif

// Empty zephyr namespace block to satisfy ci-custom's lint_namespace check.
// HAL functions live in namespace esphome (root) — they are not part of the
// zephyr component's API.
namespace esphome::zephyr {}  // namespace esphome::zephyr

namespace esphome {

// Armed only when ESPHome configured it: platform: nrf52 always, Zephyr RTOS builds unless
// watchdog_timeout: 0. A user-enabled CONFIG_WATCHDOG alone must not arm a fallback timeout.
#if defined(CONFIG_WATCHDOG) && (defined(USE_WATCHDOG) || defined(USE_NRF52))
#define ESPHOME_ZEPHYR_ARM_WATCHDOG
#endif

#ifdef ESPHOME_ZEPHYR_ARM_WATCHDOG
static int wdt_channel_id = -1;  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)
static const device *const WDT = DEVICE_DT_GET(DT_ALIAS(watchdog0));

// Renesas RA, Silicon Labs EFR32 (wdt_gecko.c), and Silicon Labs SiWx91x
// (wdt_silabs_siwx91x.c) reject callback + WDT_FLAG_RESET_SOC together (-ENOTSUP),
// leaving the watchdog unarmed -- their single-channel watchdogs only support
// callback or hardware reset, not both.
#if defined(USE_ZEPHYR_WATCHDOG_TIMEOUT_MS) && defined(USE_LOGGER) && !defined(USE_ZEPHYR_VARIANT_FAMILY_RENESAS) && \
    !defined(USE_ZEPHYR_VARIANT_FAMILY_SILABS) && !defined(USE_ZEPHYR_VARIANT_FAMILY_SILABS_SIWX91X)
// Zephyr compiles arch_stack_walk() only where the architecture has it (not Xtensa;
// on Arm only with EXTRA_EXCEPTION_INFO).
#ifdef CONFIG_ARCH_STACKWALK
static bool wdt_log_stack_frame(void *cookie, unsigned long addr) {
  char msg[48];
  int len = snprintf(msg, sizeof(msg), "  at 0x%lx", addr);
  if (len > 0)
    logger::global_logger->write_watchdog_warning(msg, static_cast<uint16_t>(len));
  return true;  // keep walking until arch_stack_walk runs out of frames
}
#endif

// Watchdog stage0 callback, mirroring ESP-IDF's task-watchdog dump. Whether this
// reliably has time to run before the stage1 reset fires is an open question on
// this driver -- this is meant to gather evidence for the Zephyr issue.
static void wdt_warning_cb(const device *dev, int channel_id) {
  if (logger::global_logger == nullptr)
    return;
  k_tid_t current = k_current_get();
  const char *name = k_thread_name_get(current);
  char msg[80];
  int len =
      snprintf(msg, sizeof(msg), "Watchdog about to reset -- current thread: %s", name != nullptr ? name : "unknown");
  if (len > 0)
    logger::global_logger->write_watchdog_warning(msg, static_cast<uint16_t>(len));
#ifdef CONFIG_ARCH_STACKWALK
  arch_stack_walk(wdt_log_stack_frame, nullptr, current, nullptr);
#endif
}
#endif  // USE_ZEPHYR_WATCHDOG_TIMEOUT_MS && USE_LOGGER
#endif

// yield(), delay(), millis(), millis_64(), delayMicroseconds(), micros() except the cycle-counter one,
// arch_get_cpu_cycle_count(), arch_get_cpu_freq_hz() inlined in
// components/zephyr/hal.h.

#ifdef USE_ZEPHYR_DWT_MICROS
// The kernel clock counts in ticks of about 30 us, and the CPU cycle counter stops while the CPU sleeps.
// micros() adds the cycles counted since the last call. While the CPU stays busy the result and the kernel clock
// each trail the true time by less than a tick, so they differ by less than a tick either way. A sleep shorter
// than a tick can go unnoticed, so the result may trail the true time by up to two ticks until the next resync.
// The CPU cannot sleep while interrupts are locked, so a lag there comes from before the lock. It and any drift
// are left for the next unlocked call, which keeps differences inside an InterruptLock exact; InterruptLock makes that
// call just before it locks, so timestamps taken under the lock line up with later unlocked ones. A counter that has
// not moved since the last call is not running, and then the kernel clock is used even under a lock.

// One struct so the code loads a single base address.
static struct {
  uint32_t value;
  uint32_t last_cycles;  // Moves on by whole microseconds only, so the remainder carries over.
} micros_state;          // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

static constexpr uint32_t CYCLES_PER_US = 64;  // nRF52 cores always run at 64 MHz.
// Drift between unlocked calls stays far below this; a larger lead is a lag of over 2^31 us that wrapped. A lag
// within this of 2^32 us still looks like a lead and holds micros() for at most a second; that is accepted.
static constexpr int32_t MAX_LEAD_US = 1000000;

uint32_t micros() {
  const unsigned int key = irq_lock();
  const uint32_t cycles = DWT->CYCCNT;
  const uint32_t elapsed_us = (cycles - micros_state.last_cycles) / CYCLES_PER_US;
  micros_state.last_cycles += elapsed_us * CYCLES_PER_US;
  uint32_t value = micros_state.value + elapsed_us;

  const uint32_t kernel_us = k_ticks_to_us_floor32(k_uptime_ticks());
  // A tick, plus 1 us because kernel_us is rounded down.
  const int32_t tick_us = k_ticks_to_us_ceil32(1) + 1;
  const int32_t ahead = static_cast<int32_t>(value - kernel_us);
  const bool unlocked = arch_irq_unlocked(key);
  if ((ahead < -tick_us || ahead > MAX_LEAD_US) && (unlocked || elapsed_us == 0)) {
    // The CPU slept, so the cycle counter fell behind.
    value = kernel_us;
    micros_state.last_cycles = cycles;
  } else if (ahead >= tick_us && ahead <= MAX_LEAD_US && unlocked) {
    // The CPU clock can run fast against the RTC; cap the drift at one tick, but never below a result already
    // returned under a lock.
    const uint32_t cap = kernel_us + tick_us - 1;
    value = static_cast<int32_t>(cap - micros_state.value) > 0 ? cap : micros_state.value;
  }
  micros_state.value = value;
  irq_unlock(key);
  return value;
}
#endif

void arch_init() {
  main_loop_thread() = k_current_get();
#if defined(USE_OTA_MCUBOOT) && !defined(USE_OTA_ROLLBACK)
  // Without safe_mode nothing confirms the image later, so MCUboot would revert it
  if (!boot_is_img_confirmed()) {
    boot_write_img_confirmed();
  }
#endif
#ifdef USE_ZEPHYR_DWT_MICROS
  CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
  DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
#endif
#ifdef ESPHOME_ZEPHYR_ARM_WATCHDOG
  if (device_is_ready(WDT)) {
    static wdt_timeout_cfg wdt_config{};
    wdt_config.flags = WDT_FLAG_RESET_SOC;
    // Timeout value is a codegen decision (see each variant's to_code()), not a platform
    // #ifdef here -- except platform: nrf52/__init__.py sets a longer one for zigbee.
#ifdef USE_ZEPHYR_WATCHDOG_TIMEOUT_MS
    wdt_config.window.max = USE_ZEPHYR_WATCHDOG_TIMEOUT_MS;
    // some families can't handle the callback, review periodically for correctness
    // since this is behavior is driven by zephyr
#if defined(USE_LOGGER) && !defined(USE_ZEPHYR_VARIANT_FAMILY_RENESAS) && \
    !defined(USE_ZEPHYR_VARIANT_FAMILY_SILABS) && !defined(USE_ZEPHYR_VARIANT_FAMILY_SILABS_SIWX91X)
    wdt_config.callback = wdt_warning_cb;
#endif
#elif !defined(USE_BOOTLOADER_MCUBOOT)
    // Adafruit bootloader doesn't feed the WDT while
    // erasing flash during a firmware update, so a shorter timeout can break the update.
    wdt_config.window.max = 30000;
#elif defined(USE_ZIGBEE)
    // zboss thread uses a lot of CPU cycles during startup
    wdt_config.window.max = 10000;
#else
    wdt_config.window.max = 2000;
#endif
    wdt_channel_id = wdt_install_timeout(WDT, &wdt_config);
    if (wdt_channel_id >= 0) {
      // Paused while a debugger halts the CPU, so flashing over SWD is not cut off by a reset
      uint8_t options = WDT_OPT_PAUSE_HALTED_BY_DBG;
#ifdef USE_DEEP_SLEEP
      options |= WDT_OPT_PAUSE_IN_SLEEP;
#endif
      wdt_setup(WDT, options);
    }
  }
#endif
  // feed watchdog early. Otherwise OTA may rollback.
  arch_feed_wdt();
}

void arch_feed_wdt() {
#ifdef ESPHOME_ZEPHYR_ARM_WATCHDOG
  if (wdt_channel_id >= 0) {
    wdt_feed(WDT, wdt_channel_id);
  }
#endif
}

void arch_restart() { sys_reboot(SYS_REBOOT_COLD); }

}  // namespace esphome

#endif  // USE_ZEPHYR
