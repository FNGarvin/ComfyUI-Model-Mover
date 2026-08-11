/**
 * ComfyUI-Model-Mover — frontend
 *
 * Toolbar button -> modal dialog with a Steam-Mover-style grid: pick any two
 * registered directories as columns A/B, see which files live where,
 * multi-select rows, move or copy between the two. Directory management
 * (add/remove/reorder/default) lives in its own tab of the same dialog.
 *
 * API surface: see core/routes.py (/model_mover/*).
 */

import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { $el, ComfyDialog } from "../../../scripts/ui.js";

const API = "/model_mover";

function fmtBytes(n) {
    if (n === null || n === undefined) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = n;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i++;
    }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
}

// A row is a "dotfile" if its own filename (the last path segment, not any
// parent directory) starts with "." — e.g. a stray .validation_cache.json
// or .DS_Store that would otherwise sort to the top of every listing.
function isDotfile(relpath) {
    const base = relpath.slice(relpath.lastIndexOf("/") + 1);
    return base.startsWith(".");
}

// Row identity as a single string, safe to round-trip through an HTML
// attribute (data-key) and back: each part is percent-encoded first, since
// encodeURIComponent's output never contains "|", then joined on it.
function rowKey(category, relpath) {
    return `${encodeURIComponent(category)}|${encodeURIComponent(relpath)}`;
}
function parseRowKey(key) {
    const i = key.indexOf("|");
    return { category: decodeURIComponent(key.slice(0, i)), relpath: decodeURIComponent(key.slice(i + 1)) };
}

class ModelMoverDialog extends ComfyDialog {
    constructor(ext) {
        super();
        this.ext = ext; // ModelMoverExtension — owns the persistent title-bar progress widget
        this.directories = [];
        this.dirAId = null;
        this.dirBId = null;
        this.categories = [];
        this.selectedCategories = new Set(); // empty == "all"
        this.rows = [];
        this.selected = new Set(); // rowKey(category, relpath) strings
        this.sortKey = "relpath"; // "relpath" | "category" | "a" | "b"
        this.sortDir = 1; // 1 = ascending, -1 = descending
        this.verifyChecksums = true;
        this.hideDotfiles = true;
        this.jobId = null;
        this.jobTimer = null;
        this.lastJob = null; // last-known job snapshot, so re-opening mid-transfer doesn't show a blank bar
        this.staleTempCount = 0;
        this.activeTab = "grid"; // "grid" | "directories"
        this._tabAutoSwitched = false; // only force onto the directories tab once
        this.dirStatusMsg = null;
        this._dirStatusTimer = null;

        this.injectStyles();

        this.backdrop = $el("div.mm-backdrop", {
            parent: document.body,
            style: {
                position: "fixed", top: "0", left: "0", width: "100vw", height: "100vh",
                backgroundColor: "rgba(0,0,0,0.5)", zIndex: "99998", display: "none",
            },
        });
        this.backdrop.addEventListener("click", () => this.close());

        this.element = $el("div.comfy-modal.mm-modal", {
            parent: document.body,
            style: {
                position: "fixed", top: "50%", left: "50%", transform: "translate(-50%, -50%)",
                width: "1300px", height: "800px", maxWidth: "96vw", maxHeight: "94vh",
                backgroundColor: "var(--comfy-menu-bg, #202020)", color: "var(--input-text, #fff)",
                border: "2px solid var(--border-color, #555)", borderRadius: "8px", padding: "0",
                zIndex: "99999", boxShadow: "0 4px 20px rgba(0,0,0,0.8)", display: "none",
                flexDirection: "column",
            },
        }, [this.createHeader(), this.createBody()]);
    }

