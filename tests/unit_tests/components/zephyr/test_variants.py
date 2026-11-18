"""Unit tests for esphome.components.zephyr.variants: framework: type: resolution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from importlib import import_module
import sys
from types import ModuleType
from typing import Any

import pytest

from esphome.components.zephyr import (
    variants,
    zephyr_framework_type,
    zephyr_variant,
    zephyr_variant_family,
)
from esphome.components.zephyr.const import KEY_BOOTLOADER, VERSION_RECOMMENDED
from esphome.components.zephyr.framework_helpers import FrameworkSource
from esphome.components.zephyr.variants import (
    VARIANTS,
    ZEPHYR_MAINLINE,
    ZephyrModule,
    ZephyrModuleTemplate,
    ZephyrSDK,
    ZephyrVariant,
    get_family_module,
    resolve_framework_version,
    resolve_sdk,
    set_core_data,
)
import esphome.config_validation as cv
from esphome.const import (
    CONF_ADVANCED,
    CONF_BOARD,
    CONF_FRAMEWORK,
    CONF_REFRESH,
    CONF_SOURCE,
    CONF_TYPE,
    CONF_VERSION,
    KEY_CORE,
    KEY_FRAMEWORK_VERSION,
    KEY_TARGET_FRAMEWORK,
)
from esphome.core import CORE, EsphomeError

_DEFAULT_SDK = ZephyrSDK(
    source=FrameworkSource(
        name="Test", url="http://dummy-default", optional=(), required=frozenset()
    ),
    default_version="1.0.0",
    min_version=cv.Version(1, 0, 0),
)
_ALT_SDK = ZephyrSDK(
    source=FrameworkSource(
        name="Test", url="http://dummy-alt", optional=(), required=frozenset()
    ),
    default_version="2.0.0",
    min_version=cv.Version(2, 0, 0),
)


def _fake_variant() -> ZephyrVariant:
    return ZephyrVariant(
        sdk=_DEFAULT_SDK,
        sdk_name="default",
        alt_sdks={"alt": _ALT_SDK},
    )


def _framework_config(**overrides: Any) -> dict:
    # Mirrors _FRAMEWORK_SCHEMA's post-validation shape: refresh always defaults to
    # "1d", every other key is only present when explicitly set.
    return {CONF_REFRESH: "1d", **overrides}


# ---------------------------------------------------------------------------
# resolve_sdk
# ---------------------------------------------------------------------------


def test_resolve_sdk_none_type_returns_variant_default() -> None:
    variant = _fake_variant()
    assert resolve_sdk(variant, None) == ("default", _DEFAULT_SDK)


def test_resolve_sdk_explicit_default_type_returns_variant_default() -> None:
    variant = _fake_variant()
    assert resolve_sdk(variant, "default") == ("default", _DEFAULT_SDK)


def test_resolve_sdk_alt_type_returns_alt_sdk() -> None:
    variant = _fake_variant()
    assert resolve_sdk(variant, "alt") == ("alt", _ALT_SDK)


def test_resolve_sdk_unknown_type_raises_keyerror() -> None:
    variant = _fake_variant()
    with pytest.raises(KeyError):
        resolve_sdk(variant, "bogus")


# ---------------------------------------------------------------------------
# resolve_framework_version
# ---------------------------------------------------------------------------


def test_resolve_framework_version_defaults_to_variant_default_sdk() -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config()}

    version_str, parsed, sdk_name, sdk = resolve_framework_version(
        variant, "fake", config, "test"
    )

    assert version_str == "1.0.0"
    assert parsed == cv.Version(1, 0, 0)
    assert sdk_name == "default"
    assert sdk is _DEFAULT_SDK


def test_resolve_framework_version_rejects_unknown_type() -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_TYPE: "bogus"})}

    with pytest.raises(cv.Invalid, match="not a valid framework type"):
        resolve_framework_version(variant, "fake", config, "test")


def test_resolve_framework_version_selects_alt_sdk_type() -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_TYPE: "alt"})}

    version_str, parsed, sdk_name, sdk = resolve_framework_version(
        variant, "fake", config, "test"
    )

    # The alt sdk's own default_version, not the variant's default sdk's.
    assert version_str == "2.0.0"
    assert parsed == cv.Version(2, 0, 0)
    assert sdk_name == "alt"
    assert sdk is _ALT_SDK


def test_resolve_framework_version_rejects_version_and_source_together() -> None:
    variant = _fake_variant()
    config = {
        CONF_FRAMEWORK: _framework_config(
            **{
                CONF_VERSION: "1.0.0",
                CONF_SOURCE: {CONF_TYPE: "local", "path": "/dummy"},
            }
        )
    }

    with pytest.raises(cv.Invalid, match="mutually exclusive"):
        resolve_framework_version(variant, "fake", config, "test")


def test_resolve_framework_version_rejects_version_below_min() -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_VERSION: "0.5.0"})}

    with pytest.raises(cv.Invalid, match=">= 1.0.0"):
        resolve_framework_version(variant, "fake", config, "test")


def test_resolve_framework_version_rejects_prerelease_of_min() -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_VERSION: "1.0.0-rc1"})}

    with pytest.raises(cv.Invalid, match=">= 1.0.0"):
        resolve_framework_version(variant, "fake", config, "test")


def test_resolve_framework_version_release_meets_prerelease_min() -> None:
    sdk = ZephyrSDK(
        source=_DEFAULT_SDK.source,
        default_version="1.0.0",
        min_version=cv.Version(1, 0, 0, "rc2"),
    )
    variant = ZephyrVariant(sdk=sdk, sdk_name="default")
    for version in ("1.0.0", "1.0.0-rc10"):
        config = {CONF_FRAMEWORK: _framework_config(**{CONF_VERSION: version})}
        resolve_framework_version(variant, "fake", config, "test")


def test_resolve_framework_version_accepts_recommended_literal() -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_VERSION: VERSION_RECOMMENDED})}

    version_str, parsed, sdk_name, sdk = resolve_framework_version(
        variant, "fake", config, "test"
    )

    assert version_str == "1.0.0"
    assert sdk_name == "default"


def test_resolve_framework_version_accepts_explicit_non_default_version(
    caplog: pytest.LogCaptureFixture,
) -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_VERSION: "1.2.0"})}

    version_str, parsed, sdk_name, sdk = resolve_framework_version(
        variant, "fake", config, "test"
    )

    assert version_str == "1.2.0"
    assert parsed == cv.Version(1, 2, 0)
    assert "not the recommended one" in caplog.text


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
    for family in {v.family for v in VARIANTS.values() if v.family}:
        hals = get_family_module(family).ADDITIONAL_FRAMEWORK_PROJECTS
        assert hals
        assert all(h.startswith("hal_") for h in hals)


def test_registry_read_while_loading_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = "esphome.components.fakeplatform.zephyr_variants"
    registry = variants._LazyVariants()
    # VARIANT_NAME is missing, so reading it calls __getattr__, which reads the registry
    chip = ModuleType(f"{package}.chip1")
    chip.__getattr__ = lambda name: registry.get("chip1")
    platform = ModuleType(package)
    platform.VARIANT_MODULES = ["chip1"]
    monkeypatch.setitem(sys.modules, package, platform)
    monkeypatch.setitem(sys.modules, chip.__name__, chip)
    monkeypatch.setattr(variants, "_ZEPHYR_PLATFORMS", ("fakeplatform",))

    for _ in range(2):
        with pytest.raises(RuntimeError, match="read VARIANTS while it loaded"):
            registry.get("chip1")


_PACKAGE: str = "esphome.components.fakeplatform.zephyr_variants"


def _module(name: str, **attrs: object) -> ModuleType:
    module = ModuleType(name)
    module.__dict__.update(attrs)
    return module


@pytest.fixture
def fake_platform(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """A platform whose zephyr_variants package lists one chip of the chipfam family."""
    variant = ZephyrVariant(sdk=_DEFAULT_SDK, sdk_name="default", family="chipfam")
    chip = _module(f"{_PACKAGE}.chip1", VARIANT_NAME="chip1", VARIANT=variant)
    family = _module(f"{_PACKAGE}.chipfam_family")
    monkeypatch.setitem(
        sys.modules, _PACKAGE, _module(_PACKAGE, VARIANT_MODULES=["chip1"])
    )
    monkeypatch.setitem(sys.modules, chip.__name__, chip)
    monkeypatch.setitem(sys.modules, family.__name__, family)
    monkeypatch.setattr(variants, "_ZEPHYR_PLATFORMS", ("fakeplatform",))
    return chip


def test_registry_loads_variants_and_families(fake_platform: ModuleType) -> None:
    registry = variants._LazyVariants()

    assert dict(registry.items()) == {"chip1": fake_platform.VARIANT}
    assert registry.module("chip1") is fake_platform
    assert (
        registry.family_module("chipfam") is sys.modules[f"{_PACKAGE}.chipfam_family"]
    )


@pytest.mark.parametrize(
    "access",
    [
        lambda r, v: r["chip1"],
        lambda r, v: r.get("chip1"),
        lambda r, v: next(iter(r.values())),
        lambda r, v: v if "chip1" in r else None,
        lambda r, v: v if list(r) == ["chip1"] else None,
        lambda r, v: v if list(r.keys()) == ["chip1"] else None,
        lambda r, v: v if r == {"chip1": v} else None,
        lambda r, v: dict(r)["chip1"],
    ],
)
def test_each_lookup_loads_the_registry(
    fake_platform: ModuleType,
    access: Callable[[Mapping[str, ZephyrVariant], ZephyrVariant], object],
) -> None:
    variant = fake_platform.VARIANT
    assert access(variants._LazyVariants(), variant) is variant


def test_module_lookups(
    fake_platform: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(variants, "_variants_instance", variants._LazyVariants())

    assert variants.get_variant_module("chip1") is fake_platform
    assert variants.get_family_module("chipfam").__name__.endswith("chipfam_family")


def test_stm32_variants_load() -> None:
    registry = variants._LazyVariants()

    assert "STM32C5" in registry
    assert registry.family_module("stm32").__name__.endswith(".stm32_family")


def test_stm32c5_default_is_its_minimum() -> None:
    assert cv.Version.parse(
        ZEPHYR_MAINLINE.default_version
    ) == variants._sdk_min_version(VARIANTS["STM32C5"], "zephyr", ZEPHYR_MAINLINE)


def test_set_core_data(
    fake_platform: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(variants, "VARIANTS", {"chip1": fake_platform.VARIANT})
    CORE.data[KEY_CORE] = {}

    config = {CONF_BOARD: "chip1_dk", CONF_FRAMEWORK: {}}
    set_core_data(
        "chip1",
        config,
        cv.Version(4, 5, 0),
        framework_type="vendor",
        bootloader="mcuboot",
    )

    assert zephyr_variant() == "chip1"
    assert zephyr_variant_family() == "chipfam"
    assert zephyr_framework_type() == "vendor"
    assert CORE.data["zephyr"][CONF_BOARD] == "chip1_dk"
    assert CORE.data["zephyr"][KEY_BOOTLOADER] == "mcuboot"
    assert CORE.data[KEY_CORE][KEY_TARGET_FRAMEWORK] == "zephyr"
    assert CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION] == cv.Version(4, 5, 0)


def test_failed_variant_import_is_raised_on_every_lookup(
    fake_platform: ModuleType,
) -> None:
    sys.modules[_PACKAGE].VARIANT_MODULES = ["chip1", "missing"]
    registry = variants._LazyVariants()

    for _ in range(2):
        with pytest.raises(ModuleNotFoundError):
            registry.get("chip1")
    assert registry._variants == {}


def test_duplicate_variant_name_is_rejected(fake_platform: ModuleType) -> None:
    sys.modules[_PACKAGE].VARIANT_MODULES = ["chip1", "chip1"]
    registry = variants._LazyVariants()

    for _ in range(2):
        with pytest.raises(ValueError, match="are both 'chip1'"):
            registry.get("chip1")


def test_registry_rejects_an_unknown_family(fake_platform: ModuleType) -> None:
    with pytest.raises(ValueError, match="Unknown Zephyr family"):
        variants._LazyVariants().family_module("other")


@pytest.mark.parametrize("version", ["v1.0.0", "1.0.0-rc1a"])
def test_resolve_framework_version_rejects_a_version_that_is_not_a_number(
    version: str,
) -> None:
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_VERSION: version})}
    with pytest.raises(cv.Invalid, match=f"'{version}' is not a version number") as err:
        resolve_framework_version(_fake_variant(), "fake", config, "test")
    assert err.value.path == [CONF_FRAMEWORK, CONF_VERSION]
