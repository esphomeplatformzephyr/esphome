"""Tests for the shared Zephyr variant code."""

from collections.abc import Callable
from dataclasses import replace
from importlib import import_module
import logging
import pkgutil
from types import ModuleType

import pytest

from esphome.components.zephyr import (
    zephyr_framework_type,
    zephyr_set_core_data,
    zephyr_variant,
    zephyr_variant_family_module,
    zephyr_variant_family_name,
    zephyr_variant_manifest,
    zephyr_variant_name,
)
from esphome.components.zephyr.const import KEY_BOOTLOADER, KEY_ZEPHYR
from esphome.components.zephyr.framework_helpers import FrameworkSource
from esphome.components.zephyr.variants import (
    ZEPHYR_MAINLINE,
    ZephyrManifest,
    ZephyrModule,
    ZephyrModuleTemplate,
    ZephyrVariant,
    ZephyrVariantResolved,
    resolve_variant,
    set_variant_core_data,
)
import esphome.config_validation as cv
from esphome.const import (
    CONF_ADVANCED,
    CONF_BOARD,
    CONF_FRAMEWORK,
    CONF_SOURCE,
    CONF_TYPE,
    CONF_VERSION,
    KEY_CORE,
    KEY_FRAMEWORK_VERSION,
    KEY_TARGET_FRAMEWORK,
    KEY_TARGET_PLATFORM,
    PLATFORM_ESP32,
    PLATFORM_NRF52,
    PLATFORM_RP2,
    PLATFORM_STM32,
    Toolchain,
)
from esphome.core import CORE, EsphomeError
from tests.unit_tests.components.zephyr.zephyr_state import VARIANTS, empty_zephyr_data

_MANIFEST: ZephyrManifest = ZephyrManifest(
    framework_type="zephyr",
    source=FrameworkSource(
        name="Zephyr",
        url="https://example.com/zephyr",
        optional=("hal_.*",),
        required=frozenset(),
    ),
    workspace_prefix="zephyr",
    default_version="4.5.0",
    min_version=cv.Version(4, 4, 0),
)
_FAMILY_MODULE: ModuleType = ModuleType("chipfam_family")
_FAMILY_MODULE.FAMILY = "chipfam"
_VARIANT: ZephyrVariant = ZephyrVariant(
    name="chip1", family_module=_FAMILY_MODULE, manifest=_MANIFEST
)
_VENDOR_MANIFEST: ZephyrManifest = ZephyrManifest(
    framework_type="vendor",
    source=FrameworkSource(
        name="Vendor",
        url="https://example.com/vendor",
        optional=(),
        required=frozenset(),
    ),
    workspace_prefix="vendor",
    default_version="2.0.0",
    min_version=cv.Version(2, 0, 0),
)
_ALT_VARIANT: ZephyrVariant = ZephyrVariant(
    name="chip1", manifest=_MANIFEST, alt_manifests=(_VENDOR_MANIFEST,)
)

_ZEPHYR_PLATFORMS: tuple[str, ...] = (
    "esp32",
    "nrf",
    "nxp",
    "renesas",
    "rp2",
    "silabs",
    "stm32",
)


def _config(
    version: str | None = None, framework_type: str | None = None
) -> dict[str, object]:
    framework: dict[str, str] = {}
    if version is not None:
        framework[CONF_VERSION] = version
    if framework_type is not None:
        framework[CONF_TYPE] = framework_type
    return {CONF_BOARD: "chip1_dk", KEY_BOOTLOADER: "", CONF_FRAMEWORK: framework}


def test_zephyr_mainline_default_is_its_minimum() -> None:
    assert (
        cv.Version.parse(ZEPHYR_MAINLINE.default_version) == ZEPHYR_MAINLINE.min_version
    )


