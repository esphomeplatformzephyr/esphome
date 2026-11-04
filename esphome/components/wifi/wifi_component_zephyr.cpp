#include "wifi_component.h"

#ifdef USE_WIFI
#ifdef USE_ZEPHYR

#include <zephyr/kernel.h>
#include <zephyr/net/dhcpv4.h>
#include <zephyr/net/dhcpv4_server.h>
#include <zephyr/net/dns_resolve.h>
#include <zephyr/net/hostname.h>
#include <zephyr/net/net_event.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/wifi_mgmt.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/version.h>

#include <algorithm>
#include <array>
#include <cstring>
#include <iterator>

#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

namespace esphome::wifi {

ESPHOME_LOG_TAG(TAG, "wifi_zephyr");

namespace {

struct ZephyrWiFiEvent {
  enum class Type : uint8_t {
    CONNECT_RESULT,
    DISCONNECT_RESULT,
    AP_ENABLE_RESULT,
    AP_DISABLE_RESULT,
#ifdef USE_WIFI_AP
    AP_STA_CONNECTED,
    AP_STA_DISCONNECTED,
#endif
    IPV4_ADDR_ADD,
    IPV4_ADDR_DEL,
#if USE_NETWORK_IPV6
    IPV6_ADDRS_CHANGED,
#endif
  };
  Type type;
#ifdef USE_WIFI_AP
  uint8_t mac[WIFI_MAC_ADDR_LEN];
#endif
  int status;
};

net_if *sta_iface = nullptr;              // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)
net_if *ap_iface = nullptr;               // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)
net_mgmt_event_callback wifi_event_cb{};  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)
net_mgmt_event_callback ipv4_event_cb{};  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)
#if USE_NETWORK_IPV6
net_mgmt_event_callback ipv6_event_cb{};  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)
#endif

// Written only from the net_mgmt event thread (single producer, enforced via
// CONFIG_NET_MGMT_EVENT_THREAD), read only from the main loop after wifi_loop_()
// drains the event -- no concurrent access window.
WiFiSTAConnectStatus g_sta_status = WiFiSTAConnectStatus::IDLE;  // NOLINT
bool g_ap_enabled = false;                                       // NOLINT
// Power-save requests fail until the driver has actually started (a side effect of
// connect/scan/AP-enable, not interface registration) -- deferred until after.
bool g_wifi_started = false;   // NOLINT
bool g_sta_manual_ip = false;  // NOLINT
in_addr g_sta_static_ip{};     // NOLINT
// Set when wifi_sta_connect_() ends the current connection or attempt itself, so wifi_loop_() does
// not let the event for it end the attempt started after it. Main loop only.
bool g_expect_disconnect = false;  // NOLINT
// Given when a connection or attempt ends; the ESP32 driver refuses a connect until then.
// NOLINTNEXTLINE(cppcoreguidelines-avoid-non-const-global-variables)
K_SEM_DEFINE(disconnect_sem, 0, 1);
constexpr int32_t DISCONNECT_WAIT_MS = 1000;

// Kernel message queue of events, from the net_mgmt thread to wifi_loop_().
constexpr uint8_t EVENT_QUEUE_SIZE = 16;
// NOLINTNEXTLINE(cppcoreguidelines-avoid-non-const-global-variables)
K_MSGQ_DEFINE(event_queue, sizeof(ZephyrWiFiEvent), EVENT_QUEUE_SIZE, alignof(ZephyrWiFiEvent));
// Events lost to a full queue, reported by wifi_loop_().
atomic_t dropped_events = ATOMIC_INIT(0);  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

void queue_event(const ZephyrWiFiEvent &event) {
  if (k_msgq_put(&event_queue, &event, K_NO_WAIT) != 0) {
    atomic_inc(&dropped_events);
  }
}

void push_event(ZephyrWiFiEvent::Type type, int status = 0) {
  ZephyrWiFiEvent event{};
  event.type = type;
  event.status = status;
  queue_event(event);
}

#ifdef USE_WIFI_AP
void push_ap_station_event(ZephyrWiFiEvent::Type type, const wifi_ap_sta_info &info) {
  ZephyrWiFiEvent event{};
  event.type = type;
  memcpy(event.mac, info.mac, sizeof(event.mac));
  queue_event(event);
}

// Stations on the AP, by MAC so a repeat join does not count twice. The API has no way to read this
// list back, so an event lost to a full queue stays lost. Main loop only.
constexpr uint8_t AP_STATION_BOUND = 8;
// NOLINTBEGIN(cppcoreguidelines-avoid-non-const-global-variables)
std::array<std::array<uint8_t, WIFI_MAC_ADDR_LEN>, AP_STATION_BOUND> ap_stations;
uint8_t ap_station_count = 0;
// NOLINTEND(cppcoreguidelines-avoid-non-const-global-variables)

// Returns ap_station_count when the station is not in the table.
uint8_t find_ap_station(const uint8_t *mac) {
  for (uint8_t i = 0; i < ap_station_count; i++) {
    if (memcmp(ap_stations[i].data(), mac, WIFI_MAC_ADDR_LEN) == 0) {
      return i;
    }
  }
  return ap_station_count;
}

void add_ap_station(const uint8_t *mac) {
  if (find_ap_station(mac) == ap_station_count && ap_station_count < AP_STATION_BOUND) {
    memcpy(ap_stations[ap_station_count++].data(), mac, WIFI_MAC_ADDR_LEN);
  }
}

void remove_ap_station(const uint8_t *mac) {
  const uint8_t index = find_ap_station(mac);
  if (index < ap_station_count) {
    ap_stations[index] = ap_stations[--ap_station_count];
  }
}
#endif  // USE_WIFI_AP

// Scan records stay out of the event queue, since a scan can report every AP in range at once.
// The main loop prepares this storage and reads it only after the handler reports the scan done.
struct ScanCandidate {
  uint8_t bssid[6];
  uint8_t channel;
  int8_t rssi;
  uint8_t security;  // wifi_security_type
  uint8_t sta_index;
};

// Used when every network is wanted, not only the configured ones.
struct ScanRecord {
  uint8_t bssid[6];
  uint8_t channel;
  int8_t rssi;
  uint8_t security;  // wifi_security_type
  uint8_t ssid_length;
  char ssid[WIFI_SSID_MAX_LEN + 1];
};

constexpr size_t SCAN_RECORD_BOUND = 32;
constexpr uint16_t SCAN_DWELL_TIME_MS = 300;

constexpr atomic_val_t SCAN_STATE_IDLE = 0;
constexpr atomic_val_t SCAN_STATE_RUNNING = 1;
constexpr atomic_val_t SCAN_STATE_DONE = 2;
constexpr atomic_val_t SCAN_STATE_FAILED = 3;
atomic_t scan_state = ATOMIC_INIT(SCAN_STATE_IDLE);  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

// NOLINTBEGIN(cppcoreguidelines-avoid-non-const-global-variables)
FixedVector<uint32_t> scan_targets;  // SSID hashes, one per entry of sta_, so the event thread never reads sta_
std::array<ScanCandidate, WIFI_SCAN_RESULT_BOUND> scan_candidates;
size_t scan_candidate_count = 0;
ScanRecord *scan_records = nullptr;  // Allocated on first use and kept, so scans do not churn the heap
bool scan_keep_all = false;
size_t scan_record_count = 0;
// NOLINTEND(cppcoreguidelines-avoid-non-const-global-variables)

