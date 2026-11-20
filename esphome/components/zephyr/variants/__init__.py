from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
from types import ModuleType
from typing import Any, Final

import esphome.config_validation as cv
from esphome.const import (
    CONF_ADVANCED,
    CONF_BOARD,
    CONF_FRAMEWORK,
    CONF_REFRESH,
    CONF_SOURCE,
    CONF_TYPE,
    CONF_URL,
    CONF_VERSION,
    KEY_CORE,
    KEY_FRAMEWORK_VERSION,
    KEY_TARGET_FRAMEWORK,
    TYPE_GIT,
    Toolchain,
)
from esphome.core import CORE, EsphomeError
from esphome.types import ConfigType

from ..board_revision import parse_board_string
from ..const import (
    BOOTLOADER_MCUBOOT,
    CONF_BOOTLOADER,
    CONF_NINJA_VERSION,
    CONF_SINGLE_SLOT,
    CONF_SNIPPETS,
    CONF_WEST_VERSION,
    KEY_BOARD_DIR_CACHE,
    KEY_BOARD_EDT_CACHE,
    KEY_BOARD_ROOT,
    KEY_BOARD_YAML_CACHE,
    KEY_BOOT_LAYOUT,
    KEY_BOOTLOADER,
    KEY_CPP_PATH,
    KEY_DTS_INCLUDE_PATHS,
    KEY_EXTRA_BUILD_FILES,
    KEY_FAMILY,
    KEY_FRAMEWORK_TYPE,
    KEY_KCONFIG,
    KEY_MODULE_OVERRIDES,
    KEY_MODULE_REQUESTS,
    KEY_OVERLAY,
    KEY_OVERLAY_BUILDER,
    KEY_PARTITION_REQUIREMENTS,
    KEY_PM_STATIC,
    KEY_PRJ_CONF,
    KEY_RUNNER,
    KEY_SHIELD_ROOT,
    KEY_SHIELDS,
    KEY_SINGLE_SLOT,
    KEY_SNIPPET_ROOT,
    KEY_SNIPPETS,
    KEY_SYSBUILD_CONF,
    KEY_USER,
    KEY_VARIANT,
    KEY_VARIANT_INFO,
    KEY_ZEPHYR,
    VERSION_RECOMMENDED,
)
from ..framework_helpers import FrameworkSource
from ..partitions import BootLayout

_LOGGER = logging.getLogger(__name__)


# A ZephyrVariant field value that means "use the family module's value"
FAMILY_DEFAULT: Final = "family default"


@dataclass(frozen=True)
class ZephyrManifest:
    """The west manifest repository a build starts from (Zephyr, NCS, etc.)."""

    # The `framework: type:` value that selects this manifest
    framework_type: str
    source: FrameworkSource
    manifest_rev: str = "recommended"
    # Prefix of the workspace folder, so manifests with the same version string don't share one
    workspace_prefix: str | None = None
    default_version: str = "4.5.0"
    min_version: cv.Version = cv.Version(4, 5, 0)
    # Set when this manifest has no tagged release usable as a default -- framework: source:
    # is then mandatory; explains why in the raised error.
    requires_source_reason: str | None = None
    # capability -> module. A module is additive to this manifest (fetched into its own folder
    # and added to the build, no boards/toolchain/version of its own) -- distinct from
    # alt_manifests, which are competing roots. See ZephyrModuleTemplate.
    modules: dict[str, "ZephyrModuleTemplate"] = field(default_factory=dict)


# A fully resolved, ready-to-fetch additive west module -- identical shape whether it
# came from a component's capability request or a user's own `framework: modules:` entry.
@dataclass
class ZephyrModule:
    name: str  # west project/module name
    manifest_url: str | None = None  # git remote; None when local_path is set
    local_path: Path | None = None  # user's `type: local` module
    revision: str = "main"  # concrete ref/tag/branch to check out
    refresh: float | None = None  # seconds between checks of revision; None = never


