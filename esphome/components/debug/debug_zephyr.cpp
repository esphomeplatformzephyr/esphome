#include "debug_component.h"
#ifdef USE_ZEPHYR
#include <cinttypes>
#include <climits>
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include <esphome/components/zephyr/reset_reason.h>
#include <zephyr/kernel.h>
#if __has_include(<zephyr/version.h>)
#include <zephyr/version.h>
#elif __has_include(<version.h>)
#include <version.h>
#endif
#ifdef CONFIG_FLASH_MAP
#include <zephyr/storage/flash_map.h>
#endif
#ifdef CONFIG_STATS
#include <cstring>
#include <zephyr/stats/stats.h>
#endif
#ifdef CONFIG_PM_STATS
#include <zephyr/pm/state.h>
#endif
// NRF52_SERIES and NRF54L_SERIES: debug_nrf52.cpp and debug_nrf54l.cpp provide get_device_info_
#if __has_include(<nrfx.h>)
#include <nrfx.h>
#endif

namespace esphome::debug {

ESPHOME_LOG_TAG(TAG, "debug");

#ifdef CONFIG_THREAD_MONITOR
static void log_thread_cb(const struct k_thread *thread, void *) {
  const char *name = k_thread_name_get(const_cast<struct k_thread *>(thread));
  if (name == nullptr || name[0] == '\0') {
    name = "<unnamed>";
  }
#if defined(CONFIG_INIT_STACKS) && defined(CONFIG_THREAD_STACK_INFO)
  size_t unused = 0;
  k_thread_stack_space_get(thread, &unused);
  ESP_LOGD(TAG, "  thread %-20s  unused stack: %zu B", name, unused);
#else
  ESP_LOGD(TAG, "  thread %s", name);
#endif
}
#endif  // CONFIG_THREAD_MONITOR

#ifdef CONFIG_STATS
#ifdef CONFIG_PM_STATS
static const LogString *pm_state_name(int state) {
  switch (static_cast<enum pm_state>(state)) {
    case PM_STATE_ACTIVE:
      return LOG_STR("Active");
    case PM_STATE_RUNTIME_IDLE:
      return LOG_STR("Runtime Idle");
    case PM_STATE_SUSPEND_TO_IDLE:
      return LOG_STR("Suspend to Idle");
    case PM_STATE_STANDBY:
      return LOG_STR("Standby");
    case PM_STATE_SUSPEND_TO_RAM:
      return LOG_STR("Suspend to Ram");
    case PM_STATE_SUSPEND_TO_DISK:
      return LOG_STR("Suspend to Disk");
    case PM_STATE_SOFT_OFF:
      return LOG_STR("Soft Off");
    default:
      return LOG_STR("Unknown");
  }
}
#endif  // CONFIG_PM_STATS
static int log_stats_entry_cb(struct stats_hdr *hdr, void *arg, const char *name, uint16_t off) {
  const auto *addr = reinterpret_cast<const uint8_t *>(hdr) + off;
  uint64_t val = 0;
  switch (hdr->s_size) {
    case sizeof(uint16_t):
      val = *reinterpret_cast<const uint16_t *>(addr);
      break;
    case sizeof(uint32_t):
      val = *reinterpret_cast<const uint32_t *>(addr);
      break;
    case sizeof(uint64_t):
      val = *reinterpret_cast<const uint64_t *>(addr);
      break;
  }
  ESP_LOGD(TAG, "    %s: %" PRIu64, name, val);
  return 0;
}

static int log_stats_group_cb(struct stats_hdr *hdr, void *arg) {
  bool hdr_logged = false;
#ifdef CONFIG_PM_STATS
  // Fixed offsets -- pm_stats.c's group name format is fixed-width.
  if (strncmp(hdr->s_name, "pm_cpu_", 7) == 0 && strncmp(hdr->s_name + 10, "_state_", 7) == 0) {
    char cpu_buf[4] = {hdr->s_name[7], hdr->s_name[8], hdr->s_name[9], '\0'};
    char state_buf[2] = {hdr->s_name[17], '\0'};
    auto cpu = parse_number<uint8_t>(cpu_buf);
    auto state = parse_number<uint8_t>(state_buf);
    if (cpu.has_value() && state.has_value()) {
      ESP_LOGD(TAG, "  CPU %u %s:", *cpu, LOG_STR_ARG(pm_state_name(*state)));
      hdr_logged = true;
    }
  }
#endif  // CONFIG_PM_STATS
  if (!hdr_logged) {
    ESP_LOGD(TAG, "  %s:", hdr->s_name);
  }
  return stats_walk(hdr, log_stats_entry_cb, arg);
}
#endif  // CONFIG_STATS

const char *DebugComponent::get_reset_reason_(std::span<char, RESET_REASON_BUFFER_SIZE> buffer) {
  const char *buf = zephyr::get_reset_reason(buffer);
  ESP_LOGD(TAG, "Reset Reason: %s", buf);
  return buf;
}

const char *DebugComponent::get_wakeup_cause_(std::span<char, WAKEUP_CAUSE_BUFFER_SIZE> buffer) {
  // Zephyr doesn't have detailed wakeup cause like ESP32
  return "";
}

uint32_t DebugComponent::get_free_heap_() { return INT_MAX; }

#ifdef CONFIG_FLASH_MAP
static void fa_cb(const struct flash_area *fa, void *user_data) {
#if CONFIG_FLASH_MAP_LABELS
  const char *fa_label = flash_area_label(fa);

  if (fa_label == nullptr) {
    fa_label = "-";
  }
  ESP_LOGCONFIG(TAG, "%2d   0x%0*" PRIxPTR "   %-26s  %-24.24s  0x%-10x 0x%-12x", (int) fa->fa_id,
                sizeof(uintptr_t) * 2, (uintptr_t) fa->fa_dev, fa->fa_dev->name, fa_label, (uint32_t) fa->fa_off,
                fa->fa_size);
#else
  ESP_LOGCONFIG(TAG, "%2d   0x%0*" PRIxPTR "   %-26s  0x%-10x 0x%-12x", (int) fa->fa_id, sizeof(uintptr_t) * 2,
                (uintptr_t) fa->fa_dev, fa->fa_dev->name, (uint32_t) fa->fa_off, fa->fa_size);
#endif
}
#endif  // CONFIG_FLASH_MAP

void DebugComponent::log_partition_info_() {
#ifdef CONFIG_FLASH_MAP
#if CONFIG_FLASH_MAP_LABELS
  ESP_LOGCONFIG(TAG, "ID | Device     | Device Name               "
                     "| Label                   | Offset     | Size");
  ESP_LOGCONFIG(TAG, "--------------------------------------------"
                     "-----------------------------------------------");
#else
  ESP_LOGCONFIG(TAG, "ID | Device     | Device Name               "
                     "| Offset     | Size");
  ESP_LOGCONFIG(TAG, "-----------------------------------------"
                     "------------------------------");
#endif
  flash_area_foreach(fa_cb, nullptr);
#endif  // CONFIG_FLASH_MAP
  // Here, not in get_device_info_, so nRF chips with their own get_device_info_ log them too
#ifdef CONFIG_THREAD_MONITOR
  ESP_LOGD(TAG, "Threads:");
  k_thread_foreach(log_thread_cb, nullptr);
#endif
#ifdef CONFIG_STATS
  ESP_LOGD(TAG, "Stats:");
  stats_group_walk(log_stats_group_cb, nullptr);
#endif
}

#if !defined(NRF52_SERIES) && !defined(NRF54L_SERIES)
size_t DebugComponent::get_device_info_(std::span<char, DEVICE_INFO_BUFFER_SIZE> buffer, size_t pos) {
  constexpr size_t size = DEVICE_INFO_BUFFER_SIZE;
  char *buf = buffer.data();

  ESP_LOGD(TAG, "Zephyr: %s", KERNEL_VERSION_STRING);
  pos = buf_append_str(buf, size, pos, "|Zephyr: ");
  pos = buf_append_str(buf, size, pos, KERNEL_VERSION_STRING);

  uint32_t cpu_freq = arch_get_cpu_freq_hz();
  ESP_LOGD(TAG, "Cycle counter: %" PRIu32 " Hz", cpu_freq);
  pos = buf_append_printf(buf, size, pos, "|Cycle counter: %" PRIu32 " Hz", cpu_freq);
  return pos;
}
#endif

// The Python side enables the statistics only for a verbose logger
#ifdef CONFIG_SCHED_THREAD_USAGE_ANALYSIS
static void log_thread_usage(const struct k_thread *thread, void *user_data) {
  const auto cpu_cycles = *static_cast<const uint64_t *>(user_data);
  auto *tid = const_cast<k_tid_t>(thread);  // NOLINT(cppcoreguidelines-pro-type-const-cast)
  k_thread_runtime_stats_t stats;
  if (k_thread_runtime_stats_get(tid, &stats) != 0)
    return;
  char state[32];
  const char *name = k_thread_name_get(tid);
  ESP_LOGV(TAG, "  %-16s prio %3d %-10s %3u%% peak %" PRIu32 " avg %" PRIu32, name != nullptr ? name : "?",
           thread->base.prio, k_thread_state_str(tid, state, sizeof(state)),
           static_cast<unsigned>(stats.execution_cycles * 100 / cpu_cycles),
           static_cast<uint32_t>(k_cyc_to_us_floor64(stats.peak_cycles)),
           static_cast<uint32_t>(k_cyc_to_us_floor64(stats.average_cycles)));
}
#endif

void DebugComponent::update_platform_() {
#ifdef CONFIG_SCHED_THREAD_USAGE_ANALYSIS
  k_thread_runtime_stats_t cpu;
  if (k_thread_runtime_stats_all_get(&cpu) != 0 || cpu.execution_cycles == 0)
    return;
  ESP_LOGV(TAG, "Thread CPU usage since boot (peak and average us per time slice):");
  // The unlocked walk lets the log output inside the callback block
  k_thread_foreach_unlocked(log_thread_usage, &cpu.execution_cycles);
#endif
}

}  // namespace esphome::debug
#endif