uint32_t scan_ssid_hash(const uint8_t *ssid, size_t length) {
  uint32_t hash = FNV1_OFFSET_BASIS;
  for (size_t i = 0; i < length; i++) {
    hash = fnv1a_hash_extend(hash, ssid[i]);
  }
  return hash;
}

// Records arrive in scan order, not by signal: a full store replaces its weakest entry.
template<typename T> void keep_strongest(T *store, size_t &count, size_t bound, const T &record) {
  T *weakest = nullptr;
  for (size_t i = 0; i < count; i++) {
    if (std::memcmp(store[i].bssid, record.bssid, sizeof(record.bssid)) == 0) {
      if (record.rssi > store[i].rssi) {
        store[i] = record;
      }
      return;
    }
    if (weakest == nullptr || store[i].rssi < weakest->rssi) {
      weakest = &store[i];
    }
  }
  if (count < bound) {
    store[count++] = record;
  } else if (weakest != nullptr && record.rssi > weakest->rssi) {
    *weakest = record;
  }
}

// Runs on the net_mgmt thread, so no logging: a busy site would flood the log from a small stack.
void store_scan_result(const wifi_scan_result &result) {
  if (atomic_get(&scan_state) != SCAN_STATE_RUNNING) {
    return;
  }
  if (scan_keep_all) {
    ScanRecord record{};
    std::memcpy(record.bssid, result.mac, sizeof(record.bssid));
    record.channel = result.channel;
    record.rssi = result.rssi;
    record.security = result.security;
    record.ssid_length = std::min<size_t>(result.ssid_length, WIFI_SSID_MAX_LEN);
    std::memcpy(record.ssid, result.ssid, record.ssid_length);
    keep_strongest(scan_records, scan_record_count, SCAN_RECORD_BOUND, record);
    return;
  }
  const uint32_t hash = scan_ssid_hash(result.ssid, result.ssid_length);
  for (size_t i = 0; i < scan_targets.size(); i++) {
    if (scan_targets[i] == hash) {
      ScanCandidate candidate{};
      std::memcpy(candidate.bssid, result.mac, sizeof(candidate.bssid));
      candidate.channel = result.channel;
      candidate.rssi = result.rssi;
      candidate.security = result.security;
      candidate.sta_index = i;
      keep_strongest(scan_candidates.data(), scan_candidate_count, scan_candidates.size(), candidate);
      return;
    }
  }
}

void wifi_event_handler(net_mgmt_event_callback *cb, uint64_t mgmt_event, net_if *iface) {
  switch (mgmt_event) {
    case NET_EVENT_WIFI_CONNECT_RESULT: {
      const auto *status = static_cast<const wifi_status *>(cb->info);
      const int result = status != nullptr ? status->status : -1;
      push_event(ZephyrWiFiEvent::Type::CONNECT_RESULT, result);
      // An attempt that is left before it joins ends with a failed result, not a disconnect
      if (result != 0) {
        k_sem_give(&disconnect_sem);
      }
      break;
    }
    case NET_EVENT_WIFI_DISCONNECT_RESULT: {
      const auto *status = static_cast<const wifi_status *>(cb->info);
      push_event(ZephyrWiFiEvent::Type::DISCONNECT_RESULT, status != nullptr ? status->status : -1);
      k_sem_give(&disconnect_sem);
      break;
    }
    case NET_EVENT_WIFI_AP_ENABLE_RESULT: {
      const auto *status = static_cast<const wifi_status *>(cb->info);
      push_event(ZephyrWiFiEvent::Type::AP_ENABLE_RESULT, status != nullptr ? status->status : -1);
      break;
    }
    case NET_EVENT_WIFI_AP_DISABLE_RESULT: {
      const auto *status = static_cast<const wifi_status *>(cb->info);
      push_event(ZephyrWiFiEvent::Type::AP_DISABLE_RESULT, status != nullptr ? status->status : -1);
      break;
    }
#ifdef USE_WIFI_AP
    case NET_EVENT_WIFI_AP_STA_CONNECTED:
      push_ap_station_event(ZephyrWiFiEvent::Type::AP_STA_CONNECTED, *static_cast<const wifi_ap_sta_info *>(cb->info));
      break;
    case NET_EVENT_WIFI_AP_STA_DISCONNECTED:
      push_ap_station_event(ZephyrWiFiEvent::Type::AP_STA_DISCONNECTED,
                            *static_cast<const wifi_ap_sta_info *>(cb->info));
      break;
#endif
    case NET_EVENT_WIFI_SCAN_RESULT:
      store_scan_result(*static_cast<const wifi_scan_result *>(cb->info));
      break;
    case NET_EVENT_WIFI_SCAN_DONE: {
      const auto *status = static_cast<const wifi_status *>(cb->info);
      const bool failed = status == nullptr || status->status != 0;
      atomic_cas(&scan_state, SCAN_STATE_RUNNING, failed ? SCAN_STATE_FAILED : SCAN_STATE_DONE);
      break;
    }
    default:
      break;
  }
}

void ipv4_event_handler(net_mgmt_event_callback *cb, uint64_t mgmt_event, net_if *iface) {
  if (iface != sta_iface) {
    return;
  }
  if (mgmt_event == NET_EVENT_IPV4_ADDR_ADD) {
    push_event(ZephyrWiFiEvent::Type::IPV4_ADDR_ADD);
  } else if (mgmt_event == NET_EVENT_IPV4_ADDR_DEL) {
    push_event(ZephyrWiFiEvent::Type::IPV4_ADDR_DEL);
  }
}

#if USE_NETWORK_IPV6
void ipv6_event_handler(net_mgmt_event_callback *cb, uint64_t mgmt_event, net_if *iface) {
  if (iface == sta_iface) {
    push_event(ZephyrWiFiEvent::Type::IPV6_ADDRS_CHANGED);
  }
}
#endif

#if USE_NETWORK_IPV6
network::IPAddress ipv4_to_ip_address(const in_addr &addr4) {
  in6_addr mapped{};
  net_ipv6_addr_create_v4_mapped(&addr4, &mapped);
  return network::IPAddress(&mapped);
}
#else
network::IPAddress ipv4_to_ip_address(const in_addr &addr4) { return network::IPAddress(&addr4); }
#endif

bool parse_ipv4(const char *str, in_addr &out) {
  if (net_addr_pton(AF_INET, str, &out) != 0) {
    ESP_LOGE(TAG, "Invalid IPv4 address '%s'", str);
    return false;
  }
  return true;
}

struct IpCollector {
  network::IPAddresses *addresses;
  uint8_t index;
};

// net_if_ipv4_addr_foreach() callbacks; Zephyr calls them for each used address.
void collect_ipv4(net_if *iface, net_if_addr *addr, void *user_data) {
  auto *collector = static_cast<IpCollector *>(user_data);
  if (collector->index < collector->addresses->size()) {
    (*collector->addresses)[collector->index++] = ipv4_to_ip_address(addr->address.in_addr);
  }
}

