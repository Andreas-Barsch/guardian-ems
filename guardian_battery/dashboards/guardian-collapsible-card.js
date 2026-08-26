class GuardianCollapsibleCard extends HTMLElement {
  constructor() {
    super();
    this._open = false;
    this._hass = undefined;
    this._child = undefined;
    this._childPromise = undefined;
    this.attachShadow({ mode: "open" });
  }

  connectedCallback() {
    this._ensureChild();
  }

  setConfig(config) {
    if (!config || typeof config.title !== "string" || typeof config.content !== "string") {
      throw new Error("guardian-collapsible-card requires title and content");
    }
    this._config = config;
    this._renderShell();
    if (this._child) this.shadowRoot.querySelector(".content").replaceChildren(this._child);
    this._ensureChild();
  }

  set hass(value) {
    this._hass = value;
    if (this._child) this._child.hass = value;
  }

  getCardSize() {
    if (!this._open) return 1;
    return 1 + (this._child?.getCardSize?.() || 1);
  }

  _ensureChild() {
    if (!this.isConnected || !this._config || this._child || this._childPromise) return;
    this._childPromise = Promise.resolve()
      .then(() => window.loadCardHelpers())
      .then((helpers) => {
        if (!this.isConnected || !this._config || this._child) return;
        const child = helpers.createCardElement({
          type: "markdown",
          content: this._config.content,
        });
        if (!child) throw new Error("Home Assistant returned no Markdown child card");
        this._child = child;
        if (this._hass) child.hass = this._hass;
        this.shadowRoot.querySelector(".content").replaceChildren(child);
      })
      .catch((error) => {
        this._showChildError(error);
        console.error("guardian-collapsible-card could not create its child", error);
      })
      .finally(() => {
        this._childPromise = undefined;
      });
  }

  _showChildError(error) {
    const message = document.createElement("div");
    message.className = "message error";
    message.setAttribute("role", "alert");
    message.textContent = `Inhalt konnte nicht geladen werden: ${error?.message || String(error)}`;
    this.shadowRoot.querySelector(".content")?.replaceChildren(message);
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
        .message { padding: 16px; }
        .error { color: var(--error-color, #db4437); }
        @media (max-width: 600px) { button { padding: 14px 12px; } }
      </style>
      <ha-card>
        <button type="button" aria-expanded="${String(this._open)}">
          <span class="title"></span><span class="indicator" aria-hidden="true">▼</span>
        </button>
        <div class="content"${this._open ? "" : " hidden"}><div class="message">Inhalt wird geladen…</div></div>
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