@pytest.mark.parametrize("version", [None, "recommended", "4.5.0"])
@pytest.mark.parametrize("framework_type", [None, "zephyr"])
def test_version_defaults_to_the_manifest_default(
    version: str | None, framework_type: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    assert resolve_variant(
        _VARIANT, _config(version, framework_type), "chip1 support"
    ) == ZephyrVariantResolved(_VARIANT, _MANIFEST, cv.Version(4, 5, 0))
    assert "not the recommended one" not in caplog.text


def test_type_selects_an_alternative_manifest() -> None:
    assert resolve_variant(
        _ALT_VARIANT, _config(framework_type="vendor"), "chip1 support"
    ) == ZephyrVariantResolved(_ALT_VARIANT, _VENDOR_MANIFEST, cv.Version(2, 0, 0))


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(cv.Invalid, match=r"Valid: \['zephyr', 'vendor'\]") as err:
        resolve_variant(_ALT_VARIANT, _config(framework_type="other"), "chip1 support")
    assert err.value.path == [CONF_FRAMEWORK, CONF_TYPE]


def test_version_other_than_the_default_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        resolved = resolve_variant(_VARIANT, _config("4.4.0"), "chip1 support")
    assert resolved.version == cv.Version(4, 4, 0)
    assert "not the recommended one (4.5.0)" in caplog.text


@pytest.mark.parametrize("version", ["v4.5.0", "4.5.0-rc1a"])
def test_version_that_is_not_a_number_is_rejected(version: str) -> None:
    with pytest.raises(cv.Invalid, match=f"'{version}' is not a version number") as err:
        resolve_variant(_VARIANT, _config(version), "chip1 support")
    assert err.value.path == [CONF_FRAMEWORK, CONF_VERSION]


@pytest.mark.parametrize("version", ["4.3.0", "4.4.0-rc1"])
def test_version_below_the_minimum_is_rejected(version: str) -> None:
    """A prerelease of the minimum is below it too."""
    with pytest.raises(cv.Invalid, match=r"zephyr >= 4.4.0 \(chip1 support\)") as err:
        resolve_variant(_VARIANT, _config(version), "chip1 support")
    assert err.value.path == [CONF_FRAMEWORK, CONF_VERSION]


def test_release_meets_a_prerelease_minimum() -> None:
    manifest = replace(
        _MANIFEST, default_version="4.5.0", min_version=cv.Version(4, 5, 0, "rc2")
    )
    variant = ZephyrVariant(name="chip1", manifest=manifest)
    for version in ("4.5.0", "4.5.0-rc10"):
        resolve_variant(variant, _config(version), "chip1 support")


@pytest.mark.parametrize(
    ("framework_type", "manifest"),
    [(None, _MANIFEST), ("zephyr", _MANIFEST), ("vendor", _VENDOR_MANIFEST)],
)
def test_manifest_for(framework_type: str | None, manifest: ZephyrManifest) -> None:
    assert _ALT_VARIANT.manifest_for(framework_type) is manifest


def test_manifest_for_unknown_type_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        _ALT_VARIANT.manifest_for("other")


def test_version_and_source_together_are_rejected() -> None:
    config = _config("4.5.0")
    config[CONF_FRAMEWORK][CONF_SOURCE] = {CONF_TYPE: "local", "path": "/dummy"}
    with pytest.raises(cv.Invalid, match="mutually exclusive"):
        resolve_variant(_VARIANT, config, "chip1 support")


@pytest.mark.parametrize(
    ("variant", "manifest", "family"),
    [(_VARIANT, _MANIFEST, "chipfam"), (_ALT_VARIANT, _VENDOR_MANIFEST, None)],
)
def test_set_variant_core_data(
    variant: ZephyrVariant, manifest: ZephyrManifest, family: str | None
) -> None:
    CORE.data[KEY_CORE] = {}

    config = {CONF_BOARD: "chip1_dk", CONF_FRAMEWORK: {}}
    resolved = ZephyrVariantResolved(variant, manifest, cv.Version(4, 5, 0))
    set_variant_core_data(resolved, config, bootloader="mcuboot")

    assert zephyr_variant_name() == "chip1"
    assert zephyr_variant_family_name() == family
    assert zephyr_framework_type() == manifest.framework_type
    assert zephyr_variant_manifest() is manifest
    assert zephyr_variant() is variant
    assert zephyr_variant_family_module() is variant.family_module
    assert CORE.data["zephyr"][CONF_BOARD] == "chip1_dk"
    assert CORE.data["zephyr"][KEY_BOOTLOADER] == "mcuboot"
    assert CORE.data[KEY_CORE][KEY_TARGET_FRAMEWORK] == "zephyr"
    assert CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION] == cv.Version(4, 5, 0)


_LOOKUPS: tuple[Callable[[], object], ...] = (
    zephyr_variant_name,
    zephyr_variant_family_name,
    zephyr_framework_type,
    zephyr_variant_manifest,
    zephyr_variant,
    zephyr_variant_family_module,
)


def test_no_variant_on_nrf52_or_other_platforms() -> None:
    """nrf52 sets up Zephyr data without a variant; other platforms have none."""
    CORE.data[KEY_CORE] = {
        KEY_TARGET_PLATFORM: PLATFORM_ESP32,
        KEY_TARGET_FRAMEWORK: "esp-idf",
    }
    assert [lookup() for lookup in _LOOKUPS] == [None] * len(_LOOKUPS)
    CORE.data[KEY_CORE] = {
        KEY_TARGET_PLATFORM: PLATFORM_NRF52,
        KEY_TARGET_FRAMEWORK: "zephyr",
    }
    zephyr_set_core_data(_config())
    assert [lookup() for lookup in _LOOKUPS] == [None] * len(_LOOKUPS)