#if USE_NETWORK_IPV6
// An address still being checked for duplicates is not usable yet.
void collect_ipv6(net_if *iface, net_if_addr *addr, void *user_data) {
  auto *collector = static_cast<IpCollector *>(user_data);
  if (addr->addr_state == NET_ADDR_PREFERRED && collector->index < collector->addresses->size()) {
    (*collector->addresses)[collector->index++] = network::IPAddress(&addr->address.in6_addr);
  }
}

void count_ipv6(net_if *iface, net_if_addr *addr, void *user_data) {
  if (addr->addr_state == NET_ADDR_PREFERRED) {
    (*static_cast<uint8_t *>(user_data))++;
  }
}
#endif

struct FirstIpv4 {
  in_addr addr{};
  bool found{false};
};

void first_ipv4(net_if *iface, net_if_addr *addr, void *user_data) {
  auto *first = static_cast<FirstIpv4 *>(user_data);
  if (!first->found) {
    first->addr = addr->address.in_addr;
    first->found = true;
  }
}

#ifdef USE_WIFI_AP
// Releases what wifi_ap_ip_config_() took, and forgets the stations that went down with the AP.
void stop_ap_network() {
#ifdef CONFIG_NET_DHCPV4_SERVER
  net_dhcpv4_server_stop(ap_iface);  // -ENOENT when it is not running
#endif
  FirstIpv4 first;
  net_if_ipv4_addr_foreach(ap_iface, first_ipv4, &first);
  if (first.found) {
    net_if_ipv4_addr_rm(ap_iface, &first.addr);
  }
  ap_station_count = 0;
}
#endif

}  // namespace

void WiFiComponent::wifi_pre_setup_() {
  sta_iface = net_if_get_wifi_sta();
  ap_iface = net_if_get_wifi_sap();
  uint64_t wifi_events = NET_EVENT_WIFI_CONNECT_RESULT | NET_EVENT_WIFI_DISCONNECT_RESULT |
                         NET_EVENT_WIFI_AP_ENABLE_RESULT | NET_EVENT_WIFI_AP_DISABLE_RESULT |
                         NET_EVENT_WIFI_SCAN_RESULT | NET_EVENT_WIFI_SCAN_DONE;
#ifdef USE_WIFI_AP
  // Stations on the AP hold the reboot timeout and keep an exclusive AP from pausing
  wifi_events |= NET_EVENT_WIFI_AP_STA_CONNECTED | NET_EVENT_WIFI_AP_STA_DISCONNECTED;
#endif
  net_mgmt_init_event_callback(&wifi_event_cb, wifi_event_handler, wifi_events);
  net_mgmt_add_event_callback(&wifi_event_cb);
  net_mgmt_init_event_callback(&ipv4_event_cb, ipv4_event_handler, NET_EVENT_IPV4_ADDR_ADD | NET_EVENT_IPV4_ADDR_DEL);
  net_mgmt_add_event_callback(&ipv4_event_cb);
#if USE_NETWORK_IPV6
  net_mgmt_init_event_callback(&ipv6_event_cb, ipv6_event_handler,
                               NET_EVENT_IPV6_ADDR_ADD | NET_EVENT_IPV6_ADDR_DEL | NET_EVENT_IPV6_DAD_SUCCEED);
  net_mgmt_add_event_callback(&ipv6_event_cb);
#endif
}

bool WiFiComponent::wifi_mode_(optional<bool> sta, optional<bool> ap) {
  // Zephyr has no STA up/down request -- enabling is implicit in wifi_sta_connect_();
  // disabling approximates ESP-IDF's esp_wifi_stop() with a disconnect, so
  // restart_adapter()'s recovery path isn't a no-op here.
  if (sta.has_value() && !sta.value()) {
    this->wifi_disconnect_();
  }
  if (ap.has_value()) {
    if (ap_iface == nullptr) {
      ESP_LOGE(TAG, "AP interface not available");
      return false;
    }
    if (ap.value() == g_ap_enabled) {
      return true;
    }
    if (ap.value()) {
      // Actual enable happens in wifi_start_ap_() once SSID/password are known;
      // this branch only handles disabling.
      return true;
    }
    int ret = net_mgmt(NET_REQUEST_WIFI_AP_DISABLE, ap_iface, nullptr, 0);
    if (ret != 0) {
      ESP_LOGE(TAG, "NET_REQUEST_WIFI_AP_DISABLE failed: %d", ret);
      return false;
    }
    g_ap_enabled = false;
#ifdef USE_WIFI_AP
    stop_ap_network();
#endif
  }
  return true;
}

bool WiFiComponent::wifi_sta_pre_setup_() { return true; }

// Zephyr's net_mgmt WiFi API has no TX power control request at all.
bool WiFiComponent::wifi_apply_output_power_(float output_power) { return true; }

namespace {

// The API takes one setting per request. Every field is still filled in, since a driver may read
// more than the one the request names.
wifi_ps_params make_ps_params(WiFiPowerSaveMode mode, wifi_ps_param_type type) {
  wifi_ps_params params{};
  params.type = type;
  params.enabled = mode == WIFI_POWER_SAVE_NONE ? WIFI_PS_DISABLED : WIFI_PS_ENABLED;
  params.wakeup_mode = mode == WIFI_POWER_SAVE_HIGH ? WIFI_PS_WAKEUP_MODE_LISTEN_INTERVAL : WIFI_PS_WAKEUP_MODE_DTIM;
  params.listen_interval = mode == WIFI_POWER_SAVE_HIGH ? 10 : 1;
  return params;
}

// The listen interval is refused once associated, so these go in before connecting. A driver
// without them returns an error, and the state request after connecting still applies.
void apply_power_save_before_connect(WiFiPowerSaveMode mode) {
  if (mode == WIFI_POWER_SAVE_NONE) {
    return;
  }
  wifi_ps_params params = make_ps_params(mode, WIFI_PS_PARAM_WAKEUP_MODE);
  int ret = net_mgmt(NET_REQUEST_WIFI_PS, sta_iface, &params, sizeof(params));
  if (ret != 0) {
    ESP_LOGD(TAG, "Power save wakeup mode not set: %d", ret);
  }
  if (mode == WIFI_POWER_SAVE_HIGH) {
    params = make_ps_params(mode, WIFI_PS_PARAM_LISTEN_INTERVAL);
    ret = net_mgmt(NET_REQUEST_WIFI_PS, sta_iface, &params, sizeof(params));
    if (ret != 0) {
      ESP_LOGD(TAG, "Power save listen interval not set: %d", ret);
    }
  }
}

}  // namespace

bool WiFiComponent::wifi_apply_power_save_() {
  if (sta_iface == nullptr) {
    return false;
  }
  if (!g_wifi_started) {
    // Nothing to apply yet -- re-applied once CONNECT_RESULT confirms the driver started
    // (see wifi_loop_()); report success so callers don't log a spurious failure.
    return true;
  }
  wifi_ps_params params = make_ps_params(this->power_save_, WIFI_PS_PARAM_STATE);
  int ret = net_mgmt(NET_REQUEST_WIFI_PS, sta_iface, &params, sizeof(params));
  bool success = ret == 0;
#ifdef USE_WIFI_POWER_SAVE_LISTENERS
  if (success) {
    for (auto *listener : this->power_save_listeners_) {
      listener->on_wifi_power_save(this->power_save_);
    }
  }
#endif
  return success;
}

