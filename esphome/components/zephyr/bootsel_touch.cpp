#include "bootsel_touch.h"

#ifdef USE_ZEPHYR_BOOTSEL_TOUCH

#include "cdc_acm.h"
#include "usb_device.h"
#include "esphome/core/log.h"

#include <zephyr/retention/bootmode.h>
#include <zephyr/sys/reboot.h>

namespace esphome::zephyr {

ESPHOME_LOG_TAG(TAG, "bootsel_touch");

static const uint32_t TOUCH_BAUD_RATE = 1200;        // reboot into the bootloader (BOOTSEL)
static const uint32_t RESET_TOUCH_BAUD_RATE = 2001;  // plain reboot back into the application

void BootselTouch::setup() {
  // Needed when no other component uses USB (logger on a hardware UART or off)
  if (int err = usb_device_enable(); err != 0)
    ESP_LOGE(TAG, "USB device setup failed: %d", err);
  // Other CDC ports carry user data, where 1200 baud must not reboot the board
  CdcAcm::add_on_rate_callback([this](const device *dev, uint32_t rate) {
    if (dev != this->port_)
      return;
    if (rate == TOUCH_BAUD_RATE) {
      bootmode_set(BOOT_MODE_TYPE_BOOTLOADER);
      sys_reboot(SYS_REBOOT_COLD);
    } else if (rate == RESET_TOUCH_BAUD_RATE) {
      sys_reboot(SYS_REBOOT_COLD);
    }
  });
}

}  // namespace esphome::zephyr

#endif