# Owned by the ZephyrManifest that can provide a capability; resolves to a concrete
# ZephyrModule given the active root manifest's version.
@dataclass
class ZephyrModuleTemplate:
    name: str
    manifest_url: str
    # root manifest's (major, minor) -> this module's matching version. Nordic pairs
    # sdk-nrf and ncs-zigbee releases but the two use unrelated numbering schemes
    # (3.4.x vs 1.4.x) -- no formula, each pairing is a real entry, added as Nordic
    # ships new paired releases.
    default_versions: dict[tuple[int, int], str] = field(default_factory=dict)
    # Lowest version this module is known to work at all -- independent of
    # default_versions' per-root-version pairing, this just rejects an obviously
    # too-old framework: modules: override at config time instead of failing much later
    # while fetching it. None = not enforced (e.g. a module with no numbered
    # releases at all).
    min_version: cv.Version | None = None

    def resolve(
        self, root_version: cv.Version, explicit_version: str | None = None
    ) -> ZephyrModule:
        version = explicit_version or self.default_versions.get(
            (root_version.major, root_version.minor)
        )
        if version is None:
            raise EsphomeError(
                f"Module '{self.name}' has no known compatible version for "
                f"framework version {root_version.major}.{root_version.minor} -- "
                "specify it explicitly under framework: modules:."
            )
        tag = version
        try:
            parsed_version = cv.Version.parse(version)
        except ValueError:
            pass  # a git ref/branch (e.g. "main"), not a semver -- used as-is, no "v"
        else:
            if self.min_version is not None and parsed_version < self.min_version:
                raise EsphomeError(
                    f"Module '{self.name}' requires version >= {self.min_version}. "
                    f"Got {version}."
                )
            # default_versions/explicit_version are bare "X.Y.Z" (for the comparison
            # above and for a user typing framework: modules: version: without having to
            # know the tag scheme), but the actual git tag needs the "v" -- same
            # normalization the root manifest's own ver_tag already does.
            if not tag.startswith("v"):
                tag = f"v{tag}"
        return ZephyrModule(
            name=self.name,
            manifest_url=self.manifest_url,
            revision=tag,
        )