bool WiFiComponent::wifi_sta_ip_config_(const optional<ManualIP> &manual_ip) {
  if (sta_iface == nullptr) {
    return false;
  }
  // A previous network's static address would otherwise stay, and could be reported as the first one.
  if (g_sta_manual_ip) {
    g_sta_manual_ip = false;
    net_if_ipv4_addr_rm(sta_iface, &g_sta_static_ip);
    const in_addr no_gateway{};
    net_if_ipv4_set_gw(sta_iface, &no_gateway);
  }
  if (!manual_ip.has_value()) {
    // DHCPv4 is started on CONNECT_RESULT (see wifi_loop_()).
    return true;
  }
  // A previous DHCP network's client may still be running.
  net_dhcpv4_stop(sta_iface);
  char buf[network::IP_ADDRESS_BUFFER_SIZE];
  in_addr addr{};
  in_addr gw{};
  in_addr mask{};
  if (!parse_ipv4(manual_ip->static_ip.str_to(buf), addr) || !parse_ipv4(manual_ip->gateway.str_to(buf), gw) ||
      !parse_ipv4(manual_ip->subnet.str_to(buf), mask)) {
    return false;
  }
  net_if_addr *ifaddr = net_if_ipv4_addr_add(sta_iface, &addr, NET_ADDR_MANUAL, 0);
  if (ifaddr == nullptr) {
    ESP_LOGE(TAG, "Failed to set static IP");
    return false;
  }
  g_sta_static_ip = addr;
  net_if_ipv4_set_gw(sta_iface, &gw);
  net_if_ipv4_set_netmask_by_addr(sta_iface, &addr, &mask);
  // Without DHCP nothing else supplies DNS servers for getaddrinfo().
  char dns1[network::IP_ADDRESS_BUFFER_SIZE];
  char dns2[network::IP_ADDRESS_BUFFER_SIZE];
  const char *servers[3]{};
  size_t count = 0;
  if (manual_ip->dns1.is_set()) {
    servers[count++] = manual_ip->dns1.str_to(dns1);
  }
  if (manual_ip->dns2.is_set()) {
    servers[count++] = manual_ip->dns2.str_to(dns2);
  }
  if (count > 0 && dns_resolve_reconfigure(dns_resolve_get_default(), servers, nullptr, DNS_SOURCE_MANUAL) < 0) {
    ESP_LOGW(TAG, "Failed to set DNS servers");
  }
  return true;
}

bool WiFiComponent::wifi_apply_hostname_() {
#ifdef CONFIG_NET_HOSTNAME_DYNAMIC
  const std::string &name = App.get_name();
  return net_hostname_set(name.c_str(), name.size()) == 0;
#else
  return true;
#endif
}

namespace {

optional<ManualIP> get_manual_ip_or_none(const WiFiAP &ap) {
#ifdef USE_WIFI_MANUAL_IP
  return ap.get_manual_ip();
#else
  return {};
#endif
}

// Takes ssid_/password_ already extracted by the caller rather than a WiFiAP reference --
// WiFiAP only grants field access to WiFiComponent (friend class), not to this free function.
void fill_ssid_and_security(wifi_connect_req_params &params, const CompactString &ssid, const CompactString &password) {
  params.ssid = reinterpret_cast<const uint8_t *>(ssid.c_str());
  params.ssid_length = ssid.size();
  // esp32_wifi_connect() (Zephyr's esp32 wifi driver) never reads params.band -- only
  // params.channel affects the actual connect attempt, and we leave that at "any". Left
  // commented out, not deleted: WiFi is currently esp32-family-only in this platform, but
  // if another Zephyr wifi driver gains support later, that driver may actually honor this
  // field and it would need to be restored (band-aware, not hardcoded to 2.4GHz).
  // params.band = WIFI_FREQ_BAND_2_4_GHZ;
  if (password.empty()) {
    params.security = WIFI_SECURITY_TYPE_NONE;
  } else {
    params.security = WIFI_SECURITY_TYPE_PSK;
    params.psk = reinterpret_cast<const uint8_t *>(password.c_str());
    params.psk_length = password.size();
  }
}

// What the last scan saw on this AP. UNKNOWN when it saw nothing, as with fast_connect.
wifi_security_type scanned_security(const uint8_t *bssid) {
  if (atomic_get(&scan_state) == SCAN_STATE_IDLE) {
    for (size_t i = 0; i < scan_candidate_count; i++) {
      if (std::memcmp(scan_candidates[i].bssid, bssid, sizeof(scan_candidates[i].bssid)) == 0) {
        return static_cast<wifi_security_type>(scan_candidates[i].security);
      }
    }
    for (size_t i = 0; scan_records != nullptr && i < scan_record_count; i++) {
      if (std::memcmp(scan_records[i].bssid, bssid, sizeof(scan_records[i].bssid)) == 0) {
        return static_cast<wifi_security_type>(scan_records[i].security);
      }
    }
  }
  return WIFI_SECURITY_TYPE_UNKNOWN;
}

bool meets_min_auth_mode(wifi_security_type security, WifiMinAuthMode min_auth_mode) {
  switch (security) {
    case WIFI_SECURITY_TYPE_WPA_PSK:
      return min_auth_mode == WIFI_MIN_AUTH_MODE_WPA;
    case WIFI_SECURITY_TYPE_PSK:
    case WIFI_SECURITY_TYPE_PSK_SHA256:
    case WIFI_SECURITY_TYPE_WPA_AUTO_PERSONAL:
      return min_auth_mode != WIFI_MIN_AUTH_MODE_WPA3;
    case WIFI_SECURITY_TYPE_SAE:
    case WIFI_SECURITY_TYPE_SAE_H2E:
    case WIFI_SECURITY_TYPE_SAE_AUTO:
      return true;
    default:
      return false;
  }
}

#ifdef USE_WIFI_WPA2_EAP
// The driver keeps its own copy of the certificates, sent ahead of the connect.
bool fill_eap(wifi_connect_req_params &params, const EAPAuth &eap) {
  const auto bytes = [](const char *text) { return reinterpret_cast<const uint8_t *>(text); };
  params.anon_id = bytes(eap.identity.c_str());
  params.aid_length = eap.identity.size();
  params.eap_identity = bytes(eap.username.c_str());
  params.eap_id_length = eap.username.size();
  params.eap_password = bytes(eap.password.c_str());
  params.eap_passwd_length = eap.password.size();

  const bool has_ca_cert = eap.ca_cert != nullptr && eap.ca_cert[0] != '\0';
  const bool has_client_cert = eap.client_cert != nullptr && eap.client_cert[0] != '\0';
  wifi_enterprise_creds_params creds{};
  // The length counts the terminator, which the PEM parser needs
  const auto set_cred = [](uint8_t *&cred, uint32_t &length, const char *pem) {
    cred =
        const_cast<uint8_t *>(reinterpret_cast<const uint8_t *>(pem));  // NOLINT(cppcoreguidelines-pro-type-const-cast)
    length = strlen(pem) + 1;
  };
  if (has_ca_cert) {
    set_cred(creds.ca_cert, creds.ca_cert_len, eap.ca_cert);
    params.verify_peer_cert = true;
  }
  if (has_client_cert) {
    set_cred(creds.client_cert, creds.client_cert_len, eap.client_cert);
    set_cred(creds.client_key, creds.client_key_len, eap.client_key);
    params.key_passwd = params.eap_password;
    params.key_passwd_length = params.eap_passwd_length;
    params.security = WIFI_SECURITY_TYPE_EAP_TLS;
  } else {
    params.security = eap.use_ttls ? WIFI_SECURITY_TYPE_EAP_TTLS_MSCHAPV2 : WIFI_SECURITY_TYPE_EAP_PEAP_MSCHAPV2;
  }
  const int ret = net_mgmt(NET_REQUEST_WIFI_ENTERPRISE_CREDS, sta_iface, &creds, sizeof(creds));
  if (ret != 0) {
    ESP_LOGW(TAG, "NET_REQUEST_WIFI_ENTERPRISE_CREDS failed: %d", ret);
  }
  return ret == 0;
}
#endif  // USE_WIFI_WPA2_EAP

bool get_iface_status(net_if *iface, wifi_iface_status *status) {
  if (iface == nullptr) {
    return false;
  }
  return net_mgmt(NET_REQUEST_WIFI_IFACE_STATUS, iface, status, sizeof(*status)) == 0;
}

}  // namespace

