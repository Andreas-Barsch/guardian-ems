class GuardianCollapsibleCard extends HTMLElement {
  constructor() {
    super();
    this._open = false;
    this._hass = undefined;
    this.attachShadow({ mode: "open" });
  }

  setConfig(config) {
    if (!config || typeof config.title !== "string" || typeof config.content !== "string") {
      throw new Error("guardian-collapsible-card requires title and content");
    }
    this._config = config;
    this._renderShell();
    window.loadCardHelpers().then((helpers) => {
      if (!this.isConnected || !this._config) return;
      this._child = helpers.createCardElement({ type: "markdown", content: this._config.content });
      if (this._hass) this._child.hass = this._hass;
      this.shadowRoot.querySelector(".content").replaceChildren(this._child);
    });
  }

  set hass(value) {
    this._hass = value;
    if (this._child) this._child.hass = value;
  }

  getCardSize() {
    if (!this._open) return 1;
    return 1 + (this._child?.getCardSize?.() || 1);
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { overflow: hidden; }
        button {
          align-items: center; background: none; border: 0; color: inherit;
          cursor: pointer; display: flex; font: inherit; font-weight: 600;
          gap: 12px; justify-content: space-between; padding: 16px;
          text-align: left; width: 100%;
        }
        button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -3px; }
        .indicator { flex: 0 0 auto; transition: transform .16s ease; }
        button[aria-expanded="true"] .indicator { transform: rotate(180deg); }
        .content[hidden] { display: none; }
        .content { border-top: 1px solid var(--divider-color); }
        .content > * { display: block; }
        @media (max-width: 600px) { button { padding: 14px 12px; } }
      </style>
      <ha-card>
        <button type="button" aria-expanded="${String(this._open)}">
          <span class="title"></span><span class="indicator" aria-hidden="true">▼</span>
        </button>
        <div class="content"${this._open ? "" : " hidden"}></div>
      </ha-card>`;
    this.shadowRoot.querySelector(".title").textContent = this._config.title;
    this.shadowRoot.querySelector("button").addEventListener("click", () => {
      this._open = !this._open;
      this.shadowRoot.querySelector("button").setAttribute("aria-expanded", String(this._open));
      this.shadowRoot.querySelector(".content").hidden = !this._open;
    });
  }
}

if (!customElements.get("guardian-collapsible-card")) {
  customElements.define("guardian-collapsible-card", GuardianCollapsibleCard);
}
