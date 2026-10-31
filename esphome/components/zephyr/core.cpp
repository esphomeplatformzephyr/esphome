#ifdef USE_ZEPHYR

#include <zephyr/kernel.h>
#include <zephyr/random/random.h>
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/defines.h"

#ifndef USE_NRF52
#include <zephyr/drivers/hwinfo.h>
#endif
#ifdef USE_ZEPHYR_VARIANT_FAMILY_NORDIC
#include <hal/nrf_ficr.h>
#endif
#ifdef USE_ZEPHYR_VARIANT_FAMILY_ESP32
#include <esp_efuse.h>
#include <esp_efuse_table.h>
#endif
#if defined(USE_ZEPHYR_VARIANT_FAMILY_RPI_PICO) && defined(USE_WIFI)
#include <zephyr/net/net_if.h>
#endif

// Probe rather than require: this header only exists once another component (sha256,
// api encryption) has turned on the MBEDTLS Kconfig.
#if __has_include(<mbedtls/build_info.h>)
#include <mbedtls/build_info.h>
#if MBEDTLS_VERSION_MAJOR >= 4
#define ESPHOME_ZEPHYR_HAS_PSA_CRYPTO
#include <psa/crypto.h>
#endif
#endif

namespace esphome {

// HAL functions live in hal.cpp.

Mutex::Mutex() {
  auto *mutex = new k_mutex();
  this->handle_ = mutex;
  k_mutex_init(mutex);
}
Mutex::~Mutex() { delete static_cast<k_mutex *>(this->handle_); }
void Mutex::lock() { k_mutex_lock(static_cast<k_mutex *>(this->handle_), K_FOREVER); }
bool Mutex::try_lock() { return k_mutex_lock(static_cast<k_mutex *>(this->handle_), K_NO_WAIT) == 0; }
void Mutex::unlock() { k_mutex_unlock(static_cast<k_mutex *>(this->handle_)); }

IRAM_ATTR InterruptLock::InterruptLock() { state_ = irq_lock(); }
IRAM_ATTR InterruptLock::~InterruptLock() { irq_unlock(state_); }

// Zephyr LwIPLock is defined inline as a no-op in helpers.h

bool random_bytes(uint8_t *data, size_t len) {
  sys_rand_get(data, len);
  return true;
}

#ifdef USE_NRF52
void get_mac_address_raw(uint8_t *mac) {  // NOLINT(readability-non-const-parameter)
  mac[0] = ((NRF_FICR->DEVICEADDR[1] & 0xFFFF) >> 8) | 0xC0;
  mac[1] = NRF_FICR->DEVICEADDR[1] & 0xFFFF;
  mac[2] = NRF_FICR->DEVICEADDR[0] >> 24;
  mac[3] = NRF_FICR->DEVICEADDR[0] >> 16;
  mac[4] = NRF_FICR->DEVICEADDR[0] >> 8;
  mac[5] = NRF_FICR->DEVICEADDR[0];
}
#else
void get_mac_address_raw(uint8_t *mac) {  // NOLINT(readability-non-const-parameter)
#if defined(USE_ZEPHYR_VARIANT_FAMILY_NORDIC) && (NRF_FICR_HAS_DEVICE_ADDR || NRF_FICR_HAS_BLE_ADDR)
  // Same address as platform: nrf52, so a device keeps its MAC when moved to nrf:
  const uint32_t addr0 = nrf_ficr_deviceaddr_get(NRF_FICR, 0);
  const uint32_t addr1 = nrf_ficr_deviceaddr_get(NRF_FICR, 1);
  mac[0] = ((addr1 & 0xFFFF) >> 8) | 0xC0;
  mac[1] = addr1 & 0xFF;
  mac[2] = addr0 >> 24;
  mac[3] = addr0 >> 16;
  mac[4] = addr0 >> 8;
  mac[5] = addr0;
#else
#ifdef USE_ZEPHYR_VARIANT_FAMILY_ESP32
  // Custom MAC eFuse first, as on ESP-IDF; hwinfo below reads the factory MAC
#ifdef CONFIG_SOC_SERIES_ESP32
  const esp_efuse_desc_t **custom = ESP_EFUSE_MAC_CUSTOM;
#else
  const esp_efuse_desc_t **custom = ESP_EFUSE_USER_DATA_MAC_CUSTOM;
#endif
  if (esp_efuse_read_field_blob(custom, mac, 48) == ESP_OK && mac_address_is_valid(mac))
    return;
#endif
#if defined(USE_ZEPHYR_VARIANT_FAMILY_RPI_PICO) && defined(USE_WIFI)
  // Matches Arduino rp2, which reports the CYW43 MAC
  struct net_if *iface = net_if_get_first_wifi();
  if (iface != nullptr && net_if_get_link_addr(iface)->len == 6) {
    memcpy(mac, net_if_get_link_addr(iface)->addr, 6);
    return;
  }
#endif
  // SiWx91x has no hwinfo backend yet: the weak default returns -ENOSYS, giving an
  // all-zero MAC instead of a link failure.
  if (hwinfo_get_device_id(mac, 6) != 6) {
    memset(mac, 0, 6);
  }
#endif
}
#endif

}  // namespace esphome

void setup();
void loop();

#ifdef CONFIG_SMP
// ESP-IDF (and every other multi-core ESPHome platform) runs the main loop pinned to the
// second core, not the boot core. On Zephyr, main() itself can't pin itself to a core while
// it's running (k_thread_cpu_pin() only accepts threads that aren't currently scheduled), so
// a dedicated worker thread is spawned here and pinned before it ever runs.
static K_THREAD_STACK_DEFINE(esphome_main_stack, CONFIG_MAIN_STACK_SIZE);
static struct k_thread esphome_main_thread_data;

static void esphome_main_thread(void *, void *, void *) {
#ifdef ESPHOME_ZEPHYR_HAS_PSA_CRYPTO
  psa_crypto_init();
#endif
  setup();
  while (true) {
    loop();
  }
}
#endif

int main() {
#ifdef CONFIG_SMP
  // k_thread_cpu_pin() only takes effect on a thread that isn't currently schedulable, so the
  // thread is created suspended (K_FOREVER) and started explicitly after it's pinned.
  k_tid_t tid =
      k_thread_create(&esphome_main_thread_data, esphome_main_stack, K_THREAD_STACK_SIZEOF(esphome_main_stack),
                      esphome_main_thread, nullptr, nullptr, nullptr, CONFIG_MAIN_THREAD_PRIORITY, 0, K_FOREVER);
  k_thread_cpu_pin(tid, 1);
  k_thread_name_set(tid, "esphome_main");
  k_thread_start(tid);
#else
#ifdef ESPHOME_ZEPHYR_HAS_PSA_CRYPTO
  psa_crypto_init();
#endif
  setup();
  while (true) {
    loop();
  }
#endif
  return 0;
}

#endif