bool WiFiComponent::wifi_sta_connect_(const WiFiAP &ap) {
  if (sta_iface == nullptr) {
    return false;
  }
  const optional<ManualIP> manual_ip = get_manual_ip_or_none(ap);
  if (!this->wifi_sta_ip_config_(manual_ip)) {
    return false;
  }
  g_sta_manual_ip = manual_ip.has_value();

  wifi_connect_req_params params{};
  fill_ssid_and_security(params, ap.ssid_, ap.password_);
  // Join the AP chosen from the scan, as on ESP-IDF; without these the driver takes the first match
  if (ap.has_bssid()) {
    std::memcpy(params.bssid, ap.get_bssid().data(), sizeof(params.bssid));
  }
  params.channel = ap.has_channel() ? ap.get_channel() : WIFI_CHANNEL_ANY;
  params.timeout = SYS_FOREVER_MS;
  if (!ap.password_.empty()) {
    wifi_security_type security = ap.has_bssid() ? scanned_security(ap.get_bssid().data()) : WIFI_SECURITY_TYPE_UNKNOWN;
    if (security == WIFI_SECURITY_TYPE_UNKNOWN) {
      security = WIFI_SECURITY_TYPE_WPA_AUTO_PERSONAL;
    }
    // A mixed AP joins at the strongest level the setting allows
    if (security == WIFI_SECURITY_TYPE_WPA_AUTO_PERSONAL && this->min_auth_mode_ == WIFI_MIN_AUTH_MODE_WPA3) {
      security = WIFI_SECURITY_TYPE_SAE;
    }
    if (!meets_min_auth_mode(security, this->min_auth_mode_)) {
      ESP_LOGW(TAG, "AP security '%s' is below min_auth_mode", wifi_security_txt(security));
      g_sta_status = WiFiSTAConnectStatus::ERROR_CONNECT_FAILED;
      return false;
    }
    params.security = security;
  }

  // A connect while connected or still connecting is refused, or drops the old AP in the driver's own way
  wifi_iface_status iface_status{};
  const bool was_active = get_iface_status(sta_iface, &iface_status) && iface_status.state >= WIFI_STATE_SCANNING &&
                          iface_status.state <= WIFI_STATE_COMPLETED;
  g_expect_disconnect = false;
  if (was_active) {
    k_sem_reset(&disconnect_sem);
    UnavoidableBlockingScope scope;
    if (net_mgmt(NET_REQUEST_WIFI_DISCONNECT, sta_iface, nullptr, 0) == 0) {
      g_expect_disconnect = true;
      if (k_sem_take(&disconnect_sem, K_MSEC(DISCONNECT_WAIT_MS)) != 0) {
        ESP_LOGW(TAG, "No disconnect event from the previous AP");
        // So the next real disconnect is not taken for this one
        g_expect_disconnect = false;
      }
    }
  }

  apply_power_save_before_connect(this->power_save_);

#ifdef USE_WIFI_WPA2_EAP
  if (ap.get_eap().has_value() && !fill_eap(params, *ap.get_eap())) {
    g_sta_status = WiFiSTAConnectStatus::ERROR_CONNECT_FAILED;
    return false;
  }
#endif

  g_sta_status = WiFiSTAConnectStatus::CONNECTING;
  g_wifi_started = true;
#if USE_NETWORK_IPV4
  this->got_ipv4_address_ = false;
#endif
#if USE_NETWORK_IPV6
  this->num_ipv6_addresses_ = 0;
#endif
  int ret;
  {
    // The driver blocks until the join has started
    UnavoidableBlockingScope scope;
    ret = net_mgmt(NET_REQUEST_WIFI_CONNECT, sta_iface, &params, sizeof(params));
    // Not every driver takes the automatic mode
    if (ret != 0 && params.security == WIFI_SECURITY_TYPE_WPA_AUTO_PERSONAL) {
      params.security = WIFI_SECURITY_TYPE_PSK;
      ret = net_mgmt(NET_REQUEST_WIFI_CONNECT, sta_iface, &params, sizeof(params));
    }
  }
  if (ret != 0) {
    ESP_LOGW(TAG, "NET_REQUEST_WIFI_CONNECT failed: %d", ret);
    g_sta_status = WiFiSTAConnectStatus::ERROR_CONNECT_FAILED;
    return false;
  }
  return true;
}

WiFiSTAConnectStatus WiFiComponent::wifi_sta_connect_status_() const {
#if USE_NETWORK_IPV4
  if (g_sta_status == WiFiSTAConnectStatus::CONNECTED && !this->got_ipv4_address_) {
    return WiFiSTAConnectStatus::CONNECTING;
  }
#endif
#if USE_NETWORK_IPV6 && (USE_NETWORK_MIN_IPV6_ADDR_COUNT > 0)
  if (g_sta_status == WiFiSTAConnectStatus::CONNECTED && this->num_ipv6_addresses_ < USE_NETWORK_MIN_IPV6_ADDR_COUNT) {
    return WiFiSTAConnectStatus::CONNECTING;
  }
#endif
  return g_sta_status;
}

