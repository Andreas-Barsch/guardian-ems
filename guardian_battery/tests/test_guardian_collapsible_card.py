from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SOURCES = (
    ROOT / "guardian_battery" / "dashboards" / "guardian-collapsible-card.js",
    ROOT / "homeassistant" / "www" / "guardian-collapsible-card.js",
)


def test_source_and_home_assistant_card_are_identical_and_registered():
    source, deployed = (path.read_text(encoding="utf-8") for path in SOURCES)
    assert source == deployed
    configuration = yaml.safe_load(
        (ROOT / "homeassistant" / "configuration.yaml").read_text(encoding="utf-8")
        .replace("!include_dir_named packages", "packages")
        .replace("!include_dir_merge_named themes", "themes")
        .replace("!include automations.yaml", "automations.yaml")
        .replace("!include scripts.yaml", "scripts.yaml")
        .replace("!include scenes.yaml", "scenes.yaml")
    )
    assert configuration["lovelace"]["resource_mode"] == "yaml"
    assert {"url": "/local/guardian-collapsible-card.js?v=2", "type": "module"} \
        in configuration["lovelace"]["resources"]
    assert len(configuration["lovelace"]["resources"]) == 1


def test_card_has_independent_closed_state_and_preserves_it_during_hass_updates():
    source = SOURCES[0].read_text(encoding="utf-8")
    assert "this._open = false" in source
    assert 'aria-expanded="${String(this._open)}"' in source
    assert 'this._open ? "" : " hidden"' in source
    assert "this._open = !this._open" in source
    assert 'hidden = !this._open' in source
    assert "set hass(value)" in source
    hass_setter = source.split("set hass(value)", 1)[1].split("getCardSize()", 1)[0]
    assert "this._renderShell" not in hass_setter
    assert "this._open" not in hass_setter
    assert "this._child.hass = value" in hass_setter
    assert "ll-rebuild" not in source
    set_config = source.split("setConfig(config) {", 1)[1].split("set hass(value)", 1)[0]
    assert 'if (this._child) this.shadowRoot.querySelector(".content").replaceChildren(this._child)' in set_config


def test_child_creation_is_lifecycle_safe_idempotent_and_reports_errors():
    source = SOURCES[0].read_text(encoding="utf-8")
    assert "connectedCallback()" in source
    assert "this._ensureChild();" in source
    ensure_child = source.split("_ensureChild() {", 1)[1].split("_showChildError(error) {", 1)[0]
    assert "!this.isConnected" in ensure_child
    assert "!this._config" in ensure_child
    assert "this._child" in ensure_child
    assert "this._childPromise" in ensure_child
    assert "window.loadCardHelpers()" in ensure_child
    assert "helpers.createCardElement" in ensure_child
    assert "if (this._hass) child.hass = this._hass" in ensure_child
    assert ".replaceChildren(child)" in ensure_child
    assert ".catch((error)" in ensure_child
    assert "this._showChildError(error)" in ensure_child
    assert "console.error" in ensure_child
    assert "this._childPromise = undefined" in ensure_child
    assert 'role", "alert"' in source
    assert "Inhalt wird geladen" in source
    assert "Inhalt konnte nicht geladen werden" in source
