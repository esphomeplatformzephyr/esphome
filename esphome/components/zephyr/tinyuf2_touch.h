#pragma once

#include "esphome/core/defines.h"
#ifdef USE_ZEPHYR_TINYUF2_TOUCH
#include "esphome/core/component.h"

#include <cstdint>
#include <zephyr/device.h>

namespace esphome::zephyr {

// A host opening the USB serial port at 1200 baud reboots into the TinyUF2 bootloader;
// 2001 baud reboots into the application.
class TinyUf2Touch final : public Component {
 public:
  /// port: the USB serial port whose bit rate requests a reboot
  TinyUf2Touch(const device *port, uintptr_t dbl_tap_addr) : port_(port), dbl_tap_addr_(dbl_tap_addr) {}
  void setup() override;

 protected:
  const device *port_;
  uintptr_t dbl_tap_addr_;
};

}  // namespace esphome::zephyr

#endif
