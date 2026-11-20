"""Unit tests for esphome.components.zephyr.variants: framework: type: resolution."""

from __future__ import annotations

from importlib import import_module
import pkgutil
from types import ModuleType
from typing import Any

import pytest

from esphome.components.zephyr import (
    zephyr_framework_type,
    zephyr_variant,
    zephyr_variant_family,
)
from esphome.components.zephyr.const import KEY_BOOTLOADER, VERSION_RECOMMENDED
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
    CONF_REFRESH,
    CONF_SOURCE,
    CONF_TYPE,
    CONF_VERSION,
    KEY_CORE,
    KEY_FRAMEWORK_VERSION,
    KEY_TARGET_FRAMEWORK,
)
from esphome.core import CORE, EsphomeError
from tests.unit_tests.components.zephyr.zephyr_state import VARIANTS

_ZEPHYR_PLATFORMS: tuple[str, ...] = (
    "esp32",
    "nrf",
    "nxp",
    "renesas",
    "rp2",
    "silabs",
    "stm32",
)

_DEFAULT_MANIFEST: ZephyrManifest = ZephyrManifest(
    framework_type="default",
    source=FrameworkSource(
        name="Test", url="http://dummy-default", optional=(), required=frozenset()
    ),
    default_version="1.0.0",
    min_version=cv.Version(1, 0, 0),
)
_ALT_MANIFEST: ZephyrManifest = ZephyrManifest(
    framework_type="alt",
    source=FrameworkSource(
        name="Test", url="http://dummy-alt", optional=(), required=frozenset()
    ),
    default_version="2.0.0",
    min_version=cv.Version(2, 0, 0),
)


def _fake_variant() -> ZephyrVariant:
    return ZephyrVariant(
        name="fake", manifest=_DEFAULT_MANIFEST, alt_manifests=(_ALT_MANIFEST,)
    )


def _framework_config(**overrides: Any) -> dict:
    # Mirrors _FRAMEWORK_SCHEMA's post-validation shape: refresh always defaults to
    # "1d", every other key is only present when explicitly set.
    return {CONF_REFRESH: "1d", **overrides}


# ---------------------------------------------------------------------------
# ZephyrVariant.manifest_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("framework_type", "manifest"),
    [(None, _DEFAULT_MANIFEST), ("default", _DEFAULT_MANIFEST), ("alt", _ALT_MANIFEST)],
)
def test_manifest_for(framework_type: str | None, manifest: ZephyrManifest) -> None:
    assert _fake_variant().manifest_for(framework_type) is manifest


def test_manifest_for_unknown_type_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        _fake_variant().manifest_for("bogus")


# ---------------------------------------------------------------------------
# resolve_variant
# ---------------------------------------------------------------------------


def test_resolve_variant_defaults_to_the_variant_manifest() -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config()}

    assert resolve_variant(variant, config, "test") == ZephyrVariantResolved(
        variant, _DEFAULT_MANIFEST, cv.Version(1, 0, 0)
    )


def test_resolve_variant_rejects_unknown_type() -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_TYPE: "bogus"})}

    with pytest.raises(cv.Invalid, match="not a valid framework type"):
        resolve_variant(variant, config, "test")


def test_resolve_variant_selects_alt_manifest_type() -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_TYPE: "alt"})}

    # The alt manifest's own default_version, not the variant's own manifest's.
    assert resolve_variant(variant, config, "test") == ZephyrVariantResolved(
        variant, _ALT_MANIFEST, cv.Version(2, 0, 0)
    )


def test_resolve_variant_rejects_version_and_source_together() -> None:
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
        resolve_variant(variant, config, "test")


def test_resolve_variant_rejects_version_below_min() -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_VERSION: "0.5.0"})}

    with pytest.raises(cv.Invalid, match=">= 1.0.0"):
        resolve_variant(variant, config, "test")


def test_resolve_variant_rejects_prerelease_of_min() -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_VERSION: "1.0.0-rc1"})}

    with pytest.raises(cv.Invalid, match=">= 1.0.0"):
        resolve_variant(variant, config, "test")


def test_resolve_variant_release_meets_prerelease_min() -> None:
    manifest = ZephyrManifest(
        framework_type="default",
        source=_DEFAULT_MANIFEST.source,
        default_version="1.0.0",
        min_version=cv.Version(1, 0, 0, "rc2"),
    )
    variant = ZephyrVariant(name="fake", manifest=manifest)
    for version in ("1.0.0", "1.0.0-rc10"):
        config = {CONF_FRAMEWORK: _framework_config(**{CONF_VERSION: version})}
        resolve_variant(variant, config, "test")


def test_resolve_variant_accepts_recommended_literal() -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_VERSION: VERSION_RECOMMENDED})}

    resolved = resolve_variant(variant, config, "test")

    assert resolved.version == cv.Version(1, 0, 0)
    assert resolved.manifest is _DEFAULT_MANIFEST


def test_resolve_variant_accepts_explicit_non_default_version(
    caplog: pytest.LogCaptureFixture,
) -> None:
    variant = _fake_variant()
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_VERSION: "1.2.0"})}

    resolved = resolve_variant(variant, config, "test")

    assert resolved.version == cv.Version(1, 2, 0)
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
    for family in {v.family_module for v in VARIANTS.values() if v.family_module}:
        hals = family.ADDITIONAL_FRAMEWORK_PROJECTS
        assert hals
        assert all(h.startswith("hal_") for h in hals)


def test_stm32c5_default_is_its_minimum() -> None:
    manifest = VARIANTS["STM32C5"].manifest
    assert cv.Version.parse(manifest.default_version) == manifest.min_version
    assert manifest is ZEPHYR_MAINLINE


@pytest.mark.parametrize("family", ["chipfam", None])
def test_set_variant_core_data(family: str | None) -> None:
    family_module = None
    if family is not None:
        family_module = ModuleType(f"{family}_family")
        family_module.FAMILY = family
    variant = ZephyrVariant(
        name="chip1", family_module=family_module, manifest=_ALT_MANIFEST
    )
    CORE.data[KEY_CORE] = {}

    config = {CONF_BOARD: "chip1_dk", CONF_FRAMEWORK: {}}
    set_variant_core_data(
        ZephyrVariantResolved(variant, _ALT_MANIFEST, cv.Version(4, 5, 0)),
        config,
        bootloader="mcuboot",
    )

    assert zephyr_variant() == "chip1"
    assert zephyr_variant_family() == family
    assert zephyr_framework_type() == "alt"
    assert CORE.data["zephyr"][CONF_BOARD] == "chip1_dk"
    assert CORE.data["zephyr"][KEY_BOOTLOADER] == "mcuboot"
    assert CORE.data[KEY_CORE][KEY_TARGET_FRAMEWORK] == "zephyr"
    assert CORE.data[KEY_CORE][KEY_FRAMEWORK_VERSION] == cv.Version(4, 5, 0)


@pytest.mark.parametrize("version", ["v1.0.0", "1.0.0-rc1a"])
def test_resolve_variant_rejects_a_version_that_is_not_a_number(
    version: str,
) -> None:
    config = {CONF_FRAMEWORK: _framework_config(**{CONF_VERSION: version})}
    with pytest.raises(cv.Invalid, match=f"'{version}' is not a version number") as err:
        resolve_variant(_fake_variant(), config, "test")
    assert err.value.path == [CONF_FRAMEWORK, CONF_VERSION]


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