    injectStyles() {
        if (document.getElementById("model-mover-styles")) return;
        const style = document.createElement("style");
        style.id = "model-mover-styles";
        style.textContent = `
            .mm-section { padding: 10px 20px; border-bottom: 1px solid var(--border-color, #444); }
            .mm-section:last-child { border-bottom: none; }
            .mm-row-flex { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
            .mm-label { font-size: 11px; text-transform: uppercase; letter-spacing: .5px; color: #999; }
            .mm-input, .mm-select { background: #2a2a2a; border: 1px solid #444; border-radius: 4px;
                color: #ddd; padding: 5px 8px; font-size: 12px; }
            .mm-btn { background: #3a3a3a; border: 1px solid #555; border-radius: 4px; color: #ddd;
                padding: 5px 10px; font-size: 12px; cursor: pointer; }
            .mm-btn:hover { background: #484848; }
            .mm-btn:disabled { opacity: .45; cursor: not-allowed; }
            .mm-btn-primary { background: #2e7d32; border-color: #2e7d32; color: #fff; }
            .mm-btn-primary:hover { background: #388e3c; }
            .mm-btn-danger { background: #b23c3c; border-color: #b23c3c; color: #fff; }
            .mm-btn-danger:hover { background: #c94b4b; }
            .mm-btn-copy { border-color: #9575cd; color: #cbb8f0; }
            .mm-btn-copy:hover { background: rgba(149,117,205,.18); }
            .mm-icon-btn { background: none; border: none; font-size: 18px; cursor: pointer; color: #ddd;
                padding: 2px 6px; border-radius: 4px; }
            .mm-icon-btn:hover { background: rgba(255,255,255,.1); }
            .mm-dir-chip { display: flex; align-items: center; gap: 6px; padding: 4px 8px;
                background: #2a2a2a; border-radius: 4px; font-size: 12px; }
            .mm-dir-chip.mm-default { border: 1px solid #4CAF50; }
            .mm-status-ok { color: #81c784; }
            .mm-chiclet { background: #3a3a3a; border: 1px solid #555; border-radius: 14px; color: #ddd;
                padding: 4px 12px; font-size: 12px; cursor: pointer; }
            .mm-chiclet:hover { background: #484848; }
            .mm-table-wrap { flex: 1; overflow: auto; padding: 0 20px; }
            table.mm-grid { width: 100%; border-collapse: collapse; font-size: 12px; }
            table.mm-grid th { position: sticky; top: 0; background: var(--comfy-menu-bg, #202020);
                text-align: left; padding: 6px 8px; border-bottom: 1px solid #444; z-index: 1; }
            table.mm-grid th.mm-sortable { cursor: pointer; user-select: none; white-space: nowrap; }
            table.mm-grid th.mm-sortable:hover { color: #fff; }
            table.mm-grid td { padding: 5px 8px; border-bottom: 1px solid #2c2c2c; vertical-align: middle; }
            table.mm-grid tr:hover td { background: #262626; }
            .mm-mono { font-family: 'SF Mono', Consolas, Monaco, monospace; }
            .mm-truncate { display: inline-block; max-width: 260px; overflow: hidden;
                text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }
            .mm-badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; }
            .mm-badge-both { background: rgba(230,160,30,.2); color: #e6a01e; border: 1px solid rgba(230,160,30,.5); }
            .mm-badge-linked { background: rgba(100,150,220,.2); color: #8ab4f8; border: 1px solid rgba(100,150,220,.5); }
            .mm-cell-btn { background: none; border: none; color: #8ab4f8; cursor: pointer; font-size: 13px; padding: 1px 4px; }
            .mm-cell-btn:hover { text-decoration: underline; }
            .mm-cell-btn:disabled { color: #666; cursor: not-allowed; text-decoration: none; }
            .mm-cell-btn-copy { color: #b39ddb; }
            .mm-cell-btn-danger { color: #e57373; }
            .mm-actions-cell { white-space: nowrap; }
            .mm-footer { padding: 10px 20px; border-top: 1px solid #444; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
            .mm-progress { flex: 1; min-width: 80px; height: 8px; background: #333; border-radius: 4px; overflow: hidden; }
            .mm-progress-fill { height: 100%; background: #4CAF50; width: 0%; transition: width .2s; }
            .mm-status { font-size: 12px; color: #999; }
            .mm-status-err { color: #e57373; }
        `;
        document.head.appendChild(style);
    }

    createHeader() {
        this.headerToggleBtn = $el("button.mm-icon-btn", {
            textContent: "\u2699\ufe0f", // gear
            title: "Directory settings",
            onclick: () => this.setActiveTab(this.activeTab === "grid" ? "directories" : "grid"),
        });
        return $el("div", {
            style: {
                display: "flex", justifyContent: "space-between", alignItems: "center",
                padding: "16px 20px", borderBottom: "1px solid var(--border-color)",
            },
        }, [
            $el("h2", { textContent: "\u{1F4E6} Model Mover", style: { margin: "0", fontSize: "18px" } }),
            $el("div", { style: { display: "flex", alignItems: "center", gap: "4px" } }, [
                this.headerToggleBtn,
                $el("button", {
                    textContent: "\u00d7", onclick: () => this.close(),
                    style: { background: "none", border: "none", fontSize: "24px", cursor: "pointer", color: "#ddd" },
                }),
            ]),
        ]);
    }

    createBody() {
        this.pickerSection = $el("div.mm-section");
        this.tableWrap = $el("div.mm-table-wrap");
        this.footer = $el("div.mm-footer");
        this.gridTab = $el("div", {
            style: { flex: "1", display: "flex", flexDirection: "column", minHeight: "0", overflow: "hidden" },
        }, [this.pickerSection, this.tableWrap, this.footer]);

        this.directoriesSection = $el("div.mm-section");
        this.directoriesTab = $el("div", {
            style: { flex: "1", overflow: "auto" },
        }, [this.directoriesSection]);

        return $el("div", {
            style: { flex: "1", display: "flex", flexDirection: "column", minHeight: "0", overflow: "hidden" },
        }, [this.gridTab, this.directoriesTab]);
    }

    setActiveTab(tab) {
        this.activeTab = tab;
        this.gridTab.style.display = tab === "grid" ? "flex" : "none";
        this.directoriesTab.style.display = tab === "directories" ? "block" : "none";
        this.headerToggleBtn.textContent = tab === "grid" ? "\u2699\ufe0f" : "\u25c0";
        this.headerToggleBtn.title = tab === "grid" ? "Directory settings" : "Back to grid";
    }

    async show() {
        this.backdrop.style.display = "block";
        this.element.style.display = "flex";
        this.setActiveTab(this.activeTab);
        // If a job is already running (dialog re-opened mid-transfer), hydrate
        // the footer immediately instead of waiting for the next poll tick.
        if (this.jobId && this.lastJob) this.applyJobToFooter(this.lastJob);
        await this.refreshStores();
    }

    close() {
        this.backdrop.style.display = "none";
        this.element.style.display = "none";
        // Deliberately NOT calling stopPolling() here — an in-flight transfer
        // (and its progress/cancel affordance, mirrored in the persistent
        // title-bar widget via this.ext) keeps running/visible after the
        // dialog closes, so a slow transfer never disappears just because
        // you clicked elsewhere in ComfyUI.
    }

