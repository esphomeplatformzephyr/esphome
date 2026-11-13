#include "tinyuf2_touch.h"

#ifdef USE_ZEPHYR_TINYUF2_TOUCH

#include "cdc_acm.h"
#include "usb_device.h"
#include "esphome/core/log.h"

#include <zephyr/sys/reboot.h>

namespace esphome::zephyr {

ESPHOME_LOG_TAG(TAG, "tinyuf2_touch");

// TinyUF2's double-tap word values (src/board_api.h).
static constexpr uint32_t DBL_TAP_MAGIC = 0xf01669ef;             // stay in the bootloader
static constexpr uint32_t DBL_TAP_MAGIC_QUICK_BOOT = 0xf02669ef;  // start the app, skip the double-tap wait
static constexpr uint32_t TOUCH_BAUD_RATE = 1200;
static constexpr uint32_t RESET_TOUCH_BAUD_RATE = 2001;

void TinyUf2Touch::setup() {
  // Needed when no other component uses USB (logger on a hardware UART or off)
  if (int err = usb_device_enable(); err != 0)
    ESP_LOGE(TAG, "USB device setup failed: %d", err);
  // Other CDC ports carry user data, where 1200 baud must not reboot the board
  CdcAcm::add_on_rate_callback([this](const device *dev, uint32_t rate) {
    if (dev != this->port_)
      return;
    uint32_t magic;
    if (rate == TOUCH_BAUD_RATE) {
      magic = DBL_TAP_MAGIC;
    } else if (rate == RESET_TOUCH_BAUD_RATE) {
      magic = DBL_TAP_MAGIC_QUICK_BOOT;
    } else {
      return;
    }
    // The word is likely inside the heap, so reboot at once, without shutdown hooks.
    *reinterpret_cast<volatile uint32_t *>(this->dbl_tap_addr_) = magic;
    sys_reboot(SYS_REBOOT_COLD);
  });
}

}  // namespace esphome::zephyr

#endif