bool WiFiComponent::wifi_scan_start_(bool passive) {
  if (sta_iface == nullptr) {
    return false;
  }
  atomic_set(&scan_state, SCAN_STATE_IDLE);
  // Allocated on first use and kept; a failure is covered by keep_all below
  if (scan_targets.capacity() < this->sta_.size()) {
    scan_targets.try_init(this->sta_.size());
  }
  scan_targets.clear();
  scan_candidate_count = 0;
  scan_record_count = 0;

  wifi_scan_params params{};
  params.scan_type = passive ? WIFI_SCAN_TYPE_PASSIVE : WIFI_SCAN_TYPE_ACTIVE;
  params.dwell_time_active = SCAN_DWELL_TIME_MS;
  params.dwell_time_passive = SCAN_DWELL_TIME_MS;

  // A network configured by BSSID alone has no SSID to match on, so it needs every record too
  bool keep_all = this->needs_full_scan_results_() || this->sta_.size() > scan_targets.capacity();
  for (const auto &sta : this->sta_) {
    keep_all |= sta.ssid_.empty();
    scan_targets.push_back(scan_ssid_hash(reinterpret_cast<const uint8_t *>(sta.ssid_.c_str()), sta.ssid_.size()));
  }
  if (keep_all && scan_records == nullptr) {
    scan_records = RAMAllocator<ScanRecord>().allocate(SCAN_RECORD_BOUND);
    if (scan_records == nullptr) {
      ESP_LOGW(TAG, "No memory for %zu scan results", SCAN_RECORD_BOUND);
    }
  }
  scan_keep_all = keep_all && scan_records != nullptr;
  if (!keep_all) {
    // These only reduce what the driver reports; store_scan_result() still does the filtering
    if (this->sta_.size() <= std::size(params.ssids)) {
      for (size_t i = 0; i < this->sta_.size(); i++) {
        params.ssids[i] = this->sta_[i].ssid_.c_str();
      }
    }
    size_t channels = 0;
    bool all_channels_known = true;
    for (const auto &sta : this->sta_) {
      if (!sta.has_channel()) {
        all_channels_known = false;
        break;
      }
      bool known = false;
      for (size_t i = 0; i < channels; i++) {
        known |= params.band_chan[i].channel == sta.get_channel();
      }
      if (known) {
        continue;
      }
      if (channels == std::size(params.band_chan)) {
        all_channels_known = false;
        break;
      }
      params.band_chan[channels++] = {WIFI_FREQ_BAND_2_4_GHZ, sta.get_channel()};
    }
    if (!all_channels_known) {
      std::memset(params.band_chan, 0, sizeof(params.band_chan));
    }
  }

  g_wifi_started = true;
  atomic_set(&scan_state, SCAN_STATE_RUNNING);
  int ret = net_mgmt(NET_REQUEST_WIFI_SCAN, sta_iface, &params, sizeof(params));
  if (ret != 0) {
    atomic_set(&scan_state, SCAN_STATE_IDLE);
    ESP_LOGV(TAG, "NET_REQUEST_WIFI_SCAN failed: %d", ret);
    return false;
  }
  this->scan_done_ = false;
  return true;
}

void WiFiComponent::wifi_scan_finished_(bool success) {
  const bool needs_full = this->needs_full_scan_results_();
  const size_t stored = scan_keep_all ? scan_record_count : scan_candidate_count;
  const size_t wanted = success ? stored : 0;
  {
    ScanResultsLock lock(this);
    this->scan_result_.clear();
    this->scan_done_ = true;
    if (wanted > 0 && this->scan_result_.capacity() < wanted && !this->scan_result_.try_init(wanted)) {
      // The retry logic scans again
      ESP_LOGW(TAG, "No memory for %zu scan results", wanted);
    } else if (wanted > 0 && scan_keep_all) {
      for (size_t i = 0; i < scan_record_count; i++) {
        const ScanRecord &record = scan_records[i];
        if (!needs_full && !this->matches_configured_network_(record.ssid, record.bssid)) {
          this->log_discarded_scan_result_(record.ssid, record.bssid, record.rssi, record.channel);
          continue;
        }
        bssid_t bssid{};
        std::copy(std::begin(record.bssid), std::end(record.bssid), bssid.begin());
        this->scan_result_.emplace_back(bssid, record.ssid, record.ssid_length, record.channel, record.rssi,
                                        record.security != WIFI_SECURITY_TYPE_NONE, record.ssid_length == 0);
      }
    } else if (wanted > 0) {
      for (size_t i = 0; i < scan_candidate_count; i++) {
        const ScanCandidate &candidate = scan_candidates[i];
        // The networks can change during a scan
        if (candidate.sta_index >= this->sta_.size()) {
          continue;
        }
        const WiFiAP &sta = this->sta_[candidate.sta_index];
        bssid_t bssid{};
        std::copy(std::begin(candidate.bssid), std::end(candidate.bssid), bssid.begin());
        this->scan_result_.emplace_back(bssid, sta.ssid_.c_str(), sta.ssid_.size(), candidate.channel, candidate.rssi,
                                        candidate.security != WIFI_SECURITY_TYPE_NONE, false);
      }
    }
  }
  atomic_set(&scan_state, SCAN_STATE_IDLE);
  ESP_LOGV(TAG, "Scan complete: %zu stored", this->scan_result_.size());
#ifdef USE_WIFI_SCAN_RESULTS_LISTENERS
  this->notify_scan_results_listeners_();
#endif
}

#ifdef USE_WIFI_AP
bool WiFiComponent::wifi_ap_ip_config_(const optional<ManualIP> &manual_ip) {
  if (ap_iface == nullptr) {
    return false;
  }
  // An AP that failed to start, or was refused late, is still holding these.
  stop_ap_network();
  char buf[network::IP_ADDRESS_BUFFER_SIZE];
  in_addr addr{};
  in_addr gateway{};
  in_addr mask{};
  if (manual_ip.has_value()) {
    if (!parse_ipv4(manual_ip->static_ip.str_to(buf), addr) || !parse_ipv4(manual_ip->gateway.str_to(buf), gateway) ||
        !parse_ipv4(manual_ip->subnet.str_to(buf), mask)) {
      return false;
    }
  } else {
    net_addr_pton(AF_INET, "192.168.4.1", &addr);
    gateway = addr;
    net_addr_pton(AF_INET, "255.255.255.0", &mask);
  }
  net_if_ipv4_set_gw(ap_iface, &gateway);
  net_if_addr *ifaddr = net_if_ipv4_addr_add(ap_iface, &addr, NET_ADDR_MANUAL, 0);
  if (ifaddr == nullptr) {
    ESP_LOGE(TAG, "Failed to set AP IP address");
    return false;
  }
  net_if_ipv4_set_netmask_by_addr(ap_iface, &addr, &mask);
#ifdef CONFIG_NET_DHCPV4_SERVER
  in_addr pool_start = addr;
  // Clamp so the whole pool (base_addr .. base_addr + ADDR_COUNT - 1) stays inside the
  // subnet: .255 is the broadcast address, and starting any later than
  // 254 - (ADDR_COUNT - 1) would let the range overflow past .255 into the next subnet.
  constexpr int max_pool_start = 254 - (CONFIG_NET_DHCPV4_SERVER_ADDR_COUNT - 1);
  pool_start.s4_addr[3] = std::min(pool_start.s4_addr[3] + 99, max_pool_start);
  if (net_dhcpv4_server_start(ap_iface, &pool_start) != 0) {
    ESP_LOGW(TAG, "Failed to start DHCPv4 server on AP interface");
  }
#endif
  return true;
}