    // ---------------------------------------------------------------- data

    async refreshStores() {
        try {
            const res = await api.fetchApi(`${API}/directories`);
            const data = await res.json();
            this.directories = data.directories || [];
            this.configWritable = data.config_writable;
            this.staleTempCount = data.stale_temp_count || 0;
            if (!this.dirAId && this.directories[0]) this.dirAId = this.directories[0].id;
            if (!this.dirBId) {
                const other = this.directories.find((d) => d.id !== this.dirAId);
                if (other) this.dirBId = other.id;
            }
            // Land new/lightly-configured installs on the Directories tab
            // instead of an almost-empty grid — there's nothing useful to
            // compare with fewer than two directories registered. Only ever
            // forced once per dialog lifetime so it doesn't fight a user who
            // deliberately switches back to the grid mid-setup.
            if (!this._tabAutoSwitched) {
                this._tabAutoSwitched = true;
                if (this.directories.length < 2) this.setActiveTab("directories");
            }
            this.renderDirectoriesPanel();
            this.renderPickers();
            if (this.dirAId && this.dirBId) await this.refreshInventory();
        } catch (err) {
            this.directoriesSection.innerHTML = `<div class="mm-status mm-status-err">Failed to load directories: ${escapeHtml(err.message)}</div>`;
        }
    }

    async refreshInventory() {
        if (!this.dirAId || !this.dirBId) return;
        const params = new URLSearchParams({ dir_a: this.dirAId, dir_b: this.dirBId });
        if (this.selectedCategories.size) params.set("categories", [...this.selectedCategories].join(","));
        try {
            const res = await api.fetchApi(`${API}/inventory?${params}`);
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
            this.categories = data.categories || [];
            this.rows = data.rows || [];
            this.categoryLinks = data.category_links || {};
            this.renderCategoryFilter();
            this.renderGrid();
        } catch (err) {
            this.tableWrap.innerHTML = `<div class="mm-status mm-status-err">Failed to load inventory: ${escapeHtml(err.message)}</div>`;
        }
    }

    directory(id) {
        return this.directories.find((d) => d.id === id);
    }

    // ------------------------------------------------------------- render

    flashDirStatus(msg) {
        this.dirStatusMsg = msg;
        this.renderDirectoriesPanel();
        if (this._dirStatusTimer) clearTimeout(this._dirStatusTimer);
        this._dirStatusTimer = setTimeout(() => {
            this.dirStatusMsg = null;
            this.renderDirectoriesPanel();
        }, 1600);
    }

    renderDirectoriesPanel() {
        const last = this.directories.length - 1;
        const rows = this.directories.map((d, idx) => {
            const usage = d.free_bytes != null && d.total_bytes != null
                ? `${fmtBytes(d.free_bytes)} free / ${fmtBytes(d.total_bytes)}`
                : (d.error ? `unavailable (${escapeHtml(d.error)})` : "\u2014");
            const removeBtn = d.is_managed
                ? `<button class="mm-btn mm-btn-danger" data-remove="${d.id}" title="Un-register (files are not deleted)">Remove</button>`
                : "";
            return `
                <div class="mm-dir-chip ${idx === 0 ? "mm-default" : ""}">
                    <b>${escapeHtml(d.label)}</b>
                    <span class="mm-mono" style="color:#999;">${escapeHtml(d.base_path)}</span>
                    <span style="color:#777;">${usage}</span>
                    <button class="mm-btn" data-up="${d.id}" ${idx === 0 ? "disabled" : ""} title="Higher priority">\u2191</button>
                    <button class="mm-btn" data-down="${d.id}" ${idx === last ? "disabled" : ""} title="Lower priority">\u2193</button>
                    ${removeBtn}
                </div>`;
        }).join("");

        this.directoriesSection.innerHTML = `
            <div class="mm-row-flex" style="justify-content:space-between;margin-bottom:6px;">
                <div class="mm-label">Directories (top = highest priority \u2014 its copy of a file wins; use \u2191/\u2193 to reorder)</div>
                <button class="mm-btn" id="mm-back-to-grid">Grid \u25b6</button>
            </div>
            <div style="display:flex;flex-direction:column;gap:4px;">${rows}</div>
            <div class="mm-row-flex" style="margin-top:8px;">
                <input class="mm-input" id="mm-new-label" placeholder="Label (e.g. External)" style="width:160px;">
                <input class="mm-input" id="mm-new-path" placeholder="Absolute path (e.g. E:\\models)" style="width:280px;">
                <button class="mm-btn mm-btn-primary" id="mm-add-directory">+ Add Directory</button>
                ${this.configWritable === false ? '<span class="mm-status mm-status-err">ruamel.yaml not installed \u2014 directory changes will fail until it is (see README)</span>' : ""}
                ${this.dirStatusMsg ? `<span class="mm-status mm-status-ok">${escapeHtml(this.dirStatusMsg)}</span>` : ""}
            </div>
        `;

        this.directoriesSection.querySelector("#mm-back-to-grid").addEventListener("click", () => this.setActiveTab("grid"));
        this.directoriesSection.querySelectorAll("[data-up]").forEach((btn) =>
            btn.addEventListener("click", () => this.moveDirectoryPriority(btn.dataset.up, -1)));
        this.directoriesSection.querySelectorAll("[data-down]").forEach((btn) =>
            btn.addEventListener("click", () => this.moveDirectoryPriority(btn.dataset.down, 1)));
        this.directoriesSection.querySelectorAll("[data-remove]").forEach((btn) =>
            btn.addEventListener("click", () => this.removeDirectory(btn.dataset.remove)));
        this.directoriesSection.querySelector("#mm-add-directory").addEventListener("click", () => this.addDirectory());
    }

