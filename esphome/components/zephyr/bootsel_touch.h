#pragma once

#include "esphome/core/defines.h"
#ifdef USE_ZEPHYR_BOOTSEL_TOUCH
#include "esphome/core/component.h"

#include <zephyr/device.h>

namespace esphome::zephyr {

// A host opening the USB serial port at 1200 baud (the "1200 baud touch") reboots into the ROM USB bootloader;
// 2001 baud reboots into the application.
class BootselTouch final : public Component {
 public:
  /// port: the USB serial port whose bit rate requests a reboot
  explicit BootselTouch(const device *port) : port_(port) {}
  void setup() override;

 protected:
  const device *port_;
};

}  // namespace esphome::zephyr

#endif