@dataclass(frozen=True, kw_only=True)
class ZephyrVariant:
    """What zephyr needs to know to build one chip."""

    # e.g. "STM32C5", the `variant:` value in the YAML
    name: str
    # The module sibling chips share, whose FAMILY is its name; None = no family
    family_module: ModuleType | None = None
    manifest: ZephyrManifest
    # Other manifests `framework: type:` may select, e.g. mainline Zephyr beside NCS
    alt_manifests: tuple[ZephyrManifest, ...] = ()
    boards: list[str] = field(default_factory=list)  # empty = accept any board
    # Toolchain identifiers supported by this variant (StrEnum values from esphome.const.Toolchain).
    valid_toolchains: tuple[str, ...] = ("platformio",)
    # Zephyr SDK cross-compiler name (arg to setup.sh -t / ZEPHYR_TOOLCHAIN_VARIANT).
    toolchain: str | None = None
    # Binary blobs required by this variant: (west_module, allow_regex, sentinel_name).
    # allow_regex filters the module's blob list to the chip-specific subset, though
    # some HALs (e.g. hal_espressif with BLE) need ".*" since Zephyr's CMake verifies
    # the entire manifest regardless of chip. sentinel_name gates the one-time fetch.
    # None means no blobs are needed.
    blobs: tuple[str, str, str] | None = None
    # Pins per GPIO devicetree port node, used to split a flat pin number into
    # (port, pin-within-port). Varies by vendor/SoC family (32 for nRF52/Espressif,
    # 16 for STM32), so this lives per variant rather than a hardcoded constant.
    gpio_port_width: int = 32
    # Devicetree node-label suffixes for this variant's GPIO ports, indexed by port
    # number -- e.g. ("a", "b", "c", "d") for Silicon Labs' gpioa/gpiob/gpioc/gpiod
    # Series 2 GPIO ports. None = numeric suffixes (gpio0, gpio1, ...), which is what
    # every DT_NODELABEL(gpio{port}) lookup has always assumed -- Nordic and Espressif
    # both use numeric node names, only their *display* prefix differs (see
    # _PORT_BANKED_FAMILIES in gpio.py). A lettered family also gets its display
    # prefix from this (e.g. "PA"), independently of Nordic's numeric "P0." style.
    gpio_port_labels: tuple[str, ...] | None = None
    # Devicetree node-label prefix for this variant's GPIO port nodes. Defaults to
    # "gpio", which happens to be right for every variant wired up so far (Nordic,
    # Espressif, Silicon Labs, STM32, RP2040) -- but that's a coincidence, not a Zephyr
    # convention: a scan of the whole Zephyr dts/ tree turns up plenty of vendors that
    # name their GPIO port nodes something else entirely (Renesas RA: ioport0/ioport1;
    # Renesas RX and RA6, Microchip SAM, NXP MCX: porta/portb; TI: main_gpio; Xilinx
    # Zynq: psgpio_bank; Infineon: gpio_prt; several RISC-V vendors: pioa/piob). Even
    # different Renesas families disagree with each other (RA4M1 vs RA6 above).
    #
    # DO NOT assume "gpio" is safe for a new variant just because it's the default here
    # and every existing variant uses it. Before adding a new ZephyrVariant, grep that
    # SoC's actual .dtsi under the Zephyr SDK for "gpio-controller;" and read the node
    # label a few lines above it -- then set this field to match. Getting it wrong fails
    # every pin: at codegen when the board's devicetree is readable (gpio.py checks the
    # node exists), otherwise at runtime with "gpio %u is not ready." (the RA4M1 bug
    # this comment is here because of).
    gpio_node_prefix: str = "gpio"
    # ESPHome components this variant can actually support. Named after the
    # component/protocol, not the raw radio -- e.g. `openthread` and `zigbee` both need
    # 802.15.4, but esp32-family only has the former ported (ZBOSS is Nordic-only).
    # Known values: "wifi", "ble", "openthread", "zigbee", "ethernet", "modem".
    transports: frozenset[str] = frozenset()
    # Per-transport Zephyr driver facts, keyed by transport name. (kconfig, dt_label):
    # kconfig is the driver's top-level Kconfig symbol (e.g. "WIFI_ESP32"), dt_label the
    # devicetree node to mark `status = "okay"`. Only transports needing this simple
    # "enable one Kconfig + one DT node" shape use it -- openthread/zigbee configure
    # their own Kconfig directly instead.
    transport_drivers: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Per-transport binary blobs, keyed by transport name -- same (west_module,
    # allow_regex, sentinel_name) shape as `blobs` above, but only fetched when that
    # transport is actually configured (unlike `blobs`, which always runs for the
    # variant). Use this when the blobs are for hardware not every board of the variant
    # has (e.g. rp2040's CYW43439 wifi chip, only present on `/w`-qualified boards).
    transport_blobs: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    # Wi-Fi security modes this variant's driver can connect with, beyond open and WPA2,
    # mapped to the Kconfig symbols each needs. Known values: "wpa", "wpa3", "eap". A mode with no
    # entry is rejected at validation.
    wifi_features: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # West board target segments (e.g. "esp32c6"/"hpcore"), used by qualify_board() to
    # expand a bare `board:` name. None = no segment.
    soc: str | None = None
    qualifier: str | None = None
    # MCUboot upgrade modes this variant's upstream port actually supports -- checked
    # against ota: platform: esphome's swap_method:. Empty = no real MCUboot/OTA rollback
    # applies (siwx917).
    swap_methods: frozenset[str] = frozenset()
    # GPIO -> ADC1 channel index, from the vendor SoC header (not discoverable from any
    # board's DTS -- ADC pins are fixed-function silicon). Empty = no ADC1 support wired up.
    # Espressif-shaped: their devicetree channel@N address IS the real silicon channel.
    adc1_channel_map: dict[int, int] = field(default_factory=dict)
    # GPIO -> SAADC analog-input name (e.g. "AIN5"), fixed-function silicon per the
    # vendor datasheet. Nordic-shaped: unlike Espressif, the devicetree channel@N
    # address is just an arbitrary config slot -- the real pin connection is this
    # AIN name, via a separate `zephyr,input-positive` property. Empty = no SAADC
    # support wired up.
    adc_ain_map: dict[int, str] = field(default_factory=dict)
    # Devicetree labels internal_temperature sets `status = "okay"` on: the on-chip die
    # temperature sensor node first, then any node it reads through (its ADC). Only the
    # status is set; the chip and board devicetree supply everything else. Empty = not
    # wired up, and the sensor is found through a `die-temp0` alias the user provides.
    die_temp_nodes: tuple[str, ...] = ()
    # GPIOs the ESP32 GPIO matrix can route to a UART TX/RX signal, from Zephyr's
    # dt-bindings pinctrl header -- a fixed per-chip pinmux fact, not always identical
    # between tx/rx (e.g. original ESP32's GPIO34-39 are RX-only). Empty = no
    # GPIO-matrix UART pin override wired up. Only for a free-mux family where any valid
    # tx/rx pin works on any instance -- see uart_valid_pins_by_instance otherwise.
    uart_valid_pins: dict[str, frozenset[int]] = field(default_factory=dict)
    # Like uart_valid_pins, but keyed by instance ("UART0", "UART1", ...) first -- for a
    # family (e.g. RP2040) whose GPIO function-select mux ties each pin to exactly one
    # fixed instance+role, verified against its real pinctrl dt-bindings header.
    uart_valid_pins_by_instance: dict[str, dict[str, frozenset[int]]] = field(
        default_factory=dict
    )
    # Like uart_valid_pins_by_instance, but for I2C sda/scl (e.g. RP2040).
    i2c_valid_pins_by_instance: dict[str, dict[str, frozenset[int]]] = field(
        default_factory=dict
    )
    # I2C instance ("I2C21") -> the GPIO ports of its power domain; SDA and SCL must be on
    # one of them, both on the same port (nRF54L datasheet). Empty = no port rule.
    i2c_ports_by_instance: dict[str, tuple[int, ...]] = field(default_factory=dict)
    # Like uart_valid_pins, but for SPI's clk/mosi/miso. Empty = not wired up
    # (left to zephyr_setup_spi_pinctrl() at codegen time instead).
    spi_valid_pins: dict[str, frozenset[int]] = field(default_factory=dict)
    # signal ("clk"/"mosi"/"miso") -> {pin: pinmux macro name}, for a family whose
    # valid SPI pins don't follow a per-pin formula (unlike EFR32/Nordic/ESP32,
    # which derive the macro from the raw pin number) -- e.g. SiWx91x, where only
    # a handful of enumerated pins have a macro at all and each one is a distinct
    # #define, not a formula. Consulted by zephyr_setup_spi_pinctrl()'s
    # family-specific branch instead of building the macro name programmatically.
    # Empty for every family that does use a formula.
    spi_pin_macros: dict[str, dict[int, str]] = field(default_factory=dict)
    # Devicetree node label of this variant's pinctrl controller. Every family
    # wired up so far names it "pinctrl" (a shared convention across EFR32/
    # Nordic/ESP32/RP2040), which the SPI/UART/I2C pinctrl-overlay builders in
    # zephyr/__init__.py hardcode -- SiWx91x is the first exception, whose SoC
    # dtsi names it "pinctrl0" instead. Threaded through the SPI, UART and I2C
    # pinctrl-overlay builders.
    pinctrl_node_label: str = "pinctrl"
    # Devicetree node labels backing the `logger: hardware_uart: UART0`/`UART1` symbolic
    # selections. ESP32-family and nRF52 boards both label their two console-capable UARTs
    # uart0/uart1, so that's the default; nRF54 series numbers peripheral instances instead
    # (e.g. uart20/uart30 on the nrf54l15dk), so that variant overrides this map.
    uart_node_labels: dict[str, str] = field(
        default_factory=lambda: {"UART0": "uart0", "UART1": "uart1"}
    )
    # Devicetree node labels of this variant's PWM peripheral instances, in
    # zephyr_pwm's block-allocation order. nRF52840 numbers them pwm0-pwm3; nRF54L
    # series numbers peripheral instances instead (pwm20-pwm22), same convention as
    # uart_node_labels above. RP2040/RP2350 expose a single "pwm" controller node
    # covering every slice, so their list repeats that same label once per slice
    # (block-allocation order still needs one entry per block). Empty = no PWM
    # support wired up.
    pwm_node_labels: list[str] = field(default_factory=list)
    # Channels per PWM block on this variant's hardware -- a block is one counter
    # with a single shared period, so this many channels must share one frequency.
    # nRF52/nRF54L's NRF_PWM peripheral has 4 channels per instance (the default).
    # RP2040/RP2350's PWM slices have only 2 channels (A/B) each. Only consulted by
    # zephyr_pwm's free (Nordic-style) block allocator -- RP2040/RP2350/RA4M1 assign
    # each pin a fixed block+channel instead (see pwm_pin_map), so this field is
    # descriptive only for those families.
    pwm_channels_per_block: int = 4
    # GPIO -> (index into pwm_node_labels, local channel within that block) for
    # variants whose PWM pins are wired to one fixed hardware position each, rather
    # than freely assignable to any channel the way Nordic's NRF_PSEL pinmux is.
    # Empty = this variant's PWM (if any) uses the free allocator instead.
    pwm_pin_map: dict[int, tuple[int, int]] = field(default_factory=dict)
    # Largest `<platform>: watchdog_timeout:` this variant's WDT hardware can actually
    # arm, in milliseconds. None = trust the generic 5-60s schema range (true for
    # every variant whose WDT clock gives it a multi-minute+ ceiling). Some
    # variants' watchdog peripheral is clocked too fast, or has too small a
    # counter, for that range -- the driver just silently substitutes a shorter
    # real timeout with no error when asked for more than it can give. Set this so
    # config validation can catch that instead of a silent boot-loop on real
    # hardware. Value should leave headroom below the real computed ceiling for
    # rounding-table granularity, not sit exactly on it.
    watchdog_max_timeout_ms: int | None = None
    # Where this chip starts the first image (MCUboot when built, else the app) and the
    # flash it keeps for itself; checked against the merged devicetree at build time
    # (see partitions.py). None = not checked.
    boot: BootLayout | None = None
    # Under mainline Zephyr, the hardware RNG is behind a second core that isn't built,
    # so an insecure software random number generator is used. NCS has its own source
    # on the main core.
    rng_needs_second_core: bool = False
    # Boards whose resident vendor bootloader moves `boot`, keyed by board name without
    # revision or qualifiers.
    board_boot: dict[str, BootLayout] = field(default_factory=dict)
    # CPU architecture, as Zephyr's ARCH: "arm", "riscv" or "xtensa". FAMILY_DEFAULT
    # takes the family module's CPU_ARCH.
    cpu_arch: str = FAMILY_DEFAULT
    # zephyr_mcumgr OTA is supported (an MCUboot layout and SMP transport work).
    mcumgr_ota: bool = False
    # Kind of USB UART the chip has: "cdc_acm" (a USB device controller, zephyr_udc0),
    # "serial_jtag" (Espressif's USB Serial/JTAG) or None. FAMILY_DEFAULT takes the
    # family module's USB_UART_TYPE; set it only where a variant differs.
    usb_uart_type: str | None = FAMILY_DEFAULT
    # zephyr_mcumgr may use the CDC/CDC1 transports. FAMILY_DEFAULT takes the family
    # module's MCUMGR_CDC.
    mcumgr_cdc: bool = FAMILY_DEFAULT
    # The SoC selects HAS_POWEROFF, which deep_sleep's sys_poweroff() needs.
    # FAMILY_DEFAULT takes the family module's HAS_POWEROFF.
    has_poweroff: bool = FAMILY_DEFAULT

    @property
    def manifests(self) -> tuple[ZephyrManifest, ...]:
        """Its own manifest first, then the alternatives."""
        return (self.manifest, *self.alt_manifests)

    def manifest_for(self, framework_type: str | None) -> ZephyrManifest:
        """The manifest a `framework: type:` selects; None selects its own."""
        if framework_type is None:
            return self.manifest
        return {m.framework_type: m for m in self.manifests}[framework_type]


@dataclass(frozen=True)
class ZephyrVariantResolved:
    """A variant with the manifest and version its `framework:` block resolved to."""

    variant: ZephyrVariant
    manifest: ZephyrManifest
    version: cv.Version


def _release_order(version: cv.Version) -> tuple[int, int, int, bool, str, int]:
    """Sort key in which a release follows its prereleases and rc2 comes before rc10."""
    if (suffix := re.fullmatch(r"(\D*)(\d*)", version.extra)) is None:
        raise ValueError(f"Cannot order the version suffix {version.extra!r}")
    return (
        version.major,
        version.minor,
        version.patch,
        not version.extra,
        suffix[1],
        int(suffix[2] or 0),
    )


def mcuboot_or_none(advanced: dict) -> str:
    """Return MCUboot if advanced: bootloader: selects it, else "" -- the value
    variants with an opt-in bootloader pass to set_variant_core_data()."""
    return (
        BOOTLOADER_MCUBOOT
        if advanced.get(CONF_BOOTLOADER) == BOOTLOADER_MCUBOOT
        else ""
    )


def board_boot_layout(variant: ZephyrVariant, board: str) -> BootLayout | None:
    """Return the boot layout for `board`: its own override, else the variant's."""
    return variant.board_boot.get(parse_board_string(board).name, variant.boot)


def qualify_board(
    variant: ZephyrVariant, board: str, qualifier: str | None = None
) -> str:
    """Expand a bare board name into its full west target string. Already-qualified input
    is left untouched.

    qualifier overrides variant.qualifier for variants where the qualifier segment isn't
    a fixed hardware fact (e.g. RP2040's board/soc/mcuboot vs. board/soc split depends on
    a user config choice, not the chip itself) -- None (the default) falls back to
    variant.qualifier, unchanged behavior for every other variant.
    """
    if "/" in board or variant.soc is None:
        return board
    qualified = f"{board}/{variant.soc}"
    qualifier = variant.qualifier if qualifier is None else qualifier
    if qualifier:
        qualified += f"/{qualifier}"
    return qualified


def resolve_variant(
    variant: ZephyrVariant, config: ConfigType, min_version_reason: str
) -> ZephyrVariantResolved:
    """Resolve `framework: type:`, `version:` and `source:` of a variant's config.

    ``min_version_reason`` says why the manifest's minimum exists, e.g. "mainline
    ESP32-C6 support". A version other than the manifest's default only warns.
    """
    framework = config[CONF_FRAMEWORK]
    framework_types = [m.framework_type for m in variant.manifests]
    framework_type = framework.get(CONF_TYPE)
    if framework_type is not None and framework_type not in framework_types:
        raise cv.Invalid(
            f"'{framework_type}' is not a valid framework type for {variant.name}. "
            f"Valid: {framework_types!r}",
            [CONF_FRAMEWORK, CONF_TYPE],
        )
    manifest = variant.manifest_for(framework_type)
    default_version = manifest.default_version
    min_version = manifest.min_version

    if CONF_VERSION in framework and CONF_SOURCE in framework:
        raise cv.Invalid(
            "'version' and 'source' are mutually exclusive within 'framework:' -- "
            "'source' determines the version from the source itself.",
            [CONF_FRAMEWORK, CONF_SOURCE],
        )

    source = framework.get(CONF_SOURCE)
    if source is None and manifest.requires_source_reason is not None:
        raise cv.Invalid(
            f"framework: type: {manifest.framework_type} has no usable default version -- "
            f"{manifest.requires_source_reason}. 'source:' is required.",
            [CONF_FRAMEWORK, CONF_TYPE],
        )

    if source is not None:
        from ..dts_fetch import (  # deferred: avoid import cycle (dts_fetch imports VARIANTS)
            resolve_sdk_source_version,
        )

        if source[CONF_TYPE] == TYPE_GIT and CONF_URL not in source:
            source[CONF_URL] = manifest.source.url
        with cv.prepend_path([CONF_FRAMEWORK, CONF_SOURCE]):
            version_str = resolve_sdk_source_version(source, framework[CONF_REFRESH])
    else:
        version_str = str(framework.get(CONF_VERSION, default_version))
        if version_str == VERSION_RECOMMENDED:
            version_str = default_version

    try:
        framework_ver = cv.Version.parse(version_str)
        framework_order = _release_order(framework_ver)
    except ValueError as err:
        raise cv.Invalid(
            f"'{version_str}' is not a version number", [CONF_FRAMEWORK, CONF_VERSION]
        ) from err
    if framework_order < _release_order(min_version):
        raise cv.Invalid(
            f"{variant.name} requires {manifest.framework_type} >= {min_version} "
            f"({min_version_reason}). Got {version_str}.",
            [CONF_FRAMEWORK, CONF_VERSION],
        )
    if source is None and framework_ver != cv.Version.parse(default_version):
        _LOGGER.warning(
            "The selected %s version is not the recommended one (%s). "
            "If there are connectivity or build issues please remove the manual version.",
            manifest.framework_type,
            default_version,
        )
    return ZephyrVariantResolved(variant, manifest, framework_ver)


def set_variant_core_data(
    resolved: ZephyrVariantResolved,
    config: ConfigType,
    *,
    bootloader: str,
    runner: str | None = None,
    sdk_source: ConfigType | None = None,
    prj_conf: dict | None = None,
    sysbuild_conf: dict | None = None,
    overlay: dict | None = None,
    overlay_builder: list | None = None,
    extra_build_files: dict | None = None,
    pm_static: list | None = None,
    user: dict | None = None,
    kconfig: str | None = None,
    fake_board_manifest: str | None = None,
    cpp_path: str | None = None,
    board_dir_cache: dict | None = None,
    dts_include_paths: list | None = None,
    board_edt_cache: dict | None = None,
    board_yaml_cache: dict | None = None,
    board_root: Path | None = None,
    shields: list[str] | None = None,
    shield_root: Path | None = None,
    snippet_root: Path | None = None,
) -> None:
    """Fill CORE.data for a variant's config_schema; ``bootloader`` is "" for none.
    The keywords after ``runner`` replace a field's start value."""
    # deferred: zephyr's __init__ may import this package
    from .. import zephyr_data, zephyr_set_core_data

    zephyr_set_core_data({**config, KEY_BOOTLOADER: bootloader})
    variant = resolved.variant
    zephyr_data()[KEY_VARIANT] = variant.name
    zephyr_data()[KEY_VARIANT_INFO] = variant
    zephyr_data()[KEY_FAMILY] = (
        variant.family_module.FAMILY if variant.family_module else None
    )
    zephyr_data()[KEY_FRAMEWORK_TYPE] = resolved.manifest.framework_type
    # The devicetree caches, filled as code generation reads the board
    zephyr_data()[KEY_CPP_PATH] = ""
    zephyr_data()[KEY_DTS_INCLUDE_PATHS] = None
    zephyr_data()[KEY_BOARD_DIR_CACHE] = {}
    zephyr_data()[KEY_BOARD_EDT_CACHE] = {}
    CORE.data[KEY_CORE][KEY_TARGET_FRAMEWORK] = KEY_ZEPHYR
    CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION] = resolved.version
    zephyr_data()[KEY_BOARD_YAML_CACHE] = {}
    zephyr_data()["fake_board_manifest"] = None
    zephyr_data()["blobs"] = []
    zephyr_data()[KEY_RUNNER] = runner
    zephyr_data()[KEY_BOOT_LAYOUT] = board_boot_layout(variant, config[CONF_BOARD])
    zephyr_data()[KEY_SYSBUILD_CONF] = {}
    framework = config.get(CONF_FRAMEWORK, {})
    advanced = framework.get(CONF_ADVANCED, {})
    zephyr_data()["sdk_source"] = sdk_source
    zephyr_data()["west_version"] = advanced.get(CONF_WEST_VERSION)
    zephyr_data()["ninja_version"] = advanced.get(CONF_NINJA_VERSION)
    zephyr_data()[KEY_BOARD_ROOT] = None
    zephyr_data()[KEY_SNIPPETS] = framework.get(CONF_SNIPPETS, [])
    zephyr_data()[KEY_SHIELDS] = []
    zephyr_data()[KEY_SHIELD_ROOT] = None
    zephyr_data()[KEY_SNIPPET_ROOT] = None
    zephyr_data()[KEY_SINGLE_SLOT] = framework.get(CONF_SINGLE_SLOT, False)
    zephyr_data()[KEY_PARTITION_REQUIREMENTS] = []
    zephyr_data()[KEY_USER] = {}
    zephyr_data()[KEY_MODULE_REQUESTS] = {}
    zephyr_data()[KEY_MODULE_OVERRIDES] = {}
    zephyr_data()["rtt_channels"] = {}
    if prj_conf is not None:
        zephyr_data()[KEY_PRJ_CONF] = prj_conf
    if sysbuild_conf is not None:
        zephyr_data()[KEY_SYSBUILD_CONF] = sysbuild_conf
    if overlay is not None:
        zephyr_data()[KEY_OVERLAY] = overlay
    if overlay_builder is not None:
        zephyr_data()[KEY_OVERLAY_BUILDER] = overlay_builder
    if extra_build_files is not None:
        zephyr_data()[KEY_EXTRA_BUILD_FILES] = extra_build_files
    if pm_static is not None:
        zephyr_data()[KEY_PM_STATIC] = pm_static
    if user is not None:
        zephyr_data()[KEY_USER] = user
    if kconfig is not None:
        zephyr_data()[KEY_KCONFIG] = kconfig
    if fake_board_manifest is not None:
        zephyr_data()["fake_board_manifest"] = fake_board_manifest
    if cpp_path is not None:
        zephyr_data()[KEY_CPP_PATH] = cpp_path
    if board_dir_cache is not None:
        zephyr_data()[KEY_BOARD_DIR_CACHE] = board_dir_cache
    if dts_include_paths is not None:
        zephyr_data()[KEY_DTS_INCLUDE_PATHS] = dts_include_paths
    if board_edt_cache is not None:
        zephyr_data()[KEY_BOARD_EDT_CACHE] = board_edt_cache
    if board_yaml_cache is not None:
        zephyr_data()[KEY_BOARD_YAML_CACHE] = board_yaml_cache
    if board_root is not None:
        zephyr_data()[KEY_BOARD_ROOT] = board_root
    if shields is not None:
        zephyr_data()[KEY_SHIELDS] = shields
    if shield_root is not None:
        zephyr_data()[KEY_SHIELD_ROOT] = shield_root
    if snippet_root is not None:
        zephyr_data()[KEY_SNIPPET_ROOT] = snippet_root
    CORE.toolchain = Toolchain.ZEPHYR