    renderPickers() {
        const opts = (selectedId) => this.directories.map((d) =>
            `<option value="${d.id}" ${d.id === selectedId ? "selected" : ""}>${escapeHtml(d.label)}</option>`).join("");

        this.pickerSection.innerHTML = `
            <div class="mm-row-flex">
                <span class="mm-label">Compare</span>
                <select class="mm-select" id="mm-dir-a">${opts(this.dirAId)}</select>
                <button class="mm-btn" id="mm-swap" title="Swap columns">\u21c4</button>
                <select class="mm-select" id="mm-dir-b">${opts(this.dirBId)}</select>
                <span style="flex:1"></span>
                <label style="font-size:12px;"><input type="checkbox" id="mm-hide-dotfiles" ${this.hideDotfiles ? "checked" : ""}> Hide dotfiles</label>
                <label style="font-size:12px;"><input type="checkbox" id="mm-verify" ${this.verifyChecksums ? "checked" : ""}> Verify checksums after transfer</label>
            </div>
            <div id="mm-category-filter" style="margin-top:8px;"></div>
        `;
        this.pickerSection.querySelector("#mm-dir-a").addEventListener("change", (e) => {
            this.dirAId = e.target.value;
            this.refreshInventory();
        });
        this.pickerSection.querySelector("#mm-dir-b").addEventListener("change", (e) => {
            this.dirBId = e.target.value;
            this.refreshInventory();
        });
        this.pickerSection.querySelector("#mm-swap").addEventListener("click", () => {
            [this.dirAId, this.dirBId] = [this.dirBId, this.dirAId];
            this.renderPickers();
            this.refreshInventory();
        });
        this.pickerSection.querySelector("#mm-hide-dotfiles").addEventListener("change", (e) => {
            this.hideDotfiles = e.target.checked;
            this.renderGrid();
        });
        this.pickerSection.querySelector("#mm-verify").addEventListener("change", (e) => {
            this.verifyChecksums = e.target.checked;
        });
        this.renderCategoryFilter();
    }

    renderCategoryFilter() {
        const box = this.pickerSection.querySelector("#mm-category-filter");
        if (!box) return;
        if (!this.categories.length) { box.innerHTML = ""; return; }
        // Once something's selected, show only the selected chiclet(s) — with
        // 17+ real categories the full row is too cramped to be useful
        // alongside the grid. Click the selected chiclet again to clear the
        // selection and see the full list again.
        const visible = this.selectedCategories.size
            ? this.categories.filter((c) => this.selectedCategories.has(c))
            : this.categories;
        const chips = visible.map((c) => {
            const active = this.selectedCategories.size === 0 || this.selectedCategories.has(c);
            return `<button class="mm-chiclet" data-cat="${escapeHtml(c)}" style="opacity:${active ? 1 : 0.5};">${escapeHtml(c)}</button>`;
        }).join(" ");
        box.innerHTML = `<div class="mm-label" style="margin-bottom:4px;">Categories (click to toggle, none selected = all)</div>${chips}`;
        box.querySelectorAll("[data-cat]").forEach((btn) => btn.addEventListener("click", () => {
            const c = btn.dataset.cat;
            if (this.selectedCategories.has(c)) this.selectedCategories.delete(c);
            else this.selectedCategories.add(c);
            this.refreshInventory();
        }));
    }

    // Rows after the hide-dotfiles filter — applied before sorting so sort
    // order/positions never include rows the user asked to hide.
    filteredRows() {
        return this.hideDotfiles ? this.rows.filter((r) => !isDotfile(r.relpath)) : this.rows;
    }

    // Click-to-sort headers, applied client-side since every row for the
    // current filter is already loaded in memory — no round-trip needed.
    sortedRows() {
        const dir = this.sortDir;
        const val = {
            relpath: (r) => r.relpath.toLowerCase(),
            category: (r) => r.category.toLowerCase(),
            a: (r) => (r.a ? r.a.size : -1),
            b: (r) => (r.b ? r.b.size : -1),
        }[this.sortKey];
        return [...this.filteredRows()].sort((x, y) => {
            const vx = val(x);
            const vy = val(y);
            if (vx < vy) return -1 * dir;
            if (vx > vy) return 1 * dir;
            return 0;
        });
    }

    setSort(key) {
        if (this.sortKey === key) {
            this.sortDir *= -1;
        } else {
            this.sortKey = key;
            this.sortDir = key === "a" || key === "b" ? -1 : 1; // biggest first for size, A-Z for text
        }
        this.renderGrid();
    }