@pytest.mark.parametrize("platform", [None, PLATFORM_ESP32, PLATFORM_STM32])
@pytest.mark.parametrize("lookup", _LOOKUPS)
def test_variant_read_before_the_platform_schema_is_rejected(
    lookup: Callable[[], object], platform: str | None
) -> None:
    """Until the platform schema sets the target framework, even esp32 may be Zephyr."""
    CORE.data[KEY_CORE] = {} if platform is None else {KEY_TARGET_PLATFORM: platform}
    with pytest.raises(RuntimeError, match="before the platform schema set it"):
        lookup()


@pytest.mark.parametrize("lookup", _LOOKUPS)
def test_variant_read_on_zephyr_without_zephyr_data_is_rejected(
    lookup: Callable[[], object],
) -> None:
    CORE.data[KEY_CORE] = {
        KEY_TARGET_PLATFORM: PLATFORM_STM32,
        KEY_TARGET_FRAMEWORK: "zephyr",
    }
    with pytest.raises(RuntimeError, match="without Zephyr data"):
        lookup()


@pytest.mark.parametrize("platform", _ZEPHYR_PLATFORMS)
def test_variant_names_are_unique_and_listed_within_a_platform(platform: str) -> None:
    """VARIANT_MODULES is a dict, so a duplicate name would silently replace a variant."""
    package = import_module(f"esphome.components.{platform}.zephyr_variants")
    names = [
        mod.VARIANT.name
        for info in pkgutil.iter_modules(package.__path__)
        if hasattr(mod := import_module(f"{package.__name__}.{info.name}"), "VARIANT")
    ]
    assert len(names) == len(set(names))
    assert set(names) == set(package.VARIANT_MODULES)


@pytest.mark.parametrize(
    ("platform", "framework"),
    [(PLATFORM_ESP32, "esp-idf"), (PLATFORM_RP2, "arduino")],
)
@pytest.mark.parametrize("lookup", _LOOKUPS)
def test_variant_read_without_zephyr_data_is_none(
    lookup: Callable[[], object], platform: str, framework: str
) -> None:
    """esp32 and rp2 also build without Zephyr."""
    CORE.data.pop(KEY_ZEPHYR, None)
    CORE.data[KEY_CORE] = {
        KEY_TARGET_PLATFORM: platform,
        KEY_TARGET_FRAMEWORK: framework,
    }
    assert lookup() is None


@pytest.mark.parametrize("lookup", _LOOKUPS)
def test_variant_read_off_the_zephyr_toolchain_is_none(
    lookup: Callable[[], object],
) -> None:
    """nrf52 keeps Zephyr data on its PlatformIO toolchain, which has no variant."""
    CORE.data[KEY_ZEPHYR] = empty_zephyr_data(variant="ESP32C6")
    CORE.toolchain = Toolchain.PLATFORMIO
    assert lookup() is None


# ---------------------------------------------------------------------------
# ZephyrModuleTemplate.resolve
# ---------------------------------------------------------------------------

_MODULE_TEMPLATE = ZephyrModuleTemplate(
    name="dummy-module",
    manifest_url="http://dummy-module",
    default_versions={(3, 4): "1.4.0"},
)


def test_module_template_resolve_uses_matching_default_version() -> None:
    module = _MODULE_TEMPLATE.resolve(cv.Version(3, 4, 2))

    assert module == ZephyrModule(
        name="dummy-module", manifest_url="http://dummy-module", revision="v1.4.0"
    )


def test_module_template_resolve_ignores_root_patch_version() -> None:
    # (major, minor) match only -- root_version's own patch (here .99) doesn't need a
    # corresponding entry.
    module = _MODULE_TEMPLATE.resolve(cv.Version(3, 4, 99))

    assert module.revision == "v1.4.0"


def test_module_template_resolve_explicit_version_wins_over_table() -> None:
    module = _MODULE_TEMPLATE.resolve(cv.Version(3, 4, 0), explicit_version="9.9.9")

    assert module.revision == "v9.9.9"


def test_module_template_resolve_explicit_version_fills_table_gap() -> None:
    # No (3, 9) entry -- would otherwise raise; an explicit version bypasses the
    # table lookup entirely.
    module = _MODULE_TEMPLATE.resolve(cv.Version(3, 9, 0), explicit_version="1.9.9")

    assert module.revision == "v1.9.9"


def test_module_template_resolve_raises_without_table_match_or_override() -> None:
    with pytest.raises(EsphomeError, match="no known compatible version"):
        _MODULE_TEMPLATE.resolve(cv.Version(3, 9, 0))