bool WiFiComponent::wifi_start_ap_(const WiFiAP &ap) {
  if (ap_iface == nullptr) {
    ESP_LOGE(TAG, "AP interface not available");
    return false;
  }
  if (!this->wifi_ap_ip_config_(get_manual_ip_or_none(ap))) {
    return false;
  }

  wifi_connect_req_params params{};
  fill_ssid_and_security(params, ap.ssid_, ap.password_);
  params.channel = ap.has_channel() ? ap.get_channel() : WIFI_CHANNEL_ANY;

  g_wifi_started = true;
  int ret = net_mgmt(NET_REQUEST_WIFI_AP_ENABLE, ap_iface, &params, sizeof(params));
  if (ret != 0) {
    ESP_LOGE(TAG, "NET_REQUEST_WIFI_AP_ENABLE failed: %d", ret);
    return false;
  }
  g_ap_enabled = true;
  return true;
}

network::IPAddress WiFiComponent::wifi_soft_ap_ip() {
  if (ap_iface == nullptr) {
    return {};
  }
  FirstIpv4 first;
  net_if_ipv4_addr_foreach(ap_iface, first_ipv4, &first);
  return first.found ? ipv4_to_ip_address(first.addr) : network::IPAddress{};
}
#endif  // USE_WIFI_AP

void WiFiComponent::on_powerdown() {
  // Leave the AP so it doesn't hold the old association against our next connect.
  if (!this->wifi_disconnect_())
    return;
  ESP_LOGD(TAG, "Disconnected before powerdown");
  delay(100);  // NOLINT: once per reboot, lets the deauth go out
}

bool WiFiComponent::wifi_disconnect_() {
  if (sta_iface == nullptr) {
    return false;
  }
  g_expect_disconnect = false;
  int ret = net_mgmt(NET_REQUEST_WIFI_DISCONNECT, sta_iface, nullptr, 0);
  if (ret != 0) {
    return false;
  }
  if (g_sta_status == WiFiSTAConnectStatus::CONNECTING) {
    g_sta_status = WiFiSTAConnectStatus::IDLE;
  }
  return true;
}

bssid_t WiFiComponent::wifi_bssid() {
  bssid_t bssid{};
  wifi_iface_status status{};
  if (get_iface_status(sta_iface, &status)) {
    std::copy(std::begin(status.bssid), std::begin(status.bssid) + 6, bssid.begin());
  }
  return bssid;
}

const char *WiFiComponent::wifi_ssid_to(std::span<char, SSID_BUFFER_SIZE> buffer) {
  wifi_iface_status status{};
  if (get_iface_status(sta_iface, &status)) {
    size_t len = std::min(static_cast<size_t>(status.ssid_len), SSID_BUFFER_SIZE - 1);
    memcpy(buffer.data(), status.ssid, len);
    buffer[len] = '\0';
  } else {
    buffer[0] = '\0';
  }
  return buffer.data();
}

int8_t WiFiComponent::wifi_rssi() {
  wifi_iface_status status{};
  if (this->is_connected_() && get_iface_status(sta_iface, &status)) {
    return static_cast<int8_t>(status.rssi);
  }
  return WIFI_RSSI_DISCONNECTED;
}

int32_t WiFiComponent::get_wifi_channel() {
  wifi_iface_status status{};
  return get_iface_status(sta_iface, &status) ? static_cast<int32_t>(status.channel) : 0;
}

network::IPAddresses WiFiComponent::wifi_sta_ip_addresses() {
  network::IPAddresses addresses{};
  if (sta_iface == nullptr) {
    return addresses;
  }
  IpCollector collector{&addresses, 0};
  net_if_ipv4_addr_foreach(sta_iface, collect_ipv4, &collector);
#if USE_NETWORK_IPV6
  // Slot 0 holds the IPv4 address, as on ESP-IDF
  collector.index = std::max<uint8_t>(collector.index, 1);
  net_if_ipv6_addr_foreach(sta_iface, collect_ipv6, &collector);
#endif
  return addresses;
}

network::IPAddress WiFiComponent::wifi_subnet_mask_() {
  if (sta_iface == nullptr) {
    return {};
  }
  FirstIpv4 first;
  net_if_ipv4_addr_foreach(sta_iface, first_ipv4, &first);
  if (!first.found) {
    return {};
  }
  return ipv4_to_ip_address(net_if_ipv4_get_netmask_by_addr(sta_iface, &first.addr));
}

#ifdef USE_WIFI_LEASE_CACHE
// Only the address is kept: it seeds DHCPv4 INIT-REBOOT and the server confirms it.
bool WiFiComponent::lease_valid_for_apply_() const {
  const SavedWifiLeaseSettings &lease = this->cached_lease_;
  return lease.version == SAVED_WIFI_LEASE_VERSION && this->selected_sta_index_ >= 0 &&
         lease.ap_index == this->selected_sta_index_;
}

void WiFiComponent::save_lease_settings_() {
  if (g_sta_manual_ip || sta_iface == nullptr)
    return;
  FirstIpv4 first;
  net_if_ipv4_addr_foreach(sta_iface, first_ipv4, &first);
  if (!first.found)
    return;
  SavedWifiLeaseSettings lease{};
  lease.version = SAVED_WIFI_LEASE_VERSION;
  lease.ap_index = this->selected_sta_index_;
  lease.ip = first.addr.s_addr;
  if (memcmp(&lease, &this->cached_lease_, sizeof(lease)) == 0)
    return;  // unchanged; spare the flash
  this->lease_cache_pref_.save(&lease);
  this->cached_lease_ = lease;
  ESP_LOGD(TAG, "Lease cache: saved address (network %d)", lease.ap_index);
}
#endif

network::IPAddress WiFiComponent::wifi_gateway_ip_() {
  if (sta_iface == nullptr) {
    return {};
  }
  return ipv4_to_ip_address(net_if_ipv4_get_gw(sta_iface));
}

network::IPAddress WiFiComponent::wifi_dns_ip_(int num) {
  const dns_resolve_context *ctx = dns_resolve_get_default();
  for (const auto &server : ctx->servers) {
    const net_sockaddr *addr = &server.dns_server;
    if (addr->sa_family == AF_INET && !net_ipv4_is_addr_mcast(&net_sin(addr)->sin_addr)) {
      if (num-- == 0) {
        return ipv4_to_ip_address(net_sin(addr)->sin_addr);
      }
    }
#if USE_NETWORK_IPV6
    if (addr->sa_family == AF_INET6 && !net_ipv6_is_addr_mcast(&net_sin6(addr)->sin6_addr)) {
      if (num-- == 0) {
        return network::IPAddress(&net_sin6(addr)->sin6_addr);
      }
    }
#endif
  }
  return {};
}