    renderGrid() {
        const dirA = this.directory(this.dirAId);
        const dirB = this.directory(this.dirBId);
        if (!dirA || !dirB) { this.tableWrap.innerHTML = ""; return; }

        const visibleRows = this.filteredRows();
        if (!visibleRows.length) {
            const reason = this.rows.length ? "every match is hidden by the dotfiles filter" : "no models found in either directory for the current filter";
            this.tableWrap.innerHTML = `<div class="mm-status" style="padding:20px;">${reason}.</div>`;
            this.renderFooter();
            return;
        }

        const linkBadge = (entry) => (entry && entry.linked)
            ? `<span class="mm-badge mm-badge-linked" title="${entry.is_hardlink ? `hard-linked (${entry.nlink}x)` : "symlink"} \u2014 not moved/copied automatically">linked</span>` : "";

        const rowsHtml = this.sortedRows().map((r) => {
            const key = rowKey(r.category, r.relpath);
            const linked = (r.a && r.a.linked) || (r.b && r.b.linked);
            const checked = this.selected.has(key);
            const aCell = r.a ? `${fmtBytes(r.a.size)} ${linkBadge(r.a)}` : "\u2014";
            const bCell = r.b ? `${fmtBytes(r.b.size)} ${linkBadge(r.b)}` : "\u2014";
            const canToA = !!r.b && !linked;
            const canToB = !!r.a && !linked;
            const canDelA = !!r.a && !linked;
            const canDelB = !!r.b && !linked;
            const bothBadge = r.both ? '<span class="mm-badge mm-badge-both" title="exists in both directories">both</span>' : "";
            return `
                <tr data-key="${escapeHtml(key)}">
                    <td><input type="checkbox" class="mm-row-check" ${checked ? "checked" : ""} ${linked ? "disabled title=\"linked entries aren't moved/copied/deleted automatically\"" : ""}></td>
                    <td>${aCell}</td>
                    <td class="mm-actions-cell">
                        <button class="mm-cell-btn" data-act="move" data-dir="btoa" ${canToA ? "" : "disabled"} title="Move ${escapeHtml(dirB.label)} \u2192 ${escapeHtml(dirA.label)}">\u2190</button>
                        <button class="mm-cell-btn mm-cell-btn-copy" data-act="copy" data-dir="btoa" ${canToA ? "" : "disabled"} title="Copy ${escapeHtml(dirB.label)} \u2192 ${escapeHtml(dirA.label)}">\u21d0</button>
                        <button class="mm-cell-btn mm-cell-btn-danger" data-act="delete" data-dir="a" ${canDelA ? "" : "disabled"} title="Delete from ${escapeHtml(dirA.label)}">\u{1F5D1}</button>
                    </td>
                    <td><span class="mm-mono mm-truncate" title="${escapeHtml(r.relpath)}">${escapeHtml(r.relpath)}</span> ${bothBadge}</td>
                    <td>${escapeHtml(r.category)}</td>
                    <td class="mm-actions-cell">
                        <button class="mm-cell-btn mm-cell-btn-danger" data-act="delete" data-dir="b" ${canDelB ? "" : "disabled"} title="Delete from ${escapeHtml(dirB.label)}">\u{1F5D1}</button>
                        <button class="mm-cell-btn mm-cell-btn-copy" data-act="copy" data-dir="atob" ${canToB ? "" : "disabled"} title="Copy ${escapeHtml(dirA.label)} \u2192 ${escapeHtml(dirB.label)}">\u21d2</button>
                        <button class="mm-cell-btn" data-act="move" data-dir="atob" ${canToB ? "" : "disabled"} title="Move ${escapeHtml(dirA.label)} \u2192 ${escapeHtml(dirB.label)}">\u2192</button>
                    </td>
                    <td>${bCell}</td>
                </tr>`;
        }).join("");

        const sortMark = (key) => this.sortKey === key ? (this.sortDir === 1 ? " ▲" : " ▼") : "";
        this.tableWrap.innerHTML = `
            <table class="mm-grid">
                <thead><tr>
                    <th><input type="checkbox" id="mm-check-all"></th>
                    <th class="mm-sortable" data-sort="a" title="Sort by size">${escapeHtml(dirA.label)}${sortMark("a")}</th>
                    <th>Actions</th>
                    <th class="mm-sortable" data-sort="relpath" title="Sort by name">Model${sortMark("relpath")}</th>
                    <th class="mm-sortable" data-sort="category" title="Sort by category">Category${sortMark("category")}</th>
                    <th>Actions</th>
                    <th class="mm-sortable" data-sort="b" title="Sort by size">${escapeHtml(dirB.label)}${sortMark("b")}</th>
                </tr></thead>
                <tbody>${rowsHtml}</tbody>
            </table>`;

        this.tableWrap.querySelectorAll("th.mm-sortable").forEach((th) =>
            th.addEventListener("click", () => this.setSort(th.dataset.sort)));
        this.tableWrap.querySelector("#mm-check-all").addEventListener("change", (e) => {
            this.tableWrap.querySelectorAll(".mm-row-check:not(:disabled)").forEach((cb) => { cb.checked = e.target.checked; });
            this.syncSelectionFromDom();
        });
        this.tableWrap.querySelectorAll(".mm-row-check").forEach((cb) =>
            cb.addEventListener("change", () => this.syncSelectionFromDom()));
        this.tableWrap.querySelectorAll("[data-act]").forEach((btn) =>
            btn.addEventListener("click", (e) => {
                const tr = e.target.closest("tr");
                const item = parseRowKey(tr.dataset.key);
                if (btn.dataset.act === "delete") this.confirmDelete([item], btn.dataset.dir);
                else this.performAction(btn.dataset.act, btn.dataset.dir, [item]);
            }));

        this.syncSelectionFromDom();
        this.renderFooter();
    }

    syncSelectionFromDom() {
        this.selected.clear();
        this.tableWrap.querySelectorAll("tr[data-key]").forEach((tr) => {
            const cb = tr.querySelector(".mm-row-check");
            if (cb && cb.checked) this.selected.add(tr.dataset.key);
        });
        this.renderFooter();
    }

