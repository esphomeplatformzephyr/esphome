#pragma once

#include "esphome/core/defines.h"
#ifdef USE_ZEPHYR_NRF52_DFU
#include "esphome/core/component.h"
#include "esphome/core/gpio.h"

#include <zephyr/device.h>

namespace esphome::zephyr {

class DeviceFirmwareUpdate final : public Component {
 public:
  /// port: the USB serial port whose bit rate requests an action
  explicit DeviceFirmwareUpdate(const device *port) : port_(port) {}
  void setup() override;
  void set_reset_pin(GPIOPin *reset) { this->reset_pin_ = reset; }
  void dump_config() override;

 protected:
  void on_baud_rate(uint32_t rate);

  const device *port_;
  GPIOPin *reset_pin_{nullptr};
};

}  // namespace esphome::zephyr

#endif