ZEPHYR_MAINLINE: ZephyrManifest = ZephyrManifest(
    framework_type="zephyr",
    source=FrameworkSource(
        name="Zephyr",
        url="https://github.com/zephyrproject-rtos/zephyr",
        # Every vendor HAL is fetched only when a build needs it. nrf_hw_models are
        # BabbleSim's nRF models; their module needs hal_nordic, and no variant
        # builds for BabbleSim
        optional=("hal_.*", "nrf_hw_models"),
        # Projects in Zephyr's hal group that match "hal_.*" but belong to no SoC
        # family: Xtensa, the Arm Ethos-U NPU, and the external chip libraries any
        # board can carry
        required=frozenset(
            {
                "hal_afbr",
                "hal_ethos_u",
                "hal_infineon",
                "hal_st",
                "hal_tdk",
                "hal_wurthelektronik",
                "hal_xtensa",
            }
        ),
    ),
    workspace_prefix="zephyr",
)

# Nordic's ZBOSS Zigbee stack, split out of sdk-nrf into this separate add-on repo as of
# NCS 3.4.0; see https://github.com/esphomeplatformzephyr/esphome/issues/31. Additive to
# NCS (built on top of the sdk-nrf workspace, no boards/toolchain/version of its own) -- not a
# competing root, so it's a module, not an alt_manifests entry. sdk-nrf and ncs-zigbee are
# versioned independently by Nordic (3.4.x vs 1.4.x, no formula between the two), hence
# the explicit per-pairing table rather than a single default_version.
NCS_ZIGBEE_TEMPLATE: ZephyrModuleTemplate = ZephyrModuleTemplate(
    name="ncs-zigbee",
    manifest_url="https://github.com/nrfconnect/ncs-zigbee",
    default_versions={(3, 4): "1.4.0"},
    min_version=cv.Version(1, 4, 0),
)