_VERSIONED_MODULE_TEMPLATE = ZephyrModuleTemplate(
    name="dummy-module",
    manifest_url="http://dummy-module",
    default_versions={(3, 4): "1.4.0"},
    min_version=cv.Version(1, 4, 0),
)


def test_module_template_resolve_rejects_override_below_min_version() -> None:
    with pytest.raises(EsphomeError, match="requires version >= 1.4.0"):
        _VERSIONED_MODULE_TEMPLATE.resolve(
            cv.Version(3, 4, 0), explicit_version="1.0.0"
        )


def test_module_template_resolve_accepts_override_at_min_version() -> None:
    module = _VERSIONED_MODULE_TEMPLATE.resolve(
        cv.Version(3, 4, 0), explicit_version="1.4.0"
    )
    assert module.revision == "v1.4.0"


def test_module_template_resolve_ignores_min_version_for_non_semver_ref() -> None:
    # A git branch/ref (e.g. "main") isn't a version at all -- nothing to compare.
    module = _VERSIONED_MODULE_TEMPLATE.resolve(
        cv.Version(3, 4, 0), explicit_version="main"
    )
    assert module.revision == "main"


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_validated_config_round_trips_through_config_schema(variant: str) -> None:
    """upload/logs feed the cached validated config back through CONFIG_SCHEMA."""
    from esphome.const import CONF_TYPE, CONF_VARIANT, KEY_CORE  # noqa: PLC0415
    from esphome.core import CORE  # noqa: PLC0415
    from tests.unit_tests.components.zephyr.zephyr_state import (  # noqa: PLC0415
        zephyr_platform_framework,
    )

    platform = zephyr_platform_framework(variant).value[0].value
    CONFIG_SCHEMA = import_module(f"esphome.components.{platform}").CONFIG_SCHEMA
    CORE.data[KEY_CORE] = {}
    validated = CONFIG_SCHEMA(
        {CONF_VARIANT: variant, CONF_FRAMEWORK: {CONF_TYPE: "zephyr"}}
    )
    re_validated = CONFIG_SCHEMA(validated)
    assert re_validated[CONF_VARIANT] == variant
    assert set(re_validated) == set(validated)


# ---------------------------------------------------------------------------
# SIMG301 -- six301 SoC support first shipped in Zephyr 4.5.0
# ---------------------------------------------------------------------------


def _simg301_schema(framework: dict) -> dict:
    from esphome.components.silabs import CONFIG_SCHEMA  # noqa: PLC0415
    from esphome.const import CONF_VARIANT, KEY_CORE  # noqa: PLC0415
    from esphome.core import CORE  # noqa: PLC0415

    CORE.data[KEY_CORE] = {}
    return CONFIG_SCHEMA({CONF_VARIANT: "SIMG301", CONF_FRAMEWORK: framework})


def test_simg301_defaults_to_a_version_with_six301_support() -> None:
    from esphome.const import KEY_CORE, KEY_FRAMEWORK_VERSION  # noqa: PLC0415
    from esphome.core import CORE  # noqa: PLC0415

    _simg301_schema({})
    assert str(CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION]) == "4.5.0"


@pytest.mark.parametrize("version", ["4.5.0", "4.5.1"])
def test_simg301_accepts_4_5(version: str) -> None:
    _simg301_schema({CONF_VERSION: version})


def test_simg301_rejects_4_4_2() -> None:
    with pytest.raises(cv.Invalid, match=r"requires zephyr >= 4\.5\.0"):
        _simg301_schema({CONF_VERSION: "4.4.2"})


def test_simg301_sets_the_runner() -> None:
    from esphome.components.zephyr.const import KEY_ZEPHYR  # noqa: PLC0415
    from esphome.core import CORE  # noqa: PLC0415

    _simg301_schema({CONF_ADVANCED: {"runner": "jlink"}})
    assert CORE.data[KEY_ZEPHYR]["runner"] == "jlink"


def test_simg301_rejects_an_unknown_advanced_key() -> None:
    with pytest.raises(cv.Invalid, match="extra keys not allowed"):
        _simg301_schema({CONF_ADVANCED: {"runer": "jlink"}})


def test_every_family_names_its_vendor_hals() -> None:
    for family in {v.family_module for v in VARIANTS.values() if v.family_module}:
        hals = family.ADDITIONAL_FRAMEWORK_PROJECTS
        assert hals
        assert all(h.startswith("hal_") for h in hals)


def test_stm32c5_default_is_its_minimum() -> None:
    manifest = VARIANTS["STM32C5"].manifest
    assert cv.Version.parse(manifest.default_version) == manifest.min_version
    assert manifest is ZEPHYR_MAINLINE