    // Resolves the current selection back to row data (size/linked flags per
    // side), so the footer can tell which bulk actions actually make sense
    // for what's checked right now — not just "something is checked".
    selectedRows() {
        return [...this.selected]
            .map((k) => {
                const { category, relpath } = parseRowKey(k);
                return this.rows.find((r) => r.category === category && r.relpath === relpath);
            })
            .filter(Boolean);
    }

    renderFooter() {
        const sel = this.selectedRows();
        const n = sel.length;
        const isLinked = (r) => (r.a && r.a.linked) || (r.b && r.b.linked);
        // A bulk action is only enabled when EVERY selected row supports it —
        // sending a mixed batch would just fail the whole thing server-side
        // on the first unsupported row (plan_items aborts on first error).
        const canA = n > 0 && sel.every((r) => r.a && !isLinked(r)); // has a source in A
        const canB = n > 0 && sel.every((r) => r.b && !isLinked(r)); // has a source in B
        const tempCount = this.staleTempCount || 0;
        const tempLabel = tempCount > 0 ? `Clean temp files (${tempCount})` : "Clean temp files";
        const jobActive = !!this.jobId;
        const dirALabel = this.directory(this.dirAId)?.label || "A";
        const dirBLabel = this.directory(this.dirBId)?.label || "B";
        // Order and labeling mirror the per-row action cells (see renderGrid):
        // move/copy "into A" on the left, delete pair innermost, move/copy
        // "into B" on the right \u2014 same side-vs-side logic, and "selected"/
        // "(A)"/"(B)" are dropped since the leading status span already says
        // how many rows are selected and position now says which side.
        this.footer.innerHTML = `
            <span class="mm-status" id="mm-selection-status">${n ? `${n} selected` : "No rows selected"}</span>
            <button class="mm-btn" id="mm-bulk-move-btoa" ${canB ? "" : "disabled"} title="Move into ${escapeHtml(dirALabel)}">\u2190 Move</button>
            <button class="mm-btn mm-btn-copy" id="mm-bulk-copy-btoa" ${canB ? "" : "disabled"} title="Copy into ${escapeHtml(dirALabel)}">\u21d0 Copy</button>
            <button class="mm-btn mm-btn-danger" id="mm-bulk-delete-a" ${canA ? "" : "disabled"} title="Delete from ${escapeHtml(dirALabel)}">\u{1F5D1}</button>
            <button class="mm-btn mm-btn-danger" id="mm-bulk-delete-b" ${canB ? "" : "disabled"} title="Delete from ${escapeHtml(dirBLabel)}">\u{1F5D1}</button>
            <button class="mm-btn mm-btn-copy" id="mm-bulk-copy-atob" ${canA ? "" : "disabled"} title="Copy into ${escapeHtml(dirBLabel)}">Copy \u21d2</button>
            <button class="mm-btn" id="mm-bulk-move-atob" ${canA ? "" : "disabled"} title="Move into ${escapeHtml(dirBLabel)}">Move \u2192</button>
            <div class="mm-progress" id="mm-progress" style="display:${jobActive ? "block" : "none"};"><div class="mm-progress-fill"></div></div>
            <span class="mm-status" id="mm-job-status"></span>
            <button class="mm-btn mm-btn-danger" id="mm-cancel-job" style="display:${jobActive ? "inline-block" : "none"};">Cancel</button>
            <button class="mm-btn" id="mm-clean-temp" ${tempCount > 0 ? "" : "disabled"}
                title="${tempCount > 0 ? "Remove stray temp/partial files (.part, .aria2, .tmp, .incomplete, .crdownload, rsync leftovers) older than 1 hour" : "No stray temp/partial files older than 1 hour found"}">${tempLabel}</button>
        `;
        const items = () => sel.map((r) => ({ category: r.category, relpath: r.relpath }));
        this.footer.querySelector("#mm-bulk-move-atob").addEventListener("click", () => this.performAction("move", "atob", items()));
        this.footer.querySelector("#mm-bulk-copy-atob").addEventListener("click", () => this.performAction("copy", "atob", items()));
        this.footer.querySelector("#mm-bulk-move-btoa").addEventListener("click", () => this.performAction("move", "btoa", items()));
        this.footer.querySelector("#mm-bulk-copy-btoa").addEventListener("click", () => this.performAction("copy", "btoa", items()));
        this.footer.querySelector("#mm-bulk-delete-a").addEventListener("click", () => this.confirmDelete(items(), "a"));
        this.footer.querySelector("#mm-bulk-delete-b").addEventListener("click", () => this.confirmDelete(items(), "b"));
        this.footer.querySelector("#mm-clean-temp").addEventListener("click", () => this.cleanTempFiles());
        this.footer.querySelector("#mm-cancel-job").addEventListener("click", () => this.cancelJob());

        // Hydrate immediately from the last known snapshot rather than
        // waiting up to 700ms for the next poll tick (matters right after
        // renderFooter() rebuilds this DOM from scratch, e.g. on reopen).
        if (jobActive && this.lastJob) this.applyJobToFooter(this.lastJob);
    }

    // ------------------------------------------------------------ actions