# nRF Connect SDK (NCS) -- Nordic's own vendor SDK, a manifest-of-manifests around a
# bundled Zephyr fork (nrfconnect/sdk-zephyr) rather than a single repo. boards/dts/
# for real Nordic silicon live in that bundled fork, not in the nrf manifest repo
# itself. 3.4.0: Nordic's stated
# minimum for this integration; its bundled zephyr fork reports 4.4.0, though NCS
# cherry-picks fixes independently of
# upstream release numbers, so that number is not a reliable cross-SDK gating signal.
NRF_CONNECT_SDK: FrameworkSource = FrameworkSource(
    name="nRF Connect SDK",
    url="https://github.com/nrfconnect/sdk-nrf",
    optional=(".*",),
    # Projects every build needs; components add others with include_west_project()
    required=frozenset({"cmsis", "hal_nordic", "nrfxlib", "zephyr"}),
)

NCS: ZephyrManifest = ZephyrManifest(
    framework_type="ncs",
    source=NRF_CONNECT_SDK,
    workspace_prefix="sdk-nrf",
    default_version="3.4.1",
    min_version=cv.Version(3, 4, 0),
    modules={"zigbee": NCS_ZIGBEE_TEMPLATE},
)

# Silicon Labs' "Simplicity SDK for Zephyr" -- same manifest-of-manifests shape as NCS:
# a vendor overlay repo (mcuboot/tf-psa-crypto overrides, hal_silabs, extra HAL modules)
# around a bundled Zephyr fork (SiliconLabsSoftware/zephyr) pinned by raw commit SHA in
# zephyr-silabs's own west.yml. Real board/DTS support for chips not yet upstreamed to
# mainline Zephyr lives in that bundled fork.
SILABS: ZephyrManifest = ZephyrManifest(
    framework_type="silabs",
    # Nothing left out: the manifest's own name-allowlist already limits the projects
    source=FrameworkSource(
        name="Silicon Labs SDK",
        url="https://github.com/SiliconLabsSoftware/zephyr-silabs",
        optional=(),
        required=frozenset(),
    ),
    workspace_prefix="zephyr-silabs",
    default_version="2026.6.0",
    min_version=cv.Version(2026, 6, 0),
)


def variant_value(info: ZephyrVariant, name: str) -> Any:
    """A ZephyrVariant field, with FAMILY_DEFAULT resolved to the family module's
    upper-case attribute of the same name (None when the family sets none)."""
    if (value := getattr(info, name)) != FAMILY_DEFAULT:
        return value
    if info.family_module is None:
        return None
    return getattr(info.family_module, name.upper(), None)
