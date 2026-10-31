#if defined(USE_ZEPHYR)

#include "esphome/core/log.h"
#include "internal_temperature.h"

#include <zephyr/device.h>
#include <zephyr/drivers/sensor.h>

namespace esphome::internal_temperature {

ESPHOME_LOG_TAG(TAG, "internal_temperature");

#ifdef USE_NRF52
// nRF52 boards don't define the standard "die-temp0" alias other Zephyr
// vendors use, so it's looked up by compatible string instead.
static const struct device *const DIE_TEMPERATURE_SENSOR = DEVICE_DT_GET_ONE(nordic_nrf_temp);
#elif defined(INTERNAL_TEMPERATURE_NODE_LABEL)
// The variant's die sensor node (die_temp_nodes), turned on by sensor.py
static const struct device *const DIE_TEMPERATURE_SENSOR = DEVICE_DT_GET(DT_NODELABEL(INTERNAL_TEMPERATURE_NODE_LABEL));
#else
// Not wired up for this variant: a "die-temp0" alias from the board or the user's overlay
static const struct device *const DIE_TEMPERATURE_SENSOR = DEVICE_DT_GET(DT_ALIAS(die_temp0));
#endif

void InternalTemperatureSensor::update() {
  struct sensor_value value;
  int result = sensor_sample_fetch(DIE_TEMPERATURE_SENSOR);
  if (result != 0) {
    ESP_LOGE(TAG, "Failed to fetch die temperature sample (%d)", result);
    if (!this->has_state()) {
      this->publish_state(NAN);
    }
    return;
  }

  result = sensor_channel_get(DIE_TEMPERATURE_SENSOR, SENSOR_CHAN_DIE_TEMP, &value);
  if (result != 0) {
    ESP_LOGE(TAG, "Failed to get die temperature (%d)", result);
    if (!this->has_state()) {
      this->publish_state(NAN);
    }
    return;
  }

  const float temperature = value.val1 + (value.val2 / 1000000.0f);
  if (std::isfinite(temperature)) {
    this->publish_state(temperature);
  } else {
    ESP_LOGD(TAG, "Ignoring invalid temperature (value=%.1f)", temperature);
    if (!this->has_state()) {
      this->publish_state(NAN);
    }
  }
}

void InternalTemperatureSensor::setup() {
  if (!device_is_ready(DIE_TEMPERATURE_SENSOR)) {
    ESP_LOGE(TAG, "Die temperature sensor device %s not ready", DIE_TEMPERATURE_SENSOR->name);
    this->mark_failed();
    return;
  }
}

}  // namespace esphome::internal_temperature

#endif  // USE_ZEPHYR