    // dir is "atob"/"btoa" for move/copy, or "a"/"b" (which side to act on) for delete.
    async performAction(mode, dir, items, overwrite = false) {
        if (!items.length) return;
        let specs;
        if (mode === "delete") {
            const sourceId = dir === "a" ? this.dirAId : this.dirBId;
            specs = items.map((i) => ({
                category: i.category, relpath: i.relpath,
                source_dir_id: sourceId, mode: "delete",
            }));
        } else {
            const sourceId = dir === "atob" ? this.dirAId : this.dirBId;
            const destId = dir === "atob" ? this.dirBId : this.dirAId;
            specs = items.map((i) => ({
                category: i.category, relpath: i.relpath,
                source_dir_id: sourceId, dest_dir_id: destId,
                mode, overwrite,
            }));
        }
        try {
            const res = await api.fetchApi(`${API}/execute`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ items: specs, verify: this.verifyChecksums }),
            });
            const data = await res.json();
            if (!res.ok) {
                if (res.status === 409 && !overwrite && items.length === 1 && /already exists/.test(data.error || "")) {
                    if (confirm(`${data.error}\n\nOverwrite the existing file?`)) {
                        return this.performAction(mode, dir, items, true);
                    }
                    return;
                }
                throw new Error(data.error || `HTTP ${res.status}`);
            }
            this.jobId = data.job_id;
            this.startPolling();
        } catch (err) {
            const statusEl = this.footer.querySelector("#mm-job-status");
            if (statusEl) { statusEl.textContent = `Error: ${err.message}`; statusEl.classList.add("mm-status-err"); }
        }
    }

    // Permanent and unrecoverable, unlike Move/Copy — always confirm first,
    // with the total size of what's about to go so it's not a guess.
    confirmDelete(items, side) {
        if (!items.length) return;
        const total = items.reduce((sum, i) => {
            const row = this.rows.find((r) => r.category === i.category && r.relpath === i.relpath);
            const entry = row && (side === "a" ? row.a : row.b);
            return sum + (entry?.size || 0);
        }, 0);
        const dirLabel = this.directory(side === "a" ? this.dirAId : this.dirBId)?.label || side;
        const plural = items.length > 1 ? "s" : "";
        const msg = `Permanently delete ${items.length} file${plural} (${fmtBytes(total)}) ` +
            `from ${dirLabel}?\n\nThis cannot be undone.`;
        if (confirm(msg)) this.performAction("delete", side, items);
    }

    // Applies a job snapshot (from either a live poll tick or the cached
    // this.lastJob) to whichever footer DOM currently exists — used both
    // from the poll loop and right after renderFooter() rebuilds it.
    applyJobToFooter(job) {
        const progress = this.footer.querySelector("#mm-progress");
        const fill = progress?.querySelector(".mm-progress-fill");
        const status = this.footer.querySelector("#mm-job-status");
        if (progress) progress.style.display = "block";
        if (fill) fill.style.width = `${job.percent || 0}%`;
        if (status) status.textContent = `${job.status} \u2014 ${fmtBytes(job.bytes_done)} / ${fmtBytes(job.total_bytes)}`;
    }

    startPolling() {
        this.stopPolling();
        const cancelBtn = this.footer.querySelector("#mm-cancel-job");
        if (cancelBtn) cancelBtn.style.display = "inline-block";
        const progress = this.footer.querySelector("#mm-progress");
        if (progress) progress.style.display = "block";

        this.jobTimer = setInterval(async () => {
            if (!this.jobId) return this.stopPolling();
            try {
                const res = await api.fetchApi(`${API}/jobs/${this.jobId}`);
                const job = await res.json();
                this.lastJob = job;
                this.applyJobToFooter(job);
                this.ext?.updateJobWidget(job, () => this.cancelJob());
                if (["done", "error", "cancelled"].includes(job.status)) {
                    this.stopPolling();
                    const cancelBtnNow = this.footer.querySelector("#mm-cancel-job");
                    if (cancelBtnNow) cancelBtnNow.style.display = "none";
                    this.ext?.clearJobWidget();
                    const errs = (job.items || []).filter((i) => i.status === "error");
                    const status = this.footer.querySelector("#mm-job-status");
                    if (errs.length && status) {
                        status.textContent = `${job.status} \u2014 ${errs.length} error(s): ${errs.map((e) => e.error).join("; ")}`;
                        status.classList.add("mm-status-err");
                    }
                    this.jobId = null;
                    this.lastJob = null;
                    this.selected.clear();
                    // refreshStores() (not just refreshInventory()) — a completed
                    // move/copy may have landed a file in a destination category
                    // that wasn't tracked yet; this re-runs auto-tracking (backend
                    // also does this itself on job completion) and re-renders the
                    // directories panel + category chiclets + grid together.
                    await this.refreshStores();
                }
            } catch {
                this.stopPolling();
            }
        }, 700);
    }

    stopPolling() {
        if (this.jobTimer) { clearInterval(this.jobTimer); this.jobTimer = null; }
    }

    async cancelJob() {
        if (!this.jobId) return;
        try { await api.fetchApi(`${API}/jobs/${this.jobId}/cancel`, { method: "POST" }); } catch { /* ignore */ }
    }

    async cleanTempFiles() {
        try {
            const res = await api.fetchApi(`${API}/cleanup`, { method: "POST" });
            const data = await res.json();
            this.staleTempCount = 0;
            this.renderFooter();
            const status = this.footer.querySelector("#mm-job-status");
            if (status) status.textContent = `Removed ${data.removed?.length || 0} stray temp file(s)`;
        } catch (err) {
            alert(`Cleanup failed: ${err.message}`);
        }
    }

    // ---------------------------------------------------- directory admin

    async addDirectory() {
        const label = this.directoriesSection.querySelector("#mm-new-label").value.trim();
        const basePath = this.directoriesSection.querySelector("#mm-new-path").value.trim();
        if (!basePath) return alert("Enter an absolute path first.");
        try {
            const res = await api.fetchApi(`${API}/directories`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ base_path: basePath, label: label || basePath }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
            await this.refreshStores();
        } catch (err) {
            alert(`Could not add directory: ${err.message}`);
        }
    }

    async removeDirectory(dirId) {
        const d = this.directory(dirId);
        if (!confirm(`Un-register "${d?.label}"? Files on disk are not touched.`)) return;
        try {
            const res = await api.fetchApi(`${API}/directories/${encodeURIComponent(dirId)}`, { method: "DELETE" });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
            if (this.dirAId === dirId || this.dirBId === dirId) {
                this.dirAId = null; this.dirBId = null;
            }
            await this.refreshStores();
        } catch (err) {
            alert(`Could not remove directory: ${err.message}`);
        }
    }

    async moveDirectoryPriority(dirId, delta) {
        const ids = this.directories.map((d) => d.id);
        const i = ids.indexOf(dirId);
        const j = i + delta;
        if (i < 0 || j < 0 || j >= ids.length) return;
        [ids[i], ids[j]] = [ids[j], ids[i]];
        try {
            const res = await api.fetchApi(`${API}/directories/reorder`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ dir_ids: ids }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
            await this.refreshStores();
            // Priority order is written straight to extra_model_paths.yaml on
            // the server (see config_writer.reorder_directories) — durable
            // across restarts already. This is just a visible confirmation of
            // that, since it wasn't obvious from the UI before.
            this.flashDirStatus("Priority saved \u2014 persists across restarts \u2713");
        } catch (err) {
            alert(`Could not reorder directories: ${err.message}`);
        }
    }

}