bool WiFiComponent::wifi_loop_() {
  bool processed = false;
  ZephyrWiFiEvent event;
  while (k_msgq_get(&event_queue, &event, K_NO_WAIT) == 0) {
    processed = true;
    switch (event.type) {
      case ZephyrWiFiEvent::Type::CONNECT_RESULT:
        if (event.status == 0) {
          g_expect_disconnect = false;
          g_sta_status = WiFiSTAConnectStatus::CONNECTED;
          if (g_sta_manual_ip) {
            // Its IPV4_ADDR_ADD event came before the link did.
#if USE_NETWORK_IPV4
            this->got_ipv4_address_ = true;
            this->update_connected_state_();
#ifdef USE_WIFI_IP_STATE_LISTENERS
            this->notify_ip_state_listeners_();
#endif
#endif
          } else {
#ifdef USE_WIFI_LEASE_CACHE
            if (this->lease_valid_for_apply_()) {
              in_addr hint{};
              hint.s_addr = this->cached_lease_.ip;
              // Fails with -EBUSY if the client still runs; it then remembers the address itself
              if (net_dhcpv4_set_reboot_hint(sta_iface, &hint) == 0) {
                ESP_LOGD(TAG, "Lease cache: requesting the cached address");
              }
            }
#endif
            net_dhcpv4_start(sta_iface);
          }
#if USE_NETWORK_IPV6
          // Addresses kept from before this link raise no event
          this->num_ipv6_addresses_ = 0;
          net_if_ipv6_addr_foreach(sta_iface, count_ipv6, &this->num_ipv6_addresses_);
#endif
          // Deferred from setup() -- the driver wasn't started yet at that point (see
          // wifi_apply_power_save_()'s g_wifi_started guard), so apply it now that it is.
          if (!this->wifi_apply_power_save_()) {
            ESP_LOGV(TAG, "Setting Power Save Option failed");
          }
#ifdef USE_WIFI_CONNECT_STATE_LISTENERS
          this->pending_.connect_state = true;
#endif
        } else if (g_expect_disconnect) {
          // The earlier attempt that wifi_sta_connect_() ended
          ESP_LOGD(TAG, "Expected end of previous attempt");
          g_expect_disconnect = false;
        } else {
          const auto reason = static_cast<wifi_conn_status>(event.status);
          char ssid[SSID_BUFFER_SIZE];
          ESP_LOGW(TAG, "Connect failed ssid='%s' reason='%s'", this->wifi_ssid_to(ssid), wifi_conn_status_txt(reason));
          g_sta_status = reason == WIFI_STATUS_CONN_AP_NOT_FOUND ? WiFiSTAConnectStatus::ERROR_NETWORK_NOT_FOUND
                                                                 : WiFiSTAConnectStatus::ERROR_CONNECT_FAILED;
        }
        break;
      case ZephyrWiFiEvent::Type::DISCONNECT_RESULT: {
        const auto reason = static_cast<wifi_disconn_reason>(event.status);
        // While connecting, a requested leave can only be the previous AP's, even after the wait for it ended
        if (g_expect_disconnect ||
            (g_sta_status == WiFiSTAConnectStatus::CONNECTING && reason == WIFI_REASON_DISCONN_USER_REQUEST)) {
          // The attempt started after it keeps CONNECTING
          ESP_LOGD(TAG, "Expected disconnect from previous AP");
          g_expect_disconnect = false;
        } else {
          char ssid[SSID_BUFFER_SIZE];
#if KERNEL_VERSION_NUMBER >= 0x040500
          ESP_LOGW(TAG, "Disconnected ssid='%s' reason='%s'", this->wifi_ssid_to(ssid),
                   wifi_disconn_reason_txt(reason));
#else
          ESP_LOGW(TAG, "Disconnected ssid='%s' reason=%d", this->wifi_ssid_to(ssid), event.status);
#endif
          g_sta_status = WiFiSTAConnectStatus::IDLE;
        }
#if USE_NETWORK_IPV4
        this->got_ipv4_address_ = false;
#endif
#if USE_NETWORK_IPV6
        this->num_ipv6_addresses_ = 0;
#endif
        this->update_connected_state_();
#ifdef USE_WIFI_CONNECT_STATE_LISTENERS
        this->notify_disconnect_state_listeners_();
#endif
        break;
      }
      case ZephyrWiFiEvent::Type::AP_ENABLE_RESULT:
        if (event.status == 0) {
          ESP_LOGD(TAG, "AP enabled");
        } else {
          ESP_LOGE(TAG, "AP enable failed: %d", event.status);
          g_ap_enabled = false;
        }
        break;
      case ZephyrWiFiEvent::Type::AP_DISABLE_RESULT:
        if (event.status == 0) {
          ESP_LOGD(TAG, "AP disabled");
        } else {
          ESP_LOGW(TAG, "AP disable failed: %d", event.status);
          g_ap_enabled = true;
        }
        break;
#ifdef USE_WIFI_AP
      case ZephyrWiFiEvent::Type::AP_STA_CONNECTED:
        add_ap_station(event.mac);
        this->ap_clients_ = ap_station_count;
        ESP_LOGV(TAG, "AP client connected");
        break;
      case ZephyrWiFiEvent::Type::AP_STA_DISCONNECTED:
        remove_ap_station(event.mac);
        this->ap_clients_ = ap_station_count;
        ESP_LOGV(TAG, "AP client disconnected");
        break;
#endif
      case ZephyrWiFiEvent::Type::IPV4_ADDR_ADD:
#if USE_NETWORK_IPV4
        this->got_ipv4_address_ = true;
#endif
#ifdef USE_WIFI_LEASE_CACHE
        this->save_lease_settings_();
#endif
        this->update_connected_state_();
#ifdef USE_WIFI_IP_STATE_LISTENERS
        this->notify_ip_state_listeners_();
#endif
        break;
      case ZephyrWiFiEvent::Type::IPV4_ADDR_DEL: {
#if USE_NETWORK_IPV4
        FirstIpv4 first;
        net_if_ipv4_addr_foreach(sta_iface, first_ipv4, &first);
        this->got_ipv4_address_ = first.found;
        if (!first.found) {
          ESP_LOGV(TAG, "Lost IP");
        }
        this->update_connected_state_();
#endif
        break;
      }
#if USE_NETWORK_IPV6
      case ZephyrWiFiEvent::Type::IPV6_ADDRS_CHANGED:
        this->num_ipv6_addresses_ = 0;
        net_if_ipv6_addr_foreach(sta_iface, count_ipv6, &this->num_ipv6_addresses_);
        this->update_connected_state_();
#ifdef USE_WIFI_IP_STATE_LISTENERS
        this->notify_ip_state_listeners_();
#endif
        break;
#endif
    }
  }

  const atomic_val_t state = atomic_get(&scan_state);
  if (state == SCAN_STATE_DONE || state == SCAN_STATE_FAILED) {
    this->wifi_scan_finished_(state == SCAN_STATE_DONE);
    processed = true;
  }

  const atomic_val_t dropped = atomic_clear(&dropped_events);
  if (dropped > 0) {
    ESP_LOGW(TAG, "Dropped %ld WiFi events due to buffer overflow", static_cast<long>(dropped));
    // The previous AP's leave event may be among them, so it can no longer be told apart
    g_expect_disconnect = false;
  }
  return processed;
}

}  // namespace esphome::wifi
#endif  // USE_ZEPHYR
#endif  // USE_WIFI