class ModelMoverExtension {
    constructor() {
        this.dialog = null;
        this.buttonGroup = null;
        this.floatingButton = null;
        this.progressWidget = null;
    }

    open() {
        if (!this.dialog) this.dialog = new ModelMoverDialog(this);
        this.dialog.show();
    }

    // A small persistent widget, independent of the modal's own open/closed
    // state, pinned to the corner of the viewport — see core/mover.py Job
    // polling: this mirrors the same job the modal's footer shows, so a slow
    // transfer's progress and Cancel button stay reachable no matter what
    // else you're doing in ComfyUI while it runs.
    createProgressWidget() {
        if (this.progressWidget) return;
        const fill = $el("div", { style: { height: "100%", background: "#4CAF50", width: "0%", transition: "width .2s" } });
        const bar = $el("div", { style: { width: "70px", height: "6px", background: "#333", borderRadius: "3px", overflow: "hidden" } }, [fill]);
        const label = $el("span", { style: { whiteSpace: "nowrap" } });
        const cancelBtn = $el("button", {
            textContent: "\u00d7", title: "Cancel transfer",
            style: {
                background: "none", border: "none", color: "#e57373", cursor: "pointer",
                fontSize: "15px", lineHeight: "1", padding: "0 2px",
            },
        });
        const el = $el("div", {
            parent: document.body,
            style: {
                position: "fixed", top: "10px", right: "10px", zIndex: "100000", display: "none",
                alignItems: "center", gap: "8px", background: "var(--comfy-menu-bg, #202020)",
                border: "1px solid var(--border-color, #555)", borderRadius: "6px",
                padding: "6px 10px", fontSize: "11px", color: "#ddd", boxShadow: "0 2px 8px rgba(0,0,0,.5)",
            },
        }, [label, bar, cancelBtn]);
        this.progressWidget = { el, bar, fill, label, cancelBtn, onCancel: null };
        cancelBtn.addEventListener("click", () => this.progressWidget.onCancel?.());
    }

    updateJobWidget(job, onCancel) {
        this.createProgressWidget();
        const { el, fill, label } = this.progressWidget;
        this.progressWidget.onCancel = onCancel;
        el.style.display = "flex";
        fill.style.width = `${job.percent || 0}%`;
        label.textContent = `Model Mover: ${job.status} ${Math.round(job.percent || 0)}%`;
    }

    clearJobWidget() {
        if (!this.progressWidget) return;
        this.progressWidget.el.style.display = "none";
    }

    setup = async () => {
        this.createProgressWidget();
        try {
            const { ComfyButtonGroup } = await import("../../../scripts/ui/components/buttonGroup.js");
            const { ComfyButton } = await import("../../../scripts/ui/components/button.js");
            this.buttonGroup = new ComfyButtonGroup(
                new ComfyButton({
                    icon: "swap-horizontal",
                    action: () => this.open(),
                    tooltip: "Model Mover \u2014 shuffle models between storage locations",
                    content: "Model Mover",
                    classList: "comfyui-button comfyui-menu-mobile-collapse",
                }).element
            );
            app.menu?.settingsGroup.element.before(this.buttonGroup.element);
        } catch (e) {
            console.warn("Model Mover: falling back to floating button:", e);
            this.floatingButton = $el("button", {
                textContent: "\u{1F4E6} Model Mover",
                title: "Open Model Mover",
                onclick: () => this.open(),
                style: {
                    position: "fixed", top: "10px", right: "150px", zIndex: "10000",
                    backgroundColor: "var(--comfy-input-bg, #353535)", color: "#fff",
                    border: "2px solid var(--primary-color, #007acc)", padding: "8px 14px",
                    borderRadius: "6px", cursor: "pointer", fontSize: "13px", fontWeight: "600",
                },
                parent: document.body,
            });
        }
    };
}

const modelMover = new ModelMoverExtension();

app.registerExtension({
    name: "ComfyUI.ModelMover",
    setup: modelMover.setup,
});
